"""应用执行栈装配（app 与脚本共用）。

装配顺序：事件总线 -> 消息总线 -> 模型网关 -> 产物库 -> 沙箱服务
-> 角色注册表 / 运行时 -> 真实执行器（可注入 stub 覆盖，测试用）。
T04：HITL 审批栈（审批引擎 + 服务）经 :func:`build_approval_stack` 装配，
审批挂点注入编排引擎（见 :mod:`ymir.orchestrator.gates`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.config import PROJECT_ROOT, Settings
from ymir.events.bus import EventPublisher
from ymir.gateway.gateway import ModelGateway, build_gateway
from ymir.hitl.engine import ApprovalEngine
from ymir.hitl.service import ApprovalService
from ymir.messages.bus import MessagePublisher
from ymir.orchestrator.executor import StageExecutor
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.runtime import RoleRuntime
from ymir.sandbox.base import NetworkPolicy
from ymir.sandbox.factory import build_sandbox
from ymir.sandbox.session import SandboxService


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


def build_execution_stack(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: StageExecutor | None = None,
) -> ExecutionStack:
    """按配置构建执行栈（``executor`` 显式注入时跳过真实执行器装配）。"""
    event_publisher = EventPublisher(session_factory)
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
    )
    service = ApprovalService(session_factory=session_factory, engine=engine)
    return ApprovalStack(engine=engine, service=service)
