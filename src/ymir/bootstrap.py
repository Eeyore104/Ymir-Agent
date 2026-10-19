"""应用执行栈装配（app 与脚本共用）。

装配顺序：事件总线 -> 消息总线 -> 模型网关 -> 产物库 -> 沙箱服务
-> 角色注册表 / 运行时 -> 真实执行器（可注入 stub 覆盖，测试用）。
T04：HITL 审批栈（审批引擎 + 服务）经 :func:`build_approval_stack` 装配，
审批挂点注入编排引擎（见 :mod:`ymir.orchestrator.gates`）。
v1.1：回边栈（确定性裁决闸门 + 回边控制器）经 :func:`build_repair_stack` 装配，
``budget_probe`` 复用既有 ``UsageRecorder`` + ``BudgetSpec.per_run_tokens``，
不新造预算设施。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.config import PROJECT_ROOT, Settings
from ymir.evaluation.gate import HarnessEvaluationGate
from ymir.evaluation.harness import EvaluationHarness
from ymir.events.bus import EventPublisher
from ymir.gateway.gateway import ModelGateway, build_gateway
from ymir.gateway.providers import load_models_config
from ymir.gateway.usage import UsageRecorder
from ymir.hitl.engine import DEFAULT_WAITING_STATUS_GRACE_SECONDS, ApprovalEngine
from ymir.hitl.service import ApprovalService
from ymir.messages.bus import MessagePublisher
from ymir.orchestrator.executor import StageExecutor
from ymir.orchestrator.repair import BudgetSnapshot, RepairController
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.runtime import RoleRuntime
from ymir.sandbox.base import NetworkPolicy
from ymir.sandbox.factory import build_sandbox
from ymir.sandbox.session import SandboxService

logger = logging.getLogger(__name__)


@dataclass
class ExecutionStack:
    """执行栈组件集合。"""

    event_publisher: EventPublisher
    message_publisher: MessagePublisher
    gateway: ModelGateway
    artifact_store: ArtifactStore
    sandbox_service: SandboxService
    role_registry: RoleRegistry
    role_runtime: RoleRuntime
    executor: StageExecutor


@dataclass
class ApprovalStack:
    """HITL 组件集合（T04，§3.8）。"""

    engine: ApprovalEngine
    service: ApprovalService


@dataclass
class RepairStack:
    """回边组件集合（v1.1，docs/07 §4.3.7 / §4.4.3）。"""

    gate: HarnessEvaluationGate
    controller: RepairController


def build_execution_stack(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: StageExecutor | None = None,
) -> ExecutionStack:
    """按配置构建执行栈（``executor`` 显式注入时跳过真实执行器装配）。"""
    event_publisher = EventPublisher(session_factory, queue_size=settings.event_queue_size)
    message_publisher = MessagePublisher(session_factory, event_publisher)
    gateway = build_gateway(session_factory)
    artifact_store = ArtifactStore(session_factory, settings.resolved_artifacts_dir)
    sandbox_service = SandboxService(
        session_factory=session_factory,
        sandbox_loader=lambda: build_sandbox(settings),  # 懒加载：首次用到才探测后端
    )

    roles_dir = PROJECT_ROOT / "config" / "roles"
    role_registry = RoleRegistry(roles_dir)
    role_runtime = RoleRuntime(
        gateway=gateway,
        registry=role_registry,
        renderer=PromptRenderer(roles_dir / "prompts"),
    )

    if executor is None:
        executor = RoleStageExecutor(
            session_factory=session_factory,
            store=artifact_store,
            runtime=role_runtime,
            sandbox_service=sandbox_service,
            events=event_publisher,
            sandbox_network=NetworkPolicy(settings.sandbox_network),
            exec_timeout=float(settings.sandbox_exec_timeout),
        )

    return ExecutionStack(
        event_publisher=event_publisher,
        message_publisher=message_publisher,
        gateway=gateway,
        artifact_store=artifact_store,
        sandbox_service=sandbox_service,
        role_registry=role_registry,
        role_runtime=role_runtime,
        executor=executor,
    )


def build_approval_stack(
    session_factory: async_sessionmaker[AsyncSession],
    events: EventPublisher,
    messages: MessagePublisher,
    *,
    waiting_grace_seconds: float = DEFAULT_WAITING_STATUS_GRACE_SECONDS,
) -> ApprovalStack:
    """构建 HITL 审批栈（T04，§3.8）。

    引擎负责审批生命周期（挂点 / 决策 / 驳回回退），服务负责聚合查询与
    决策入口；引擎与编排引擎的相互绑定由调用方在装配完成后完成
    （``ApprovalStack.engine.bind_orchestrator``，打破循环依赖）。
    """
    engine = ApprovalEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        waiting_grace_seconds=waiting_grace_seconds,
    )
    service = ApprovalService(session_factory=session_factory, engine=engine)
    return ApprovalStack(engine=engine, service=service)


def build_repair_stack(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    events: EventPublisher,
    messages: MessagePublisher,
    harness: EvaluationHarness,
    store: ArtifactStore,
    per_run_tokens: int | None = None,
) -> RepairStack:
    """构建回边栈：确定性裁决闸门 + 回边控制器（v1.1）。

    ``budget_probe`` 复用既有 ``UsageRecorder.run_tokens`` 与
    ``BudgetSpec.per_run_tokens``（不新造预算设施）：修复发起前预检「已用 >= 上限」
    → 不发起修复、直接终态（``error_code=1004`` / ``stop_reason=budget_exceeded``）。
    预检本身失败时**放行**修复（宁可多跑一轮，也不因探针故障误停）。
    """
    gate = HarnessEvaluationGate(harness=harness, store=store)
    recorder = UsageRecorder(session_factory)
    limit = per_run_tokens if per_run_tokens is not None else _per_run_token_limit()

    async def budget_probe(run_id: str) -> BudgetSnapshot:
        used = await recorder.run_tokens(run_id)
        return BudgetSnapshot(used_tokens=used, limit_tokens=limit)

    controller = RepairController(
        session_factory=session_factory,
        events=events,
        messages=messages,
        budget_probe=budget_probe,
    )
    return RepairStack(gate=gate, controller=controller)


def _per_run_token_limit() -> int:
    """单 run token 上限（读 ``config/models.yaml``；读不到 -> 0 = 回边预检不限额）。

    兜底说明：返回 0 只表示**回边预检**不设上限，此时仍由 gateway ``BudgetGuard``
    在每次模型调用前守门（单 run token / 日成本两条红线不变）；若 Gateway 配置
    本身缺失，应用会在 ``build_gateway`` 阶段直接启动失败，不会静默裸奔。
    """
    try:
        return int(load_models_config().budget.per_run_tokens)
    except Exception:  # noqa: BLE001 - 配置缺失不阻断装配（守门仍在 BudgetGuard 每次调用前）
        logger.error(
            "模型配置不可读，回边预算预检按不限额处理（仍由 BudgetGuard 每次调用前守门）",
            exc_info=True,
        )
        return 0
