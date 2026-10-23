/**
 * 领域类型定义（与后端 ``src/ymir/api/schemas.py`` 一一对应）。
 *
 * 时间字段为 ISO 8601 UTC 字符串（后端 TZDateTime 序列化产物）。
 */

/** 项目。 */
export interface Project {
  id: string
  name: string
  description: string | null
  tenant_id: string
  created_at: string
}

/** 项目详情（含运行数）。 */
export interface ProjectDetail extends Project {
  run_count: number
}

/** HITL 运行模式（docs/02 §3.8）。 */
export type RunMode = 'key_node' | 'batch' | 'debug'

/** 运行状态（docs/02 §3.4 状态机）。 */
export type RunStatus =
  | 'pending'
  | 'running'
  | 'waiting_approval'
  | 'paused'
  | 'completed'
  | 'failed'

/** 一次流水线运行。 */
export interface Run {
  id: string
  project_id: string
  pipeline_id: string
  mode: RunMode
  status: RunStatus
  current_stage_id: string | null
  context: Record<string, unknown>
  error: string | null
  /** 失败/暂停业务错误码（§6.3：1004 预算 / 1005 沙箱 / 1006 模型…；PM-10）。 */
  error_code?: number | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

/** 发起运行请求。 */
export interface RunCreatePayload {
  requirement: string
  pipeline_id?: string
  mode?: RunMode
  context_refs?: string[]
  /**
   * 模板 id（v1.2，docs/09 §4.1.2）：模板是**运行参数**而非流水线拓扑，
   * 缺省 ``null`` = 通用模式（行为与 v1.1 一致）。
   */
  template_id?: string | null
  /** 基线产物 id（须属于本项目且 ``type=baseline``）；缺省 ``null`` = 从零生成。 */
  baseline_artifact_id?: string | null
  /** 基线版本号；缺省 ``null`` = 该基线产物的当前版本。 */
  baseline_version?: number | null
}

/** 运行控制动作。 */
export type RunAction = 'pause' | 'resume' | 'cancel' | 'retry-stage'

/** 角色消息（消息流面板数据源）。 */
export interface RunMessage {
  id: string
  run_id: string
  seq: number
  type: string
  from_role: string | null
  to_role: string | null
  content_refs: unknown[]
  summary: string | null
  payload: Record<string, unknown>
  created_at: string
}

/** 产物。 */
export interface Artifact {
  id: string
  project_id: string
  name: string
  type: string
  current_version: number
  created_at: string
}

/** 产物版本元数据。 */
export interface ArtifactVersion {
  id: string
  artifact_id: string
  version: number
  run_id: string | null
  stage_id: string | null
  storage_path: string
  content_hash: string | null
  file_count: number
  size_bytes: number
  message: string | null
  parent_version_id: string | null
  created_by: string | null
  created_at: string
}

/** 产物详情（含版本历史）。 */
export interface ArtifactDetail extends Artifact {
  versions: ArtifactVersion[]
}

/** 产物版本内容（无 path 为文件清单，有 path 为单文件内容）。 */
export interface ArtifactContent {
  artifact_id: string
  version: number
  files?: string[]
  path?: string | null
  content?: string | null
}

/** 单文件 diff。 */
export interface FileDiff {
  path: string
  status: string
  diff: string
  additions: number
  deletions: number
}

/** 版本间 diff。 */
export interface ArtifactDiff {
  artifact_id: string
  v1: number
  v2: number
  files: FileDiff[]
  stats: { files_changed: number; additions: number; deletions: number }
}

/** 审批状态。 */
export type ApprovalStatus = 'pending' | 'approved' | 'rejected'

/** 审批卡片用的 run 摘要。 */
export interface RunBrief {
  id: string
  project_id: string
  project_name: string
  requirement: string
  mode: string
  status: string
  current_stage_id: string | null
}

/** 审批记录（列表项，含 run 摘要 + 历史反馈链）。 */
export interface Approval {
  id: string
  run_id: string
  stage_id: string
  gate_name: string
  artifact_refs: unknown[]
  status: ApprovalStatus
  decision: string | null
  feedback: string | null
  decided_by: string | null
  decided_at: string | null
  created_at: string
  run?: RunBrief
  /** 历史反馈链（列表接口已内联；PM-12）。 */
  feedback_history?: FeedbackItem[]
}

/** 历史反馈链条目。 */
export interface FeedbackItem {
  id: string
  seq: number
  from_role: string | null
  to_role: string | null
  summary: string | null
  created_at: string
}

/** 审批详情（含历史反馈链）。 */
export interface ApprovalDetail extends Approval {
  feedback_history: FeedbackItem[]
}

/** 审批决策请求。 */
export interface ApprovalDecision {
  decision: 'approve' | 'reject'
  feedback?: string
  decided_by?: string
}

/** 健康检查数据。 */
export interface HealthData {
  status: string
  database: string
  version: string
  gateway: string
}

/** 模型价目。 */
export interface ModelInfo {
  name: string
  tier: string
  price_in: number
  price_out: number
}

/** provider 视图（脱敏）。 */
export interface ProviderInfo {
  name: string
  base_url: string
  enabled: boolean
  key_ready: boolean
  models: ModelInfo[]
}

/** 网关配置视图。 */
export interface ModelsOut {
  version: string
  providers: ProviderInfo[]
  routing: Record<string, string[]>
  budget: Record<string, number>
}

/** 用量统计。 */
export interface UsageOut {
  scope: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_cny: number
}

/** 产物引用（审批卡片待审内容元素）。 */
export interface ArtifactRef {
  artifact_id?: string
  name?: string
  type?: string
  version?: number
}

/** 评测报告摘要（列表项，出自后端 evaluation_report.json）。 */
export interface EvaluationSummary {
  artifact_id: string
  version: number
  project_id: string
  suite_id: string
  total_score: number
  runnability: number
  passed: boolean
  generated_at: string | null
  created_at: string
  /**
   * 报告类型（v1.1，T5.2）：``evaluation_report``（QA 叙事）或 ``eval_verdict``
   * （harness 确定性裁决，唯一裁决源）。旧后端不返回时缺省按 ``evaluation_report``。
   */
  kind?: string
}

/** 单条评分证据（可定位）。 */
export interface EvalEvidence {
  kind: string
  ref: string
  detail: string
}

/** 单维评分（0-100，附证据链）。 */
export interface EvalDimension {
  name: string
  label: string
  score: number
  weight: number
  rationale: string
  evidence: EvalEvidence[]
  cost_cny: number
  tokens: number
  /** 该维度是否降级（评估器异常 / 沙箱不可用，分数不可信；QA-10）。 */
  degraded?: boolean
}

/** 单任务评测结果。 */
export interface EvalTask {
  id: string
  requirement: string
  artifact_ref: Record<string, unknown>
  dimensions: EvalDimension[]
  total_score: number
  runnability: number
  passed: boolean
  thresholds: Record<string, number>
  wall_time_ms: number
  cost_cny: number
  tokens: number
}

/** 评测报告（一等产物，机读 JSON）。 */
export interface EvaluationReport {
  suite_id: string
  suite_version: string
  project_id: string | null
  generated_at: string
  weights: Record<string, number>
  thresholds: Record<string, number>
  tasks: EvalTask[]
  total_score: number
  runnability: number
  passed: boolean
  cost_cny: number
  total_tokens: number
  wall_time_ms: number
  // --- 追溯字段（v1.1，R6）：哪个 run、哪个产物版本、第几轮修复、裁决来源 ---
  run_id?: string | null
  artifact_version?: number | null
  repair_round?: number | null
  verdict_source?: 'harness' | 'llm_qa'
}

/** 沙箱会话（GET /sandboxes；§3.5）。 */
export interface SandboxSession {
  id: string
  run_id: string
  container_id: string | null
  image: string
  status: string
  created_at: string
  destroyed_at: string | null
}

/** 模板摘要（GET /templates、GET /templates/{id}）。 */
export interface TemplateSummary {
  id: string
  version: string
  name: string
  description: string
  supports: string[]
  /** stack 原样透传：``dependencies`` 即依赖白名单。 */
  stack: Record<string, unknown>
  /** layout 原样透传：``required_dirs`` 即目录规范。 */
  layout: Record<string, unknown>
  conventions: string[]
  /** 保留路径：逐字节要求精确，模型不得改动。 */
  reserved_paths: string[]
  /** 种子路径：允许整文件重写（须输出完整版）。 */
  seed_paths: string[]
  checks: Array<Record<string, unknown>>
  /** 角色 -> 追加约束文本（**骨架正文永远不在这里**）。 */
  prompting: Record<string, string>
  prompting_roles: string[]
  /** 加载失败原因（``null`` 表示模板可用；非空表示该模板不可选）。 */
  loading_error: string | null
}

/** 基线上传结果（POST /projects/{id}/baselines，201）。 */
export interface BaselineUploadResult {
  artifact_id: string
  version: number
  file_count: number
  content_hash: string
  size_bytes: number
}

/** 推送目标（对应后端 ``ExportTarget``，``extra="forbid"``：多一个键即 1001）。 */
export interface ExportTargetPayload {
  kind?: 'github'
  /** ``owner/name``。 */
  repo: string
  /** ``null`` -> 后端生成 ``ymir/<artifact>-v<n>``。 */
  branch?: string | null
  /** monorepo 子目录（空 = 根）。 */
  path_prefix?: string
  create_repo?: boolean
  private?: boolean
  commit_message?: string | null
  /** 显式允许推 main/master（缺省 false，后端拒绝）。 */
  allow_protected_branch?: boolean
  strict_secret_check?: boolean
}

/**
 * 推送请求体（v1.2 T3.4）。
 *
 * 后端 ``ExportCreate`` 为 ``extra="forbid"``：**绝不接受 token/pat 字段**，
 * 凭证只从服务端环境变量读取。
 */
export interface ExportCreatePayload {
  target: ExportTargetPayload
  artifact_id: string
  /** 缺省 ``null`` = 最新版本。 */
  artifact_version?: number | null
  /** ``files`` = 原产物 / ``merged`` = 基线⊕补丁（推荐）。 */
  tree?: 'files' | 'merged'
  /** 缺省预演（前端确认后才真推）。 */
  dry_run?: boolean
  strict_secret_check?: boolean
}

/** 被拦截 / 被跳过的文件（``path`` + ``reason``，**不含正文**）。 */
export interface BlockedFile {
  path: string
  reason: string
}

/** 推送回执（POST/GET /projects/{id}/exports）。 */
export interface ExportReceipt {
  id: string
  artifact_id: string
  artifact_version: number
  tree: string
  target_kind: string
  repo: string
  branch: string
  path_prefix: string
  status: string
  /** 真推成功时的提交 sha（预演为 ``null``）。 */
  commit_sha: string | null
  file_count: number
  blocked_count: number
  skipped_count: number
  error_code: number | null
  error_class: string | null
  error_message: string | null
  idempotency_key: string
  dry_run: boolean
  blocked_files: BlockedFile[]
  skipped_files: BlockedFile[]
  created_at: string
}
