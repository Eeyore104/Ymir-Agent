/**
 * 系统 Store（Pinia setup 风格）：健康检查 / 模型网关视图 / 用量统计
 * （Settings 页面数据源）。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as systemApi from '@/api/system'
import type { HealthData, ModelsOut, UsageOut } from '@/api/types'
import { messageOf } from './projects'

export const useSystemStore = defineStore('system', () => {
  const health = ref<HealthData | null>(null)
  const models = ref<ModelsOut | null>(null)
  const usage = ref<UsageOut | null>(null)
  const healthLoading = ref(false)
  const healthError = ref<string | null>(null)
  const usageScope = ref<'global' | 'run' | 'project'>('global')

  /** 健康探测（失败不抛出，仅记录 healthError 供页面展示）。 */
  async function probeHealth(): Promise<boolean> {
    healthLoading.value = true
    healthError.value = null
    try {
      health.value = await systemApi.fetchHealth()
      return true
    } catch (e) {
      health.value = null
      healthError.value = messageOf(e)
      return false
    } finally {
      healthLoading.value = false
    }
  }

  /** 模型网关配置（脱敏）。 */
  async function loadModels(): Promise<void> {
    try {
      models.value = await systemApi.fetchModels()
    } catch (e) {
      healthError.value = messageOf(e)
    }
  }

  /** 用量统计（默认 global；可传 run / project 口径）。 */
  async function loadUsage(
    scope: 'global' | 'run' | 'project' = usageScope.value,
    params: { runId?: string; projectId?: string } = {},
  ): Promise<void> {
    usageScope.value = scope
    try {
      usage.value = await systemApi.fetchUsage(scope, params)
    } catch (e) {
      healthError.value = messageOf(e)
    }
  }

  return { health, models, usage, healthLoading, healthError, usageScope, probeHealth, loadModels, loadUsage }
})
