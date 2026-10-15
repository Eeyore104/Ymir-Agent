<!--
  产物库（/artifacts?project_id=...）：产物列表 / 版本历史 / 版本内容 / 版本 diff
  （验收点④：驳回重跑后的版本递增 lineage 一目了然）。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import DiffView from '@/components/DiffView.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import { useArtifactsStore } from '@/stores/artifacts'
import { useProjectsStore } from '@/stores/projects'
import type { Artifact, ArtifactVersion } from '@/api/types'

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

const artifacts = computed<Artifact[]>(() => artifactsStore.items)
const currentArtifact = computed(() => artifactsStore.current)

onMounted(async () => {
  await projectsStore.loadProjects()
  const queryProject = route.query.project_id
  projectId.value =
    typeof queryProject === 'string' && queryProject
      ? queryProject
      : (projectsStore.items[0]?.id ?? '')
  if (projectId.value) await artifactsStore.loadProjectArtifacts(projectId.value)
})

watch(projectId, async (next) => {
  if (!next) return
  selectedArtifactId.value = ''
  await artifactsStore.loadProjectArtifacts(next)
})

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
</script>

<template>
  <div class="artifact-library">
    <div class="library-header">
      <h2 class="library-title">产物库</h2>
      <el-select v-model="projectId" placeholder="选择项目" style="width: 280px">
        <el-option
          v-for="project in projectsStore.items"
          :key="project.id"
          :label="project.name"
          :value="project.id"
        />
      </el-select>
    </div>

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
          </el-tabs>
        </template>
      </div>
    </div>
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
</style>
