/**
 * 项目 Store（Pinia setup 风格）：项目列表 / 当前项目 / 创建项目。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as projectsApi from '@/api/projects'
import type { Project, ProjectDetail } from '@/api/types'

/** 统一错误消息提取（BizError.message 或原生 Error.message）。 */
export function messageOf(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return String(error)
}

export const useProjectsStore = defineStore('projects', () => {
  const items = ref<Project[]>([])
  const current = ref<ProjectDetail | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 加载项目列表（项目看板）；并行拉详情补齐 run_count（列表接口不含该字段）。 */
  async function loadProjects(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const list = await projectsApi.fetchProjects()
      const details = await Promise.all(
        list.map(async (project) => {
          try {
            return await projectsApi.fetchProject(project.id)
          } catch {
            return null
          }
        }),
      )
      const countById = new Map(
        details
          .filter((detail): detail is ProjectDetail => detail !== null)
          .map((detail) => [detail.id, detail.run_count]),
      )
      items.value = list.map((project) => ({
        ...project,
        run_count: countById.get(project.id) ?? 0,
      }))
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  /** 加载项目详情（含 run_count）。 */
  async function loadProject(projectId: string): Promise<ProjectDetail | null> {
    loading.value = true
    error.value = null
    try {
      const detail = await projectsApi.fetchProject(projectId)
      current.value = detail
      return detail
    } catch (e) {
      error.value = messageOf(e)
      return null
    } finally {
      loading.value = false
    }
  }

  /** 创建项目并插入列表头部；返回新项目（失败抛 BizError 由调用方提示）。 */
  async function createProject(name: string, description?: string): Promise<Project> {
    const created = await projectsApi.createProject(name, description)
    items.value = [created, ...items.value]
    return created
  }

  function clearCurrent(): void {
    current.value = null
  }

  return { items, current, loading, error, loadProjects, loadProject, createProject, clearCurrent }
})
