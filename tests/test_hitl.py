"""HITL 审批测试（T04）：三模式策略 / 审批生命周期 / 驳回回退 / 服务聚合 / API。

纪律（§10.3-10）：一律 mock LLM（0 费用）—— stub 执行器 + 落盘测试执行器
（不经网关，仅验证驳回重跑的产物版本递增链路）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import InvalidParamError, NotFoundError, StateConflictError
from ymir.events.bus import EventPublisher
from ymir.hitl.engine import ApprovalEngine
from ymir.hitl.policy import ApprovalPolicy, coerce_mode, gate_name_of, reject_target_of
from ymir.hitl.service import ApprovalService
from ymir.messages.bus import MessagePublisher
from ymir.models.entities import Approval, Event, Message, StageExecution
from ymir.models.enums import RunMode
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor, StageOutcome
from ymir.orchestrator.gates import build_approval_hook
from ymir.sop.models import StageSpec
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------------- 工具


async def _new_run(
    session: AsyncSession, *, mode: str = "key_node", requirement: str = "测试需求"
) -> str:
    project = await ProjectRepository(session).create(name="hitl-test")
    run = await RunRepository(session).create(
        project_id=project.id, requirement=requirement, mode=mode
    )
    await session.commit()
    return run.id


async def _get_run(session_factory: async_sessionmaker[AsyncSession], run_id: str):
    async with session_factory() as s:
        return await RunRepository(s).get(run_id)


async def _wait_status(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    statuses: set[str],
    timeout: float = 10.0,
):
    deadline = time.monotonic() + timeout
    while True:
        run = await _get_run(session_factory, run_id)
        if run is not None and run.status in statuses:
            return run
        if time.monotonic() > deadline:
            current = run.status if run is not None else "missing"
            raise AssertionError(f"run 未在 {timeout}s 内到达 {statuses}（当前 {current}）")
        await asyncio.sleep(0.05)


async def _list_approvals(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> list[Approval]:
    async with session_factory() as s:
        rows = list(
            (
                await s.execute(
                    select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)
                )
            )
            .scalars()
            .all()
        )
    return rows


async def _latest_stage_execs(
    session_factory: async_sessionmaker[AsyncSession], run_id: str, stage_id: str
) -> list[StageExecution]:
    async with session_factory() as s:
        rows = list(
            (
                await s.execute(
                    select(StageExecution)
                    .where(StageExecution.run_id == run_id, StageExecution.stage_id == stage_id)
                    .order_by(StageExecution.attempt)
                )
            )
            .scalars()
            .all()
        )
    return rows


async def _wait_pending_approval(
    session_factory: async_sessionmaker[AsyncSession], run_id: str, timeout: float = 10.0
) -> Approval:
    """等待 run 进入 waiting_approval 且出现 pending 审批（消除建审批与转状态间隙）。"""
    deadline = time.monotonic() + timeout
    while True:
        run = await _get_run(session_factory, run_id)
        rows = await _list_approvals(session_factory, run_id)
        pending = [row for row in rows if row.status == "pending"]
        if pending and run is not None and run.status == "waiting_approval":
            return pending[-1]
        if time.monotonic() > deadline:
            raise AssertionError(f"pending 审批未在 {timeout}s 内出现（已有 {len(rows)} 条）")
        await asyncio.sleep(0.05)


def make_stack(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: Any = None,
) -> tuple[OrchestrationEngine, ApprovalEngine]:
    """装配「引擎 + 审批引擎」（挂点注入 + 双向绑定，与 bootstrap 等价）。"""
    events = EventPublisher(session_factory)
    messages = MessagePublisher(session_factory, events)
    approval_engine = ApprovalEngine(
        session_factory=session_factory, events=events, messages=messages
    )
    engine = OrchestrationEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        executor=executor or StageExecutor(),
        approval_hook=build_approval_hook(approval_engine),
    )
    approval_engine.bind_orchestrator(engine)
    return engine, approval_engine


class ArtifactSavingExecutor(StageExecutor):
    """测试执行器：落真实产物版本（不经 LLM），验证驳回重跑版本递增链路。"""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self.calls: list[tuple[str, int]] = []

    async def execute(self, *, run, stage, attempt):  # type: ignore[override]
        self.calls.append((stage.id, attempt))
        name = stage.outputs[0] if stage.outputs else stage.id
        saved = await self._store.save(
            project_id=run.project_id,
            name=name,
            artifact_type="doc",
            files={f"{name}.md": f"# {name}\n\nattempt={attempt}\n"},
            run_id=run.id,
            stage_id=stage.id,
            created_by=stage.role,
            message=f"第 {attempt} 次执行（重跑/修订）",
        )
        return StageOutcome(
            output_refs=[saved.ref()],
            summary=f"[test] {stage.id} attempt={attempt}",
            payload={
                "stage_id": stage.id,
                "role_id": stage.role,
                "attempt": attempt,
                "artifact_id": saved.artifact_id,
                "artifact_version": saved.version,
            },
        )


def _fake_stage(stage_id: str, *, with_gate: bool = False) -> StageSpec:
    """策略单测用的阶段桩（不依赖 YAML 加载）。"""
    gate = None
    if with_gate:
        from ymir.sop.models import GateSpec

        gate = GateSpec(name=f"{stage_id} 关卡", on_reject_target=stage_id)
    return StageSpec(id=stage_id, role="pm", inputs=[], outputs=[], gate=gate)


# --------------------------------------------------------------------- 策略单测


def test_policy_requires_approval_three_modes() -> None:
    gated = _fake_stage("prd", with_gate=True)
    plain = _fake_stage("evaluation")

    key_node = ApprovalPolicy(RunMode.KEY_NODE)
    assert key_node.requires_approval(gated) is True
    assert key_node.requires_approval(plain) is False

    batch = ApprovalPolicy(RunMode.BATCH)
    assert batch.requires_approval(gated) is False
    assert batch.requires_approval(plain) is False

    debug = ApprovalPolicy(RunMode.DEBUG)
    assert debug.requires_approval(gated) is True
    assert debug.requires_approval(plain) is True


def test_policy_for_run_uses_run_mode_and_falls_back() -> None:
    class _Run:
        def __init__(self, mode: str) -> None:
            self.mode = mode

    assert ApprovalPolicy.for_run(_Run("debug")).mode is RunMode.DEBUG  # type: ignore[arg-type]
    assert ApprovalPolicy.for_run(_Run("batch")).mode is RunMode.BATCH  # type: ignore[arg-type]
    assert ApprovalPolicy.for_run(_Run("")).mode is RunMode.KEY_NODE  # type: ignore[arg-type]
    assert ApprovalPolicy.for_run(_Run("bogus")).mode is RunMode.KEY_NODE  # type: ignore[arg-type]


def test_coerce_mode_and_gate_helpers() -> None:
    assert coerce_mode(None) is RunMode.KEY_NODE
    assert coerce_mode("debug") is RunMode.DEBUG
    assert coerce_mode("nope", default=RunMode.BATCH) is RunMode.BATCH
    gated = _fake_stage("prd", with_gate=True)
    plain = _fake_stage("evaluation")
    assert gate_name_of(gated) == "prd 关卡"
    assert gate_name_of(plain) == "evaluation"
    assert reject_target_of(gated) == "prd"
    assert reject_target_of(plain) == "evaluation"


# --------------------------------------------------------------------- 挂点与生命周期


async def test_key_node_parks_at_first_gate_with_pending_approval(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """key_node：prd 完成后阻断在 waiting_approval，pending 审批字段正确。"""
    run_id = await _new_run(session, mode="key_node")
    engine, _approval_engine = make_stack(session_factory)
    await engine.launch(run_id)

    run = await _wait_status(session_factory, run_id, {"waiting_approval"})
    # 首阶段未推进过，current_stage_id 保持 None（引擎按 initial_stage 解释，T02 行为）
    assert run.current_stage_id in (None, "prd")

    approvals = await _list_approvals(session_factory, run_id)
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.stage_id == "prd"
    assert approval.gate_name == "PRD 确认"
    assert approval.status == "pending"
    assert approval.artifact_refs  # 取自阶段执行记录 output_refs

    # 阶段执行记录已 completed（等待人工决策，不推进）
    execs = await _latest_stage_execs(session_factory, run_id, "prd")
    assert [e.attempt for e in execs] == [1]
    assert execs[0].status == "completed"

    # approval.requested 事件已落库
    async with session_factory() as s:
        types = {
            row.type
            for row in (
                await s.execute(select(Event).where(Event.run_id == run_id))
            ).scalars()
        }
    assert "approval.requested" in types
    await engine.shutdown()


async def test_approve_resumes_and_completes_all_three_gates(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """三关依次通过：每关审批后推进，最终 completed（evaluation 无关卡）。"""
    run_id = await _new_run(session, mode="key_node")
    engine, approval_engine = make_stack(session_factory)
    await engine.launch(run_id)

    expected_gates = ["PRD 确认", "设计评审", "代码合并前评审"]
    for gate_name in expected_gates:
        approval = await _wait_pending_approval(session_factory, run_id)
        assert approval.gate_name == gate_name
        await approval_engine.decide(approval.id, decision="approve")

    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"

    approvals = await _list_approvals(session_factory, run_id)
    assert sorted(a.gate_name for a in approvals) == sorted(expected_gates)
    assert all(a.status == "approved" and a.decided_by == "human" for a in approvals)

    # 四阶段各执行一次（attempt=1）
    for stage_id in ("prd", "architecture", "coding", "evaluation"):
        execs = await _latest_stage_execs(session_factory, run_id, stage_id)
        assert [(e.attempt, e.status) for e in execs] == [(1, "completed")], stage_id
    await engine.shutdown()


async def test_reject_reruns_stage_attempt_plus_one_with_new_artifact_version(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    temp_settings,
) -> None:
    """驳回回退：attempt+1 重跑 + feedback 入上下文 + 产物新版本（带血缘）。"""
    store = ArtifactStore(session_factory, temp_settings.artifacts_dir)
    run_id = await _new_run(session, mode="key_node", requirement="做一个 CLI 工具")
    engine, approval_engine = make_stack(session_factory, executor=ArtifactSavingExecutor(store))
    await engine.launch(run_id)

    first = await _wait_pending_approval(session_factory, run_id)
    assert first.stage_id == "prd"

    await approval_engine.decide(
        first.id, decision="reject", feedback="请补充非功能需求与验收标准"
    )

    # 重跑后再次进入审批（attempt=2）
    second = await _wait_pending_approval(session_factory, run_id)
    assert second.id != first.id
    assert second.stage_id == "prd"
    assert second.status == "pending"

    # prd 执行记录：attempt=1 被标记 failed（驳回作废），attempt=2 completed
    execs = await _latest_stage_execs(session_factory, run_id, "prd")
    assert [(e.attempt, e.status) for e in execs] == [(1, "failed"), (2, "completed")]
    assert "被驳回" in (execs[0].error or "")

    # feedback 消息进入角色上下文（to_role=pm，type=feedback）
    async with session_factory() as s:
        feedbacks = list(
            (
                await s.execute(
                    select(Message)
                    .where(Message.run_id == run_id, Message.type == "feedback")
                    .order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )
    assert len(feedbacks) == 1
    assert feedbacks[0].to_role == "pm"
    assert "非功能需求" in (feedbacks[0].summary or "")

    # 产物新版本：prd v2，parent 血缘指向 v1
    artifact_id = first.artifact_refs[0]["artifact_id"]
    artifact, versions = await store.get_artifact_detail(artifact_id)
    assert artifact.name == "prd"
    assert [v.version for v in versions] == [1, 2]
    assert versions[1].parent_version_id == versions[0].id
    assert "重跑" in (versions[1].message or "")

    # approval.decided（reject）事件已落库
    async with session_factory() as s:
        decided = list(
            (
                await s.execute(
                    select(Event).where(
                        Event.run_id == run_id, Event.type == "approval.decided"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert any(row.payload.get("decision") == "reject" for row in decided)
    assert any(row.payload.get("rerun_stage") == "prd" for row in decided)

    # 放行 prd 重审 -> 继续后两关 -> 全流程完成
    await approval_engine.decide(second.id, decision="approve")
    for _gate_name in ("设计评审", "代码合并前评审"):
        nxt = await _wait_pending_approval(session_factory, run_id)
        assert nxt.gate_name == _gate_name
        await approval_engine.decide(nxt.id, decision="approve")
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"

    # 二次审批共 4 条（首关 2 次 + 后两关各 1 次）；时序戳可能相同，按多重集断言
    approvals = await _list_approvals(session_factory, run_id)
    assert len(approvals) == 4
    assert sorted(a.status for a in approvals) == ["approved", "approved", "approved", "rejected"]
    await engine.shutdown()


async def test_batch_mode_auto_approves_without_blocking(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """batch：全自动不阻断，三关落自动通过记录（审计轨迹），run 直接完成。"""
    run_id = await _new_run(session, mode="batch")
    engine, _approval_engine = make_stack(session_factory)
    await engine.launch(run_id)

    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"

    approvals = await _list_approvals(session_factory, run_id)
    assert sorted(a.gate_name for a in approvals) == [
        "PRD 确认",
        "代码合并前评审",
        "设计评审",
    ]
    assert all(a.status == "approved" for a in approvals)
    assert all(a.decided_by == "batch" for a in approvals)
    assert all(a.decision == "approve" for a in approvals)

    # 全程无 waiting_approval、无 pending
    finished = await _get_run(session_factory, run_id)
    assert finished.status == "completed"
    await engine.shutdown()


async def test_debug_mode_blocks_every_stage(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """debug：每个阶段完成都阻断（evaluation 无关卡也阻断，关卡名=阶段 ID）。"""
    run_id = await _new_run(session, mode="debug")
    engine, approval_engine = make_stack(session_factory)
    await engine.launch(run_id)

    expected = ["prd", "architecture", "coding", "evaluation"]
    seen: list[str] = []
    for stage_id in expected:
        approval = await _wait_pending_approval(session_factory, run_id)
        assert approval.stage_id == stage_id
        seen.append(approval.gate_name)
        await approval_engine.decide(approval.id, decision="approve")

    # 前三关用 YAML 关卡名，evaluation 无关卡回落阶段 ID
    assert seen == ["PRD 确认", "设计评审", "代码合并前评审", "evaluation"]
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"
    await engine.shutdown()


# --------------------------------------------------------------------- 决策校验


async def test_duplicate_decision_raises_1003(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """重复决策同一审批 -> StateConflictError（1003）。"""
    run_id = await _new_run(session, mode="key_node")
    engine, approval_engine = make_stack(session_factory)
    await engine.launch(run_id)
    approval = await _wait_pending_approval(session_factory, run_id)

    await approval_engine.decide(approval.id, decision="approve")
    with pytest.raises(StateConflictError) as first:
        await approval_engine.decide(approval.id, decision="approve")
    assert first.value.biz_code == 1003

    with pytest.raises(StateConflictError) as second:
        await approval_engine.decide(approval.id, decision="reject", feedback="又想驳回")
    assert second.value.biz_code == 1003
    await engine.shutdown()


async def test_decision_on_missing_approval_raises_1002(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _engine, approval_engine = make_stack(session_factory)
    with pytest.raises(NotFoundError) as exc_info:
        await approval_engine.decide("no-such-approval", decision="approve")
    assert exc_info.value.biz_code == 1002


async def test_invalid_decision_value_raises_1001(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session, mode="key_node")
    engine, approval_engine = make_stack(session_factory)
    await engine.launch(run_id)
    approval = await _wait_pending_approval(session_factory, run_id)
    with pytest.raises(InvalidParamError) as exc_info:
        await approval_engine.decide(approval.id, decision="abstain")
    assert exc_info.value.biz_code == 1001
    await engine.shutdown()


async def test_decision_after_cancel_raises_1003(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """等待审批中被取消（failed）后再决策 -> 1003（run 不在 waiting_approval）。"""
    run_id = await _new_run(session, mode="key_node")
    engine, approval_engine = make_stack(session_factory)
    await engine.launch(run_id)
    approval = await _wait_pending_approval(session_factory, run_id)

    await engine.request_cancel(run_id)
    with pytest.raises(StateConflictError) as exc_info:
        await approval_engine.decide(approval.id, decision="approve")
    assert exc_info.value.biz_code == 1003
    await engine.shutdown()


# --------------------------------------------------------------------- 服务层聚合


async def test_service_list_and_detail_views(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """列表过滤 + run 摘要；详情含历史反馈链。"""
    run_id = await _new_run(session, mode="key_node", requirement="实现待办 CLI")
    engine, approval_engine = make_stack(session_factory)
    await engine.launch(run_id)
    first = await _wait_pending_approval(session_factory, run_id)

    service = ApprovalService(session_factory=session_factory, engine=approval_engine)

    # 列表：pending 过滤 + run 摘要
    pending = await service.list_approvals(status="pending")
    assert len(pending) == 1
    assert pending[0].approval.id == first.id
    assert pending[0].run is not None
    assert pending[0].run.requirement == "实现待办 CLI"
    assert pending[0].run.project_name == "hitl-test"
    assert pending[0].run.mode == "key_node"

    # 列表：run_id 过滤（不匹配 -> 空）
    assert await service.list_approvals(run_id="other-run") == []

    # 非法状态过滤 -> 1001
    with pytest.raises(InvalidParamError):
        await service.list_approvals(status="bogus")

    # 驳回一次（产生 feedback 消息 + 新 pending）
    await approval_engine.decide(first.id, decision="reject", feedback="补充边界场景")
    second = await _wait_pending_approval(session_factory, run_id)

    # 详情（首条=rejected）：历史反馈链含刚产生的 feedback
    detail = await service.get_approval(first.id)
    assert detail.approval.status == "rejected"
    assert detail.run is not None and detail.run.status in ("waiting_approval", "running")
    assert len(detail.feedback_history) == 1
    assert "边界场景" in (detail.feedback_history[0].summary or "")

    # 详情（新 pending）：过滤后的列表也能按 approved/rejected 取到
    rejected_views = await service.list_approvals(status="rejected")
    assert [v.approval.id for v in rejected_views] == [first.id]
    assert second.id != first.id

    # 详情不存在 -> 1002
    with pytest.raises(NotFoundError):
        await service.get_approval("no-such-approval")
    await engine.shutdown()


# --------------------------------------------------------------------- API 端点


async def _create_project_and_run(
    client: AsyncClient, *, mode: str = "key_node", requirement: str = "API 测试需求"
) -> str:
    created = await client.post("/api/v1/projects", json={"name": "hitl-api"})
    project_id = created.json()["data"]["id"]
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"requirement": requirement, "mode": mode},
    )
    assert resp.status_code == 202
    return resp.json()["data"]["run_id"]


async def _wait_run_status(client: AsyncClient, run_id: str, statuses: set[str]) -> dict:
    deadline = time.monotonic() + 10
    while True:
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        if data["status"] in statuses:
            return data
        if time.monotonic() > deadline:
            raise AssertionError(f"run 未到达 {statuses}（当前 {data['status']}）")
        await asyncio.sleep(0.05)


async def _wait_pending_via_api(client: AsyncClient, run_id: str) -> dict:
    deadline = time.monotonic() + 10
    while True:
        body = (
            await client.get(f"/api/v1/approvals?status=pending&run_id={run_id}")
        ).json()
        if body["data"]["total"] >= 1:
            return body["data"]["items"][-1]
        if time.monotonic() > deadline:
            raise AssertionError("pending 审批未出现")
        await asyncio.sleep(0.05)


async def test_api_approvals_full_flow_with_reject(client: AsyncClient) -> None:
    """API 全流程：发起 -> 三关审批（首关驳回带反馈回退）-> completed。"""
    run_id = await _create_project_and_run(client)

    # 第一关：详情 + 驳回（带反馈）
    first = await _wait_pending_via_api(client, run_id)
    assert first["gate_name"] == "PRD 确认"
    assert first["run"]["requirement"] == "API 测试需求"
    assert first["run"]["project_name"] == "hitl-api"
    assert first["artifact_refs"]

    detail = (await client.get(f"/api/v1/approvals/{first['id']}")).json()
    assert detail["code"] == 0
    assert detail["data"]["feedback_history"] == []
    assert detail["data"]["run"]["mode"] == "key_node"

    resp = await client.post(
        f"/api/v1/approvals/{first['id']}/decision",
        json={"decision": "reject", "feedback": "请补充验收标准", "decided_by": "reviewer"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "rejected"

    # 重复决策 -> 409 / 1003
    resp = await client.post(
        f"/api/v1/approvals/{first['id']}/decision", json={"decision": "approve"}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 1003

    # 驳回后重跑，再次待审（详情此时含 1 条历史反馈）
    second = await _wait_pending_via_api(client, run_id)
    assert second["id"] != first["id"]
    detail = (await client.get(f"/api/v1/approvals/{second['id']}")).json()["data"]
    assert len(detail["feedback_history"]) == 1
    assert "验收标准" in detail["feedback_history"][0]["summary"]

    # 依次放行：prd 重审 -> 设计评审 -> 代码合并前评审 -> evaluation 无关卡直接完成
    await client.post(
        f"/api/v1/approvals/{second['id']}/decision", json={"decision": "approve"}
    )
    second = await _wait_pending_via_api(client, run_id)
    assert second["gate_name"] == "设计评审"
    await client.post(
        f"/api/v1/approvals/{second['id']}/decision", json={"decision": "approve"}
    )
    second = await _wait_pending_via_api(client, run_id)
    assert second["gate_name"] == "代码合并前评审"
    await client.post(
        f"/api/v1/approvals/{second['id']}/decision", json={"decision": "approve"}
    )

    run = await _wait_run_status(client, run_id, {"completed"})
    assert run["status"] == "completed"

    # 列表过滤：全量 4 条（首关 2 次 + 后两关各 1 次；新的在前）
    body = (await client.get(f"/api/v1/approvals?run_id={run_id}")).json()
    assert body["data"]["total"] == 4
    assert sorted(item["status"] for item in body["data"]["items"]) == [
        "approved",
        "approved",
        "approved",
        "rejected",
    ]


async def test_api_approvals_error_envelopes(client: AsyncClient) -> None:
    """决策不存在 -> 1002；非法 decision -> 400/1001；非法 status 过滤 -> 400/1001。"""
    resp = await client.post("/api/v1/approvals/no-such/decision", json={"decision": "approve"})
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002

    resp = await client.get("/api/v1/approvals/no-such")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002

    resp = await client.get("/api/v1/approvals?status=bogus")
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_api_batch_mode_completes_without_pending(client: AsyncClient) -> None:
    """API：batch 模式直接完成，审批中心仅有自动通过记录。"""
    run_id = await _create_project_and_run(client, mode="batch")
    await _wait_run_status(client, run_id, {"completed"})

    pending = (await client.get("/api/v1/approvals?status=pending")).json()
    assert pending["data"]["total"] == 0

    body = (await client.get(f"/api/v1/approvals?run_id={run_id}")).json()
    assert body["data"]["total"] == 3
    assert all(item["decided_by"] == "batch" for item in body["data"]["items"])
