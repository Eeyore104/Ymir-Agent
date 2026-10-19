"""评测报告一等产物（架构设计 §12.3、§12.4；docs/07 §4.1.4 增量）。

``EvaluationReport`` 作为一等产物入库：``type=report``、带版本、可 diff
（两次报告分数对比）。序列化为 ``evaluation_report.md``（人读）+
``evaluation_report.json``（机读，供前端 / 回归对比消费）。

v1.1 增量（全部带缺省值，旧报告 JSON 缺字段仍可解析）：

- ``TaskOut.status`` / ``skip_reason``：区分「未执行」与「未通过」，未执行任务
  不进通过率分母（docs/07 §4.1.4、§14 跳过原因）；
- ``TaskOut.scenario`` / ``project_id`` / ``run_id``：追溯与按场景聚合；
- ``EvaluationReport.run_id`` / ``artifact_version`` / ``repair_round`` /
  ``verdict_source``：报告追溯字段（R6 追溯纪律）；
- ``executed`` / ``skipped`` 口径 + ``executed_count`` / ``skipped_count`` /
  ``first_pass_rate`` / ``final_pass_rate``（分母 0 时为 ``None``，不返回 0）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ymir.evaluation.evaluators.base import DIMENSION_LABELS, DIMENSION_ORDER, EvalScore
from ymir.evaluation.suite import Thresholds

REPORT_ARTIFACT_NAME = "evaluation_report"
REPORT_ARTIFACT_TYPE = "report"

#: 任务执行状态（docs/07 §4.1.4）。``skipped`` 不算失败、不进通过率分母。
STATUS_EVALUATED: str = "evaluated"
STATUS_SKIPPED: str = "skipped"

#: 跳过原因（docs/07 §14「跳过原因」唯一口径：unbound_task | missing_artifact | budget | not_run）
SKIP_UNBOUND_TASK: str = "unbound_task"
SKIP_MISSING_ARTIFACT: str = "missing_artifact"
SKIP_BUDGET: str = "budget"
SKIP_NOT_RUN: str = "not_run"

TaskStatus = Literal["evaluated", "skipped"]
VerdictSource = Literal["harness", "llm_qa"]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class EvidenceOut(BaseModel):
    """评分证据（可定位）。"""

    kind: str
    ref: str
    detail: str = ""


class DimensionOut(BaseModel):
    """单维评分（0-100，附证据链）。"""

    name: str
    label: str
    score: float
    weight: float
    rationale: str = ""
    evidence: list[EvidenceOut] = Field(default_factory=list)
    cost_cny: float = 0.0
    tokens: int = 0
    #: 该维度是否降级（评估器异常 / 沙箱不可用），供前端显著标记（QA-10）
    degraded: bool = False

    @classmethod
    def from_score(cls, score: EvalScore, *, weight: float | None = None) -> DimensionOut:
        return cls(
            name=score.name,
            label=DIMENSION_LABELS.get(score.name, score.name),
            score=round(score.score, 2),
            weight=score.weight if weight is None else weight,
            rationale=score.rationale,
            evidence=[EvidenceOut(**item.to_dict()) for item in score.evidence],
            cost_cny=round(score.cost_cny, 6),
            tokens=score.tokens,
            degraded=score.degraded,
        )


class TaskOut(BaseModel):
    """单任务评测结果。

    ``status="skipped"`` 表示该任务**未执行**（未绑定产物 / 产物缺失 / 预算耗尽），
    此时 ``dimensions`` 为空、``total_score`` / ``runnability`` / ``cost_cny`` /
    ``tokens`` 全为 0，且**不计入**通过率分母。
    """

    id: str
    scenario: str = ""
    status: TaskStatus = STATUS_EVALUATED
    skip_reason: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    requirement: str = ""
    artifact_ref: dict[str, Any] = Field(default_factory=dict)
    dimensions: list[DimensionOut] = Field(default_factory=list)
    total_score: float = 0.0
    runnability: float = 0.0
    passed: bool = False
    thresholds: dict[str, float] = Field(default_factory=dict)
    wall_time_ms: int = 0
    cost_cny: float = 0.0
    tokens: int = 0

    def dimension(self, name: str) -> DimensionOut | None:
        for item in self.dimensions:
            if item.name == name:
                return item
        return None

    @property
    def executed(self) -> bool:
        """该任务是否真的执行过（``skipped`` 为未执行）。"""
        return self.status == STATUS_EVALUATED

    @classmethod
    def skipped_task(
        cls,
        task_id: str,
        *,
        reason: str,
        requirement: str = "",
        scenario: str = "",
        project_id: str | None = None,
        run_id: str | None = None,
        thresholds: dict[str, float] | None = None,
        wall_time_ms: int = 0,
    ) -> TaskOut:
        """构造「未执行」任务结果（无沙箱、无 judge、零成本）。"""
        return cls(
            id=task_id,
            scenario=scenario,
            status=STATUS_SKIPPED,
            skip_reason=reason,
            project_id=project_id,
            run_id=run_id,
            requirement=requirement,
            artifact_ref={"artifact_id": None, "name": None, "version": None},
            dimensions=[],
            total_score=0.0,
            runnability=0.0,
            passed=False,
            thresholds=dict(thresholds or {}),
            wall_time_ms=wall_time_ms,
            cost_cny=0.0,
            tokens=0,
        )


class EvaluationReport(BaseModel):
    """一次评测的完整报告（一等产物）。"""

    suite_id: str
    suite_version: str = "1.0"
    project_id: str | None = None
    # --- 追溯字段（R6）：哪个 run、哪个产物版本、第几轮修复、裁决来源 ---
    run_id: str | None = None
    artifact_version: int | None = None
    repair_round: int | None = None
    verdict_source: VerdictSource = "harness"
    generated_at: str = Field(default_factory=_utcnow_iso)
    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    tasks: list[TaskOut] = Field(default_factory=list)
    total_score: float = 0.0
    runnability: float = 0.0
    passed: bool = False
    cost_cny: float = 0.0
    total_tokens: int = 0
    wall_time_ms: int = 0
    # --- 汇总口径（分母 = executed；分母 0 时为 None，避免「0% 通过率」误读）---
    executed_count: int = 0
    skipped_count: int = 0
    first_pass_rate: float | None = None
    #: 派生字段，勿手写；单次报告 ``first_pass_rate == final_pass_rate``
    #: （单次只评一个产物版本）；批量口径见 BaselineTable
    final_pass_rate: float | None = None

    def model_post_init(self, __context: Any) -> None:
        """构造后按 ``tasks`` 归算执行/跳过计数与通过率（派生字段，始终一致）。"""
        self.refresh_summary()

    def refresh_summary(self) -> None:
        """按当前 ``tasks`` 重算汇总口径：恒重算，派生字段唯一写入口。

        通过率**无条件**由 ``tasks`` 派生（分母 0 -> ``None``）：不允许任何调用方
        预设数值，否则会出现 ``tasks[].passed=false`` 却 ``final_pass_rate=1.0``
        的自相矛盾报告（违反 R6「任何分数必须可追溯」）。
        """
        executed = self.executed
        self.executed_count = len(executed)
        self.skipped_count = len(self.skipped)
        if not executed:
            self.first_pass_rate = None
            self.final_pass_rate = None
            return
        rate = round(sum(1 for task in executed if task.passed) / len(executed), 6)
        self.first_pass_rate = rate
        self.final_pass_rate = rate

    # ------------------------------------------------------------------ 口径

    @property
    def executed(self) -> list[TaskOut]:
        """已执行任务（``status == evaluated``，通过率分母）。"""
        return [task for task in self.tasks if task.status == STATUS_EVALUATED]

    @property
    def skipped(self) -> list[TaskOut]:
        """未执行任务（``status == skipped``，不算失败、不进分母）。"""
        return [task for task in self.tasks if task.status == STATUS_SKIPPED]

    # ------------------------------------------------------------------ 组装

    @classmethod
    def build(
        cls,
        *,
        suite_id: str,
        suite_version: str,
        project_id: str | None,
        weights: dict[str, float],
        thresholds: Thresholds,
        tasks: Sequence[TaskOut],
        wall_time_ms: int,
        run_id: str | None = None,
        artifact_version: int | None = None,
        repair_round: int | None = None,
        verdict_source: VerdictSource = "harness",
    ) -> EvaluationReport:
        """汇总报告：报告级总分 = 各**已执行**任务均值；双门槛在报告级复核。

        全为 ``evaluated`` 时与 v1.0 逐字节等价（分母集合不变）。
        """
        task_list = list(tasks)
        executed = [task for task in task_list if task.status == STATUS_EVALUATED]
        total = _mean([task.total_score for task in executed])
        runnability = _mean([task.runnability for task in executed])
        cost = round(sum(task.cost_cny for task in task_list), 6)
        tokens = sum(task.tokens for task in task_list)
        passed = (
            bool(executed)
            and total >= thresholds.total
            and runnability >= thresholds.runnability
        )
        return cls(
            suite_id=suite_id,
            suite_version=suite_version,
            project_id=project_id,
            run_id=run_id,
            artifact_version=artifact_version,
            repair_round=repair_round,
            verdict_source=verdict_source,
            weights=weights,
            thresholds={"total": thresholds.total, "runnability": thresholds.runnability},
            tasks=task_list,
            total_score=round(total, 2),
            runnability=round(runnability, 2),
            passed=passed,
            cost_cny=cost,
            total_tokens=tokens,
            wall_time_ms=wall_time_ms,
        )

    # ------------------------------------------------------------------ 序列化

    def to_markdown(self) -> str:
        """渲染为 Markdown 报告（维度分 + 证据链 + 通过判定）。

        「未执行」任务单独标注 ``skip_reason``，不与「未通过」混写；
        未执行计数行仅在存在未执行任务时输出（全执行时与 v1.0 逐字节一致）。
        """
        verdict = "✅ 通过" if self.passed else "❌ 未通过"
        lines: list[str] = [
            f"# 评测报告（{verdict}）",
            "",
            f"- 套件：`{self.suite_id}` v{self.suite_version}",
            f"- 生成时间：{self.generated_at}",
            f"- 加权总分：**{self.total_score:.2f}** / 100"
            f"（门槛 {self.thresholds.get('total', 75)}）",
            f"- 可运行率：**{self.runnability:.2f}**"
            f"（门槛 {self.thresholds.get('runnability', 60)}）",
            f"- 评测成本：¥{self.cost_cny:.6f} / {self.total_tokens} tokens / "
            f"{self.wall_time_ms} ms",
            f"- 权重：{self._weights_text()}",
            "",
        ]
        if self.skipped_count:
            lines += [
                f"- 已执行/未执行：{self.executed_count} / {self.skipped_count}"
                f"（未执行不计入通过率分母）",
                "",
            ]
        for task in self.tasks:
            if task.status == STATUS_SKIPPED:
                state = f"未执行：{task.skip_reason or SKIP_NOT_RUN}"
            else:
                state = "通过" if task.passed else "未通过"
            lines += [
                f"## 任务 `{task.id}`（{state}，总分 {task.total_score:.2f}）",
                "",
                f"- 产物：{json.dumps(task.artifact_ref, ensure_ascii=False)}",
                f"- 需求：{task.requirement or '（未提供）'}",
                "",
                "| 维度 | 分数 | 权重 | 理由 |",
                "|---|---|---|---|",
            ]
            for dim in task.dimensions:
                lines.append(
                    f"| {dim.label} | {dim.score:.2f} | {dim.weight:.2f} | "
                    f"{_cell(dim.rationale)} |"
                )
            lines.append("")
            lines.append("### 证据链")
            lines.append("")
            for dim in task.dimensions:
                lines.append(f"**{dim.label}**：")
                if not dim.evidence:
                    lines.append("- （无证据）")
                for item in dim.evidence:
                    lines.append(f"- `[{item.kind}]` {item.ref} — {_cell(item.detail)}")
                lines.append("")
        return "\n".join(lines).strip() + "\n"

    def to_json(self) -> str:
        """机读 JSON（前端 / 回归对比消费）。"""
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2)

    def files(self) -> dict[str, str]:
        """产物文件集（落 :class:`ArtifactStore`）。"""
        return {
            "evaluation_report.md": self.to_markdown(),
            "evaluation_report.json": self.to_json() + "\n",
        }

    async def save(
        self,
        store: Any,
        *,
        project_id: str,
        run_id: str | None = None,
        stage_id: str | None = None,
        created_by: str | None = "qa",
        name: str = REPORT_ARTIFACT_NAME,
    ) -> Any:
        """作为一等产物入库（``type=report``，带版本可 diff，§12.4）。"""
        return await store.save(
            project_id=project_id,
            name=name,
            artifact_type=REPORT_ARTIFACT_TYPE,
            files=self.files(),
            run_id=run_id,
            stage_id=stage_id,
            created_by=created_by,
            message=(
                f"评测报告 {self.suite_id} v{self.suite_version}"
                f"（总分 {self.total_score:.2f}）"
            ),
        )

    # ------------------------------------------------------------------ 对比

    def compare(self, other: EvaluationReport) -> dict[str, Any]:
        """两次报告分数对比（回归信号，§12.4）。"""
        return {
            "total_score": _delta(self.total_score, other.total_score),
            "runnability": _delta(self.runnability, other.runnability),
            "dimensions": {
                name: _delta(self._dimension_mean(name), other._dimension_mean(name))
                for name in DIMENSION_ORDER
            },
        }

    def _dimension_mean(self, name: str) -> float:
        values = [
            dim.score
            for task in self.tasks
            for dim in task.dimensions
            if dim.name == name
        ]
        return _mean(values)

    def _weights_text(self) -> str:
        return " / ".join(
            f"{DIMENSION_LABELS.get(name, name)} {self.weights.get(name, 0.0):.0%}"
            for name in DIMENSION_ORDER
        )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _delta(current: float, previous: float) -> float:
    return round(current - previous, 2)


def _cell(text: str) -> str:
    return (text or "").replace("\n", " ").replace("|", "\\|")[:300]
