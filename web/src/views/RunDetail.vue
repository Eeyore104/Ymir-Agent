<!--
  运行详情（/runs/:id）：阶段看板 + 人工审批（内嵌卡片，验收点①②③）+
  事件日志 + 角色消息 + 阶段产物。

  SSE（useRunEvents）驱动实时刷新；断线由浏览器自动重连（Last-Event-ID），
  服务端按 seq 重放，前端按 seq 去重。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import ApprovalCard from '@/components/ApprovalCard.vue'
import ArtifactViewDialog from '@/components/ArtifactViewDialog.vue'
import LogStream from '@/components/LogStream.vue'
import RepairTimeline from '@/components/RepairTimeline.vue'
import StageStepper from '@/components/StageStepper.vue'
import { useRunEvents, type RunEvent, type SseStatus } from '@/api/sse'
import { useApprovalsStore } from '@/stores/approvals'
import { useRunsStore } from '@/stores/runs'
import { RUN_MODE_LABELS, RUN_STATUS_LABELS, RUN_STATUS_TAG_TYPES } from '@/constants'
import type { ApprovalDetail, RunAction, RunMessage } from '@/api/types'

const route = useRoute()
const runsStore = useRunsStore()
const approvalsStore = useApprovalsStore()

const runId = String(route.params.id)

/** 当前待审批记录（running 详情内嵌审批卡）。 */
const pendingApproval = ref<ApprovalDetail | null>(null)
const sseStatus = ref<SseStatus>('idle')

const run = computed(() => runsStore.current)
const isTerminal = computed(() => runsStore.isTerminal)

/** 需求原文（run.context.requirement，context 为自由字典）。 */
const requirement = computed<string>(() => {
  const value = run.value?.context?.['requirement']
  return typeof value === 'string' && value ? value : '-'
})

/** 安全网轮询（SSE 异常时的兜底刷新；终态后停止）。 */
let pollTimer: number | undefined

onMounted(async () => {
  await runsStore.loadRun(runId)
  await runsStore.loadMessages(runId)
  await refreshPendingApproval()
  pollTimer = window.setInterval(() => {
    if (!isTerminal.value) void runsStore.loadRun(runId)
  }, 5000)
})

onBeforeUnmount(() => {
  window.clearInterval(pollTimer)
})

/** SSE 订阅（自动重连 + Last-Event-ID 重放）。 */
useRunEvents(runId, {
  onEvent: (event: RunEvent) => {
    runsStore.ingestEvent(event)
    switch (event.type) {
      case 'message.created':
        // 消息事件已携带完整消息体：按 seq 去重增量追加（替代整体刷新）
        ingestMessage(event)
        break
      case 'approval.requested':
        void refreshPendingApproval()
        break
      case 'approval.decided':
        void runsStore.loadRun(runId)
        void refreshPendingApproval()
        break
      case 'run.completed':
      case 'run.failed':
        void runsStore.loadRun(runId)
        break
      default:
        break
    }
  },
  onStatusChange: (status: SseStatus) => {
    sseStatus.value = status
  },
})

/**
 * 从 message.created 事件增量追加一条角色消息（按 seq 去重）。
 * 事件 payload 若缺关键字段则回退整体刷新（兼容旧帧 / 重放）。
 */
function ingestMessage(event: RunEvent): void {
  const p = (event.payload ?? {}) as Record<string, unknown>
  const str = (value: unknown): string | null => (typeof value === 'string' ? value : null)
  if (typeof p.message_id !== 'string' || typeof p.seq !== 'number') {
    void runsStore.loadMessages(runId)
    return
  }
  const message: RunMessage = {
    id: p.message_id,
    run_id: runId,
    seq: p.seq,
    type: str(p.type) ?? 'system',
    from_role: str(p.from_role),
    to_role: str(p.to_role),
    content_refs: Array.isArray(p.content_refs) ? p.content_refs : [],
    summary: str(p.summary),
    payload: (p.payload as Record<string, unknown>) ?? {},
    created_at: str(p.created_at) ?? event.timestamp,
  }
  runsStore.appendMessage(message)
}

/** 刷新当前 run 的待审批（审批卡数据源）。 */async function refreshPendingApproval(): Promise<void> {
  await approvalsStore.loadApprovals({ status: 'pending', runId })
  const first = approvalsStore.items.find((a) => a.run_id === runId)
  if (!first) {
    pendingApproval.value = null
    return
  }
  pendingApproval.value = await approvalsStore.loadApproval(first.id)
}

/** 审批决策（通过 / 驳回带反馈）。 */
async function onDecide(payload: { decision: 'approve' | 'reject'; feedback?: string }): Promise<void> {
  if (!pendingApproval.value) return
  try {
    await approvalsStore.decide(pendingApproval.value.id, payload)
    ElMessage.success(payload.decision === 'approve' ? '已通过，流水线继续推进' : '已驳回，角色将带反馈修订重跑')
    await runsStore.loadRun(runId)
    await refreshPendingApproval()
  } catch (e) {
    ElMessage.error(`决策失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

/** 运行控制动作。 */
async function onAction(action: RunAction): Promise<void> {
  try {
    await runsStore.act(runId, action)
    ElMessage.success(`动作已执行：${action}`)
  } catch (e) {
    ElMessage.error(`动作失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

/** 阶段产物查看对话框（共享 ArtifactViewDialog；PM-08）。 */
const artifactDialogVisible = ref(false)
const artifactDialogId = ref('')
const artifactDialogVersion = ref<number | null>(null)
const artifactDialogTitle = ref('')

function onSelectArtifact(artifactId: string): void {
  const artifact = runsStore.stages
    .flatMap((s) => s.artifacts)
    .find((a) => a.artifact_id === artifactId)
  artifactDialogId.value = artifactId
  artifactDialogVersion.value = artifact?.version ?? null
  artifactDialogTitle.value = artifact ? `${artifact.name} v${artifact.version}` : artifactId
  artifactDialogVisible.value = true
}

/**
 * 修复时间线的「查看 vN」入口：事件里只有版本号，先在阶段产物索引里反查 artifact_id。
 * 反查不到（例如刷新后 artifact.created 事件已不在本地）时给出明确提示，不静默失败。
 */
function onSelectRepairArtifact(version: number): void {
  const artifact = runsStore.stages
    .flatMap((s) => s.artifacts)
    .find((a) => a.version === version)
  if (!artifact) {
    ElMessage.info(`未定位到产物 v${version} 的引用（可在「阶段产物」中按名查看最新版本）`)
    return
  }
  onSelectArtifact(artifact.artifact_id)
}

/** 业务错误码中文标签（PM-10：run 失败码透传展示）。 */
const ERROR_CODE_LABELS: Record<number, string> = {
  1001: '参数错误',
  1002: '资源不存在',
  1003: '状态冲突',
  1004: '预算超限',
  1005: '沙箱错误',
  1006: '模型调用失败',
  500: '服务端错误',
}

function errorCodeLabel(code: number): string {
  return `${code} · ${ERROR_CODE_LABELS[code] ?? '未知错误'}`
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <div v-loading="runsStore.loading" class="run-detail">
    <!-- 加载错误横幅（PM-11：区分「run 不存在」与「运行失败」） -->
    <el-alert
      v-if="runsStore.error"
      class="run-load-error"
      :type="runsStore.errorCode === 1002 ? 'warning' : 'error'"
      :closable="false"
      show-icon
      :title="runsStore.errorCode === 1002 ? '运行不存在' : '加载运行失败'"
      :description="runsStore.error"
    />

    <!-- 头部：状态 + 动作 -->
    <div class="run-header">
      <div class="run-title">
        <span class="run-id">运行 {{ runId.slice(0, 8) }}</span>
        <el-tag v-if="run" :type="RUN_STATUS_TAG_TYPES[run.status]" size="default">
          {{ RUN_STATUS_LABELS[run.status] }}
        </el-tag>
        <el-tag v-if="run" type="info" size="default" effect="plain">
          {{ RUN_MODE_LABELS[run.mode] ?? run.mode }}
        </el-tag>
      </div>
      <div class="run-actions">
        <el-button size="small" :disabled="!run || isTerminal" @click="onAction('pause')">暂停</el-button>
        <el-button size="small" :disabled="run?.status !== 'paused'" @click="onAction('resume')">恢复</el-button>
        <el-button size="small" :disabled="!run || isTerminal" @click="onAction('retry-stage')">重试当前阶段</el-button>
        <el-button size="small" type="danger" plain :disabled="!run || isTerminal" @click="onAction('cancel')">
          取消
        </el-button>
        <el-button size="small" @click="runsStore.loadRun(runId)">刷新</el-button>
      </div>
    </div>

    <el-descriptions v-if="run" :column="2" size="small" border class="run-meta">
      <el-descriptions-item label="需求" :span="2">
        {{ requirement }}
      </el-descriptions-item>
      <el-descriptions-item label="启动时间">{{ formatTime(run.started_at) }}</el-descriptions-item>
      <el-descriptions-item label="结束时间">{{ formatTime(run.finished_at) }}</el-descriptions-item>
      <el-descriptions-item v-if="run.error" label="错误" :span="2">
        <div class="run-error-row">
          <el-tag v-if="run.error_code" size="small" type="danger" effect="dark">
            {{ errorCodeLabel(run.error_code) }}
          </el-tag>
          <span class="run-error">{{ run.error }}</span>
        </div>
      </el-descriptions-item>
    </el-descriptions>

    <!-- 阶段看板 -->
    <el-card shadow="never" class="panel">
      <template #header><strong>流水线阶段</strong></template>
      <StageStepper :stages="runsStore.stages" @select-artifact="onSelectArtifact" />
    </el-card>

    <!-- 人工审批（运行中内嵌） -->
    <el-card v-if="pendingApproval" shadow="never" class="panel">
      <template #header><strong>人工审批</strong></template>
      <ApprovalCard
        :approval="pendingApproval"
        :deciding="approvalsStore.deciding"
        @decide="onDecide"
        @select-artifact="onSelectArtifact"
      />
    </el-card>

    <!-- 修复时间线（v1.1：按 repair.* 事件折叠「第 N 轮」；无回边时不渲染） -->
    <RepairTimeline
      :rounds="runsStore.repairRounds"
      @select-artifact="onSelectRepairArtifact"
    />

    <!-- 事件日志 -->
    <el-card shadow="never" class="panel">
      <template #header><strong>实时事件</strong></template>
      <LogStream :events="runsStore.events" :connection-status="sseStatus" />
    </el-card>

    <!-- 角色消息 -->
    <el-card shadow="never" class="panel">
      <template #header><strong>角色消息</strong></template>
      <div v-if="runsStore.messages.length === 0" class="panel-empty">暂无角色消息</div>
      <el-timeline v-else>
        <el-timeline-item
          v-for="message in runsStore.messages"
          :key="message.id"
          :timestamp="formatTime(message.created_at)"
          placement="top"
          :type="message.type === 'feedback' ? 'warning' : 'primary'"
        >
          <div class="message-head">
            <el-tag size="small" effect="plain">{{ message.type }}</el-tag>
            <span class="message-roles">
              {{ message.from_role ?? 'system' }} → {{ message.to_role ?? 'broadcast' }}
            </span>
          </div>
          <div v-if="message.summary" class="message-summary">{{ message.summary }}</div>
        </el-timeline-item>
      </el-timeline>
    </el-card>

    <!-- 阶段产物查看（共享对话框） -->
    <ArtifactViewDialog
      v-model:visible="artifactDialogVisible"
      :artifact-id="artifactDialogId"
      :version="artifactDialogVersion"
      :title="artifactDialogTitle"
    />
  </div>
</template>

<style scoped>
.run-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.run-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.run-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.run-id {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.run-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.run-meta {
  margin-top: 4px;
}

.run-error {
  color: #d54941;
  word-break: break-all;
}

.run-error-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.run-load-error {
  margin-bottom: 4px;
}

.panel {
  margin-top: 4px;
}

.panel-empty {
  color: #909399;
  font-size: 13px;
  padding: 16px 0;
  text-align: center;
}

.message-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.message-roles {
  font-size: 12px;
  color: #606266;
}

.message-summary {
  margin-top: 4px;
  font-size: 13px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
