/**
 * SSE 事件流 composable（docs/02 §6.2，验收点③：断线重订阅不丢事件）。
 *
 * 连接 ``GET /runs/{id}/events``（经 vite 代理）：
 * - 原生 ``EventSource`` 断线后自动重连，并携带 ``Last-Event-ID`` 头
 *   （值为最后收到帧的 ``id:`` = 事件 seq），服务端按 seq 重放缺口；
 * - 本层额外跟踪 ``lastSeq``（来自事件体），并在服务端彻底关闭连接
 *   （``readyState === CLOSED``）时按退避间隔主动重建订阅；
 * - 心跳帧（``: keepalive``）由浏览器 EventSource 自动忽略。
 */

import { onUnmounted, ref, type Ref } from 'vue'

/** SSE 事件信封（与后端 events/sse.py format_sse 一致）。 */
export interface RunEvent {
  seq: number
  type: string
  run_id: string
  timestamp: string
  payload: Record<string, unknown>
}

/** 订阅状态。 */
export type SseStatus = 'idle' | 'connecting' | 'open' | 'closed'

/** 事件类型全集（后端 events/log.py EventType；T05 预留 evaluation.completed）。 */
export const EVENT_TYPES = [
  'run.started',
  'run.completed',
  'run.failed',
  'run.paused',
  'stage.started',
  'stage.completed',
  'stage.failed',
  'stage.log',
  'approval.requested',
  'approval.decided',
  'artifact.created',
  'message.created',
  'usage.updated',
  'budget.exceeded',
  'evaluation.completed',
] as const

/** 主动重建订阅的退避间隔（毫秒；原生重连间隔由浏览器控制）。 */
const REBUILD_BACKOFF_MS = 2000

export interface UseRunEventsOptions {
  /** 收到一条事件。 */
  onEvent: (event: RunEvent) => void
  /** 订阅状态变化。 */
  onStatusChange?: (status: SseStatus) => void
}

export interface RunEventsHandle {
  /** 当前订阅状态。 */
  status: Ref<SseStatus>
  /** 最后收到的事件 seq（用于展示与重连对账）。 */
  lastSeq: Ref<number>
  /** 手动重订阅（关闭当前连接并以 Last-Event-ID 重建）。 */
  reconnect: () => void
  /** 关闭订阅（组件卸载时调用）。 */
  close: () => void
}

export function useRunEvents(runId: string, options: UseRunEventsOptions): RunEventsHandle {
  const status = ref<SseStatus>('idle')
  const lastSeq = ref(0)
  let source: EventSource | null = null
  let rebuildTimer: number | undefined
  let disposed = false

  const setStatus = (next: SseStatus): void => {
    status.value = next
    options.onStatusChange?.(next)
  }

  const handleFrame = (raw: MessageEvent<string>): void => {
    let event: RunEvent
    try {
      event = JSON.parse(raw.data) as RunEvent
    } catch {
      return // 非 JSON 帧（理论不存在）：忽略
    }
    if (typeof event.seq === 'number' && event.seq > lastSeq.value) {
      lastSeq.value = event.seq
    }
    options.onEvent(event)
  }

  const open = (): void => {
    if (disposed) return
    source?.close()
    // 浏览器自动携带 Last-Event-ID（= 最后帧 id）；首连为全量重放
    const es = new EventSource(`/api/v1/runs/${runId}/events`)
    source = es
    setStatus('connecting')
    for (const type of EVENT_TYPES) {
      es.addEventListener(type, handleFrame as EventListener)
    }
    es.onopen = () => setStatus('open')
    es.onerror = () => {
      if (disposed) return
      setStatus('closed')
      if (es.readyState === EventSource.CLOSED) {
        // 服务端彻底关闭：退避后主动重建（原生自动重连已放弃）
        window.clearTimeout(rebuildTimer)
        rebuildTimer = window.setTimeout(open, REBUILD_BACKOFF_MS)
      }
      // 非 CLOSED（CONNECTING）：浏览器会自动重连并带 Last-Event-ID
    }
  }

  const close = (): void => {
    disposed = true
    window.clearTimeout(rebuildTimer)
    if (source) {
      source.close()
      source = null
    }
    setStatus('closed')
  }

  const reconnect = (): void => {
    if (disposed) {
      disposed = false
    }
    open()
  }

  onUnmounted(close)

  open()

  return { status, lastSeq, reconnect, close }
}
