<!--
  产物库（/artifacts?project_id=...）：产物列表 / 版本历史 / 版本内容 / 版本 diff
  （验收点④：驳回重跑后的版本递增 lineage 一目了然）。

  v1.2 T4.3 增补：基线面板（vN / 文件数 / hash 前 8 位 / 上传时间）、
  跨产物对比（基线 vs 基线⊕补丁，复用 DiffView）、推送三步对话框。

  v1.3.1 T5.4：「另存为新基线」的**唯一动作入口**落在本视图的 ``code`` 版本行
  （该行已带 ``run_id`` / ``stage_id`` / ``version``，零额外查询就知道"要提升哪一版"）。
  ``BaselinePanel.vue`` 只加一句引导文案 —— 它拿不到"要提升哪一版 code"，
  自己取最新版正是 R9 明令禁止的「拿最新顶上」。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import BaselinePanel from '@/components/BaselinePanel.vue'
import DiffView from '@/components/DiffView.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import PushExportDialog from '@/components/PushExportDialog.vue'
import {
  exportProjectZip,
  fetchArtifact,
  fetchProjectArtifacts,
  promoteMergedToBaseline,
} from '@/api/artifacts'
import { useArtifactsStore } from '@/stores/artifacts'
import { messageOf, useProjectsStore } from '@/stores/projects'
import { buildDiffRef } from '@/utils/baseline'
import {
  CODE_ARTIFACT_NAME,
  PATCH_MANIFEST_NAME,
  PROMOTE_ACTION_LABEL,
  PROMOTE_DISABLED_HINT,
  canPromote,
  parsePromoteManifest,
  promoteConfirmLines,
  promoteErrorText,
  promoteResultText,
} from '@/utils/promoteBaseline'
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

// --- 「另存为新基线」（v1.3.1 T5.4）-------------------------------------------
/** 二次确认框可见性。 */
const promoteDialogVisible = ref(false)
/** 待提升的 code 版本行（``null`` = 未选中）。 */
const promoteTarget = ref<ArtifactVersion | null>(null)
/** 请求进行中（防重复点击）。 */
const promoting = ref(false)
/**
 * 已打开过的 ``patch_manifest.json`` 正文缓存（键 ``artifact_id@version``）。
 *
 * ★ 为什么需要它、以及它**不会**变成"点一次拉一次"：
 * 版本行本身不带文件正文，为了预检而额外发一次 content 请求就等于给"打开页面
 * 零额外查询"加了一条按需查询。这里**只缓存用户已经在「内容」页主动打开过的正文**
 * （见 ``onSelectFile``），一次不多拉；拿不到正文时按钮改用 ``run_id`` 门控，
 * 结论交给服务端 1001 / 1003 的原文兜住。
 */
const manifestCache = ref<Record<string, string>>({})

/** 仅 ``code`` 产物允许「另存为新基线」（其它产物没有可重建的合并树）。 */
const isCodeArtifact = computed<boolean>(() => currentArtifact.value?.name === CODE_ARTIFACT_NAME)

/** 预计基线版本号（当前最新基线 + 1；尚无基线 ⇒ v1；幂等命中时服务端不新建）。 */
const nextBaselineVersion = computed<number>(() => latestBaselineVersion.value + 1)

/** 确认框三要素（``run_id`` 前 8 位 / ``base v?`` / **预计**基线版本号）。 */
const promoteLines = computed<string[]>(() => {
  const row = promoteTarget.value
  if (row === null) {
    return []
  }
  const raw = manifestCache.value[manifestKey(row)] ?? null
  const facts = raw === null ? null : parsePromoteManifest(raw)
  return promoteConfirmLines({
    runId: row.run_id,
    baseVersion: facts?.baseVersion ?? null,
    targetVersion: nextBaselineVersion.value,
  })
})
// ---------------------------------------------------------------------------

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

/** 选择文件查看内容（顺带缓存 ``patch_manifest.json`` 正文，供「另存为新基线」预检）。 */
async function onSelectFile(path: string): Promise<void> {
  selectedPath.value = path
  await artifactsStore.loadVersionContent(selectedArtifactId.value, contentVersion.value, path)
  if (path !== PATCH_MANIFEST_NAME || selectedArtifactId.value === '' || contentVersion.value <= 0) {
    return
  }
  const loaded = artifactsStore.content
  if (loaded?.path !== PATCH_MANIFEST_NAME || typeof loaded.content !== 'string') {
    return
  }
  if (loaded.content.trim() === '') {
    return
  }
  manifestCache.value[`${selectedArtifactId.value}@${contentVersion.value}`] = loaded.content
}

/** 版本行在 manifest 缓存里的键（``artifact_id@version``，与展示无关、稳定可复现）。 */
function manifestKey(row: ArtifactVersion): string {
  return `${row.artifact_id}@${row.version}`
}

/**
 * 打开「另存为新基线」二次确认框（唯一动作入口，T5.4）。
 *
 * 门控分两层：
 * 1. ``run_id`` 为空 ⇒ 服务端无从取溯源，直接拒绝（按钮本身也禁用）；
 * 2. 正文**已缓存**时再跑一遍 ``canPromote`` 预检 —— 只挡"必然失败"（非增量产物 /
 *    正文损坏）；正文未缓存则跳过预检（**不为此新增查询**），交给服务端 1001 / 1003。
 */
function openPromoteDialog(row: ArtifactVersion): void {
  if (!row.run_id) {
    ElMessage.warning(PROMOTE_DISABLED_HINT)
    return
  }
  const raw = manifestCache.value[manifestKey(row)] ?? null
  if (raw !== null) {
    const gate = canPromote(raw)
    if (!gate.ok) {
      ElMessage.warning(gate.reason)
      return
    }
  }
  promoteTarget.value = row
  promoteDialogVisible.value = true
}

/** 关闭确认框并清空选中行。 */
function closePromoteDialog(): void {
  promoteDialogVisible.value = false
  promoteTarget.value = null
}

/**
 * 确认「另存为新基线」：``POST /projects/{id}/baselines/from-merge``。
 *
 * ★ 失败文案一律**原样上屏**（1001 超上限 / 非增量产物，1003 指纹不符 / 缺溯源 /
 * 模板已变更）—— 这些 message 本身就是修复指引，改写成「操作失败」等于把用户堵死。
 * ★ ``reused=true`` 只提示「基线已是该内容」且**不显示新版本号**（见 ``promoteResultText``）。
 */
async function onPromote(): Promise<void> {
  const row = promoteTarget.value
  if (row === null || !row.run_id || !projectId.value) {
    return
  }
  promoting.value = true
  try {
    const result = await promoteMergedToBaseline(projectId.value, {
      run_id: row.run_id,
      stage_id: row.stage_id ?? undefined,
      artifact_version: row.version,
    })
    const feedback = promoteResultText(result)
    ElMessage({ type: feedback.type, message: feedback.text })
    closePromoteDialog()
    await loadBaselineDetail(projectId.value)
  } catch (e) {
    ElMessage.error(promoteErrorText(e))
  } finally {
    promoting.value = false
  }
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
                <el-table-column label="操作" :width="isCodeArtifact ? 330 : 220" fixed="right">
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
                    <!--
                      「另存为新基线」唯一动作入口（v1.3.1 T5.4）：只在 code 版本行出现。
                      run_id 为空 ⇒ 禁用；disabled 按钮不派发鼠标事件，故 tooltip 需包一层 span。
                    -->
                    <el-tooltip
                      v-if="isCodeArtifact"
                      :content="row.run_id ? '' : PROMOTE_DISABLED_HINT"
                      placement="top"
                      :disabled="Boolean(row.run_id)"
                    >
                      <span class="promote-wrap">
                        <el-button
                          link
                          type="primary"
                          size="small"
                          :disabled="!row.run_id"
                          @click="openPromoteDialog(row)"
                        >
                          {{ PROMOTE_ACTION_LABEL }}
                        </el-button>
                      </span>
                    </el-tooltip>
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

    <!--
      「另存为新基线」二次确认框（v1.3.1 T5.4）：展示 run_id 前 8 位 / base v?（取不到则省略）
      / **预计**基线版本号。确认后调用 POST /projects/{id}/baselines/from-merge。
    -->
    <el-dialog
      v-model="promoteDialogVisible"
      :title="PROMOTE_ACTION_LABEL"
      width="520px"
      :close-on-click-modal="false"
      @closed="promoteTarget = null"
    >
      <div class="promote-facts">
        <el-tag v-for="line in promoteLines" :key="line" size="small" type="info" effect="plain">
          {{ line }}
        </el-tag>
      </div>
      <div class="promote-hint">
        将按该版本的合入溯源（基线引用 + 补丁指纹 + 模板指纹 + 合并树指纹）确定性重建合并树，
        并落成新的基线版本；若重建结果与当前最新基线内容一致，则复用、不新增版本。
      </div>
      <div class="promote-hint">
        「预计基线版本」= 当前最新基线 + 1，最终版本号以服务端回执为准（幂等命中时不变）。
      </div>
      <div class="promote-hint">
        模板文件有改动（含注释）、指纹不符、或合并结果超出基线上限时，服务端会拒绝并给出原因。
      </div>
      <template #footer>
        <el-button @click="closePromoteDialog">取消</el-button>
        <el-button type="primary" :loading="promoting" @click="onPromote">
          确认另存为新基线
        </el-button>
      </template>
    </el-dialog>
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

/* 「另存为新基线」确认框 */
.promote-wrap {
  display: inline-block;
}

.promote-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.promote-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
  margin-bottom: 6px;
}
</style>
