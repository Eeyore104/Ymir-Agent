"""事件总线（架构设计 §3.4、§6.2）。

统一 emit 入口：
1. 落库（``events`` 表）——seq 按 run 单调递增，服务端写锁串行化分配；
2. 分发给进程内订阅者（SSE 桥实时推送）；订阅者慢消费丢帧时，
   客户端凭 ``Last-Event-ID`` 重连由 :meth:`list_after` 补齐（事件源是库）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.events.log import EventType
from ymir.models.entities import DEFAULT_TENANT_ID, Event

logger = logging.getLogger(__name__)

#: 订阅队列上限（超出丢帧，由重连重放兜底）
_SUBSCRIBER_QUEUE_SIZE = 1000


class EventPublisher:
    """事件写入 + 订阅广播。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        queue_size: int = _SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._queue_size = queue_size
        # 按 run 维度的写锁：seq 仅需 run 内唯一，per-run 锁避免跨 run 全局串行（ARCH-06）。
        # 弱引用字典（QA-06-1）：锁仅在 `async with` 作用域内被强引用，释放后自动回收，
        # 不再随历史 run 数无界增长；同一 run 的并发 emit 仍拿到同一把锁（语义不变）。
        self._write_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = {}

    def _write_lock_for(self, run_id: str) -> asyncio.Lock:
        """取（懒建）某 run 的写锁；同一 run 的 seq 分配串行化。

        无 await 的原子「取或建」，故并发调用不会为同一 run 生成两把锁；
        返回值在 ``async with`` 存续期被强引用，弱引用字典不会在持锁期间回收它。
        """
        lock = self._write_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[run_id] = lock
        return lock

    async def emit(
        self,
        run_id: str,
        event_type: EventType | str,
        payload: dict[str, Any] | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Event:
        """落库并广播一条事件（seq 事务内取 max+1，per-run 写锁串行化）。"""
        type_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
        async with self._write_lock_for(run_id), self._session_factory() as session:
            seq = (
                await session.execute(
                    select(func.coalesce(func.max(Event.seq), 0)).where(Event.run_id == run_id)
                )
            ).scalar_one() + 1
            event = Event(
                run_id=run_id,
                seq=seq,
                type=type_value,
                payload=payload or {},
                tenant_id=tenant_id,
            )
            session.add(event)
            await session.commit()
        self._dispatch(event)
        return event

    async def list_after(
        self, run_id: str, after_seq: int = 0, *, limit: int | None = None
    ) -> list[Event]:
        """重放：返回 seq > after_seq 的历史事件（升序）。"""
        stmt = (
            select(Event)
            .where(Event.run_id == run_id, Event.seq > after_seq)
            .order_by(Event.seq.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[asyncio.Queue[Event]]:
        """订阅某 run 的实时事件（上下文退出自动退订）。"""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        subscribers = self._subscribers.setdefault(run_id, set())
        subscribers.add(queue)
        try:
            yield queue
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    def _dispatch(self, event: Event) -> None:
        for queue in self._subscribers.get(event.run_id, set()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 慢消费客户端：丢弃实时帧，交由重连 + Last-Event-ID 重放补齐
                logger.warning(
                    "SSE 订阅队列已满，丢弃事件 run=%s seq=%s", event.run_id, event.seq
                )


#: 便捷别名：事件回调（供引擎等模块做类型注解）
EventEmitter = Callable[..., Any]
