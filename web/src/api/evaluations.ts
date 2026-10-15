/**
 * 评测 API（§3.7、§12、§6.1）：
 * - ``GET /evaluations``：全局评测报告列表（跨项目）；
 * - ``GET /projects/{id}/evaluations``：项目评测报告列表；
 * - ``POST /projects/{id}/evaluations``：对项目产物发起评测（返回完整报告）。
 *
 * 报告正文（四维分 + 证据链）以 ``evaluation_report.json`` 产物形态存储，
 * 经产物内容接口（``fetchVersionContent``）读取。
 */

import { request, type PageData } from './client'
import type { EvaluationReport, EvaluationSummary } from './types'

/** 全局评测报告列表（最新在前）。 */
export function fetchEvaluations(): Promise<EvaluationSummary[]> {
  return request<PageData<EvaluationSummary>>({ method: 'get', url: '/evaluations' }).then(
    (data) => data?.items ?? [],
  )
}

/** 项目评测报告列表。 */
export function fetchProjectEvaluations(projectId: string): Promise<EvaluationSummary[]> {
  return request<PageData<EvaluationSummary>>({
    method: 'get',
    url: `/projects/${projectId}/evaluations`,
  }).then((data) => data?.items ?? [])
}

/** 发起评测（对项目某产物版本跑套件）。 */
export function runEvaluation(
  projectId: string,
  payload: { suite_id?: string; artifact_name?: string; version?: number | null; save?: boolean },
): Promise<EvaluationReport> {
  return request<EvaluationReport>({
    method: 'post',
    url: `/projects/${projectId}/evaluations`,
    data: payload,
  })
}
