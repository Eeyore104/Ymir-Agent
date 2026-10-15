/**
 * 项目 API（§6.1：POST /projects、GET /projects、GET /projects/{id}）。
 */

import { request, type PageData } from './client'
import type { Project, ProjectDetail } from './types'

/** 创建项目。 */
export function createProject(name: string, description?: string): Promise<Project> {
  return request<Project>({
    method: 'post',
    url: '/projects',
    data: { name, description: description ?? null },
  })
}

/** 项目列表（信封 data 为 PageData：{items, total}）。 */
export function fetchProjects(limit = 50, offset = 0): Promise<Project[]> {
  return request<PageData<Project>>({ method: 'get', url: '/projects', params: { limit, offset } }).then(
    (data) => data?.items ?? [],
  )
}

/** 项目详情（含 run_count）。 */
export function fetchProject(projectId: string): Promise<ProjectDetail> {
  return request<ProjectDetail>({ method: 'get', url: `/projects/${projectId}` })
}
