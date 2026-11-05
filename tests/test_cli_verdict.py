"""T04：CLI verdict 链路（v1.5，docs/17 §1.3 / §2-T04）的钉子。

**要防的裂缝（PRD §2-T4）**：`--role engineer` 的截断链**不含** ``evaluation`` 阶段
⇒ run 内 gate 永远不会被调用 ⇒ 不落 ``eval_verdict`` ⇒ ``from-merge`` 只能
fail-closed 409 ``baseline_health_unknown`` ⇒ 用户被迫 ``force``，而留痕写的是
"没评测过"—— 与"刚评测完"的事实相反。

**本版不改 engine 的 ``and``**（v1.4 §0.2 已实测：放开它会顺带改
``record_verdict`` 的语义），而是让 ``--evaluate`` **落盘结论**：

- 落盘为 ``eval_verdict`` **新版本**（不覆写，产物不可变）；
- 结论行有固定文案（A13/A14 的 grep 目标）；
- ``--repair`` 默认关，链上无 evaluation 阶段时 **stderr** 警告且不改退出码（A16）；
- 未加 ``--evaluate`` ⇒ 显式 ``verdict=absent（未裁决）``（A17：不裁决 ≠ 通过）。

纪律：零真实模型、零 DB（store 用 tmp 目录的真 ``ArtifactStore``；
``run_main`` 的全链真跑属 T05.3 的台账，本文件只钉**可离线判定**的部分）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ymir.artifacts.store import ArtifactStore
from ymir.config import PROJECT_ROOT
from ymir.evaluation.gate import (
    HarnessEvaluationGate,
    build_run_report,
    reduce_to_verdict,
    repair_round_of,
)
from ymir.evaluation.report import EvaluationReport, TaskOut
from ymir.evaluation.run_task import evaluate_run, evaluate_run_report
from ymir.models.entities import PipelineRun
from ymir.sop.loader import load_pipeline, truncate_pipeline

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_single_role.py"


def _load_cli() -> Any:
    """按文件路径加载 CLI 模块（``scripts/`` 不是包，且要避免污染 ``sys.path``）。"""
    spec = importlib.util.spec_from_file_location("ymir_run_single_role_verdict", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()


def _verdict(status: str, *, reason: str = "") -> Any:
    """最小 ``EvalVerdict``（只填文案生成需要的字段）。"""
    from ymir.common.contracts import EvalVerdict

    return EvalVerdict(status=status, reason=reason)


def _task(**overrides: Any) -> TaskOut:
    params: dict[str, Any] = {
        "id": "run:r-1",
        "project_id": "p-1",
        "run_id": "r-1",
        "artifact_ref": {"artifact_id": "a-1", "name": "code", "version": 7},
        "total_score": 82.0,
        "runnability": 90.0,
        "passed": True,
        "thresholds": {"total": 75.0, "runnability": 60.0},
        "criteria_source": "template",
        "template_id": "fastapi-vue3",
        "criteria_version": "f" * 64,
    }
    params.update(overrides)
    return TaskOut(**params)


def _run(context: dict[str, Any] | None = None, *, run_id: str = "r-1") -> PipelineRun:
    return PipelineRun(
        id=run_id,
        project_id="p-1",
        pipeline_id="software-dev",
        context=context
        if context is not None
        # 带模板 ⇒ 报告自证 ``criteria_source="template"``（真实 CLI 路径的形状）
        else {"requirement": "加导出接口", "template": {"id": "fastapi-vue3"}},
    )


class _StubHarness:
    """记录装配结果并回一个脚本化 ``TaskOut``（零沙箱、零模型）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    async def evaluate_task(
        self, task: Any, *, project_id: Any, version_override: Any = None, suite: Any = None
    ) -> TaskOut:
        self.calls.append((task, suite))
        return _task(
            id=task.id,
            run_id=task.run_id,
            criteria_source=task.criteria_source,
            template_id=task.template_id,
            criteria_version=task.criteria_version,
        )


# ============================================================ T04.1 · 归约提为公开


class TestReduceToVerdictIsPublic:
    """``reduce_to_verdict`` 是模块级公开函数，``gate._to_verdict`` 只是薄壳。

    完成判据（§2-T04.1）：**既有 gate 用例零改动通过** —— 本类补的是"两者同一段代码"
    这一层（否则 CLI 打印的 verdict 与 gate 的裁决会各写一份，正是本版要堵的口子）。
    """

    def test_gate_shell_delegates_to_the_module_function(self) -> None:
        from ymir.evaluation.suite import EvalSuite

        suite = EvalSuite.load("default")
        outcome = _task()
        gate = HarnessEvaluationGate(harness=None, store=None)  # type: ignore[arg-type]

        via_gate = gate._to_verdict(suite, outcome, 2)  # noqa: SLF001
        direct = reduce_to_verdict(
            suite, outcome, artifact_name="code", repair_round=2, detail_chars=1200
        )

        assert via_gate == direct  # EvalVerdict 是不可变 dataclass ⇒ 逐字段相等
        assert via_gate.status == "pass"
        assert via_gate.artifact_version == 7

    def test_repair_round_of_is_the_single_implementation(self) -> None:
        assert repair_round_of({}) == 0
        assert repair_round_of({"repair": {"round": 2}}) == 2
        assert repair_round_of({"repair": {"round": "3"}}) == 3
        assert repair_round_of({"repair": "oops"}) == 0


class TestEvaluateRunReport:
    """``evaluate_run_report``：一次装配 + 一次评测 + 一份报告（CLI 落盘用）。"""

    async def test_report_matches_outcome(self) -> None:
        harness = _StubHarness()
        outcome, suite, report = await evaluate_run_report(harness, _run(), repair_round=1)

        assert suite.id == "default"
        assert report.tasks[0] is outcome
        assert report.run_id == "r-1"
        assert report.artifact_version == 7  # 取**实际加载到**的版本
        assert report.repair_round == 1
        assert report.verdict_source == "harness"
        assert report.criteria_source == "template"
        # 装配点确实被走过一次（不是两次装配）
        assert len(harness.calls) == 1

    async def test_evaluate_run_is_the_same_assembly(self) -> None:
        """``evaluate_run`` 是薄壳：两条路径的 ``TaskOut`` 逐字段同形。"""
        first = _StubHarness()
        second = _StubHarness()
        only = await evaluate_run(first, _run())
        outcome, _suite, _report = await evaluate_run_report(second, _run())

        assert only.model_dump() == outcome.model_dump()
        assert first.calls[0][0].model_dump() == second.calls[0][0].model_dump()

    def test_gate_and_cli_share_the_report_builder(self) -> None:
        """``build_run_report`` 是 gate 与 CLI 的**唯一**报告构建器（A8 的第三层保障）。"""
        from ymir.evaluation.suite import EvalSuite

        suite = EvalSuite.load("default")
        outcome = _task()
        direct = build_run_report(_run(), suite, outcome, repair_round=0)
        gate = HarnessEvaluationGate(harness=None, store=None)  # type: ignore[arg-type]
        via_gate = gate._build_report(_run(), suite, outcome, 0)  # noqa: SLF001
        assert set(direct.model_dump(mode="json")) == set(via_gate.model_dump(mode="json"))


# ============================================================ T04.2 · 落盘


class TestPersistVerdict:
    """``eval_verdict`` 落盘：**新版本**、可被闸门读到、失败只降级。"""

    async def _project_and_run(
        self, session_factory: Any, tmp_path: Path, *, name: str
    ) -> tuple[ArtifactStore, str, str]:
        """铺一个真实项目 + 真实 run（``artifact_versions.run_id`` 有外键，不能编）。"""
        from ymir.storage.repositories import ProjectRepository, RunRepository

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name=name)
            await session.commit()
            project_id = project.id
        async with session_factory() as session:
            run = await RunRepository(session).create(
                project_id=project_id,
                requirement="加导出接口",
                context={"requirement": "加导出接口", "template": {"id": "fastapi-vue3"}},
            )
            await session.commit()
            run_id = run.id
        return store, project_id, run_id

    async def _report(self, run: PipelineRun) -> EvaluationReport:
        from ymir.evaluation.suite import EvalSuite

        suite = EvalSuite.load("default")
        return build_run_report(run, suite, _task(), repair_round=0)

    async def test_cli_evaluate_persists_verdict_artifact(
        self, session_factory: Any, tmp_path: Path, capsys: Any
    ) -> None:
        """A13：两次落盘 ⇒ v1 → v2（产物不可变：只新版本，不覆写）。"""
        store, project_id, run_id = await self._project_and_run(
            session_factory, tmp_path, name="cli-verdict"
        )
        report = await self._report(_run(run_id=run_id))

        first = await CLI.persist_verdict(store, report, run_id=run_id, project_id=project_id)
        second = await CLI.persist_verdict(store, report, run_id=run_id, project_id=project_id)

        assert first is not None and second is not None
        assert (first.version, second.version) == (1, 2)
        assert capsys.readouterr().err == ""

        stored = [a for a in await store.list_artifacts(project_id) if a.name == "eval_verdict"]
        assert len(stored) == 1  # 同一个 artifact，两个版本
        loaded = await store.load(stored[0].id, 2)
        payload = json.loads(loaded.files["evaluation_report.json"])
        # A13 的三件套：归属 run / 绑定实际版本 / 裁决来源
        assert payload["run_id"] == run_id
        assert payload["artifact_version"] == 7
        assert payload["verdict_source"] == "harness"
        # v1.5：报告自证判据来源（否则读取侧会按弱口径 fail-closed）
        assert payload["criteria_source"] == "template"

    async def test_persisted_verdict_unblocks_the_gate(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A13 的核心：落盘后 ``_known_merged_health`` 能读到结论（不再是 unknown）。"""
        from ymir.api.routers.artifacts import _known_merged_health

        store, project_id, run_id = await self._project_and_run(
            session_factory, tmp_path, name="cli-unlock"
        )
        report = await self._report(_run(run_id=run_id))
        saved = await CLI.persist_verdict(store, report, run_id=run_id, project_id=project_id)
        assert saved is not None

        reading = await _known_merged_health(
            store,
            project_id=project_id,
            run_id=run_id,
            tenant_id="default",
            artifact_version=7,
            artifact_name="code",
        )
        assert reading.status == "healthy"
        assert reading.source_name == "eval_verdict"

    async def test_persist_failure_degrades(
        self, capsys: Any
    ) -> None:
        """T04.4：落盘异常**不抛**、不改退出码，只在 stderr 提示（评测是附加信息）。"""

        class _BrokenStore:
            async def save(self, **kwargs: Any) -> Any:
                raise OSError("disk full")

        class _Report:
            async def save(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("disk full")

        saved = await CLI.persist_verdict(
            _BrokenStore(), _Report(), run_id="r-1", project_id="p-1"
        )
        assert saved is None
        err = capsys.readouterr().err
        assert "结论未落盘" in err
        assert "force" in err


# ============================================================ T04.2/3 · 文案


class TestVerdictLines:
    """结论行文案（A13/A14 的 grep 目标 + R8 的"禁止写通过"）。"""

    def test_pass_line_contains_the_required_sentence(self) -> None:
        lines = CLI.verdict_lines(
            _verdict("pass"), saved_version=3, artifact_version=2
        )
        assert "评测结论已落盘（eval_verdict v3，绑定 code v2）——提升闸门可用" in lines[0]
        assert lines[1].startswith("[verdict] pass")
        assert "提升闸门可用" in lines[1]

    def test_fail_line_carries_the_reason(self) -> None:
        lines = CLI.verdict_lines(
            _verdict("fail", reason="阻断项失败（python -m pytest -q）"),
            saved_version=4,
            artifact_version=2,
        )
        assert "评测结论已落盘（eval_verdict v4，绑定 code v2）" in lines[0]
        assert "阻断项失败" in lines[1]

    def test_unverified_line_never_says_passed(self) -> None:
        """R8：``unverified`` 只能写「未验证」，且必须带「未验证 ≠ 通过」。"""
        lines = CLI.verdict_lines(
            _verdict("pass"),
            saved_version=3,
            artifact_version=2,
            unverified_groups=["backend"],
        )
        joined = "\n".join(lines)
        assert "（部分检查未执行：backend）" in joined
        assert "未验证 ≠ 通过" in joined
        assert "评测通过" not in joined

    def test_absent_verdict_is_labelled_absent(self) -> None:
        """没落盘 ⇒ 只能是 ``absent``（**不许**说 pass：闸门读不到任何东西）。"""
        lines = CLI.verdict_lines(_verdict("pass"), saved_version=None, artifact_version=2)
        assert lines == [CLI.ABSENT_VERDICT_LINE]

    def test_absent_literal_is_the_documented_one(self) -> None:
        assert (
            CLI.ABSENT_VERDICT_LINE
            == "[verdict] absent（未裁决）— 提升需 force；如需结论请加 --evaluate"
        )

    def test_unknown_verdict_is_absent_even_if_persisted(self) -> None:
        """``unknown``（降级 / 基线不健康）落盘了也**不是**通过：文案必须说 absent。"""
        lines = CLI.verdict_lines(
            _verdict("unknown", reason="评测降级"), saved_version=5, artifact_version=2
        )
        assert "absent（未裁决）" in lines[1]
        assert "force" in lines[1]


# ============================================================ T04.3 · --repair


class TestRepairSwitch:
    """``--repair``：默认关；链上无 evaluation 阶段 ⇒ stderr 警告、退出码不变。"""

    def test_repair_flag_warns_on_truncated_chain(self) -> None:
        """★ A16 的现场：``--role engineer`` 截断到 coding ⇒ evaluation 不在链上。"""
        definition = load_pipeline("software-dev")
        truncated = truncate_pipeline(definition, "coding")
        warning = CLI.repair_chain_warning(definition, truncated)
        assert warning == "--repair 未生效：本次阶段链不含 evaluation 阶段"

    def test_qa_chain_is_effective(self) -> None:
        """``--role qa`` 的链含 evaluation（回边两端都在链上）⇒ 不警告。"""
        definition = load_pipeline("software-dev")
        truncated = truncate_pipeline(definition, "evaluation")
        assert CLI.repair_chain_warning(definition, truncated) == ""

    def test_repair_is_off_by_default(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(sys, "argv", ["run_single_role.py", "--requirement", "x"])
        args = CLI.parse_args()
        assert args.repair is False
        assert args.evaluate is False  # A17 的前提：默认不评测

    def test_repair_flag_is_parsed(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            sys, "argv", ["run_single_role.py", "--requirement", "x", "--repair"]
        )
        args = CLI.parse_args()
        assert args.repair is True


class TestRunMainWiring:
    """结构性钉子：``run_main`` 的两个分支**必须**用上面那些唯一实现。

    ★ 为什么是源码级断言：``run_main`` 需要 DB / 网关 / 编排栈，全链真跑的取证属
    T05.3 的台账（A13/A14 的 C 类命令）。这里守的是"接线没被改回去"——
    例如有人把 ``absent`` 分支删掉、或另写一份 verdict 文案。

    **A15（默认不返修）也在这层**：``build_repair_stack`` 只能在
    ``args.repair and not repair_warning`` 之下被调用 —— 默认路径既不注入 gate
    也不注入 repair（``engine.py`` 的 ``and`` 语义下等于"完全线性"）。
    """

    def test_cli_default_does_not_repair(self, monkeypatch: Any) -> None:
        """A15：不加 ``--repair`` ⇒ 不注入 eval_gate / repair（无回边、无额外成本）。"""
        monkeypatch.setattr(sys, "argv", ["run_single_role.py", "--requirement", "x"])
        args = CLI.parse_args()
        assert args.repair is False

        code = __import__("inspect").getsource(CLI.run_main)
        guard = code.index("if args.repair and not repair_warning:")
        build = code.index("build_repair_stack(")
        assert guard < build, "build_repair_stack 必须在 --repair 的守卫之内（A15）"
        # 其余两个注入点都是"没有栈就传 None"（不注入 ⇒ 引擎走 v1.0 线性行为）
        assert "eval_gate=(repair_stack.gate if repair_stack is not None else None)" in code
        assert "repair=(repair_stack.controller if repair_stack is not None else None)" in code

    def test_absent_branch_exists(self) -> None:
        code = __import__("inspect").getsource(CLI.run_main)
        assert "ABSENT_VERDICT_LINE" in code, "run_main 丢了 absent 分支（A17）"
        assert "else:" in code, "absent 分支必须是 --evaluate 的 else（不是静默省略）"

    def test_evaluate_branch_persists_and_formats(self) -> None:
        code = __import__("inspect").getsource(CLI.run_main)
        assert "evaluate_run_report(" in code, "CLI 应走 run_task 的唯一装配点"
        assert "persist_verdict(" in code, "结论必须落盘（A13/A14）"
        assert "reduce_to_verdict(" in code, "verdict 归约必须与 gate 同源"
        assert "verdict_lines(" in code, "结论行必须由唯一生成点产出"

    def test_repair_warning_goes_to_stderr(self) -> None:
        code = __import__("inspect").getsource(CLI.run_main)
        assert "repair_chain_warning(" in code
        assert "file=sys.stderr" in code, "A16 要求警告写 stderr（写 stdout 会被当正常输出）"

    def test_dry_run_returns_before_creating_anything(self) -> None:
        """T04.4：``--dry-run`` 在 build_execution_stack / create run 之前 return。"""
        code = __import__("inspect").getsource(CLI.run_main)
        dry = code.index("if args.dry_run:")
        stack = code.index("build_execution_stack(")
        create = code.index("RunRepository(session).create")
        assert dry < stack < create, "--dry-run 必须早于建栈与建 run"


class TestVerdictStatusesAreClosed:
    """``EvalVerdict.status`` 只有三态 —— 文案分支不许漏（漏了就是静默 unknown）。"""

    @pytest.mark.parametrize("status", ["pass", "fail", "unknown"])
    def test_every_status_has_a_line(self, status: str) -> None:
        lines = CLI.verdict_lines(_verdict(status), saved_version=1, artifact_version=1)
        assert lines, f"{status} 没有结论行"
        assert any(line.startswith("[verdict]") for line in lines)

    def test_no_status_is_silently_treated_as_pass(self) -> None:
        for status in ("unknown", ""):
            lines = CLI.verdict_lines(_verdict(status), saved_version=1, artifact_version=1)
            assert "评测通过" not in "\n".join(lines)


def test_module_imports_stay_client_side() -> None:
    """CLI 只 import ``ymir`` 的公开入口（不反向依赖 api / bootstrap 内部）。"""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "from ymir.evaluation.gate import" in source
    assert "from ymir.evaluation.run_task import" in source
    # 不新造 verdict 归约（必须复用 gate 的实现）
    assert "def reduce_to_verdict" not in source


def test_simple_namespace_round_trip() -> None:
    """``repair_round_of`` 对 ``SimpleNamespace`` 风格的 context 也稳（不抛）。"""
    assert repair_round_of(SimpleNamespace()) == 0
