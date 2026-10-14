"""FastAPI 依赖：数据库会话与租户上下文。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.config import Settings, get_settings
from ymir.events.bus import EventPublisher
from ymir.gateway.gateway import ModelGateway
from ymir.messages.bus import MessagePublisher
from ymir.orchestrator.engine import OrchestrationEngine


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """从应用状态获取会话工厂并产出会话（请求级生命周期）。"""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_settings_dep(request: Request) -> Settings:
    """返回当前应用绑定的配置。"""
    return getattr(request.app.state, "settings", None) or get_settings()


def get_tenant_id(request: Request) -> str:
    """返回当前请求的租户 ID（MVP 恒为 default，§2.4）。"""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return getattr(request.state, "tenant_id", settings.default_tenant_id)


# --- 应用级组件（T02：网关 / 引擎 / 事件与消息总线）---


def get_orchestrator(request: Request) -> OrchestrationEngine:
    """返回应用绑定的编排引擎。"""
    return request.app.state.orchestrator


def get_event_publisher(request: Request) -> EventPublisher:
    """返回应用绑定的事件总线。"""
    return request.app.state.events


def get_message_publisher(request: Request) -> MessagePublisher:
    """返回应用绑定的消息总线。"""
    return request.app.state.messages


def get_gateway(request: Request) -> ModelGateway:
    """返回应用绑定的模型网关。"""
    return request.app.state.gateway


# --- Annotated 依赖别名（供路由签名使用，避免 B008）---
SessionDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
TenantDep = Annotated[str, Depends(get_tenant_id)]
OrchestratorDep = Annotated[OrchestrationEngine, Depends(get_orchestrator)]
EventsDep = Annotated[EventPublisher, Depends(get_event_publisher)]
MessagesDep = Annotated[MessagePublisher, Depends(get_message_publisher)]
GatewayDep = Annotated[ModelGateway, Depends(get_gateway)]
