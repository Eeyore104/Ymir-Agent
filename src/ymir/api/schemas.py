"""请求/响应模型（Pydantic）。

响应信封遵循架构设计 §6.3：
- 成功：``{"code": 0, "data": ...}``
- 列表：``{"code": 0, "data": {"items": [...], "total": n}}``
- 错误：``{"code": <biz_code>, "message": "..."}``
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ymir.models.enums import RunMode


class ApiResponse[T](BaseModel):
    """统一成功响应信封。"""

    code: int = 0
    data: T | None = None


class PageData[T](BaseModel):
    """分页数据体。"""

    items: list[T]
    total: int


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    code: int
    message: str


# --- 项目 ---


class ProjectCreate(BaseModel):
    """创建项目请求。"""

    name: str = Field(..., min_length=1, max_length=255, description="项目名称")
    description: str | None = Field(default=None, max_length=2000, description="项目描述")


class ProjectOut(BaseModel):
    """项目响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    tenant_id: str
    created_at: datetime


class ProjectDetail(ProjectOut):
    """项目详情（含运行数概览）。"""

    run_count: int = 0


# --- 运行 ---


class RunCreate(BaseModel):
    """发起运行请求。"""

    requirement: str = Field(..., min_length=1, description="原始需求文本")
    pipeline_id: str = Field(default="software-dev", description="流水线 ID")
    mode: RunMode = Field(default=RunMode.KEY_NODE, description="HITL 模式")
    context_refs: list[str] = Field(default_factory=list, description="附加上下文引用")


class RunOut(BaseModel):
    """运行详情响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    pipeline_id: str
    mode: str
    status: str
    current_stage_id: str | None = None
    context: dict[str, Any]
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class RunCreated(BaseModel):
    """发起运行的结果（202）。"""

    run_id: str


# --- 健康检查 ---


class HealthData(BaseModel):
    """健康检查数据。"""

    status: str
    database: str
    version: str
    gateway: str = "unknown"


# --- 运行动作（T02）---


class RunActionPayload(BaseModel):
    """运行控制动作（§6.1 runs/{id}/actions）。"""

    action: Literal["pause", "resume", "cancel", "retry-stage"]


class RunActionOut(BaseModel):
    """动作执行结果（返回最新运行状态）。"""

    run_id: str
    action: str
    status: str


# --- 角色消息（T02）---


class MessageOut(BaseModel):
    """角色消息（消息流面板 / SSE message.created）。"""

    id: str
    run_id: str
    seq: int
    type: str
    from_role: str | None = None
    to_role: str | None = None
    content_refs: list[Any]
    summary: str | None = None
    payload: dict[str, Any]
    created_at: datetime


# --- 模型网关（T02，脱敏）---


class ModelInfo(BaseModel):
    """单个模型（脱敏视图）。"""

    name: str
    tier: str
    price_in: float
    price_out: float


class ProviderInfo(BaseModel):
    """provider 视图（不含密钥，仅密钥就绪状态）。"""

    name: str
    base_url: str
    enabled: bool
    key_ready: bool
    models: list[ModelInfo]


class ModelsOut(BaseModel):
    """网关配置视图（GET /models）。"""

    version: str
    providers: list[ProviderInfo]
    routing: dict[str, list[str]]
    budget: dict[str, float]


# --- 用量（T02）---


class UsageOut(BaseModel):
    """token / 成本统计（GET /usage）。"""

    scope: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_cny: float
