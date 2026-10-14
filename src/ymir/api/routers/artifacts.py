"""产物库接口（§6.1、§3.6）。

- ``GET /projects/{id}/artifacts``：项目产物列表；
- ``GET /artifacts/{id}``：产物详情（含版本历史与血缘）；
- ``GET /artifacts/{id}/versions/{v}/content``：版本文件清单 / 单文件内容；
- ``GET /artifacts/{id}/diff?v1=&v2=``：版本间 unified diff（按文件分组）。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import ArtifactsDep, SessionDep, TenantDep
from ymir.api.schemas import (
    ApiResponse,
    ArtifactContentOut,
    ArtifactDetailOut,
    ArtifactDiffOut,
    ArtifactOut,
    ArtifactVersionOut,
    DiffStatsOut,
    FileDiffOut,
    PageData,
)
from ymir.common.errors import NotFoundError
from ymir.storage.repositories import ProjectRepository

router = APIRouter(tags=["artifacts"])


@router.get(
    "/projects/{project_id}/artifacts",
    response_model=ApiResponse[PageData[ArtifactOut]],
    summary="项目产物列表",
)
async def list_project_artifacts(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    artifacts = await store.list_artifacts(project_id, tenant_id=tenant_id)
    items = [ArtifactOut.model_validate(artifact) for artifact in artifacts]
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ApiResponse[ArtifactDetailOut],
    summary="产物详情（含版本列表）",
)
async def get_artifact(
    artifact_id: str,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    artifact, versions = await store.get_artifact_detail(artifact_id, tenant_id=tenant_id)
    detail = ArtifactDetailOut(
        **ArtifactOut.model_validate(artifact).model_dump(),
        versions=[ArtifactVersionOut.model_validate(row) for row in versions],
    )
    return {"code": 0, "data": detail}


@router.get(
    "/artifacts/{artifact_id}/versions/{version}/content",
    response_model=ApiResponse[ArtifactContentOut],
    summary="版本内容（无 path 返回文件清单，有 path 返回单文件）",
)
async def get_version_content(
    artifact_id: str,
    version: int,
    tenant_id: TenantDep,
    store: ArtifactsDep,
    path: str | None = None,
) -> dict[str, object]:
    loaded = await store.load(artifact_id, version, tenant_id=tenant_id)
    if path is None:
        data = ArtifactContentOut(
            artifact_id=artifact_id,
            version=loaded.version,
            files=sorted(loaded.files),
        )
        return {"code": 0, "data": data}
    if path not in loaded.files:
        raise NotFoundError(f"产物文件不存在: {path}")
    data = ArtifactContentOut(
        artifact_id=artifact_id,
        version=loaded.version,
        path=path,
        content=loaded.files[path],
    )
    return {"code": 0, "data": data}


@router.get(
    "/artifacts/{artifact_id}/diff",
    response_model=ApiResponse[ArtifactDiffOut],
    summary="版本间 diff（unified，按文件分组）",
)
async def get_artifact_diff(
    artifact_id: str,
    v1: int,
    v2: int,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    report = await store.diff(artifact_id, v1, v2, tenant_id=tenant_id)
    data = ArtifactDiffOut(
        artifact_id=artifact_id,
        v1=v1,
        v2=v2,
        files=[FileDiffOut(**item.to_dict()) for item in report.files],
        stats=DiffStatsOut(
            files_changed=report.files_changed,
            additions=report.additions,
            deletions=report.deletions,
        ),
    )
    return {"code": 0, "data": data}
