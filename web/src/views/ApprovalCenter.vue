<!--
  审批中心（/approvals）：跨 run 的审批列表（待审批 / 已通过 / 已驳回）。

  列表项聚合 run 摘要；卡片级历史反馈链按需拉取详情（并行，数量可控）。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import ApprovalCard from '@/components/ApprovalCard.vue'
import { useApprovalsStore } from '@/stores/approvals'
import type { ApprovalDetail, ApprovalStatus } from '@/api/types'

const router = useRouter()
const approvalsStore = useApprovalsStore()

/** 详情富化后的卡片列表。 */
const details = ref<ApprovalDetail[]>([])
const activeFilter = ref<ApprovalStatus | 'all'>('pending')

const FILTERS: Array<{ value: ApprovalStatus | 'all'; label: string }> = [
  { value: 'pending', label: '待审批' },
  { value: 'approved', label: '已通过' },
  { value: 'rejected', label: '已驳回' },
  { value: 'all', label: '全部' },
]

const pendingCount = computed(() => approvalsStore.pendingCount)

onMounted(async () => {
  await load()
})

async function load(): Promise<void> {
  await approvalsStore.loadApprovals({
    status: activeFilter.value === 'all' ? '' : activeFilter.value,
  })
  // 并行拉取详情（含历史反馈链）；失败项退化为列表数据
  const rich = await Promise.all(
    approvalsStore.items.map(async (item) => {
      const detail = await approvalsStore.loadApproval(item.id)
      return detail ?? ({ ...item, feedback_history: [] } as ApprovalDetail)
    }),
  )
  details.value = rich
}

async function onFilterChange(value: ApprovalStatus | 'all'): Promise<void> {
  activeFilter.value = value
  await load()
}

async function onDecide(payload: { decision: 'approve' | 'reject'; feedback?: string }, approvalId: string): Promise<void> {
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
      <template v-if="details.length > 0">
        <div v-for="detail in details" :key="detail.id" class="card-wrapper">
          <ApprovalCard
            :approval="detail"
            :deciding="approvalsStore.deciding"
            @decide="(payload) => onDecide(payload, detail.id)"
          />
          <el-button link type="primary" size="small" class="goto-run" @click="onGotoRun(detail.run_id)">
            前往运行详情 →
          </el-button>
        </div>
      </template>
      <el-empty v-else description="暂无审批记录" :image-size="80" />
    </div>
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
