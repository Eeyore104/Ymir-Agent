<!--
  产物库（/artifacts?project_id=...）：产物列表 / 版本历史 / 版本内容 / 版本 diff
  （验收点④：驳回重跑后的版本递增 lineage 一目了然）。

  v1.2 T4.3 增补：基线面板（vN / 文件数 / hash 前 8 位 / 上传时间）、
  跨产物对比（基线 vs 基线⊕补丁，复用 DiffView）、推送三步对话框。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import BaselinePanel from '@/components/BaselinePanel.vue'
import DiffView from '@/components/DiffView.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import PushExportDialog from '@/components/PushExportDialog.vue'
import { exportProjectZip, fetchArtifact, fetchProjectArtifacts } from '@/api/artifacts'
import { useArtifactsStore } from '@/stores/artifacts'
import { messageOf, useProjectsStore } from '@/stores/projects'
import { buildDiffRef } from '@/utils/baseline'
import type { Artifact, ArtifactDetail, ArtifactVersion } from '@/api/types'

const route = useRoute()
const projectsStore = useProjectsStore()
const artifactsStore = useArtifactsStore()

const projectId = ref<string>('')
const selectedArtifactId = ref<string>('')
const activeTab = ref('history')
const contentVersion = ref<number>(0)
const selectedPath = ref<string | null>(null)
const diffV1 = ref<number>(0)
const diffV2 = ref<number>(0)

/** 项目基线详情（type=baseline 的产物；null = 该项目没有基线）。 */
const baselineDetail = ref<ArtifactDetail | null>(null)
const baselineLoading = ref(false)

/** 推送对话框可见性（T4.3 第三步流程的入口）。 */
const pushDialogVisible = ref(false)

const artifacts = computed<Artifact[]>(() => artifactsStore.items)
const currentArtifact = computed(() => artifactsStore.current)

/** 基线最新版本号（基线对比「一键」按钮用；0 = 无基线）。 */
const latestBaselineVersion = computed<number>(() => baselineDetail.value?.versions.at(-1)?.version ?? 0)

onMounted(async () => {
  await projectsStore.loadProjects()
  const queryProject = route.query.project_id
  projectId.value =
    typeof queryProject === 'string' && queryProject
      ? queryProject
      : (projectsStore.items[0]?.id ?? '')
  if (projectId.value) {
    await artifactsStore.loadProjectArtifacts(projectId.value)
    await loadBaselineDetail(projectId.value)
  }
})

watch(projectId, async (next) => {
  if (!next) return
  selectedArtifactId.value = ''
  baselineDetail.value = null
  artifactsStore.clearDiffAcross()
  await artifactsStore.loadProjectArtifacts(next)
  await loadBaselineDetail(next)
})

/**
 * 加载项目基线详情：在产物列表里找 ``type=baseline`` 的产物，再拉其版本历史。
 *
 * 失败降级为「无基线」并提示，不打断主流程。
 */
async function loadBaselineDetail(targetProjectId: string): Promise<void> {
  if (!targetProjectId) {
    baselineDetail.value = null
    return
  }
  baselineLoading.value = true
  try {
    const list = artifactsStore.items.length
      ? artifactsStore.items
      : await fetchProjectArtifacts(targetProjectId)
    const baseline = list.find((item) => item.type === 'baseline') ?? null
    baselineDetail.value = baseline ? await fetchArtifact(baseline.id) : null
  } catch (e) {
    baselineDetail.value = null
    ElMessage.error(`基线信息加载失败：${messageOf(e)}`)
  } finally {
    baselineLoading.value = false
  }
}

/**
 * 「与基线对比」：GET /projects/{id}/diff-across。
 *
 * ``base`` = ``baseline@N``；``target`` = 当前产物 ``name@M``（无选中产物时用 ``code``）。
 * 返回体与 ``ArtifactDiff`` 同构，因此直接交给 ``DiffView`` 渲染（不另写 diff 组件）。
 */
async function onCompareBaseline(baselineVersion: number): Promise<void> {
  if (!projectId.value || baselineVersion <= 0) return
  const target = currentArtifact.value
  const baseRef = buildDiffRef(baselineDetail.value?.name || 'baseline', baselineVersion)
  const targetRef = buildDiffRef(target?.name || 'code', target?.current_version ?? null)
  activeTab.value = 'baseline-diff'
  await artifactsStore.loadDiffAcross(projectId.value, baseRef, targetRef)
  if (artifactsStore.error) {
    ElMessage.error(`基线对比失败：${artifactsStore.error}`)
  }
}

/** 打开发起推送对话框前的校验。 */
function openPushDialog(): void {
  if (!currentArtifact.value) {
    ElMessage.warning('请先选择左侧产物')
    return
  }
  pushDialogVisible.value = true
}

watch(selectedArtifactId, async (artifactId: string) => {
  if (!artifactId) return
  await artifactsStore.loadArtifact(artifactId)
  const latest = currentArtifact.value?.versions.at(-1)
  const earliest = currentArtifact.value?.versions[0]
  contentVersion.value = latest?.version ?? 0
  diffV2.value = latest?.version ?? 0
  diffV1.value = earliest && earliest.version !== diffV2.value ? earliest.version : diffV2.value
  if (contentVersion.value > 0) {
    selectedPath.value = null
    await artifactsStore.loadVersionContent(artifactId, contentVersion.value)
  }
})

/** 查看某版本内容（版本历史行操作）。 */
async function onViewVersion(version: ArtifactVersion): Promise<void> {
  activeTab.value = 'content'
  contentVersion.value = version.version
  selectedPath.value = null
  await artifactsStore.loadVersionContent(version.artifact_id, version.version)
}

/** 选择文件查看内容。 */
async function onSelectFile(path: string): Promise<void> {
  selectedPath.value = path
  await artifactsStore.loadVersionContent(selectedArtifactId.value, contentVersion.value, path)
}

/** 执行版本对比。 */
async function onCompare(): Promise<void> {
  if (diffV1.value === diffV2.value) return
  activeTab.value = 'diff'
  await artifactsStore.loadDiff(selectedArtifactId.value, diffV1.value, diffV2.value)
}

function formatTime(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 导出项目整库 zip（PM-02：GET /projects/{id}/export）。 */
const exporting = ref(false)

async function onExport(): Promise<void> {
  if (!projectId.value) return
  exporting.value = true
  try {
    const blob = await exportProjectZip(projectId.value)
    downloadBlob(blob, `ymir-${projectId.value.slice(0, 8)}.zip`)
    ElMessage.success('导出完成')
  } catch (e) {
    ElMessage.error(`导出失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    exporting.value = false
  }
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="artifact-library">
    <div class="library-header">
      <h2 class="library-title">产物库</h2>
      <div class="header-actions">
        <el-select v-model="projectId" placeholder="选择项目" style="width: 280px">
          <el-option
            v-for="project in projectsStore.items"
            :key="project.id"
            :label="project.name"
            :value="project.id"
          />
        </el-select>
        <el-button
          size="small"
          type="primary"
          plain
          :disabled="!projectId"
          :loading="exporting"
          @click="onExport"
        >
          导出 zip
        </el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="!projectId || !currentArtifact"
          @click="openPushDialog"
        >
          推送到 Git
        </el-button>
      </div>
    </div>

    <!-- 基线面板（T4.3 ①：vN / 文件数 / hash 前 8 位 / 上传时间） -->
    <BaselinePanel
      :detail="baselineDetail"
      :loading="baselineLoading"
      @compare="onCompareBaseline"
    />

    <div class="library-body">
      <!-- 产物列表 -->
      <div class="artifact-list" v-loading="artifactsStore.loading">
        <div v-if="artifacts.length === 0" class="panel-empty">该项目暂无产物</div>
        <div
          v-for="artifact in artifacts"
          :key="artifact.id"
          class="artifact-item"
          :class="{ active: artifact.id === selectedArtifactId }"
          @click="selectedArtifactId = artifact.id"
        >
          <div class="artifact-name">{{ artifact.name }}</div>
          <div class="artifact-meta">
            <el-tag size="small" type="info" effect="plain">{{ artifact.type }}</el-tag>
            <span class="artifact-version">v{{ artifact.current_version }}</span>
          </div>
        </div>
      </div>

      <!-- 详情 -->
      <div class="artifact-detail">
        <el-empty v-if="!currentArtifact" description="选择左侧产物查看版本历史" :image-size="60" />
        <template v-else>
          <el-tabs v-model="activeTab">
            <!-- 版本历史 -->
            <el-tab-pane label="版本历史" name="history">
              <el-table :data="currentArtifact.versions" border size="small">
                <el-table-column prop="version" label="版本" width="70" align="center" />
                <el-table-column prop="stage_id" label="阶段" width="120" />
                <el-table-column prop="message" label="说明" min-width="160" show-overflow-tooltip />
                <el-table-column prop="created_by" label="创建者" width="110" />
                <el-table-column label="文件/大小" width="120" align="center">
                  <template #default="{ row }">
                    {{ row.file_count }} 个 /
                    {{ formatSize(row.size_bytes) }}
                  </template>
                </el-table-column>
                <el-table-column label="时间" width="180">
                  <template #default="{ row }">
                    {{ formatTime(row.created_at) }}
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="220" fixed="right">
                  <template #default="{ row }">
                    <el-button link type="primary" size="small" @click="onViewVersion(row)">
                      查看内容
                    </el-button>
                    <el-button
                      link
                      type="primary"
                      size="small"
                      @click="
                        diffV1 = row.version;
                        diffV2 = currentArtifact?.current_version ?? diffV1;
                        onCompare();
                      "
                    >
                      与最新版对比
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-tab-pane>

            <!-- 版本内容 -->
            <el-tab-pane :label="`内容（v${contentVersion}）`" name="content">
              <div class="content-pane">
                <div class="file-list">
                  <div class="file-list-title">文件清单</div>
                  <div v-if="!artifactsStore.content?.files?.length" class="panel-empty">（空产物）</div>
                  <el-button
                    v-for="file in artifactsStore.content?.files ?? []"
                    :key="file"
                    link
                    :type="selectedPath === file ? 'primary' : 'info'"
                    @click="onSelectFile(file)"
                  >
                    {{ file }}
                  </el-button>
                </div>
                <div class="file-content">
                  <MarkdownView
                    :content="selectedPath ? (artifactsStore.content?.content ?? '') : ''"
                    :loading="artifactsStore.loading"
                  />
                </div>
              </div>
            </el-tab-pane>

            <!-- 版本对比 -->
            <el-tab-pane label="版本对比" name="diff">
              <div class="diff-controls">
                <el-select v-model="diffV1" style="width: 140px">
                  <el-option
                    v-for="version in currentArtifact.versions"
                    :key="version.id"
                    :label="`v${version.version}`"
                    :value="version.version"
                  />
                </el-select>
                <span class="diff-arrow">→</span>
                <el-select v-model="diffV2" style="width: 140px">
                  <el-option
                    v-for="version in currentArtifact.versions"
                    :key="version.id"
                    :label="`v${version.version}`"
                    :value="version.version"
                  />
                </el-select>
                <el-button type="primary" :disabled="diffV1 === diffV2" @click="onCompare">对比</el-button>
              </div>
              <DiffView :diff="artifactsStore.diff" :loading="artifactsStore.loading" />
            </el-tab-pane>

            <!-- 基线对比（T4.3 ②：diff-across，复用 DiffView） -->
            <el-tab-pane label="与基线对比" name="baseline-diff">
              <div class="diff-controls">
                <span class="diff-hint">
                  基线 {{ baselineDetail?.name || 'baseline' }} v{{ latestBaselineVersion || '-' }}
                  vs
                  {{ currentArtifact.name }} v{{ currentArtifact.current_version }}
                </span>
                <el-button
                  type="primary"
                  size="small"
                  :disabled="!baselineDetail || latestBaselineVersion <= 0"
                  :loading="artifactsStore.loading"
                  @click="onCompareBaseline(latestBaselineVersion)"
                >
                  与基线对比
                </el-button>
              </div>
              <div class="diff-hint diff-hint-block">
                左侧 = 基线快照，右侧 = 基线 ⊕ 本次补丁（仅新增/修改参与，逐字节相同即丢弃）。
              </div>
              <DiffView :diff="artifactsStore.diffAcross" :loading="artifactsStore.loading" />
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </div>

    <!-- 推送三步对话框（T4.3 ③：预演 -> 真推 -> 结果） -->
    <PushExportDialog
      v-model="pushDialogVisible"
      :project-id="projectId"
      :artifact="currentArtifact"
    />
  </div>
</template>

<style scoped>
.library-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.library-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.library-body {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 12px;
  align-items: start;
}

.artifact-list {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fff;
  padding: 8px;
  min-height: 300px;
}

.artifact-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
}

.artifact-item:hover {
  background: #f5f7fa;
}

.artifact-item.active {
  background: #ecf5ff;
}

.artifact-name {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.artifact-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}

.artifact-version {
  font-size: 12px;
  color: #909399;
}

.artifact-detail {
  min-height: 300px;
}

.panel-empty {
  color: #909399;
  font-size: 13px;
  padding: 16px 0;
  text-align: center;
}

.content-pane {
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

.diff-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.diff-arrow {
  color: #909399;
}

.diff-hint {
  font-size: 12px;
  color: #909399;
}

.diff-hint-block {
  margin-bottom: 10px;
  line-height: 1.7;
}
</style>
