<!--
  运行详情（/runs/:id）：阶段看板 + 人工审批（内嵌卡片，验收点①②③）+
  事件日志 + 角色消息 + 阶段产物。

  SSE（useRunEvents）驱动实时刷新；断线由浏览器自动重连（Last-Event-ID），
  服务端按 seq 重放，前端按 seq 去重。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'

import ApprovalCard from '@/components/ApprovalCard.vue'
import LogStream from '@/components/LogStream.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import StageStepper from '@/components/StageStepper.vue'
import { useRunEvents, type RunEvent, type SseStatus } from '@/api/sse'
import { useApprovalsStore } from '@/stores/approvals'
import { useArtifactsStore } from '@/stores/artifacts'
import { useRunsStore } from '@/stores/runs'
import { RUN_MODE_LABELS, RUN_STATUS_LABELS, RUN_STATUS_TAG_TYPES } from '@/constants'
import type { ApprovalDetail, ArtifactContent, RunAction, RunMessage } from '@/api/types'

const route = useRoute()
const runsStore = useRunsStore()
const approvalsStore = useApprovalsStore()
const artifactsStore = useArtifactsStore()

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

/** 阶段产物查看对话框。 */
const artifactDialogVisible = ref(false)
const artifactDialogTitle = ref('')
const selectedPath = ref<string | null>(null)

async function onSelectArtifact(artifactId: string): Promise<void> {
  const artifact = runsStore.stages
    .flatMap((s) => s.artifacts)
    .find((a) => a.artifact_id === artifactId)
  artifactDialogTitle.value = artifact ? `${artifact.name} v${artifact.version}` : artifactId
  selectedPath.value = null
  artifactDialogVisible.value = true
  if (artifact) {
    await artifactsStore.loadVersionContent(artifactId, artifact.version)
  }
}

async function onSelectFile(path: string): Promise<void> {
  const artifact = runsStore.stages
    .flatMap((s) => s.artifacts)
    .find((a) => a.artifact_id === artifactsStore.content?.artifact_id)
  if (!artifact) return
  selectedPath.value = path
  await artifactsStore.loadVersionContent(artifact.artifact_id, artifact.version, path)
}

const content = computed<ArtifactContent | null>(() => artifactsStore.content)

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <div v-loading="runsStore.loading" class="run-detail">
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
        <span class="run-error">{{ run.error }}</span>
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

    <!-- 阶段产物查看 -->
    <el-dialog v-model="artifactDialogVisible" :title="artifactDialogTitle" width="720px">
      <div class="artifact-dialog">
        <div class="file-list">
          <div class="file-list-title">文件清单</div>
          <div v-if="!content?.files?.length" class="panel-empty">（空产物）</div>
          <el-button
            v-for="file in content?.files ?? []"
            :key="file"
            link
            :type="selectedPath === file ? 'primary' : 'info'"
            @click="onSelectFile(file)"
          >
            {{ file }}
          </el-button>
        </div>
        <div class="file-content">
          <div class="file-list-title">
            {{ selectedPath ? `内容：${selectedPath}` : '选择文件查看内容' }}
          </div>
          <MarkdownView :content="selectedPath ? content?.content ?? '' : ''" :loading="artifactsStore.loading" />
        </div>
      </div>
    </el-dialog>
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

.artifact-dialog {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 16px;
  max-height: 60vh;
}

.file-list {
  border-right: 1px solid #ebeef5;
  padding-right: 12px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.file-list-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 8px;
}

.file-content {
  overflow-y: auto;
}
</style>
