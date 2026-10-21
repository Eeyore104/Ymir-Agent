<!--
  评测报告（/evaluation）：四维评分 + 证据链 + 通过判定（§12.3）。

  数据源：后端评测报告一等产物（§12.4）。列表经 ``/evaluations``（全局）或
  ``/projects/{id}/evaluations``（项目）；**详情经 ``GET /evaluations/{id}``**。
  工具栏 / 空态可「发起四维评测」（POST /projects/{id}/evaluations，PM-07）。

  降级维度（degraded=true，评估器异常 / 沙箱不可用）显著标记（QA-10 / PM-07）。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  fetchEvaluationDetail,
  fetchEvaluations,
  fetchProjectEvaluations,
  runEvaluation,
} from '@/api/evaluations'
import { useProjectsStore } from '@/stores/projects'
import type { EvaluationReport, EvaluationSummary } from '@/api/types'

const route = useRoute()
const router = useRouter()
const projectsStore = useProjectsStore()

/** 路由携带的项目上下文（有则列表按项目、发起评测锁定该项目）。 */
const scopedProjectId = typeof route.query.project_id === 'string' ? route.query.project_id : ''

const summaries = ref<EvaluationSummary[]>([])
const loading = ref(false)
const detail = ref<EvaluationReport | null>(null)
const selected = ref<EvaluationSummary | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)

/** 发起评测弹窗。 */
const dialogVisible = ref(false)
const submitting = ref(false)
const form = ref({
  projectId: scopedProjectId,
  artifactName: 'code',
  suiteId: 'default',
  version: '',
})

/** 报告中是否存在降级维度（分数不可信，需前端提示）。 */
const anyDegraded = computed<boolean>(
  () =>
    detail.value?.tasks.some((task) => task.dimensions.some((dim) => dim.degraded)) ?? false,
)

// --- 追溯徽标（v1.1 R6）：四字段来自报告 JSON，缺省时降级显示而不报错 ---

/** 关联 run（截断显示，title 给全量）。 */
const traceRunId = computed<string>(() => detail.value?.run_id ?? '')
const runIdShort = computed<string>(() => (traceRunId.value ? traceRunId.value.slice(0, 8) : '—'))

/** 被评产物版本（`code v2`）。 */
const traceVersion = computed<number | null>(() => detail.value?.artifact_version ?? null)

/** 第几轮修复（0 = 首轮）。 */
const traceRound = computed<number | null>(() => detail.value?.repair_round ?? null)

/** 裁决来源：harness（绿，唯一裁决源）/ llm_qa（灰，仅叙事）。 */
const traceVerdictSource = computed<'harness' | 'llm_qa'>(
  () => (detail.value?.verdict_source === 'llm_qa' ? 'llm_qa' : 'harness'),
)

onMounted(async () => {
  await projectsStore.loadProjects()
  if (!form.value.projectId) form.value.projectId = projectsStore.items[0]?.id ?? ''
  await loadList()
})

async function loadList(): Promise<void> {
  loading.value = true
  try {
    summaries.value = scopedProjectId
      ? await fetchProjectEvaluations(scopedProjectId)
      : await fetchEvaluations()
    if (summaries.value.length > 0) {
      await open(summaries.value[0])
    } else {
      detail.value = null
      selected.value = null
    }
  } catch (e) {
    ElMessage.error(`加载评测列表失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    loading.value = false
  }
}

/** 打开报告详情（GET /evaluations/{id}；失败 -> 内联错误态 + 重试）。 */
async function open(row: EvaluationSummary): Promise<void> {
  selected.value = row
  detailLoading.value = true
  detailError.value = null
  try {
    detail.value = await fetchEvaluationDetail(row.artifact_id)
  } catch (e) {
    detail.value = null
    detailError.value = e instanceof Error ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

function retryDetail(): void {
  if (selected.value) void open(selected.value)
}

function openDialog(): void {
  form.value.projectId = form.value.projectId || projectsStore.items[0]?.id || ''
  dialogVisible.value = true
}

/** 发起四维评测（POST /projects/{id}/evaluations）。 */
async function submitEvaluation(): Promise<void> {
  if (!form.value.projectId) {
    ElMessage.warning('请先选择项目')
    return
  }
  submitting.value = true
  try {
    const report = await runEvaluation(form.value.projectId, {
      suite_id: form.value.suiteId.trim() || 'default',
      artifact_name: form.value.artifactName.trim() || 'code',
      version: form.value.version.trim() ? Number(form.value.version) : null,
      save: true,
    })
    ElMessage.success(
      `评测完成：总分 ${report.total_score.toFixed(2)}（${report.passed ? '通过' : '未通过'}）`,
    )
    dialogVisible.value = false
    await loadList()
  } catch (e) {
    ElMessage.error(`发起评测失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    submitting.value = false
  }
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}

function gotoArtifacts(): void {
  void router.push(scopedProjectId ? `/artifacts?project_id=${scopedProjectId}` : '/artifacts')
}
</script>

<template>
  <div v-loading="loading" class="eval-report">
    <div class="page-header">
      <h2 class="page-title">评测报告</h2>
      <div class="header-actions">
        <el-button type="primary" size="small" @click="openDialog">发起四维评测</el-button>
        <el-button size="small" @click="loadList">刷新</el-button>
      </div>
    </div>

    <el-empty
      v-if="summaries.length === 0"
      description="暂无评测报告（可对项目产物发起评测）"
    >
      <el-button type="primary" @click="openDialog">立即评测</el-button>
      <el-button @click="gotoArtifacts">前往产物库</el-button>
    </el-empty>

    <template v-else>
      <el-card shadow="never" class="panel">
        <template #header><strong>报告列表</strong></template>
        <el-table :data="summaries" border size="small" @row-click="open">
          <el-table-column prop="project_id" label="项目" width="140" show-overflow-tooltip />
          <el-table-column prop="suite_id" label="套件" width="120" />
          <el-table-column prop="version" label="版本" width="70" align="center" />
          <el-table-column prop="total_score" label="总分" width="90" align="center" />
          <el-table-column prop="runnability" label="可运行率" width="100" align="center" />
          <el-table-column label="判定" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="row.passed ? 'success' : 'danger'">
                {{ row.passed ? '通过' : '未通过' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="生成时间" min-width="160">
            <template #default="{ row }">{{ formatTime(row.generated_at) }}</template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 详情加载失败：内联错误态 + 重试（PM-11） -->
      <el-alert
        v-if="detailError"
        type="error"
        :closable="false"
        show-icon
        title="加载报告详情失败"
        :description="detailError"
      >
        <template #default>
          <el-button size="small" type="primary" @click="retryDetail">重试</el-button>
        </template>
      </el-alert>

      <template v-if="detail">
        <el-alert
          v-if="anyDegraded"
          type="warning"
          :closable="false"
          show-icon
          title="存在降级维度"
          description="部分维度因评估器异常或沙箱不可用而降级（标记「降级」），其分数仅供参考。"
        />

        <el-card shadow="never" class="panel">
          <template #header>
            <div class="card-header">
              <strong>
                报告详情（{{ selected?.artifact_id?.slice(0, 8) }} v{{ selected?.version }}）
              </strong>
              <el-tag :type="detail.passed ? 'success' : 'danger'">
                {{ detail.passed ? '通过' : '未通过' }}
              </el-tag>
            </div>
          </template>

          <!-- 追溯徽标（v1.1 R6）：裁决必须能追到 run → 产物版本 → 修复轮次 → 来源 -->
          <div class="trace-badges">
            <el-tag size="small" type="info" effect="plain" :title="traceRunId">
              run {{ runIdShort }}
            </el-tag>
            <el-tag v-if="traceVersion !== null" size="small" type="info" effect="plain">
              code v{{ traceVersion }}
            </el-tag>
            <el-tag v-if="traceRound !== null" size="small" type="info" effect="plain">
              第 {{ traceRound }} 轮修复
            </el-tag>
            <el-tag
              size="small"
              :type="traceVerdictSource === 'harness' ? 'success' : 'info'"
              effect="plain"
            >
              裁决来源 {{ traceVerdictSource === 'harness' ? 'harness' : 'llm_qa' }}
            </el-tag>
            <el-tag v-if="selected?.kind === 'eval_verdict'" size="small" type="success" effect="dark">
              eval_verdict
            </el-tag>
          </div>

          <div class="metric-grid">
            <div class="metric">
              <div class="metric-value">{{ detail.total_score.toFixed(2) }}</div>
              <div class="metric-label">加权总分（门槛 {{ detail.thresholds.total }}）</div>
            </div>
            <div class="metric">
              <div class="metric-value">{{ detail.runnability.toFixed(2) }}</div>
              <div class="metric-label">可运行率（门槛 {{ detail.thresholds.runnability }}）</div>
            </div>
            <div class="metric">
              <div class="metric-value">¥{{ detail.cost_cny.toFixed(4) }}</div>
              <div class="metric-label">评测成本</div>
            </div>
            <div class="metric">
              <div class="metric-value">{{ detail.total_tokens }}</div>
              <div class="metric-label">judge tokens</div>
            </div>
          </div>
        </el-card>

        <el-card v-for="task in detail.tasks" :key="task.id" shadow="never" class="panel">
          <template #header>
            <div class="card-header">
              <strong>任务 {{ task.id }}（总分 {{ task.total_score.toFixed(2) }}）</strong>
              <el-tag size="small" :type="task.passed ? 'success' : 'danger'">
                {{ task.passed ? '通过' : '未通过' }}
              </el-tag>
            </div>
          </template>
          <div v-if="task.requirement" class="task-requirement">需求：{{ task.requirement }}</div>

          <div v-for="dim in task.dimensions" :key="dim.name" class="dimension">
            <div class="dimension-head">
              <span class="dimension-name">{{ dim.label }}</span>
              <el-progress
                :percentage="Math.round(dim.score)"
                :stroke-width="10"
                style="width: 260px"
              />
              <span class="dimension-score">{{ dim.score.toFixed(2) }}</span>
              <el-tag size="small" effect="plain">权重 {{ (dim.weight * 100).toFixed(0) }}%</el-tag>
              <el-tag v-if="dim.degraded" size="small" type="warning" effect="dark">降级</el-tag>
            </div>
            <div class="dimension-rationale">{{ dim.rationale }}</div>
            <ul class="evidence-list">
              <li v-for="(item, index) in dim.evidence" :key="index">
                <el-tag size="small" effect="plain">{{ item.kind }}</el-tag>
                <span class="evidence-ref">{{ item.ref }}</span>
                <span class="evidence-detail">{{ item.detail }}</span>
              </li>
            </ul>
          </div>
        </el-card>
      </template>
    </template>

    <!-- 发起评测弹窗 -->
    <el-dialog v-model="dialogVisible" title="发起四维评测" width="520px">
      <el-form label-width="90px">
        <el-form-item label="项目">
          <el-select
            v-model="form.projectId"
            :disabled="!!scopedProjectId"
            placeholder="选择项目"
            style="width: 100%"
          >
            <el-option
              v-for="project in projectsStore.items"
              :key="project.id"
              :label="project.name"
              :value="project.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="目标产物">
          <el-input v-model="form.artifactName" placeholder="code" />
        </el-form-item>
        <el-form-item label="评测套件">
          <el-input v-model="form.suiteId" placeholder="default" />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="form.version" placeholder="留空 = 最新版本" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitEvaluation">发起评测</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.eval-report {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.page-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.panel {
  margin-bottom: 4px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* 追溯徽标（v1.1 R6）：一行只读元信息，不抢主体分数 */
.trace-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.metric-grid {
  display: grid;  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}

.metric {
  padding: 14px;
  background: #f5f7fa;
  border-radius: 6px;
  text-align: center;
}

.metric-value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
}

.metric-label {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}

.task-requirement {
  font-size: 13px;
  color: #606266;
  margin-bottom: 12px;
  white-space: pre-wrap;
}

.dimension {
  padding: 10px 0;
  border-top: 1px solid #ebeef5;
}

.dimension-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.dimension-name {
  width: 120px;
  font-weight: 600;
  color: #303133;
}

.dimension-score {
  font-weight: 600;
  color: #409eff;
}

.dimension-rationale {
  margin: 6px 0;
  font-size: 12px;
  color: #909399;
  white-space: pre-wrap;
}

.evidence-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: #606266;
}

.evidence-list li {
  margin: 4px 0;
  line-height: 1.6;
}

.evidence-ref {
  margin: 0 6px;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  color: #303133;
}

.evidence-detail {
  color: #909399;
}
</style>
