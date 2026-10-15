"""消息总线（架构设计 §3.3）。

发布角色间消息：seq 按 run 单调递增（与事件同构的写锁串行化），
落库后发 ``message.created`` 事件（SSE 消息流面板的实时源）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.events.bus import EventPublisher
from ymir.events.log import EventType
from ymir.messages.schema import MessageCreate
from ymir.models.entities import DEFAULT_TENANT_ID, Message


class MessagePublisher:
    """消息写入 + 事件联动。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventPublisher,
    ) -> None:
        self._session_factory = session_factory
        self._events = events
        self._write_lock = asyncio.Lock()

    async def publish(
        self,
        run_id: str,
        message: MessageCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Message:
        """落库一条消息并发出 ``message.created`` 事件。"""
        async with self._write_lock, self._session_factory() as session:
            seq = (
                await session.execute(
                    select(func.coalesce(func.max(Message.seq), 0)).where(
                        Message.run_id == run_id
                    )
                )
            ).scalar_one() + 1
            record = Message(
                run_id=run_id,
                seq=seq,
                type=message.type.value,
                from_role=message.from_role,
                to_role=message.to_role,
                content_refs=list(message.content_refs),
                summary=message.summary,
                payload=dict(message.payload),
                tenant_id=tenant_id,
            )
            session.add(record)
            await session.commit()

        await self._events.emit(
            run_id,
            EventType.MESSAGE_CREATED,
            {
                "message_id": record.id,
                "seq": record.seq,
                "type": record.type,
                "from_role": record.from_role,
                "to_role": record.to_role,
                "content_refs": record.content_refs,
                "summary": record.summary,
                "payload": record.payload,
                "created_at": record.created_at.isoformat(),
            },
            tenant_id=tenant_id,
        )
        return record

    async def list_by_run(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[Message]:
        """按 run 拉取消息（升序，供 ``GET /runs/{id}/messages``）。"""
        stmt = (
            select(Message)
            .where(
                Message.run_id == run_id,
                Message.seq > after_seq,
                Message.tenant_id == tenant_id,
            )
            .order_by(Message.seq.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())


def message_summary_of(record: Message) -> dict[str, Any]:
    """消息的简化视图（供 API 序列化）。"""
    return {
        "id": record.id,
        "run_id": record.run_id,
        "seq": record.seq,
        "type": record.type,
        "from_role": record.from_role,
        "to_role": record.to_role,
        "content_refs": record.content_refs,
        "summary": record.summary,
        "payload": record.payload,
        "created_at": record.created_at.isoformat(),
    }
