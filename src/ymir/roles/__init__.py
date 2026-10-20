"""角色运行时（架构设计 §3.1）：定义 / 注册 / 提示词 / 输出契约 / 执行。"""

from ymir.roles.definition import RetrySpec, RoleDefinition
from ymir.roles.io import RepairContext, StageInput, StageOutput, UpstreamArtifact
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.runtime import RoleOutputError, RoleRuntime, extract_json
from ymir.roles.schemas import (
    OUTPUT_SCHEMAS,
    CodeBundle,
    DesignDocument,
    EvalSummary,
    PRDDocument,
    get_output_schema,
)

__all__ = [
    "OUTPUT_SCHEMAS",
    "CodeBundle",
    "DesignDocument",
    "EvalSummary",
    "PRDDocument",
    "PromptRenderer",
    "RepairContext",
    "RetrySpec",
    "RoleDefinition",
    "RoleOutputError",
    "RoleRegistry",
    "RoleRuntime",
    "StageInput",
    "StageOutput",
    "UpstreamArtifact",
    "extract_json",
    "get_output_schema",
]
