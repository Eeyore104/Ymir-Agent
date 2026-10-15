/**
 * 产物库 API（§6.1、§3.6：项目产物列表 / 详情 / 版本内容 / 版本 diff）。
 */

import { request, type PageData } from './client'
import type { Artifact, ArtifactContent, ArtifactDetail, ArtifactDiff } from './types'

/** 项目产物列表（信封 data 为 PageData：{items, total}）。 */
export function fetchProjectArtifacts(projectId: string): Promise<Artifact[]> {
  return request<PageData<Artifact>>({ method: 'get', url: `/projects/${projectId}/artifacts` }).then(
    (data) => data?.items ?? [],
  )
}

/** 产物详情（含版本历史）。 */
export function fetchArtifact(artifactId: string): Promise<ArtifactDetail> {
  return request<ArtifactDetail>({ method: 'get', url: `/artifacts/${artifactId}` })
}

/** 版本内容：无 path 返回文件清单，有 path 返回单文件内容。 */
export function fetchVersionContent(
  artifactId: string,
  version: number,
  path?: string,
): Promise<ArtifactContent> {
  return request<ArtifactContent>({
    method: 'get',
    url: `/artifacts/${artifactId}/versions/${version}/content`,
    params: path ? { path } : undefined,
  })
}

/** 两个版本间的 unified diff（按文件分组）。 */
export function fetchArtifactDiff(
  artifactId: string,
  v1: number,
  v2: number,
): Promise<ArtifactDiff> {
  return request<ArtifactDiff>({
    method: 'get',
    url: `/artifacts/${artifactId}/diff`,
    params: { v1, v2 },
  })
}
