"""批量跑分（架构设计 §12.4）。

对同一个套件、不同「模型路由组合 / 产物版本」跑分，输出**性价比榜单**
（``total_score`` vs ``cost_cny`` 表格）：直接验证分级路由策略的有效性，
也作为提示词 / 角色 / 流水线变更后的回归基线（分数变化即回归信号）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ymir.evaluation.harness import EvaluationHarness


@dataclass
class BatchTarget:
    """一个待跑分目标（一个产物版本 / 一个路由组合）。"""

    label: str
    version: int | None = None
    route: str = ""
    #: 生成该产物的模型成本（元）；为 0 时回退评测自身成本。
    cost_cny: float = 0.0


@dataclass
class BatchEntry:
    """一条跑分结果。"""

    label: str
    route: str
    version: int | None
    total_score: float
    runnability: float
    passed: bool
    cost_cny: float
    wall_time_ms: int
    cost_per_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "route": self.route,
            "version": self.version,
            "total_score": self.total_score,
            "runnability": self.runnability,
            "passed": self.passed,
            "cost_cny": self.cost_cny,
            "cost_per_score": self.cost_per_score,
            "wall_time_ms": self.wall_time_ms,
        }


@dataclass
class BatchReport:
    """批量跑分汇总（性价比榜单）。"""

    suite_id: str
    project_id: str
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    entries: list[BatchEntry] = field(default_factory=list)

    def best_value(self) -> BatchEntry | None:
        """性价比最优（每分成本最低且得分 > 0）。"""
        candidates = [entry for entry in self.entries if entry.total_score > 0]
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry.cost_per_score)

    def best_score(self) -> BatchEntry | None:
        """总分最高。"""
        return max(self.entries, key=lambda entry: entry.total_score, default=None)

    def to_table(self) -> str:
        """ASCII 对比表（total_score vs cost_cny）。"""
        header = (
            f"{'标签':<14} {'路由':<18} {'版本':<6} {'总分':>7} "
            f"{'可运行':>7} {'通过':>5} {'成本(¥)':>10} {'分/元':>9}"
        )
        lines = [header, "-" * len(header.encode("gbk", errors="replace"))]
        for entry in sorted(self.entries, key=lambda e: e.total_score, reverse=True):
            version = "-" if entry.version is None else str(entry.version)
            lines.append(
                f"{entry.label:<14} {entry.route or '-':<18} {version:<6} "
                f"{entry.total_score:>7.2f} {entry.runnability:>7.2f} "
                f"{'是' if entry.passed else '否':>5} {entry.cost_cny:>10.6f} "
                f"{entry.cost_per_score:>9.4f}"
            )
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """Markdown 对比表（可写入报告 / 前端渲染）。"""
        lines = [
            f"# 批量跑分榜单（套件 `{self.suite_id}`）",
            "",
            f"- 项目：`{self.project_id}`",
            f"- 生成时间：{self.generated_at}",
            "",
            "| 标签 | 路由 | 版本 | 总分 | 可运行率 | 通过 | 成本(¥) | 分/元 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for entry in sorted(self.entries, key=lambda e: e.total_score, reverse=True):
            lines.append(
                f"| {entry.label} | {entry.route or '-'} | "
                f"{entry.version if entry.version is not None else '-'} | "
                f"{entry.total_score:.2f} | {entry.runnability:.2f} | "
                f"{'✅' if entry.passed else '❌'} | {entry.cost_cny:.6f} | "
                f"{entry.cost_per_score:.4f} |"
            )
        best = self.best_value()
        if best is not None:
            lines += [
                "",
                f"**性价比最优**：`{best.label}`（{best.total_score:.2f} 分 / "
                f"¥{best.cost_cny:.6f}）。",
            ]
        return "\n".join(lines) + "\n"


async def run_batch(
    *,
    harness: EvaluationHarness,
    suite_id: str,
    project_id: str,
    targets: list[BatchTarget],
    suites_dir: Any = None,
) -> BatchReport:
    """对一批目标逐个跑套件，汇总为性价比榜单。"""
    entries: list[BatchEntry] = []
    for target in targets:
        report = await harness.run_suite(
            suite_id, project_id=project_id, version_override=target.version, suites_dir=suites_dir
        )
        cost = target.cost_cny if target.cost_cny > 0 else report.cost_cny
        cost_per_score = round(cost / report.total_score, 6) if report.total_score > 0 else 0.0
        entries.append(
            BatchEntry(
                label=target.label,
                route=target.route,
                version=target.version,
                total_score=report.total_score,
                runnability=report.runnability,
                passed=report.passed,
                cost_cny=round(cost, 6),
                wall_time_ms=report.wall_time_ms,
                cost_per_score=cost_per_score,
            )
        )
    return BatchReport(suite_id=suite_id, project_id=project_id, entries=entries)
