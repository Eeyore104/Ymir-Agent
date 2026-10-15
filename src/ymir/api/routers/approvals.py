"""审批接口（架构设计 §6.1、§3.8）。

- ``GET /approvals``：审批列表（审批中心；``?status=pending&run_id=`` 过滤），
  列表项附 run 摘要（项目名 / 需求 / 模式 / 状态）供审批卡片渲染；
- ``GET /approvals/{id}``：审批详情（记录 + run 摘要 + 历史反馈链）；
- ``POST /approvals/{id}/decision``：决策（``{decision, feedback?, decided_by?}``）；
  重复决策 / 非 pending -> 1003，审批不存在 -> 1002。

响应遵循统一信封（§6.3）：成功 ``{code:0,data}``；列表
``{code:0,data:{items,total}}``。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ymir.api.deps import ApprovalsDep, TenantDep
from ymir.api.schemas import (
    ApiResponse,
    ApprovalDecisionPayload,
    ApprovalDetail,
    ApprovalFeedbackOut,
    ApprovalListItem,
    ApprovalOut,
    PageData,
    RunBriefOut,
)
from ymir.hitl.service import ApprovalDetailView, ApprovalView, RunBrief

router = APIRouter(tags=["approvals"])


def _run_brief_out(brief: RunBrief | None) -> RunBriefOut | None:
    """run 摘要序列化（缺失时返回 None）。"""
    if brief is None:
        return None
    return RunBriefOut.model_validate(brief)


def _to_list_item(view: ApprovalView) -> ApprovalListItem:
    """审批列表项（聚合视图 -> API 模型）。"""
    return ApprovalListItem(
        **ApprovalOut.model_validate(view.approval).model_dump(),
        run=_run_brief_out(view.run),
    )


def _to_detail(view: ApprovalDetailView) -> ApprovalDetail:
    """审批详情（聚合视图 -> API 模型，含历史反馈链）。"""
    return ApprovalDetail(
        **ApprovalOut.model_validate(view.approval).model_dump(),
        run=_run_brief_out(view.run),
        feedback_history=[
            ApprovalFeedbackOut.model_validate(message) for message in view.feedback_history
        ],
    )


@router.get(
    "/approvals",
    response_model=ApiResponse[PageData[ApprovalListItem]],
    summary="审批列表（审批中心）",
)
async def list_approvals(
    service: ApprovalsDep,
    tenant_id: TenantDep,
    status: str | None = Query(default=None, description="按状态过滤：pending/approved/rejected"),
    run_id: str | None = Query(default=None, description="按运行 ID 过滤"),
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
) -> dict[str, object]:
    views = await service.list_approvals(
        status=status, run_id=run_id, tenant_id=tenant_id, limit=limit, offset=offset
    )
    items = [_to_list_item(view) for view in views]
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.get(
    "/approvals/{approval_id}",
    response_model=ApiResponse[ApprovalDetail],
    summary="审批详情（阶段摘要 + 待审产物 + 历史反馈链）",
)
async def get_approval(
    approval_id: str,
    service: ApprovalsDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    view = await service.get_approval(approval_id, tenant_id=tenant_id)
    return {"code": 0, "data": _to_detail(view)}


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApiResponse[ApprovalOut],
    summary="审批决策（approve / reject，驳回带反馈回退重跑）",
)
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionPayload,
    service: ApprovalsDep,
) -> dict[str, object]:
    approval = await service.decide(
        approval_id,
        decision=payload.decision,
        feedback=payload.feedback,
        decided_by=payload.decided_by,
    )
    return {"code": 0, "data": ApprovalOut.model_validate(approval)}
