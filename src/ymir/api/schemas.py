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

from ymir.artifacts.exporter import ExportTarget
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
    """发起运行请求。

    v1.2（docs/09 §4.1.2 / §4.2.1）：``template_id`` 与 ``baseline_*`` 都是**可选**
    运行参数 —— 模板是 run 的运行参数而**不是**流水线拓扑（写进 ``stage.inputs``
    会被 ``sop/validator._check_inputs`` 直接否决）。两者缺省 ``None`` 时行为与
    v1.1 完全一致。
    """

    requirement: str = Field(..., min_length=1, description="原始需求文本")
    pipeline_id: str = Field(default="software-dev", description="流水线 ID")
    mode: RunMode = Field(default=RunMode.KEY_NODE, description="HITL 模式")
    context_refs: list[str] = Field(default_factory=list, description="附加上下文引用")
    template_id: str | None = Field(
        default=None, description="模板 id（不存在/非法 → 1001）；缺省 = 通用模式"
    )
    baseline_artifact_id: str | None = Field(
        default=None, description="基线产物 id（须属于本项目且 type=baseline）"
    )
    baseline_version: int | None = Field(
        default=None, ge=1, description="基线版本号；缺省 = 该产物当前版本"
    )


class BaselineUploadOut(BaseModel):
    """基线上传结果（v1.2，docs/09 §4.2.1）。"""

    artifact_id: str
    version: int
    file_count: int
    content_hash: str
    size_bytes: int


class BaselineMergeRequest(BaseModel):
    """「以合入结果为新基线」请求（v1.3.1 T5.3，docs/13 §7.3-8）。

    刻意**只收引用**（run / stage / 版本号），不接受任何文件正文：合入树由服务端按
    产物内自描述的溯源**确定性重建**，客户端无法参与构造（否则等于绕过校验上传）。

    ``stage_id`` / ``artifact_version`` 都可缺省：前者缺省 = "该 run 里产出了 ``code``
    的最后一次成功阶段"（不按角色名硬编码）；后者缺省 = 该阶段记录里的那一版。
    两者一起给，就是"重跑之后取历史版本"的场景。
    """

    run_id: str = Field(..., min_length=1, description="产生该增量产物的运行 id")
    stage_id: str | None = Field(
        default=None, description="阶段 id；缺省 = 该 run 中产出 code 的最后一次成功阶段"
    )
    artifact_version: int | None = Field(
        default=None, ge=1, description="产物版本号；缺省 = 该阶段记录里的版本"
    )


class BaselineMergeOut(BaselineUploadOut):
    """「以合入结果为新基线」响应（v1.3.1 §7.3-8）。

    ★ **刻意继承** :class:`BaselineUploadOut` 而不是给基类加字段：
    给基类加 ``reused`` 会让 ``POST /baselines`` 的响应 JSON 多出 ``"reused": false``
    —— 那就是「未配置也变了字节」（A9）。

    ``reused=True``：重建结果与当前基线最新版**内容相同**，未新建版本（幂等）。
    HTTP 状态仍是 201（与既有基线端点一致），语义只靠该字段区分。
    """

    reused: bool = False


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
    error_code: int | None = None
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
    """运行控制动作（§6.1 runs/{id}/actions）。

    白名单（由路由层校验，非法 -> 1001）：``pause`` / ``resume`` / ``cancel`` /
    ``retry-stage``（v1.0 四动作）+ ``accept-best`` / ``extend-repair``（v1.1 人工介入，
    §4.5.3）。字段类型放宽为 ``str``，以便给出「非法动作 + 合法动作清单」的可读报错
    （``Literal`` 只会返回 422 校验错误）。
    """

    action: str


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


# --- 产物库（T03，§3.6）---


class ArtifactOut(BaseModel):
    """产物（列表 / 详情公共字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    type: str
    current_version: int
    created_at: datetime


class ArtifactVersionOut(BaseModel):
    """产物版本元数据。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    artifact_id: str
    version: int
    run_id: str | None = None
    stage_id: str | None = None
    storage_path: str
    content_hash: str | None = None
    file_count: int
    size_bytes: int
    message: str | None = None
    parent_version_id: str | None = None
    created_by: str | None = None
    created_at: datetime


class ArtifactDetailOut(ArtifactOut):
    """产物详情（含版本列表）。"""

    versions: list[ArtifactVersionOut] = []


class ArtifactContentOut(BaseModel):
    """产物版本内容：无 ``path`` 返回文件清单；有 ``path`` 返回单文件内容。"""

    artifact_id: str
    version: int
    files: list[str] = []
    path: str | None = None
    content: str | None = None


class FileDiffOut(BaseModel):
    """单文件差异。"""

    path: str
    status: str
    diff: str
    additions: int
    deletions: int


class DiffStatsOut(BaseModel):
    """差异统计。"""

    files_changed: int
    additions: int
    deletions: int


class ArtifactDiffOut(BaseModel):
    """版本间 diff（unified，按文件分组）。"""

    artifact_id: str
    v1: int
    v2: int
    files: list[FileDiffOut]
    stats: DiffStatsOut


# --- 推送（v1.2 T3.4，docs/09 §4.3.1）---


class ExportCreate(BaseModel):
    """推送请求体（**绝不接受 token 字段**）。

    ``extra="forbid"`` 是安全底线：请求体里出现 ``token`` / ``pat`` 之类键一律
    400/1001 拒掉，避免凭证经请求体流入日志、回执表或异常信息（§4.3.2）。
    token 只从环境变量读（``settings.github_token_env``）。
    """

    model_config = ConfigDict(extra="forbid")

    target: ExportTarget = Field(description="推送目标（repo/branch/path_prefix…）")
    artifact_id: str = Field(min_length=1, description="要推送的产物 id")
    artifact_version: int | None = Field(default=None, ge=1, description="缺省 = 最新版本")
    tree: Literal["files", "merged"] = Field(
        default="merged", description="files=原产物 / merged=基线⊕补丁（推荐）"
    )
    dry_run: bool = Field(default=True, description="缺省预演（前端确认后才真推）")
    strict_secret_check: bool = Field(default=False)


class BlockedFileOut(BaseModel):
    """被拦/被跳过的文件（``path`` + ``reason``，**不含正文**）。"""

    path: str
    reason: str


class ExportReceiptOut(BaseModel):
    """推送回执（v1.2 T4.1）。"""

    id: str
    artifact_id: str
    artifact_version: int
    tree: str
    target_kind: str
    repo: str
    branch: str
    path_prefix: str = ""
    status: str
    commit_sha: str | None = None
    file_count: int = 0
    blocked_count: int = 0
    skipped_count: int = 0
    error_code: int | None = None
    error_class: str | None = None
    error_message: str | None = None
    idempotency_key: str = ""
    dry_run: bool = False
    #: dry-run 预演结果（"将推送 N 个 / 拦截 M 个 / 跳过 K 个"的 M、K 部分）
    blocked_files: list[BlockedFileOut] = Field(default_factory=list)
    skipped_files: list[BlockedFileOut] = Field(default_factory=list)
    created_at: datetime


# --- 模板（v1.2 T4.1，§4.1.1）---


class TemplateSummaryOut(BaseModel):
    """模板摘要（**只含路径清单，不含骨架正文**；坏模板只带 ``loading_error``）。"""

    id: str
    version: str = ""
    name: str = ""
    description: str = ""
    supports: list[str] = Field(default_factory=list)
    stack: dict[str, Any] = Field(default_factory=dict)
    layout: dict[str, Any] = Field(default_factory=dict)
    conventions: list[str] = Field(default_factory=list)
    reserved_paths: list[str] = Field(default_factory=list)
    seed_paths: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    #: 角色 -> 追加约束文本（骨架正文永远不在这里）
    prompting: dict[str, str] = Field(default_factory=dict)
    prompting_roles: list[str] = Field(default_factory=list)
    #: 加载失败原因（为 ``None`` 表示该模板可用）；坏模板不让列表整体 500
    loading_error: str | None = None


# --- 沙箱（T03，§3.5）---


class SandboxSessionOut(BaseModel):
    """沙箱会话视图。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    container_id: str | None = None
    image: str
    status: str
    created_at: datetime
    destroyed_at: datetime | None = None


# --- 审批（T04，§3.8）---


class ApprovalOut(BaseModel):
    """审批记录（列表 / 决策结果公共字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    stage_id: str
    gate_name: str
    artifact_refs: list[Any]
    status: str
    decision: str | None = None
    feedback: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime


class RunBriefOut(BaseModel):
    """审批卡片用的运行摘要（项目名 / 需求 / 模式 / 状态）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    project_name: str
    requirement: str
    mode: str
    status: str
    current_stage_id: str | None = None


class ApprovalFeedbackOut(BaseModel):
    """历史反馈链条目（历次驳回产生的 feedback 消息）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    seq: int
    from_role: str | None = None
    to_role: str | None = None
    summary: str | None = None
    created_at: datetime


class ApprovalListItem(ApprovalOut):
    """审批列表项（含 run 摘要 + 历史反馈链的聚合视图）。"""

    run: RunBriefOut | None = None
    feedback_history: list[ApprovalFeedbackOut] = []


class ApprovalDetail(ApprovalListItem):
    """审批详情（含 run 摘要与历史反馈链）。"""


class ApprovalDecisionPayload(BaseModel):
    """审批决策请求（§3.8）。"""

    decision: Literal["approve", "reject"]
    feedback: str | None = Field(
        default=None, max_length=5000, description="驳回反馈（注入角色上下文）"
    )
    decided_by: str = Field(default="human", max_length=128, description="决策人标识")


# --- 评测（T05，§3.7、§12）---


class EvaluationSummaryOut(BaseModel):
    """评测报告摘要（列表项，取自报告产物的 ``*.json``）。

    v1.1：``kind`` 区分两种一等报告产物 —— ``evaluation_report``（QA 叙事）与
    ``eval_verdict``（harness 确定性裁决，唯一裁决源）；缺省值保证旧响应可解析。
    """

    artifact_id: str
    version: int
    project_id: str
    suite_id: str
    total_score: float
    runnability: float
    passed: bool
    generated_at: str | None = None
    created_at: datetime
    kind: str = "evaluation_report"


class EvaluationRunRequest(BaseModel):
    """发起评测请求（对项目某产物版本跑套件）。"""

    suite_id: str = Field(default="default", description="评测套件 ID")
    artifact_name: str = Field(default="code", description="目标产物名")
    version: int | None = Field(default=None, description="产物版本（缺省 latest）")
    requirement: str | None = Field(default=None, description="原始需求（缺省取最新 run）")
    run_id: str | None = Field(default=None, description="关联 run（用于发 evaluation.completed）")
    save: bool = Field(default=True, description="是否将报告作为产物入库")
