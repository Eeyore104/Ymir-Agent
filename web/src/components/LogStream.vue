<!--
  日志流（§6.2 SSE 事件流 + 角色消息流的时间线展示）。

  数据源为运行 Store 的 events（useRunEvents 消费 SSE 后 ingest 进来）。
  用户上滚浏览时暂停自动跟随，滚回底部恢复。
-->
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import type { RunEvent } from '@/api/sse'

interface Props {
  events: RunEvent[]
  /** 订阅状态文案（由父组件从 useRunEvents 透传）。 */
  connectionStatus?: string
}

const props = withDefaults(defineProps<Props>(), {
  connectionStatus: 'idle',
})

interface LogEntry {
  key: string
  seq: number
  time: string
  type: string
  tagType: 'info' | 'success' | 'warning' | 'danger' | 'primary'
  text: string
}

const TAG_TYPES: Record<string, LogEntry['tagType']> = {
  'run.started': 'primary',
  'run.completed': 'success',
  'run.failed': 'danger',
  'run.paused': 'warning',
  'stage.started': 'primary',
  'stage.completed': 'success',
  'stage.failed': 'danger',
  'stage.log': 'info',
  'approval.requested': 'warning',
  'approval.decided': 'success',
  'artifact.created': 'primary',
  'message.created': 'info',
  'usage.updated': 'info',
  'budget.exceeded': 'danger',
  'evaluation.completed': 'success',
}

const entries = computed<LogEntry[]>(() =>
  props.events.map((event, index) => ({
    key: `${event.seq}-${index}`,
    seq: event.seq,
    time: formatTime(event.timestamp),
    type: event.type,
    tagType: TAG_TYPES[event.type] ?? 'info',
    text: describe(event),
  })),
)

const streamRef = ref<HTMLElement | null>(null)
const autoFollow = ref(true)

watch(
  () => props.events.length,
  async () => {
    if (!autoFollow.value) return
    await nextTick()
    scrollToBottom()
  },
)

function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

function scrollToBottom(): void {
  const el = streamRef.value
  if (el) el.scrollTop = el.scrollHeight
}

function onScroll(): void {
  const el = streamRef.value
  if (!el) return
  // 距底部 24px 内视为跟随
  autoFollow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24
}

/** 事件 -> 人类可读摘要（各类型 payload 见后端 events/log.py）。 */
function describe(event: RunEvent): string {
  const p = event.payload ?? {}
  const text = (value: unknown): string => (typeof value === 'string' ? value : '')
  switch (event.type) {
    case 'run.started':
      return `运行启动（模式 ${text(p.mode) || '-'}）`
    case 'run.completed':
      return '运行完成'
    case 'run.failed':
      return `运行失败：${text(p.error) || '未知错误'}`
    case 'run.paused':
      return `运行暂停（${text(p.reason) || '手动'}）`
    case 'stage.started':
      return `阶段启动：${text(p.stage_id)}（attempt ${String(p.attempt ?? 1)}）`
    case 'stage.completed':
      return `阶段完成：${text(p.stage_id)}`
    case 'stage.failed':
      return `阶段失败：${text(p.stage_id)}：${text(p.error) || '未知错误'}`
    case 'stage.log':
      return text(p.message) || '阶段日志'
    case 'approval.requested':
      return `待审批：${text(p.gate_name)}（阶段 ${text(p.stage_id)}）`
    case 'approval.decided': {
      const decision = text(p.decision) === 'reject' ? '驳回' : '通过'
      const rerun = text(p.rerun_stage)
      const auto = p.auto === true ? '（自动）' : ''
      const suffix = rerun ? `，回退重跑 ${rerun}` : ''
      return `审批${decision}${auto}：${text(p.gate_name)}${suffix}`
    }
    case 'artifact.created':
      return `产物落库：${text(p.name)} v${String(p.version ?? 1)}`
    case 'message.created':
      return `角色消息：${text(p.from_role) || '-'} → ${text(p.to_role) || '-'}`
    case 'usage.updated':
      return `用量更新：tokens ${String(p.total_tokens ?? 0)}`
    case 'budget.exceeded':
      return `预算超限：${text(p.reason) || 'budget'}`
    case 'evaluation.completed':
      return `评测完成：${text(p.verdict) || '-'}`
    default:
      return JSON.stringify(p)
  }
}
</script>

<template>
  <div class="log-stream">
    <div class="log-header">
      <span class="log-title">事件日志</span>
      <el-tag size="small" type="info" effect="plain">连接：{{ connectionStatus }}</el-tag>
    </div>
    <div ref="streamRef" class="log-body" @scroll.passive="onScroll">
      <div v-if="entries.length === 0" class="log-empty">暂无事件，等待运行启动…</div>
      <div v-for="entry in entries" :key="entry.key" class="log-line">
        <span class="log-time">{{ entry.time }}</span>
        <el-tag size="small" :type="entry.tagType" effect="light" class="log-type">
          {{ entry.type }}
        </el-tag>
        <span class="log-text">{{ entry.text }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.log-stream {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fff;
}

.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid #ebeef5;
}

.log-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.log-body {
  max-height: 360px;
  overflow-y: auto;
  padding: 8px 12px;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.9;
}

.log-empty {
  color: #909399;
  text-align: center;
  padding: 24px 0;
}

.log-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  white-space: pre-wrap;
  word-break: break-all;
}

.log-time {
  color: #909399;
  flex-shrink: 0;
}

.log-type {
  flex-shrink: 0;
}

.log-text {
  color: #303133;
}
</style>
