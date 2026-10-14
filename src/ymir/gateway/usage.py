"""用量计量（架构设计 §3.9）。

每次模型调用落 ``model_calls`` 表（token / 成本 / 延迟），
并向预算守卫与 ``/usage`` 端点提供按 run / project / global 的汇总查询。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.gateway.providers import ModelSpec
from ymir.models.entities import DEFAULT_TENANT_ID, ModelCall, PipelineRun


def calc_cost(spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
    """按价格表计算单次调用成本（元）。价格单位：元 / 百万 token。"""
    return (
        prompt_tokens / 1_000_000 * spec.price_in
        + completion_tokens / 1_000_000 * spec.price_out
    )


def _day_start(now: datetime | None = None) -> datetime:
    """返回 UTC 当日 00:00（预算的「日」按 UTC 日历日口径）。"""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class UsageTotals:
    """一段口径下的用量汇总。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class UsageRecorder:
    """``model_calls`` 的写入与汇总。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        provider: str,
        model: str,
        tier: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_cny: float,
        latency_ms: int,
        run_id: str | None = None,
        stage_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> str:
        """写入一条调用记录并返回其 ID（独立事务，引擎线程安全）。"""
        call = ModelCall(
            run_id=run_id,
            stage_id=stage_id,
            provider=provider,
            model=model,
            tier=tier,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cny=cost_cny,
            latency_ms=latency_ms,
            tenant_id=tenant_id,
        )
        async with self._session_factory() as session:
            session.add(call)
            await session.commit()
            return call.id

    async def totals(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        since: datetime | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> UsageTotals:
        """按 run / project / since 口径汇总用量。

        优先级：project_id 存在时按项目 join 聚合；否则按 run_id（若给）；
        否则全局；``since`` 可叠加为时间下界。
        """
        stmt = select(
            func.count(ModelCall.id),
            func.coalesce(func.sum(ModelCall.prompt_tokens), 0),
            func.coalesce(func.sum(ModelCall.completion_tokens), 0),
            func.coalesce(func.sum(ModelCall.cost_cny), 0.0),
        ).where(ModelCall.tenant_id == tenant_id)
        if project_id is not None:
            stmt = stmt.join(PipelineRun, ModelCall.run_id == PipelineRun.id).where(
                PipelineRun.project_id == project_id
            )
        elif run_id is not None:
            stmt = stmt.where(ModelCall.run_id == run_id)
        if since is not None:
            stmt = stmt.where(ModelCall.created_at >= since)

        async with self._session_factory() as session:
            row = (await session.execute(stmt)).one()
        return UsageTotals(
            calls=int(row[0]),
            prompt_tokens=int(row[1]),
            completion_tokens=int(row[2]),
            cost_cny=float(row[3]),
        )

    async def run_tokens(self, run_id: str) -> int:
        """单 run 已消耗 token 总数（预算检查用）。"""
        return (await self.totals(run_id=run_id)).total_tokens

    async def day_cost(self, now: datetime | None = None) -> float:
        """UTC 当日已消耗成本（元，预算检查用）。"""
        return (await self.totals(since=_day_start(now))).cost_cny

    async def stats(
        self,
        scope: Literal["run", "project", "global"] = "global",
        *,
        run_id: str | None = None,
        project_id: str | None = None,
    ) -> UsageTotals:
        """按 scope 汇总（供 ``/usage`` 端点）。"""
        if scope == "run":
            return await self.totals(run_id=run_id)
        if scope == "project":
            return await self.totals(project_id=project_id)
        return await self.totals()
