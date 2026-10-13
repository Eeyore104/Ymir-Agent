"""数据库引擎与会话（SQLAlchemy 2.0 async + aiosqlite）。

- SQLite 开启 WAL 模式与外键约束（§3.10、N5）；
- 引擎创建为同步操作（不建连接），便于应用启动即持有。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ymir.config import Settings, get_settings


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """按配置构建异步引擎，并注册 SQLite PRAGMA（WAL + 外键）。"""
    settings = settings or get_settings()
    settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        settings.sqlalchemy_url,
        echo=settings.debug,
        future=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def build_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """构建会话工厂（``expire_on_commit=False`` 便于返回已提交对象）。"""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """会话上下文：自动提交，异常回滚。"""
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
