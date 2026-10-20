"""审批生命周期引擎（架构设计 §3.8）：挂点阻断 + 决策应用 + 驳回回退重跑。

作为 ``OrchestrationEngine`` 的 ``ApprovalHook`` 接入（适配层见
:mod:`ymir.orchestrator.gates`），职责：

1. **挂点**（:meth:`on_stage_completed`）：阶段完成 -> 按
   :class:`~ymir.hitl.policy.ApprovalPolicy` 判断是否阻断；阻断则创建
   pending Approval（``gate_name`` 取关卡名或阶段名，``artifact_refs`` 取
   阶段最新执行记录的 ``output_refs``）并发 ``approval.requested``；
2. **决策**（:meth:`decide`）：校验状态机（pending 才可决策，否则
   ``StateConflictError`` 1003）-> 落库 -> 发 ``approval.decided`` ->
   驱动 run 回 running 并唤醒推进任务；
3. **驳回回退**（关键设计，详见 :meth:`_apply_reject`）：

   - 引擎 ``_process_stage`` 有「恢复补推进」分支 —— 阶段最新执行记录已
     completed 时直接推进而不重跑。驳回后需要的是 attempt+1 重跑同一阶段，
     因此把回退目标阶段「最新一条 completed 执行记录」标记为 failed
     （error=被驳回…，现场保留为历史记录）—— 与断点恢复对中断记录的处理
     同一机制（recovery 把残留 running 标记 failed），**引擎零改动**即走
     attempt+1 重跑分支；
   - feedback 以 ``type=feedback`` Message 发布（``to_role`` = 目标阶段角色），
     T03 执行器已按 ``to_role`` 过滤注入提示词（「历史反馈」段），
     这是最小侵入的上下文路径（无需改 run.context 结构）；
   - 重走正常保存路径，产物经 ArtifactStore 产出新版本（版本不可变 +
     parent 血缘由 T03 保证）；
   - 回退目标取 ``gate.on_reject_target``（当前三关均指向自身，即原地重跑）；
     指向上游阶段时同步纠正 ``run.current_stage_id``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.contracts import ApprovalContext
from ymir.common.errors import InvalidParamError, NotFoundError, StateConflictError
from ymir.events.bus import EventPublisher
from ymir.events.log import EventType
from ymir.hitl.policy import (
    BATCH_DECIDER,
    DECISION_APPROVE,
    DECISION_REJECT,
    DEFAULT_DECIDER,
    VALID_DECISIONS,
    ApprovalPolicy,
    gate_name_of,
    is_human_gate,
    reject_target_of,
)
from ymir.messages.bus import MessagePublisher
from ymir.messages.schema import SUMMARY_MAX_LENGTH, MessageCreate
from ymir.models.base import utcnow
from ymir.models.entities import Approval, PipelineRun, StageExecution
from ymir.models.enums import ApprovalStatus, MessageType, RunMode, RunStatus, StageStatus
from ymir.sop.loader import load_pipeline
from ymir.sop.models import PipelineDefinition, StageSpec
from ymir.storage.repositories import (
    ApprovalRepository,
    RunRepository,
    StageExecutionRepository,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ResumeTrigger(Protocol):
    """编排引擎恢复入口（接口倒置，打破 ``hitl <-> orchestrator`` 循环依赖）。

    审批引擎决策后只需「唤醒 run 推进」这一能力，故仅声明
    :meth:`request_resume`；:class:`~ymir.orchestrator.engine.OrchestrationEngine`
    结构上隐式满足该协议，hitl 无需 import 具体编排类（ARCH-02）。
    """

    async def request_resume(self, run_id: str) -> object: ...


#: 决策时等待 run 进入 waiting_approval 的容忍窗口（秒）默认值——
#: 引擎「创建审批记录 -> 迁移 run 状态」之间有毫秒级间隙，期间到达的决策
#: 不应误判为状态冲突；窗口外仍严格校验（如等待中被取消 -> 1003）。
DEFAULT_WAITING_STATUS_GRACE_SECONDS = 2.0


def _downstream_stage_ids(definition: PipelineDefinition, stage_id: str) -> list[str]:
    """沿 ``stage_completed`` 主链求某阶段的下游阶段集合（不含自身）。

    用于驳回回退到上游阶段时的「下游级联重跑」判定（QA-01）：目标阶段的
    下游（可达链上的）阶段需与新一次上游产出保持一致，不能沿用旧产物。

    v1.1：实现**委托** :meth:`PipelineDefinition.downstream_of` —— 与引擎回边
    共用同一套闭包语义（此前是两份私有实现，容易漂移）。
    """
    return definition.downstream_of(stage_id)


class ApprovalEngine:
    """审批生命周期：挂点阻断 / 决策应用 / 驳回回退重跑。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventPublisher,
        messages: MessagePublisher,
        definition_loader: Callable[[str], PipelineDefinition] | None = None,
        waiting_grace_seconds: float = DEFAULT_WAITING_STATUS_GRACE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._events = events
        self._messages = messages
        self._definition_loader = definition_loader or load_pipeline
        self._waiting_grace = waiting_grace_seconds
        self._orchestrator: ResumeTrigger | None = None

    def bind_orchestrator(self, orchestrator: ResumeTrigger) -> None:
        """绑定编排引擎（决策后恢复推进并唤醒任务；bootstrap 装配后注入）。

        采用后置绑定而非构造注入，是为了打破「引擎需要挂点 / 挂点需要引擎」
        的循环依赖：``OrchestrationEngine`` 构造时收 ``approval_hook``，
        而决策路径需要反向调用引擎的 ``request_resume``（经
        :class:`ResumeTrigger` 协议解耦，避免 hitl 反向 import 编排类）。
        """
        self._orchestrator = orchestrator

    # ------------------------------------------------------------------ 挂点

    async def on_stage_completed(
        self,
        run: PipelineRun,
        stage: StageSpec,
        ctx: ApprovalContext | None = None,
    ) -> bool:
        """``ApprovalHook`` 实现：返回 True 表示阶段完成后进入 waiting_approval。

        - **v1.1 defer**（§4.5.1）：``key_node`` 模式下，回边生效且该阶段属于修复闭环
          （``[repair.stage .. eval_stage]``）时，中间候选**不建卡片**，只发
          ``approval.deferred`` 并放行；最终候选由引擎的「终局第三关」补审 1 次。
          ``batch``（自动通过轨迹）与 ``debug``（全步骤人审）保持 v1.0 语义不变；
        - key_node：人工关卡阶段 -> 建 pending 审批并阻断；
        - debug：所有阶段 -> 建 pending 审批并阻断（关卡名缺省为阶段 ID）；
        - batch：人工关卡阶段 -> 落自动通过记录（审计轨迹）后放行。
        """
        policy = ApprovalPolicy.for_run(run)
        if ctx is not None and self._should_defer(run, stage, policy, ctx):
            await self._events.emit(
                run.id,
                EventType.APPROVAL_DEFERRED,
                {
                    "stage_id": stage.id,
                    "gate_name": gate_name_of(stage),
                    "repair_round": ctx.repair_round,
                    "reason": "repair_candidate",
                },
            )
            return False
        if not policy.requires_approval(stage):
            if policy.mode.value == "batch" and is_human_gate(stage):
                await self._record_auto_approval(run, stage)
            return False
        await self._request_approval(run, stage, policy)
        return True

    def _should_defer(
        self,
        run: PipelineRun,
        stage: StageSpec,
        policy: ApprovalPolicy,
        ctx: ApprovalContext,
    ) -> bool:
        """是否对「修复闭环内的中间候选」跳过第三关（仅 key_node）。

        取 key_node 限定是刻意的：``batch`` 需要落自动通过审计轨迹、``debug`` 是
        用户显式的全步骤人审，两者都不该被回边语义悄悄改掉（v1.0 断言不变）。
        """
        if ctx.phase != "stage_completed" or not ctx.repair_active:
            return False
        if policy.mode is not RunMode.KEY_NODE or not is_human_gate(stage):
            return False
        try:
            definition = self._definition_loader(run.pipeline_id)
        except Exception:  # noqa: BLE001 - 定义不可读时退回 v1.0 行为（照常审）
            logger.warning("defer 判定：流水线定义不可读 %s", run.pipeline_id, exc_info=True)
            return False
        spec = definition.repair
        if spec is None or not definition.repair_active():
            return False
        scope = {spec.stage, *definition.downstream_of(spec.stage)}
        return stage.id in scope

    async def _request_approval(
        self, run: PipelineRun, stage: StageSpec, policy: ApprovalPolicy
    ) -> Approval:
        """创建 pending 审批并发 ``approval.requested``（引擎随后转入 waiting_approval）。

        v1.1 **防刷屏**（§4.5.1）：同 run + 同 stage 已有 pending 时直接复用，
        不新建记录、不重复发 ``approval.requested``。人工驳回后该记录已变
        ``rejected``，下次终局会新建一次（R8③ 语义：人工判定是新一轮）。
        """
        refs = await self._latest_output_refs(run.id, stage.id)
        async with self._session_factory() as session:
            repo = ApprovalRepository(session)
            existing = await repo.get_pending(run.id, stage.id)
            if existing is not None:
                return existing
            approval = await repo.create(
                run_id=run.id,
                stage_id=stage.id,
                gate_name=gate_name_of(stage),
                artifact_refs=refs,
                status=ApprovalStatus.PENDING,
            )
            await session.commit()
        await self._events.emit(
            run.id,
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": approval.id,
                "stage_id": stage.id,
                "gate_name": approval.gate_name,
                "artifact_refs": refs,
                "mode": policy.mode.value,
            },
        )
        return approval

    async def _record_auto_approval(self, run: PipelineRun, stage: StageSpec) -> Approval:
        """batch 模式审计轨迹：人工关卡自动通过（approved，不阻断）。

        v1.1 幂等：同一 (run, stage) 已有 batch 自动通过记录时直接复用 ——
        终局第三关会再次扫到该阶段（``final_candidate``），不得重复落审计记录
        （否则 batch 的审批条数会多出一条，破坏「一阶段一轨迹」的口径）。
        """
        existing = await self._existing_auto_approval(run.id, stage.id)
        if existing is not None:
            return existing
        refs = await self._latest_output_refs(run.id, stage.id)
        async with self._session_factory() as session:
            repo = ApprovalRepository(session)
            approval = await repo.create(
                run_id=run.id,
                stage_id=stage.id,
                gate_name=gate_name_of(stage),
                artifact_refs=refs,
                status=ApprovalStatus.APPROVED,
            )
            approval.decision = DECISION_APPROVE
            approval.feedback = "batch 模式：关卡自动通过"
            approval.decided_by = BATCH_DECIDER
            approval.decided_at = utcnow()
            await session.commit()
        await self._events.emit(
            run.id,
            EventType.APPROVAL_DECIDED,
            {
                "approval_id": approval.id,
                "stage_id": stage.id,
                "gate_name": approval.gate_name,
                "decision": DECISION_APPROVE,
                "decided_by": BATCH_DECIDER,
                "auto": True,
            },
        )
        return approval

    async def _existing_auto_approval(self, run_id: str, stage_id: str) -> Approval | None:
        """该 (run, stage) 是否已有 batch 自动通过记录（幂等复用）。"""
        stmt = (
            select(Approval)
            .where(
                Approval.run_id == run_id,
                Approval.stage_id == stage_id,
                Approval.status == ApprovalStatus.APPROVED.value,
                Approval.decided_by == BATCH_DECIDER,
            )
            .limit(1)
        )
        async with self._session_factory() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _latest_output_refs(self, run_id: str, stage_id: str) -> list[object]:
        """阶段最新执行记录的产物引用（审批卡片的待审内容）。"""
        async with self._session_factory() as session:
            row = await StageExecutionRepository(session).latest(run_id, stage_id)
        if row is None:
            return []
        return list(row.output_refs or [])

    # ------------------------------------------------------------------ 决策

    async def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        feedback: str | None = None,
        decided_by: str = DEFAULT_DECIDER,
    ) -> Approval:
        """审批决策（approve / reject）。

        校验：审批不存在 -> ``NotFoundError``（1002）；审批非 pending
        （重复决策 / 已决策）-> ``StateConflictError``（1003）；run 不在
        waiting_approval（如等待中被取消）-> ``StateConflictError``（1003）。

        并发安全（ARCH-04 / QA-02）：落库改为原子条件更新
        （``UPDATE ... WHERE id=? AND status='pending'``），以 ``rowcount==1``
        作为「决策成功」的唯一裁决；并发双写时仅一方成功，另一方抛 1003，
        保证 ``_after_approved`` / ``_after_rejected`` 仅由成功方执行一次。
        """
        normalized = str(decision).strip().lower()
        if normalized not in VALID_DECISIONS:
            raise InvalidParamError(f"不支持的审批决策: {decision}（仅 approve / reject）")

        new_status = (
            ApprovalStatus.APPROVED.value
            if normalized == DECISION_APPROVE
            else ApprovalStatus.REJECTED.value
        )
        resolved_by = decided_by or DEFAULT_DECIDER
        decided_at = utcnow()

        async with self._session_factory() as session:
            approval = await ApprovalRepository(session).get(approval_id)
            if approval is None:
                raise NotFoundError(f"审批不存在: {approval_id}")
            if approval.status != ApprovalStatus.PENDING.value:
                raise StateConflictError(
                    f"审批已决策（{approval.status}），不能重复决策: {approval_id}"
                )
            run = await RunRepository(session).get(approval.run_id)
            if run is None:
                raise NotFoundError(f"审批所属运行不存在: {approval.run_id}")
            if run.status != RunStatus.WAITING_APPROVAL.value:
                # 容忍引擎「建审批 -> 转 waiting_approval」的毫秒级间隙；超窗口仍冲突
                run = await self._await_waiting_status(run.id) or run
            if run.status != RunStatus.WAITING_APPROVAL.value:
                raise StateConflictError(
                    f"运行当前状态为 {run.status}（非等待审批），无法决策: {approval.run_id}"
                )

            # 原子条件更新（CAS）：并发下仅一个事务能命中 pending 行
            cas = (
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.status == ApprovalStatus.PENDING.value,
                )
                .values(
                    status=new_status,
                    decision=normalized,
                    feedback=feedback,
                    decided_by=resolved_by,
                    decided_at=decided_at,
                )
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(cas)
            if result.rowcount != 1:
                await session.rollback()
                raise StateConflictError(
                    f"审批已被并发决策，不能重复决策: {approval_id}"
                )
            await session.commit()
            await session.refresh(approval)

        if normalized == DECISION_APPROVE:
            await self._after_approved(approval)
        else:
            await self._after_rejected(approval, feedback)
        return approval

    async def _after_approved(self, approval: Approval) -> None:
        """通过：发事件并恢复推进（引擎补推进到下一阶段）。"""
        await self._events.emit(
            approval.run_id,
            EventType.APPROVAL_DECIDED,
            {
                "approval_id": approval.id,
                "stage_id": approval.stage_id,
                "gate_name": approval.gate_name,
                "decision": DECISION_APPROVE,
                "decided_by": approval.decided_by,
            },
        )
        await self._resume_run(approval.run_id)

    async def _after_rejected(self, approval: Approval, feedback: str | None) -> None:
        """驳回：回退目标作废已完成记录 -> feedback 入角色上下文 -> 恢复重跑。"""
        target_stage_id, target_role = await self._apply_reject(approval)
        text = (feedback or "").strip()
        if text:
            # feedback Message：T03 执行器按 to_role 过滤后注入提示词「历史反馈」段；
            # summary 受消息协议 500 字限制，完整反馈保留在 Approval.feedback。
            await self._messages.publish(
                approval.run_id,
                MessageCreate(
                    type=MessageType.FEEDBACK,
                    from_role=approval.decided_by or DEFAULT_DECIDER,
                    to_role=target_role,
                    content_refs=list(approval.artifact_refs),
                    summary=text[:SUMMARY_MAX_LENGTH],
                    payload={
                        "approval_id": approval.id,
                        "stage_id": target_stage_id,
                        "gate_name": approval.gate_name,
                    },
                ),
            )
        await self._events.emit(
            approval.run_id,
            EventType.APPROVAL_DECIDED,
            {
                "approval_id": approval.id,
                "stage_id": approval.stage_id,
                "gate_name": approval.gate_name,
                "decision": DECISION_REJECT,
                "decided_by": approval.decided_by,
                "feedback": text,
                "rerun_stage": target_stage_id,
            },
        )
        await self._resume_run(approval.run_id)

    async def _apply_reject(self, approval: Approval) -> tuple[str, str | None]:
        """应用驳回回退：作废回退目标的已完成执行记录，返回 (目标阶段, 目标角色)。

        设计说明（关键）：引擎「恢复补推进」分支以「最新执行记录 completed」
        为前提；把该记录标记为 failed（error=被驳回…，现场保留为历史）后，
        引擎恢复推进时自然走 attempt+1 重跑 —— 与断点恢复同一机制，引擎零改动。
        目标阶段取 ``gate.on_reject_target``（缺省为审批阶段自身）；指向上游
        阶段时同步纠正 ``run.current_stage_id``。

        下游级联（QA-01）：目标非自身（回退到上游阶段）时，沿主链作废目标
        下游所有 ``completed`` 执行 —— 否则引擎「恢复补推进」会对下游已
        completed 的执行直接跳过、沿用旧产物而不随上游修订重跑。
        """
        async with self._session_factory() as session:
            run = await RunRepository(session).get(approval.run_id)
            if run is None:
                raise NotFoundError(f"审批所属运行不存在: {approval.run_id}")
            definition = self._definition_loader(run.pipeline_id)
            stage = definition.stage(approval.stage_id)
            target_stage_id = reject_target_of(stage) if stage is not None else approval.stage_id
            target_stage = definition.stage(target_stage_id)
            target_role = target_stage.role if target_stage is not None else None

            latest = await StageExecutionRepository(session).latest(run.id, target_stage_id)
            if latest is not None and latest.status == StageStatus.COMPLETED.value:
                latest.status = StageStatus.FAILED.value
                latest.error = f"被驳回（审批不通过），等待重跑: {approval.gate_name}"
            # v1.1：**无条件**作废下游并把推进指针回退到目标阶段。
            # 原因：终局第三关的卡片在 evaluation 阶段创建、审的却是 coding 产物
            # （approval.stage_id == target），若只在「驳回目标 ≠ 卡片阶段」时才级联，
            # 已被终局审查过的 evaluation 会残留 completed，恢复时被「补推进」直接带到终点，
            # 驳回形同虚设。中间候选正常驳回时下游尚无 completed 行，本调用是 no-op。
            await self._invalidate_downstream(
                session, run.id, definition, target_stage_id, approval.gate_name
            )
            if run.current_stage_id != target_stage_id:
                run.current_stage_id = target_stage_id
            await session.commit()
        return target_stage_id, target_role

    async def _invalidate_downstream(
        self,
        session: AsyncSession,
        run_id: str,
        definition: PipelineDefinition,
        target_stage_id: str,
        gate_name: str,
    ) -> None:
        """作废目标阶段下游（沿主链可达）的 ``completed`` 执行（QA-01）。

        仅当回退目标为上游阶段时调用；保证下游阶段随上游修订同步重跑，
        产物产出新版本，而非沿用旧产物。
        """
        downstream = _downstream_stage_ids(definition, target_stage_id)
        if not downstream:
            return
        rows = list(
            (
                await session.execute(
                    select(StageExecution).where(
                        StageExecution.run_id == run_id,
                        StageExecution.stage_id.in_(downstream),
                        StageExecution.status == StageStatus.COMPLETED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = StageStatus.FAILED.value
            row.error = f"上游阶段 {target_stage_id} 被驳回，需同步重跑: {gate_name}"

    async def _resume_run(self, run_id: str) -> None:
        """决策后恢复推进：waiting_approval -> running 并唤醒挂起任务。

        复用引擎 ``request_resume``（迁移校验 + ``_wake``；无活任务时自动
        拉起新任务，覆盖「进程重启后决策」的恢复场景）。
        """
        if self._orchestrator is None:
            raise StateConflictError("审批引擎尚未绑定编排引擎（装配缺失）")
        await self._orchestrator.request_resume(run_id)

    async def _await_waiting_status(self, run_id: str) -> PipelineRun | None:
        """短轮询等待 run 进入 waiting_approval（容忍建审批与转状态的间隙）。

        每轮用全新会话读取（规避外层会话的事务快照看不到新提交）；
        超时返回最后一次读到的 run（可能仍非等待态，由调用方判定冲突）。
        """
        deadline = time.monotonic() + self._waiting_grace
        run: PipelineRun | None = None
        while True:
            async with self._session_factory() as session:
                run = await RunRepository(session).get(run_id)
            if run is None or run.status == RunStatus.WAITING_APPROVAL.value:
                return run
            if time.monotonic() >= deadline:
                return run
            await asyncio.sleep(0.02)
