/**
 * 运行 API（§6.1：POST /projects/{id}/runs、GET /runs/{id}、
 * POST /runs/{id}/actions、GET /runs/{id}/messages）。
 */

import { request, type PageData } from './client'
import type { Run, RunAction, RunCreatePayload, RunMessage } from './types'

/** 发起运行（202，引擎接管推进）。 */
export function createRun(projectId: string, payload: RunCreatePayload): Promise<string> {
  return request<{ run_id: string }>({
    method: 'post',
    url: `/projects/${projectId}/runs`,
    data: payload,
  }).then((data) => data.run_id)
}

/** 运行详情。 */
export function fetchRun(runId: string): Promise<Run> {
  return request<Run>({ method: 'get', url: `/runs/${runId}` })
}

/** 运行控制动作（pause / resume / cancel / retry-stage）；返回最新运行状态。 */
export function runAction(runId: string, action: RunAction): Promise<Run> {
  return request<{ run_id: string; action: string; status: string }>({
    method: 'post',
    url: `/runs/${runId}/actions`,
    data: { action },
  }).then(() => fetchRun(runId))
}

/** 角色消息流（信封 data 为 PageData：{items, total}）。 */
export function fetchMessages(runId: string, afterSeq = 0): Promise<RunMessage[]> {
  return request<PageData<RunMessage>>({
    method: 'get',
    url: `/runs/${runId}/messages`,
    params: { after_seq: afterSeq, limit: 200 },
  }).then((data) => data?.items ?? [])
}
