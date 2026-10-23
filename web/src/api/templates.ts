/**
 * 模板库 API（v1.2 · T4.1，docs/09 §4.1.2 / §4.4.3）。
 *
 * 纪律（与后端一致）：
 * - ``GET /templates``：坏模板**不让列表整体失败** —— 该条目 ``loading_error`` 非空，
 *   前端据此置灰不可选；
 * - ``GET /templates/{id}``：**只回传路径清单**（``reserved_paths`` / ``seed_paths``）
 *   与 stack / layout / checks，绝不含骨架正文。
 */

import { request, type PageData } from './client'
import type { TemplateSummary } from './types'

/** 模板列表（信封 data 为 PageData：{items, total}）。 */
export function fetchTemplates(): Promise<TemplateSummary[]> {
  return request<PageData<TemplateSummary>>({ method: 'get', url: '/templates' }).then(
    (data) => data?.items ?? [],
  )
}

/** 单个模板详情（不存在 1002 / 定义非法 1001）。 */
export function fetchTemplate(templateId: string): Promise<TemplateSummary> {
  return request<TemplateSummary>({ method: 'get', url: `/templates/${templateId}` })
}
