"""运行相关接口（§6.1）。

T02：发起运行后由编排引擎接管推进（pending -> running）；
新增 pause/resume/cancel/retry-stage 动作与角色消息流查询。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import (
    MessagesDep,
    OrchestratorDep,
    SessionDep,
    SettingsDep,
    TenantDep,
)
from ymir.api.schemas import (
    ApiResponse,
    MessageOut,
    PageData,
    RunActionOut,
    RunActionPayload,
    RunCreate,
    RunCreated,
    RunOut,
)
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.messages.bus import message_summary_of
from ymir.storage.repositories import ProjectRepository, RunRepository

router = APIRouter(tags=["runs"])

#: 运行控制动作白名单（v1.0 四动作 + v1.1 人工介入两动作，§4.5.3）
RUN_ACTIONS: tuple[str, ...] = (
    "pause",
    "resume",
    "cancel",
    "retry-stage",
    "accept-best",
    "extend-repair",
)


@router.post(
    "/projects/{project_id}/runs",
    response_model=ApiResponse[RunCreated],
    status_code=202,
    summary="发起运行",
)
async def create_run(
    project_id: str,
    payload: RunCreate,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    orchestrator: OrchestratorDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    pipeline_id = payload.pipeline_id or settings.default_pipeline_id
    run = await RunRepository(session).create(
        project_id=project_id,
        requirement=payload.requirement,
        pipeline_id=pipeline_id,
        mode=payload.mode,
        context={"context_refs": payload.context_refs},
        tenant_id=tenant_id,
    )
    await session.commit()
    # pending -> running，引擎接管推进（launch 失败时 run 保留为 pending 现场）
    await orchestrator.launch(run.id)
    return {"code": 0, "data": RunCreated(run_id=run.id)}


@router.get("/runs/{run_id}", response_model=ApiResponse[RunOut], summary="运行详情")
async def get_run(
    run_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")
    return {"code": 0, "data": RunOut.model_validate(run)}


@router.post(
    "/runs/{run_id}/actions",
    response_model=ApiResponse[RunActionOut],
    summary="运行控制动作（pause / resume / cancel / retry-stage / accept-best / extend-repair）",
)
async def run_action(
    run_id: str,
    payload: RunActionPayload,
    orchestrator: OrchestratorDep,
    session: SessionDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")
    handlers = {
        "pause": orchestrator.request_pause,
        "resume": orchestrator.request_resume,
        "cancel": orchestrator.request_cancel,
        "retry-stage": orchestrator.request_retry_stage,
        "accept-best": orchestrator.request_accept_best,
        "extend-repair": orchestrator.request_extend_repair,
    }
    handler = handlers.get(payload.action)
    if handler is None:
        raise InvalidParamError(
            f"非法的运行动作 {payload.action!r}，合法动作：{', '.join(RUN_ACTIONS)}"
        )
    updated = await handler(run_id)
    return {
        "code": 0,
        "data": RunActionOut(run_id=run_id, action=payload.action, status=updated.status),
    }


@router.get(
    "/runs/{run_id}/messages",
    response_model=ApiResponse[PageData[MessageOut]],
    summary="角色消息流",
)
async def list_run_messages(
    run_id: str,
    messages: MessagesDep,
    session: SessionDep,
    tenant_id: TenantDep,
    after_seq: int = 0,
    limit: int = 200,
) -> dict[str, object]:
    run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")
    page_size = max(1, min(limit, 500))
    records = await messages.list_by_run(
        run_id, after_seq=max(0, after_seq), limit=page_size, tenant_id=tenant_id
    )
    items = [MessageOut.model_validate(message_summary_of(record)) for record in records]
    return {"code": 0, "data": {"items": items, "total": len(items)}}
