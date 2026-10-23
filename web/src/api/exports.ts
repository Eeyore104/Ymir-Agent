/**
 * 推送 API（v1.2 · T3.4，docs/09 §4.3.1）。
 *
 * - ``POST /projects/{id}/exports``：``dry_run=true`` 只预演（缺省），
 *   ``dry_run=false`` 才真推；
 * - ``GET  /projects/{id}/exports``：推送回执历史（时间倒序）。
 *
 * 安全：请求体**绝不含 token**（后端 ``extra="forbid"``，凭证只从服务端环境变量读）。
 */

import { request, type PageData } from './client'
import type { ExportCreatePayload, ExportReceipt } from './types'

/** 发起推送（202；失败回执由后端转业务异常，此处只处理成功路径）。 */
export function createExport(
  projectId: string,
  payload: ExportCreatePayload,
): Promise<ExportReceipt> {
  return request<ExportReceipt>({
    method: 'post',
    url: `/projects/${projectId}/exports`,
    data: payload,
  })
}

/** 项目推送回执历史。 */
export function fetchExports(projectId: string, limit = 20): Promise<ExportReceipt[]> {
  return request<PageData<ExportReceipt>>({
    method: 'get',
    url: `/projects/${projectId}/exports`,
    params: { limit, offset: 0 },
  }).then((data) => data?.items ?? [])
}
