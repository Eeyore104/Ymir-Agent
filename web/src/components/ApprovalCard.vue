<!--
  审批卡片（docs/02 §3.8 三关 HITL 交互核心）。

  内容：关卡 / 运行摘要 / 待审产物（artifact_refs）/ 历史反馈链 / 决策操作。
  - 通过：直接放行，引擎补推进到下一阶段；
  - 驳回：反馈文本经 Message(type=feedback) 注入目标角色上下文，
    引擎作废该阶段最新完成记录 -> attempt+1 重跑 -> 产物版本递增。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'

import type { Approval, ApprovalDecision, ArtifactRef, FeedbackItem } from '@/api/types'
import {
  APPROVAL_STATUS_LABELS,
  APPROVAL_STATUS_TAG_TYPES,
  ROLE_LABELS,
  RUN_MODE_LABELS,
  STAGE_META,
} from '@/constants'

interface Props {
  /** 审批记录（含 run 摘要与历史反馈链）；null 时渲染空态。 */
  approval: Approval | null
  /** 决策请求进行中（父组件 Store 的 deciding）。 */
  deciding?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  approval: null,
  deciding: false,
})

const emit = defineEmits<{
  (e: 'decide', payload: ApprovalDecision): void
  (e: 'select-artifact', artifactId: string): void
}>()

/** 驳回反馈输入（弹层内编辑）。 */
const rejectFeedback = ref('')
const rejectPanelOpen = ref(false)
const FEEDBACK_MAX_LENGTH = 5000

const isPending = computed(() => props.approval?.status === 'pending')

/** 待审产物引用（output_refs = {artifact_id, name, type, version} 列表）。 */
const artifactRefs = computed<ArtifactRef[]>(() => {
  const refs = props.approval?.artifact_refs ?? []
  return refs.filter(
    (ref): ref is ArtifactRef =>
      typeof ref === 'object' && ref !== null && 'artifact_id' in ref,
  )
})

const stageRole = computed<string>(() => {
  const stageId = props.approval?.stage_id ?? ''
  const meta = STAGE_META.find((s) => s.id === stageId)
  return meta ? ROLE_LABELS[meta.role] ?? meta.role : stageId
})

const statusTagType = computed<'info' | 'success' | 'warning' | 'danger'>(() =>
  props.approval ? APPROVAL_STATUS_TAG_TYPES[props.approval.status] : 'info',
)

const statusLabel = computed<string>(() =>
  props.approval ? APPROVAL_STATUS_LABELS[props.approval.status] : '-',
)

const modeLabel = computed<string>(() => {
  const mode = props.approval?.run?.mode ?? ''
  return RUN_MODE_LABELS[mode as keyof typeof RUN_MODE_LABELS] ?? mode
})

function onApprove(): void {
  emit('decide', { decision: 'approve' })
}

function openRejectPanel(): void {
  rejectFeedback.value = ''
  rejectPanelOpen.value = true
}

function onConfirmReject(): void {
  const feedback = rejectFeedback.value.trim()
  if (!feedback) {
    return // 前端校验：反馈为空时确认按钮为禁用态，双保险
  }
  emit('decide', { decision: 'reject', feedback })
  rejectPanelOpen.value = false
}

function onCancelReject(): void {
  rejectPanelOpen.value = false
  rejectFeedback.value = ''
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}

function feedbackFrom(item: FeedbackItem): string {
  return `${item.from_role ?? '-'} → ${item.to_role ?? '-'}`
}

/** 历史反馈链（列表接口已内联；缺省空数组）。 */
const feedbackHistory = computed<FeedbackItem[]>(() => props.approval?.feedback_history ?? [])
</script>

<template>
  <el-card v-if="approval" class="approval-card" shadow="never">
    <template #header>
      <div class="card-header">
        <span class="gate-name">{{ approval.gate_name }}</span>
        <el-tag size="small" :type="statusTagType">{{ statusLabel }}</el-tag>
      </div>
    </template>

    <!-- 运行摘要 -->
    <el-descriptions :column="2" size="small" border>
      <el-descriptions-item label="项目">
        {{ approval.run?.project_name ?? approval.run_id }}
      </el-descriptions-item>
      <el-descriptions-item label="模式">{{ modeLabel || '-' }}</el-descriptions-item>
      <el-descriptions-item label="阶段">
        {{ approval.stage_id }}（{{ stageRole }}）
      </el-descriptions-item>
      <el-descriptions-item label="创建时间">
        {{ formatTime(approval.created_at) }}
      </el-descriptions-item>
      <el-descriptions-item label="需求" :span="2">
        <span class="requirement">{{ approval.run?.requirement ?? '-' }}</span>
      </el-descriptions-item>
    </el-descriptions>

    <!-- 待审产物 -->
    <div class="section">
      <div class="section-title">待审产物</div>
      <div v-if="artifactRefs.length === 0" class="section-empty">（无产物引用）</div>
      <div v-else class="artifact-list">
        <el-button
          v-for="ref in artifactRefs"
          :key="ref.artifact_id"
          link
          type="primary"
          @click="$emit('select-artifact', String(ref.artifact_id))"
        >
          {{ ref.name ?? ref.artifact_id }}
          <span v-if="ref.version"> v{{ ref.version }}</span>
        </el-button>
      </div>
    </div>

    <!-- 历史反馈链（历次驳回 -> 修订 的轨迹） -->
    <div v-if="feedbackHistory.length > 0" class="section">
      <div class="section-title">历史反馈链</div>
      <el-timeline>
        <el-timeline-item
          v-for="item in feedbackHistory"
          :key="item.id"
          size="normal"
          :timestamp="formatTime(item.created_at)"
          placement="top"
          type="warning"
        >
          <div class="feedback-from">{{ feedbackFrom(item) }}</div>
          <div class="feedback-text">{{ item.summary ?? '-' }}</div>
        </el-timeline-item>
      </el-timeline>
    </div>

    <!-- 已决策结果 -->
    <div v-if="!isPending" class="section">
      <el-alert
        :type="approval.decision === 'reject' ? 'warning' : 'success'"
        :closable="false"
        show-icon
      >
        <template #title>
          {{ approval.decision === 'reject' ? '已驳回' : '已通过' }} ·
          {{ approval.decided_by ?? '-' }} · {{ formatTime(approval.decided_at) }}
        </template>
        <div v-if="approval.feedback" class="decision-feedback">{{ approval.feedback }}</div>
      </el-alert>
    </div>

    <!-- 决策操作 -->
    <div v-if="isPending" class="actions">
      <el-button type="success" :loading="deciding" @click="onApprove">通过</el-button>
      <el-button type="danger" plain :disabled="deciding" @click="openRejectPanel">
        驳回（带反馈回退）
      </el-button>
    </div>

    <!-- 驳回反馈输入 -->
    <div v-if="isPending && rejectPanelOpen" class="reject-panel">
      <el-alert type="info" :closable="false" show-icon>
        驳回反馈将注入角色上下文（prompt「历史反馈」节），驱动对应角色修订后重跑，
        产物会生成新版本。
      </el-alert>
      <el-input
        v-model="rejectFeedback"
        type="textarea"
        :rows="4"
        maxlength="5000"
        show-word-limit
        placeholder="请填写驳回原因与修改要求（必填）…"
      />
      <div class="reject-actions">
        <span v-if="rejectFeedback.trim().length === 0" class="reject-hint">
          反馈不能为空（将作为角色修订依据）
        </span>
        <span class="reject-buttons">
          <el-button size="small" @click="onCancelReject">取消</el-button>
          <el-button
            size="small"
            type="danger"
            :loading="deciding"
            :disabled="rejectFeedback.trim().length === 0 || rejectFeedback.length > FEEDBACK_MAX_LENGTH"
            @click="onConfirmReject"
          >
            确认驳回
          </el-button>
        </span>
      </div>
    </div>
  </el-card>

  <el-empty v-else description="暂无审批记录" :image-size="60" />
</template>

<style scoped>
.approval-card {
  margin-bottom: 12px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.gate-name {
  font-weight: 600;
  color: #303133;
}

.section {
  margin-top: 14px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 6px;
}

.section-empty {
  font-size: 12px;
  color: #909399;
}

.artifact-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.requirement {
  color: #606266;
}

.feedback-from {
  font-size: 12px;
  color: #909399;
}

.feedback-text {
  font-size: 13px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
}

.decision-feedback {
  margin-top: 4px;
}

.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}

.reject-panel {
  margin-top: 12px;
  padding: 12px;
  background: #fdf6ec;
  border-radius: 6px;
}

.reject-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.reject-hint {
  font-size: 12px;
  color: #e6a23c;
}

.reject-buttons {
  display: flex;
  gap: 8px;
}
</style>
