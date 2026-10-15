/**
 * 运行 Store（Pinia setup 风格）：当前运行 / 角色消息流 / SSE 事件流 /
 * 阶段看板状态（由事件驱动推进，与后端编排内核状态机同构展示）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import * as runsApi from '@/api/runs'
import type { RunEvent } from '@/api/sse'
import type { Run, RunAction, RunCreatePayload, RunMessage } from '@/api/types'
import { STAGE_META, type StageState } from '@/constants'
import { messageOf } from './projects'

/** 事件流保留上限（超出后丢弃最旧，防内存膨胀）。 */
const EVENT_LOG_LIMIT = 500

/** 阶段产物引用（来自 artifact.created 事件 payload）。 */
export interface StageArtifact {
  artifact_id: string
  name: string
  type: string
  version: number
  stage_id: string
}

/** 阶段看板行（组件 StageStepper 的直接数据源）。 */
export interface StageView {
  id: string
  name: string
  role: string
  gateName: string | null
  state: StageState
  artifacts: StageArtifact[]
}

export const useRunsStore = defineStore('runs', () => {
  const current = ref<Run | null>(null)
  const messages = ref<RunMessage[]>([])
  const events = ref<RunEvent[]>([])
  const stageStates = ref<Record<string, StageState>>({})
  const stageArtifacts = ref<Record<string, StageArtifact[]>>({})
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 阶段看板（STAGE_META × 实时状态 × 产物）。 */
  const stages = computed<StageView[]>(() =>
    STAGE_META.map((meta) => ({
      id: meta.id,
      name: meta.name,
      role: meta.role,
      gateName: meta.gateName,
      state: stageStates.value[meta.id] ?? 'pending',
      artifacts: stageArtifacts.value[meta.id] ?? [],
    })),
  )

  /** 终态判断（completed / failed 不再订阅刷新）。 */
  const isTerminal = computed<boolean>(
    () => current.value?.status === 'completed' || current.value?.status === 'failed',
  )

  function resetStageBoard(): void {
    const states: Record<string, StageState> = {}
    for (const meta of STAGE_META) states[meta.id] = 'pending'
    stageStates.value = states
    stageArtifacts.value = {}
  }

  function markStage(stageId: string, state: StageState): void {
    if (!(stageId in stageStates.value)) return
    stageStates.value = { ...stageStates.value, [stageId]: state }
  }

  /** 首帧回填：依据 current_stage_id 推断已完成阶段（SSE 连接前的初始态）。 */
  function backfillFromRun(run: Run): void {
    const idx = STAGE_META.findIndex((s) => s.id === run.current_stage_id)
    if (idx > 0) {
      for (let i = 0; i < idx; i += 1) {
        markStage(STAGE_META[i].id, 'completed')
      }
    }
    if (run.status === 'waiting_approval' && run.current_stage_id) {
      markStage(run.current_stage_id, 'waiting')
    }
  }

  function addArtifact(artifact: StageArtifact): void {
    const list = stageArtifacts.value[artifact.stage_id] ?? []
    // 同一 artifact_id + version 去重（SSE 重放可能重复投递）
    if (list.some((a) => a.artifact_id === artifact.artifact_id && a.version === artifact.version)) {
      return
    }
    stageArtifacts.value = {
      ...stageArtifacts.value,
      [artifact.stage_id]: [...list, artifact],
    }
  }

  /** 加载运行详情；并按 current_stage_id 回填已完成阶段（事件流未连接时）。 */
  async function loadRun(runId: string): Promise<Run | null> {
    loading.value = true
    error.value = null
    try {
      const run = await runsApi.fetchRun(runId)
      // 切换 run 才重置看板与日志并做首帧回填（同一 run 的重复刷新不清空，
      // 避免与 SSE 已摄入的事件竞争导致日志缺帧 / 状态回退）
      if (current.value?.id !== runId) {
        events.value = []
        messages.value = []
        resetStageBoard()
        current.value = run
        backfillFromRun(run)
      } else {
        current.value = run
      }
      return run
    } catch (e) {
      error.value = messageOf(e)
      return null
    } finally {
      loading.value = false
    }
  }

  /** 发起运行：重置看板并返回 run_id。 */
  async function startRun(projectId: string, payload: RunCreatePayload): Promise<string> {
    const runId = await runsApi.createRun(projectId, payload)
    current.value = null
    events.value = []
    messages.value = []
    resetStageBoard()
    return runId
  }

  /** 运行控制动作（pause / resume / cancel / retry-stage）。 */
  async function act(runId: string, action: RunAction): Promise<void> {
    const run = await runsApi.runAction(runId, action)
    current.value = run
  }

  /** 加载角色消息流（初始快照；后续由 SSE message.created 增量）。 */
  async function loadMessages(runId: string): Promise<void> {
    try {
      messages.value = await runsApi.fetchMessages(runId)
    } catch (e) {
      error.value = messageOf(e)
    }
  }

  /** 消费一条 SSE 事件：追加日志 + 推进阶段看板。 */
  function ingestEvent(event: RunEvent): void {
    events.value = [...events.value, event].slice(-EVENT_LOG_LIMIT)
    const payload = event.payload ?? {}
    const stageId = typeof payload.stage_id === 'string' ? payload.stage_id : null
    switch (event.type) {
      case 'stage.started':
        if (stageId) markStage(stageId, 'running')
        break
      case 'stage.completed':
        if (stageId) markStage(stageId, 'completed')
        break
      case 'stage.failed':
        if (stageId) markStage(stageId, 'failed')
        break
      case 'approval.requested':
        if (stageId) markStage(stageId, 'waiting')
        break
      case 'approval.decided': {
        // 通过：关卡解除，阶段保持 completed 等待引擎补推进；
        // 驳回：rerun_stage 回到 pending 等待重跑（随后 stage.started）
        const rerun = typeof payload.rerun_stage === 'string' ? payload.rerun_stage : null
        if (rerun) markStage(rerun, 'pending')
        break
      }
      case 'artifact.created': {
        const artifactId = payload.artifact_id
        if (stageId && typeof artifactId === 'string') {
          addArtifact({
            artifact_id: artifactId,
            name: typeof payload.name === 'string' ? payload.name : artifactId,
            type: typeof payload.type === 'string' ? payload.type : 'unknown',
            version: typeof payload.version === 'number' ? payload.version : 1,
            stage_id: stageId,
          })
        }
        break
      }
      case 'message.created':
        // 消息增量由调用方（RunDetail）按 seq 去重后追加，此处不处理
        break
      case 'run.completed':
        for (const meta of STAGE_META) markStage(meta.id, 'completed')
        break
      default:
        break
    }
  }

  /** 追加角色消息（按 seq 去重）。 */
  function appendMessage(message: RunMessage): void {
    if (messages.value.some((m) => m.seq === message.seq)) return
    messages.value = [...messages.value, message].sort((a, b) => a.seq - b.seq)
  }

  /** 同步运行实体（轮询 / SSE 之外的手动刷新）。 */
  function setRun(run: Run): void {
    current.value = run
  }

  function reset(): void {
    current.value = null
    events.value = []
    messages.value = []
    resetStageBoard()
    error.value = null
  }

  return {
    current,
    messages,
    events,
    loading,
    error,
    stages,
    isTerminal,
    loadRun,
    startRun,
    act,
    loadMessages,
    ingestEvent,
    appendMessage,
    setRun,
    reset,
  }
})
