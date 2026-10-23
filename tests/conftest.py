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
from ymir.orchestrator.executor import StageExecutor
from ymir.sandbox import LocalSandbox, SandboxService
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
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """注入隔离引擎的测试客户端（ASGI 内存传输，不启真实端口）。"""
    app = create_app(
        settings=temp_settings,
        engine=engine,
        session_factory=session_factory,
        executor=StageExecutor(),  # T03：API 测试注入 stub 执行器（不发真实模型调用）
    )
    # T03：测试注入 LocalSandbox（不依赖 Docker；工作区隔离到 tmp_path）
    app.state.sandbox_service = SandboxService(
        session_factory=session_factory,
        sandbox=LocalSandbox(base_dir=tmp_path / "sandboxes"),
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        # T02：取消引擎推进任务，避免事件循环关闭时后台任务泄漏告警。
        # QA-16（v1.2）：必须放在 ``finally`` 里 —— 生成器夹具在**用例失败**时是
        # 把异常抛回 yield 处，yield 之后的语句默认不会执行，于是该 app 的后台推进
        # 任务（持有 engine / session_factory）会活到后续用例里，形成"跨用例污染"：
        # 表现为不相关的用例间歇失败、且复跑即绿。清理必须是异常安全的。
        await app.state.orchestrator.shutdown()
        # QA-16：应用级**可变**注册表（推送通道）显式归零。``client`` 是 function
        # 作用域、每个用例一份新 app，今天靠"新 app"天然隔离；但隔离性不应依赖
        # 这条隐含约定 —— 一旦夹具作用域被改动，注册表就会跨用例泄漏
        # （典型症状：某个用例注入的 fake 通道出现在另一个用例的响应里）。
        exports = getattr(app.state, "exports", None)
        if exports is not None:
            exports._exporters.clear()  # noqa: SLF001 - 夹具负责重置应用级可变状态
