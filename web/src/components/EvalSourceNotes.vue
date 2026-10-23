<!--
  评测报告的两处来源标注（v1.2 T4.4）。

  - 「检查命令来源：模板 / 套件」：报告 JSON 没有显式字段，判据为
    ``run.context.template`` 是否存在（见 ``utils/runContext.ts`` 注释）；
  - 「骨架冲突 N 处」：产物含 ``patch_manifest.json`` 且 ``reserved_conflicts``
    非空时显示 —— 模板固定文件被保留、模型曾尝试覆盖，不阻断但必须可见；
    N 为 0 时整块隐藏（不渲染空标注）。
-->
<script setup lang="ts">
import { computed } from 'vue'

import { checksSourceLabel } from '@/utils/runContext'

interface Props {
  /** 检查命令来源：template = 模板 checks / suite = 套件默认检查。 */
  checksSource: 'template' | 'suite'
  /** 骨架冲突条数（0 = 不显示）。 */
  conflictCount?: number
}

const props = withDefaults(defineProps<Props>(), { conflictCount: 0 })

const sourceLabel = computed<string>(() => checksSourceLabel(props.checksSource))
const hasConflicts = computed<boolean>(() => props.conflictCount > 0)
</script>

<template>
  <div class="eval-source-notes">
    <el-tag size="small" type="info" effect="plain">{{ sourceLabel }}</el-tag>

    <template v-if="hasConflicts">
      <el-tag size="small" type="warning" effect="dark">
        骨架冲突 {{ conflictCount }} 处
      </el-tag>
      <span class="conflict-hint">模板固定文件已被保留（模型曾尝试覆盖）</span>
    </template>
  </div>
</template>

<style scoped>
.eval-source-notes {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.conflict-hint {
  font-size: 12px;
  color: #909399;
}
</style>
