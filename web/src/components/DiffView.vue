<!--
  版本 Diff 视图（验收点④：产物版本递增的直观呈现）。

  数据源为后端 unified diff（按文件分组）：
  删除行红底、新增行绿底、hunk 头蓝灰、统计栏汇总。
-->
<script setup lang="ts">
import { computed } from 'vue'

import type { ArtifactDiff, FileDiff } from '@/api/types'

interface Props {
  diff: ArtifactDiff | null
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  diff: null,
  loading: false,
})

/** 单文件渲染行（分类 + 文本）。 */
interface DiffLine {
  kind: 'add' | 'del' | 'hunk' | 'meta' | 'normal'
  text: string
}

const files = computed<Array<{ file: FileDiff; lines: DiffLine[] }>>(() => {
  if (!props.diff) return []
  return props.diff.files.map((file) => ({ file, lines: parseLines(file.diff) }))
})

const stats = computed(() => props.diff?.stats ?? null)

/** 解析 unified diff 文本为着色行。 */
function parseLines(raw: string): DiffLine[] {
  if (!raw) return []
  return raw.split('\n').map((line) => {
    if (line.startsWith('@@')) return { kind: 'hunk', text: line }
    if (line.startsWith('+') && !line.startsWith('+++')) return { kind: 'add', text: line }
    if (line.startsWith('-') && !line.startsWith('---')) return { kind: 'del', text: line }
    if (line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) {
      return { kind: 'meta', text: line }
    }
    return { kind: 'normal', text: line }
  })
}

const STATUS_TAGS: Record<string, { type: 'success' | 'warning' | 'danger' | 'info'; label: string }> = {
  added: { type: 'success', label: '新增' },
  modified: { type: 'warning', label: '修改' },
  deleted: { type: 'danger', label: '删除' },
  renamed: { type: 'info', label: '重命名' },
}

function statusTag(status: string): { type: 'success' | 'warning' | 'danger' | 'info'; label: string } {
  return STATUS_TAGS[status] ?? { type: 'info', label: status }
}
</script>

<template>
  <div v-loading="loading" class="diff-view">
    <el-empty v-if="!diff" description="选择两个版本以查看差异" :image-size="60" />

    <template v-else>
      <!-- 统计栏 -->
      <div class="diff-stats">
        <span class="stat-item">v{{ diff.v1 }} → v{{ diff.v2 }}</span>
        <span class="stat-item">变更文件：{{ stats?.files_changed ?? diff.files.length }}</span>
        <span class="stat-add">+{{ stats?.additions ?? 0 }}</span>
        <span class="stat-del">-{{ stats?.deletions ?? 0 }}</span>
      </div>

      <!-- 按文件分组 -->
      <div v-for="group in files" :key="group.file.path" class="diff-file">
        <div class="diff-file-header">
          <span class="diff-path">{{ group.file.path }}</span>
          <el-tag size="small" :type="statusTag(group.file.status).type" effect="light">
            {{ statusTag(group.file.status).label }}
          </el-tag>
          <span class="diff-file-counts">
            <span class="stat-add">+{{ group.file.additions }}</span>
            <span class="stat-del">-{{ group.file.deletions }}</span>
          </span>
        </div>
        <div class="diff-lines">
          <div
            v-for="(line, index) in group.lines"
            :key="index"
            class="diff-line"
            :class="`diff-line-${line.kind}`"
          >
            {{ line.text }}
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.diff-view {
  min-height: 120px;
}

.diff-stats {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 13px;
  color: #606266;
  margin-bottom: 12px;
}

.stat-add {
  color: #189f6f;
  font-weight: 600;
}

.stat-del {
  color: #d54941;
  font-weight: 600;
}

.diff-file {
  margin-bottom: 14px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  overflow: hidden;
}

.diff-file-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 12px;
  background: #fafbfc;
  border-bottom: 1px solid #ebeef5;
  font-size: 12px;
}

.diff-path {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  color: #303133;
  font-weight: 600;
}

.diff-file-counts {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.diff-lines {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.7;
  overflow-x: auto;
}

.diff-line {
  padding: 0 12px;
  white-space: pre;
}

.diff-line-add {
  background: #e6f7ee;
  color: #0a7d4f;
}

.diff-line-del {
  background: #fdecea;
  color: #b32e29;
}

.diff-line-hunk {
  background: #ecf2fb;
  color: #3a6ea5;
}

.diff-line-meta {
  background: #f5f7fa;
  color: #909399;
}

.diff-line-normal {
  color: #606266;
}
</style>
