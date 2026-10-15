"""评测体系（架构设计 §3.7 接口层 + §12 专项设计）。

一键评测出四维报告（需求符合度 / 可运行率 / 规范符合度 / 架构合理性），
加权总分 + 双门槛判定 + 逐维证据链；报告作为一等产物入库（``type=report``，
带版本、可 diff）；批量跑分输出「性价比榜单」（total_score vs cost）。

模块划分：

- :mod:`ymir.evaluation.evaluators`：四个维度评估器（可插拔）；
- :mod:`ymir.evaluation.aggregator`：加权聚合 + 双门槛判定；
- :mod:`ymir.evaluation.report`：``EvaluationReport`` 一等产物（序列化 / Markdown / 入库）；
- :mod:`ymir.evaluation.suite`：``EvalSuite``（``evals/suites/*.yaml``）加载与校验；
- :mod:`ymir.evaluation.harness`：单任务 / 套件编排（沙箱生命周期、四维并行调度）；
- :mod:`ymir.evaluation.runner`：批量跑分与性价比对比。
"""

from ymir.evaluation.aggregator import AggregateResult, aggregate, normalize_weights
from ymir.evaluation.evaluators import (
    ARCHITECTURE,
    CONVENTION,
    DEFAULT_WEIGHTS,
    DIMENSION_LABELS,
    DIMENSION_ORDER,
    REQUIREMENT,
    RUNNABILITY,
    ArchitectureEvaluator,
    ConventionEvaluator,
    EvalCheck,
    EvalContext,
    EvalScore,
    EvalTask,
    Evaluator,
    Evidence,
    Judge,
    JudgeOutcome,
    LLMJudge,
    RequirementConformanceEvaluator,
    RunnabilityEvaluator,
    default_evaluators,
)
from ymir.evaluation.harness import (
    BackendSandboxProvider,
    EvaluationHarness,
    SandboxProvider,
    build_eval_harness,
)
from ymir.evaluation.report import DimensionOut, EvaluationReport, EvidenceOut, TaskOut
from ymir.evaluation.runner import (
    BatchEntry,
    BatchReport,
    BatchTarget,
    run_batch,
)
from ymir.evaluation.suite import EvalSuite, Thresholds

__all__ = [
    "ARCHITECTURE",
    "CONVENTION",
    "DEFAULT_WEIGHTS",
    "DIMENSION_LABELS",
    "DIMENSION_ORDER",
    "REQUIREMENT",
    "RUNNABILITY",
    "AggregateResult",
    "ArchitectureEvaluator",
    "BackendSandboxProvider",
    "BatchEntry",
    "BatchReport",
    "BatchTarget",
    "ConventionEvaluator",
    "DimensionOut",
    "EvalCheck",
    "EvalContext",
    "EvalScore",
    "EvalSuite",
    "EvalTask",
    "EvaluationHarness",
    "EvaluationReport",
    "Evaluator",
    "Evidence",
    "EvidenceOut",
    "Judge",
    "JudgeOutcome",
    "LLMJudge",
    "RequirementConformanceEvaluator",
    "RunnabilityEvaluator",
    "SandboxProvider",
    "TaskOut",
    "Thresholds",
    "aggregate",
    "build_eval_harness",
    "default_evaluators",
    "normalize_weights",
    "run_batch",
]
