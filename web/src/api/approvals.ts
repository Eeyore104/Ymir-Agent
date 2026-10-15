/**
 * 审批 API（§6.1、§3.8：列表 / 详情 / 决策）。
 */

import { request, type PageData } from './client'
import type { Approval, ApprovalDecision, ApprovalDetail, ApprovalStatus } from './types'

/** 审批列表（审批中心；按 status / run_id 过滤）。 */
export function fetchApprovals(
  params: { status?: ApprovalStatus | ''; runId?: string } = {},
): Promise<Approval[]> {
  return request<PageData<Approval>>({
    method: 'get',
    url: '/approvals',
    params: {
      status: params.status || undefined,
      run_id: params.runId || undefined,
      limit: 200,
    },
  }).then((data) => data?.items ?? [])
}

/** 审批详情（含 run 摘要与历史反馈链）。 */
export function fetchApproval(approvalId: string): Promise<ApprovalDetail> {
  return request<ApprovalDetail>({ method: 'get', url: `/approvals/${approvalId}` })
}

/** 审批决策（approve / reject；驳回带反馈回退重跑）。 */
export function decideApproval(
  approvalId: string,
  payload: ApprovalDecision,
): Promise<Approval> {
  return request<Approval>({
    method: 'post',
    url: `/approvals/${approvalId}/decision`,
    data: payload,
  })
}
