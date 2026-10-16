"""SSE 事件流接口（§6.1、§6.2）。

``GET /runs/{id}/events``：支持 ``Last-Event-ID`` 头与 ``after_seq`` 查询参数
断线重放（事件源在库，实时帧经订阅队列分发）。

注意：SSE 为长连接，禁止在流存续期持有请求级 Session（会占满连接池），
run 存在性检查走独立短会话。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ymir.api.deps import EventsDep, SettingsDep, TenantDep
from ymir.common.errors import NotFoundError
from ymir.events.sse import stream_run_events
from ymir.storage.repositories import RunRepository

router = APIRouter(tags=["events"])


@router.get("/runs/{run_id}/events", summary="SSE 事件流（支持断线重放）")
async def stream_events(
    run_id: str,
    request: Request,
    events: EventsDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
    after_seq: int | None = Query(default=None, ge=0, description="从该 seq 之后重放"),
) -> StreamingResponse:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")

    resume_from = after_seq
    if resume_from is None:
        header = request.headers.get("last-event-id", "")
        resume_from = int(header) if header.isdigit() else 0

    return StreamingResponse(
        stream_run_events(
            events, run_id, after_seq=resume_from, heartbeat=settings.sse_heartbeat_seconds
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 反向代理下禁用缓冲
        },
    )
