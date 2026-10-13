"""FastAPI 依赖：数据库会话与租户上下文。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.config import Settings, get_settings


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


# --- Annotated 依赖别名（供路由签名使用，避免 B008）---
SessionDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
TenantDep = Annotated[str, Depends(get_tenant_id)]
