"""确定性裁决闸门实现（docs/07 §4.4.3；T3.2 核心）。

把 :class:`~ymir.evaluation.harness.EvaluationHarness` 的四维评测收敛成一份
不可变合同 :class:`~ymir.common.contracts.EvalVerdict`（``pass`` / ``fail`` /
``unknown``），供编排层回边判定消费 —— **QA 的 LLM 叙事报告不参与裁决**（§4.4.1）。

一个 ``evaluate`` 调用做五件事：

1. 解析套件 / 任务：``run.context["benchmark"]["eval_suite"/"eval_task"]`` >
   流水线 ``repair.eval.suite/task`` > ``default_suite``；
2. 构造 ``EvalTask``（``project_id=run.project_id``、``run_id=run.id``，
   保证「哪个 run 的哪一版产物」可追溯）；
3. ``harness.evaluate_task(...)`` 走四维 + 双门槛；
4. 报告落盘为 **``eval_verdict``**（不与 QA 的 ``evaluation_report`` 抢名），
   并写入 ``run_id / artifact_version / repair_round / verdict_source="harness"``；
5. 归约为 ``EvalVerdict``：``skipped`` / 降级 / 异常一律 ``unknown``
   （不回边、不阻断流水线，退回 v1.0 线性行为）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ymir.common.contracts import EvalVerdict, FailedCheck, LayoutRules
from ymir.common.errors import YmirError
from ymir.evaluation.aggregator import normalize_weights
from ymir.evaluation.evaluators.base import RUNNABILITY, EvalCheck, EvalTask
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.report import EvaluationReport
from ymir.evaluation.suite import EvalSuite
from ymir.models.entities import PipelineRun
from ymir.sop.loader import load_pipeline
from ymir.sop.models import PipelineDefinition, StageSpec
from ymir.templates import load_template
from ymir.templates.models import TemplateSpec

logger = logging.getLogger(__name__)

#: 确定性裁决报告的落盘名（与 QA 的 ``evaluation_report`` 并存，§4.4.3）
VERDICT_ARTIFACT_NAME = "eval_verdict"

#: 失败检查的证据格式：``exit=1 timed_out=False duration=12ms｜<输出片段>``
_EVIDENCE_RE = re.compile(r"exit=(-?\d+) timed_out=(True|False)")
_DETAIL_SEPARATOR = "｜"


class HarnessEvaluationGate:
    """``EvaluationGate`` 的 harness 实现（确定性、可追溯、降级不阻断）。"""

    def __init__(
        self,
        *,
        harness: EvaluationHarness,
        store: Any,
        default_suite: str = "default",
        artifact_name: str = "code",
        detail_chars: int = 1200,
    ) -> None:
        self._harness = harness
        self._store = store
        self._default_suite = default_suite
        self._artifact_name = artifact_name
        self._detail_chars = max(0, int(detail_chars))
        self._definitions: dict[str, PipelineDefinition | None] = {}

    # ------------------------------------------------------------------ 入口

    async def evaluate(
        self, *, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> EvalVerdict:
        """对本次 run 的最新产物给出确定性裁决（任何异常都归为 ``unknown``）。"""
        suite_id, task_id = self._select(run)
        repair_round = _repair_round(run.context)
        try:
            suite = EvalSuite.load(suite_id)
            base = suite.task(task_id) if task_id else None
            if base is None and suite.tasks:
                base = suite.tasks[0]
            task = EvalTask(
                id=f"run:{run.id}",
                requirement=str((run.context or {}).get("requirement", "")),
                scenario=base.scenario if base is not None else "",
                project_id=run.project_id,
                run_id=run.id,
                artifact_name=self._artifact_name,
                # v1.2（docs/09 §4.1.2 唯一注入点）：模板 checks 覆盖套件 checks；
                # 无模板时与套件逐条一致（`list(base.checks)`）。
                checks=self._resolve_checks(run, base),
                # 模板结构规则（纯数据）：None = 评估器走 v1.1 原逻辑
                layout=self._resolve_layout(run),
                # 基线精确引用（R9）：评测侧据此加载基线，绝不猜"当前基线"
                baseline_ref=_baseline_ref(run.context),
            )
            outcome = await self._harness.evaluate_task(
                task, project_id=run.project_id, suite=suite
            )
        except Exception as exc:  # noqa: BLE001 - 评测不可用属降级，不阻断流水线
            logger.exception("确定性裁决失败，降级为 unknown：run=%s", run.id)
            return EvalVerdict(
                status="unknown",
                suite_id=suite_id,
                suite_version="",
                artifact_name=self._artifact_name,
                degraded=True,
                reason=f"评测异常：{exc}",
            )

        report = self._build_report(run, suite, outcome, repair_round)
        await self._save_report(report, run, stage)

        return self._to_verdict(suite, outcome, repair_round)

    # ------------------------------------------------------------------ 解析

    def _resolve_checks(self, run: PipelineRun, base: EvalTask | None) -> list[EvalCheck]:
        """确定性检查命令：模板 ``checks`` 覆盖套件 ``checks``（v1.2，§4.1.2）。

        无模板（或模板已不可读）时**逐条沿用套件**，与 v1.1 完全一致（§10-9）。
        字段名是 ``type``（与 ``EvalCheck.type`` 同构）：模板 YAML 里写 ``kind`` 早在
        加载期就被 ``extra="forbid"`` 拒掉，这里不做二次改名。
        """
        spec = self._template_spec(run)
        if spec is None:
            return list(base.checks) if base is not None else []
        return [
            EvalCheck(type=check.type, run=check.run, weight=check.weight)
            for check in spec.checks
        ]

    def _resolve_layout(self, run: PipelineRun) -> LayoutRules | None:
        """模板结构规则（``LayoutRules`` 纯数据；``None`` = 不做模板结构校验）。"""
        spec = self._template_spec(run)
        return spec.to_layout_rules() if spec is not None else None

    def _template_spec(self, run: PipelineRun) -> TemplateSpec | None:
        """按 ``run.context["template"]["id"]`` 读模板（失败只告警，退回套件口径）。"""
        context = run.context if isinstance(run.context, dict) else {}
        ref = context.get("template")
        template_id = str(ref.get("id") or "").strip() if isinstance(ref, dict) else ""
        if not template_id:
            return None
        try:
            return load_template(template_id)
        except YmirError:
            logger.warning(
                "裁决：模板不可读，退回套件 checks（run=%s template=%s）",
                run.id,
                template_id,
                exc_info=True,
            )
            return None

    def _select(self, run: PipelineRun) -> tuple[str, str | None]:
        """套件 / 任务解析（run.context 覆盖 > 流水线 repair.eval > 缺省）。"""
        context = run.context or {}
        benchmark = context.get("benchmark")
        benchmark = benchmark if isinstance(benchmark, dict) else {}
        suite_id = _first_str(benchmark.get("eval_suite"))
        task_id = _first_str(benchmark.get("eval_task"))

        if suite_id is None or task_id is None:
            definition = self._definition(run.pipeline_id)
            spec = definition.repair.eval if definition and definition.repair else None
            if suite_id is None and spec is not None:
                suite_id = _first_str(spec.suite)
            if task_id is None and spec is not None:
                task_id = _first_str(spec.task)
        return (suite_id or self._default_suite), task_id

    def _definition(self, pipeline_id: str) -> PipelineDefinition | None:
        """加载（并缓存）流水线定义（用于读 ``repair.eval`` 缺省值）。"""
        if pipeline_id not in self._definitions:
            try:
                self._definitions[pipeline_id] = load_pipeline(pipeline_id)
            except Exception:  # noqa: BLE001 - 定义不可读时退回缺省套件
                logger.warning("裁决解析：流水线定义不可读 %s", pipeline_id, exc_info=True)
                self._definitions[pipeline_id] = None
        return self._definitions[pipeline_id]

    # ------------------------------------------------------------------ 报告

    def _build_report(
        self,
        run: PipelineRun,
        suite: EvalSuite,
        outcome: Any,
        repair_round: int,
    ) -> EvaluationReport:
        artifact_version = outcome.artifact_ref.get("version")
        return EvaluationReport.build(
            suite_id=suite.id,
            suite_version=suite.version,
            project_id=run.project_id,
            weights=normalize_weights(suite.weights),
            thresholds=suite.thresholds,
            tasks=[outcome],
            wall_time_ms=int(outcome.wall_time_ms),
            run_id=run.id,
            artifact_version=artifact_version if isinstance(artifact_version, int) else None,
            repair_round=repair_round,
            verdict_source="harness",
        )

    async def _save_report(
        self, report: EvaluationReport, run: PipelineRun, stage: StageSpec
    ) -> None:
        """落盘 ``eval_verdict`` 产物（失败不阻断裁决）。"""
        if self._store is None:
            return
        try:
            await report.save(
                self._store,
                project_id=run.project_id,
                run_id=run.id,
                stage_id=stage.id,
                name=VERDICT_ARTIFACT_NAME,
            )
        except Exception:  # noqa: BLE001 - 落盘失败只影响追溯，不影响裁决
            logger.exception("eval_verdict 报告落盘失败：run=%s", run.id)

    # ------------------------------------------------------------------ 归约

    def _to_verdict(self, suite: EvalSuite, outcome: Any, repair_round: int) -> EvalVerdict:
        """``TaskOut`` -> ``EvalVerdict``（未执行 / 降级 -> ``unknown``）。"""
        artifact_version = outcome.artifact_ref.get("version")
        thresholds = dict(outcome.thresholds or {})
        base = {
            "suite_id": suite.id,
            "suite_version": suite.version,
            "artifact_name": self._artifact_name,
            "artifact_version": artifact_version if isinstance(artifact_version, int) else None,
            "thresholds": thresholds,
            "cost_cny": float(outcome.cost_cny),
            "wall_time_ms": int(outcome.wall_time_ms),
        }

        if outcome.status != "evaluated":
            return EvalVerdict(
                status="unknown",
                failure_class="missing_artifact",
                degraded=True,
                reason=f"评测未执行：{outcome.skip_reason or 'unknown'}",
                **base,
            )

        if outcome.artifact_ref.get("name") is None:
            # 无产物可评（single 模式回退出「0 分报告」）：这属于「产物缺失」降级，
            # 不能当成「代码质量未达标」去触发回边（否则 stub / 空项目会无限返工）。
            return EvalVerdict(
                status="unknown",
                failure_class="missing_artifact",
                degraded=True,
                reason=f"未解析到产物 {self._artifact_name}（无可评内容）",
                **base,
            )

        failed_checks = self._failed_checks(outcome)
        degraded = any(dim.degraded for dim in outcome.dimensions)

        if degraded:
            return EvalVerdict(
                status="unknown",
                passed=bool(outcome.passed),
                total_score=float(outcome.total_score),
                runnability=float(outcome.runnability),
                failed_checks=tuple(failed_checks),
                failure_class="degraded_scoring",
                degraded=True,
                reason="评测降级（评估器或沙箱不可用），不据此回边",
                **base,
            )

        if outcome.passed:
            return EvalVerdict(
                status="pass",
                passed=True,
                total_score=float(outcome.total_score),
                runnability=float(outcome.runnability),
                failed_checks=(),
                reason=(
                    f"双门槛通过（total={outcome.total_score}"
                    f" runnability={outcome.runnability}）"
                ),
                **base,
            )

        return EvalVerdict(
            status="fail",
            passed=False,
            total_score=float(outcome.total_score),
            runnability=float(outcome.runnability),
            failed_checks=tuple(failed_checks),
            failure_class="check_failed" if failed_checks else "below_threshold",
            reason=(
                f"未达标 total={outcome.total_score}"
                f"(<{thresholds.get('total', 75):g}) runnability={outcome.runnability}"
                f"(<{thresholds.get('runnability', 60):g})"
            ),
            **base,
        )

    def _failed_checks(self, outcome: Any) -> list[FailedCheck]:
        """从可运行率维度的命令证据里提取失败检查（输出按 ``detail_chars`` 截断）。"""
        dimension = outcome.dimension(RUNNABILITY)
        if dimension is None:
            return []
        failed: list[FailedCheck] = []
        for evidence in dimension.evidence:
            if evidence.kind != "command":
                continue
            exit_code, timed_out = _parse_exit(evidence.detail)
            if exit_code == 0 and not timed_out:
                continue
            failed.append(
                FailedCheck(
                    command=evidence.ref,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    output=_output_of(evidence.detail)[: self._detail_chars],
                )
            )
        return failed


def _parse_exit(detail: str) -> tuple[int | None, bool]:
    """从证据 detail 解析退出码与超时标记（解析失败 -> ``(None, False)``）。"""
    match = _EVIDENCE_RE.search(detail or "")
    if match is None:
        return None, False
    return int(match.group(1)), match.group(2) == "True"


def _output_of(detail: str) -> str:
    """取证据里的输出片段（``exit=… ｜<片段>`` 的分隔符之后）。"""
    if _DETAIL_SEPARATOR in (detail or ""):
        return detail.split(_DETAIL_SEPARATOR, 1)[1].strip()
    return (detail or "").strip()


def _repair_round(context: dict[str, Any] | None) -> int:
    """当前修复轮次（0 = 首轮）。"""
    repair = (context or {}).get("repair")
    if not isinstance(repair, dict):
        return 0
    try:
        return int(repair.get("round", 0))
    except (TypeError, ValueError):
        return 0


def _first_str(value: Any) -> str | None:
    """把配置值归一为「非空字符串或 None」。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _baseline_ref(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """取基线精确引用（``run.context["baseline"]``，R9 要求按它加载而非猜最新版本）。"""
    ref = (context or {}).get("baseline")
    return ref if isinstance(ref, dict) and ref else None
