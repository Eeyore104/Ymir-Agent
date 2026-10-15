<!--
  阶段看板（docs/02 §3.2 四角色流水线可视化）。

  数据源为运行 Store 的 stages（由 SSE 事件驱动推进）：
  pending -> running -> waiting(人工关卡) -> completed，reject 后回到 pending 重跑。
-->
<script setup lang="ts">
import { computed } from 'vue'

import type { StageView } from '@/stores/runs'
import { ROLE_LABELS, type StageState } from '@/constants'

interface Props {
  stages: StageView[]
}

const props = defineProps<Props>()

defineEmits<{
  (e: 'select-artifact', artifactId: string): void
}>()

/** 阶段状态 -> el-step status。 */
const STEP_STATUS: Record<StageState, 'wait' | 'process' | 'finish' | 'error' | 'success'> = {
  pending: 'wait',
  running: 'process',
  waiting: 'finish',
  completed: 'success',
  failed: 'error',
}

const STATE_LABELS: Record<StageState, string> = {
  pending: '待执行',
  running: '执行中',
  waiting: '等待审批',
  completed: '已完成',
  failed: '失败',
}

const STATE_TAG_TYPES: Record<StageState, 'info' | 'success' | 'warning' | 'danger'> = {
  pending: 'info',
  running: 'warning',
  waiting: 'warning',
  completed: 'success',
  failed: 'danger',
}

/** el-steps 的 active：首个未完成阶段的下标（仅作视觉兜底，各 step 显式置状态）。 */
const activeIndex = computed(() => {
  const idx = props.stages.findIndex((s) => s.state !== 'completed')
  return idx === -1 ? props.stages.length : idx
})

function stepStatus(state: StageState): 'wait' | 'process' | 'finish' | 'error' | 'success' {
  return STEP_STATUS[state]
}

function stateLabel(state: StageState): string {
  return STATE_LABELS[state]
}

function stateTagType(state: StageState): 'info' | 'success' | 'warning' | 'danger' {
  return STATE_TAG_TYPES[state]
}

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}
</script>

<template>
  <div class="stage-stepper">
    <el-steps :active="activeIndex" align-center>
      <el-step v-for="stage in stages" :key="stage.id" :status="stepStatus(stage.state)">
        <template #title>
          <span class="stage-title">{{ stage.name }}</span>
        </template>
        <template #description>
          <div class="stage-desc">
            <span class="stage-role">{{ roleLabel(stage.role) }}</span>
            <el-tag size="small" :type="stateTagType(stage.state)" effect="plain">
              {{ stateLabel(stage.state) }}
            </el-tag>
          </div>
          <div v-if="stage.gateName" class="stage-gate">关卡：{{ stage.gateName }}</div>
          <div v-if="stage.artifacts.length > 0" class="stage-artifacts">
            <el-button
              v-for="artifact in stage.artifacts"
              :key="`${artifact.artifact_id}@${artifact.version}`"
              link
              type="primary"
              size="small"
              @click="$emit('select-artifact', artifact.artifact_id)"
            >
              {{ artifact.name }} v{{ artifact.version }}
            </el-button>
          </div>
        </template>
      </el-step>
    </el-steps>
  </div>
</template>

<style scoped>
.stage-stepper {
  padding: 8px 0 4px;
}

.stage-desc {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 4px;
}

.stage-role {
  font-size: 12px;
  color: #606266;
}

.stage-gate {
  margin-top: 2px;
  font-size: 12px;
  color: #e6a23c;
}

.stage-artifacts {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-top: 2px;
}
</style>
