"""运行相关接口（§6.1）。

T01 仅落地：发起运行（创建 pending 记录，返回 202 + run_id）与运行查询。
引擎推进、pause/resume/cancel/retry-stage 留待 T02。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import SessionDep, SettingsDep, TenantDep
from ymir.api.schemas import ApiResponse, RunCreate, RunCreated, RunOut
from ymir.common.errors import NotFoundError
from ymir.storage.repositories import ProjectRepository, RunRepository

router = APIRouter(tags=["runs"])


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
