<!--
  运行上下文的头部徽标（v1.2 T4.4）：模板 / 模板未生效 / 增量·基线 / 从零。

  兼容纪律（v1.1 缺陷 #6 的教训）：v1.1 的 ``run.context`` 没有 template /
  baseline / degraded 字段，此时徽标**整块 v-if 隐藏**，绝不渲染空壳徽标，
  也不出现 ``undefined``。

  「模板未生效」是 r5 硬约束：模板加载失败会降级为通用模式继续跑，必须显式提示，
  tooltip（同时落到 ``title`` 属性）给出 ``degraded`` 的原文原因。
-->
<script setup lang="ts">
import { computed } from 'vue'

import {
  baselineBadgeLabel,
  parseBaselineRef,
  parseTemplateDegraded,
  parseTemplateRef,
  templateBadgeLabel,
} from '@/utils/runContext'

interface Props {
  /** ``run.context``（自由字典；``null`` = run 尚未加载）。 */
  context: Record<string, unknown> | null
}

const props = withDefaults(defineProps<Props>(), { context: null })

/** 模板快照（null = 未使用模板 / 老帧）。 */
const templateRef = computed(() => parseTemplateRef(props.context))
const templateLabel = computed<string>(() => templateBadgeLabel(templateRef.value))

/** 模板降级原因原文（空串 = 未降级）。 */
const degradedReason = computed<string>(() => parseTemplateDegraded(props.context))

/** 基线快照（null = 从零生成）。 */
const baselineRef = computed(() => parseBaselineRef(props.context))
const baselineLabel = computed<string>(() => baselineBadgeLabel(baselineRef.value))
</script>

<template>
  <el-tag v-if="templateLabel" size="default" type="success" effect="plain" class="context-badge">
    {{ templateLabel }}
  </el-tag>

  <el-tooltip v-if="degradedReason" :content="degradedReason" placement="bottom">
    <!-- title 同步落地：无鼠标场景（截图 / 无障碍 / 测试）也能看到原文原因 -->
    <el-tag size="default" type="danger" effect="dark" :title="degradedReason" class="context-badge">
      模板未生效
    </el-tag>
  </el-tooltip>

  <el-tag v-if="baselineLabel" size="default" type="warning" effect="plain" class="context-badge">
    {{ baselineLabel }}
  </el-tag>
  <el-tag v-else-if="context" size="default" type="info" effect="plain" class="context-badge">
    从零
  </el-tag>
</template>

<style scoped>
.context-badge {
  margin-left: 4px;
}
</style>
