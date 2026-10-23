"""产物库接口（§6.1、§3.6）。

- ``GET /projects/{id}/artifacts``：项目产物列表；
- ``GET /artifacts/{id}``：产物详情（含版本历史与血缘）；
- ``GET /artifacts/{id}/versions/{v}/content``：版本文件清单 / 单文件内容；
- ``GET /artifacts/{id}/diff?v1=&v2=``：版本间 unified diff（按文件分组）；
- ``POST /projects/{id}/baselines``：**上传现有工程快照**（v1.2，docs/09 §4.2.1）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request

from ymir.api.deps import ArtifactsDep, SessionDep, SettingsDep, TenantDep
from ymir.api.schemas import (
    ApiResponse,
    ArtifactContentOut,
    ArtifactDetailOut,
    ArtifactDiffOut,
    ArtifactOut,
    ArtifactVersionOut,
    BaselineUploadOut,
    DiffStatsOut,
    FileDiffOut,
    PageData,
)
from ymir.artifacts.baseline import read_zip_files
from ymir.artifacts.diff import diff_file_sets
from ymir.artifacts.store import BASELINE_TYPE
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.common.paths import normalize_rel_path
from ymir.config import Settings
from ymir.storage.repositories import ProjectRepository

router = APIRouter(tags=["artifacts"])

#: 基线产物名（同项目内复用同名 artifact，多次上传即 v1 -> v2 -> …）
BASELINE_ARTIFACT_NAME = "baseline"


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


@router.get(
    "/projects/{project_id}/diff-across",
    response_model=ApiResponse[ArtifactDiffOut],
    summary="跨产物对比（基线 vs 合并结果，复用 DiffReport 结构）",
)
async def diff_across(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
    base: str = "baseline",
    target: str = "code",
) -> dict[str, object]:
    """跨产物 diff（v1.2，docs/09 §4.2.4）：``base`` 与「基线 ⊕ 补丁」的对比。

    ``base`` / ``target`` 形如 ``name`` 或 ``name@version``（缺省 ``baseline`` /
    ``code`` 的最新版本）。返回复用 :class:`ArtifactDiffOut` —— 前端 ``DiffView.vue``
    与 ``stores/artifacts.ts`` **零改动**即可渲染（DoD ⑤）。

    Raises:
        NotFoundError: 项目不存在 / 产物或版本不存在（1002）。
        InvalidParamError: 项目**没有基线**（1001，提示改用同产物 diff）。
    """
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    base_name, base_version = _split_ref(base)
    target_name, target_version = _split_ref(target)

    base_latest = await store.latest_by_name(project_id, base_name, tenant_id=tenant_id)
    if base_latest is None:
        # 无基线项目是**入参问题**（1001，提示改用同产物 diff）；
        # 只有"基线存在但版本不存在"才走 1002。
        if base_name == "baseline":
            raise InvalidParamError(
                "项目没有基线，无法做跨产物对比；请先用 "
                "POST /projects/{id}/baselines 上传基线，或改用 GET /artifacts/{id}/diff"
            )
        raise NotFoundError(f"产物不存在: {base_name}")
    base_loaded = (
        base_latest
        if base_version in (None, base_latest.version)
        else await store.load(base_latest.artifact.id, base_version, tenant_id=tenant_id)
    )
    target_loaded = await _load_named(store, project_id, target_name, target_version, tenant_id)

    if base_loaded.artifact.type != BASELINE_TYPE:
        raise InvalidParamError(
            f"{base_name} 不是基线（type={base_loaded.artifact.type}）；"
            "无基线项目请改用 GET /artifacts/{id}/diff"
        )

    merged = await store.merged_tree(
        project_id, name=target_name, version=target_version or None, tenant_id=tenant_id
    )
    # 复用纯函数，不重写 diff（DoD ①）
    report = diff_file_sets(base_loaded.files, merged)
    data = ArtifactDiffOut(
        artifact_id=target_loaded.artifact.id,
        v1=base_loaded.version,
        v2=target_loaded.version,
        files=[FileDiffOut(**item.to_dict()) for item in report.files],
        stats=DiffStatsOut(
            files_changed=report.files_changed,
            additions=report.additions,
            deletions=report.deletions,
        ),
    )
    return {"code": 0, "data": data}


def _split_ref(raw: str) -> tuple[str, int | None]:
    """``name@version`` -> ``(name, version)``；无 ``@`` 时版本为 ``None``（= latest）。"""
    name, _, version_text = raw.partition("@")
    name = name.strip()
    if not name:
        raise InvalidParamError(f"非法的产物引用: {raw!r}（形如 name 或 name@version）")
    if not version_text:
        return name, None
    try:
        version = int(version_text)
    except ValueError as exc:
        raise InvalidParamError(f"非法的产物版本号: {raw!r}") from exc
    if version < 1:
        raise InvalidParamError(f"产物版本号必须 >= 1: {raw!r}")
    return name, version


async def _load_named(
    store: ArtifactsDep, project_id: str, name: str, version: int | None, tenant_id: str
) -> Any:
    """按「项目 + 名字（+ 版本）」加载产物（不存在 → 1002）。"""
    latest = await store.latest_by_name(project_id, name, tenant_id=tenant_id)
    if latest is None:
        raise NotFoundError(f"产物不存在: {name}")
    if version is None or version == latest.version:
        return latest
    return await store.load(latest.artifact.id, version, tenant_id=tenant_id)


@router.post(
    "/projects/{project_id}/baselines",
    response_model=ApiResponse[BaselineUploadOut],
    status_code=201,
    summary="上传现有工程快照为基线（zip 原始字节，或 JSON files 映射）",
)
async def upload_baseline(
    project_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    """把用户的现有工程落成 ``type=baseline`` 的**不可变产物**（docs/09 §4.2.1）。

    两种入参形态（**都不接受服务端路径**，规避 LFI，§4.5.2）：

    - ``Content-Type: application/zip``（前端 / curl）：请求体 = zip 原始字节；
    - ``Content-Type: application/json``（仅测试 / 脚本）：``{"files": {"a.py": "..."}}``。

    为什么不是 ``multipart/form-data``：FastAPI 的 ``UploadFile`` 依赖
    ``python-multipart``，而 v1.2 明确"Python 依赖零新增"（docs/09 §8）。
    原始字节上传与会话级 multipart 在语义上等价，且前端可直接
    ``axios.post(url, file, {headers: {"Content-Type": "application/zip"}})``。

    Raises:
        NotFoundError: 项目不存在（1002）。
        InvalidParamError: 非 zip / zip slip / 超上限 / 非 UTF-8 / 空基线（1001）。
    """
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    body = await request.body()
    if not body:
        raise InvalidParamError("基线内容为空（请上传 zip 或 JSON files 映射）")

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json" or body.lstrip().startswith(b"{"):
        files, size_bytes = _files_from_json(body, settings=settings)
    else:
        files, size_bytes = read_zip_files(
            body,
            max_files=settings.baseline_max_files,
            max_bytes=settings.baseline_max_bytes,
        )

    # 落盘前校验已完成（路径/上限/编码），store.save 再做一次归一化 + 原子占位
    saved = await store.save(
        project_id=project_id,
        name=BASELINE_ARTIFACT_NAME,
        artifact_type=BASELINE_TYPE,
        files=files,
        created_by="user",
        message=f"用户上传基线（{len(files)} 文件 / {size_bytes} 字节）",
        tenant_id=tenant_id,
    )
    return {
        "code": 0,
        "data": BaselineUploadOut(
            artifact_id=saved.artifact_id,
            version=saved.version,
            file_count=saved.file_count,
            content_hash=saved.content_hash,
            size_bytes=saved.size_bytes,
        ),
    }


def _files_from_json(body: bytes, *, settings: Settings) -> tuple[dict[str, str], int]:
    """解析 ``{"files": {path: content}}``（仅测试/脚本通道）。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidParamError(f"基线 JSON 解析失败: {exc}") from exc
    raw_files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw_files, dict) or not raw_files:
        raise InvalidParamError("基线 JSON 必须形如 {\"files\": {\"path\": \"content\"}}")
    if len(raw_files) > settings.baseline_max_files:
        raise InvalidParamError(
            f"基线文件数 {len(raw_files)} 超过上限 {settings.baseline_max_files}"
        )
    files: dict[str, str] = {}
    size_bytes = 0
    for raw_path, raw_content in raw_files.items():
        if not isinstance(raw_content, str):
            raise InvalidParamError(f"基线文件内容必须是字符串: {raw_path}")
        path = normalize_rel_path(str(raw_path))
        if path in files:
            raise InvalidParamError(f"基线包含重复路径: {path}")
        files[path] = raw_content
        size_bytes += len(raw_content.encode("utf-8"))
    if size_bytes > settings.baseline_max_bytes:
        raise InvalidParamError(
            f"基线内容 {size_bytes} 字节超过上限 {settings.baseline_max_bytes}"
        )
    return files, size_bytes
