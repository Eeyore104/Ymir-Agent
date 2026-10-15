/**
 * 系统 API（§6.1）：``GET /health``（根路径，无 /api/v1 前缀）、
 * ``GET /models``（脱敏网关视图）、``GET /usage``（token / 成本统计）。
 */

import { request } from './client'
import type { HealthData, ModelsOut, UsageOut } from './types'

/** 健康检查（/health 位于根路径，需覆盖 baseURL）。 */
export function fetchHealth(): Promise<HealthData> {
  return request<HealthData>({ method: 'get', url: '/health', baseURL: '/' })
}

/** 模型网关配置（脱敏：provider / 端点 / 密钥就绪状态 / 价目）。 */
export function fetchModels(): Promise<ModelsOut> {
  return request<ModelsOut>({ method: 'get', url: '/models' })
}

/** 用量统计（scope=global 时不传其它参数）。 */
export function fetchUsage(
  scope: 'global' | 'run' | 'project',
  params: { runId?: string; projectId?: string } = {},
): Promise<UsageOut> {
  return request<UsageOut>({
    method: 'get',
    url: '/usage',
    params: {
      scope,
      run_id: params.runId || undefined,
      project_id: params.projectId || undefined,
    },
  })
}
