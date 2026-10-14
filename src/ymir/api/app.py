"""FastAPI 应用装配（§6）。

- 统一响应/异常约定（§6.3）
- CORS 仅放行 localhost（N4）
- TenantContext no-op 中间件（§2.4 扩展点）
- ``/health`` 含 DB 可达性与网关密钥就绪检查
- T02：装配模型网关 / 事件与消息总线 / 编排引擎，启动时断点恢复
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ymir.api.errors import register_exception_handlers
from ymir.api.middleware import TenantContextMiddleware
from ymir.api.routers import events, models, projects, runs, usage
from ymir.api.schemas import ApiResponse, HealthData
from ymir.common.errors import BizCode
from ymir.config import Settings, get_settings, load_env_file
from ymir.events.bus import EventPublisher
from ymir.gateway.gateway import build_gateway
from ymir.messages.bus import MessagePublisher
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.recovery import recover_runs
from ymir.storage.db import build_engine, build_session_factory

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """构建并装配 FastAPI 应用。

    允许注入 ``engine`` / ``session_factory``（测试用），否则按配置构建。
    """
    load_env_file()  # 注入 .env 密钥（如 DEEPSEEK_API_KEY，幂等，仅补齐缺失项）
    settings = settings or get_settings()
    engine = engine or build_engine(settings)
    session_factory = session_factory or build_session_factory(engine)

    # --- T02 组件装配：事件/消息总线 -> 引擎；模型网关 ---
    event_publisher = EventPublisher(session_factory)
    message_publisher = MessagePublisher(session_factory, event_publisher)
    orchestrator = OrchestrationEngine(
        session_factory=session_factory,
        events=event_publisher,
        messages=message_publisher,
    )
    gateway = build_gateway(session_factory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 断点恢复：扫描上次中断的 run 并重新拉起推进任务（§3.4）
        try:
            recovered = await recover_runs(session_factory=session_factory, engine=orchestrator)
            if recovered:
                logger.info("启动恢复：重新拉起 %d 个未完成 run", len(recovered))
        except Exception:  # noqa: BLE001 - 恢复失败不阻塞服务启动
            logger.exception("启动恢复失败")
        yield
        await orchestrator.shutdown()
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
    app.state.events = event_publisher
    app.state.messages = message_publisher
    app.state.orchestrator = orchestrator
    app.state.gateway = gateway

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TenantContextMiddleware, default_tenant_id=settings.default_tenant_id)

    register_exception_handlers(app)

    app.include_router(projects.router, prefix=settings.api_prefix)
    app.include_router(runs.router, prefix=settings.api_prefix)
    app.include_router(events.router, prefix=settings.api_prefix)
    app.include_router(models.router, prefix=settings.api_prefix)
    app.include_router(usage.router, prefix=settings.api_prefix)

    @app.get("/health", response_model=ApiResponse[HealthData], tags=["system"])
    async def health(request: Request) -> JSONResponse:
        factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            # 网关就绪度：至少一条可用路由（密钥就绪）才为 ready（§6.1）
            gw = getattr(request.app.state, "gateway", None)
            gateway_status = "unknown"
            if gw is not None:
                has_route = bool(
                    gw.router.candidates("planner") or gw.router.candidates("executor")
                )
                gateway_status = "ready" if has_route else "no-key"
            payload = {
                "code": 0,
                "data": {
                    "status": "ok",
                    "database": "ok",
                    "version": settings.app_version,
                    "gateway": gateway_status,
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
