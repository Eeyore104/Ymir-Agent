"""审批服务层（架构设计 §3.8、§6.1 ``/approvals``）：聚合查询 + 决策入口。

- 列表：按 ``status`` / ``run_id`` 过滤，附 run 摘要（项目名 / 需求 / 模式 /
  状态）构成审批卡片聚合视图；
- 详情：审批记录 + run 摘要 + 历史反馈链（同 run 的 ``type=feedback`` 消息，
  即历次驳回留下的修订要求）；
- 决策：参数校验后委托 :class:`~ymir.hitl.engine.ApprovalEngine` 执行状态机
  （重复决策 / 非 pending -> ``StateConflictError`` 1003）。

本层只做「查询聚合 + 入口编排」，不持有状态机逻辑（引擎负责）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.hitl.engine import ApprovalEngine
from ymir.hitl.policy import DEFAULT_DECIDER
from ymir.models.entities import DEFAULT_TENANT_ID, Approval, Message
from ymir.models.enums import ApprovalStatus, MessageType
from ymir.storage.repositories import ApprovalRepository, ProjectRepository, RunRepository

#: 合法的审批状态过滤值
_VALID_STATUS: frozenset[str] = frozenset({status.value for status in ApprovalStatus})


@dataclass(frozen=True)
class RunBrief:
    """审批卡片用的运行摘要（避免前端为拿需求再查一次 run）。"""

    id: str
    project_id: str
    project_name: str
    requirement: str
    mode: str
    status: str
    current_stage_id: str | None


@dataclass(frozen=True)
class ApprovalView:
    """审批列表项（聚合视图：审批记录 + run 摘要）。"""

    approval: Approval
    run: RunBrief | None


@dataclass(frozen=True)
class ApprovalDetailView:
    """审批详情（聚合视图：审批记录 + run 摘要 + 历史反馈链）。"""

    approval: Approval
    run: RunBrief | None
    feedback_history: list[Message]


class ApprovalService:
    """审批服务：聚合查询与决策入口。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        engine: ApprovalEngine,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine

    async def list_approvals(
        self,
        *,
        status: str | None = None,
        run_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalView]:
        """审批列表（按 status / run_id 过滤，附 run 摘要）。"""
        if status is not None and status not in _VALID_STATUS:
            raise InvalidParamError(
                f"不支持的审批状态过滤: {status}（仅 {sorted(_VALID_STATUS)}）"
            )
        async with self._session_factory() as session:
            approvals = await ApprovalRepository(session).list_filtered(
                status=status, run_id=run_id, tenant_id=tenant_id, limit=limit, offset=offset
            )
            views: list[ApprovalView] = []
            for approval in approvals:
                brief = await self._run_brief(session, approval.run_id)
                views.append(ApprovalView(approval=approval, run=brief))
            return views

    async def get_approval(
        self, approval_id: str, *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> ApprovalDetailView:
        """审批详情：记录 + run 摘要 + 同 run 历史反馈链（按 seq 升序）。"""
        async with self._session_factory() as session:
            approval = await ApprovalRepository(session).get(approval_id, tenant_id=tenant_id)
            if approval is None:
                raise NotFoundError(f"审批不存在: {approval_id}")
            brief = await self._run_brief(session, approval.run_id)
            history = await self._feedback_history(session, approval.run_id)
            return ApprovalDetailView(approval=approval, run=brief, feedback_history=history)

    async def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        feedback: str | None = None,
        decided_by: str = DEFAULT_DECIDER,
    ) -> Approval:
        """审批决策入口（委托审批引擎执行状态机与推进唤醒）。"""
        return await self._engine.decide(
            approval_id, decision=decision, feedback=feedback, decided_by=decided_by
        )

    # ------------------------------------------------------------------ 内部

    async def _run_brief(
        self, session: AsyncSession, run_id: str
    ) -> RunBrief | None:
        """run 摘要（项目名 + 需求 + 模式 / 状态）；run 缺失时返回 None。"""
        run = await RunRepository(session).get(run_id)
        if run is None:
            return None
        project = await ProjectRepository(session).get(run.project_id)
        context = run.context if isinstance(run.context, dict) else {}
        return RunBrief(
            id=run.id,
            project_id=run.project_id,
            project_name=project.name if project is not None else "",
            requirement=str(context.get("requirement", "")),
            mode=run.mode,
            status=run.status,
            current_stage_id=run.current_stage_id,
        )

    async def _feedback_history(
        self, session: AsyncSession, run_id: str
    ) -> list[Message]:
        """同 run 的 feedback 消息链（历次驳回的修订要求，按 seq 升序）。"""
        stmt = (
            select(Message)
            .where(
                Message.run_id == run_id,
                Message.type == MessageType.FEEDBACK.value,
            )
            .order_by(Message.seq.asc())
        )
        return list((await session.execute(stmt)).scalars().all())
