/**
 * 审批 Store（Pinia setup 风格）：审批列表（审批中心 / 运行内嵌卡片）/
 * 审批详情（历史反馈链）/ 决策（approve / reject 带反馈回退）。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as approvalsApi from '@/api/approvals'
import type { Approval, ApprovalDecision, ApprovalDetail, ApprovalStatus } from '@/api/types'
import { messageOf } from './projects'

/** 列表过滤条件。 */
export interface ApprovalFilter {
  status: ApprovalStatus | ''
  runId?: string
}

export const useApprovalsStore = defineStore('approvals', () => {
  const items = ref<Approval[]>([])
  const current = ref<ApprovalDetail | null>(null)
  const filter = ref<ApprovalFilter>({ status: 'pending' })
  const loading = ref(false)
  const deciding = ref(false)
  const error = ref<string | null>(null)

  /** 待审批数量（导航角标；与列表 filter 解耦，独立刷新）。 */
  const pendingCount = ref(0)

  /** 审批列表（默认待审批；按 status / run_id 过滤）。 */
  async function loadApprovals(params: ApprovalFilter = filter.value): Promise<void> {
    filter.value = params
    loading.value = true
    error.value = null
    try {
      items.value = await approvalsApi.fetchApprovals({
        status: params.status || undefined,
        runId: params.runId,
      })
      if (params.status === 'pending' || params.status === '') {
        pendingCount.value = items.value.filter((a) => a.status === 'pending').length
      }
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  /** 刷新导航角标（不影响当前列表 filter；供 App 级轮询）。 */
  async function refreshPendingCount(): Promise<void> {
    try {
      const pending = await approvalsApi.fetchApprovals({ status: 'pending' })
      pendingCount.value = pending.length
    } catch {
      // 角标刷新失败静默（不打断当前页面）
    }
  }

  /** 审批详情（含 run 摘要与历史反馈链）。 */
  async function loadApproval(approvalId: string): Promise<ApprovalDetail | null> {
    loading.value = true
    error.value = null
    try {
      const detail = await approvalsApi.fetchApproval(approvalId)
      current.value = detail
      return detail
    } catch (e) {
      error.value = messageOf(e)
      return null
    } finally {
      loading.value = false
    }
  }

  /**
   * 审批决策。成功后同步列表项与 current；
   * 业务错误（1003 重复决策等）上抛，由调用方 ElMessage 提示。
   */
  async function decide(approvalId: string, payload: ApprovalDecision): Promise<Approval> {
    deciding.value = true
    error.value = null
    try {
      const updated = await approvalsApi.decideApproval(approvalId, payload)
      items.value = items.value.map((a) => (a.id === approvalId ? { ...a, ...updated } : a))
      if (current.value?.id === approvalId) {
        current.value = {
          ...current.value,
          ...updated,
          feedback_history: current.value.feedback_history,
        }
      }
      return updated
    } catch (e) {
      error.value = messageOf(e)
      throw e
    } finally {
      deciding.value = false
    }
  }

  function clearCurrent(): void {
    current.value = null
  }

  function reset(): void {
    items.value = []
    current.value = null
    filter.value = { status: 'pending' }
    pendingCount.value = 0
    error.value = null
  }

  return {
    items,
    current,
    filter,
    loading,
    deciding,
    error,
    pendingCount,
    loadApprovals,
    loadApproval,
    decide,
    refreshPendingCount,
    clearCurrent,
    reset,
  }
})
