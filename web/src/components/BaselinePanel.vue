<!--
  基线面板（v1.2 T4.3，docs/09 §4.2）。

  展示本项目 ``type=baseline`` 产物的版本历史：vN / 文件数 / hash 前 8 位 / 上传时间，
  并提供「与基线对比」入口（调用方负责走 GET /projects/{id}/diff-across 并用
  DiffView 渲染，本组件只发事件、不含 diff 逻辑）。
-->
<script setup lang="ts">
import { computed } from 'vue'

import type { ArtifactDetail, ArtifactVersion } from '@/api/types'
import { formatDateTime, shortHash } from '@/utils/baseline'

interface Props {
  /** 基线产物详情（含版本历史）；``null`` = 该项目还没有基线。 */
  detail: ArtifactDetail | null
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  detail: null,
  loading: false,
})

/** 「与基线对比」事件：携带被选中的基线版本号。 */
const emit = defineEmits<{ (event: 'compare', version: number): void }>()

const versions = computed<ArtifactVersion[]>(() => props.detail?.versions ?? [])

const basename = computed<string>(() => props.detail?.name ?? 'baseline')

/** hash 列展示（前 8 位；缺失显示占位符，不渲染 undefined）。 */
function hashLabel(version: ArtifactVersion): string {
  return shortHash(version.content_hash) || '—'
}

function timeLabel(version: ArtifactVersion): string {
  return formatDateTime(version.created_at) || '—'
}

function onCompare(version: ArtifactVersion): void {
  emit('compare', version.version)
}
</script>

<template>
  <div v-loading="loading" class="baseline-panel">
    <div class="panel-header">
      <span class="panel-title">项目基线</span>
      <el-tag v-if="versions.length" size="small" type="warning" effect="plain">
        {{ basename }} · 共 {{ versions.length }} 个版本
      </el-tag>
      <el-tag v-else size="small" type="info" effect="plain">无基线</el-tag>
    </div>

    <!-- 引导文案（v1.3.1 T5.4）：动作入口在下方 code 版本行 —— 本组件拿不到"要提升哪一版"，
         自己取最新版就是 R9 禁止的「拿最新顶上」，所以这里只给一句话，不放按钮。 -->
    <div class="panel-tip panel-tip-lead">
      增量产物可在下方「版本历史」的 code 版本行点「另存为新基线」，把合入结果提升为项目基线。
    </div>

    <div v-if="!versions.length" class="panel-empty">
      该项目还没有基线。可在「项目看板 · 发起运行」上传现有工程 zip 快照后，改为增量生成。
    </div>

    <template v-else>
      <el-table :data="versions" border size="small">
        <el-table-column label="版本" width="80" align="center">
          <template #default="{ row }">v{{ row.version }}</template>
        </el-table-column>
        <el-table-column label="文件数" width="90" align="center">
          <template #default="{ row }">{{ row.file_count }}</template>
        </el-table-column>
        <el-table-column label="内容 hash" width="120">
          <template #default="{ row }">
            <span class="hash-cell">{{ hashLabel(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="180">
          <template #default="{ row }">{{ timeLabel(row) }}</template>
        </el-table-column>
        <el-table-column prop="message" label="说明" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="onCompare(row)">与基线对比</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="panel-tip">
        「与基线对比」= 选中基线版本与「基线 ⊕ 本次补丁」的逐文件差异
        （GET /projects/{id}/diff-across，复用 DiffView 渲染）。
      </div>
    </template>
  </div>
</template>

<style scoped>
.baseline-panel {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fff;
  padding: 10px 12px;
  margin-bottom: 12px;
  min-height: 80px;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.panel-empty {
  color: #909399;
  font-size: 13px;
  padding: 8px 0;
}

.hash-cell {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  color: #606266;
}

.panel-tip {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
}

/* 位于标题下方的引导文案（无上方表格，故上边距清零、改留底距） */
.panel-tip-lead {
  margin-top: 0;
  margin-bottom: 8px;
  line-height: 1.7;
}
</style>
