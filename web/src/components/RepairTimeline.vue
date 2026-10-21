<!--
  修复时间线（v1.1，T5.1）：把 repair.* 事件折叠成「第 N 轮」块。

  数据源：`runsStore.repairRounds`（由 SSE 事件推进，刷新时按 run.context 回填）。
  - running：蓝；completed：绿；exhausted：红；
  - 展开可见触发原因（evaluation.failed 的分数/分类/失败检查前 3 条）、耗时、成本、
    产物版本（点击由父组件打开产物详情）、人工接受信息。

  `rounds` 为空时整块不渲染（v1.0 无回边 run 完全不出现该面板）。
-->
<script setup lang="ts">
import { computed } from 'vue'

import type { RepairRound, RepairRoundStatus } from '@/stores/runs'

const props = defineProps<{ rounds: RepairRound[] }>()

const emit = defineEmits<{ 'select-artifact': [version: number] }>()

const STATUS_LABELS: Record<RepairRoundStatus, string> = {
  running: '修复中',
  completed: '已通过',
  exhausted: '已耗尽',
}

const STATUS_TAG_TYPES: Record<RepairRoundStatus, 'primary' | 'success' | 'danger'> = {
  running: 'primary',
  completed: 'success',
  exhausted: 'danger',
}

/** 展开哪些轮：默认全部展开（轮数通常 ≤ 3，折叠反而增加点击）。 */
const activeNames = computed<string[]>(() => props.rounds.map((item) => String(item.round)))

function statusLabel(status: RepairRoundStatus): string {
  return STATUS_LABELS[status] ?? status
}

function scoreText(total?: number, runnability?: number): string {
  const parts: string[] = []
  if (typeof total === 'number') parts.push(`total ${total.toFixed(1)}`)
  if (typeof runnability === 'number') parts.push(`runnable ${runnability.toFixed(1)}`)
  return parts.length > 0 ? parts.join(' / ') : '—'
}

function durationText(ms?: number): string {
  if (typeof ms !== 'number') return '—'
  return `${(ms / 1000).toFixed(1)}s`
}

function costText(cost?: number): string {
  return typeof cost === 'number' ? `¥${cost.toFixed(4)}` : '—'
}

function stopReasonText(reason?: string | null): string {
  if (reason === 'budget_exceeded') return '预算超限（可提额后 extend-repair）'
  if (reason === 'rounds_exhausted') return '轮次耗尽（可 accept-best / extend-repair）'
  return reason ?? '—'
}
</script>

<template>
  <el-card v-if="rounds.length > 0" shadow="never" class="repair-timeline">
    <template #header><strong>修复时间线</strong></template>
    <el-collapse :model-value="activeNames">
      <el-collapse-item
        v-for="item in rounds"
        :key="item.round"
        :name="String(item.round)"
        class="repair-round"
      >
        <template #title>
          <div class="round-title">
            <el-tag size="small" type="info" effect="plain">第 {{ item.round }} 轮</el-tag>
            <el-tag size="small" :type="STATUS_TAG_TYPES[item.status]">
              {{ statusLabel(item.status) }}
            </el-tag>
            <span class="round-score">{{ scoreText(item.total, item.runnability) }}</span>
            <span class="round-duration">{{ durationText(item.durationMs) }}</span>
          </div>
        </template>

        <div class="round-body">
          <div class="round-row">
            <span class="round-label">触发原因</span>
            <span class="round-value">
              <template v-if="item.trigger">
                {{ scoreText(item.trigger.total, item.trigger.runnability) }}
                <el-tag v-if="item.trigger.failureClass" size="small" type="warning" effect="plain">
                  {{ item.trigger.failureClass }}
                </el-tag>
              </template>
              <template v-else>—</template>
            </span>
          </div>

          <ul
            v-if="item.trigger?.failedChecks && item.trigger.failedChecks.length > 0"
            class="round-checks"
          >
            <li v-for="(check, index) in item.trigger.failedChecks.slice(0, 3)" :key="index">
              <code>{{ check }}</code>
            </li>
          </ul>

          <div class="round-row">
            <span class="round-label">耗时 / 成本</span>
            <span class="round-value">
              {{ durationText(item.durationMs) }} / {{ costText(item.costCny) }}
            </span>
          </div>

          <div v-if="item.status === 'exhausted'" class="round-row">
            <span class="round-label">终止原因</span>
            <span class="round-value">{{ stopReasonText(item.stopReason) }}</span>
          </div>

          <div v-if="item.accepted" class="round-row">
            <span class="round-label">人工接受</span>
            <span class="round-value">
              v{{ item.accepted.version }}（total {{ item.accepted.total.toFixed(1) }}，by
              {{ item.accepted.acceptedBy }}）
            </span>
          </div>

          <div v-if="item.status !== 'exhausted' && typeof item.artifactVersion === 'number'" class="round-row">
            <span class="round-label">产物版本</span>
            <span class="round-value">
              <el-button link type="primary" size="small" @click="emit('select-artifact', item.artifactVersion!)">
                查看 v{{ item.artifactVersion }}
              </el-button>
            </span>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </el-card>
</template>

<style scoped>
.repair-timeline {
  margin: 0;
}

.round-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.round-score {
  color: var(--el-text-color-regular);
  font-variant-numeric: tabular-nums;
}

.round-duration {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.round-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}

.round-row {
  display: flex;
  gap: 12px;
}

.round-label {
  min-width: 72px;
  color: var(--el-text-color-secondary);
}

.round-value {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-primary);
}

.round-checks {
  margin: 0 0 0 84px;
  padding-left: 16px;
  color: var(--el-text-color-regular);
}

.round-checks code {
  font-size: 12px;
}
</style>
