"""v1.5 T01：判据来源（唯一装配点 + 报告自证）的钉子。

**要防的裂缝（docs/17 §1.1）**

v1.4 里「把产物装配成 ``EvalTask``」有两份实现：gate 走
``run_task.build_task_for_run``（模板 ``checks``），门补评测走
``harness.evaluate_project`` 里自拼的一份（``checks = suite.tasks[0].checks``）。
套件 checks **没有** ``blocking`` / ``group`` ⇒ 补评测的 ``blocking_failed`` 恒空
⇒ 只要任务 ``evaluated`` 就是 ``healthy``。于是"对一棵已被判坏的树点一次补评测"
就能让闸门放行，而且响应里没有 warning、日志里没有记录 —— **静默旁路**。

本文件钉住三件事：

1. **同尺**（T01.1）：``build_task_for_project`` 与 ``build_task_for_run`` 对同一
   ``(project, artifact, version, template)`` 产出的 ``checks`` **逐字段相等**；
2. **三档回退**（T01.1）：归属 run 的模板 > 产物 ``patch_manifest.provenance`` >
   套件口径（显式 ``criteria_source="suite"``，绝不静默留 ``null``）；
3. **报告自证**（T01.3 / T01.5）：判据三键由 ``EvaluationReport.build`` 从
   ``tasks[0]`` 派生（**caller 传不进来**），且默认值一律 pop（A19 字节契约）。

测试纪律：judge 用 ``FakeJudge``、沙箱用 ``FakeSandboxProvider``（0 真实调用、
0 Docker）；模板用**真实的** ``fastapi-vue3``（指纹才有真值）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ymir.artifacts.provenance import template_yaml_hash
from ymir.artifacts.store import ArtifactStore
from ymir.evaluation.gate import HarnessEvaluationGate
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.report import (
    CRITERIA_SOURCE_SUITE,
    CRITERIA_SOURCE_TEMPLATE,
    EvaluationReport,
    TaskOut,
)
from ymir.evaluation.run_task import (
    TaskCriteria,
    build_run_report,
    build_task_for_project,
    build_task_for_run,
    criteria_for_template,
)
from ymir.evaluation.suite import EvalSuite, Thresholds
from ymir.models.entities import PipelineRun
from ymir.storage.repositories import ProjectRepository
from ymir.templates import DEFAULT_TEMPLATES_DIR, load_template

TEMPLATE_ID = "fastapi-vue3"
#: 真实流水线 id（``repair.eval.suite = default``）⇒ 套件解析可复现
PIPELINE_ID = "software-dev"

#: ``EvalCheck`` 里所有**参与判定**的字段（T2-R1 点名的「逐字段相等」清单）
CHECK_FIELDS: tuple[str, ...] = (
    "type",
    "run",
    "weight",
    "group",
    "blocking",
    "requires",
    "timeout_s",
    "requires_files",
)


def _run(*, context: dict[str, Any] | None = None) -> PipelineRun:
    """内存里的 run（**不落库**）：装配只需要 id / project_id / context / pipeline_id。"""
    return PipelineRun(
        id="run-v15",
        project_id="proj-v15",
        pipeline_id=PIPELINE_ID,
        context=context
        if context is not None
        else {"requirement": "加导出接口", "template": {"id": TEMPLATE_ID}},
    )


def _check_signature(checks: list[Any]) -> list[tuple[Any, ...]]:
    """把 checks 压成可比对的元组列表（逐字段，不靠对象同一性）。"""
    return [tuple(getattr(check, field) for field in CHECK_FIELDS) for check in checks]


def _manifest_files(template_id: str, template_hash: str) -> dict[str, str]:
    """最小可解析的 ``patch_manifest.json``（只有溯源相关字段有意义）。"""
    return {
        "patch_manifest.json": json.dumps(
            {
                "mode": "patch",
                "provenance": {
                    "template_id": template_id,
                    "template_hash": template_hash,
                },
            }
        )
    }


# ============================================================ T01.1 · 同尺


class TestSingleAssemblyPoint:
    """T01.1：两个入口共用 ``_assemble`` ⇒ 同一输入的 ``checks`` 逐字段相等。"""

    def test_project_builder_matches_run_builder_field_by_field(self) -> None:
        """★ 本版最核心的一条：补评测与 gate 用**同一把尺子**。"""
        run_task, run_suite = build_task_for_run(_run(), artifact_name="code")
        project_task, project_suite, criteria = build_task_for_project(
            project_id="proj-v15",
            artifact_name="code",
            version=3,
            template_id=TEMPLATE_ID,
            run_id="run-v15",
        )

        assert run_suite.id == project_suite.id == "default"
        assert _check_signature(project_task.checks) == _check_signature(run_task.checks)
        # 装配的其余同源件（layout / scoring / 骨架）也必须一致
        assert project_task.layout == run_task.layout
        assert project_task.scoring == run_task.scoring
        assert project_task.baseline_skeleton == run_task.baseline_skeleton
        # 判据三元组同源
        assert (project_task.criteria_source, project_task.template_id) == (
            run_task.criteria_source,
            run_task.template_id,
        )
        assert project_task.criteria_version == run_task.criteria_version
        assert criteria == TaskCriteria(
            template_id=TEMPLATE_ID,
            criteria_source=CRITERIA_SOURCE_TEMPLATE,
            criteria_version=project_task.criteria_version,
        )

    def test_checks_come_from_the_template_not_the_suite(self) -> None:
        """模板 ``checks`` 覆盖套件 ``checks``（含 ``blocking`` / ``group`` / ``tier``）。"""
        task, _suite, _criteria = build_task_for_project(
            project_id="proj-v15", template_id=TEMPLATE_ID
        )
        spec = load_template(TEMPLATE_ID)
        assert [check.run for check in task.checks] == [check.run for check in spec.checks]
        blocking = [check.run for check in task.checks if check.blocking]
        # v1.5 T03：模板把 D1/D2 全部标成阻断 ⇒ 阻断项不再只有前端构建
        assert "fe-build web" in blocking
        assert "python -m pytest -q" in blocking
        # 套件 checks 一条 blocking 都没有（这是"弱口径"的根源）
        suite = EvalSuite.load("default")
        assert not any(check.blocking for check in suite.tasks[0].checks)
        assert [check.run for check in suite.tasks[0].checks] != [
            check.run for check in task.checks
        ]

    def test_version_and_artifact_name_are_carried(self) -> None:
        """目标产物版本必须进 task（补评测要评**这一版**，不是 latest）。"""
        task, _suite, _criteria = build_task_for_project(
            project_id="proj-v15", artifact_name="code", version=7, template_id=TEMPLATE_ID
        )
        assert task.artifact_name == "code"
        assert task.version == 7
        assert task.id == "project:proj-v15"
        # ★ 刻意**不**把 run_id 写进 EvalTask（会牵动基线解析 / judge 成本归属）
        assert task.run_id is None

    def test_empty_requirement_is_blank_not_none(self) -> None:
        task, _suite, _criteria = build_task_for_project(project_id="proj-v15")
        assert task.requirement == ""


# ============================================================ T01.1 · 三档回退


class TestCriteriaFallbackTiers:
    """T01.1：判据来源三档回退（docs/17 §1.1.3 候选③）。"""

    def test_tier1_caller_template_wins(self) -> None:
        """① 调用方反查的模板 id 最高优先级，且 ``criteria_version`` **现算**。"""
        task, _suite, criteria = build_task_for_project(
            project_id="proj-v15",
            template_id=TEMPLATE_ID,
            # 故意给一个假的产物溯源（更弱的来源），证明第 1 档真的赢了
            manifest_files=_manifest_files("python-cli", "0" * 64),
        )
        assert criteria.template_id == TEMPLATE_ID
        assert criteria.criteria_source == CRITERIA_SOURCE_TEMPLATE
        assert criteria.criteria_version == template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)
        assert [check.run for check in task.checks] == [
            check.run for check in load_template(TEMPLATE_ID).checks
        ]

    def test_tier2_manifest_provenance_is_used_when_no_caller_template(self) -> None:
        """② 没有调用方模板 ⇒ 读产物 ``patch_manifest.provenance``。

        ★ ``criteria_version`` 取**产物里记的** ``template_hash``（"当时那把尺子"），
        不是现算值 —— 这样"模板后来改过"会被读取侧抓成"判据不同源"。
        """
        recorded = "a" * 64
        _task, _suite, criteria = build_task_for_project(
            project_id="proj-v15",
            manifest_files=_manifest_files(TEMPLATE_ID, recorded),
        )
        assert criteria.template_id == TEMPLATE_ID
        assert criteria.criteria_source == CRITERIA_SOURCE_TEMPLATE
        assert criteria.criteria_version == recorded
        assert recorded != template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)

    def test_tier3_suite_is_explicit_not_null(self) -> None:
        """③ 都拿不到 ⇒ **显式** ``suite`` + 空串（A12：jq 不返回 null）。"""
        task, suite, criteria = build_task_for_project(project_id="proj-v15")
        assert criteria == TaskCriteria()
        assert criteria.criteria_source == CRITERIA_SOURCE_SUITE
        assert criteria.template_id == ""
        assert criteria.criteria_version == ""
        # 套件口径 ⇒ 逐条沿用套件 checks（= v1.4 行为，零行为变化）
        assert _check_signature(task.checks) == _check_signature(suite.tasks[0].checks)

    def test_run_without_template_is_suite_criteria(self) -> None:
        """run 没有 ``context.template`` ⇒ 套件口径（不是"三键缺失"）。"""
        task, _suite = build_task_for_run(_run(context={"requirement": "x"}))
        assert task.criteria_source == CRITERIA_SOURCE_SUITE
        assert task.template_id == ""
        assert task.criteria_version == ""

    def test_unreadable_template_degrades_to_suite(self) -> None:
        """模板不可读 ⇒ 退化到第 3 档，**不让评测本身失败**（§1.1.6 失败降级）。"""
        task, _suite, criteria = build_task_for_project(
            project_id="proj-v15", template_id="no-such-template"
        )
        assert criteria.criteria_source == CRITERIA_SOURCE_SUITE
        assert task.layout is None
        assert task.checks  # 仍然有套件 checks 可跑

    def test_corrupt_manifest_degrades_to_suite(self) -> None:
        """损坏的 ``patch_manifest`` ⇒ 同样落第 3 档（绝不抛）。"""
        _task, _suite, criteria = build_task_for_project(
            project_id="proj-v15",
            manifest_files={"patch_manifest.json": "{not json"},
        )
        assert criteria.criteria_source == CRITERIA_SOURCE_SUITE

    def test_manifest_without_template_id_degrades_to_suite(self) -> None:
        """``from_scratch`` 产物（无 ``provenance``）⇒ 套件口径。"""
        _task, _suite, criteria = build_task_for_project(
            project_id="proj-v15",
            manifest_files={"patch_manifest.json": json.dumps({"mode": "from_scratch"})},
        )
        assert criteria.criteria_source == CRITERIA_SOURCE_SUITE

    def test_criteria_for_template_is_the_only_writer(self) -> None:
        """``criteria_for_template`` 是判据三元组的**唯一**写入口（纯函数）。"""
        spec = load_template(TEMPLATE_ID)
        assert criteria_for_template("", spec).criteria_source == CRITERIA_SOURCE_SUITE
        assert criteria_for_template(TEMPLATE_ID, None).criteria_source == CRITERIA_SOURCE_SUITE
        bound = criteria_for_template(TEMPLATE_ID, spec)
        assert bound.criteria_source == CRITERIA_SOURCE_TEMPLATE
        assert len(bound.criteria_version) == 64
        # 给了已知指纹就**不现算**（产物溯源那一路）
        known = criteria_for_template(TEMPLATE_ID, spec, template_hash="b" * 64)
        assert known.criteria_version == "b" * 64


# ============================================================ T01.3 · 报告自证


def _task_with(criteria_source: str, **overrides: Any) -> TaskOut:
    """最小 ``TaskOut``（判据三键按参数写，其余取默认值 → 便于测 pop 纪律）。"""
    params: dict[str, Any] = {
        "id": "t1",
        "criteria_source": criteria_source,
        "template_id": overrides.pop("template_id", ""),
        "criteria_version": overrides.pop("criteria_version", ""),
    }
    params.update(overrides)
    return TaskOut(**params)


def _report(tasks: list[TaskOut]) -> EvaluationReport:
    return EvaluationReport.build(
        suite_id="default",
        suite_version="1.0",
        project_id="proj-v15",
        weights={"runnability": 1.0},
        thresholds=Thresholds(total=75.0, runnability=60.0),
        tasks=tasks,
        wall_time_ms=1,
    )


class TestReportSelfDescribesCriteria:
    """T01.3：报告根部三键**由 build 从 ``tasks[0]`` 派生**（caller 传不进来）。"""

    def test_report_derives_criteria_from_the_first_task(self) -> None:
        report = _report(
            [
                _task_with(
                    CRITERIA_SOURCE_TEMPLATE,
                    template_id=TEMPLATE_ID,
                    criteria_version="c" * 64,
                )
            ]
        )
        assert report.criteria_source == CRITERIA_SOURCE_TEMPLATE
        assert report.template_id == TEMPLATE_ID
        assert report.criteria_version == "c" * 64
        payload = json.loads(report.to_json())
        assert payload["criteria_source"] == CRITERIA_SOURCE_TEMPLATE
        assert payload["template_id"] == TEMPLATE_ID
        assert payload["criteria_version"] == "c" * 64

    def test_build_has_no_criteria_parameters(self) -> None:
        """★ 构造上不可能"忘传"：``build`` 的签名里没有判据参数。"""
        import inspect

        params = inspect.signature(EvaluationReport.build).parameters
        assert "criteria_source" not in params
        assert "template_id" not in params
        assert "criteria_version" not in params

    def test_suite_criteria_keeps_empty_strings_not_null(self) -> None:
        """A12：``suite`` 口径下三键**一起保留**，``template_id`` 是空串而非 null。"""
        report = _report([_task_with(CRITERIA_SOURCE_SUITE)])
        payload = json.loads(report.to_json())
        assert payload["criteria_source"] == CRITERIA_SOURCE_SUITE
        assert payload["template_id"] == ""
        assert payload["criteria_version"] == ""

    def test_unset_criteria_is_popped_from_json(self) -> None:
        """A19：三键全空 ⇒ JSON 里**不存在**这些键（v1.3/v1.4 正文逐字节不变）。"""
        report = _report([_task_with("")])
        payload = json.loads(report.to_json())
        for key in ("criteria_source", "template_id", "criteria_version"):
            assert key not in payload, f"{key} 被写进了正文 ⇒ 字节契约破了"
            assert key not in payload["tasks"][0]

    def test_unverified_groups_is_popped_when_empty(self) -> None:
        task = _task_with(CRITERIA_SOURCE_TEMPLATE, template_id=TEMPLATE_ID)
        assert "unverified_groups" not in json.loads(_report([task]).to_json())["tasks"][0]
        loud = _task_with(
            CRITERIA_SOURCE_TEMPLATE,
            template_id=TEMPLATE_ID,
            unverified_groups=["backend"],
        )
        assert json.loads(_report([loud]).to_json())["tasks"][0]["unverified_groups"] == [
            "backend"
        ]

    def test_markdown_states_the_criteria_source(self) -> None:
        """markdown 必须写清"用哪把尺子"，``suite`` 口径显式标「未绑定模板」。"""
        bound = _report(
            [
                _task_with(
                    CRITERIA_SOURCE_TEMPLATE,
                    template_id=TEMPLATE_ID,
                    criteria_version="c" * 64,
                )
            ]
        ).to_markdown()
        assert f"判据来源：模板 `{TEMPLATE_ID}`" in bound

        unbound = _report([_task_with(CRITERIA_SOURCE_SUITE)]).to_markdown()
        assert "判据来源：套件 `default`（**未绑定模板**）" in unbound

        legacy = _report([_task_with("")]).to_markdown()
        assert "判据来源" not in legacy  # 老报告口径：不新增行（字节契约）

    def test_markdown_unverified_line_never_says_passed(self) -> None:
        """v1.5 §1.2.4 文案：只允许"未验证"（**禁止**写成"通过"）。"""
        report = _report(
            [
                _task_with(
                    CRITERIA_SOURCE_TEMPLATE,
                    template_id=TEMPLATE_ID,
                    unverified_groups=["backend"],
                )
            ]
        )
        markdown = report.to_markdown()
        assert "未验证（环境缺失：backend）" in markdown
        assert "未验证 ≠ 通过" in markdown


# ============================================================ T01.5 · 唯一报告构建器


class TestSingleReportBuilder:
    """T01.5：gate 的落盘正文**委托** ``build_run_report`` ⇒ 字段集合不可能漂移。"""

    def test_gate_delegates_to_build_run_report(self) -> None:
        run = _run()
        task, suite = build_task_for_run(run)
        outcome = _task_with(
            CRITERIA_SOURCE_TEMPLATE, template_id=TEMPLATE_ID, **{"criteria_version": "d" * 64}
        )
        outcome.artifact_ref = {"artifact_id": "a1", "name": "code", "version": 5}

        direct = build_run_report(run, suite, outcome, repair_round=2)
        gate = HarnessEvaluationGate(harness=None, store=None)  # type: ignore[arg-type]
        via_gate = gate._build_report(run, suite, outcome, 2)  # noqa: SLF001

        assert set(direct.model_dump(mode="json")) == set(via_gate.model_dump(mode="json"))
        assert direct.run_id == via_gate.run_id == run.id
        assert direct.artifact_version == via_gate.artifact_version == 5
        assert direct.repair_round == via_gate.repair_round == 2
        assert direct.verdict_source == via_gate.verdict_source == "harness"
        assert direct.criteria_source == via_gate.criteria_source == CRITERIA_SOURCE_TEMPLATE
        # 落盘文件集也必须同形（T04 的 CLI 走同一函数，这是 A13/A14 的前提）
        assert set(direct.files()) == set(via_gate.files())
        assert task.criteria_source == CRITERIA_SOURCE_TEMPLATE  # 装配点确实写入了判据


# ============================================================ T01.2 / T01.4 · 端点同源


class TestEvaluateProjectUsesAssemblyPoint:
    """T01.2 / T01.4：``harness.evaluate_project`` 走装配点，报告自证判据来源。"""

    async def test_evaluate_project_self_describes_criteria(
        self, session_factory, tmp_path: Any
    ) -> None:
        from test_evaluation import FakeJudge, FakeSandboxProvider, _seed_project

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )

        report = await harness.evaluate_project(
            project_id, requirement="实现待办 API", template_id=TEMPLATE_ID
        )

        assert report.criteria_source == CRITERIA_SOURCE_TEMPLATE
        assert report.template_id == TEMPLATE_ID
        assert report.criteria_version == template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)
        payload = json.loads(report.to_json())
        assert payload["template_id"] and payload["criteria_version"]
        assert payload["artifact_version"] >= 1

    async def test_evaluate_project_uses_template_checks(
        self, session_factory, tmp_path: Any
    ) -> None:
        """★ 证明 checks 真的来自模板：让**只有模板才有**的阻断项失败。

        套件 checks 只有 ``pytest`` / ``ruff``，永远不会有 ``fe-build web``；
        因此这条断言在旧的"自拼 EvalTask"实现上必然红。
        """
        from test_evaluation import (
            FakeJudge,
            FakeSandboxProvider,
            FakeSandboxSession,
            _seed_project,
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        provider = FakeSandboxProvider(FakeSandboxSession(exit_codes={"fe-build web": 1}))
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=provider,
            session_factory=session_factory,
        )

        report = await harness.evaluate_project(
            project_id, requirement="实现待办 API", template_id=TEMPLATE_ID
        )

        task = report.tasks[0]
        assert task.blocking_failed == ["fe-build web"]
        assert task.passed is False  # 阻断项失败 ⇒ 不可交付
        assert report.passed is False

    async def test_without_template_it_is_an_explicit_suite_report(
        self, session_factory, tmp_path: Any
    ) -> None:
        """没有模板 ⇒ ``criteria_source="suite"``（显式弱口径，不静默留 null）。"""
        from test_evaluation import FakeJudge, FakeSandboxProvider, _seed_project

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _seed_project(store, session_factory)
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )

        report = await harness.evaluate_project(project_id, requirement="实现待办 API")

        assert report.criteria_source == CRITERIA_SOURCE_SUITE
        assert report.template_id == ""
        assert report.criteria_version == ""
        payload = json.loads(report.to_json())
        assert payload["template_id"] == ""  # 空串而不是 null（A12）
        assert payload["criteria_version"] == ""

    async def test_manifest_tier_is_used_by_evaluate_project(
        self, session_factory, tmp_path: Any
    ) -> None:
        """T01.1 第 2 档在**端到端**路径上也生效（手工导入的产物靠产物溯源自证）。

        ★ 这里刻意不传 ``template_id``：装配点必须自己从产物里的
        ``patch_manifest.provenance`` 读出模板（并沿用**当时记的**指纹）。
        """
        from test_evaluation import CODE_MAIN, FakeJudge, FakeSandboxProvider

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="manifest-tier")
            await session.commit()
            project_id = project.id
        recorded = "e" * 64
        await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={
                "app/main.py": CODE_MAIN,
                "patch_manifest.json": json.dumps(
                    {
                        "mode": "patch",
                        "provenance": {
                            "template_id": TEMPLATE_ID,
                            "template_hash": recorded,
                        },
                    }
                ),
            },
        )
        harness = EvaluationHarness(
            store=store,
            judge=FakeJudge(),
            sandbox_provider=FakeSandboxProvider(),
            session_factory=session_factory,
        )

        report = await harness.evaluate_project(project_id, requirement="实现待办 API")

        assert report.criteria_source == CRITERIA_SOURCE_TEMPLATE
        assert report.template_id == TEMPLATE_ID
        assert report.criteria_version == recorded


@pytest.mark.parametrize("template_id", ["fastapi-vue3", "python-cli", "data-pipeline"])
def test_all_templates_are_loadable_for_assembly(template_id: str) -> None:
    """装配点对四套模板都成立（本版**不改**任何 YAML，只证明装配通用）。"""
    task, _suite, criteria = build_task_for_project(
        project_id="proj-v15", template_id=template_id
    )
    assert criteria.template_id == template_id
    assert criteria.criteria_source == CRITERIA_SOURCE_TEMPLATE
    assert task.checks, f"{template_id} 装配后没有任何检查可跑"
    assert all(check.run for check in task.checks)
