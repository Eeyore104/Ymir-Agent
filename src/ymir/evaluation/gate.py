"""确定性裁决闸门实现（docs/07 §4.4.3；T3.2 核心）。

把 :class:`~ymir.evaluation.harness.EvaluationHarness` 的四维评测收敛成一份
不可变合同 :class:`~ymir.common.contracts.EvalVerdict`（``pass`` / ``fail`` /
``unknown``），供编排层回边判定消费 —— **QA 的 LLM 叙事报告不参与裁决**（§4.4.1）。

一个 ``evaluate`` 调用做五件事：

1. **任务装配交给** :func:`ymir.evaluation.run_task.build_task_for_run`（v1.3 T4.1：
   唯一装配点 —— 套件解析 / 模板 checks 覆盖 / layout / baseline_ref 都在那里，
   批量补评与单跑脚本共用同一实现，杜绝"两条路径两套分数"）；
2. ``harness.evaluate_task(...)`` 走四维 + 双门槛；
4. 报告落盘为 **``eval_verdict``**（不与 QA 的 ``evaluation_report`` 抢名），
   并写入 ``run_id / artifact_version / repair_round / verdict_source="harness"``；
5. 归约为 ``EvalVerdict``：``skipped`` / 降级 / 异常一律 ``unknown``
   （不回边、不阻断流水线，退回 v1.0 线性行为）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ymir.common.contracts import EvalVerdict, FailedCheck
from ymir.evaluation.aggregator import normalize_weights
from ymir.evaluation.baseline_probe import BASELINE_UNHEALTHY
from ymir.evaluation.evaluators.base import RUNNABILITY
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.report import EvaluationReport
from ymir.evaluation.run_task import build_task_for_run, resolve_suite_id
from ymir.evaluation.suite import EvalSuite
from ymir.models.entities import PipelineRun
from ymir.sop.models import StageSpec

logger = logging.getLogger(__name__)

#: 确定性裁决报告的落盘名（与 QA 的 ``evaluation_report`` 并存，§4.4.3）
VERDICT_ARTIFACT_NAME = "eval_verdict"

#: 失败检查的证据格式：``exit=1 timed_out=False duration=12ms｜<输出片段>``
_EVIDENCE_RE = re.compile(r"exit=(-?\d+) timed_out=(True|False)")
_DETAIL_SEPARATOR = "｜"

#: 失败分类 · 基线不健康（v1.3 T1.2；与 ``benchmark.FAILURE_BASELINE_UNHEALTHY`` 同值）。
FAILURE_BASELINE_UNHEALTHY: str = "baseline_unhealthy"

#: 阻断项失败时 ``reason`` 的固定前缀（v1.3 Q4，docs/11 §7.5-5）。
BLOCKING_REASON_PREFIX: str = "阻断项失败"


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

    # ------------------------------------------------------------------ 入口

    async def evaluate(
        self, *, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> EvalVerdict:
        """对本次 run 的最新产物给出确定性裁决（任何异常都归为 ``unknown``）。"""
        repair_round = _repair_round(run.context)
        try:
            # v1.3（T4.1）：装配收敛到 run_task，与批量补评 / 单跑脚本同源
            task, suite = build_task_for_run(
                run,
                artifact_name=self._artifact_name,
                default_suite=self._default_suite,
            )
            outcome = await self._harness.evaluate_task(
                task, project_id=run.project_id, suite=suite
            )
        except Exception as exc:  # noqa: BLE001 - 评测不可用属降级，不阻断流水线
            logger.exception("确定性裁决失败，降级为 unknown：run=%s", run.id)
            return EvalVerdict(
                status="unknown",
                # 套件 id 仍要可追溯：用同一套解析口径（不抛异常）
                suite_id=resolve_suite_id(run, default_suite=self._default_suite),
                suite_version="",
                artifact_name=self._artifact_name,
                degraded=True,
                reason=f"评测异常：{exc}",
            )

        report = self._build_report(run, suite, outcome, repair_round)
        await self._save_report(report, run, stage)

        return self._to_verdict(suite, outcome, repair_round)

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
        """``TaskOut`` -> ``EvalVerdict``（未执行 / 降级 -> ``unknown``）。

        分支顺序（v1.3 追加 2 个分支，位置是**刻意**的，docs/11 §7.3-7）：

        1. ``status != "evaluated"`` → ``unknown``
        2. 无产物 → ``unknown``
        3. ★ **基线不健康** → ``unknown(baseline_unhealthy)``（"分数不可比"比
           "某维度降级"更根本，故排在 4 之前）
        4. 维度降级 → ``unknown(degraded_scoring)``
        5. ★ **阻断项失败** → ``fail(check_failed)``（Q4：优先级**高于**分数，
           故排在 6 之前）
        6. 通过 → ``pass``
        7. 其它 → ``fail``

        所有分支都把 ``baseline_status`` / ``regression`` / ``blocking_failed``
        抄进 ``EvalVerdict``（供 ``RepairController`` 写 history、``benchmark`` 读回分类）。
        """
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
        # v1.3：三个新字段在所有分支都抄一遍（缺省值形态对旧数据无害）
        incremental = _incremental_fields(outcome)

        if outcome.status != "evaluated":
            return EvalVerdict(
                status="unknown",
                failure_class="missing_artifact",
                degraded=True,
                reason=f"评测未执行：{outcome.skip_reason or 'unknown'}",
                **base,
                **incremental,
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
                **incremental,
            )

        if incremental["baseline_status"] == BASELINE_UNHEALTHY:
            # T1.2 三态之二：基线自身就失败 ⇒ 分数不可比，按 unknown 降级**不阻断**
            return EvalVerdict(
                status="unknown",
                passed=False,
                total_score=float(outcome.total_score),
                runnability=float(outcome.runnability),
                failure_class=FAILURE_BASELINE_UNHEALTHY,
                degraded=True,
                reason="基线自身检查未通过（基线不健康），本次分数不可比、不据此回边",
                **base,
                **incremental,
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
                **incremental,
            )

        if incremental["blocking_failed"]:
            # Q4：阻断项失败 ⇒ 不可交付。**不新增失败分类**（复用 check_failed），
            # 分数照算照披露（可能出现「总分 78 但未通过：阻断项失败」）。
            return EvalVerdict(
                status="fail",
                passed=False,
                total_score=float(outcome.total_score),
                runnability=float(outcome.runnability),
                failed_checks=tuple(failed_checks),
                failure_class="check_failed",
                reason=(
                    f"{BLOCKING_REASON_PREFIX}（"
                    + "、".join(incremental["blocking_failed"])
                    + f"）；total={outcome.total_score} runnability={outcome.runnability}"
                ),
                **base,
                **incremental,
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
                **incremental,
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
            **incremental,
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


def _incremental_fields(outcome: Any) -> dict[str, Any]:
    """从 ``TaskOut`` 抄出 v1.3 三个新字段（缺省值形态对旧数据无害）。

    ``TaskOut`` 是 pydantic 模型，新字段有默认值；用 ``getattr`` 兜底是为了让
    既有测试里的 ``SimpleNamespace`` 伪 outcome 也能喂进 ``_to_verdict``。
    """
    probe = getattr(outcome, "baseline_runnability", None)
    status = getattr(probe, "status", "") if probe is not None else ""
    blocking = getattr(outcome, "blocking_failed", None) or ()
    return {
        "baseline_status": str(status or ""),
        "regression": bool(getattr(outcome, "regression", False)),
        "blocking_failed": tuple(str(item) for item in blocking),
    }


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


def _baseline_ref(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """取基线精确引用（``run.context["baseline"]``，R9 要求按它加载而非猜最新版本）。"""
    ref = (context or {}).get("baseline")
    return ref if isinstance(ref, dict) and ref else None
