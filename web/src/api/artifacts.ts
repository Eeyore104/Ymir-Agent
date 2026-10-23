/**
 * 产物库 API（§6.1、§3.6：项目产物列表 / 详情 / 版本内容 / 版本 diff）；
 * v1.2 增补：跨产物对比（基线 vs 合并结果）与基线上传（docs/09 §4.2）。
 */

import client, { request, type PageData } from './client'
import type {
  Artifact,
  ArtifactContent,
  ArtifactDetail,
  ArtifactDiff,
  BaselineUploadResult,
} from './types'

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

/** 整库导出（GET /projects/{id}/export -> zip blob；PM-02）。 */
export async function exportProjectZip(projectId: string): Promise<Blob> {
  // 直接走 client（responseType=blob）：zip 非信封响应，绕开 request 的 data 解包
  const response = await client.request<Blob>({
    method: 'get',
    url: `/projects/${projectId}/export`,
    responseType: 'blob',
  })
  return response.data
}

/**
 * 上传现有工程快照为基线（v1.2，docs/09 §4.2.1）。
 *
 * **必须用原始字节**：后端刻意不引入 ``python-multipart``，因此这里把 ``File``
 * 作为请求体直接 POST，并显式声明 ``Content-Type: application/zip``；
 * **禁止 FormData / multipart**。
 *
 * @param projectId 项目 id
 * @param file zip 文件（浏览器 ``File`` / ``Blob``）
 */
export function uploadBaseline(projectId: string, file: File | Blob): Promise<BaselineUploadResult> {
  return request<BaselineUploadResult>({
    method: 'post',
    url: `/projects/${projectId}/baselines`,
    data: file,
    headers: { 'Content-Type': 'application/zip' },
  })
}

/**
 * 跨产物对比（v1.2，docs/09 §4.2.4）：``base`` 与「基线 ⊕ 补丁」的差异。
 *
 * ``base`` / ``target`` 形如 ``name`` 或 ``name@version``；返回体复用
 * :class:`ArtifactDiff`，因此 ``DiffView.vue`` 零改动即可渲染。
 */
export function fetchDiffAcross(
  projectId: string,
  base = 'baseline',
  target = 'code',
): Promise<ArtifactDiff> {
  return request<ArtifactDiff>({
    method: 'get',
    url: `/projects/${projectId}/diff-across`,
    params: { base, target },
  })
}
