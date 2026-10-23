"""评测体系测试（T05）：四维评估器 / 加权聚合 / 双门槛 / 报告 / harness / 批量跑分。

纪律：judge 用 FakeJudge（0 真实调用）；沙箱用 FakeSandbox（不依赖 Docker）；
套件用真实 ``evals/suites/default.yaml``（顺带回归套件结构）。

v1.1（docs/07 §4.1 / T1.1）追加：任务 ↔ 产物绑定与「未执行」语义
（``single`` 模式逐条回归 v1.0，``per_task`` 模式不跨项目取材）。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.evaluation import (
    ARCHITECTURE,
    CONVENTION,
    REQUIREMENT,
    RUNNABILITY,
    BatchTarget,
    EvalCheck,
    EvalContext,
    EvalScore,
    EvalSuite,
    EvalTask,
    EvaluationHarness,
    EvaluationReport,
    TaskOut,
    aggregate,
    normalize_weights,
    run_batch,
)
from ymir.evaluation.evaluators import (
    ArchitectureEvaluator,
    ConventionEvaluator,
    JudgeOutcome,
    RequirementConformanceEvaluator,
    RunnabilityEvaluator,
)
from ymir.evaluation.evaluators.architecture import (
    _build_graph,
    _DesignJudgement,
    _find_cycles,
    _layer_rank,
    _module_name,
)
from ymir.evaluation.evaluators.convention import _ConventionJudgement
from ymir.evaluation.evaluators.requirement import _ItemJudgement, _RequirementJudgement
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.storage.repositories import ProjectRepository

SUITES_DIR = PROJECT_ROOT / "evals" / "suites"

PRD_JSON = json.dumps(
    {
        "title": "待办 API",
        "requirements": [
            {"id": "R-1", "title": "新增待办", "description": "POST /todos", "priority": "P0"},
            {"id": "R-2", "title": "查询待办", "description": "GET /todos", "priority": "P1"},
        ],
    },
    ensure_ascii=False,
)

CODE_MAIN = "def create_todo():\n    return {'id': 1}\n"
CODE_ROUTES = "from main import create_todo\n\n\ndef handle():\n    return create_todo()\n"


# --------------------------------------------------------------------- 假实现


class FakeJudge:
    """假 judge：按 schema 类型返回脚本化结果（记录调用参数）。"""

    def __init__(
        self,
        *,
        requirement_score: int = 4,
        requirement_evidence: bool = True,
        convention_score: int = 4,
        design_score: int = 4,
    ) -> None:
        self.requirement_score = requirement_score
        self.requirement_evidence = requirement_evidence
        self.convention_score = convention_score
        self.design_score = design_score
        self.calls: list[dict[str, Any]] = []

    async def judge(self, *, system: str, user: str, schema: type) -> JudgeOutcome:
        self.calls.append({"schema": schema.__name__, "user": user})
        if schema is _RequirementJudgement:
            ids = re.findall(r"^- \[([^\]]+)\]", user, flags=re.MULTILINE)
            evidence = ["main.py: create_todo"] if self.requirement_evidence else []
            data = _RequirementJudgement(
                items=[
                    _ItemJudgement(
                        item_id=item_id,
                        score=self.requirement_score,
                        evidence=list(evidence),
                        rationale="脚本化判定",
                    )
                    for item_id in ids
                ]
            )
        elif schema is _ConventionJudgement:
            data = _ConventionJudgement(
                score=self.convention_score,
                evidence=["命名清晰" if self.requirement_evidence else ""],
                rationale="脚本化判定",
            )
        elif schema is _DesignJudgement:
            data = _DesignJudgement(score=self.design_score, evidence=["分层对齐"], rationale="ok")
        else:  # pragma: no cover - 兜底
            data = schema()
        return JudgeOutcome(
            data=data,
            provider="fake",
            model="fake-judge",
            prompt_tokens=12,
            completion_tokens=24,
            cost_cny=0.0005,
        )


class FakeSandboxSession(SandboxSession):
    """假沙箱会话：按命令内容返回脚本化退出码，记录写入文件。"""

    def __init__(self, *, exit_codes: dict[str, int] | None = None) -> None:
        self._exit_codes = exit_codes or {}
        self.files: dict[str, str] = {}
        self.executed: list[str] = []

    @property
    def sandbox_id(self) -> str | None:
        return "fake-eval-1"

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.RESTRICTED

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        self.executed.append(cmd)
        code = 0
        for key, value in self._exit_codes.items():
            if key in cmd:
                code = value
                break
        return ExecResult(stdout="", stderr="", exit_code=code, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class FakeSandboxProvider:
    """假 provider：复用同一个会话，记录生命周期。"""

    def __init__(self, session: FakeSandboxSession | None = None) -> None:
        self.session_obj = session or FakeSandboxSession()
        self.started = 0
        self.destroyed = 0

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        self.started += 1
        try:
            yield self.session_obj
        finally:
            self.destroyed += 1


class BrokenSandboxProvider:
    """会话获取即失败的 provider（验证评测降级不整体失败）。"""

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        raise RuntimeError("sandbox backend unavailable")
        yield None  # pragma: no cover - 不可达（进入即抛错）


def _ctx(
    *,
    workdir: dict[str, str],
    references: dict[str, dict[str, str]] | None = None,
    checks: list[EvalCheck] | None = None,
    sandbox: SandboxSession | None = None,
    judge: Any = None,
    requirement: str = "",
) -> EvalContext:
    task = EvalTask(id="t", requirement=requirement, checks=checks or [])
    return EvalContext(
        task=task,
        workdir=workdir,
        references=references or {},
        sandbox=sandbox,
        judge=judge,
    )


# --------------------------------------------------------------------- 聚合 / 门槛


class TestAggregator:
    def test_normalize_weights(self) -> None:
        weights = normalize_weights(
            {REQUIREMENT: 3, RUNNABILITY: 3, CONVENTION: 2, ARCHITECTURE: 2}
        )
        assert abs(sum(weights.values()) - 1.0) < 1e-9
        assert weights[REQUIREMENT] == pytest.approx(0.3)

    def test_normalize_all_zero_falls_back_equal(self) -> None:
        zeros = dict.fromkeys([REQUIREMENT, RUNNABILITY, CONVENTION, ARCHITECTURE], 0)
        weights = normalize_weights(zeros)
        assert all(value == pytest.approx(0.25) for value in weights.values())

    def test_double_threshold_pass(self) -> None:
        scores = [
            EvalScore(REQUIREMENT, 90, 0.3),
            EvalScore(RUNNABILITY, 80, 0.3),
            EvalScore(CONVENTION, 80, 0.2),
            EvalScore(ARCHITECTURE, 80, 0.2),
        ]
        result = aggregate(scores)
        assert result.total_score == pytest.approx(83.0)
        assert result.passed is True

    def test_double_threshold_runnability_blocks(self) -> None:
        scores = [
            EvalScore(REQUIREMENT, 100, 0.3),
            EvalScore(RUNNABILITY, 50, 0.3),
            EvalScore(CONVENTION, 100, 0.2),
            EvalScore(ARCHITECTURE, 100, 0.2),
        ]
        result = aggregate(scores)
        assert result.total_score == pytest.approx(85.0)
        assert result.runnability == pytest.approx(50.0)
        assert result.passed is False  # 分高但跑不起来：双门槛拦住


# --------------------------------------------------------------------- 评估器


class TestRequirementEvaluator:
    async def test_deterministic_without_judge(self) -> None:
        evaluator = RequirementConformanceEvaluator(weight=0.3)
        ctx = _ctx(
            workdir={"main.py": "POST /todos 新增待办 查询"},
            references={"prd": {"prd.json": PRD_JSON}},
        )
        score = await evaluator.evaluate(ctx)
        assert 0.0 <= score.score <= 100.0
        assert score.evidence  # 每分必须附证据
        assert score.name == REQUIREMENT

    async def test_judge_path_with_evidence(self) -> None:
        judge = FakeJudge(requirement_score=4, requirement_evidence=True)
        evaluator = RequirementConformanceEvaluator(weight=0.3)
        ctx = _ctx(
            workdir={"main.py": CODE_MAIN},
            references={"prd": {"prd.json": PRD_JSON}},
            judge=judge,
        )
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(80.0)  # 4/5 -> 80
        assert score.tokens > 0 and score.cost_cny > 0

    async def test_missing_evidence_forced_low(self) -> None:
        judge = FakeJudge(requirement_score=5, requirement_evidence=False)
        evaluator = RequirementConformanceEvaluator(weight=0.3)
        ctx = _ctx(
            workdir={"main.py": CODE_MAIN},
            references={"prd": {"prd.json": PRD_JSON}},
            judge=judge,
        )
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(20.0)  # 无证据强制压到 1/5
        assert "压低" in score.rationale

    async def test_no_requirement_returns_zero(self) -> None:
        evaluator = RequirementConformanceEvaluator(weight=0.3)
        score = await evaluator.evaluate(_ctx(workdir={"main.py": "x=1"}))
        assert score.score == 0.0
        assert score.evidence


class TestRunnabilityEvaluator:
    async def test_all_checks_pass(self) -> None:
        session = FakeSandboxSession()
        evaluator = RunnabilityEvaluator(weight=0.3)
        ctx = _ctx(
            workdir={"main.py": CODE_MAIN},
            checks=[
                EvalCheck(type="command", run="python -m pytest -q", weight=0.6),
                EvalCheck(type="command", run="ruff check .", weight=0.4),
            ],
            sandbox=session,
        )
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(100.0)
        assert len(score.evidence) == 2

    async def test_failing_check_lowers_score(self) -> None:
        session = FakeSandboxSession(exit_codes={"ruff": 1})
        evaluator = RunnabilityEvaluator(weight=0.3)
        ctx = _ctx(
            workdir={"main.py": CODE_MAIN},
            checks=[
                EvalCheck(type="command", run="python -m pytest -q", weight=0.6),
                EvalCheck(type="command", run="ruff check .", weight=0.4),
            ],
            sandbox=session,
        )
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(60.0)

    async def test_static_fallback_syntax_error(self) -> None:
        evaluator = RunnabilityEvaluator(weight=0.3)
        ctx = _ctx(workdir={"ok.py": "x = 1\n", "bad.py": "def broken(:\n"})
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(50.0)
        assert any(item.kind == "file" for item in score.evidence)


class TestConventionEvaluator:
    async def test_structure_penalties(self) -> None:
        evaluator = ConventionEvaluator(weight=0.2)
        ctx = _ctx(workdir={"BadName.py": "x = 1\n"})  # 非 snake_case + 无测试
        score = await evaluator.evaluate(ctx)
        assert score.score < 100.0
        assert any("snake_case" in item.detail for item in score.evidence)

    async def test_clean_snake_case_no_tests_evidence(self) -> None:
        # 干净 snake_case + 无 tests + 无沙箱 + 无 judge：仍须有证据（失败规则也留痕）
        evaluator = ConventionEvaluator(weight=0.2)
        score = await evaluator.evaluate(_ctx(workdir={"main.py": CODE_MAIN}))
        assert score.score == pytest.approx(200.0 / 3.0)  # 3 条结构中 2 条通过
        assert score.evidence
        assert any("测试" in item.detail for item in score.evidence)

    async def test_lint_and_judge(self) -> None:
        session = FakeSandboxSession()
        judge = FakeJudge(convention_score=5)
        evaluator = ConventionEvaluator(weight=0.2)
        ctx = _ctx(
            workdir={"main.py": CODE_MAIN, "tests/test_main.py": "def test_x():\n    assert 1\n"},
            sandbox=session,
            judge=judge,
        )
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(100.0)
        assert "ruff check ." in session.executed


class TestArchitectureEvaluator:
    async def test_cycle_detected(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        ctx = _ctx(
            workdir={
                "a.py": "import b\n\n\ndef a():\n    return 1\n",
                "b.py": "import a\n\n\ndef b():\n    return 2\n",
            }
        )
        score = await evaluator.evaluate(ctx)
        assert score.score < 100.0
        assert any("循环依赖" in item.detail for item in score.evidence)

    async def test_clean_structure_full_score(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        ctx = _ctx(
            workdir={"src/main.py": CODE_MAIN, "src/util.py": "def helper():\n    return 1\n"}
        )
        score = await evaluator.evaluate(ctx)
        assert score.score == pytest.approx(100.0)
        # 干净代码也须留正向证据（§12.3 每分必附 evidence）
        assert score.evidence
        assert any(item.kind == "metric" for item in score.evidence)

    async def test_no_python_files_zero(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        score = await evaluator.evaluate(_ctx(workdir={"README.md": "hi"}))
        assert score.score == 0.0

    async def test_layer_violation_nested_dirs(self) -> None:
        # 嵌套目录下层违规（domain/x → api/y，rank2→rank0）必须真实触发扣分
        evaluator = ArchitectureEvaluator(weight=0.2)
        score = await evaluator.evaluate(
            _ctx(
                workdir={
                    "domain/x.py": "import api.y\n\n\ndef x():\n    return 1\n",
                    "api/y.py": "def y():\n    return 2\n",
                }
            )
        )
        assert score.score == pytest.approx(94.0)
        assert any("分层违规" in item.detail for item in score.evidence)

    async def test_frozen_score_constants(self) -> None:
        # 冻结常量（等价性探针）：干净=100 / 1 环=85 / 1 超大文件=95 / 顶层层违规=94
        evaluator = ArchitectureEvaluator(weight=0.2)
        clean = await evaluator.evaluate(_ctx(workdir={"src/main.py": CODE_MAIN}))
        assert clean.score == pytest.approx(100.0)

        cycle = await evaluator.evaluate(
            _ctx(workdir={"a.py": "import b\n", "b.py": "import a\n"})
        )
        assert cycle.score == pytest.approx(85.0)

        oversized = await evaluator.evaluate(_ctx(workdir={"main.py": "x = 1\n" * 810}))
        assert oversized.score == pytest.approx(95.0)

        top_layer = await evaluator.evaluate(
            _ctx(workdir={"domain.py": "import api\n", "api.py": "x = 1\n"})
        )
        assert top_layer.score == pytest.approx(94.0)


# --------------------------------------------------------------------- 报告


def _sample_report() -> EvaluationReport:
    from ymir.evaluation.report import DimensionOut, TaskOut

    dims = [
        DimensionOut(name=REQUIREMENT, label="需求符合度", score=80, weight=0.3, rationale="ok",
                     evidence=[]),
        DimensionOut(name=RUNNABILITY, label="代码可运行率", score=90, weight=0.3, rationale="ok",
                     evidence=[]),
        DimensionOut(name=CONVENTION, label="规范符合度", score=70, weight=0.2, rationale="ok",
                     evidence=[]),
        DimensionOut(name=ARCHITECTURE, label="架构合理性", score=60, weight=0.2, rationale="ok",
                     evidence=[]),
    ]
    task = TaskOut(id="t1", requirement="x", dimensions=dims, total_score=78.0,
                   runnability=90.0, passed=True, thresholds={"total": 75, "runnability": 60})
    from ymir.evaluation.suite import Thresholds

    return EvaluationReport.build(
        suite_id="default",
        suite_version="1.0",
        project_id="p1",
        weights=normalize_weights(None),
        thresholds=Thresholds(),
        tasks=[task],
        wall_time_ms=5,
    )


class TestReport:
    def test_markdown_and_json(self) -> None:
        report = _sample_report()
        md = report.to_markdown()
        assert "需求符合度" in md
        assert "评估成本" not in md  # 用「评测成本」
        assert "评测成本" in md
        parsed = json.loads(report.to_json())
        assert parsed["tasks"][0]["total_score"] == 78.0

    def test_compare_deltas(self) -> None:
        first = _sample_report()
        second = _sample_report()
        second.tasks[0].dimensions[0].score = 70
        second.tasks[0].total_score = 75.0
        second.total_score = 75.0
        delta = first.compare(second)
        assert delta["total_score"] == pytest.approx(3.0)
        assert delta["dimensions"][REQUIREMENT] == pytest.approx(10.0)

    async def test_save_as_artifact(self, session_factory, tmp_path: Path) -> None:
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="report-save")
            await session.commit()
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        report = _sample_report()
        saved = await report.save(store, project_id=project.id)
        assert saved.artifact_type == "report"
        loaded = await store.load(saved.artifact_id)
        assert "evaluation_report.json" in loaded.files
        assert loaded.artifact.name == "evaluation_report"


# --------------------------------------------------------------------- 套件 / harness


class TestSuite:
    def test_load_default(self) -> None:
        suite = EvalSuite.load("default")
        assert suite.weights[REQUIREMENT] == pytest.approx(0.3)
        assert suite.thresholds.total == 75
        assert suite.thresholds.runnability == 60
        assert suite.tasks and suite.tasks[0].checks

    def test_load_missing(self) -> None:
        with pytest.raises(NotFoundError):
            EvalSuite.load("no-such-suite")


async def _seed_project(store: ArtifactStore, session_factory) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="harness")
        await session.commit()
    project_id = project.id
    await store.save(project_id=project_id, name="prd", artifact_type="prd",
                     files={"prd.json": PRD_JSON, "prd.md": "# PRD"})
    await store.save(project_id=project_id, name="design", artifact_type="design",
                     files={"design.md": "# 设计", "design.json": "{}"})
    await store.save(project_id=project_id, name="code", artifact_type="code",
                     files={"main.py": CODE_MAIN, "routes.py": CODE_ROUTES,
                            "tests/test_main.py": "def test_x():\n    assert 1\n"})
    return project_id


class TestHarness:
    async def test_evaluate_task_four_dimensions(self, session_factory, tmp_path: Path) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        task = EvalTask(id="t1", requirement="实现待办 API", artifact_name="code")
        out = await harness.evaluate_task(task, project_id=project_id)
        assert {dim.name for dim in out.dimensions} == {
            REQUIREMENT, RUNNABILITY, CONVENTION, ARCHITECTURE
        }
        assert out.artifact_ref["name"] == "code"
        assert isinstance(out.passed, bool)

    async def test_run_suite_default(self, session_factory, tmp_path: Path) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        provider = FakeSandboxProvider()
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=provider,
            session_factory=session_factory,
        )
        report = await harness.run_suite("default", project_id=project_id)
        assert report.suite_id == "default"
        assert len(report.tasks) == 1
        assert provider.started == provider.destroyed >= 1
        assert report.to_markdown()

    async def test_evaluate_project_via_harness(self, session_factory, tmp_path: Path) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store, judge=FakeJudge(), sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        report = await harness.evaluate_project(project_id, requirement="实现待办 API")
        assert report.tasks[0].id == f"project:{project_id}"

    async def test_sandbox_provider_failure_degrades(self, session_factory, tmp_path: Path) -> None:
        """沙箱会话获取失败 -> 降级为无沙箱判定，评测仍产出完整报告（不整体失败）。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=BrokenSandboxProvider(),
            session_factory=session_factory,
        )
        report = await harness.run_suite("default", project_id=project_id)
        assert len(report.tasks) == 1
        dims = {dim.name: dim for dim in report.tasks[0].dimensions}
        assert set(dims) == {REQUIREMENT, RUNNABILITY, CONVENTION, ARCHITECTURE}
        # runnability 走 ast 语法降级路径（证据含「语法可解析」）
        assert dims[RUNNABILITY].evidence
        assert any("语法可解析" in item.detail for item in dims[RUNNABILITY].evidence)

    async def test_run_batch_value_ranking(self, session_factory, tmp_path: Path) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store, judge=FakeJudge(), sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        batch = await run_batch(
            harness=harness,
            suite_id="default",
            project_id=project_id,
            targets=[BatchTarget(label="cheap", version=1, route="r/cheap", cost_cny=0.01),
                     BatchTarget(label="pricey", version=1, route="r/pricey", cost_cny=0.10)],
        )
        assert len(batch.entries) == 2
        assert batch.best_value() is not None
        assert batch.best_value().label == "cheap"  # 同分更低成本
        assert "性价比" in batch.to_markdown()
        assert "总分" in batch.to_table()


# --------------------------------------------------------------------- API

CODE_FILES = {
    "main.py": CODE_MAIN,
    "tests/test_main.py": "def test_x():\n    assert 1\n",
}


def _install_fake_harness(client) -> FakeSandboxProvider:
    app = client._transport.app
    provider = FakeSandboxProvider()
    app.state.evaluations = EvaluationHarness(
        store=app.state.artifacts,
        judge=FakeJudge(),
        sandbox_provider=provider,
        session_factory=app.state.session_factory,
    )
    return provider


async def _create_project(client, name: str) -> str:
    resp = await client.post("/api/v1/projects", json={"name": name})
    return resp.json()["data"]["id"]


async def test_evaluations_api_flow(client) -> None:
    _install_fake_harness(client)
    project_id = await _create_project(client, "评测 API")
    store = client._transport.app.state.artifacts
    await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.json": PRD_JSON}
    )
    await store.save(project_id=project_id, name="code", artifact_type="code", files=CODE_FILES)

    resp = await client.post(
        f"/api/v1/projects/{project_id}/evaluations",
        json={"suite_id": "default", "artifact_name": "code", "requirement": "实现待办 API"},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["suite_id"] == "default"
    assert len(report["tasks"]) == 1
    assert {d["name"] for d in report["tasks"][0]["dimensions"]} == {
        REQUIREMENT, RUNNABILITY, CONVENTION, ARCHITECTURE
    }

    listing = (await client.get(f"/api/v1/projects/{project_id}/evaluations")).json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["suite_id"] == "default"

    global_listing = (await client.get("/api/v1/evaluations")).json()["data"]
    assert global_listing["total"] >= 1


async def test_evaluation_project_missing_artifact(client) -> None:
    _install_fake_harness(client)
    project_id = await _create_project(client, "空项目")
    resp = await client.post(
        f"/api/v1/projects/{project_id}/evaluations", json={"artifact_name": "code"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_export_zip_api(client) -> None:
    project_id = await _create_project(client, "导出项目")
    store = client._transport.app.state.artifacts
    await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# v1"}
    )

    resp = await client.get(f"/api/v1/projects/{project_id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert any(name.startswith("prd/") for name in names)


async def test_export_project_not_found(client) -> None:
    resp = await client.get("/api/v1/projects/no-such/export")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_evaluation_detail_by_id(client) -> None:
    """GET /evaluations/{id}：报告详情（分数 + 证据链）；不存在 -> 404/1002（ARCH-03）。"""
    _install_fake_harness(client)
    project_id = await _create_project(client, "评测详情")
    store = client._transport.app.state.artifacts
    await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.json": PRD_JSON}
    )
    await store.save(project_id=project_id, name="code", artifact_type="code", files=CODE_FILES)

    await client.post(
        f"/api/v1/projects/{project_id}/evaluations",
        json={"suite_id": "default", "artifact_name": "code", "requirement": "实现待办 API"},
    )
    listing = (await client.get(f"/api/v1/projects/{project_id}/evaluations")).json()["data"]
    assert listing["total"] == 1
    artifact_id = listing["items"][0]["artifact_id"]

    detail = await client.get(f"/api/v1/evaluations/{artifact_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["code"] == 0
    data = detail.json()["data"]
    assert data["suite_id"] == "default"
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["dimensions"]  # 分数 + 证据链

    missing = await client.get("/api/v1/evaluations/no-such-report")
    assert missing.status_code == 404
    assert missing.json()["code"] == 1002


# --------------------------------------------------------------------- 图分析定点（B3）


class TestArchitectureGraphAnalysis:
    """B3：architecture 评估器图分析定点用例（环检测 / 分层判分 / 嵌套路径）。

    覆盖巡检盲区 B3：``_find_cycles`` 环检测与 ``_layer_penalty`` 分层判分的
    独立断言（不经端到端评测间接覆盖），并回归 T05 修过的「嵌套路径匹配」问题。
    """

    def test_module_name_and_layer_rank(self) -> None:
        assert _module_name("a.py") == "a"
        assert _module_name("pkg/__init__.py") == "pkg"
        assert _module_name("pkg/mod.py") == "pkg.mod"
        # 分层：序号越小越靠外
        assert _layer_rank("api.handler") == 0
        assert _layer_rank("services.user") == 1
        assert _layer_rank("domain.x") == 2
        assert _layer_rank("db.session") == 3
        assert _layer_rank("common.util") == 4
        assert _layer_rank("app.main") is None  # 无层名匹配

    def test_find_cycles_none_for_dag(self) -> None:
        # 菱形依赖（非环）：a -> b/c -> d
        graph = {"a.py": {"b.py", "c.py"}, "b.py": {"d.py"}, "c.py": {"d.py"}, "d.py": set()}
        assert _find_cycles(graph) == []

    def test_find_cycles_two_node(self) -> None:
        graph = {"a.py": {"b.py"}, "b.py": {"a.py"}}
        cycles = _find_cycles(graph)
        assert len(cycles) == 1
        assert frozenset(cycles[0]) == frozenset({"a.py", "b.py"})

    def test_find_cycles_self_loop(self) -> None:
        graph = {"a.py": {"a.py"}}
        cycles = _find_cycles(graph)
        assert len(cycles) == 1
        assert cycles[0] == ["a.py", "a.py"]

    def test_find_cycles_two_independent(self) -> None:
        graph = {
            "a.py": {"b.py"},
            "b.py": {"a.py"},
            "c.py": {"d.py"},
            "d.py": {"c.py"},
        }
        assert len(_find_cycles(graph)) == 2

    def test_find_cycles_reports_distinct_cycles(self) -> None:
        # a-b-c-a 与 b-c-b 节点集合不同，分别记录（frozenset 去重以节点集为准）
        graph = {"a.py": {"b.py"}, "b.py": {"c.py"}, "c.py": {"a.py", "b.py"}}
        cycles = _find_cycles(graph)
        assert len(cycles) == 2
        assert {frozenset(cycle) for cycle in cycles} == {
            frozenset({"a.py", "b.py", "c.py"}),
            frozenset({"b.py", "c.py"}),
        }

    # --- 分层判分 ---

    def test_layer_penalty_legal_direction_is_zero(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        # api(rank0) -> domain(rank2)：外层依赖内层，合法
        penalty = evaluator._layer_penalty({"api/y.py": {"domain/x.py"}}, [])
        assert penalty == 0.0

    def test_layer_penalty_same_layer_is_zero(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        assert evaluator._layer_penalty({"domain/a.py": {"domain/b.py"}}, []) == 0.0

    def test_layer_penalty_unmatched_modules_ignored(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        assert evaluator._layer_penalty({"app/main.py": {"app/util.py"}}, []) == 0.0

    def test_layer_penalty_violation_counts(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        evidence: list[Any] = []
        # domain(rank2) -> api(rank0)：低层反向导入外层 = 违规
        penalty = evaluator._layer_penalty({"domain/x.py": {"api/y.py"}}, evidence)
        assert penalty == pytest.approx(6.0)
        assert len(evidence) == 1
        assert "分层违规" in evidence[0].detail

    def test_layer_penalty_is_capped(self) -> None:
        evaluator = ArchitectureEvaluator(weight=0.2)
        graph = {f"domain/m{i}.py": {"api/y.py"} for i in range(6)}
        assert evaluator._layer_penalty(graph, []) == pytest.approx(30.0)

    # --- 嵌套路径（回归 T05「嵌套路径匹配失效」）---

    def test_build_graph_resolves_nested_package(self) -> None:
        py = {
            "api/y.py": "from domain.models import X\n",
            "domain/models.py": "X = 1\n",
        }
        name_to_path = {"api.y": "api/y.py", "domain.models": "domain/models.py"}
        graph = _build_graph(py, name_to_path)
        assert graph["api/y.py"] == {"domain/models.py"}

    def test_build_graph_resolves_package_init(self) -> None:
        py = {"api/y.py": "import domain\n", "domain/__init__.py": "X = 1\n"}
        name_to_path = {"api.y": "api/y.py", "domain": "domain/__init__.py"}
        graph = _build_graph(py, name_to_path)
        assert graph["api/y.py"] == {"domain/__init__.py"}

    def test_build_graph_ignores_self_and_external(self) -> None:
        py = {"a.py": "import a\nimport requests\n"}
        graph = _build_graph(py, {"a": "a.py"})
        assert graph["a.py"] == set()  # 自环不入图；外部依赖不可解析

    async def test_nested_dirs_violation_detected_end_to_end(self) -> None:
        # 嵌套目录下的分层违规必须真实触发扣分（T05 修复回归）
        evaluator = ArchitectureEvaluator(weight=0.2)
        score = await evaluator.evaluate(
            _ctx(
                workdir={
                    "domain/x.py": "import api.y\n\n\ndef x():\n    return 1\n",
                    "api/y.py": "def y():\n    return 2\n",
                }
            )
        )
        assert score.score == pytest.approx(94.0)
        assert any("分层违规" in item.detail for item in score.evidence)


# --------------------------------------------------------------------- T1.1 绑定与跳过


class SpyArtifactStore:
    """产物库代理：记录 ``list_artifacts`` / ``load`` 调用（断言不跨项目取材）。

    只代理 harness 用到的两个读方法，其余行为完全透传真实
    :class:`ArtifactStore`，保证既有语义（版本不可变、血缘）不被测试替身改变。
    """

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self.listed: list[str] = []
        self.loaded: list[str] = []

    async def list_artifacts(self, project_id: str, **kwargs: Any) -> list[Any]:
        self.listed.append(project_id)
        return await self._store.list_artifacts(project_id, **kwargs)

    async def load(self, artifact_id: str, version: int | None = None, **kwargs: Any) -> Any:
        self.loaded.append(artifact_id)
        return await self._store.load(artifact_id, version, **kwargs)


def _limit() -> Any:
    """默认双门槛（延迟导入，避免顶部 import 顺序变动）。"""
    from ymir.evaluation.suite import Thresholds

    return Thresholds()


def _write_suite(directory: Path, suite_id: str, tasks: list[dict[str, Any]]) -> Path:
    """落一个 per_task 套件到临时目录（结构同 ``benchmark-12``，2 条检查）。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{suite_id}.yaml"
    payload = {
        "id": suite_id,
        "version": "1.1",
        "description": "per_task 绑定语义回归用",
        "binding_mode": "per_task",
        "weights": {
            REQUIREMENT: 0.3,
            RUNNABILITY: 0.3,
            CONVENTION: 0.2,
            ARCHITECTURE: 0.2,
        },
        "thresholds": {"total": 75, "runnability": 60},
        "tasks": tasks,
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _task_spec(
    task_id: str, *, project_id: str | None, scenario: str = "rest_crud"
) -> dict[str, Any]:
    """构造一条 per_task 任务声明（未绑定 -> 不写 project_id）。"""
    spec: dict[str, Any] = {
        "id": task_id,
        "scenario": scenario,
        "requirement": f"{task_id} 的需求",
    }
    if project_id is not None:
        spec["project_id"] = project_id
    spec["checks"] = [
        {"type": "command", "run": "python -m pytest -q", "weight": 0.7},
        {"type": "command", "run": "ruff check .", "weight": 0.3},
    ]
    return spec


async def _new_project(session_factory, name: str) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name=name)
        await session.commit()
    return project.id


class TestPerTaskBinding:
    """``per_task`` 绑定语义（docs/07 §4.1.2）：不跨项目、不回退、不虚跑。"""

    async def test_missing_artifact_skipped_without_cross_project(
        self, session_factory, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_a = await _seed_project(store, session_factory)  # 有 code / prd / design
        project_b = await _new_project(session_factory, "无 code 的项目")
        await store.save(
            project_id=project_b, name="prd", artifact_type="prd", files={"prd.json": PRD_JSON}
        )
        spy = SpyArtifactStore(store)
        provider = FakeSandboxProvider()
        judge = FakeJudge()
        harness = EvaluationHarness(
            store=spy, judge=judge, sandbox_provider=provider, session_factory=session_factory
        )
        suites_dir = tmp_path / "suites"
        _write_suite(
            suites_dir,
            "per-task-demo",
            [
                _task_spec("t-a", project_id=project_a),
                _task_spec("t-b", project_id=project_b, scenario="cli_tool"),
            ],
        )

        report = await harness.run_suite(
            "per-task-demo", project_id=project_a, suites_dir=suites_dir
        )

        first, second = report.tasks
        # A 任务：用自己的产物正常评测
        assert first.status == "evaluated"
        assert first.artifact_ref["name"] == "code"
        assert first.project_id == project_a
        # B 任务：有绑定但无 code 产物 -> 未执行（不得借用 A 项目的产物）
        assert second.status == "skipped"
        assert second.skip_reason == "missing_artifact"
        assert second.artifact_ref["name"] is None
        assert second.artifact_ref["artifact_id"] is None
        assert second.project_id == project_b
        assert second.dimensions == []
        assert second.total_score == 0.0
        assert second.cost_cny == 0.0
        assert second.tokens == 0
        # spy：每任务恰取一次产物，且 project_id 与任务绑定一致（run_suite 的
        # project_id 不参与解析 —— 否则 B 任务会取到 A 项目的 code）
        assert spy.listed == [project_a, project_b]
        assert report.executed_count == 1
        assert report.skipped_count == 1
        assert len(report.executed) == 1
        assert len(report.skipped) == 1

    async def test_unbound_task_skips_sandbox_judge_and_lookup(
        self, session_factory, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_a = await _seed_project(store, session_factory)
        spy = SpyArtifactStore(store)
        provider = FakeSandboxProvider()
        judge = FakeJudge()
        harness = EvaluationHarness(
            store=spy, judge=judge, sandbox_provider=provider, session_factory=session_factory
        )
        suites_dir = tmp_path / "suites"
        _write_suite(suites_dir, "per-task-unbound", [_task_spec("t-x", project_id=None)])

        report = await harness.run_suite(
            "per-task-unbound", project_id=project_a, suites_dir=suites_dir
        )

        task = report.tasks[0]
        assert task.status == "skipped"
        assert task.skip_reason == "unbound_task"
        assert task.artifact_ref["name"] is None
        # 短路证明：不启沙箱 / 不调 judge / 不取任何产物（无跨项目取材）
        assert provider.started == 0
        assert judge.calls == []
        assert spy.listed == []
        assert task.cost_cny == 0.0

    async def test_all_skipped_report_rates_are_none(
        self, session_factory, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        suites_dir = tmp_path / "suites"
        _write_suite(
            suites_dir,
            "per-task-empty",
            [
                _task_spec("t-x", project_id=None),
                _task_spec("t-y", project_id=None, scenario="cli_tool"),
            ],
        )

        report = await harness.run_suite("per-task-empty", suites_dir=suites_dir)

        assert [task.skip_reason for task in report.tasks] == ["unbound_task", "unbound_task"]
        assert report.executed_count == 0
        assert report.skipped_count == 2
        # Q3：分母为 0 时必须 None（不能返回 0，避免「0% 通过率」误读）
        assert report.first_pass_rate is None
        assert report.final_pass_rate is None
        assert report.total_score == 0.0
        assert report.passed is False
        assert "未执行" in report.to_markdown()

    async def test_task_ids_subset_keeps_suite_order(
        self, session_factory, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_a = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        suites_dir = tmp_path / "suites"
        _write_suite(
            suites_dir,
            "per-task-subset",
            [
                _task_spec("t-a", project_id=project_a),
                _task_spec("t-b", project_id=project_a, scenario="cli_tool"),
                _task_spec("t-c", project_id=project_a, scenario="data_cleaning"),
            ],
        )

        report = await harness.run_suite(
            "per-task-subset", suites_dir=suites_dir, task_ids=["t-c", "t-a"]
        )

        assert [task.id for task in report.tasks] == ["t-a", "t-c"]

    async def test_single_mode_keeps_default_artifact_fallback(
        self, session_factory, tmp_path: Path
    ) -> None:
        # v1.0 兼容红线：single 模式 artifact_name 未命中 -> 回退 code
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        single_suite = EvalSuite.load("default")

        single = await harness.evaluate_task(
            EvalTask(id="s1", requirement="x", artifact_name="no-such-artifact"),
            project_id=project_id,
            suite=single_suite,
        )
        assert single.status == "evaluated"
        assert single.artifact_ref["name"] == "code"  # 回退生效（v1.0 行为）

        per_task_suite = EvalSuite(id="tmp-per-task", binding_mode="per_task")
        strict = await harness.evaluate_task(
            EvalTask(id="s2", requirement="x", artifact_name="no-such-artifact"),
            project_id=project_id,
            suite=per_task_suite,
        )
        assert strict.status == "skipped"
        assert strict.skip_reason == "missing_artifact"  # 永不回退 _DEFAULT_ARTIFACT

    async def test_single_mode_missing_project_artifact_still_evaluates(
        self, session_factory, tmp_path: Path
    ) -> None:
        # v1.0 行为：项目里连 code 都没有时仍产出「0 分报告」，不得改成 skipped
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _new_project(session_factory, "空产物项目")
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        report = await harness.run_suite("default", project_id=project_id)
        assert report.tasks[0].status == "evaluated"
        assert report.tasks[0].artifact_ref["name"] is None
        assert report.skipped_count == 0


class TestReportBindingFields:
    """报告新增字段的兼容性（旧 JSON 缺字段必须能按默认值解析）。"""

    def test_legacy_json_parses_with_defaults(self) -> None:
        legacy = {
            "suite_id": "default",
            "suite_version": "1.0",
            "project_id": "p1",
            "tasks": [
                {
                    "id": "t1",
                    "requirement": "x",
                    "total_score": 80.0,
                    "runnability": 90.0,
                    "passed": True,
                }
            ],
            "total_score": 80.0,
            "runnability": 90.0,
            "passed": True,
        }
        report = EvaluationReport.model_validate(legacy)
        task = report.tasks[0]
        assert task.status == "evaluated"
        assert task.skip_reason is None
        assert task.scenario == ""
        assert task.project_id is None
        assert task.run_id is None
        assert report.verdict_source == "harness"
        assert report.run_id is None
        assert report.artifact_version is None
        assert report.repair_round is None
        assert report.executed_count == 1
        assert report.skipped_count == 0
        assert report.first_pass_rate == pytest.approx(1.0)

    def test_json_round_trip_keeps_binding_fields(self) -> None:
        from ymir.evaluation.report import TaskOut

        skipped = TaskOut.skipped_task(
            "t1",
            reason="missing_artifact",
            scenario="rest_crud",
            project_id="p-b",
            run_id="run-b",
        )
        report = EvaluationReport.build(
            suite_id="benchmark-12",
            suite_version="1.1",
            project_id=None,
            weights=normalize_weights(None),
            thresholds=_limit(),
            tasks=[skipped],
            wall_time_ms=0,
            run_id="run-a",
            artifact_version=2,
            repair_round=1,
            verdict_source="llm_qa",
        )
        parsed = EvaluationReport.model_validate(json.loads(report.to_json()))
        assert parsed.run_id == "run-a"
        assert parsed.artifact_version == 2
        assert parsed.repair_round == 1
        assert parsed.verdict_source == "llm_qa"
        restored = parsed.tasks[0]
        assert restored.status == "skipped"
        assert restored.skip_reason == "missing_artifact"
        assert restored.scenario == "rest_crud"
        assert restored.project_id == "p-b"
        assert restored.run_id == "run-b"
        assert parsed.skipped_count == 1
        assert parsed.first_pass_rate is None

    def test_pass_rates_always_derived(self) -> None:
        # 通过率是纯派生字段：即使外部先塞数字，refresh_summary() 必须纠正
        passed = TaskOut(id="t1", total_score=90.0, runnability=90.0, passed=True)
        failed = TaskOut(id="t2", total_score=10.0, runnability=10.0, passed=False)
        report = EvaluationReport.build(
            suite_id="benchmark-12",
            suite_version="1.1",
            project_id=None,
            weights=normalize_weights(None),
            thresholds=_limit(),
            tasks=[passed, failed],
            wall_time_ms=0,
        )
        assert report.executed_count == 2
        assert report.first_pass_rate == pytest.approx(0.5)
        assert report.final_pass_rate == pytest.approx(0.5)  # 单次报告两者恒等

        tampered = report.model_copy(update={"first_pass_rate": 1.0, "final_pass_rate": 1.0})
        assert tampered.first_pass_rate == pytest.approx(1.0)  # 已写入（证明字段可写）
        tampered.refresh_summary()
        assert tampered.first_pass_rate == pytest.approx(0.5)  # 被纠正回真值
        assert tampered.final_pass_rate == pytest.approx(0.5)

    def test_pass_rates_none_when_no_executed(self) -> None:
        skipped = [
            TaskOut.skipped_task("t1", reason="unbound_task"),
            TaskOut.skipped_task("t2", reason="missing_artifact"),
        ]
        report = EvaluationReport.build(
            suite_id="benchmark-12",
            suite_version="1.1",
            project_id=None,
            weights=normalize_weights(None),
            thresholds=_limit(),
            tasks=skipped,
            wall_time_ms=0,
        )
        assert report.executed_count == 0
        assert report.skipped_count == 2
        # Q3：分母 0 -> None（不得返回 0，避免「0% 通过率」误读）
        assert report.first_pass_rate is None
        assert report.final_pass_rate is None
        report.refresh_summary()
        assert report.first_pass_rate is None
        assert report.final_pass_rate is None

    def test_legacy_json_derives_rates(self) -> None:
        # 旧报告 JSON 无 rates 字段：解析后必须由 tasks 派生（且不报错）
        legacy = {
            "suite_id": "default",
            "suite_version": "1.0",
            "tasks": [
                {"id": "t1", "total_score": 90.0, "runnability": 90.0, "passed": True},
                {"id": "t2", "total_score": 10.0, "runnability": 10.0, "passed": False},
            ],
        }
        report = EvaluationReport.model_validate(legacy)
        assert report.executed_count == 2
        assert report.skipped_count == 0
        assert report.first_pass_rate == pytest.approx(0.5)
        assert report.final_pass_rate == pytest.approx(0.5)
        # 与 tasks[].passed 自洽（防止派生值与明细脱钩）
        passed_ratio = sum(1 for task in report.tasks if task.passed) / len(report.tasks)
        assert report.first_pass_rate == round(passed_ratio, 6)

    async def test_evaluate_project_shape_unchanged(
        self, session_factory, tmp_path: Path
    ) -> None:
        # v1.0 兼容红线：evaluate_project 签名 / 返回 / 双门槛判定不变
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        report = await harness.evaluate_project(project_id, requirement="实现待办 API")
        out = report.tasks[0]
        assert out.id == f"project:{project_id}"
        assert out.status == "evaluated"
        assert out.artifact_ref["name"] == "code"
        assert out.thresholds == {"total": 75.0, "runnability": 60.0}
        assert report.skipped_count == 0
        assert set(report.thresholds) == {"total", "runnability"}


class TestNoCrossProjectLeakage:
    """跨项目取材的长期回归保护（docs/07 §4.1.2「永不回退到其它项目」）。

    与 ``TestPerTaskBinding`` 互补：那组验证「有绑定但产物缺失」，本组验证两条
    更隐蔽的泄漏路径 ——

    1. 任务未绑定却硬写了**别的项目**的 ``artifact_id``：仍须
       ``unbound_task``，且产物库一次都不被读取（否则等于跨项目取材）；
    2. ``skipped`` 任务的**整条序列化结果**（不只是 ``artifact_ref``）不得出现
       其它项目的产物 id 与文件内容（防止将来把 references 写进报告）。
    """

    async def test_unbound_task_with_foreign_artifact_id_never_touches_store(
        self, session_factory, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_a = await _seed_project(store, session_factory)
        code_a = await store.latest_by_name(project_a, "code")
        assert code_a is not None
        spy = SpyArtifactStore(store)
        provider = FakeSandboxProvider()
        judge = FakeJudge()
        harness = EvaluationHarness(
            store=spy, judge=judge, sandbox_provider=provider, session_factory=session_factory
        )
        suites_dir = tmp_path / "suites"
        hijacked = _task_spec("t-hijacked", project_id=None)
        hijacked["artifact_id"] = code_a.artifact.id  # 指向 A 项目的 code（越权取材试探）
        _write_suite(suites_dir, "per-task-hijacked", [hijacked])

        report = await harness.run_suite(
            "per-task-hijacked", project_id=project_a, suites_dir=suites_dir
        )

        out = report.tasks[0]
        assert out.status == "skipped"
        assert out.skip_reason == "unbound_task"
        assert out.project_id is None
        assert out.artifact_ref == {"artifact_id": None, "name": None, "version": None}
        # 短路证明：产物库、沙箱、judge 三者均未被触达
        assert spy.listed == []
        assert spy.loaded == []
        assert provider.started == 0
        assert judge.calls == []
        dumped = json.dumps(out.model_dump(mode="json"), ensure_ascii=False)
        assert code_a.artifact.id not in dumped
        assert "create_todo" not in dumped  # A 项目 code 的内容

    async def test_skipped_task_payload_carries_no_foreign_content(
        self, session_factory, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_a = await _seed_project(store, session_factory)  # 有 code
        project_b = await _new_project(session_factory, "无 code 的项目")
        await store.save(
            project_id=project_b, name="prd", artifact_type="prd", files={"prd.json": PRD_JSON}
        )
        code_a = await store.latest_by_name(project_a, "code")
        assert code_a is not None
        spy = SpyArtifactStore(store)
        harness = EvaluationHarness(
            store=spy,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )
        suites_dir = tmp_path / "suites"
        _write_suite(
            suites_dir,
            "per-task-leak-check",
            [
                _task_spec("t-a", project_id=project_a),
                _task_spec("t-b", project_id=project_b, scenario="cli_tool"),
            ],
        )

        report = await harness.run_suite(
            "per-task-leak-check", project_id=project_a, suites_dir=suites_dir
        )

        # 每任务只读自己的项目（run_suite 传入的 A 不参与 B 的解析）
        assert spy.listed == [project_a, project_b]
        skipped = report.tasks[1]
        dumped = json.dumps(skipped.model_dump(mode="json"), ensure_ascii=False)
        assert code_a.artifact.id not in dumped
        for fragment in ("create_todo", "CODE_MAIN", "routes.py"):
            assert fragment not in dumped


# ------------------------------------------- 模板评测口径接入（v1.2 · T1.3）


class TestTemplateChecksInjection:
    """DoD ①：模板 ``checks`` 覆盖套件 ``checks``；无模板时与套件逐条一致。"""

    def _gate(self) -> Any:
        from ymir.evaluation.gate import HarnessEvaluationGate

        return HarnessEvaluationGate(harness=EvaluationHarness(store=object()), store=object())

    def test_checks_follow_template_and_fall_back_to_suite(self) -> None:
        from types import SimpleNamespace

        from ymir.templates import load_template

        gate = self._gate()
        spec = load_template("fastapi-vue3")
        run = SimpleNamespace(
            id="run-tpl", context={"template": {"id": "fastapi-vue3", "version": "1.0"}}
        )
        base = EvalTask(
            id="t", checks=[EvalCheck(type="command", run="suite-only-cmd", weight=3)]
        )

        checks = gate._resolve_checks(run, base)
        assert [item.run for item in checks] == [item.run for item in spec.checks]
        assert [item.weight for item in checks] == [item.weight for item in spec.checks]
        # 字段名是 type（不是 kind）
        assert {item.type for item in checks} == {"command"}

        # 无模板 → 与套件逐条一致
        plain = gate._resolve_checks(SimpleNamespace(id="run-plain", context={}), base)
        assert plain == list(base.checks)
        assert [item.run for item in plain] == ["suite-only-cmd"]

        # 模板不可读 → 退回套件（只告警，不让裁决失败）
        missing = gate._resolve_checks(
            SimpleNamespace(id="run-missing", context={"template": {"id": "no-such"}}), base
        )
        assert [item.run for item in missing] == ["suite-only-cmd"]

    def test_layout_resolved_only_for_template_runs(self) -> None:
        from types import SimpleNamespace

        gate = self._gate()
        assert gate._resolve_layout(SimpleNamespace(id="r0", context={})) is None
        layout = gate._resolve_layout(
            SimpleNamespace(id="r1", context={"template": {"id": "fastapi-vue3"}})
        )
        assert layout is not None
        assert layout.required_dirs == ["app", "tests", "web/src"]
        # 前缀带尾斜杠（否则 app 会误匹配 application/）
        assert layout.allowed_prefixes == ["app/", "tests/", "web/"]


class TestConventionLayoutParameterization:
    """DoD ②③：``layout is None`` 分数与 v1.1 完全相同；``layout`` 非空走模板规则。"""

    async def test_layout_none_keeps_v11_structure_score(self) -> None:
        """§10-13 兼容契约：同输入同分数（v1.1 规则 = 命名 + 文件大小 + 测试约定）。"""
        from ymir.evaluation.evaluators.convention import ConventionEvaluator

        workdir = {"app.py": "x = 1\n", "README.md": "# x\n"}
        ctx = _ctx(workdir=workdir)  # layout=None
        score = await ConventionEvaluator(weight=0.2).evaluate(ctx)

        # v1.1：naming 通过 + 无超大文件 通过 + 无测试 不通过 = 2/3
        assert score.score == pytest.approx(100 * 2 / 3, abs=0.01)
        assert "结构规范 2/3" in score.rationale

    async def test_layout_rules_change_structure_verdict(self) -> None:
        from ymir.common.contracts import LayoutRules
        from ymir.evaluation.evaluators.convention import ConventionEvaluator

        layout = LayoutRules(
            required_dirs=["app"],
            required_files=["pyproject.toml"],
            snake_case_py=True,
            max_file_lines=50,
            allowed_prefixes=["app/"],
        )
        ctx = EvalContext(
            task=EvalTask(id="t"),
            workdir={"other.py": "x = 1\n"},  # 缺 app/、缺 pyproject、路径越界
            layout=layout,
        )
        score = await ConventionEvaluator(weight=0.2).evaluate(ctx)

        assert "模板结构规范" in score.rationale
        refs = {item.ref for item in score.evidence}
        assert {"app", "pyproject.toml", "allowed_prefixes"} <= refs
        assert any("缺失" in item.detail for item in score.evidence)
        # 模板规则 5 条：缺 app/、缺 pyproject、路径越界 3 条不通过；
        # 命名 snake_case 与文件大小 2 条通过 → 2/5 = 40（v1.1 口径下同输入是 66.67）
        assert "模板结构规范 2/5" in score.rationale
        assert score.score == pytest.approx(40.0, abs=0.01)

    async def test_frontend_rules_marked_static_only(self) -> None:
        """DoD ③：``frontend`` 命中时证据标注「静态检查（未构建）」。"""
        from ymir.common.contracts import FrontendRules, LayoutRules
        from ymir.evaluation.evaluators.convention import ConventionEvaluator

        layout = LayoutRules(
            frontend=FrontendRules(
                required_dirs=["web/src"],
                required_glob=["web/src/**/*.vue"],
                forbidden_imports=["jquery"],
            )
        )
        ctx = EvalContext(
            task=EvalTask(id="t"),
            workdir={"web/src/App.vue": "<template><div/></template>\n"},
            layout=layout,
        )
        score = await ConventionEvaluator(weight=0.2).evaluate(ctx)
        frontend_evidence = [
            item for item in score.evidence if "静态检查（未构建）" in item.detail
        ]
        assert frontend_evidence, [item.detail for item in score.evidence]


def test_sandbox_dockerfile_has_v12_packages() -> None:
    """DoD ④：Dockerfile 三处改动静态核对（**真机 docker build 待用户手动验证**）。

    CI 不跑构建（团队约定），因此这里只钉住文本形态：预装清单 / Node 注释 / pip 行。
    """
    text = (PROJECT_ROOT / "docker" / "sandbox.Dockerfile").read_text(encoding="utf-8")
    for package in (
        "fastapi>=0.115",
        "uvicorn>=0.30",
        # [asyncio] extra 提供 greenlet —— 缺了 FastAPI 异步 session 一 import 就报
        # "The SQLAlchemy asyncio module requires greenlet"（T5.1 真跑实测）
        "sqlalchemy[asyncio]>=2.0",
        "aiosqlite>=0.20",
        "alembic>=1.13",
        # 模型为 FastAPI 生成的 conftest 常规会 import pytest_asyncio，
        # 缺了 collection 直接 exit 4（T5.1 真跑实测）
        "pytest-asyncio>=0.23,<1",
    ):
        assert package in text, package
    assert "pytest / pytest-cov / httpx / pydantic" in text  # :7 预装清单已同步
    assert "Node 预装与前端构建校验留待 v1.3 镜像变体" in text
    assert "v1.2 前端只做静态检查" in text


def test_find_cycles_is_independent_of_node_order() -> None:
    """QA-复验：DFS **起点顺序**不得影响环条数（否则跨进程不可复现）。

    生产图的邻接集来自 ``set``，dict 插入序受 ``PYTHONHASHSEED`` 影响；同一张图
    ``a→b→c→a`` 且 ``c→b`` 在不同起点下会数出 2 或 1 条环。修复方式是
    ``_find_cycles`` 内部按 ``sorted(graph)`` 固定起点顺序。
    """
    forward = {"a": {"b"}, "b": {"c"}, "c": {"a", "b"}}
    backward = {"c": {"b", "a"}, "b": {"c"}, "a": {"b"}}
    assert _find_cycles(forward) == _find_cycles(backward)
    assert len(_find_cycles(forward)) == 2  # a→b→c→a 与 b→c→b


def test_find_cycles_is_repeatable_within_process() -> None:
    """同一图**重复调用**必须返回逐项相同的列表（顺序也相同，不只是长度相同）。

    环数直接进 ``architecture`` 维度的 20% 权重，一旦列表顺序/条数抖动，
    "同代码同输入同分数"的基线承诺就破了。邻接集用 ``set`` 构造（真实生产形态），
    刻意放多个环 + 一个悬挂节点。
    """
    graph: dict[str, set[str]] = {
        "a": {"b", "d"},
        "b": {"c"},
        "c": {"a", "b"},
        "d": set(),
        "e": {"f"},
        "f": {"e"},
    }
    baseline = _find_cycles(graph)
    for _ in range(5):
        assert _find_cycles(graph) == baseline
    assert len(baseline) == 3  # a→b→c→a、b→c→b、e→f→e


def test_find_cycles_is_stable_across_hash_seeds() -> None:
    """跨进程（不同 ``PYTHONHASHSEED``）结果必须**完全相同**。

    进程内无法改 hash 种子，因此真的起两个子进程跑：``set`` 的迭代序随种子变化，
    若 DFS 起点沿用 dict 插入序，同一张图会数出不同的环数 —— 这正是本用例要挡住的
    回归（修复方式：``for node in sorted(graph)``）。
    """
    import os
    import subprocess  # noqa: S404 - 固定脚本、无 shell、无外部输入
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        from ymir.evaluation.evaluators.architecture import _find_cycles

        # 用 set 构造邻接集（与生产一致）：迭代序随 PYTHONHASHSEED 变化
        edges = {("a", "b"), ("b", "c"), ("c", "a"), ("c", "b"), ("x", "y"), ("y", "x")}
        nodes = {src for src, _ in edges} | {dst for _, dst in edges}
        graph = {node: set() for node in nodes}
        for src, dst in edges:
            graph[src].add(dst)
        print(repr(_find_cycles(graph)))
        """
    )
    outputs: list[str] = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        completed = subprocess.run(  # noqa: S603 - 固定脚本
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout.strip())
    assert len(set(outputs)) == 1, f"不同 hash 种子下环列表不一致: {outputs}"
    assert outputs[0].count("["), outputs[0]  # 至少确实返回了环
