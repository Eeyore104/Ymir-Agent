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

v1.1（docs/07 §4.1）：任务 ↔ 产物绑定（``EvalSuite.binding_mode``）与「未执行」
语义（``TaskOut.status`` / ``skip_reason``），基准套件的跳过原因常量一并导出供
runner / 基线表复用。
"""

from ymir.evaluation.aggregator import AggregateResult, aggregate, normalize_weights
from ymir.evaluation.baseline_probe import (
    BASELINE_HEALTHY,
    BASELINE_SKIPPED,
    BASELINE_UNHEALTHY,
    BaselineCheck,
    BaselineProbe,
    BaselineProbeCache,
    baseline_cache_key,
    probe_baseline,
)
from ymir.evaluation.evaluators import (
    ARCHITECTURE,
    CONVENTION,
    DEFAULT_WEIGHTS,
    DIMENSION_LABELS,
    DIMENSION_ORDER,
    REQUIREMENT,
    RUNNABILITY,
    ArchitectureEvaluator,
    CheckRun,
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
    run_checks,
)
from ymir.evaluation.harness import (
    BackendSandboxProvider,
    EvaluationHarness,
    SandboxProvider,
    build_eval_harness,
)
from ymir.evaluation.report import (
    SKIP_BUDGET,
    SKIP_MISSING_ARTIFACT,
    SKIP_NOT_RUN,
    SKIP_UNBOUND_TASK,
    STATUS_EVALUATED,
    STATUS_SKIPPED,
    BaselineCheckOut,
    BaselineProbeOut,
    DimensionOut,
    EvaluationReport,
    EvidenceOut,
    SkippedCheckOut,
    TaskOut,
)
from ymir.evaluation.run_task import (
    build_task_for_run,
    evaluate_run,
    resolve_suite_id,
)
from ymir.evaluation.runner import (
    BatchEntry,
    BatchReport,
    BatchTarget,
    run_batch,
)
from ymir.evaluation.suite import (
    BINDING_MODE_PER_TASK,
    BINDING_MODE_SINGLE,
    BindingMode,
    EvalSuite,
    Thresholds,
)

__all__ = [
    "ARCHITECTURE",
    "BASELINE_HEALTHY",
    "BASELINE_SKIPPED",
    "BASELINE_UNHEALTHY",
    "BINDING_MODE_PER_TASK",
    "BINDING_MODE_SINGLE",
    "CONVENTION",
    "DEFAULT_WEIGHTS",
    "DIMENSION_LABELS",
    "DIMENSION_ORDER",
    "REQUIREMENT",
    "RUNNABILITY",
    "SKIP_BUDGET",
    "SKIP_MISSING_ARTIFACT",
    "SKIP_NOT_RUN",
    "SKIP_UNBOUND_TASK",
    "STATUS_EVALUATED",
    "STATUS_SKIPPED",
    "AggregateResult",
    "ArchitectureEvaluator",
    "BackendSandboxProvider",
    "BaselineCheck",
    "BaselineCheckOut",
    "BaselineProbe",
    "BaselineProbeCache",
    "BaselineProbeOut",
    "BatchEntry",
    "BatchReport",
    "BatchTarget",
    "BindingMode",
    "CheckRun",
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
    "SkippedCheckOut",
    "TaskOut",
    "Thresholds",
    "aggregate",
    "baseline_cache_key",
    "build_eval_harness",
    "build_task_for_run",
    "default_evaluators",
    "evaluate_run",
    "normalize_weights",
    "probe_baseline",
    "resolve_suite_id",
    "run_batch",
    "run_checks",
]
