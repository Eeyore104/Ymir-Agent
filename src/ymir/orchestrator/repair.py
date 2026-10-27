"""回边控制器与裁决闸门契约（docs/07 §4.3.7、§4.5.2；T3.2 核心）。

职责划分（唯一权威表述，docs/07 §4.4.1）：

- **协议在这里（orchestrator，rank 1）**：:class:`EvaluationGate` 只声明
  「给一次 run / stage / attempt，返回 :class:`~ymir.common.contracts.EvalVerdict`」；
- **实现在 evaluation（rank 1）**：:class:`~ymir.evaluation.gate.HarnessEvaluationGate`；
- **合同在 common（rank 5）**：``EvalVerdict`` / ``FailedCheck``。

:class:`RepairController` 承担「纯判定 + 副作用」：轮次判定（0 成本）→ 预算预检
→ 作废执行（回边一步到位）→ 写轮次 → 发事件 / 发定向修复消息。

两条纪律（踩到就会出事故）：

1. ``run.context`` 是 JSON 列，**必须**「读 -> ``dict(...)`` 复制 -> 整体赋值 -> commit」
   （:func:`patch_run_context`），原地改 SQLAlchemy 不追踪；
2. 回边必须**同时**作废 ``repair.stage`` 与其 ``downstream_of`` 的 completed 执行，
   否则重跑后引擎会走「补推进」跳过重评（C7）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.contracts import EvalVerdict
from ymir.common.errors import BizCode, NotFoundError
from ymir.events.bus import EventPublisher
from ymir.events.log import EventType
from ymir.messages.bus import MessagePublisher
from ymir.messages.schema import MessageCreate
from ymir.models.base import utcnow
from ymir.models.entities import PipelineRun
from ymir.models.enums import MessageType, StageStatus
from ymir.sop.models import PipelineDefinition, StageSpec
from ymir.storage.repositories import RunRepository, StageExecutionRepository

logger = logging.getLogger(__name__)

#: 判定结果（引擎据此选事件 / 终态）
Decision = Literal["pass", "skip", "repair", "exhausted", "budget_blocked"]

#: 跳过原因 / 终止原因（docs/07 §14 唯一口径）
STOP_ROUNDS_EXHAUSTED = "rounds_exhausted"
STOP_BUDGET_EXCEEDED = "budget_exceeded"
STOP_ACCEPTED = "accepted"

#: 自动修复指令原文（§4.4.4；定向修复只动与失败检查相关的实现）
REPAIR_DIRECTIVE = (
    "只修改与上述失败检查相关的实现；已通过的部分（ruff check）保持不动；"
    "不要重写整个项目；不要删除已有测试。"
)

#: ``failed_check_details[].output`` 的截断长度（只留可定位的摘要，控制 context 体积）
DETAIL_PREVIEW_CHARS = 500

#: 自动修复消息的发送角色（消息面板中的来源标识）
_AUTO_REPAIR_FROM = "evaluator"


class EvaluationGate(Protocol):
    """确定性裁决闸门（编排层只认契约，实现由 bootstrap 注入）。"""

    async def evaluate(
        self, *, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> EvalVerdict:
        """对一个阶段的候选产物给出裁决（``pass`` / ``fail`` / ``unknown``）。"""
        ...


@dataclass(frozen=True)
class BudgetSnapshot:
    """单 run token 用量快照（修复发起前的预检输入，§4.5.2）。

    ``exhausted`` 既可由调用方显式给定，也可由 ``used_tokens >= limit_tokens``
    推导（``limit_tokens=0`` 视为不限，恒不耗尽）。
    """

    used_tokens: int = 0
    limit_tokens: int = 0
    exhausted: bool = False

    def __post_init__(self) -> None:
        if not self.exhausted and self.limit_tokens > 0 and self.used_tokens >= self.limit_tokens:
            object.__setattr__(self, "exhausted", True)


#: 预算预检回调（按 run 取当前用量与上限）
BudgetProbe = Callable[[str], Awaitable[BudgetSnapshot]]


# --------------------------------------------------------------------- context 写入


async def patch_run_context(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """把 ``patch`` 浅合并进 ``run.context``（读 -> 复制 -> 整体赋值 -> commit）。

    返回合并后的 context。**禁止**任何地方原地修改 ``run.context``（SQLAlchemy
    不追踪 JSON 列的原地变更，表现为「轮次永远是 1」这类幽灵 bug）。
    """
    async with session_factory() as session:
        run = await RunRepository(session).get(run_id)
        if run is None:
            raise NotFoundError(f"运行不存在: {run_id}")
        context = dict(run.context or {})
        context.update(patch)
        run.context = context
        await session.commit()
        return context


def repair_state(context: dict[str, Any] | None) -> dict[str, Any]:
    """从 run.context 取 ``repair`` 轮次状态（缺失 / 非法时返回空字典副本）。"""
    repair = (context or {}).get("repair")
    return dict(repair) if isinstance(repair, dict) else {}


# --------------------------------------------------------------------- 控制器


class RepairController:
    """回边状态机：判定 + 副作用（作废执行 / 写轮次 / 发事件与消息）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventPublisher,
        messages: MessagePublisher,
        budget_probe: BudgetProbe | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._events = events
        self._messages = messages
        self._budget_probe = budget_probe

    # ------------------------------------------------------------------ 判定

    def should_evaluate(self, definition: PipelineDefinition, stage: StageSpec) -> bool:
        """该阶段完成后是否进入裁决（回边生效且是评估阶段）。"""
        if not definition.repair_active():
            return False
        return stage.id == definition.eval_stage_id()

    async def record_verdict(self, run_id: str, verdict: EvalVerdict) -> dict[str, Any]:
        """把本轮裁决追加进 ``context["repair"]["history"]``（首轮 round=0 必记）。"""
        async with self._session_factory() as session:
            run = await RunRepository(session).get(run_id)
            if run is None:
                raise NotFoundError(f"运行不存在: {run_id}")
            context = dict(run.context or {})
            repair = repair_state(context)
            history = list(repair.get("history") or [])
            entry = {
                "round": int(repair.get("round", 0)),
                "verdict": verdict.status,
                "total": round(float(verdict.total_score), 2),
                "runnability": round(float(verdict.runnability), 2),
                # 命令名列表（既有形状，多处消费，保持不动）
                "failed_checks": [check.command for check in verdict.failed_checks],
                # v1.1 追加：六分类（A-06）需要 exit_code/timed_out/stderr，命令名不够
                "failed_check_details": [
                    {
                        "command": check.command,
                        "exit_code": check.exit_code,
                        "timed_out": check.timed_out,
                        "output": (check.output or "")[:DETAIL_PREVIEW_CHARS],
                    }
                    for check in verdict.failed_checks
                ],
                "artifact_version": verdict.artifact_version,
                "cost_cny": round(float(verdict.cost_cny), 6),
                "wall_time_ms": int(verdict.wall_time_ms),
                "failure_class": verdict.failure_class,
                "degraded": verdict.degraded,
                # v1.3（T1.2 / Q4）：**只增不改** —— 既有键一个都不动，
                # 旧状态文件重放 / 重渲染结果不受影响（docs/11 §7.5-7）。
                "baseline_status": verdict.baseline_status,
                "regression": bool(verdict.regression),
                "blocking_failed": list(verdict.blocking_failed),
                "at": utcnow().isoformat(),
            }
            history.append(entry)
            repair["history"] = history
            repair.setdefault("round", 0)
            repair.setdefault("extra_rounds", 0)
            repair.setdefault("stop_reason", None)
            repair.setdefault("accepted", None)
            context["repair"] = repair
            run.context = context
            await session.commit()
            return repair

    async def decide(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        verdict: EvalVerdict,
        attempt: int,
    ) -> Decision:
        """纯判定 + 副作用：``pass`` / ``skip`` / ``repair`` / ``exhausted`` /
        ``budget_blocked``。"""
        spec = definition.repair
        if spec is None:  # 防御：should_evaluate 已排除，调用方误用时不回边
            return "skip"

        state = await self._load_repair_state(run.id, run.context)
        current_round = int(state.get("round", 0))
        extra_rounds = max(0, int(state.get("extra_rounds", 0)))
        limit = spec.max_rounds + extra_rounds

        if verdict.status == "unknown":
            # 降级：不回边、不阻断（退回 v1.0 线性行为），只披露
            logger.warning(
                "裁决降级（unknown），按 v1.0 线性推进：run=%s reason=%s",
                run.id,
                verdict.reason or verdict.failure_class or "",
            )
            return "skip"

        if verdict.status == "pass":
            await self._events.emit(
                run.id,
                EventType.EVALUATION_COMPLETED,
                {
                    "suite_id": verdict.suite_id,
                    "total_score": verdict.total_score,
                    "runnability": verdict.runnability,
                    "passed": True,
                    "verdict": "pass",
                    "repair_round": current_round,
                    "artifact_version": verdict.artifact_version,
                },
            )
            if current_round >= 1:
                # v1.1（§4.6.1）：修复后通过 —— 让时间线能收尾「第 N 轮修复成功」。
                # 首轮直接 pass（round=0）不 emit：那不是「修复完成」，只是正常通过。
                await self._events.emit(
                    run.id,
                    EventType.REPAIR_COMPLETED,
                    {
                        "repair_round": current_round,
                        "total": verdict.total_score,
                        "runnability": verdict.runnability,
                        "artifact_version": verdict.artifact_version,
                        "wall_time_ms": verdict.wall_time_ms,
                        "cost_cny": verdict.cost_cny,
                    },
                )
            return "pass"

        # --- 未达标：先判轮次，再做预算预检（顺序即成本：纯判定 0 成本优先）---
        if current_round >= limit:
            await self._events.emit(
                run.id,
                EventType.REPAIR_EXHAUSTED,
                {
                    "repair_round": current_round,
                    "max_rounds": limit,
                    "stop_reason": STOP_ROUNDS_EXHAUSTED,
                    "last_total": verdict.total_score,
                    "last_runnability": verdict.runnability,
                    "error_code": None,
                },
            )
            return "exhausted"

        if await self._budget_blocked(run.id):
            await self._events.emit(
                run.id,
                EventType.REPAIR_EXHAUSTED,
                {
                    "repair_round": current_round,
                    "max_rounds": limit,
                    "stop_reason": STOP_BUDGET_EXCEEDED,
                    "last_total": verdict.total_score,
                    "last_runnability": verdict.runnability,
                    "error_code": int(BizCode.BUDGET_EXCEEDED),
                },
            )
            return "budget_blocked"

        # --- 发起回边：作废执行（coding + evaluation）-> 写轮次 -> 事件 / 消息 ---
        await self.rewind_to_repair(run.id, definition, reason="auto_repair")
        next_round = current_round + 1
        await patch_run_context(
            self._session_factory,
            run.id,
            {
                "repair": {
                    **state,
                    "round": next_round,
                    "max_rounds": limit,
                    "stage": spec.stage,
                    "extra_rounds": extra_rounds,
                    "stop_reason": None,
                }
            },
        )
        await self._emit_evaluation_failed(run.id, verdict, current_round, limit)
        await self._emit_repair_started(run.id, definition, verdict, next_round, limit)
        await self._publish_repair_message(run.id, definition, verdict, next_round, limit)
        return "repair"

    async def check_budget(self, run_id: str) -> bool:
        """预算预检（``True`` = 可以继续发起修复；供 ``extend-repair`` 复用）。

        与 :meth:`decide` 内的预检同源（:meth:`_budget_blocked`），因此同样 **fail-closed**：
        探针故障视为不通过（宁可误停不可超支）。
        """
        return not await self._budget_blocked(run_id)

    async def rewind_to_repair(
        self,
        run_id: str,
        definition: PipelineDefinition,
        reason: str,
    ) -> list[str]:
        """作废 ``repair.stage`` 与其全部下游的 completed 执行（回边一步到位，C7）。

        返回被作废的阶段 ID 列表（便于事件 / 测试断言）。
        """
        spec = definition.repair
        if spec is None:
            return []
        targets = [spec.stage, *definition.downstream_of(spec.stage)]
        invalidated: list[str] = []
        async with self._session_factory() as session:
            repo = StageExecutionRepository(session)
            for stage_id in targets:
                latest = await repo.latest(run_id, stage_id)
                if latest is None or latest.status != StageStatus.COMPLETED.value:
                    continue
                latest.status = StageStatus.FAILED.value
                latest.error = f"回边重跑（{reason}）：等待按修复上下文重做"
                invalidated.append(stage_id)
            await session.commit()
        return invalidated

    # ------------------------------------------------------------------ 内部

    async def _load_repair_state(self, run_id: str, cached: Any) -> dict[str, Any]:
        """取最新 repair 轮次状态（优先读库，避免用调用方持有的陈旧快照）。"""
        async with self._session_factory() as session:
            run = await RunRepository(session).get(run_id)
        context = run.context if run is not None else cached
        return repair_state(context)

    async def _budget_blocked(self, run_id: str) -> bool:
        """修复发起前的预算预检：超限或预检故障返回 True（**不发起修复**）。

        预检故障按 **fail-closed** 处理（与 ``gateway/budget.py``「先拦截再说，
        宁可误停不可超支」的既有原则一致）：探针挂了等于失去守门，此时放行修复
        恰恰是最不该花钱的时刻。
        """
        if self._budget_probe is None:
            return False
        try:
            snapshot = await self._budget_probe(run_id)
        except Exception:  # noqa: BLE001 - 预检故障保守拦截（宁可误停不可超支）
            logger.error("预算预检失败，保守拦截修复：run=%s", run_id, exc_info=True)
            return True
        if snapshot.exhausted:
            logger.warning(
                "预算预检超限，不发起修复：run=%s used=%s limit=%s",
                run_id,
                snapshot.used_tokens,
                snapshot.limit_tokens,
            )
        return snapshot.exhausted

    async def _emit_evaluation_failed(
        self,
        run_id: str,
        verdict: EvalVerdict,
        repair_round: int,
        limit: int,
    ) -> None:
        """发 ``evaluation.failed``（触发本轮修复的那次裁决，§4.6.1 payload 口径）。"""
        await self._events.emit(
            run_id,
            EventType.EVALUATION_FAILED,
            {
                "total": verdict.total_score,
                "runnability": verdict.runnability,
                "thresholds": {
                    "total": (verdict.thresholds or {}).get("total", 75.0),
                    "runnability": (verdict.thresholds or {}).get("runnability", 60.0),
                },
                "failed_checks": [check.to_dict() for check in verdict.failed_checks],
                "failure_class": verdict.failure_class,
                "repair_round": repair_round,
                "max_rounds": limit,
                "artifact_version": verdict.artifact_version,
                "degraded": verdict.degraded,
            },
        )

    async def _emit_repair_started(
        self,
        run_id: str,
        definition: PipelineDefinition,
        verdict: EvalVerdict,
        repair_round: int,
        limit: int,
    ) -> None:
        spec = definition.repair
        await self._events.emit(
            run_id,
            EventType.REPAIR_STARTED,
            {
                "repair_round": repair_round,
                "max_rounds": limit,
                "target_stage": spec.stage if spec is not None else None,
                "from_stage": definition.eval_stage_id(),
                "artifact_version": verdict.artifact_version,
                "manual": False,
                "directive_digest": REPAIR_DIRECTIVE[:120],
                "failure_class": verdict.failure_class,
                "failed_check_count": len(verdict.failed_checks),
            },
        )

    async def _publish_repair_message(
        self,
        run_id: str,
        definition: PipelineDefinition,
        verdict: EvalVerdict,
        repair_round: int,
        limit: int,
    ) -> None:
        """发一条定向修复消息（``type=feedback``，``payload.source=auto_repair``）。"""
        spec = definition.repair
        target_stage = spec.stage if spec is not None else None
        target = definition.stage(target_stage) if target_stage else None
        max_checks = spec.context.max_failed_checks if spec is not None else 5
        failed = list(verdict.failed_checks)[:max_checks]
        thresholds = verdict.thresholds or {}
        summary = self._summary(verdict, repair_round, failed)
        await self._messages.publish(
            run_id,
            MessageCreate(
                type=MessageType.FEEDBACK,
                from_role=_AUTO_REPAIR_FROM,
                to_role=target.role if target is not None else "engineer",
                content_refs=[
                    {
                        "artifact_name": verdict.artifact_name,
                        "version": verdict.artifact_version,
                    }
                ],
                summary=summary,
                payload={
                    "source": "auto_repair",
                    "repair_round": repair_round,
                    "max_rounds": limit,
                    "verdict": {
                        "total": verdict.total_score,
                        "runnability": verdict.runnability,
                        "thresholds": {
                            "total": thresholds.get("total", 75.0),
                            "runnability": thresholds.get("runnability", 60.0),
                        },
                    },
                    "failed_checks": [check.to_dict() for check in failed],
                    "previous": {
                        "artifact_name": verdict.artifact_name,
                        "version": verdict.artifact_version,
                    },
                    "directive": REPAIR_DIRECTIVE,
                },
            ),
        )

    @staticmethod
    def _summary(verdict: EvalVerdict, repair_round: int, failed: list[Any]) -> str:
        """一条人读摘要（≤500 字，超长截断）。"""
        head = (
            f"[自动修复 第{repair_round}轮] 未达标 "
            f"total={verdict.total_score:.1f}(<{verdict.thresholds.get('total', 75):g}) "
            f"runnability={verdict.runnability:.1f}"
            f"(<{verdict.thresholds.get('runnability', 60):g})"
        )
        if failed:
            first = failed[0]
            head += (
                f"；失败检查 {len(failed)} 条：{first.command}"
                f" exit={first.exit_code}"
                + (" timed_out" if first.timed_out else "")
            )
        return head[:500]
