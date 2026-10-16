/**
 * 沙箱 API（§6.1、§3.5）：会话列表（可按 run 过滤）与详情。
 *
 * 会话创建是执行器内部行为，前端仅查询展示（设置页沙箱状态面板）。
 */

import { request, type PageData } from './client'
import type { SandboxSession } from './types'

/** 沙箱会话列表（可按 run_id 过滤）。 */
export function fetchSandboxes(runId?: string, limit = 100): Promise<SandboxSession[]> {
  return request<PageData<SandboxSession>>({
    method: 'get',
    url: '/sandboxes',
    params: { run_id: runId || undefined, limit },
  }).then((data) => data?.items ?? [])
}

/** 沙箱会话详情。 */
export function fetchSandbox(sessionId: string): Promise<SandboxSession> {
  return request<SandboxSession>({ method: 'get', url: `/sandboxes/${sessionId}` })
}
