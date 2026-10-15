/**
 * 前端常量：流水线阶段元数据与枚举中文标签。
 *
 * 阶段定义与后端 ``config/pipelines/software-dev.yaml`` 保持一致
 * （docs/02 §3.2：需求 -> PRD -> 架构 -> 代码 -> 评测，三角色人工关卡）。
 */

import type { ApprovalStatus, RunMode, RunStatus } from '@/api/types'

/** 默认流水线 ID。 */
export const PIPELINE_ID = 'software-dev'

/** 阶段元数据（StageStepper / 运行详情的阶段看板数据源）。 */
export interface StageMeta {
  id: string
  name: string
  role: string
  /** 人工关卡名（null 表示无阻断关卡）。 */
  gateName: string | null
}

/** 四角色流水线阶段。 */
export const STAGE_META: readonly StageMeta[] = [
  { id: 'prd', name: '需求 → PRD', role: 'pm', gateName: 'PRD 确认' },
  { id: 'architecture', name: 'PRD → 架构', role: 'architect', gateName: '设计评审' },
  { id: 'coding', name: '架构 → 代码', role: 'engineer', gateName: '代码合并前评审' },
  { id: 'evaluation', name: '代码 → 评测', role: 'qa', gateName: null },
] as const

/** 阶段运行态（StageStepper 展示）。 */
export type StageState = 'pending' | 'running' | 'waiting' | 'completed' | 'failed'

/** 角色 ID -> 中文名。 */
export const ROLE_LABELS: Record<string, string> = {
  pm: '产品经理',
  architect: '架构师',
  engineer: '工程师',
  qa: 'QA',
  human: '人工审批',
  batch: '批量模式',
}

/** HITL 模式标签与说明（§3.8 三模式）。 */
export const RUN_MODE_LABELS: Record<RunMode, string> = {
  key_node: '关键节点审批',
  batch: '批量自动通过',
  debug: '全步骤调试',
}

export const RUN_MODE_DESCRIPTIONS: Record<RunMode, string> = {
  key_node: '每个人工关卡暂停等待审批，可驳回回退',
  batch: '人工关卡自动通过（保留审计轨迹），不阻断',
  debug: '每个阶段完成后都暂停等待审批',
}

/** 运行状态标签。 */
export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  pending: '等待中',
  running: '运行中',
  waiting_approval: '等待审批',
  paused: '已暂停',
  completed: '已完成',
  failed: '失败',
}

/** 审批状态标签。 */
export const APPROVAL_STATUS_LABELS: Record<ApprovalStatus, string> = {
  pending: '待审批',
  approved: '已通过',
  rejected: '已驳回',
}

/** 运行状态 -> Element Plus 标签类型。 */
export const RUN_STATUS_TAG_TYPES: Record<RunStatus, 'info' | 'success' | 'warning' | 'danger'> = {
  pending: 'info',
  running: 'warning',
  waiting_approval: 'warning',
  paused: 'info',
  completed: 'success',
  failed: 'danger',
}

/** 审批状态 -> Element Plus 标签类型。 */
export const APPROVAL_STATUS_TAG_TYPES: Record<ApprovalStatus, 'warning' | 'success' | 'danger'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'danger',
}
