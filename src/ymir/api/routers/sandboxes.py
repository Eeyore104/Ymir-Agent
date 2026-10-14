"""沙箱接口（§6.1、§3.5）。

会话创建为执行器内部行为（coding 阶段冒烟 / T05 评测），
API 仅暴露查询：列表（可按 run 过滤）与详情。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import SandboxesDep, TenantDep
from ymir.api.schemas import ApiResponse, PageData, SandboxSessionOut

router = APIRouter(tags=["sandboxes"])


@router.get(
    "/sandboxes",
    response_model=ApiResponse[PageData[SandboxSessionOut]],
    summary="沙箱会话列表（可按 run 过滤）",
)
async def list_sandboxes(
    tenant_id: TenantDep,
    service: SandboxesDep,
    run_id: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    page_size = max(1, min(limit, 500))
    records = await service.list_sessions(
        run_id=run_id, limit=page_size, tenant_id=tenant_id
    )
    items = [SandboxSessionOut.model_validate(record) for record in records]
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.get(
    "/sandboxes/{session_id}",
    response_model=ApiResponse[SandboxSessionOut],
    summary="沙箱会话详情",
)
async def get_sandbox(
    session_id: str,
    tenant_id: TenantDep,
    service: SandboxesDep,
) -> dict[str, object]:
    record = await service.get(session_id, tenant_id=tenant_id)
    return {"code": 0, "data": SandboxSessionOut.model_validate(record)}
