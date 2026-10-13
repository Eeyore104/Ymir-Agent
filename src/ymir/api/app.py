"""FastAPI 应用装配（§6）。

- 统一响应/异常约定（§6.3）
- CORS 仅放行 localhost（N4）
- TenantContext no-op 中间件（§2.4 扩展点）
- ``/health`` 含 DB 可达性检查（网关检查留 T02）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ymir.api.errors import register_exception_handlers
from ymir.api.middleware import TenantContextMiddleware
from ymir.api.routers import projects, runs
from ymir.api.schemas import ApiResponse, HealthData
from ymir.common.errors import BizCode
from ymir.config import Settings, get_settings
from ymir.storage.db import build_engine, build_session_factory


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """构建并装配 FastAPI 应用。

    允许注入 ``engine`` / ``session_factory``（测试用），否则按配置构建。
    """
    settings = settings or get_settings()
    engine = engine or build_engine(settings)
    session_factory = session_factory or build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="多 Agent 协作的软件开发平台 API",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        TenantContextMiddleware, default_tenant_id=settings.default_tenant_id
    )

    register_exception_handlers(app)

    app.include_router(projects.router, prefix=settings.api_prefix)
    app.include_router(runs.router, prefix=settings.api_prefix)

    @app.get("/health", response_model=ApiResponse[HealthData], tags=["system"])
    async def health(request: Request) -> JSONResponse:
        factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            payload = {
                "code": 0,
                "data": {
                    "status": "ok",
                    "database": "ok",
                    "version": settings.app_version,
                },
            }
            return JSONResponse(status_code=200, content=payload)
        except Exception as exc:  # noqa: BLE001 - 健康检查需捕获所有 DB 异常
            payload = {
                "code": int(BizCode.INTERNAL_ERROR),
                "message": f"数据库不可达: {exc}",
            }
            return JSONResponse(status_code=503, content=payload)

    return app


app = create_app()
