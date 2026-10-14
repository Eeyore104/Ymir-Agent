"""用量统计接口（§6.1）：``GET /usage?scope=run|project|global``。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from ymir.api.deps import GatewayDep
from ymir.api.schemas import ApiResponse, UsageOut
from ymir.common.errors import InvalidParamError

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=ApiResponse[UsageOut], summary="token / 成本统计")
async def get_usage(
    gateway: GatewayDep,
    scope: Literal["run", "project", "global"] = Query(default="global"),
    run_id: str | None = Query(default=None, description="scope=run 时必填"),
    project_id: str | None = Query(default=None, description="scope=project 时必填"),
) -> dict[str, object]:
    if scope == "run" and not run_id:
        raise InvalidParamError("scope=run 需要提供 run_id")
    if scope == "project" and not project_id:
        raise InvalidParamError("scope=project 需要提供 project_id")

    totals = await gateway.recorder.stats(scope, run_id=run_id, project_id=project_id)
    data = UsageOut(
        scope=scope,
        calls=totals.calls,
        prompt_tokens=totals.prompt_tokens,
        completion_tokens=totals.completion_tokens,
        total_tokens=totals.total_tokens,
        cost_cny=round(totals.cost_cny, 6),
    )
    return {"code": 0, "data": data}
