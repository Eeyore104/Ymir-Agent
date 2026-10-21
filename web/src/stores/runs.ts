/**
 * 运行 Store（Pinia setup 风格）：当前运行 / 角色消息流 / SSE 事件流 /
 * 阶段看板状态（由事件驱动推进，与后端编排内核状态机同构展示）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import * as runsApi from '@/api/runs'
import { BizError } from '@/api/client'
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

/** 修复轮次状态（v1.1，T5.1 时间线）。 */
export type RepairRoundStatus = 'running' | 'completed' | 'exhausted'

/** 一轮修复的触发原因（来自 ``evaluation.failed`` 事件）。 */
export interface RepairRoundTrigger {
  total?: number
  runnability?: number
  failureClass?: string | null
  failedChecks?: string[]
}

/** 人工接受未达标版本的信息（``repair.accepted``）。 */
export interface RepairAccepted {
  version: number
  total: number
  runnability: number
  acceptedBy: string
}

/** 修复轮次（组件 RepairTimeline 的直接数据源）。 */
export interface RepairRound {
  round: number
  status: RepairRoundStatus
  maxRounds?: number
  trigger?: RepairRoundTrigger
  startedAt?: string
  durationMs?: number
  total?: number
  runnability?: number
  artifactVersion?: number | null
  costCny?: number
  stopReason?: string | null
  accepted?: RepairAccepted
}

/** 事件 payload 取数工具（后端 payload 是自由字典，前端逐项校验类型）。 */
function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined
}

function asVersion(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}


export const useRunsStore = defineStore('runs', () => {
  const current = ref<Run | null>(null)
  const messages = ref<RunMessage[]>([])
  const events = ref<RunEvent[]>([])
  const stageStates = ref<Record<string, StageState>>({})
  const stageArtifacts = ref<Record<string, StageArtifact[]>>({})
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** 最近一次加载失败的业务码（PM-11：区分「run 不存在 1002」与其它失败）。 */
  const errorCode = ref<number | null>(null)
  /** 修复轮次时间线（v1.1，T5.1）：由 repair.* 事件推进，刷新时按 context 回填。 */
  const repairRounds = ref<RepairRound[]>([])
  /** 最近一条 evaluation.failed 的触发原因（供紧随其后的 repair.started 取用）。 */
  const pendingTrigger = ref<RepairRoundTrigger | null>(null)

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
    backfillRepairRounds(run)
  }

  /**
   * 刷新页面后的修复轮次回填（v1.1）：``run.context.repair.history`` 是流水线内裁决的
   * 落库形态，先按它播种各轮（状态默认 completed），后续事件到达再覆盖/追加。
   * 这样「刷新即空时间线」不会发生，且与 SSE 增量不冲突（同 round 覆盖而非重复）。
   */
  function backfillRepairRounds(run: Run): void {
    if (repairRounds.value.length > 0) return
    const context = (run.context ?? {}) as Record<string, unknown>
    const repair = context['repair']
    if (!repair || typeof repair !== 'object') return
    const state = repair as Record<string, unknown>
    const history = Array.isArray(state['history']) ? state['history'] : []
    const rounds: RepairRound[] = []
    for (const entry of history) {
      if (!entry || typeof entry !== 'object') continue
      const row = entry as Record<string, unknown>
      const round = asNumber(row['round'])
      if (round === undefined) continue
      rounds.push({
        round,
        status: 'completed',
        total: asNumber(row['total']),
        runnability: asNumber(row['runnability']),
        artifactVersion: asVersion(row['artifact_version']),
      })
    }
    const accepted = state['accepted']
    if (accepted && typeof accepted === 'object') {
      const info = accepted as Record<string, unknown>
      const version = asNumber(info['version'])
      if (version !== undefined) {
        const target = rounds.find((item) => item.artifactVersion === version) ?? rounds.at(-1)
        if (target) {
          target.accepted = {
            version,
            total: asNumber(info['total']) ?? 0,
            runnability: asNumber(info['runnability']) ?? 0,
            acceptedBy: asString(info['accepted_by']) ?? 'user',
          }
        }
      }
    }
    repairRounds.value = rounds.sort((a, b) => a.round - b.round)
  }

  /** 插入或更新一轮修复（同 round 覆盖，不重复 push）。 */
  function upsertRepairRound(round: number, patch: Partial<RepairRound>): void {
    const index = repairRounds.value.findIndex((item) => item.round === round)
    if (index < 0) {
      const created: RepairRound = { round, status: 'running', ...patch }
      repairRounds.value = [...repairRounds.value, created].sort((a, b) => a.round - b.round)
      return
    }
    const next = [...repairRounds.value]
    next[index] = { ...next[index], ...patch }
    repairRounds.value = next
  }

  /** 按产物版本找对应轮次（``repair.accepted`` 的 payload 不带 round）。 */
  function roundOfVersion(version: number): number {
    const matched = [...repairRounds.value].reverse().find((item) => item.artifactVersion === version)
    return matched?.round ?? repairRounds.value.at(-1)?.round ?? 0
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
    errorCode.value = null
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
      errorCode.value = e instanceof BizError ? e.code : null
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

  /** 消费一条 SSE 事件：按 seq 去重后追加日志 + 推进阶段看板。 */
  function ingestEvent(event: RunEvent): void {
    // 去重（QA-05）：断线重建后「全量重放 × 实时帧」可能重复投递同 seq 事件
    if (events.value.some((e) => e.seq === event.seq)) return
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
      case 'evaluation.failed':
        // 暂存触发原因：紧随其后的 repair.started 取用（事件顺序由后端保证）
        pendingTrigger.value = {
          total: asNumber(payload.total),
          runnability: asNumber(payload.runnability),
          failureClass: asString(payload.failure_class) ?? null,
          failedChecks: Array.isArray(payload.failed_checks)
            ? payload.failed_checks
                .map((item) =>
                  typeof item === 'string'
                    ? item
                    : asString((item as Record<string, unknown> | null)?.['command']),
                )
                .filter((value): value is string => typeof value === 'string')
            : [],
        }
        break
      case 'repair.started': {
        const round = asNumber(payload.repair_round) ?? 0
        const patch: Partial<RepairRound> = {
          status: 'running',
          maxRounds: asNumber(payload.max_rounds),
          artifactVersion: asVersion(payload.artifact_version),
          startedAt: event.timestamp,
        }
        // 触发原因只在本次事件**真的捕获到**时写入：同 round 的重复帧 / 重放帧
        // （pendingTrigger 已被前一条消费）不得把既有 trigger 清空（T5.1 修复）。
        if (pendingTrigger.value) patch.trigger = pendingTrigger.value
        upsertRepairRound(round, patch)
        pendingTrigger.value = null
        break
      }
      case 'repair.completed': {
        const round = asNumber(payload.repair_round) ?? 0
        upsertRepairRound(round, {
          status: 'completed',
          total: asNumber(payload.total),
          runnability: asNumber(payload.runnability),
          artifactVersion: asVersion(payload.artifact_version),
          durationMs: asNumber(payload.wall_time_ms),
          costCny: asNumber(payload.cost_cny),
        })
        break
      }
      case 'repair.exhausted': {
        const round = asNumber(payload.repair_round) ?? 0
        upsertRepairRound(round, {
          status: 'exhausted',
          stopReason: asString(payload.stop_reason) ?? null,
          total: asNumber(payload.last_total),
          runnability: asNumber(payload.last_runnability),
        })
        break
      }
      case 'repair.accepted': {
        const version = asNumber(payload.accepted_version)
        if (version !== undefined) {
          const round = roundOfVersion(version)
          upsertRepairRound(round, {
            accepted: {
              version,
              total: asNumber(payload.total) ?? 0,
              runnability: asNumber(payload.runnability) ?? 0,
              acceptedBy: asString(payload.accepted_by) ?? 'user',
            },
          })
        }
        break
      }
      case 'approval.deferred':
        // 中间候选跳过第三关：仅审计，不建卡片（时间线不呈现）
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
    repairRounds.value = []
    pendingTrigger.value = null
    resetStageBoard()
    error.value = null
    errorCode.value = null
  }

  return {
    current,
    messages,
    events,
    loading,
    error,
    errorCode,
    repairRounds,
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
