"""项目相关接口（§6.1）。"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import SessionDep, SettingsDep, TenantDep
from ymir.api.schemas import (
    ApiResponse,
    PageData,
    ProjectCreate,
    ProjectDetail,
    ProjectOut,
)
from ymir.common.errors import NotFoundError
from ymir.storage.repositories import ProjectRepository, RunRepository

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ApiResponse[ProjectOut], summary="创建项目")
async def create_project(
    payload: ProjectCreate,
    session: SessionDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    repo = ProjectRepository(session)
    project = await repo.create(
        name=payload.name, description=payload.description, tenant_id=tenant_id
    )
    await session.commit()
    return {"code": 0, "data": ProjectOut.model_validate(project)}


@router.get("", response_model=ApiResponse[PageData[ProjectOut]], summary="项目列表")
async def list_projects(
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, object]:
    page_size = settings.default_page_size if limit is None else limit
    page_size = max(1, min(page_size, settings.max_page_size))
    repo = ProjectRepository(session)
    items = await repo.list(tenant_id=tenant_id, limit=page_size, offset=max(0, offset))
    total = await repo.count(tenant_id=tenant_id)
    return {
        "code": 0,
        "data": {
            "items": [ProjectOut.model_validate(item) for item in items],
            "total": total,
        },
    }


@router.get(
    "/{project_id}", response_model=ApiResponse[ProjectDetail], summary="项目详情"
)
async def get_project(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    run_count = await RunRepository(session).count_by_project(project_id, tenant_id)
    detail = ProjectDetail(
        **ProjectOut.model_validate(project).model_dump(), run_count=run_count
    )
    return {"code": 0, "data": detail}
