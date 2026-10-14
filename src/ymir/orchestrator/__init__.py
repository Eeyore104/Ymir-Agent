"""编排内核（T02）：状态机引擎 / stub 执行器 / 迁移表 / 启动恢复。"""

from ymir.orchestrator.engine import ApprovalHook, OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor, StageOutcome
from ymir.orchestrator.recovery import recover_runs
from ymir.orchestrator.transitions import (
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    STAGE_TRANSITIONS,
    can_run_transition,
    can_stage_transition,
    ensure_run_transition,
    ensure_stage_transition,
)

__all__ = [
    "RUN_TERMINAL",
    "RUN_TRANSITIONS",
    "STAGE_TRANSITIONS",
    "ApprovalHook",
    "OrchestrationEngine",
    "StageExecutor",
    "StageOutcome",
    "can_run_transition",
    "can_stage_transition",
    "ensure_run_transition",
    "ensure_stage_transition",
    "recover_runs",
]
