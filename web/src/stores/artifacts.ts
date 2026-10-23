/**
 * 产物库 Store（Pinia setup 风格）：项目产物列表 / 产物详情（版本历史）/
 * 版本内容 / 版本 diff（ArtifactLibrary 与运行详情的产物面板共用）；
 * v1.2 增补：跨产物对比（基线 vs 合并结果，T4.3）。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as artifactsApi from '@/api/artifacts'
import type { Artifact, ArtifactContent, ArtifactDetail, ArtifactDiff } from '@/api/types'
import { messageOf } from './projects'

export const useArtifactsStore = defineStore('artifacts', () => {
  const items = ref<Artifact[]>([])
  const current = ref<ArtifactDetail | null>(null)
  const content = ref<ArtifactContent | null>(null)
  const diff = ref<ArtifactDiff | null>(null)
  /** 跨产物对比结果（基线 vs 基线⊕补丁）；与同产物 ``diff`` 分开，互不覆盖。 */
  const diffAcross = ref<ArtifactDiff | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 项目产物列表。 */
  async function loadProjectArtifacts(projectId: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      items.value = await artifactsApi.fetchProjectArtifacts(projectId)
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  /** 产物详情（含版本历史）。 */
  async function loadArtifact(artifactId: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      current.value = await artifactsApi.fetchArtifact(artifactId)
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  /** 版本内容（无 path 为文件清单；有 path 为单文件内容）。 */
  async function loadVersionContent(
    artifactId: string,
    version: number,
    path?: string,
  ): Promise<void> {
    loading.value = true
    error.value = null
    try {
      content.value = await artifactsApi.fetchVersionContent(artifactId, version, path)
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  /** 两个版本间的 unified diff。 */
  async function loadDiff(artifactId: string, v1: number, v2: number): Promise<void> {
    loading.value = true
    error.value = null
    try {
      diff.value = await artifactsApi.fetchArtifactDiff(artifactId, v1, v2)
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  function clearContent(): void {
    content.value = null
  }

  function clearDiff(): void {
    diff.value = null
  }

  /**
   * 跨产物对比（GET /projects/{id}/diff-across）：``base`` / ``target`` 形如
   * ``name`` 或 ``name@version``。返回体与 :class:`ArtifactDiff` 同构，
   * 因此 ``DiffView.vue`` 零改动即可渲染。
   */
  async function loadDiffAcross(projectId: string, base: string, target: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      diffAcross.value = await artifactsApi.fetchDiffAcross(projectId, base, target)
    } catch (e) {
      diffAcross.value = null
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  function clearDiffAcross(): void {
    diffAcross.value = null
  }

  function reset(): void {
    items.value = []
    current.value = null
    content.value = null
    diff.value = null
    diffAcross.value = null
    error.value = null
  }

  return {
    items,
    current,
    content,
    diff,
    diffAcross,
    loading,
    error,
    loadProjectArtifacts,
    loadArtifact,
    loadVersionContent,
    loadDiff,
    loadDiffAcross,
    clearContent,
    clearDiff,
    clearDiffAcross,
    reset,
  }
})
