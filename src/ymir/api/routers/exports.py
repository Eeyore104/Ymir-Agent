"""推送接口（v1.2 · T4.1，docs/09 §4.3.1 / §4.4.3）。

- ``POST /projects/{id}/exports``：发起推送（``dry_run=true`` 只过滤不推送）；
- ``GET /projects/{id}/exports``：回执列表（时间倒序）。

**错误码口径**（与 T3.4 §C 定稿一致）：

- 1001 入参非法（``ExportCreate`` ``extra="forbid"``、受保护分支、repo/branch 格式）；
- 1002 项目 / 产物 / 通道不存在；
- 1003 远端 ref 冲突（GitHub 422 / 分支保护），**不是**"推送失败"；
- 1007 推送失败（鉴权 / 权限 / 配额 / 网络 / 未配 token）。

失败时**回执已落库**（审计不丢），再把错误以 1007/1003 抛给前端。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import (
    ExportServiceDep,
    SessionDep,
    TenantDep,
)
from ymir.api.schemas import (
    ApiResponse,
    BlockedFileOut,
    ExportCreate,
    ExportReceiptOut,
    PageData,
)
from ymir.artifacts.exporter import ExportRequest
from ymir.common.errors import ExportError, NotFoundError, StateConflictError
from ymir.models.entities import Export
from ymir.storage.repositories import ProjectRepository

router = APIRouter(tags=["exports"])

#: 远端 422（ref 冲突 / 分支保护）→ 1003；其余失败 → 1007
_CONFLICT_CLASSES = frozenset({"validation_failed"})


@router.post(
    "/projects/{project_id}/exports",
    response_model=ApiResponse[ExportReceiptOut],
    status_code=202,
    summary="发起推送（dry_run=true 只预演；否则真推）",
)
async def create_export(
    project_id: str,
    payload: ExportCreate,
    session: SessionDep,
    tenant_id: TenantDep,
    service: ExportServiceDep,
) -> dict[str, object]:
    """选树 → 过滤 → 幂等 → 推送 → 回执（docs/09 §4.3.1）。

    ``dry_run=true``（**缺省值**）只做过滤与预演，不产生任何网络请求。
    """
    body = payload
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    request = ExportRequest(
        project_id=project_id,
        artifact_id=body.artifact_id,
        artifact_version=body.artifact_version,
        target=body.target,
        tree=body.tree,
        dry_run=body.dry_run,
        strict_secret_check=body.strict_secret_check,
    )
    receipt = await service.export(
        project_id=project_id, request=request, tenant_id=tenant_id
    )
    _raise_for_failure(receipt)
    return {"code": 0, "data": _to_out(receipt)}


@router.get(
    "/projects/{project_id}/exports",
    response_model=ApiResponse[PageData[ExportReceiptOut]],
    summary="推送回执列表（时间倒序）",
)
async def list_exports(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    service: ExportServiceDep,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """项目推送历史（每次尝试一条，含 dry_run 与失败）。"""
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    receipts = await service.list_receipts(
        project_id, tenant_id=tenant_id, limit=max(1, min(limit, 200))
    )
    items = [_to_out(row) for row in receipts[offset:]]
    return {"code": 0, "data": {"items": items, "total": len(receipts)}}


def _raise_for_failure(receipt: Export) -> None:
    """失败回执 → 抛业务异常（回执已落库，审计不丢）。"""
    if receipt.status != "failed":
        return
    message = receipt.error_message or "推送失败"
    if receipt.error_class in _CONFLICT_CLASSES:
        raise StateConflictError(message)
    raise ExportError(message)


def _to_out(receipt: Export) -> ExportReceiptOut:
    """ORM 回执 → 响应模型（只暴露路径与原因，**不含任何密钥/正文**）。"""
    detail = receipt.detail if isinstance(receipt.detail, dict) else {}
    return ExportReceiptOut(
        id=receipt.id,
        artifact_id=receipt.artifact_id,
        artifact_version=receipt.artifact_version,
        tree=receipt.tree_mode,
        target_kind=receipt.target_kind,
        repo=receipt.repo,
        branch=receipt.branch,
        path_prefix=receipt.path_prefix,
        status=receipt.status,
        commit_sha=receipt.commit_sha,
        file_count=receipt.file_count,
        blocked_count=receipt.blocked_count,
        skipped_count=receipt.skipped_count,
        error_code=receipt.error_code,
        error_class=receipt.error_class,
        error_message=receipt.error_message,
        idempotency_key=receipt.idempotency_key,
        dry_run=receipt.dry_run,
        blocked_files=_blocked_out(detail.get("blocked_files")),
        skipped_files=_blocked_out(detail.get("skipped_files")),
        created_at=receipt.created_at,
    )


def _blocked_out(raw: object) -> list[BlockedFileOut]:
    """``detail`` 里的 ``{path, reason}`` 列表 → 响应模型（丢弃脏项）。"""
    if not isinstance(raw, list):
        return []
    return [
        BlockedFileOut(path=str(item.get("path", "")), reason=str(item.get("reason", "")))
        for item in raw
        if isinstance(item, dict)
    ]


__all__ = ["router"]
