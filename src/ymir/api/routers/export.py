"""导出接口（§3.6、§6.1）：整库导出 zip。

``GET /projects/{id}/export`` 打包项目全部产物的**最新版本**内容 +
``manifest.json``（含版本历史元数据），可直接解压还原项目全貌。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from ymir.api.deps import ArtifactsDep, SessionDep, TenantDep
from ymir.common.errors import NotFoundError
from ymir.storage.repositories import ProjectRepository

router = APIRouter(tags=["export"])


@router.get("/projects/{project_id}/export", summary="整库导出 zip（含 manifest.json）")
async def export_project(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> Response:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    payload = await store.export_zip(project_id, tenant_id=tenant_id)
    # 中文项目名走 RFC 5987（filename*）编码；ASCII 回退名保证兼容
    ascii_name = f"ymir-{project_id[:8]}.zip"
    utf8_name = quote(f"ymir-{project.name}-{project_id[:8]}.zip")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "X-Manifest": "manifest.json",
        },
    )
