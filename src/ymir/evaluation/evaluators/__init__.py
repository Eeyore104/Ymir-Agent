"""四维评估器集合（架构设计 §12.1）与共享契约再导出。

维度与权重（冻结口径，勿改）：需求符合度 30% / 代码可运行率 30% /
规范符合度 20% / 架构合理性 20%。
"""

from __future__ import annotations

from collections.abc import Mapping

from ymir.evaluation.evaluators.architecture import ArchitectureEvaluator
from ymir.evaluation.evaluators.base import (
    ARCHITECTURE,
    CONVENTION,
    DEFAULT_WEIGHTS,
    DIMENSION_LABELS,
    DIMENSION_ORDER,
    REQUIREMENT,
    RUNNABILITY,
    EvalCheck,
    EvalContext,
    EvalScore,
    EvalTask,
    Evaluator,
    Evidence,
    Judge,
    JudgeOutcome,
    LLMJudge,
)
from ymir.evaluation.evaluators.convention import ConventionEvaluator
from ymir.evaluation.evaluators.requirement import RequirementConformanceEvaluator
from ymir.evaluation.evaluators.runnability import RunnabilityEvaluator


def default_evaluators(weights: Mapping[str, float] | None = None) -> list[Evaluator]:
    """构建默认四维评估器（权重取自套件，缺省用规范默认值）。"""
    effective = dict(DEFAULT_WEIGHTS)
    if weights:
        effective.update({key: float(value) for key, value in weights.items()})
    return [
        RequirementConformanceEvaluator(weight=effective[REQUIREMENT]),
        RunnabilityEvaluator(weight=effective[RUNNABILITY]),
        ConventionEvaluator(weight=effective[CONVENTION]),
        ArchitectureEvaluator(weight=effective[ARCHITECTURE]),
    ]


__all__ = [
    "ARCHITECTURE",
    "CONVENTION",
    "DEFAULT_WEIGHTS",
    "DIMENSION_LABELS",
    "DIMENSION_ORDER",
    "REQUIREMENT",
    "RUNNABILITY",
    "ArchitectureEvaluator",
    "ConventionEvaluator",
    "EvalCheck",
    "EvalContext",
    "EvalScore",
    "EvalTask",
    "Evaluator",
    "Evidence",
    "Judge",
    "JudgeOutcome",
    "LLMJudge",
    "RequirementConformanceEvaluator",
    "RunnabilityEvaluator",
    "default_evaluators",
]
