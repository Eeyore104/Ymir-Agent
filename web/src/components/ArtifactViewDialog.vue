<!--
  产物内容查看对话框（共享组件）：文件清单 + 单文件 Markdown 渲染。

  供运行详情（阶段产物）与审批中心（待审产物）复用，消除重复的弹窗逻辑。
  通过 v-model:visible 控制显隐；打开时按 artifactId（+version，缺省取最新）加载内容。
-->
<script setup lang="ts">
import { ref, watch } from 'vue'

import MarkdownView from './MarkdownView.vue'
import { useArtifactsStore } from '@/stores/artifacts'

interface Props {
  visible: boolean
  artifactId: string
  /** 目标版本；缺省时取产物最新版本。 */
  version?: number | null
  title?: string
}

const props = withDefaults(defineProps<Props>(), { version: null, title: '' })
const emit = defineEmits<{ (e: 'update:visible', value: boolean): void }>()

const store = useArtifactsStore()
const selectedPath = ref<string | null>(null)

watch(
  () => props.visible,
  async (opened) => {
    if (!opened || !props.artifactId) return
    selectedPath.value = null
    let version = props.version ?? 0
    if (!version) {
      await store.loadArtifact(props.artifactId)
      version = store.current?.current_version ?? 0
    }
    if (version > 0) await store.loadVersionContent(props.artifactId, version)
  },
)

async function onSelectFile(path: string): Promise<void> {
  const artifactId = store.content?.artifact_id ?? props.artifactId
  const version = store.content?.version ?? props.version ?? 0
  if (!version) return
  selectedPath.value = path
  await store.loadVersionContent(artifactId, version, path)
}

function onVisibleChange(value: boolean): void {
  emit('update:visible', value)
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    :title="title || '产物内容'"
    width="720px"
    @update:model-value="onVisibleChange"
  >
    <div v-loading="store.loading" class="artifact-dialog">
      <div class="file-list">
        <div class="file-list-title">文件清单</div>
        <div v-if="!store.content?.files?.length" class="panel-empty">（空产物）</div>
        <el-button
          v-for="file in store.content?.files ?? []"
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
        <MarkdownView
          :content="selectedPath ? store.content?.content ?? '' : ''"
          :loading="store.loading"
        />
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
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

.panel-empty {
  color: #909399;
  font-size: 13px;
  padding: 8px 0;
}
</style>
