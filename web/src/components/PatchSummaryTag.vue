<!--
  增量改动面徽标（v1.3 T1.3，docs/11 §7.4）。

  展示两类信息：
  - 「改动面 78%，建议复核」：change_ratio 超阈值（服务端 `warnings` 已有短码）；
  - 「受保护路径命中 N 处」：模型撞了受保护路径被拦截（硬约束生效的证据）。

  ★ 无数据时 `v-if` 直接隐藏（不渲染空壳）—— 非增量产物/旧产物不应出现噪音。
-->
<script setup lang="ts">
import { computed } from 'vue'

import { formatChangeRatio, hasPatchSummary, type PatchSummary } from '@/utils/patchSummary'

interface Props {
  /** 解析后的 manifest 摘要（`parsePatchSummary` 的输出）。 */
  summary: PatchSummary | null
}

const props = withDefaults(defineProps<Props>(), {
  summary: null,
})

/** 是否渲染（无摘要 / 全空 ⇒ 隐藏） */
const visible = computed<boolean>(() => props.summary !== null && hasPatchSummary(props.summary))

/** 改动面文本（如 `改动面 78%，建议复核`；无 change_ratio 时只显示变更文件数） */
const ratioText = computed<string>(() => {
  const summary = props.summary
  if (summary === null) {
    return ''
  }
  const percent = formatChangeRatio(summary.changeRatio)
  if (percent && summary.exceeded) {
    return `改动面 ${percent}，建议复核`
  }
  if (percent) {
    return `改动面 ${percent}`
  }
  return `变更 ${summary.changedCount} 个文件`
})

/** 徽标颜色：超阈值用 warning，否则用 info */
const ratioTagType = computed<'warning' | 'info'>(() =>
  props.summary?.exceeded ? 'warning' : 'info',
)

/** 受保护路径命中提示（0 条时不渲染） */
const protectedText = computed<string>(() => {
  const hits = props.summary?.protectedHits ?? 0
  return hits > 0 ? `受保护路径命中 ${hits} 处` : ''
})

/** tooltip：把短码原样列出，便于排查（短码是机器口径，docs/11 §7.5-2） */
const warningsText = computed<string>(() => (props.summary?.warnings ?? []).join(' / '))
</script>

<template>
  <div v-if="visible" class="patch-summary-tag">
    <el-tooltip :content="warningsText || ratioText" placement="top" :disabled="!warningsText">
      <el-tag size="small" :type="ratioTagType">{{ ratioText }}</el-tag>
    </el-tooltip>
    <el-tag v-if="protectedText" size="small" type="danger" effect="plain">
      {{ protectedText }}
    </el-tag>
  </div>
</template>

<style scoped>
.patch-summary-tag {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
</style>
