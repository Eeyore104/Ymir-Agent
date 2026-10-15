/**
 * 产物库 Store（Pinia setup 风格）：项目产物列表 / 产物详情（版本历史）/
 * 版本内容 / 版本 diff（ArtifactLibrary 与运行详情的产物面板共用）。
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

  function reset(): void {
    items.value = []
    current.value = null
    content.value = null
    diff.value = null
    error.value = null
  }

  return {
    items,
    current,
    content,
    diff,
    loading,
    error,
    loadProjectArtifacts,
    loadArtifact,
    loadVersionContent,
    loadDiff,
    clearContent,
    clearDiff,
    reset,
  }
})
