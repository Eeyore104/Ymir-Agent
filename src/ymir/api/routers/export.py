"""导出接口（§3.6、§6.1）：整库导出 zip / 合并树导出（v1.2）。

``GET /projects/{id}/export`` 有两种口径（``mode`` 查询参数）：

- ``mode=latest``（**缺省**）：v1.1 行为，各产物**最新版本**内容 +
  ``manifest.json``（含版本历史元数据），zip 内为 ``{name}/v{n}/{path}``；
- ``mode=merged``（v1.2，docs/09 §9-T2.5 ③）：把「基线 ⊕ 补丁」的**合并树**打成
  **可运行工程布局** —— 顶层直接 ``app/main.py``，无 ``code/v1/`` 前缀，
  ``manifest.json`` 额外标注 ``mode="merged"``。
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from ymir.api.deps import ArtifactsDep, SessionDep, TenantDep
from ymir.common.errors import NotFoundError
from ymir.storage.repositories import ProjectRepository

router = APIRouter(tags=["export"])


@router.get(
    "/projects/{project_id}/export",
    summary="导出 zip（mode=latest 整库 / mode=merged 合并树扁平布局）",
)
async def export_project(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
    mode: Literal["latest", "merged"] = "latest",
) -> Response:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    payload = await store.export_zip(project_id, tenant_id=tenant_id, mode=mode)
    # 中文项目名走 RFC 5987（filename*）编码；ASCII 回退名保证兼容
    ascii_name = f"ymir-{project_id[:8]}.zip"
    utf8_name = quote(f"ymir-{project.name}-{project_id[:8]}.zip")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
    headers = {
        "Content-Disposition": disposition,
        "X-Manifest": "manifest.json",
    }
    # ★只有 merged 模式才加标记头：缺省（latest）响应头必须与 v1.1 逐字节一致（§10-5）
    if mode == "merged":
        headers["X-Export-Mode"] = "merged"
    return Response(content=payload, media_type="application/zip", headers=headers)
