"""SOP 流水线定义引擎（T02）：models / loader / validator。

v1.1（docs/07 §4.3.1）追加声明式回边的包级导出：``RepairSpec`` 族与事件常量，
供编排层 / 脚本按包级接口消费（无需依赖 ``ymir.sop.models`` 的内部路径）。
"""

from ymir.sop.loader import DEFAULT_PIPELINES_DIR, load_pipeline, sync_pipeline
from ymir.sop.models import (
    END_TOKEN,
    ENTRY_INPUT,
    MAIN_EVENT,
    RESERVED_EVENTS,
    GateSpec,
    PipelineDefinition,
    RepairContextSpec,
    RepairEvalSpec,
    RepairSpec,
    StageSpec,
    TransitionSpec,
)
from ymir.sop.validator import SopDefinitionError, SopValidator, ValidationIssue

__all__ = [
    "DEFAULT_PIPELINES_DIR",
    "END_TOKEN",
    "ENTRY_INPUT",
    "MAIN_EVENT",
    "RESERVED_EVENTS",
    "GateSpec",
    "PipelineDefinition",
    "RepairContextSpec",
    "RepairEvalSpec",
    "RepairSpec",
    "SopDefinitionError",
    "SopValidator",
    "StageSpec",
    "TransitionSpec",
    "ValidationIssue",
    "load_pipeline",
    "sync_pipeline",
]
