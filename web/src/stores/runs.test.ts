/**
 * runs store 单测（L7）：阶段看板事件推进、事件/消息按 seq 去重、首帧回填、
 * 加载失败错误码（PM-11）。
 *
 * mock ``@/api/runs``（不触网）；仅驱动 store 的纯状态逻辑。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { RunEvent } from '@/api/sse'
import type { Run, RunMessage } from '@/api/types'

const mocks = vi.hoisted(() => ({
  fetchRun: vi.fn(),
  createRun: vi.fn(),
  runAction: vi.fn(),
  fetchMessages: vi.fn(),
}))

vi.mock('@/api/runs', () => ({
  fetchRun: mocks.fetchRun,
  createRun: mocks.createRun,
  runAction: mocks.runAction,
  fetchMessages: mocks.fetchMessages,
}))

import { BizError } from '@/api/client'
import { useRunsStore } from './runs'

function event(seq: number, type: string, payload: Record<string, unknown> = {}): RunEvent {
  return { seq, type, run_id: 'r1', timestamp: '2026-01-01T00:00:00Z', payload }
}

function message(seq: number, id = `m${seq}`): RunMessage {
  return {
    id,
    run_id: 'r1',
    seq,
    type: 'artifact',
    from_role: 'pm',
    to_role: 'architect',
    content_refs: [],
    summary: `msg-${seq}`,
    payload: {},
    created_at: '2026-01-01T00:00:00Z',
  }
}

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 'r1',
    project_id: 'p1',
    pipeline_id: 'software-dev',
    mode: 'key_node',
    status: 'running',
    current_stage_id: null,
    context: { requirement: 'x' },
    error: null,
    started_at: null,
    finished_at: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const stateOf = (store: ReturnType<typeof useRunsStore>, stageId: string): string =>
  store.stages.find((stage) => stage.id === stageId)?.state ?? 'missing'

describe('runs store：阶段看板事件推进', () => {
  let store: ReturnType<typeof useRunsStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    mocks.fetchRun.mockReset()
    mocks.createRun.mockReset()
    mocks.runAction.mockReset()
    mocks.fetchMessages.mockReset()
    store = useRunsStore()
    store.reset() // 初始化阶段看板（对齐真实流程 loadRun/startRun 的复位）
  })

  it('stage.started -> running；approval.requested -> waiting', () => {
    store.ingestEvent(event(1, 'stage.started', { stage_id: 'prd' }))
    expect(stateOf(store, 'prd')).toBe('running')
    store.ingestEvent(event(2, 'approval.requested', { stage_id: 'prd' }))
    expect(stateOf(store, 'prd')).toBe('waiting')
  })

  it('approval.decided + rerun_stage -> 目标阶段回 pending', () => {
    store.ingestEvent(event(1, 'stage.completed', { stage_id: 'prd' }))
    expect(stateOf(store, 'prd')).toBe('completed')
    store.ingestEvent(event(2, 'approval.decided', { decision: 'reject', rerun_stage: 'prd' }))
    expect(stateOf(store, 'prd')).toBe('pending')
  })

  it('stage.failed -> failed；run.completed -> 全阶段 completed', () => {
    store.ingestEvent(event(1, 'stage.failed', { stage_id: 'coding' }))
    expect(stateOf(store, 'coding')).toBe('failed')
    store.ingestEvent(event(2, 'run.completed', {}))
    for (const id of ['prd', 'architecture', 'coding', 'evaluation']) {
      expect(stateOf(store, id)).toBe('completed')
    }
  })

  it('未知 stage_id 被忽略（不产生看板行）', () => {
    store.ingestEvent(event(1, 'stage.started', { stage_id: 'nope' }))
    expect(stateOf(store, 'nope')).toBe('missing')
    expect(store.stages.every((stage) => stage.state === 'pending')).toBe(true)
    expect(store.events).toHaveLength(1)
  })
})

describe('runs store：去重', () => {
  let store: ReturnType<typeof useRunsStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    mocks.fetchRun.mockReset()
    mocks.createRun.mockReset()
    mocks.runAction.mockReset()
    mocks.fetchMessages.mockReset()
    store = useRunsStore()
    store.reset() // 初始化阶段看板（对齐真实流程 loadRun/startRun 的复位）
  })

  it('ingestEvent 按 seq 去重（QA-05：重连全量重放 × 实时帧）', () => {
    store.ingestEvent(event(1, 'stage.started', { stage_id: 'prd' }))
    store.ingestEvent(event(1, 'stage.started', { stage_id: 'prd' })) // 重复 seq
    expect(store.events).toHaveLength(1)
    expect(store.events[0].seq).toBe(1)
  })

  it('appendMessage 按 seq 去重且保持有序', () => {
    store.appendMessage(message(2))
    store.appendMessage(message(1))
    store.appendMessage(message(2, 'dup'))
    expect(store.messages.map((m) => m.seq)).toEqual([1, 2])
  })
})

describe('runs store：加载与错误态', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mocks.fetchRun.mockReset()
    mocks.createRun.mockReset()
    mocks.runAction.mockReset()
    mocks.fetchMessages.mockReset()
  })

  it('loadRun 成功：按 current_stage_id 首帧回填已完成阶段', async () => {
    mocks.fetchRun.mockResolvedValue(run({ current_stage_id: 'coding' }))
    const store = useRunsStore()
    await store.loadRun('r1')
    expect(stateOf(store, 'prd')).toBe('completed')
    expect(stateOf(store, 'architecture')).toBe('completed')
    expect(stateOf(store, 'coding')).toBe('pending')
  })

  it('loadRun 失败：记录 error 与业务码（PM-11：区分 1002 运行不存在）', async () => {
    mocks.fetchRun.mockRejectedValue(new BizError(1002, '运行不存在'))
    const store = useRunsStore()
    const result = await store.loadRun('missing')
    expect(result).toBeNull()
    expect(store.error).toBe('运行不存在')
    expect(store.errorCode).toBe(1002)
    expect(store.current).toBeNull()
  })

  it('loadRun 失败后 reset 清理错误态', async () => {
    mocks.fetchRun.mockRejectedValue(new BizError(-1, '网络错误'))
    const store = useRunsStore()
    await store.loadRun('r1')
    expect(store.errorCode).toBe(-1)
    store.reset()
    expect(store.error).toBeNull()
    expect(store.errorCode).toBeNull()
  })

  it('startRun 重置看板与日志', async () => {
    mocks.createRun.mockResolvedValue('r2')
    const store = useRunsStore()
    store.ingestEvent(event(1, 'run.completed', {}))
    const runId = await store.startRun('p1', { requirement: 'x' })
    expect(runId).toBe('r2')
    expect(store.events).toHaveLength(0)
    expect(store.stages.every((stage) => stage.state === 'pending')).toBe(true)
  })
})

describe('runs store：修复时间线（T5.1）', () => {
  let store: ReturnType<typeof useRunsStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    mocks.fetchRun.mockReset()
    mocks.createRun.mockReset()
    mocks.runAction.mockReset()
    mocks.fetchMessages.mockReset()
    store = useRunsStore()
    store.reset()
  })

  /** 重新开一个 store（同文件内多场景隔离用）。 */
  function fresh(): ReturnType<typeof useRunsStore> {
    setActivePinia(createPinia())
    const next = useRunsStore()
    next.reset()
    return next
  }

  it('同 round 的 repair.started 只 patch 不重复 push（QA 变异 M3）', () => {
    store.ingestEvent(
      event(1, 'evaluation.failed', {
        total: 62.4,
        runnability: 48,
        failure_class: 'check_failed',
        failed_checks: [{ command: 'python -m pytest -q', exit_code: 1 }],
      }),
    )
    store.ingestEvent(event(2, 'repair.started', { repair_round: 1, max_rounds: 2 }))

    expect(store.repairRounds).toHaveLength(1)
    expect(store.repairRounds[0].trigger?.total).toBe(62.4)
    expect(store.repairRounds[0].trigger?.failedChecks).toEqual(['python -m pytest -q'])

    // 同 round 再来一条（重放 / 重复触发）：字段被后一条 patch，轮次不重复
    store.ingestEvent(
      event(3, 'repair.started', { repair_round: 1, max_rounds: 3, artifact_version: 2 }),
    )
    expect(store.repairRounds).toHaveLength(1)
    expect(store.repairRounds[0]).toMatchObject({
      round: 1,
      status: 'running',
      maxRounds: 3,
      artifactVersion: 2,
    })
    // 重复帧不得清空已捕获的触发原因（T5.1：pendingTrigger 已被前一条消费）
    expect(store.repairRounds[0].trigger?.total).toBe(62.4)
    expect(store.repairRounds[0].trigger?.failureClass).toBe('check_failed')
  })

  it('repair.exhausted -> 该轮 exhausted + 终止原因 / 末轮分数', () => {
    store.ingestEvent(event(1, 'repair.started', { repair_round: 1, max_rounds: 2 }))
    store.ingestEvent(
      event(2, 'repair.exhausted', {
        repair_round: 1,
        stop_reason: 'rounds_exhausted',
        last_total: 62.4,
        last_runnability: 48,
      }),
    )

    expect(store.repairRounds).toHaveLength(1)
    expect(store.repairRounds[0]).toMatchObject({
      round: 1,
      status: 'exhausted',
      stopReason: 'rounds_exhausted',
      total: 62.4,
      runnability: 48,
    })
  })

  it('repair.accepted 按版本反查轮次；查不到取末轮；无轮次时建 round 0', () => {
    // 场景 A：v1 在 round 1、v2 在 round 2 -> accepted v1 落到 round 1
    store.ingestEvent(
      event(1, 'repair.started', { repair_round: 1, artifact_version: 1 }),
    )
    store.ingestEvent(event(2, 'repair.started', { repair_round: 2, artifact_version: 2 }))
    store.ingestEvent(
      event(3, 'repair.accepted', {
        accepted_version: 1,
        total: 62.4,
        runnability: 48,
        accepted_by: 'user',
      }),
    )
    const matched = store.repairRounds.find((item) => item.round === 1)
    expect(matched?.accepted).toEqual({
      version: 1,
      total: 62.4,
      runnability: 48,
      acceptedBy: 'user',
    })
    expect(store.repairRounds.find((item) => item.round === 2)?.accepted).toBeUndefined()

    // 场景 B：版本不在任何轮 -> 贴到末轮
    const second = fresh()
    second.ingestEvent(event(1, 'repair.started', { repair_round: 1, artifact_version: 1 }))
    second.ingestEvent(event(2, 'repair.started', { repair_round: 2, artifact_version: 2 }))
    second.ingestEvent(
      event(3, 'repair.accepted', { accepted_version: 99, total: 70, runnability: 60 }),
    )
    expect(second.repairRounds).toHaveLength(2)
    expect(second.repairRounds[1].accepted?.version).toBe(99)
    expect(second.repairRounds[0].accepted).toBeUndefined()

    // 场景 C：全新 store（无任何轮）-> 建 round 0 并写入 accepted
    const third = fresh()
    third.ingestEvent(
      event(1, 'repair.accepted', { accepted_version: 3, total: 71, runnability: 58 }),
    )
    expect(third.repairRounds).toHaveLength(1)
    expect(third.repairRounds[0]).toMatchObject({ round: 0, accepted: { version: 3 } })
  })

  it('backfillFromRun：按 context.repair.history 播种 + accepted 贴到对应版本轮', async () => {
    mocks.fetchRun.mockResolvedValue(
      run({
        context: {
          requirement: 'x',
          repair: {
            round: 1,
            max_rounds: 2,
            history: [
              { round: 0, total: 62, runnability: 48, artifact_version: 1 },
              { round: 1, total: 81, runnability: 80, artifact_version: 2 },
            ],
            accepted: { version: 2, total: 81, runnability: 80, accepted_by: 'qa' },
          },
        },
      }),
    )

    await store.loadRun('r1')

    expect(store.repairRounds).toHaveLength(2)
    expect(store.repairRounds.map((item) => item.round)).toEqual([0, 1])
    const withAccepted = store.repairRounds.find((item) => item.artifactVersion === 2)
    expect(withAccepted?.accepted).toEqual({
      version: 2,
      total: 81,
      runnability: 80,
      acceptedBy: 'qa',
    })

    // 回填后同 round 的增量事件只 patch，不重复 push
    store.ingestEvent(event(1, 'repair.started', { repair_round: 1, max_rounds: 3 }))
    expect(store.repairRounds).toHaveLength(2)
    expect(store.repairRounds.find((item) => item.round === 1)?.maxRounds).toBe(3)
  })

  it('未知事件类型被忽略（不抛错、时间线不变）', () => {
    store.ingestEvent(event(1, 'repair.started', { repair_round: 1 }))
    const before = JSON.stringify(store.repairRounds)

    expect(() => store.ingestEvent(event(2, 'some.unknown', { foo: 'bar' }))).not.toThrow()

    expect(JSON.stringify(store.repairRounds)).toBe(before)
    expect(store.events).toHaveLength(2) // 事件日志仍记录未知类型
  })
})
