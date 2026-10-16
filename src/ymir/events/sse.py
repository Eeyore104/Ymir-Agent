"""SSE 桥（架构设计 §6.2）：事件 -> SSE 帧。

- 统一信封 ``{seq, type, run_id, timestamp, payload}``；
- 支持 ``Last-Event-ID`` / ``after_seq`` 断线重放：先订阅（收实时）再重放历史，
  按 seq 去重，保证「重放与实时」衔接无缝；
- 心跳保活（默认 15s 发注释帧），防代理 / 浏览器超时断连。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from ymir.events.bus import EventPublisher
from ymir.models.entities import Event

#: 心跳间隔（秒）
SSE_HEARTBEAT_SECONDS = 15.0

#: 断线重放分页大小（避免超长 run 一次性全量载入，QA-11）
_REPLAY_PAGE_SIZE = 500


def format_sse(event: Event) -> str:
    """把一条事件格式化为 SSE 帧（id / event / data + 空行）。"""
    envelope = {
        "seq": event.seq,
        "type": event.type,
        "run_id": event.run_id,
        "timestamp": event.created_at.isoformat(),
        "payload": event.payload,
    }
    data = json.dumps(envelope, ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


async def stream_run_events(
    publisher: EventPublisher,
    run_id: str,
    *,
    after_seq: int = 0,
    heartbeat: float = SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """产出某 run 的 SSE 帧流：先重放缺口，再实时推送。

    去重水位 = 「本次重放实际发出的最大 seq」（无重放则为 0），**不能**用
    ``after_seq`` 初始化：越界的 ``after_seq``（如旧库残留 seq）会抑制后续
    实时帧（T02 QA 报告 P3 修复）。
    """
    async with publisher.subscribe(run_id) as queue:
        replayed_upto = 0
        cursor = after_seq or 0
        # 分页重放：每页 _REPLAY_PAGE_SIZE 条，直到走完历史（QA-11 分页上限）
        while True:
            batch = await publisher.list_after(run_id, cursor, limit=_REPLAY_PAGE_SIZE)
            if not batch:
                break
            for event in batch:
                yield format_sse(event)
                replayed_upto = max(replayed_upto, event.seq)
            cursor = batch[-1].seq
            if len(batch) < _REPLAY_PAGE_SIZE:
                break

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event.seq <= replayed_upto:
                continue  # 去重：该事件已在重放阶段发出
            replayed_upto = event.seq
            yield format_sse(event)
