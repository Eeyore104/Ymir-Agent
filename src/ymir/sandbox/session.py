"""沙箱会话服务（架构设计 §3.5）：对外统一入口，负责生命周期与 DB 记录。

把「沙箱后端（可换）」与「会话记录（``sandbox_sessions`` 表）」绑定：
``start`` 落记录并启动会话、``stop`` / ``destroy`` 更新记录状态；
路由层 / 执行器只与本服务交互，不直接触达后端细节。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.errors import NotFoundError
from ymir.models.base import utcnow
from ymir.models.entities import DEFAULT_TENANT_ID
from ymir.models.entities import SandboxSession as SandboxSessionRecord
from ymir.models.enums import SandboxStatus
from ymir.sandbox.base import NetworkPolicy, Sandbox, SandboxSession
from ymir.sandbox.local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)


@dataclass
class ManagedSession:
    """被服务管理的会话（DB 记录 + 运行时会话）。"""

    record_id: str
    run_id: str
    sandbox: Sandbox
    session: SandboxSession


class SandboxService:
    """沙箱会话服务（后端可懒加载：首次使用才做 Docker 探测 / 降级决策）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sandbox: Sandbox | None = None,
        sandbox_loader: Callable[[], Sandbox] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._sandbox = sandbox
        self._sandbox_loader = sandbox_loader

    @property
    def sandbox(self) -> Sandbox:
        """解析沙箱后端（显式注入 > loader > LocalSandbox 兜底）。"""
        if self._sandbox is None:
            if self._sandbox_loader is not None:
                self._sandbox = self._sandbox_loader()
            else:
                self._sandbox = LocalSandbox()
        return self._sandbox

    # ------------------------------------------------------------------ 生命周期

    async def start(
        self,
        run_id: str,
        *,
        network: NetworkPolicy = NetworkPolicy.NONE,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ManagedSession:
        """启动会话：先落记录（created），再启动后端并更新为 running。"""
        sandbox = self.sandbox
        async with self._session_factory() as session:
            record = SandboxSessionRecord(
                run_id=run_id,
                image=getattr(sandbox, "image", "unknown"),
                status=SandboxStatus.CREATED.value,
                tenant_id=tenant_id,
            )
            session.add(record)
            await session.commit()
            record_id = record.id

        try:
            sandbox_session = await sandbox.start(network=network)
        except Exception:
            await self._update_record(record_id, status=SandboxStatus.STOPPED.value)
            logger.exception("沙箱会话启动失败（记录 %s 保留供排查）", record_id)
            raise

        await self._update_record(
            record_id,
            status=SandboxStatus.RUNNING.value,
            container_id=sandbox_session.sandbox_id,
        )
        return ManagedSession(
            record_id=record_id,
            run_id=run_id,
            sandbox=sandbox,
            session=sandbox_session,
        )

    async def stop(self, managed: ManagedSession) -> None:
        """停止会话（保留现场）。"""
        await managed.sandbox.stop(managed.session)
        await self._update_record(managed.record_id, status=SandboxStatus.STOPPED.value)

    async def destroy(self, managed: ManagedSession) -> None:
        """销毁会话（释放资源，记录标记 destroyed）。"""
        await managed.sandbox.destroy(managed.session)
        await self._update_record(
            managed.record_id,
            status=SandboxStatus.DESTROYED.value,
            destroyed=True,
        )

    # ------------------------------------------------------------------ 查询

    async def list_sessions(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[SandboxSessionRecord]:
        stmt = select(SandboxSessionRecord).where(
            SandboxSessionRecord.tenant_id == tenant_id
        )
        if run_id is not None:
            stmt = stmt.where(SandboxSessionRecord.run_id == run_id)
        stmt = stmt.order_by(
            SandboxSessionRecord.created_at.desc(), SandboxSessionRecord.id.desc()
        ).limit(limit)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get(
        self, session_id: str, *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> SandboxSessionRecord:
        async with self._session_factory() as session:
            record = await session.get(SandboxSessionRecord, session_id)
            if record is None or record.tenant_id != tenant_id:
                raise NotFoundError(f"沙箱会话不存在: {session_id}")
            return record

    # ------------------------------------------------------------------ 内部

    async def _update_record(
        self,
        record_id: str,
        *,
        status: str,
        container_id: str | None = None,
        destroyed: bool = False,
    ) -> None:
        async with self._session_factory() as session:
            record = await session.get(SandboxSessionRecord, record_id)
            if record is None:
                return
            record.status = status
            if container_id is not None:
                record.container_id = container_id
            if destroyed:
                record.destroyed_at = utcnow()
            await session.commit()
