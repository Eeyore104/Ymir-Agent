"""基准表渲染（v1.1 T2.2，docs/07 §4.2.7）。

把 :class:`~ymir.evaluation.benchmark.BenchmarkState` 渲染成两份**同源**产物：

- ``data/benchmark/baseline-<batch>.md``：人读（表头元信息 + 12 行明细）；
- ``data/benchmark/baseline-<batch>.json``：机读（metadata + rows + summary）。

三条不变量：

1. **表头必含执行环境指纹**（A-04②）：日期 / 批量 ID / limit+partial / 模型路由 /
   judge 模型与温度 / 沙箱类型与镜像 tag / network / 套件版本 / 流水线版本 /
   成本口径 / ``executed``·``skipped`` 数 / ``score_source`` 分布；
2. **12 行字段无空缺**：``skipped`` 行只填 ``skip_reason``（不填 ``failure_class``），
   ``done`` 行六个评分字段齐全；同一状态文件两次渲染**字节一致**（表内无 ``now()``）；
3. **两种沙箱不得混表**：``docker`` 与 ``local`` 混在一起会让读者误以为同一口径，
   直接 ``InvalidParamError``（1001）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ymir.common.errors import InvalidParamError
from ymir.evaluation.benchmark import DEFAULT_THRESHOLDS, FAILURE_ORDER, BenchmarkState
from ymir.evaluation.suite import EvalSuite

#: 未执行原因的人读标签
SKIP_LABELS: dict[str, str] = {
    "not_run": "本轮未发起",
    "budget": "批次预算达上限",
    "unbound_task": "未绑定产物",
    "missing_artifact": "产物缺失",
}

#: 失败分类的中文标签（六分类，§4.2.6）
FAILURE_LABELS: dict[str, str] = {
    "missing_artifact": "缺文件",
    "dependency_missing": "依赖缺失",
    "timeout": "超时",
    "check_failed": "检查失败",
    "below_threshold": "未达门槛",
    "degraded_scoring": "评分降级",
}

#: 表头/表尾固定文案（渲染确定性）
_COST_SOURCE_LABEL = "实测"
_DASH = "—"


@dataclass
class BaselineRow:
    """基线表的一行（12 个任务各一行，字段与状态文件一一对应）。"""

    task_id: str
    scenario: str
    status: str
    first_total: float | None = None
    first_pass: bool | None = None
    final_total: float | None = None
    final_pass: bool | None = None
    repair_rounds: int | None = None
    cost_cny: float = 0.0
    wall_time_s: float | None = None
    failure_class: str | None = None
    failure_reason: str = ""
    skip_reason: str | None = None
    score_source: str = "pipeline"
    sandbox: str = "docker"

    @property
    def executed(self) -> bool:
        return self.status == "done"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "scenario": self.scenario,
            "status": self.status,
            "first_total": self.first_total,
            "first_pass": self.first_pass,
            "final_total": self.final_total,
            "final_pass": self.final_pass,
            "repair_rounds": self.repair_rounds,
            "cost_cny": round(self.cost_cny, 6),
            "wall_time_s": self.wall_time_s,
            "failure_class": self.failure_class,
            "failure_reason": self.failure_reason,
            "skip_reason": self.skip_reason,
            "score_source": self.score_source,
            "sandbox": self.sandbox,
        }


@dataclass
class BaselineTable:
    """一份基准表（markdown + json 双渲染的同源数据）。"""

    batch_id: str
    suite_id: str
    suite_version: str
    pipeline_id: str = "software-dev"
    pipeline_version: str = ""
    started_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    rows: list[BaselineRow] = field(default_factory=list)
    limit: int | None = None
    partial: bool = False
    budget_cny: float | None = None
    spent_cny: float = 0.0

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_state(cls, state: BenchmarkState, suite: EvalSuite) -> BaselineTable:
        """按套件声明顺序展开 12 行（缺行补 ``skipped{not_run}``，保证无空缺）。"""
        rows = [_row_of(task.id, task.scenario, state.tasks.get(task.id)) for task in suite.tasks]
        table = cls(
            batch_id=state.batch_id,
            suite_id=state.suite_id or suite.id,
            suite_version=state.suite_version or suite.version,
            pipeline_id=state.pipeline_id,
            pipeline_version=state.pipeline_version,
            started_at=state.started_at,
            meta=dict(state.meta),
            rows=rows,
            limit=state.limit,
            partial=state.partial,
            budget_cny=state.budget_cny,
            spent_cny=state.spent_cny,
        )
        table.validate()
        return table

    def validate(self) -> None:
        """两种沙箱不得混表（``docker`` 与 ``local`` 口径不同，混表会误导）。"""
        sandboxes = {row.sandbox for row in self.rows if row.executed}
        if len(sandboxes) > 1:
            raise InvalidParamError(
                f"基线表不得混用沙箱口径：{sorted(sandboxes)}（一次批次只允许一种 sandbox）"
            )

    # ------------------------------------------------------------------ 汇总

    @property
    def executed(self) -> list[BaselineRow]:
        return [row for row in self.rows if row.executed]

    @property
    def skipped(self) -> list[BaselineRow]:
        return [row for row in self.rows if not row.executed]

    @property
    def pipeline_rows(self) -> list[BaselineRow]:
        """主路径行（``score_source="pipeline"``）：首轮通过率只统计它们。"""
        return [row for row in self.executed if row.score_source == "pipeline"]

    @property
    def first_pass_rate(self) -> float | None:
        """首轮通过率：分母只含 ``pipeline`` 行（``fallback`` 是事后补评，不混算）。"""
        rows = self.pipeline_rows
        if not rows:
            return None
        return round(sum(1 for row in rows if row.first_pass) / len(rows), 6)

    @property
    def final_pass_rate(self) -> float | None:
        rows = self.executed
        if not rows:
            return None
        return round(sum(1 for row in rows if row.final_pass) / len(rows), 6)

    def score_source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"pipeline": 0, "fallback": 0}
        for row in self.executed:
            counts[row.score_source] = counts.get(row.score_source, 0) + 1
        return counts

    def failure_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(FAILURE_ORDER, 0)
        for row in self.executed:
            if row.failure_class:
                counts[row.failure_class] = counts.get(row.failure_class, 0) + 1
        return counts

    # ------------------------------------------------------------------ 渲染

    def summary(self) -> dict[str, Any]:
        """机读汇总（json 的 ``summary`` 段）。"""
        return {
            "executed": len(self.executed),
            "skipped": len(self.skipped),
            "first_pass_rate": self.first_pass_rate,
            "final_pass_rate": self.final_pass_rate,
            "score_source": self.score_source_counts(),
            "failure_counts": self.failure_counts(),
            "cost_cny": round(self.spent_cny, 6),
            "cost_source": _COST_SOURCE_LABEL,
        }

    def to_markdown(self) -> str:
        """人读表格（确定性：不含 ``now()``，两次渲染字节一致）。"""
        meta = self.meta
        partial_text = f"是（limit={self.limit}）" if self.partial else "否（全量）"
        judge = meta.get("judge", _DASH)
        routing = meta.get("model_routing", _DASH)
        sandbox = self.rows[0].sandbox if self.rows else meta.get("sandbox", "docker")
        image = meta.get("image", _DASH)
        probe = _probe_text(meta.get("image_probe"))
        network = meta.get("network", "none")
        counts = self.score_source_counts()
        budget = "无上限" if self.budget_cny is None else f"{self.budget_cny:.2f}"

        lines = [
            f"# v1.1 基准表（套件 {self.suite_id} v{self.suite_version}）",
            "",
            f"- 跑分时间：{self.started_at} ｜ 批次：{self.batch_id} ｜ 部分执行：{partial_text}",
            f"- 模型路由：{routing} ｜ judge={judge}",
            f"- 沙箱：{sandbox} `{image}`（{probe}，network={network}） ｜ 成本口径："
            f"{_COST_SOURCE_LABEL}（上限 {budget}）",
            f"- 流水线：{self.pipeline_id} v{self.pipeline_version or _DASH} ｜ "
            f"套件版本：{self.suite_id} v{self.suite_version}",
            "- "
            f"执行 {len(self.executed)} / 跳过 {len(self.skipped)} → "
            f"首轮通过率 {_rate_text(self.first_pass_rate, self.pipeline_rows, 'first')}"
            f" ｜ 最终通过率 {_rate_text(self.final_pass_rate, self.executed, 'final')}",
            f"- 评分来源：pipeline {counts.get('pipeline', 0)} / fallback "
            f"{counts.get('fallback', 0)}（fallback 为事后补评，不计入首轮通过率） ｜ "
            f"实测成本：¥{self.spent_cny:.4f}",
            "",
            "| 任务 | 场景 | 首轮 | 首轮通过 | 修复后 | 最终通过 | 轮次 | 成本(¥) | 时长(s) "
            "| 失败分类 | 原因 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in self.rows:
            lines.append(_row_markdown(row))
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict[str, Any]:
        """机读（metadata + rows + summary，字段固定、顺序稳定）。"""
        return {
            "batch_id": self.batch_id,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "started_at": self.started_at,
            "meta": dict(self.meta),
            "limit": self.limit,
            "partial": self.partial,
            "budget_cny": self.budget_cny,
            "thresholds": dict(DEFAULT_THRESHOLDS),
            "summary": self.summary(),
            "rows": [row.to_dict() for row in self.rows],
        }

    def save(self, directory: Path | None = None) -> tuple[Path, Path]:
        """落盘两份产物（同名不同扩展）；返回 ``(md_path, json_path)``。"""
        target = Path(directory) if directory is not None else Path("data") / "benchmark"
        target.mkdir(parents=True, exist_ok=True)
        md_path = target / f"baseline-{self.batch_id}.md"
        json_path = target / f"baseline-{self.batch_id}.json"
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        json_path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return md_path, json_path


# --------------------------------------------------------------------- 工具


def _row_of(task_id: str, scenario: str, raw: dict[str, Any] | None) -> BaselineRow:
    """状态行 -> 表行（缺行按 ``not_run`` 补齐，保证 12 行无空缺）。"""
    if raw is None:
        return BaselineRow(
            task_id=task_id,
            scenario=scenario,
            status="skipped",
            skip_reason="not_run",
            failure_reason=SKIP_LABELS["not_run"],
        )
    wall_ms = raw.get("wall_time_ms")
    return BaselineRow(
        task_id=task_id,
        scenario=scenario,
        status=str(raw.get("status", "skipped")),
        first_total=raw.get("first_total"),
        first_pass=raw.get("first_pass"),
        final_total=raw.get("final_total"),
        final_pass=raw.get("final_pass"),
        repair_rounds=raw.get("repair_rounds"),
        cost_cny=float(raw.get("cost_cny") or 0.0),
        wall_time_s=round(wall_ms / 1000.0, 1) if isinstance(wall_ms, (int, float)) else None,
        failure_class=raw.get("failure_class"),
        failure_reason=str(raw.get("failure_reason") or ""),
        skip_reason=raw.get("skip_reason"),
        score_source=str(raw.get("score_source") or "pipeline"),
        sandbox=str(raw.get("sandbox") or "docker"),
    )


def _row_markdown(row: BaselineRow) -> str:
    if not row.executed:
        skip_label = SKIP_LABELS.get(row.skip_reason or "", row.skip_reason or "未执行")
        cells = [
            row.task_id,
            row.scenario,
            _DASH,
            _DASH,
            _DASH,
            _DASH,
            _DASH,
            f"{row.cost_cny:.4f}",
            _DASH,
            _DASH,
            f"未执行（{skip_label}）",
        ]
        return "| " + " | ".join(cells) + " |"

    failure = FAILURE_LABELS.get(row.failure_class or "", _DASH) if row.failure_class else _DASH
    cells = [
        row.task_id,
        row.scenario,
        _num(row.first_total),
        _flag(row.first_pass),
        _num(row.final_total),
        _flag(row.final_pass),
        str(row.repair_rounds) if row.repair_rounds is not None else _DASH,
        f"{row.cost_cny:.4f}",
        f"{row.wall_time_s:.1f}" if row.wall_time_s is not None else _DASH,
        failure,
        row.failure_reason or _DASH,
    ]
    return "| " + " | ".join(cells) + " |"


def _num(value: float | None) -> str:
    return _DASH if value is None else f"{value:.1f}"


def _flag(value: bool | None) -> str:
    if value is None:
        return _DASH
    return "✅" if value else "❌"


def _rate_text(rate: float | None, rows: list[BaselineRow], kind: str) -> str:
    if rate is None or not rows:
        return f"{_DASH}（0/0）"
    passed = sum(1 for row in rows if (row.first_pass if kind == "first" else row.final_pass))
    return f"{rate * 100:.1f}%（{passed}/{len(rows)}）"


def _probe_text(probe: Any) -> str:
    if not isinstance(probe, dict) or not probe:
        return "probe 未记录"
    parts = [f"{name} {version}" for name, version in sorted(probe.items())]
    return " / ".join(parts) + " 预置"


__all__ = [
    "FAILURE_LABELS",
    "SKIP_LABELS",
    "BaselineRow",
    "BaselineTable",
]
