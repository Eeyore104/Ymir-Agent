"""评测体系测试（T05）：四维评估器 / 加权聚合 / 双门槛 / 报告 / harness / 批量跑分。

纪律：judge 用 FakeJudge（0 真实调用）；沙箱用 FakeSandbox（不依赖 Docker）；
套件用真实 ``evals/suites/default.yaml``（顺带回归套件结构）。
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
from ymir.evaluation.evaluators.architecture import _DesignJudgement
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
