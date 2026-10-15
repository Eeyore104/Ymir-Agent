"""加权聚合与双门槛判定（架构设计 §12.3）。

- ``total_score`` = Σ(dimension_score × weight)（权重先归一化，防配置漏项）；
- ``passed`` = ``total_score ≥ thresholds.total`` **且**
  ``runnability ≥ thresholds.runnability``（双门槛，防「分高但跑不起来」）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ymir.evaluation.evaluators.base import (
    DIMENSION_ORDER,
    RUNNABILITY,
    EvalScore,
)
from ymir.evaluation.suite import Thresholds


@dataclass(frozen=True)
class AggregateResult:
    """加权聚合结果。"""

    total_score: float
    runnability: float
    passed: bool
    weights: dict[str, float]


def normalize_weights(weights: Mapping[str, float] | None = None) -> dict[str, float]:
    """归一化权重（仅保留已知维度，和归一为 1；全零时回退等权）。"""
    from ymir.evaluation.evaluators.base import DEFAULT_WEIGHTS

    source = dict(DEFAULT_WEIGHTS)
    if weights:
        source.update(
            {key: float(value) for key, value in weights.items() if key in DIMENSION_ORDER}
        )
    positives = {key: max(0.0, source.get(key, 0.0)) for key in DIMENSION_ORDER}
    total = sum(positives.values())
    if total <= 0:
        equal = 1.0 / len(DIMENSION_ORDER)
        return dict.fromkeys(DIMENSION_ORDER, equal)
    return {key: value / total for key, value in positives.items()}


def aggregate(
    scores: Sequence[EvalScore],
    *,
    weights: Mapping[str, float] | None = None,
    thresholds: Thresholds | None = None,
) -> AggregateResult:
    """加权总分 + 双门槛判定。

    ``weights`` 省略时取各 ``EvalScore.weight``；``thresholds`` 省略时用默认
    （total 75 / runnability 60）。
    """
    limits = thresholds or Thresholds()
    if weights is None:
        weights = {score.name: score.weight for score in scores}
    normalized = normalize_weights(weights)

    by_name = {score.name: score.score for score in scores}
    total_score = sum(
        by_name.get(name, 0.0) * normalized.get(name, 0.0) for name in DIMENSION_ORDER
    )
    runnability = by_name.get(RUNNABILITY, 0.0)
    passed = total_score >= limits.total and runnability >= limits.runnability
    return AggregateResult(
        total_score=round(total_score, 2),
        runnability=round(runnability, 2),
        passed=passed,
        weights=normalized,
    )
