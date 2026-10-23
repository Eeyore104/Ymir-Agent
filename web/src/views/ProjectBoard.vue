<!--
  项目看板（/）：创建项目 / 项目列表 / 发起运行（选 HITL 模式 + 模板 + 基线）。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { fetchArtifact, fetchProjectArtifacts, uploadBaseline } from '@/api/artifacts'
import { fetchTemplate, fetchTemplates } from '@/api/templates'
import { useProjectsStore, messageOf } from '@/stores/projects'
import { useRunsStore } from '@/stores/runs'
import { RUN_MODE_DESCRIPTIONS, RUN_MODE_LABELS } from '@/constants'
import type { Artifact, RunMode, TemplateSummary } from '@/api/types'
import {
  NO_TEMPLATE_VALUE,
  buildTemplateOptions,
  summarizeTemplate,
  type TemplateOption,
  type TemplateSummaryView,
} from '@/utils/templates'

const router = useRouter()
const projectsStore = useProjectsStore()
const runsStore = useRunsStore()

onMounted(() => {
  void projectsStore.loadProjects()
})

/** 创建项目对话框。 */
const createDialogVisible = ref(false)
const createForm = reactive({ name: '', description: '' })
const creating = ref(false)

async function onCreateProject(): Promise<void> {
  if (!createForm.name.trim()) {
    ElMessage.warning('请填写项目名称')
    return
  }
  creating.value = true
  try {
    await projectsStore.createProject(createForm.name.trim(), createForm.description.trim() || undefined)
    ElMessage.success('项目已创建')
    createDialogVisible.value = false
    createForm.name = ''
    createForm.description = ''
  } catch (e) {
    ElMessage.error(`创建失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    creating.value = false
  }
}

/** 发起运行对话框。 */
const runDialogVisible = ref(false)
const runForm = reactive<{
  projectId: string
  projectName: string
  requirement: string
  mode: RunMode
  /** 模板 id（空 = 不使用模板 / 通用模式）。 */
  templateId: string
  /** 基线引用 ``<artifactId>#<version>``（空 = 从零生成）。 */
  baselineRef: string
}>({
  projectId: '',
  projectName: '',
  requirement: '',
  mode: 'key_node',
  templateId: NO_TEMPLATE_VALUE,
  baselineRef: '',
})
const launching = ref(false)

const MODE_OPTIONS: RunMode[] = ['key_node', 'batch', 'debug']

// --- 模板（T4.2）：下拉选项 + 摘要卡片 ---

const templateOptions = ref<TemplateOption[]>([])
const templateDetail = ref<TemplateSummary | null>(null)
const templateLoading = ref(false)
let templatesLoaded = false

/** 摘要卡片数据（目录规范 / 依赖白名单 / 检查命令条数 + 路径清单）。 */
const templateSummary = computed<TemplateSummaryView>(() => summarizeTemplate(templateDetail.value))

/** 下拉里被置灰的模板（坏模板）——渲染时把原因显示在选项右侧。 */
const disabledOptionReason = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const option of templateOptions.value) {
    if (option.disabled && option.reason) map[option.value] = option.reason
  }
  return map
})

/** 加载模板列表（只加载一次；坏模板不影响整体）。 */
async function loadTemplateOptions(): Promise<void> {
  if (templatesLoaded) return
  try {
    const list = await fetchTemplates()
    templateOptions.value = buildTemplateOptions(list)
    templatesLoaded = true
  } catch (e) {
    ElMessage.error(`模板列表加载失败：${messageOf(e)}`)
  }
}

// 选中模板 -> 拉详情（**只含路径清单，不含骨架正文**）
watch(
  () => runForm.templateId,
  async (templateId: string) => {
    if (!templateId) {
      templateDetail.value = null
      return
    }
    templateLoading.value = true
    try {
      templateDetail.value = await fetchTemplate(templateId)
    } catch (e) {
      templateDetail.value = null
      ElMessage.error(`模板详情加载失败：${messageOf(e)}`)
    } finally {
      templateLoading.value = false
    }
  },
)

// --- 基线（T4.2）：本项目 type=baseline 的产物版本 ---

/** 基线下拉选项（一个产物版本一项）。 */
interface BaselineOption {
  /** ``<artifactId>#<version>`` */
  value: string
  label: string
  artifactId: string
  version: number
}

const baselineOptions = ref<BaselineOption[]>([])
const baselineLoading = ref(false)
const uploading = ref(false)
const baselineInput = ref<HTMLInputElement | null>(null)

/** 当前选中的基线版本（null = 从零生成）。 */
const selectedBaseline = computed<BaselineOption | null>(
  () => baselineOptions.value.find((option) => option.value === runForm.baselineRef) ?? null,
)

/** 模式徽标：选了基线 = 增量，否则从零。 */
const isIncremental = computed<boolean>(() => selectedBaseline.value !== null)

/** 加载本项目基线版本（type=baseline）；失败时降级为空列表并提示。 */
async function loadBaselineOptions(projectId: string): Promise<void> {
  if (!projectId) {
    baselineOptions.value = []
    return
  }
  baselineLoading.value = true
  try {
    const artifacts = await fetchProjectArtifacts(projectId)
    const baselines: Artifact[] = artifacts.filter((item) => item.type === 'baseline')
    const details = await Promise.all(
      baselines.map(async (item) => {
        try {
          return await fetchArtifact(item.id)
        } catch {
          // 单个基线详情失败不影响其它基线的展示
          return null
        }
      }),
    )
    const options: BaselineOption[] = []
    for (const detail of details) {
      if (!detail) continue
      for (const version of detail.versions) {
        options.push({
          value: `${detail.id}#${version.version}`,
          label: `${detail.name || 'baseline'} · v${version.version}（${version.file_count} 个文件）`,
          artifactId: detail.id,
          version: version.version,
        })
      }
    }
    options.sort((left, right) =>
      left.artifactId === right.artifactId
        ? right.version - left.version
        : left.artifactId.localeCompare(right.artifactId),
    )
    baselineOptions.value = options
  } catch (e) {
    baselineOptions.value = []
    ElMessage.error(`基线列表加载失败：${messageOf(e)}`)
  } finally {
    baselineLoading.value = false
  }
}

/** 触发文件选择（上传基线 zip）。 */
function pickBaselineFile(): void {
  baselineInput.value?.click()
}

/**
 * 上传基线（**原始字节**，``Content-Type: application/zip``）。
 *
 * 后端刻意不引入 python-multipart，因此**禁止 FormData**。
 */
async function onBaselineFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files && input.files.length > 0 ? input.files[0] : null
  // 清空 value：允许重复选择同一个文件
  input.value = ''
  if (!file || !runForm.projectId) return
  uploading.value = true
  try {
    const result = await uploadBaseline(runForm.projectId, file)
    ElMessage.success(`基线已上传：v${result.version}（${result.file_count} 个文件）`)
    await loadBaselineOptions(runForm.projectId)
    runForm.baselineRef = `${result.artifact_id}#${result.version}`
  } catch (e) {
    ElMessage.error(`基线上传失败：${messageOf(e)}`)
  } finally {
    uploading.value = false
  }
}

function openRunDialog(projectId: string, projectName: string): void {
  runForm.projectId = projectId
  runForm.projectName = projectName
  runForm.requirement = ''
  runForm.mode = 'key_node'
  runForm.templateId = NO_TEMPLATE_VALUE
  runForm.baselineRef = ''
  templateDetail.value = null
  baselineOptions.value = []
  runDialogVisible.value = true
  void loadTemplateOptions()
  void loadBaselineOptions(projectId)
}

async function onLaunchRun(): Promise<void> {
  if (!runForm.requirement.trim()) {
    ElMessage.warning('请填写需求描述')
    return
  }
  launching.value = true
  try {
    const baseline = selectedBaseline.value
    const runId = await runsStore.startRun(runForm.projectId, {
      requirement: runForm.requirement.trim(),
      mode: runForm.mode,
      template_id: runForm.templateId || null,
      baseline_artifact_id: baseline?.artifactId ?? null,
      baseline_version: baseline?.version ?? null,
    })
    runDialogVisible.value = false
    ElMessage.success(
      isIncremental.value ? '已按增量模式发起运行，正在跳转详情…' : '运行已发起，正在跳转详情…',
    )
    await router.push(`/runs/${runId}`)
  } catch (e) {
    ElMessage.error(`发起失败：${messageOf(e)}`)
  } finally {
    launching.value = false
  }
}
</script>

<template>
  <div class="project-board">
    <div class="board-header">
      <h2 class="board-title">项目看板</h2>
      <el-button type="primary" @click="createDialogVisible = true">新建项目</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="board-tip"
      title="一句话需求 -> PRD -> 架构 -> 代码 -> 评测 的四角色流水线；关键节点人工审批，可驳回回退。"
    />

    <el-table :data="projectsStore.items" v-loading="projectsStore.loading" border stripe>
      <el-table-column prop="name" label="项目" min-width="160" />
      <el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip />
      <el-table-column prop="run_count" label="运行数" width="90" align="center">
        <template #default="{ row }">
          {{ row.run_count ?? '-' }}
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">
          {{ new Date(row.created_at).toLocaleString('zh-CN', { hour12: false }) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openRunDialog(row.id, row.name)">发起运行</el-button>
          <el-button link type="primary" @click="router.push(`/artifacts?project_id=${row.id}`)">
            产物库
          </el-button>
          <el-button link type="primary" @click="router.push(`/evaluation?project_id=${row.id}`)">
            评测报告
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无项目，点击「新建项目」开始" :image-size="60" />
      </template>
    </el-table>

    <!-- 创建项目 -->
    <el-dialog v-model="createDialogVisible" title="新建项目" width="480px">
      <el-form label-width="72px">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" maxlength="255" show-word-limit placeholder="项目名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="项目描述（选填）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreateProject">创建</el-button>
      </template>
    </el-dialog>

    <!-- 发起运行 -->
    <el-dialog v-model="runDialogVisible" :title="`发起运行 - ${runForm.projectName}`" width="680px">
      <el-form label-width="88px">
        <el-form-item label="需求" required>
          <el-input
            v-model="runForm.requirement"
            type="textarea"
            :rows="4"
            placeholder="一句话描述要构建的软件，例如：做一个带审批流的任务看板 Web 应用"
          />
        </el-form-item>
        <el-form-item label="HITL 模式" required>
          <el-radio-group v-model="runForm.mode">
            <el-radio-button v-for="mode in MODE_OPTIONS" :key="mode" :value="mode">
              {{ RUN_MODE_LABELS[mode] }}
            </el-radio-button>
          </el-radio-group>
          <div class="mode-desc">{{ RUN_MODE_DESCRIPTIONS[runForm.mode] }}</div>
        </el-form-item>

        <!-- 模板（T4.2 ①：缺省「不使用模板」，坏模板置灰并显示原因） -->
        <el-form-item label="工程模板">
          <el-select
            v-model="runForm.templateId"
            placeholder="选择工程模板"
            style="width: 100%"
            :loading="templateLoading"
          >
            <el-option
              v-for="option in templateOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
              :disabled="option.disabled"
            >
              <span class="option-label">{{ option.label }}</span>
              <span v-if="disabledOptionReason[option.value]" class="option-reason">
                {{ disabledOptionReason[option.value] }}
              </span>
            </el-option>
          </el-select>
          <div class="field-hint">不选 = 通用模式（从零生成，不受模板目录/依赖约束）</div>
        </el-form-item>

        <!-- 模板摘要卡片（T4.2 ②：目录规范 / 依赖白名单 / 检查命令条数） -->
        <div v-if="templateDetail" class="template-card">
          <div class="template-card-title">
            {{ templateDetail.name || templateDetail.id }}
            <span v-if="templateDetail.version" class="template-version">v{{ templateDetail.version }}</span>
          </div>
          <div class="template-metrics">
            <div class="metric">
              <div class="metric-value">{{ templateSummary.requiredDirs.length }}</div>
              <div class="metric-label">目录规范</div>
            </div>
            <div class="metric">
              <div class="metric-value">{{ templateSummary.dependencies.length }}</div>
              <div class="metric-label">依赖白名单</div>
            </div>
            <div class="metric">
              <div class="metric-value">{{ templateSummary.checkCount }}</div>
              <div class="metric-label">检查命令（条）</div>
            </div>
          </div>
          <div v-if="templateSummary.requiredDirs.length" class="template-line">
            目录：{{ templateSummary.requiredDirs.join(' / ') }}
          </div>
          <div v-if="templateSummary.dependencies.length" class="template-line">
            依赖：{{ templateSummary.dependencies.join(', ') }}
          </div>
          <div v-if="templateSummary.reservedPaths.length" class="template-line">
            保留路径（逐字节要求精确）：{{ templateSummary.reservedPaths.join(', ') }}
          </div>
          <div v-if="templateSummary.seedPaths.length" class="template-line">
            种子路径（可整文件重写）：{{ templateSummary.seedPaths.join(', ') }}
          </div>
          <div class="template-note">模板详情只提供路径清单，不含骨架正文</div>
        </div>

        <!-- 基线（T4.2 ③④：选基线 -> 增量模式；上传 zip 走原始字节） -->
        <el-form-item label="基线工程">
          <div class="baseline-row">
            <el-select
              v-model="runForm.baselineRef"
              placeholder="不使用基线（从零生成）"
              clearable
              class="baseline-select"
              :loading="baselineLoading"
            >
              <el-option
                v-for="option in baselineOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
            <el-button :loading="uploading" @click="pickBaselineFile">上传基线 zip</el-button>
            <input
              ref="baselineInput"
              type="file"
              accept=".zip,application/zip"
              class="hidden-input"
              @change="onBaselineFileChange"
            />
          </div>
          <div class="baseline-hint">
            <el-tag size="small" :type="isIncremental ? 'warning' : 'info'" effect="plain">
              {{ isIncremental ? '增量' : '从零' }}
            </el-tag>
            <span v-if="isIncremental" class="hint-text">
              只生成新增/修改，与基线逐字节相同的文件会被丢弃
            </span>
            <span v-else class="hint-text">
              不使用基线：按需求从零生成整套产物
            </span>
          </div>
          <div v-if="!baselineOptions.length" class="field-hint">
            该项目暂无基线快照，可上传现有工程 zip 后改为增量生成
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="runDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="launching" @click="onLaunchRun">发起</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.board-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.board-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.board-tip {
  margin-bottom: 12px;
}

.mode-desc {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
}

.field-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}

.option-label {
  margin-right: 8px;
}

.option-reason {
  font-size: 12px;
  color: #d54941;
}

.template-card {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fafbfc;
  padding: 10px 12px;
  margin: 0 0 16px 88px;
}

.template-card-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}

.template-version {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
  margin-left: 6px;
}

.template-metrics {
  display: flex;
  gap: 24px;
  margin-bottom: 6px;
}

.metric {
  text-align: center;
}

.metric-value {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.metric-label {
  font-size: 12px;
  color: #909399;
}

.template-line {
  font-size: 12px;
  color: #606266;
  line-height: 1.7;
  word-break: break-all;
}

.template-note {
  margin-top: 4px;
  font-size: 12px;
  color: #c0c4cc;
}

.baseline-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.baseline-select {
  flex: 1;
}

.hidden-input {
  display: none;
}

.baseline-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.hint-text {
  font-size: 12px;
  color: #909399;
}
</style>
