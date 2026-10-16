<!--
  审批中心（/approvals）：跨 run 的审批列表（待审批 / 已通过 / 已驳回）。

  列表项已内联 run 摘要与历史反馈链（后端聚合，PM-12），无需再逐条拉详情；
  待审产物点击经共享 ArtifactViewDialog 查看内容（PM-08）。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import ApprovalCard from '@/components/ApprovalCard.vue'
import ArtifactViewDialog from '@/components/ArtifactViewDialog.vue'
import { useApprovalsStore } from '@/stores/approvals'
import type { Approval, ApprovalStatus, ArtifactRef } from '@/api/types'

const router = useRouter()
const approvalsStore = useApprovalsStore()

const activeFilter = ref<ApprovalStatus | 'all'>('pending')

const FILTERS: Array<{ value: ApprovalStatus | 'all'; label: string }> = [
  { value: 'pending', label: '待审批' },
  { value: 'approved', label: '已通过' },
  { value: 'rejected', label: '已驳回' },
  { value: 'all', label: '全部' },
]

const items = computed<Approval[]>(() => approvalsStore.items)
const pendingCount = computed(() => approvalsStore.pendingCount)

/** 待审产物查看对话框。 */
const dialogVisible = ref(false)
const dialogArtifactId = ref('')
const dialogVersion = ref<number | null>(null)
const dialogTitle = ref('')

onMounted(async () => {
  await load()
})

async function load(): Promise<void> {
  await approvalsStore.loadApprovals({
    status: activeFilter.value === 'all' ? '' : activeFilter.value,
  })
}

async function onFilterChange(value: ApprovalStatus | 'all'): Promise<void> {
  activeFilter.value = value
  await load()
}

async function onDecide(
  payload: { decision: 'approve' | 'reject'; feedback?: string },
  approvalId: string,
): Promise<void> {
  try {
    await approvalsStore.decide(approvalId, payload)
    ElMessage.success(payload.decision === 'approve' ? '已通过' : '已驳回，将带反馈回退重跑')
    await load()
  } catch (e) {
    ElMessage.error(`决策失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

function onGotoRun(runId: string): void {
  void router.push(`/runs/${runId}`)
}

/** 查看待审产物内容（PM-08）。 */
function onSelectArtifact(artifactId: string, approval: Approval): void {
  const ref = (approval.artifact_refs as ArtifactRef[]).find(
    (item) => String(item.artifact_id) === artifactId,
  )
  const name = ref?.name ?? artifactId
  dialogArtifactId.value = artifactId
  dialogVersion.value = typeof ref?.version === 'number' ? ref.version : null
  dialogTitle.value = ref?.version ? `${name} v${ref.version}` : name
  dialogVisible.value = true
}
</script>

<template>
  <div class="approval-center">
    <div class="center-header">
      <h2 class="center-title">
        审批中心
        <el-badge v-if="pendingCount > 0" :value="pendingCount" type="warning" class="pending-badge" />
      </h2>
      <el-radio-group :model-value="activeFilter" @change="onFilterChange">
        <el-radio-button v-for="filter in FILTERS" :key="filter.value" :value="filter.value">
          {{ filter.label }}
        </el-radio-button>
      </el-radio-group>
    </div>

    <div v-loading="approvalsStore.loading" class="card-list">
      <template v-if="items.length > 0">
        <div v-for="item in items" :key="item.id" class="card-wrapper">
          <ApprovalCard
            :approval="item"
            :deciding="approvalsStore.deciding"
            @decide="(payload) => onDecide(payload, item.id)"
            @select-artifact="(id) => onSelectArtifact(id, item)"
          />
          <el-button link type="primary" size="small" class="goto-run" @click="onGotoRun(item.run_id)">
            前往运行详情 →
          </el-button>
        </div>
      </template>
      <el-empty v-else description="暂无审批记录" :image-size="80" />
    </div>

    <ArtifactViewDialog
      v-model:visible="dialogVisible"
      :artifact-id="dialogArtifactId"
      :version="dialogVersion"
      :title="dialogTitle"
    />
  </div>
</template>

<style scoped>
.center-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.center-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.pending-badge {
  margin-top: 2px;
}

.card-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.card-wrapper {
  position: relative;
}

.goto-run {
  position: absolute;
  top: 14px;
  right: 16px;
}
</style>
