"""预算守卫（架构设计 §3.9）。

调用前置检查两条红线：单 run token 上限 / 全局日成本上限。
超限抛 ``BudgetExceededError``（1004）——引擎捕获后转 ``paused`` 并推 SSE 告警；
该异常不参与 provider fallback（预算是全局决策，换 provider 不解决）。
"""

from __future__ import annotations

from ymir.common.errors import BudgetExceededError
from ymir.gateway.providers import BudgetSpec
from ymir.gateway.usage import UsageRecorder


class BudgetGuard:
    """预算前置检查。"""

    def __init__(self, recorder: UsageRecorder, spec: BudgetSpec) -> None:
        self._recorder = recorder
        self._spec = spec

    @property
    def spec(self) -> BudgetSpec:
        return self._spec

    async def check(
        self,
        *,
        estimated_tokens: int = 0,
        estimated_cost_cny: float = 0.0,
        run_id: str | None = None,
    ) -> None:
        """检查本次预估是否越过红线；超限抛 BudgetExceededError。

        规则：``已用量 + 本次预估 > 上限`` 即拦截（先拦截再说，宁可误停不可超支）。

        已知边界（ARCH-05 TOCTOU）：本检查为「读用量 -> 判定放行」，与
        后续 :meth:`~ymir.gateway.usage.UsageRecorder.record` 结算分属两段，
        并发调用下两请求可同时读到未超限并各自放行，造成轻微超支。v1.0 为
        单 run 串行（引擎 task-per-run），该窗口不触发，故本版以注释标注为
        已知边界，不引入「预留-结算」的额外复杂度；v1.x 多 run 并行时再评估。
        """
        if run_id is not None and self._spec.per_run_tokens > 0:
            used = await self._recorder.run_tokens(run_id)
            if used + estimated_tokens > self._spec.per_run_tokens:
                raise BudgetExceededError(
                    "单 run token 超限："
                    f"已用 {used} + 预估 {estimated_tokens} > 上限 {self._spec.per_run_tokens}"
                )
        if self._spec.per_day_cost_cny > 0:
            used_cost = await self._recorder.day_cost()
            if used_cost + estimated_cost_cny > self._spec.per_day_cost_cny:
                raise BudgetExceededError(
                    "日成本超限："
                    f"已用 ¥{used_cost:.4f} + 预估 ¥{estimated_cost_cny:.4f}"
                    f" > 上限 ¥{self._spec.per_day_cost_cny:.2f}"
                )
