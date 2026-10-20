"""编排引擎：SOP 状态机解释执行（架构设计 §3.4）。

职责：
- 驱动 run 沿 SOP 定义推进（阶段执行 / 状态迁移 / 每步 checkpoint 落库）；
- 事件与消息发出（经 EventPublisher / MessagePublisher）；
- 任务管理：launch / pause / resume / cancel / retry-stage / 启动恢复 / shutdown。

并发模型（MVP）：单 run 串行 —— 每个 run 一个 asyncio 任务，任务生命周期与
run 绑定：从 launch 到终态只有一个任务；``paused`` / ``waiting_approval``
时任务挂起等待唤醒（Event，不轮询、不自杀重启）。v1.x 多 run 并行只需在
``_spawn`` 放开限流（接口已按 task-per-run 设计）。

断点恢复（recovery 调用 ``resume_interrupted``）：引擎按「当前阶段最新一条
执行记录」决定幂等动作 —— 已 completed 则补推进（进程被杀在推进间隙），
否则 attempt+1 重跑（现场保留为历史记录）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.contracts import ApprovalContext
from ymir.common.errors import (
    BizCode,
    BudgetExceededError,
    InvalidParamError,
    NotFoundError,
    StateConflictError,
)
from ymir.events.bus import EventPublisher
from ymir.events.log import EventType
from ymir.messages.bus import MessagePublisher
from ymir.messages.schema import MessageCreate
from ymir.models.base import utcnow
from ymir.models.entities import PipelineRun, StageExecution
from ymir.models.enums import RunStatus, StageStatus
from ymir.orchestrator.executor import StageExecutor, StageOutcome
from ymir.orchestrator.repair import (
    STOP_ACCEPTED,
    EvaluationGate,
    RepairController,
    repair_state,
)
from ymir.orchestrator.transitions import ensure_run_transition
from ymir.sop.loader import load_pipeline
from ymir.sop.models import END_TOKEN, MAIN_EVENT, PipelineDefinition, StageSpec
from ymir.storage.repositories import RunRepository, StageExecutionRepository

logger = logging.getLogger(__name__)

#: T04 审批挂点：返回 True 时该阶段完成后进入 waiting_approval（T02 为 None，不阻断）
#: v1.1：第三参 ``ctx`` 区分「阶段完成 / 终局候选」并携带回边状态（缺省 None，
#: 既有两参 hook 仍可用 —— 引擎按签名自适应调用，见 ``_hook_passes_context``）。
ApprovalHook = Callable[..., Awaitable[bool]]


def _biz_code_of(exc: BaseException) -> int | None:
    """提取业务异常的业务码（``YmirError.biz_code``）；非业务异常返回 None（PM-10）。"""
    code = getattr(exc, "biz_code", None)
    return int(code) if code is not None else None


def _repair_event(definition: PipelineDefinition) -> str:
    """回边事件名（``definition.repair.on_failure``；无 repair 块时退回主链事件）。"""
    repair = definition.repair
    return repair.on_failure if repair is not None else MAIN_EVENT


def _rounds_text(total_score: float, thresholds: dict[str, float]) -> str:
    """轮次耗尽的人读原因（§4.5.2 文案口径）。"""
    limit = thresholds.get("total", 75.0)
    return f"轮次耗尽，仍未通过（total={total_score:.1f}<{limit:g}）"


def _ensure_failed_for_manual(run: PipelineRun, action: str) -> None:
    """``accept-best`` / ``extend-repair`` 只允许作用于 ``failed`` 的 run（否则 1003）。

    注意：不能只依赖 ``ensure_run_transition`` —— 迁移表里 ``running -> completed``
    是合法边（正常完成），若不做前置状态判断，``accept-best`` 会误放行运行中的 run。
    """
    if run.status != RunStatus.FAILED.value:
        raise StateConflictError(
            f"动作 {action} 仅可作用于 failed 的 run（当前状态 {run.status}）"
        )


def _history_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """``context.repair.history`` 中的合法条目（脏数据一律丢弃）。"""
    history = state.get("history")
    if not isinstance(history, list):
        return []
    return [entry for entry in history if isinstance(entry, dict)]


def _best_history_entry(state: dict[str, Any]) -> dict[str, Any]:
    """最优未达标轮次：``total`` 最高，并列取 ``round`` 最大（§4.5.3）。"""
    entries = _history_entries(state)
    if not entries:
        raise InvalidParamError("无可接受的历史轮次（context.repair.history 为空）")
    return max(
        entries,
        key=lambda entry: (_as_float(entry.get("total")), _as_int(entry.get("round"))),
    )


def _as_float(value: Any) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _hook_passes_context(hook: Any) -> bool:
    """审批挂点是否接受第三个 ``ctx`` 参数（v1.0 两参 hook 兼容）。

    签名无法内省时按「接受 ctx」处理（我们自己的 :func:`build_approval_hook`
    就是三参；无法内省通常意味着可调用对象包装，宁可多传）。
    """
    if hook is None:
        return False
    try:
        params = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        return True
    positional = [
        param
        for param in params.values()
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        param.kind is inspect.Parameter.VAR_POSITIONAL for param in params.values()
    )
    return has_varargs or len(positional) >= 3


def _repair_round(context: dict[str, Any] | None) -> int:
    """当前修复轮次（0 = 首轮）；``context["repair"]["round"]`` 缺省 0。"""
    return int(repair_state(context).get("round", 0) or 0)


class OrchestrationEngine:
    """自研轻量状态机引擎（约千行级内核的核心部分）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventPublisher,
        messages: MessagePublisher,
        executor: StageExecutor | None = None,
        approval_hook: ApprovalHook | None = None,
        definition_loader: Callable[[str], PipelineDefinition] | None = None,
        eval_gate: EvaluationGate | None = None,
        repair: RepairController | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._events = events
        self._messages = messages
        self._executor = executor or StageExecutor()
        self._approval_hook = approval_hook
        # 既有两参 hook（tests/test_orchestrator.py）仍可用：按签名决定是否传 ctx
        self._hook_takes_ctx = _hook_passes_context(approval_hook)
        self._definition_loader = definition_loader or load_pipeline
        # v1.1 回边（docs/07 §4.3.6）：两者同时注入才进入裁决分叉；
        # 任一为 None（含单角色 / 截断流水线）时行为与 v1.0 完全一致。
        self._eval_gate = eval_gate
        self._repair = repair
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ 任务管理

    async def launch(self, run_id: str) -> PipelineRun:
        """pending -> running 并启动推进任务（POST /runs 的收尾动作）。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            ensure_run_transition(run.status, RunStatus.RUNNING)
            run.status = RunStatus.RUNNING.value
            run.started_at = utcnow()
            run.error = None
            await session.commit()
        await self._events.emit(
            run_id, EventType.RUN_STARTED, {"status": "running", "mode": run.mode}
        )
        self._spawn(run_id)
        return run

    async def request_pause(self, run_id: str) -> PipelineRun:
        """人工暂停：running -> paused（引擎在下一 checkpoint 停止推进）。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            ensure_run_transition(run.status, RunStatus.PAUSED)
            run.status = RunStatus.PAUSED.value
            await session.commit()
        await self._events.emit(
            run_id, EventType.RUN_PAUSED, {"status": "paused", "reason": "manual"}
        )
        return run

    async def request_resume(self, run_id: str) -> PipelineRun:
        """恢复：paused -> running，唤醒挂起中的推进任务。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            ensure_run_transition(run.status, RunStatus.RUNNING)
            run.status = RunStatus.RUNNING.value
            await session.commit()
        await self._events.emit(
            run_id, EventType.RUN_STARTED, {"status": "running", "resumed": True, "mode": run.mode}
        )
        self._wake(run_id)
        return run

    async def request_cancel(self, run_id: str) -> PipelineRun:
        """取消：任意非终态 -> failed（error=cancelled by user；现场保留可 retry-stage）。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            ensure_run_transition(run.status, RunStatus.FAILED)
            run.status = RunStatus.FAILED.value
            run.error = "cancelled by user"
            run.finished_at = utcnow()
            await session.commit()
        await self._events.emit(
            run_id, EventType.RUN_FAILED, {"status": "failed", "error": "cancelled by user"}
        )
        self._wake(run_id)  # 挂起中的任务需醒来看到终态并退出
        return run

    async def request_retry_stage(self, run_id: str) -> PipelineRun:
        """失败重试：failed -> running，从当前阶段 attempt+1 重跑。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            ensure_run_transition(run.status, RunStatus.RUNNING)
            run.status = RunStatus.RUNNING.value
            run.error = None
            run.finished_at = None
            await session.commit()
        await self._events.emit(
            run_id, EventType.RUN_STARTED, {"status": "running", "retry": True, "mode": run.mode}
        )
        self._wake(run_id)
        return run

    # ------------------------------------------------------------------ 人工介入（v1.1）

    async def request_accept_best(
        self, run_id: str, *, accepted_by: str = "user"
    ) -> PipelineRun:
        """接受「最优未达标版本」：failed -> completed（B-07，docs/07 §4.5.3）。

        前置：``status=failed`` 且 ``context.repair.history`` 非空；取 ``history`` 中
        ``total`` 最高项（并列取 ``round`` 最大）的 ``artifact_version`` 作为接受版本。
        写入 ``context.repair.accepted`` 并落 ``repair.accepted`` + ``run.completed
        {accepted_unqualified: true}`` 两个事件（机器可读原因不新增错误码）。
        """
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            _ensure_failed_for_manual(run, "accept-best")
            best = _best_history_entry(repair_state(run.context))
            version = best.get("artifact_version")
            if version is None:
                raise InvalidParamError("最优轮次缺少 artifact_version，无法接受")
            total = best.get("total")
            runnability = best.get("runnability")
            accepted = {
                "version": version,
                "total": total,
                "runnability": runnability,
                "accepted_by": accepted_by,
                "at": utcnow().isoformat(),
            }
            context = dict(run.context or {})
            repair = dict(context.get("repair") or {})
            repair["accepted"] = accepted
            repair["stop_reason"] = STOP_ACCEPTED
            context["repair"] = repair
            run.context = context
            ensure_run_transition(run.status, RunStatus.COMPLETED)
            run.status = RunStatus.COMPLETED.value
            run.error = f"人工接受未达标版本 v{version}（total={total}）"
            run.error_code = None
            run.finished_at = utcnow()
            await session.commit()
        await self._events.emit(
            run_id,
            EventType.REPAIR_ACCEPTED,
            {
                "accepted_version": version,
                "total": total,
                "runnability": runnability,
                "accepted_by": accepted_by,
            },
        )
        await self._events.emit(
            run_id,
            EventType.RUN_COMPLETED,
            {"status": "completed", "accepted_unqualified": True},
        )
        self._wake(run_id)  # 挂起中的任务需醒来看到终态并退出
        return run

    async def request_extend_repair(self, run_id: str) -> PipelineRun:
        """追加 1 轮修复：failed -> running（B-07，docs/07 §4.5.3）。

        前置：``status=failed`` 且 ``history`` 非空；先做**预算预检**（fail-closed，
        不通过抛 ``BudgetExceededError`` 且 run 保持 failed）。通过后 ``extra_rounds += 1``、
        ``current_stage_id = repair.stage``（否则恢复会从 evaluation 重跑而不是 coding）、
        作废 coding 与下游的 completed 执行，最后 ``running`` 并唤醒推进任务。
        """
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            _ensure_failed_for_manual(run, "extend-repair")
            state = repair_state(run.context)
            if not _history_entries(state):
                raise InvalidParamError("无可追加的修复轮次（context.repair.history 为空）")
            definition = self._definition_loader(run.pipeline_id)
            spec = definition.repair
            if spec is None or not definition.repair_active():
                raise InvalidParamError("该流水线未启用回边（repair），无法追加修复轮次")

        if self._repair is None:
            raise InvalidParamError("回边控制器未装配，无法追加修复轮次")
        if not await self._repair.check_budget(run_id):
            raise BudgetExceededError(
                "追加修复被拒绝：预算预检未通过（已用达上限或预检故障）"
            )

        await self._repair.rewind_to_repair(run_id, definition, reason="manual_extend")

        round_no = int(state.get("round", 0))
        extra_rounds = max(0, int(state.get("extra_rounds", 0))) + 1
        limit = spec.max_rounds + extra_rounds
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            ensure_run_transition(run.status, RunStatus.RUNNING)
            context = dict(run.context or {})
            repair = dict(context.get("repair") or {})
            repair["extra_rounds"] = extra_rounds
            repair["max_rounds"] = limit
            repair["stage"] = spec.stage
            repair["stop_reason"] = None
            context["repair"] = repair
            run.context = context
            run.status = RunStatus.RUNNING.value
            run.error = None
            run.error_code = None
            run.finished_at = None
            run.current_stage_id = spec.stage
            await session.commit()
        await self._events.emit(
            run_id,
            EventType.REPAIR_STARTED,
            {
                "repair_round": round_no,
                "max_rounds": limit,
                "target_stage": spec.stage,
                "from_stage": definition.eval_stage_id(),
                "artifact_version": None,
                "manual": True,
                "directive_digest": "",
            },
        )
        self._wake(run_id)
        return run

    async def resume_interrupted(self, run_id: str) -> None:
        """启动恢复（recovery 调用）：run 已是 running，重新拉起推进任务。"""
        run = await self._load_run(run_id)
        await self._events.emit(
            run_id,
            EventType.RUN_STARTED,
            {
                "status": "running",
                "resumed": True,
                "interrupted": True,
                "mode": run.mode if run is not None else None,
            },
        )
        self._wake(run_id)

    async def shutdown(self) -> None:
        """取消全部推进任务（进程退出 / 测试清理）。"""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 退出清理不允许再抛出
                logger.exception("推进任务退出时异常")
        self._tasks.clear()
        self._wakeups.clear()

    def _spawn(self, run_id: str) -> None:
        """创建推进任务（已存在活任务则幂等忽略）。"""
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        self._tasks[run_id] = asyncio.create_task(
            self._run_loop(run_id), name=f"ymir-run-{run_id}"
        )

    def _wake(self, run_id: str) -> None:
        """唤醒挂起中的任务；无活任务时拉起新任务（进程重启恢复场景）。

        Event 为 sticky：即使唤醒先于挂起到达也不会丢失。
        """
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            self._wakeups.setdefault(run_id, asyncio.Event()).set()
        else:
            self._spawn(run_id)

    # ------------------------------------------------------------------ 推进循环

    async def _run_loop(self, run_id: str) -> None:
        """推进循环：每轮「重读状态 -> （挂起等待 | 执行一个阶段 -> 推进）」。"""
        try:
            definition: PipelineDefinition | None = None
            while True:
                run = await self._load_run(run_id)
                if run is None:
                    return
                if run.status in (RunStatus.COMPLETED.value, RunStatus.FAILED.value):
                    return
                if run.status in (RunStatus.PAUSED.value, RunStatus.WAITING_APPROVAL.value):
                    await self._wait_for_wakeup(run_id)
                    continue
                if run.status != RunStatus.RUNNING.value:
                    return  # pending / 未知状态：不在本任务职责内
                if definition is None or definition.id != run.pipeline_id:
                    definition = self._definition_loader(run.pipeline_id)

                stage_id = run.current_stage_id or definition.initial_stage().id
                stage = definition.stage(stage_id)
                if stage is None:
                    await self._fail_run(run_id, f"当前阶段在定义中不存在: {stage_id}")
                    return

                advanced = await self._process_stage(run, definition, stage)
                if advanced == "halted":
                    continue  # 状态被外部改动：回顶部重读（可能挂起 / 退出）
                if advanced in ("finished", "failed"):
                    return
                # "moved" -> 继续下一阶段
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 引擎级兜底：尽力标记失败
            logger.exception("run %s 推进异常", run_id)
            try:
                await self._fail_run(run_id, f"引擎异常: {exc}", error_code=_biz_code_of(exc))
            except Exception:  # noqa: BLE001
                logger.exception("run %s 标记失败时再次异常", run_id)
        finally:
            self._wakeups.pop(run_id, None)
            if asyncio.current_task() is self._tasks.get(run_id):
                self._tasks.pop(run_id, None)

    async def _wait_for_wakeup(self, run_id: str) -> None:
        """挂起直到被唤醒（resume / 审批决策 / 取消）。"""
        event = self._wakeups.setdefault(run_id, asyncio.Event())
        await event.wait()
        event.clear()

    async def _process_stage(
        self, run: PipelineRun, definition: PipelineDefinition, stage: StageSpec
    ) -> str:
        """处理一个阶段：恢复补推进 或 执行（attempt+1）后推进。

        返回：``moved`` | ``finished`` | ``halted`` | ``failed``。
        """
        run_id = run.id
        latest = await self._latest_stage_exec(run_id, stage.id)

        # 恢复场景：进程被杀在「阶段已完成、run 未推进」的间隙 -> 补推进
        # （终局审批放行后也走这里：evaluation 已完成，补推进到终点）
        if latest is not None and latest.status == StageStatus.COMPLETED.value:
            result = await self._advance(run_id, definition, stage)
            if result == "finished":
                await self._events.emit(
                    run_id, EventType.RUN_COMPLETED, {"status": "completed"}
                )
            return result

        attempt = (latest.attempt + 1) if latest is not None else 1
        payload_common = {"stage_id": stage.id, "role_id": stage.role, "attempt": attempt}

        await self._events.emit(run_id, EventType.STAGE_STARTED, payload_common)
        exec_id = await self._create_stage_exec(run_id, stage, attempt)

        try:
            outcome = await self._executor.execute(run=run, stage=stage, attempt=attempt)
        except BudgetExceededError as exc:
            await self._finish_stage_exec(exec_id, StageStatus.FAILED, error=str(exc))
            await self._events.emit(
                run_id, EventType.STAGE_FAILED, {**payload_common, "error": str(exc)}
            )
            await self._pause_for_budget(run_id, str(exc))
            return "halted"
        except Exception as exc:  # noqa: BLE001 - 阶段失败：保留现场并整卷失败
            await self._finish_stage_exec(exec_id, StageStatus.FAILED, error=str(exc))
            await self._events.emit(
                run_id, EventType.STAGE_FAILED, {**payload_common, "error": str(exc)}
            )
            await self._fail_run(
                run_id, f"阶段 {stage.id} 执行失败: {exc}", error_code=_biz_code_of(exc)
            )
            return "failed"

        await self._finish_stage_exec(
            exec_id, StageStatus.COMPLETED, output_refs=outcome.output_refs
        )
        await self._events.emit(
            run_id,
            EventType.STAGE_COMPLETED,
            {**payload_common, "output_refs": outcome.output_refs},
        )

        # 裁决分叉（v1.1，docs/07 §4.3.6）：在审批之前、消息之后决定推进事件
        event = MAIN_EVENT
        if (
            self._repair is not None
            and self._eval_gate is not None
            and self._repair.should_evaluate(definition, stage)
        ):
            verdict = await self._eval_gate.evaluate(run=run, stage=stage, attempt=attempt)
            await self._repair.record_verdict(run_id, verdict)
            decision = await self._repair.decide(run, definition, verdict, attempt)
            if decision == "repair":
                event = _repair_event(definition)
            elif decision == "exhausted":
                await self._fail_run(
                    run_id,
                    f"评测未达标：{_rounds_text(verdict.total_score, verdict.thresholds)}",
                    stop_reason="rounds_exhausted",
                )
                return "failed"
            elif decision == "budget_blocked":
                await self._fail_run(
                    run_id,
                    "修复未发起：预算预检未通过（已用达上限或预检故障）",
                    error_code=int(BizCode.BUDGET_EXCEEDED),
                    stop_reason="budget_exceeded",
                )
                return "failed"

        # 消息去向与推进边使用**同一个** event（否则前端消息流与真实路径错位）
        await self._publish_stage_message(run_id, definition, stage, outcome, on=event)

        # 审批挂点（T02 无 hook 直接推进；T04 接入三关与审批记录）
        # v1.1：传 ctx（阶段完成 / 回边状态），HITL 引擎据此对中间候选 defer
        stage_ctx = ApprovalContext(
            phase="stage_completed",
            repair_active=definition.repair_active(),
            repair_round=_repair_round(run.context),
        )
        if await self._invoke_hook(run, stage, stage_ctx):
            await self._enter_waiting_approval(run_id)
            return "halted"

        # 终局第三关（§4.5.1）：裁决 pass 且即将进入终点时，对最终候选补审 1 次。
        # exhausted / budget_blocked 已在上面提前 return，不会走到这里。
        if (
            event == MAIN_EVENT
            and definition.repair_active()
            and definition.next_stage(stage.id) == END_TOKEN
            and await self._final_gate(run, definition)
        ):
            await self._enter_waiting_approval(run_id)
            return "halted"

        result = await self._advance(run_id, definition, stage, on=event)
        if result == "finished":
            await self._events.emit(run_id, EventType.RUN_COMPLETED, {"status": "completed"})
        elif result == "invalid":
            await self._fail_run(run_id, f"阶段 {stage.id} 无后继路径（定义缺陷）")
            return "failed"
        return result

    async def _invoke_hook(
        self, run: PipelineRun, stage: StageSpec, ctx: ApprovalContext
    ) -> bool:
        """调用审批挂点（按签名自适应：v1.0 两参 hook / v1.1 三参 hook 都支持）。"""
        hook = self._approval_hook
        if hook is None:
            return False
        if self._hook_takes_ctx:
            return bool(await hook(run, stage, ctx))
        return bool(await hook(run, stage))

    async def _final_gate(
        self, run: PipelineRun, definition: PipelineDefinition
    ) -> bool:
        """终局第三关（§4.5.1）：只对**最终候选**补审 1 次（回边生效时）。

        扫描修复闭环内的阶段，仅对声明了人工关卡的阶段调挂点（``phase=final_candidate``）；
        任一返回 True 即阻断（引擎随后转 waiting_approval）。中间候选已在
        ``on_stage_completed`` 里 defer，因此这里一个 run 内最多产生 1 条卡片
        （``_request_approval`` 另有 pending 复用保护）。
        """
        if self._approval_hook is None:
            return False
        spec = definition.repair
        if spec is None:
            return False
        scope = [spec.stage, *definition.downstream_of(spec.stage)]
        final_ctx = ApprovalContext(
            phase="final_candidate",
            repair_active=True,
            repair_round=_repair_round(run.context),
            is_final=True,
        )
        for stage_id in scope:
            gate_stage = definition.stage(stage_id)
            if gate_stage is None or gate_stage.gate is None:
                continue  # 只对人工关卡补审
            if await self._invoke_hook(run, gate_stage, final_ctx):
                return True
        return False

    async def _advance(
        self,
        run_id: str,
        definition: PipelineDefinition,
        stage: StageSpec,
        *,
        on: str = MAIN_EVENT,
    ) -> str:
        """阶段完成后的状态推进（事务内校验状态未被外部改动）。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            if run.status != RunStatus.RUNNING.value:
                return "halted"  # 外部暂停 / 取消：不推进
            next_id = definition.next_stage(stage.id, on=on)
            if next_id is None:
                return "invalid"
            if next_id == END_TOKEN:
                run.status = RunStatus.COMPLETED.value
                run.finished_at = utcnow()
                await session.commit()
                return "finished"
            run.current_stage_id = next_id
            await session.commit()
            return "moved"

    # ------------------------------------------------------------------ 记录与事件

    async def _publish_stage_message(
        self,
        run_id: str,
        definition: PipelineDefinition,
        stage: StageSpec,
        outcome: StageOutcome,
        *,
        on: str = MAIN_EVENT,
    ) -> None:
        """阶段产物消息（to_role=下游阶段角色；终点阶段为广播）。"""
        next_id = definition.next_stage(stage.id, on=on)
        to_role: str | None = None
        if next_id not in (None, END_TOKEN):
            next_stage = definition.stage(next_id)
            to_role = next_stage.role if next_stage is not None else None
        await self._messages.publish(
            run_id,
            MessageCreate(
                type=outcome.message_type,
                from_role=stage.role,
                to_role=to_role,
                content_refs=outcome.output_refs,
                summary=outcome.summary,
                payload={**outcome.payload, "stage_id": stage.id},
            ),
        )

    async def _create_stage_exec(self, run_id: str, stage: StageSpec, attempt: int) -> str:
        async with self._session_factory() as session:
            row = StageExecution(
                run_id=run_id,
                stage_id=stage.id,
                role_id=stage.role,
                status=StageStatus.RUNNING.value,
                attempt=attempt,
                input_refs=list(stage.inputs),
                started_at=utcnow(),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def _finish_stage_exec(
        self,
        exec_id: str,
        status: StageStatus,
        *,
        error: str | None = None,
        output_refs: list[Any] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(StageExecution, exec_id)
            if row is None:
                return
            row.status = status.value
            row.finished_at = utcnow()
            if error is not None:
                row.error = error
            if output_refs is not None:
                row.output_refs = list(output_refs)
            await session.commit()

    async def _latest_stage_exec(self, run_id: str, stage_id: str) -> StageExecution | None:
        async with self._session_factory() as session:
            return await StageExecutionRepository(session).latest(run_id, stage_id)

    async def _fail_run(
        self,
        run_id: str,
        error: str,
        *,
        error_code: int | None = None,
        stop_reason: str | None = None,
    ) -> None:
        """标记 run 失败；``stop_reason`` 写入 ``context["repair"].stop_reason``。

        两种终态可区分（§4.5.2）：``rounds_exhausted``（质量未达标，error_code=None）
        与 ``budget_exceeded``（预算中断，error_code=1004）。写入走「读 -> 复制 ->
        整体赋值 -> commit」，禁止原地改 JSON 列。
        """
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            if run.status in (RunStatus.COMPLETED.value, RunStatus.FAILED.value):
                return  # 已是终态（如已被 cancel），不覆盖
            run.status = RunStatus.FAILED.value
            run.error = error
            run.error_code = error_code
            run.finished_at = utcnow()
            if stop_reason is not None:
                context = dict(run.context or {})
                repair = dict(context.get("repair") or {})
                repair["stop_reason"] = stop_reason
                context["repair"] = repair
                run.context = context
            await session.commit()
        await self._events.emit(
            run_id,
            EventType.RUN_FAILED,
            {
                "status": "failed",
                "error": error,
                "error_code": error_code,
                "stop_reason": stop_reason,
            },
        )

    async def _pause_for_budget(self, run_id: str, error: str) -> None:
        """预算超限：run 转 paused（等待人工提额/改路由后 resume）。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            if run.status == RunStatus.RUNNING.value:
                run.status = RunStatus.PAUSED.value
                run.error_code = int(BizCode.BUDGET_EXCEEDED)  # PM-10：预算码透传
                await session.commit()
        await self._events.emit(
            run_id, EventType.BUDGET_EXCEEDED, {"status": "paused", "error": error}
        )
        await self._events.emit(
            run_id, EventType.RUN_PAUSED, {"status": "paused", "reason": "budget_exceeded"}
        )

    async def _enter_waiting_approval(self, run_id: str) -> None:
        """进入等待审批（T04 接入：此时创建 Approval 记录并发 approval.requested）。"""
        async with self._session_factory() as session:
            run = await self._require_run(session, run_id)
            if run.status != RunStatus.RUNNING.value:
                return
            ensure_run_transition(run.status, RunStatus.WAITING_APPROVAL)
            run.status = RunStatus.WAITING_APPROVAL.value
            await session.commit()

    # ------------------------------------------------------------------ 读取

    async def _load_run(self, run_id: str) -> PipelineRun | None:
        async with self._session_factory() as session:
            return await RunRepository(session).get(run_id)

    async def _require_run(self, session: AsyncSession, run_id: str) -> PipelineRun:
        run = await RunRepository(session).get(run_id)
        if run is None:
            raise NotFoundError(f"运行不存在: {run_id}")
        return run
