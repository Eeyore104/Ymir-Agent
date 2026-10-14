"""pytest 共享夹具：临时数据库（tmp 目录，不污染 data/）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ymir.api.app import create_app
from ymir.config import Settings
from ymir.models.base import Base
from ymir.storage import db as db_module


@pytest.fixture
def temp_settings(tmp_path) -> Settings:
    """指向 tmp_path 的隔离配置。"""
    return Settings(
        db_path=tmp_path / "ymir_test.db",
        artifacts_dir=tmp_path / "artifacts",
        default_tenant_id="default",
        default_pipeline_id="software-dev",
        default_run_mode="key_node",
    )


@pytest_asyncio.fixture
async def engine(temp_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """构建临时引擎并创建全部表（使用 metadata，等价迁移结果）。"""
    eng = db_module.build_engine(temp_settings)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return db_module.build_session_factory(engine)


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        try:
            yield s
        finally:
            await s.close()


@pytest_asyncio.fixture
async def client(
    temp_settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """注入隔离引擎的测试客户端（ASGI 内存传输，不启真实端口）。"""
    app = create_app(settings=temp_settings, engine=engine, session_factory=session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    # T02：取消引擎推进任务，避免事件循环关闭时后台任务泄漏告警
    await app.state.orchestrator.shutdown()
