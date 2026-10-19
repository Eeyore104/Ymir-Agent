"""回边自愈（v1.1 T3.2）：裁决闸门 / 回边状态机 / 轮次与预算守门 / 作废重跑。

纪律（§14 测试纪律）：**零真实模型、零 Docker** —— 执行器是落真实产物版本的假
执行器，裁决由脚本化 fake gate 给出，预算预检用固定快照。引擎与控制器均为真实
实现，覆盖「首轮过 / 1 轮过 / 2 轮过 / 2 轮不过 / unknown 不回边 / 预算中断 /
作废重跑」七条主路径。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.common.contracts import EvalVerdict, FailedCheck
from ymir.events.bus import EventPublisher
from ymir.messages.bus import MessagePublisher
from ymir.models.entities import Event, Message, StageExecution
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor, StageOutcome
from ymir.orchestrator.repair import BudgetSnapshot, RepairController, repair_state
from ymir.sop.models import END_TOKEN, PipelineDefinition, RepairSpec, StageSpec, TransitionSpec
from ymir.storage.repositories import ProjectRepository, RunRepository

PIPELINE_ID = "repair-test"
REPAIR_EVENT = "evaluation_failed"
CODE_V1 = "def handle():\n    return 1\n"


# --------------------------------------------------------------------- 装配


def _definition(*, max_rounds: int = 2, with_repair: bool = True) -> PipelineDefinition:
    """``prd -> coding -> evaluation`` 主链 + 可选回边（无人工关卡，聚焦回边）。"""
    repair = RepairSpec(enabled=True, max_rounds=max_rounds) if with_repair else None
    stages = [
        StageSpec(id="prd", role="pm", inputs=["requirement"], outputs=["prd"]),
        StageSpec(id="coding", role="engineer", inputs=["prd"], outputs=["code"]),
        StageSpec(
            id="evaluation",
            role="qa",
            inputs=["prd", "code"],
            outputs=["evaluation_report"],
        ),
    ]
    transitions = [
        TransitionSpec(**{"from": "prd", "on": "stage_completed", "to": "coding"}),
        TransitionSpec(**{"from": "coding", "on": "stage_completed", "to": "evaluation"}),
        TransitionSpec(**{"from": "evaluation", "on": "stage_completed", "to": END_TOKEN}),
    ]
    if with_repair:
        transitions.append(
            TransitionSpec(**{"from": "evaluation", "on": REPAIR_EVENT, "to": "coding"})
        )
    return PipelineDefinition(
        id=PIPELINE_ID,
        version="1.1",
        description="回边自愈测试流水线",
        repair=repair,
        stages=stages,
        transitions=transitions,
    )


class SavingExecutor(StageExecutor):
    """假执行器：每个阶段落一个真实产物版本（零 LLM），记录调用。"""

    def __init__(self, store: ArtifactStore, *, code_content: str = CODE_V1) -> None:
        self._store = store
        self._code_content = code_content
        self.calls: list[tuple[str, int]] = []

    async def execute(self, *, run, stage, attempt):  # type: ignore[override]
        self.calls.append((stage.id, attempt))
        name = stage.outputs[0] if stage.outputs else stage.id
        files = self._files_for(name, attempt)
        saved = await self._store.save(
            project_id=run.project_id,
            name=name,
            artifact_type="code" if name == "code" else "doc",
            files=files,
            run_id=run.id,
            stage_id=stage.id,
            created_by=stage.role,
            message=f"{stage.id} attempt={attempt}",
        )
        return StageOutcome(
            output_refs=[saved.ref()],
            summary=f"[fake] {stage.id} attempt={attempt}",
            payload={
                "stage_id": stage.id,
                "role_id": stage.role,
                "attempt": attempt,
                "artifact_version": saved.version,
            },
        )

    def _files_for(self, name: str, attempt: int) -> dict[str, str]:
        if name == "code":
            return {
                "main.py": f"{self._code_content}# attempt={attempt}\n",
                "tests/test_main.py": "def test_ok():\n    assert True\n",
            }
        return {f"{name}.md": f"# {name}\n\nattempt={attempt}\n"}

    def attempts_of(self, stage_id: str) -> list[int]:
        return [attempt for sid, attempt in self.calls if sid == stage_id]


class ScriptedGate:
    """脚本化裁决闸门：按调用顺序返回预设状态（记录调用与轮次）。"""

    def __init__(
        self,
        store: ArtifactStore,
        statuses: list[str],
        *,
        total: float = 62.4,
        runnability: float = 48.0,
    ) -> None:
        self._store = store
        self._statuses = statuses
        self._total = total
        self._runnability = runnability
        self.calls: list[int] = []

    async def evaluate(self, *, run, stage, attempt: int) -> EvalVerdict:
        index = len(self.calls)
        self.calls.append(attempt)
        status = self._statuses[min(index, len(self._statuses) - 1)]
        loaded = await self._store.latest_by_name(run.project_id, "code")
        version = loaded.version if loaded is not None else None
        passed = status == "pass"
        failed = (
            FailedCheck(
                command="python -m pytest -q",
                exit_code=1,
                timed_out=False,
                output="FAILED tests/test_main.py::test_ok",
            ),
        ) if status == "fail" else ()
        return EvalVerdict(
            status=status,
            passed=passed,
            total_score=88.0 if passed else self._total,
            runnability=90.0 if passed else self._runnability,
            thresholds={"total": 75.0, "runnability": 60.0},
            suite_id="default",
            suite_version="1.0",
            artifact_version=version,
            failed_checks=failed,
            failure_class=None if passed else ("check_failed" if failed else "below_threshold"),
            reason="" if passed else "脚本化未达标",
        )


def _make_engine(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    definition: PipelineDefinition,
    executor: SavingExecutor,
    gate: Any,
    budget: BudgetSnapshot | None = None,
    budget_raises: bool = False,
) -> tuple[OrchestrationEngine, RepairController]:
    events = EventPublisher(session_factory)
    messages = MessagePublisher(session_factory, events)

    async def budget_probe(_run_id: str) -> BudgetSnapshot:
        if budget_raises:
            raise RuntimeError("usage recorder unavailable")
        return budget or BudgetSnapshot(used_tokens=0, limit_tokens=500_000)

    controller = RepairController(
        session_factory=session_factory,
        events=events,
        messages=messages,
        budget_probe=budget_probe,
    )
    engine = OrchestrationEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        executor=executor,
        eval_gate=gate,
        repair=controller,
        definition_loader=lambda _pipeline_id: definition,
    )
    return engine, controller


async def _new_run(session: AsyncSession, project_name: str = "repair-test") -> tuple[str, str]:
    project = await ProjectRepository(session).create(name=project_name)
    run = await RunRepository(session).create(
        project_id=project.id, requirement="做一个 CLI 工具", mode="batch"
    )
    await session.commit()
    return project.id, run.id


async def _wait_status(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    statuses: set[str],
    timeout: float = 15.0,
):
    deadline = time.monotonic() + timeout
    while True:
        async with session_factory() as session:
            run = await RunRepository(session).get(run_id)
        if run is not None and run.status in statuses:
            return run
        if time.monotonic() > deadline:
            current = run.status if run is not None else "missing"
            raise AssertionError(f"run 未在 {timeout}s 内到达 {statuses}（当前 {current}）")
        await asyncio.sleep(0.05)


async def _events(session_factory: async_sessionmaker[AsyncSession], run_id: str) -> list[Event]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Event).where(Event.run_id == run_id).order_by(Event.seq)
                )
            )
            .scalars()
            .all()
        )


async def _messages(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> list[Message]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Message).where(Message.run_id == run_id).order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )


async def _stage_rows(
    session_factory: async_sessionmaker[AsyncSession], run_id: str, stage_id: str
) -> list[StageExecution]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(StageExecution)
                    .where(StageExecution.run_id == run_id, StageExecution.stage_id == stage_id)
                    .order_by(StageExecution.attempt)
                )
            )
            .scalars()
            .all()
        )


async def _run_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    statuses: list[str],
    max_rounds: int = 2,
    budget: BudgetSnapshot | None = None,
    budget_raises: bool = False,
    with_repair: bool = True,
) -> dict[str, Any]:
    """跑一次完整 run，返回观测句柄（run / 执行器 / gate / 事件 / 消息）。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    executor = SavingExecutor(store)
    gate = ScriptedGate(store, statuses)
    definition = _definition(max_rounds=max_rounds, with_repair=with_repair)
    engine, controller = _make_engine(
        session_factory,
        definition=definition,
        executor=executor,
        gate=gate,
        budget=budget,
        budget_raises=budget_raises,
    )
    async with session_factory() as session:
        project_id, run_id = await _new_run(session)
    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    await engine.shutdown()
    return {
        "store": store,
        "executor": executor,
        "gate": gate,
        "controller": controller,
        "definition": definition,
        "project_id": project_id,
        "run_id": run_id,
        "run": run,
        "events": await _events(session_factory, run_id),
        "messages": await _messages(session_factory, run_id),
    }


# --------------------------------------------------------------------- 四种组合


async def test_first_round_pass_completes(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """首轮即过：completed，无回边，code 只有 1 个版本。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["pass"])

    assert result["run"].status == "completed"
    assert result["executor"].attempts_of("coding") == [1]
    assert result["gate"].calls == [1]
    state = repair_state(result["run"].context)
    assert state["round"] == 0
    assert len(state["history"]) == 1  # 首轮必记（round=0）
    assert state["history"][0]["verdict"] == "pass"
    detail = await result["store"].latest_by_name(result["project_id"], "code")
    assert detail is not None and detail.version == 1
    types = [event.type for event in result["events"]]
    assert "evaluation.completed" in types
    assert "repair.started" not in types


async def test_pass_after_one_repair(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """1 轮修复后通过：coding 跑 2 次，history 两轮（round=0/1），最终 completed。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["fail", "pass"])

    assert result["run"].status == "completed"
    assert result["executor"].attempts_of("coding") == [1, 2]
    assert result["executor"].attempts_of("evaluation") == [1, 2]
    state = repair_state(result["run"].context)
    assert state["round"] == 1
    assert [entry["round"] for entry in state["history"]] == [0, 1]
    assert [entry["verdict"] for entry in state["history"]] == ["fail", "pass"]


async def test_pass_after_two_repairs(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """2 轮修复后通过（上限内）：coding 跑 3 次，最终 completed。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["fail", "fail", "pass"])

    assert result["run"].status == "completed"
    assert result["executor"].attempts_of("coding") == [1, 2, 3]
    state = repair_state(result["run"].context)
    assert state["round"] == 2
    assert [entry["verdict"] for entry in state["history"]] == ["fail", "fail", "pass"]


async def test_two_rounds_exhausted_fails(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """2 轮修复仍不过：failed + rounds_exhausted + error_code=None，**不得** completed。"""
    result = await _run_to_end(
        session_factory, tmp_path, statuses=["fail", "fail", "fail"], max_rounds=2
    )

    run = result["run"]
    assert run.status == "failed"
    assert run.error_code is None
    state = repair_state(run.context)
    assert state["stop_reason"] == "rounds_exhausted"
    assert state["round"] == 2
    assert [entry["round"] for entry in state["history"]] == [0, 1, 2]
    assert result["executor"].attempts_of("coding") == [1, 2, 3]
    types = [event.type for event in result["events"]]
    assert "repair.exhausted" in types
    exhausted = next(event for event in result["events"] if event.type == "repair.exhausted")
    assert exhausted.payload["stop_reason"] == "rounds_exhausted"
    assert exhausted.payload["error_code"] is None
    assert exhausted.payload["repair_round"] == 2


# --------------------------------------------------------------------- 降级与预算


async def test_unknown_verdict_does_not_rewind(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``unknown``（降级）：不回边、不阻断，按 v1.0 线性走终点。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["unknown", "fail"])

    assert result["run"].status == "completed"
    assert result["executor"].attempts_of("coding") == [1]  # 没有第二次 coding
    state = repair_state(result["run"].context)
    assert state["round"] == 0
    assert state["history"][0]["verdict"] == "unknown"
    types = [event.type for event in result["events"]]
    assert "repair.started" not in types
    assert "evaluation.failed" not in types


async def test_budget_blocked_fails_with_1004(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """修复前预算预检超限：failed + error_code=1004 + budget_exceeded，不发起修复。"""
    result = await _run_to_end(
        session_factory,
        tmp_path,
        statuses=["fail", "pass"],
        budget=BudgetSnapshot(used_tokens=500_000, limit_tokens=500_000),
    )

    run = result["run"]
    assert run.status == "failed"
    assert run.error_code == 1004
    state = repair_state(run.context)
    assert state["stop_reason"] == "budget_exceeded"
    # 未发起修复：coding 只跑过首轮，且没有 repair.started
    assert result["executor"].attempts_of("coding") == [1]
    types = [event.type for event in result["events"]]
    assert "repair.started" not in types
    assert "repair.exhausted" in types
    exhausted = next(event for event in result["events"] if event.type == "repair.exhausted")
    assert exhausted.payload["stop_reason"] == "budget_exceeded"
    assert exhausted.payload["error_code"] == 1004


async def test_budget_probe_failure_blocks_conservatively(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """预算探针故障 -> **fail-closed**：不发起修复、直接 1004 终态（宁可误停不可超支）。"""
    result = await _run_to_end(
        session_factory, tmp_path, statuses=["fail", "pass"], budget_raises=True
    )

    run = result["run"]
    assert run.status == "failed"
    assert run.error_code == 1004
    state = repair_state(run.context)
    assert state["stop_reason"] == "budget_exceeded"
    # 保守拦截：coding 只跑过首轮，且从未发起修复
    assert result["executor"].attempts_of("coding") == [1]
    assert "repair.started" not in [event.type for event in result["events"]]


# --------------------------------------------------------------------- 作废重跑与版本



async def test_rewind_invalidates_coding_and_evaluation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """回边作废 coding 与 evaluation 的 completed 执行（否则「补推进」跳过重评，C7）。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["fail", "pass"])
    run_id = result["run_id"]

    coding = await _stage_rows(session_factory, run_id, "coding")
    evaluation = await _stage_rows(session_factory, run_id, "evaluation")
    assert [(row.attempt, row.status) for row in coding] == [
        (1, "failed"),
        (2, "completed"),
    ]
    assert [(row.attempt, row.status) for row in evaluation] == [
        (1, "failed"),
        (2, "completed"),
    ]
    assert "回边重跑" in (coding[0].error or "")


async def test_repair_produces_new_code_version_with_parent(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """每次修复产出新 ``code`` 版本（v2 parent=v1，v1 不可变）。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["fail", "pass"])
    store: ArtifactStore = result["store"]

    latest = await store.latest_by_name(result["project_id"], "code")
    assert latest is not None
    artifact, versions = await store.get_artifact_detail(latest.artifact.id)
    assert [row.version for row in versions] == [1, 2]
    first, second = versions
    assert first.parent_version_id is None
    assert second.parent_version_id == first.id  # 血缘必填（指向 v1 版本行）
    assert artifact.current_version == 2
    # v1 内容不可变（版本目录一经写入永不修改）
    v1 = await store.load(latest.artifact.id, 1)
    assert v1.files["main.py"] == f"{CODE_V1}# attempt=1\n"
    v2 = await store.load(latest.artifact.id, 2)
    assert v2.files["main.py"] == f"{CODE_V1}# attempt=2\n"


async def test_pass_path_creates_no_extra_code_version(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """双门均过时不产生额外产物版本（版本数断言）。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["pass"])
    latest = await result["store"].latest_by_name(result["project_id"], "code")
    assert latest is not None
    _, versions = await result["store"].get_artifact_detail(latest.artifact.id)
    assert [row.version for row in versions] == [1]


# --------------------------------------------------------------------- 事件与消息


async def test_repair_events_and_message_payload(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """事件序列含 evaluation.failed / repair.started，payload 字段齐全；消息为定向修复。"""
    result = await _run_to_end(session_factory, tmp_path, statuses=["fail", "pass"])
    events = result["events"]
    types = [event.type for event in events]

    assert "evaluation.failed" in types
    assert "repair.started" in types
    assert types.index("evaluation.failed") < types.index("repair.started")

    failed = next(event for event in events if event.type == "evaluation.failed")
    payload = failed.payload
    assert payload["total"] == 62.4
    assert payload["runnability"] == 48.0
    assert payload["thresholds"] == {"total": 75.0, "runnability": 60.0}
    assert payload["failed_checks"][0]["command"] == "python -m pytest -q"
    assert payload["repair_round"] == 0  # 触发本轮修复的是第 0 轮裁决
    assert payload["max_rounds"] == 2
    assert payload["artifact_version"] == 1
    assert payload["degraded"] is False

    started = next(event for event in events if event.type == "repair.started")
    started_payload = started.payload
    for key in (
        "repair_round",
        "max_rounds",
        "target_stage",
        "from_stage",
        "artifact_version",
        "manual",
        "directive_digest",
    ):
        assert key in started_payload
    assert started_payload["repair_round"] == 1
    assert started_payload["target_stage"] == "coding"
    assert started_payload["from_stage"] == "evaluation"
    assert started_payload["manual"] is False

    feedback = [msg for msg in result["messages"] if msg.type == "feedback"]
    assert len(feedback) == 1
    message = feedback[0]
    assert message.payload["source"] == "auto_repair"
    assert message.payload["repair_round"] == 1
    assert message.payload["failed_checks"][0]["exit_code"] == 1
    assert message.payload["directive"]
    assert message.to_role == "engineer"
    assert message.from_role == "evaluator"


async def test_repair_disabled_keeps_linear_behaviour(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``repair=None``（v1.0）：即使裁决未达标也不回边、不阻断。"""
    result = await _run_to_end(
        session_factory, tmp_path, statuses=["fail"], with_repair=False
    )

    assert result["run"].status == "completed"
    assert result["gate"].calls == []  # should_evaluate 为 False：根本没调闸门
    assert result["executor"].attempts_of("coding") == [1]
    assert repair_state(result["run"].context) == {}


# --------------------------------------------------------------------- 裁决闸门实现


class StubHarness:
    """假 harness：返回预设 ``TaskOut``（记录传入的任务与套件，零评测成本）。"""

    def __init__(self, outcome: Any, *, error: Exception | None = None) -> None:
        self._outcome = outcome
        self._error = error
        self.tasks: list[Any] = []
        self.suites: list[Any] = []

    async def evaluate_task(self, task, *, project_id, version_override=None, suite=None):  # noqa: ANN001, ANN003
        self.tasks.append(task)
        self.suites.append(suite)
        if self._error is not None:
            raise self._error
        return self._outcome


def _task_out(
    *,
    passed: bool = True,
    total: float = 88.0,
    runnability: float = 90.0,
    artifact_version: int | None = 1,
    status: str = "evaluated",
    skip_reason: str | None = None,
    failed_check: bool = False,
    artifact_name: str | None = "code",
) -> Any:
    """构造一个与 harness 输出同构的 ``TaskOut``（含命令证据链）。"""
    from ymir.evaluation.report import DimensionOut, EvidenceOut, TaskOut

    evidence = (
        [
            EvidenceOut(
                kind="command",
                ref="python -m pytest -q",
                detail="exit=1 timed_out=False duration=12ms｜FAILED tests/test_main.py",
            )
        ]
        if failed_check
        else []
    )
    dims = [
        DimensionOut(name="requirement_conformance", label="需求符合度", score=total, weight=0.3),
        DimensionOut(
            name="runnability",
            label="代码可运行率",
            score=runnability,
            weight=0.3,
            evidence=evidence,
        ),
        DimensionOut(name="convention", label="规范符合度", score=total, weight=0.2),
        DimensionOut(name="architecture", label="架构合理性", score=total, weight=0.2),
    ]
    return TaskOut(
        id="run:stub",
        status=status,
        skip_reason=skip_reason,
        artifact_ref={
            "artifact_id": "a1" if artifact_name else None,
            "name": artifact_name,
            "version": artifact_version,
        },
        dimensions=dims,
        total_score=total,
        runnability=runnability,
        passed=passed,
        thresholds={"total": 75.0, "runnability": 60.0},
        wall_time_ms=12,
        cost_cny=0.0,
        tokens=0,
    )


async def _gate_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    outcome: Any,
    *,
    error: Exception | None = None,
    context: dict[str, Any] | None = None,
    repair_round: int = 0,
):
    """装配「真实 store + 假 harness + 真 gate」并建好 project / run。"""
    from ymir.evaluation.gate import HarnessEvaluationGate
    from ymir.orchestrator.repair import patch_run_context

    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    harness = StubHarness(outcome, error=error)
    gate = HarnessEvaluationGate(harness=harness, store=store, detail_chars=200)
    async with session_factory() as session:
        project_id, run_id = await _new_run(session, "gate-test")
    if repair_round:
        await patch_run_context(
            session_factory, run_id, {"repair": {"round": repair_round, "history": []}}
        )
    if context:
        await patch_run_context(session_factory, run_id, context)
    async with session_factory() as session:
        run = await RunRepository(session).get(run_id)
    definition = _definition()
    stage = definition.stage("evaluation")
    assert stage is not None
    return store, harness, gate, run, stage, project_id, run_id


async def test_gate_pass_maps_verdict_and_saves_eval_verdict(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``pass``：落盘 ``eval_verdict`` 且报告带 run_id / 版本 / 轮次 / 裁决来源。"""
    store, _, gate, run, stage, project_id, run_id = await _gate_fixture(
        session_factory, tmp_path, _task_out(), repair_round=1
    )

    verdict = await gate.evaluate(run=run, stage=stage, attempt=1)

    assert verdict.status == "pass"
    assert verdict.passed is True
    assert verdict.suite_id == "default"
    assert verdict.artifact_version == 1
    assert verdict.degraded is False

    saved = await store.latest_by_name(project_id, "eval_verdict")
    assert saved is not None
    assert saved.artifact.name == "eval_verdict"
    assert saved.artifact.type == "report"
    import json

    report = json.loads(saved.files["evaluation_report.json"])
    assert report["run_id"] == run_id
    assert report["verdict_source"] == "harness"
    assert report["repair_round"] == 1
    assert report["artifact_version"] == 1


async def test_gate_fail_extracts_failed_checks(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``fail``：从命令证据提取失败检查（命令 / 退出码 / 截断输出）。"""
    _, _, gate, run, stage, _, _ = await _gate_fixture(
        session_factory,
        tmp_path,
        _task_out(passed=False, total=62.4, runnability=48.0, failed_check=True),
    )

    verdict = await gate.evaluate(run=run, stage=stage, attempt=1)

    assert verdict.status == "fail"
    assert verdict.failure_class == "check_failed"
    assert len(verdict.failed_checks) == 1
    check = verdict.failed_checks[0]
    assert check.command == "python -m pytest -q"
    assert check.exit_code == 1
    assert check.timed_out is False
    assert "FAILED tests/test_main.py" in check.output


async def test_gate_skipped_task_is_unknown(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """任务未执行（产物缺失）-> ``unknown``（不回边、不阻断）。"""
    skipped = _task_out(status="skipped", skip_reason="missing_artifact", artifact_name=None)
    _, _, gate, run, stage, _, _ = await _gate_fixture(session_factory, tmp_path, skipped)

    verdict = await gate.evaluate(run=run, stage=stage, attempt=1)

    assert verdict.status == "unknown"
    assert verdict.failure_class == "missing_artifact"
    assert verdict.degraded is True


async def test_gate_harness_exception_is_unknown(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """harness 抛异常 -> ``unknown``（评测不可用属降级，绝不冒泡成 run 失败）。"""
    _, _, gate, run, stage, _, _ = await _gate_fixture(
        session_factory, tmp_path, _task_out(), error=RuntimeError("sandbox boom")
    )

    verdict = await gate.evaluate(run=run, stage=stage, attempt=1)

    assert verdict.status == "unknown"
    assert verdict.degraded is True
    assert "sandbox boom" in verdict.reason


async def test_gate_selection_priority_and_task_shape(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """套件 / 任务解析优先级：``run.context["benchmark"]`` > 流水线 ``repair.eval`` > default。"""
    store, harness, gate, run, stage, _, _ = await _gate_fixture(
        session_factory,
        tmp_path,
        _task_out(),
        context={"benchmark": {"eval_suite": "benchmark-12", "eval_task": "task-cli-notes-02"}},
    )

    await gate.evaluate(run=run, stage=stage, attempt=1)

    suite = harness.suites[0]
    task = harness.tasks[0]
    assert suite.id == "benchmark-12"
    assert task.scenario == "cli_tool"
    assert task.project_id == run.project_id  # 归属逐任务记（不跨项目取材）
    assert task.run_id == run.id
    assert task.artifact_name == "code"
    assert [check.run for check in task.checks][0] == "python -m pytest -q"
    assert len(task.checks) == 3  # 产品清单里 cli 任务有 3 条检查
    assert store is not None

