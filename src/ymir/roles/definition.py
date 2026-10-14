"""角色定义模型（架构设计 §3.1）：``config/roles/<id>.yaml`` 的结构化表示。

定义「规划/执行分级 + 能力集合 + 提示词模板 + 输入输出契约」，
放入 ``config/roles/`` 即注册（E4 扩展点），SOP 用 ``role_id`` 引用。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ymir.models.enums import ModelTier


class RetrySpec(BaseModel):
    """输出校验重试（解析失败 / schema 不过时，携带错误信息重问模型）。"""

    max_attempts: int = Field(default=2, ge=1, le=5)


class RoleDefinition(BaseModel):
    """一个 Agent 角色（运行时由 StageExecutor 调度）。"""

    id: str
    name: str
    description: str = ""
    tier: ModelTier = ModelTier.EXECUTOR  # planner=规划模型 / executor=执行模型（§3.9 分级路由）
    system_prompt: str = Field(description="相对 config/roles 的 Jinja2 模板路径")
    capabilities: list[str] = Field(default_factory=list, description="工具白名单")
    inputs: list[str] = Field(default_factory=list, description="上游产物类型")
    outputs: list[str] = Field(default_factory=list, description="产出产物类型")
    output_schema: str = Field(description="输出契约名（roles.schemas 注册表键）")
    retry: RetrySpec = Field(default_factory=RetrySpec)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=256, le=65536)
