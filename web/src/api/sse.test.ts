/**
 * SSE composable 单测（L7，QA-05）：断线重建 / 退避 / lastSeq 水位。
 *
 * 用假 EventSource 驱动：断言
 * - 首连 URL 不带 after_seq（全量重放由浏览器 Last-Event-ID / 服务端处理）；
 * - 服务端彻底关闭（readyState===CLOSED）后按退避间隔**主动重建**，
 *   且重建 URL 显式携带 ``?after_seq=lastSeq``；
 * - CONNECTING 分支不主动重建（交由浏览器自动重连 + Last-Event-ID）；
 * - lastSeq 单调递增；close() 后不再重建。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { EVENT_TYPES, useRunEvents, type RunEvent } from './sse'

/** 假 EventSource：记录 URL / 监听器，可注入帧与切换 readyState。 */
class FakeEventSource {
  static readonly CLOSED = 2
  static instances: FakeEventSource[] = []

  readyState = 0
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private readonly handlers = new Map<string, Set<(event: MessageEvent<string>) => void>>()

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, callback: (event: MessageEvent<string>) => void): void {
    const set = this.handlers.get(type) ?? new Set()
    set.add(callback)
    this.handlers.set(type, set)
  }

  close(): void {
    this.closed = true
    this.readyState = FakeEventSource.CLOSED
  }

  /** 向某事件类型的监听器投递一帧。 */
  emit(type: string, payload: unknown): void {
    const frame = { data: JSON.stringify(payload) } as MessageEvent<string>
    for (const callback of this.handlers.get(type) ?? []) callback(frame)
  }

  /** 投递**原始** data 字符串（用于注入非 JSON 帧）。 */
  emitRaw(type: string, data: string): void {
    const frame = { data } as MessageEvent<string>
    for (const callback of this.handlers.get(type) ?? []) callback(frame)
  }

  listenerCount(type: string): number {
    return this.handlers.get(type)?.size ?? 0
  }
}

const lastSource = (): FakeEventSource => {
  const source = FakeEventSource.instances.at(-1)
  if (!source) throw new Error('no EventSource created')
  return source
}

describe('useRunEvents', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    // composable 在无组件上下文调用时 Vue 会告警，静默以保持输出干净
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('首连：URL 不带 after_seq，注册全部事件类型，状态 connecting', () => {
    const handle = useRunEvents('run-1', { onEvent: () => {} })
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(lastSource().url).toBe('/api/v1/runs/run-1/events')
    expect(handle.status.value).toBe('connecting')
    for (const type of EVENT_TYPES) {
      expect(lastSource().listenerCount(type)).toBe(1)
    }
    handle.close()
  })

  it('onopen -> 状态 open；收帧 -> lastSeq 与 onEvent 回调', () => {
    const seen: RunEvent[] = []
    const handle = useRunEvents('run-1', { onEvent: (event) => seen.push(event) })
    lastSource().onopen?.()
    expect(handle.status.value).toBe('open')

    lastSource().emit('stage.started', {
      seq: 1,
      type: 'stage.started',
      run_id: 'run-1',
      timestamp: 't1',
      payload: { stage_id: 'prd' },
    })
    expect(seen).toHaveLength(1)
    expect(handle.lastSeq.value).toBe(1)
    handle.close()
  })

  it('lastSeq 单调递增（旧 seq 不回退）', () => {
    const handle = useRunEvents('run-1', { onEvent: () => {} })
    lastSource().emit('stage.log', { seq: 5, type: 'stage.log', run_id: 'r', timestamp: '', payload: {} })
    lastSource().emit('stage.log', { seq: 3, type: 'stage.log', run_id: 'r', timestamp: '', payload: {} })
    expect(handle.lastSeq.value).toBe(5)
    handle.close()
  })

  it('非 JSON 帧被忽略（不抛、lastSeq 不变）', () => {
    const seen: RunEvent[] = []
    const handle = useRunEvents('run-1', { onEvent: (event) => seen.push(event) })
    lastSource().emitRaw('stage.log', 'not-json')
    expect(seen).toHaveLength(0)
    expect(handle.lastSeq.value).toBe(0)
    handle.close()
  })

  it('服务端关闭（CLOSED）：退避后主动重建，URL 带 after_seq=lastSeq', () => {
    vi.useFakeTimers()
    const handle = useRunEvents('run-1', { onEvent: () => {} })
    lastSource().emit('stage.log', {
      seq: 7,
      type: 'stage.log',
      run_id: 'run-1',
      timestamp: '',
      payload: {},
    })
    lastSource().readyState = FakeEventSource.CLOSED
    lastSource().onerror?.()

    expect(handle.status.value).toBe('closed')
    expect(FakeEventSource.instances).toHaveLength(1) // 尚未重建

    vi.advanceTimersByTime(2000)
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(lastSource().url).toBe('/api/v1/runs/run-1/events?after_seq=7')
    handle.close()
  })

  it('CONNECTING 分支：不主动重建（交由浏览器自动重连）', () => {
    vi.useFakeTimers()
    const handle = useRunEvents('run-1', { onEvent: () => {} })
    lastSource().readyState = 0 // CONNECTING
    lastSource().onerror?.()
    vi.advanceTimersByTime(10_000)
    expect(FakeEventSource.instances).toHaveLength(1)
    handle.close()
  })

  it('close()：关闭源并不再重建', () => {
    vi.useFakeTimers()
    const handle = useRunEvents('run-1', { onEvent: () => {} })
    handle.close()
    expect(handle.status.value).toBe('closed')
    expect(lastSource().closed).toBe(true)
    vi.advanceTimersByTime(10_000)
    expect(FakeEventSource.instances).toHaveLength(1)
  })
})
