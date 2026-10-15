<!--
  评测报告（/evaluation）：四维评分 + 证据链 + 通过判定（§12.3）。

  数据源：后端评测报告一等产物（``type=report``）。列表经 ``/evaluations``（全局）
  或 ``/projects/{id}/evaluations``（项目）；正文经产物内容接口读取
  ``evaluation_report.json`` 结构化渲染。
-->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { fetchVersionContent } from '@/api/artifacts'
import { fetchEvaluations, fetchProjectEvaluations } from '@/api/evaluations'
import type { EvaluationReport, EvaluationSummary } from '@/api/types'

const route = useRoute()
const router = useRouter()

const projectId = typeof route.query.project_id === 'string' ? route.query.project_id : ''

const summaries = ref<EvaluationSummary[]>([])
const loading = ref(false)
const detail = ref<EvaluationReport | null>(null)
const selected = ref<EvaluationSummary | null>(null)

onMounted(loadList)

async function loadList(): Promise<void> {
  loading.value = true
  try {
    summaries.value = projectId
      ? await fetchProjectEvaluations(projectId)
      : await fetchEvaluations()
    if (summaries.value.length > 0) {
      await open(summaries.value[0])
    }
  } catch (e) {
    ElMessage.error(`加载评测列表失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    loading.value = false
  }
}

async function open(row: EvaluationSummary): Promise<void> {
  selected.value = row
  try {
    const content = await fetchVersionContent(row.artifact_id, row.version, 'evaluation_report.json')
    const raw = content.content ?? ''
    detail.value = raw ? (JSON.parse(raw) as EvaluationReport) : null
  } catch (e) {
    detail.value = null
    ElMessage.error(`加载报告正文失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}

function gotoArtifacts(): void {
  void router.push(projectId ? `/artifacts?project_id=${projectId}` : '/artifacts')
}
</script>

<template>
  <div v-loading="loading" class="eval-report">
    <div class="page-header">
      <h2 class="page-title">评测报告</h2>
      <el-button size="small" @click="loadList">刷新</el-button>
    </div>

    <el-empty
      v-if="summaries.length === 0"
      description="暂无评测报告（在项目看板发起运行后，或对产物发起评测）"
    >
      <el-button type="primary" @click="gotoArtifacts">前往产物库</el-button>
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

      <template v-if="detail">
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

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
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
