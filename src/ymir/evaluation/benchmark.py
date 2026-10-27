"""基准批量执行器（v1.1 T2.1，docs/07 §4.2.1–§4.2.6）。

**不新造编排能力**：每个任务复用一个真实 run（`software-dev` 流水线 + `mode=batch`），
由 :class:`~ymir.orchestrator.engine.OrchestrationEngine` 推进；本模块只负责
「建项目 → 建 run → launch → 等终态 → 打分 → 落状态文件」这一圈调度。

三条硬约束（写进实现，不只写注释）：

1. **状态文件原子写**：`tmp` + ``os.replace`` —— 进程被杀也不产生半截 JSON，
   ``--resume`` 永远能解析；
2. **成本按实测口径**：``SUM(model_calls.cost_cny) WHERE run_id = ?``，judge 成本
   通过 ``LLMJudge.with_run(run_id)`` 归属到同一 run（C11），``cost_source="measured"``；
3. **评分来源可区分**：主路径读 ``run.context["repair"].history``（``score_source="pipeline"``）；
   ``history`` 为空（`max_rounds=0` / 裁决降级 / v1.0 线性）时**必须**用同一套件、
   同一 judge 配置补一次完整评测（``score_source="fallback"``），不得只跑确定性检查凑分。

失败六分类（§4.2.6）在 :func:`classify_failure` 里按 first-match 实现；「未执行」
用 ``status=skipped + skip_reason``，**不填** ``failure_class``（两个维度不混）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.errors import InvalidParamError
from ymir.evaluation.baseline_probe import BASELINE_UNHEALTHY
from ymir.evaluation.evaluators.base import EvalTask
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.run_task import evaluate_run
from ymir.evaluation.suite import EvalSuite
from ymir.models.entities import ModelCall, PipelineRun
from ymir.models.enums import RunMode
from ymir.storage.repositories import ProjectRepository, RunRepository

logger = logging.getLogger(__name__)

#: 状态文件 schema 版本
STATE_SCHEMA = 1

#: 视为终态的 run 状态（paused 不是终态：需人工 resume，本版按未终态处理）
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed")

#: 未执行原因（与 ``TaskOut.skip_reason`` 口径一致）
SKIP_NOT_RUN = "not_run"
SKIP_BUDGET = "budget"

#: 失败分类（v1.2 六分类 + v1.3 两类，first-match 顺序见 :data:`FAILURE_ORDER`）
FAILURE_MISSING_ARTIFACT = "missing_artifact"
FAILURE_DEPENDENCY_MISSING = "dependency_missing"
FAILURE_TIMEOUT = "timeout"
FAILURE_CHECK_FAILED = "check_failed"
FAILURE_BELOW_THRESHOLD = "below_threshold"
FAILURE_DEGRADED_SCORING = "degraded_scoring"
#: v1.3（T1.2）新增：基线自身就失败 ⇒ 分数不可比（"这行不可比"是最重要信息，排最前）。
FAILURE_BASELINE_UNHEALTHY = "baseline_unhealthy"
#: v1.3（T1.2）新增：基线健康但合并树出现失败 = 增量改坏了既有功能。
FAILURE_REGRESSION = "regression"

FAILURE_ORDER: tuple[str, ...] = (
    FAILURE_BASELINE_UNHEALTHY,
    FAILURE_MISSING_ARTIFACT,
    FAILURE_DEPENDENCY_MISSING,
    FAILURE_TIMEOUT,
    FAILURE_REGRESSION,
    FAILURE_CHECK_FAILED,
    FAILURE_BELOW_THRESHOLD,
    FAILURE_DEGRADED_SCORING,
)

#: 依赖缺失的**运行期**判据（只认 ImportError 类信号，不是静态白名单扫描）
DEPENDENCY_ERROR_PATTERNS: tuple[str, ...] = (
    "ModuleNotFoundError",
    "No module named",
    "Could not find a version",
    "No matching distribution",
)

#: 缺上游产物的 run.error 特征（`InvalidParamError`「缺少上游产物」）
MISSING_UPSTREAM_PATTERN = "缺少上游产物"

#: 默认双门槛（套件未声明时兜底）
DEFAULT_THRESHOLDS: dict[str, float] = {"total": 75.0, "runnability": 60.0}

#: ``failure_reason`` 长度上限（§4.2.6：≤120 字）
REASON_MAX_CHARS = 120


@dataclass
class BenchmarkOptions:
    """一次批量执行的可选参数（`--limit / --resume / --budget-cny / --no-judge`）。"""

    suite_id: str = "benchmark-12"
    batch_id: str = ""
    limit: int | None = None
    resume: bool = False
    budget_cny: float | None = None
    no_judge: bool = False
    state_path: Path | None = None
    report_only: bool = False
    timeout_s: float = 1800.0


@dataclass
class FailureSignals:
    """失败分类的输入快照（主路径从 history / 兜底路径从 ``TaskOut`` 归一而来）。"""

    has_code_artifact: bool = True
    run_error: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    total: float | None = None
    runnability: float | None = None
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    degraded: bool = False
    sandbox: str = "docker"
    run_timed_out: bool = False
    #: 基线健康度（v1.3 T1.2）：``""`` | ``healthy`` | ``unhealthy`` | ``skipped``。
    baseline_status: str = ""
    #: 「基线健康但合并树有失败」（v1.3 T1.2）。
    regression: bool = False
    #: 阻断项失败的命令清单（v1.3 Q4）：非空 ⇒ 分类为 ``check_failed`` 且原因带前缀。
    blocking_failed: list[str] = field(default_factory=list)


def classify_failure(signals: FailureSignals) -> tuple[str | None, str]:
    """按 first-match 顺序给出 ``(failure_class, failure_reason)``（v1.3 八分类）。

    顺序（docs/11 §7.3-8）：

    1. ★ 基线不健康 → ``baseline_unhealthy``（"这行不可比"最重要）
    2. 缺文件 → ``missing_artifact``
    3. 依赖缺失 → ``dependency_missing``
    4. 超时 → ``timeout``
    5. ★ 回归 → ``regression``（比 ``check_failed`` 更精确的诊断优先）
    6. 检查失败 → ``check_failed``（含 blocking 失败项，原因带「阻断项」前缀）
    7. 未达双门槛 → ``below_threshold``
    8. 评分降级 → ``degraded_scoring``

    两个新分类**只可能由 v1.3 新数据触发**（``baseline_status`` / ``regression``
    在旧状态文件里缺失 ⇒ 取默认值）⇒ 旧状态文件重渲染结果不变。
    """
    reason = _first_line(signals.run_error)

    # 1) 基线不健康（T1.2）：分数不可比，先于一切"质量结论"
    # ★ 口径区分：``baseline_status`` 是**健康度三态**（healthy/unhealthy/skipped），
    # ``FAILURE_BASELINE_UNHEALTHY`` 是**失败分类值**（baseline_unhealthy）。
    # 两者字面量不同，混用会让分支永不命中（v1.3 全局审查发现的坑）。
    if signals.baseline_status == BASELINE_UNHEALTHY:
        return (
            FAILURE_BASELINE_UNHEALTHY,
            "基线自身检查未通过（基线不健康），本次分数与健康基线不可比",
        )

    # 2) 缺文件
    if not signals.has_code_artifact or MISSING_UPSTREAM_PATTERN in signals.run_error:
        return FAILURE_MISSING_ARTIFACT, reason or "项目内无 code 产物"

    # 3) 依赖缺失（运行期 ImportError 类信号）
    for check in signals.checks:
        output = str(check.get("output") or "")
        if any(pattern in output for pattern in DEPENDENCY_ERROR_PATTERNS):
            return FAILURE_DEPENDENCY_MISSING, _first_line(output) or "产物依赖镜像外包"

    # 4) 超时
    if signals.run_timed_out or any(bool(check.get("timed_out")) for check in signals.checks):
        return FAILURE_TIMEOUT, reason or "检查执行超时"

    # 5) 回归（T1.2）：基线通过、合并树失败 ⇒ 增量改坏了既有功能（R7 固定格式）
    if signals.regression:
        first_failed = next(
            (
                str(check.get("command") or "")
                for check in signals.checks
                if check.get("exit_code") not in (None, 0)
            ),
            "",
        )
        detail = f"基线通过，合并树在 `{first_failed}` 失败" if first_failed else (
            "基线通过，合并树出现失败"
        )
        return FAILURE_REGRESSION, detail

    # 6) 检查执行失败（含 pytest 退出码 5：no tests collected）
    for check in signals.checks:
        exit_code = check.get("exit_code")
        if exit_code is not None and int(exit_code) != 0:
            output = str(check.get("output") or "")
            if signals.blocking_failed:
                return (
                    FAILURE_CHECK_FAILED,
                    f"阻断项失败（{'、'.join(signals.blocking_failed)}）；"
                    f"{_first_line(output) or f'exit={exit_code}'}",
                )
            return FAILURE_CHECK_FAILED, _first_line(output) or f"exit={exit_code}"

    # 7) 未达双门槛
    thresholds = {**DEFAULT_THRESHOLDS, **(signals.thresholds or {})}
    if signals.total is not None and signals.runnability is not None:
        total_limit = float(thresholds["total"])
        runnability_limit = float(thresholds["runnability"])
        if signals.total < total_limit or signals.runnability < runnability_limit:
            return (
                FAILURE_BELOW_THRESHOLD,
                f"total {signals.total:.1f} / runnability {signals.runnability:.1f}"
                f" < {total_limit:.0f}/{runnability_limit:.0f}",
            )

    # 8) 评分降级
    if signals.degraded or signals.sandbox == "local":
        return FAILURE_DEGRADED_SCORING, "评分降级（维度 degraded 或沙箱回退 local）"

    return None, ""


def _first_line(text: str) -> str:
    """取首行并截断（``failure_reason`` ≤120 字）。"""
    first = (text or "").strip().splitlines()
    value = first[0].strip() if first else ""
    return value[:REASON_MAX_CHARS]


@dataclass
class BenchmarkState:
    """断点续跑状态（§4.2.5）：字段与状态文件 JSON 一一对应。"""

    schema: int = STATE_SCHEMA
    batch_id: str = ""
    suite_id: str = ""
    suite_version: str = ""
    pipeline_id: str = "software-dev"
    pipeline_version: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    meta: dict[str, Any] = field(default_factory=dict)
    budget_cny: float | None = None
    spent_cny: float = 0.0
    limit: int | None = None
    partial: bool = False
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------ 读写

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BenchmarkState:
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in raw.items() if key in known})

    @classmethod
    def load(cls, path: Path) -> BenchmarkState:
        """读取状态文件（不存在时返回空状态；坏文件抛 ``InvalidParamError``）。"""
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InvalidParamError(f"状态文件不可解析（{path}）：{exc}") from exc
        if not isinstance(raw, dict):
            raise InvalidParamError(f"状态文件结构非法（{path}）")
        return cls.from_dict(raw)

    def save(self, path: Path) -> None:
        """原子写：``tmp`` + ``os.replace``（中断不产生半截 JSON）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)


class BenchmarkRunner:
    """逐任务调度器：建项目 → 建 run → launch → 等终态 → 打分 → 落状态。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        orchestrator: Any,
        harness: EvaluationHarness,
        suite_id: str = "benchmark-12",
        batch_id: str = "",
        limit: int | None = None,
        budget_cny: float | None = None,
        sandbox: str = "docker",
        data_dir: Path | None = None,
        meta: dict[str, Any] | None = None,
        timeout_s: float = 1800.0,
    ) -> None:
        self._session_factory = session_factory
        self._orchestrator = orchestrator
        self._harness = harness
        self._suite_id = suite_id
        self._batch_id = batch_id
        self._limit = limit
        self._budget_cny = budget_cny
        self._sandbox = sandbox
        self._data_dir = data_dir or Path("data") / "benchmark"
        self._meta = dict(meta or {})
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------ 入口

    async def run(self, options: BenchmarkOptions | None = None) -> BenchmarkState:
        """跑完本次批次（受 ``limit`` / ``budget_cny`` / ``resume`` 约束）。"""
        opts = options or BenchmarkOptions(
            suite_id=self._suite_id,
            batch_id=self._batch_id,
            limit=self._limit,
            budget_cny=self._budget_cny,
            timeout_s=self._timeout_s,
        )
        suite = EvalSuite.load(opts.suite_id)
        if not suite.is_per_task:
            raise InvalidParamError(
                f"基准套件必须 binding_mode=per_task（{suite.id} 当前为 {suite.binding_mode}）"
            )
        state_path = opts.state_path or self._state_path(opts.suite_id, self._state_batch_id(opts))
        state = self._init_state(suite, opts, state_path)

        launched = 0
        for task in suite.tasks:
            existing = state.tasks.get(task.id)
            if opts.resume and existing is not None and existing.get("status") == "done":
                continue
            if opts.limit is not None and launched >= opts.limit:
                state.tasks[task.id] = _skipped_row(SKIP_NOT_RUN, "本次未发起（--limit）")
                continue
            if self._budget_hit(state, opts):
                state.tasks[task.id] = _skipped_row(
                    SKIP_BUDGET, "批次预算达上限（Q2 立即停止）"
                )
                continue
            state.tasks[task.id] = await self._run_one(task, state, suite, opts)
            launched += 1
            state.save(state_path)

        state.partial = opts.limit is not None and launched < len(suite.tasks)
        state.save(state_path)
        return state

    async def shutdown(self) -> None:
        """停止推进任务（脚本收尾用；runner 不持有任务表，委托编排引擎）。"""
        shutdown = getattr(self._orchestrator, "shutdown", None)
        if callable(shutdown):
            await shutdown()

    # ------------------------------------------------------------------ 单任务

    async def _run_one(
        self,
        task: EvalTask,
        state: BenchmarkState,
        suite: EvalSuite,
        options: BenchmarkOptions,
    ) -> dict[str, Any]:
        """建项目 + 建 run + launch + 等终态 + 打分，返回状态行。"""
        started = time.perf_counter()
        project = await self._create_project(task, suite)
        run = await self._create_run(task, project.id, suite, state)
        await self._orchestrator.launch(run.id)
        final_run = await self._wait_terminal(run.id, options.timeout_s)

        cost = await self._run_cost(run.id)
        state.spent_cny = round(state.spent_cny + cost, 6)

        row = await self._score(task, project.id, final_run, suite, options)
        row.update(
            {
                "project_id": project.id,
                "run_id": run.id,
                "cost_cny": round(cost, 6),
                "cost_source": "measured",
                "sandbox": self._sandbox,
                "wall_time_ms": int((time.perf_counter() - started) * 1000),
            }
        )
        logger.info(
            "benchmark 任务完成：task=%s status=%s score=%s source=%s cost=%.4f",
            task.id,
            row["status"],
            row.get("final_total"),
            row["score_source"],
            cost,
        )
        return row

    async def _create_project(self, task: EvalTask, suite: EvalSuite) -> Any:
        async with self._session_factory() as session:
            project = await ProjectRepository(session).create(
                name=f"bench-{self._batch_id}-{task.id}",
                description=f"{suite.id}@{suite.version}",
            )
            await session.commit()
            return project

    async def _create_run(
        self,
        task: EvalTask,
        project_id: str,
        suite: EvalSuite,
        state: BenchmarkState,
    ) -> PipelineRun:
        context = {
            "requirement": task.requirement,
            "benchmark": {
                "batch_id": self._batch_id,
                "task_id": task.id,
                "eval_suite": suite.id,
                "eval_task": task.id,
            },
        }
        async with self._session_factory() as session:
            run = await RunRepository(session).create(
                project_id=project_id,
                requirement=task.requirement,
                mode=RunMode.BATCH,
                context=context,
            )
            await session.commit()
            return run

    async def _wait_terminal(self, run_id: str, timeout_s: float) -> PipelineRun | None:
        """轮询到终态（``completed`` / ``failed``）；超时返回当时的 run（分类为 timeout）。"""
        deadline = time.monotonic() + max(1.0, timeout_s)
        while True:
            async with self._session_factory() as session:
                run = await RunRepository(session).get(run_id)
            if run is not None and run.status in TERMINAL_STATUSES:
                return run
            if time.monotonic() > deadline:
                logger.warning("benchmark 等待 run 终态超时：run=%s", run_id)
                return run
            await _sleep(0.05)

    async def _run_cost(self, run_id: str) -> float:
        """实测成本（元）：``SUM(model_calls.cost_cny)``（judge 成本已归属同一 run）。"""
        stmt = select(func.coalesce(func.sum(ModelCall.cost_cny), 0.0)).where(
            ModelCall.run_id == run_id
        )
        async with self._session_factory() as session:
            return float((await session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------ 打分

    async def _score(
        self,
        task: EvalTask,
        project_id: str,
        run: PipelineRun | None,
        suite: EvalSuite,
        options: BenchmarkOptions,
    ) -> dict[str, Any]:
        """按「主路径 history → 兜底完整评测」给出一行状态。"""
        history = _repair_history(run)
        if history:
            return self._row_from_history(history, run)
        return await self._row_from_fallback(task, project_id, run, suite, options)

    def _row_from_history(
        self, history: list[dict[str, Any]], run: PipelineRun | None
    ) -> dict[str, Any]:
        first, final = history[0], history[-1]
        thresholds = dict(DEFAULT_THRESHOLDS)
        # v1.3（T1.2 / Q4）：三个新键**只增不改** —— 旧 history 条目缺键时取默认值，
        # 重渲染结果与 v1.2 逐字节一致（docs/11 §7.5-7）。
        baseline_status = str(final.get("baseline_status") or "")
        regression = bool(final.get("regression"))
        blocking_failed = [
            str(item) for item in (final.get("blocking_failed") or []) if str(item)
        ]
        signals = FailureSignals(
            has_code_artifact=final.get("artifact_version") is not None,
            run_error=(run.error if run is not None else "") or "",
            checks=_checks_from_history(final),
            total=float(final.get("total") or 0.0),
            runnability=float(final.get("runnability") or 0.0),
            thresholds=thresholds,
            degraded=bool(final.get("degraded")),
            sandbox=self._sandbox,
            run_timed_out=_is_timeout_error(run.error if run is not None else ""),
            baseline_status=baseline_status,
            regression=regression,
            blocking_failed=blocking_failed,
        )
        failure_class, failure_reason = classify_failure(signals)
        passed = bool(final.get("verdict") == "pass")
        return {
            "status": "done",
            "first_total": _as_float(first.get("total")),
            "first_pass": bool(first.get("verdict") == "pass"),
            "final_total": _as_float(final.get("total")),
            "final_pass": passed,
            "repair_rounds": int(final.get("round") or 0),
            "score_source": "pipeline",
            "failure_class": None if passed else failure_class,
            "failure_reason": "" if passed else failure_reason,
            "artifact_version": final.get("artifact_version"),
            # ---- v1.3 可选键（旧读取方忽略即可）----
            "baseline_status": baseline_status or None,
            "regression": regression,
            "comparable": baseline_status != BASELINE_UNHEALTHY,
        }

    async def _row_from_fallback(
        self,
        task: EvalTask,
        project_id: str,
        run: PipelineRun | None,
        suite: EvalSuite,
        options: BenchmarkOptions,
    ) -> dict[str, Any]:
        """``history`` 为空：用同一套件 / 同一 judge 补一次**完整评测**（不得凑分）。

        v1.3（T4.1 / T4.2）：装配改走 :func:`ymir.evaluation.run_task.evaluate_run`
        —— 与 gate 裁决 / 单跑脚本**同一装配点**。v1.2 这里自己拼 ``EvalTask``，
        只绑套件 ``checks``、**不带**模板 ``layout`` 与 ``baseline_ref``，导致同一产物
        走批量与走裁决可能得到两套分数（真实口径裂缝）。
        """
        harness = self._harness.without_judge() if options.no_judge else self._harness
        if run is not None:
            outcome = await evaluate_run(
                harness,
                run,
                artifact_name=task.artifact_name or "code",
                default_suite=suite.id,
            )
        else:
            # run 已不存在（极端兜底）：无法装配上下文，退化为最小任务（不伪装成同源）
            logger.warning(
                "批量补评：run 缺失，退化为套件最小任务（task=%s suite=%s）",
                task.id,
                suite.id,
            )
            bound = EvalTask(
                id=task.id,
                requirement=task.requirement,
                scenario=task.scenario,
                project_id=project_id,
                artifact_name=task.artifact_name,
                checks=list(task.checks),
            )
            outcome = await harness.evaluate_task(
                bound, project_id=project_id, suite=suite
            )
        probe = outcome.baseline_runnability
        baseline_status = str(getattr(probe, "status", "") or "") if probe else ""
        signals = FailureSignals(
            has_code_artifact=outcome.artifact_ref.get("name") is not None,
            run_error=(run.error if run is not None else "") or "",
            checks=_checks_of(outcome),
            total=float(outcome.total_score),
            runnability=float(outcome.runnability),
            thresholds=dict(outcome.thresholds or DEFAULT_THRESHOLDS),
            degraded=any(dim.degraded for dim in outcome.dimensions),
            sandbox=self._sandbox,
            run_timed_out=_is_timeout_error(run.error if run is not None else ""),
            baseline_status=baseline_status,
            regression=bool(outcome.regression),
            blocking_failed=list(outcome.blocking_failed),
        )
        if outcome.status != "evaluated":
            return {
                "status": "skipped",
                "skip_reason": outcome.skip_reason or SKIP_NOT_RUN,
                "score_source": "fallback",
                "cost_cny": 0.0,
                "failure_class": None,
                "failure_reason": "评测未执行（产物缺失或未绑定）",
            }
        failure_class, failure_reason = classify_failure(signals)
        passed = bool(outcome.passed)
        return {
            "status": "done",
            "first_total": float(outcome.total_score),
            "first_pass": passed,
            "final_total": float(outcome.total_score),
            "final_pass": passed,
            "repair_rounds": 0,
            "score_source": "fallback",
            "failure_class": None if passed else failure_class,
            "failure_reason": "" if passed else failure_reason,
            "artifact_version": outcome.artifact_ref.get("version"),
            # ---- v1.3 可选键 ----
            "baseline_status": baseline_status or None,
            "regression": bool(outcome.regression),
            "comparable": baseline_status != BASELINE_UNHEALTHY,
        }

    # ------------------------------------------------------------------ 状态

    def _state_batch_id(self, options: BenchmarkOptions) -> str:
        return options.batch_id or self._batch_id or datetime.now(UTC).strftime("%Y%m%d-1")

    def _state_path(self, suite_id: str, batch_id: str) -> Path:
        return Path(self._data_dir) / f"state-{suite_id}-{batch_id}.json"

    def _init_state(
        self, suite: EvalSuite, options: BenchmarkOptions, state_path: Path
    ) -> BenchmarkState:
        """装载或新建状态（``--resume`` 时保留已有行）。"""
        state = BenchmarkState.load(state_path) if options.resume else BenchmarkState()
        state.schema = STATE_SCHEMA
        state.batch_id = self._state_batch_id(options)
        state.suite_id = suite.id
        state.suite_version = suite.version
        state.pipeline_id = self._meta.get("pipeline_id", state.pipeline_id)
        state.pipeline_version = self._meta.get("pipeline_version", state.pipeline_version)
        state.meta = {**state.meta, **self._meta}
        if options.budget_cny is not None:
            state.budget_cny = options.budget_cny
        state.limit = options.limit if options.limit is not None else state.limit
        if not options.resume or not state.started_at:
            state.started_at = datetime.now(UTC).isoformat()
        return state

    def _budget_hit(self, state: BenchmarkState, options: BenchmarkOptions) -> bool:
        """批次预算硬上限（Q2：达到即停止，剩余任务 ``skipped{budget}``、零模型调用）。"""
        budget = options.budget_cny if options.budget_cny is not None else state.budget_cny
        if budget is None:
            return False
        return state.spent_cny >= float(budget)


# --------------------------------------------------------------------- 工具


async def _sleep(seconds: float) -> None:
    """可被测试替换的睡眠（避免测试里真等）。"""
    import asyncio

    await asyncio.sleep(seconds)


def _skipped_row(skip_reason: str, reason: str) -> dict[str, Any]:
    """未执行行：``status=skipped`` + ``skip_reason``，**不填** ``failure_class``。"""
    return {
        "status": "skipped",
        "skip_reason": skip_reason,
        "cost_cny": 0.0,
        "failure_class": None,
        "failure_reason": reason[:REASON_MAX_CHARS],
    }


def _repair_history(run: PipelineRun | None) -> list[dict[str, Any]]:
    """从 ``run.context["repair"]["history"]`` 取裁决历史（脏数据安全）。"""
    context = run.context if run is not None and isinstance(run.context, dict) else {}
    repair = context.get("repair")
    if not isinstance(repair, dict):
        return []
    history = repair.get("history")
    if not isinstance(history, list):
        return []
    return [entry for entry in history if isinstance(entry, dict)]


def _checks_from_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """从 history 条目取失败检查明细（优先新字段，缺省回退旧形状）。

    v1.1：``failed_check_details`` 带 ``exit_code`` / ``timed_out`` / ``output``，
    六分类（A-06）才能在主路径上精确判定；旧状态文件只有 ``failed_checks``
    （命令名列表）时回退为空明细，由「产物缺失 / 双门槛 / degraded / run.error」
    兜底分类 —— 向后兼容，不因缺字段而失败。
    """
    details = entry.get("failed_check_details")
    if isinstance(details, list) and details:
        return [
            {
                "command": str(item.get("command") or ""),
                "exit_code": item.get("exit_code"),
                "timed_out": bool(item.get("timed_out")),
                "output": str(item.get("output") or ""),
            }
            for item in details
            if isinstance(item, dict)
        ]
    return [
        {"command": str(command), "exit_code": None, "timed_out": False, "output": ""}
        for command in (entry.get("failed_checks") or [])
    ]


def _checks_of(outcome: Any) -> list[dict[str, Any]]:
    """从 ``TaskOut`` 的可运行率证据里取检查结果（命令 / 退出码 / 超时 / 输出）。"""
    dimension = outcome.dimension("runnability")
    if dimension is None:
        return []
    checks: list[dict[str, Any]] = []
    for evidence in dimension.evidence:
        if evidence.kind != "command":
            continue
        detail = evidence.detail or ""
        exit_code, timed_out = _parse_exit(detail)
        checks.append(
            {
                "command": evidence.ref,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "output": detail.split("｜", 1)[1].strip() if "｜" in detail else detail,
            }
        )
    return checks


def _parse_exit(detail: str) -> tuple[int | None, bool]:
    marker = "exit="
    if marker not in detail:
        return None, False
    tail = detail.split(marker, 1)[1]
    token = tail.split()[0] if tail.split() else ""
    try:
        exit_code = int(token)
    except ValueError:
        exit_code = None
    return exit_code, "timed_out=True" in detail


def _is_timeout_error(error: str) -> bool:
    text = (error or "").lower()
    return "超时" in (error or "") or "timeout" in text


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
