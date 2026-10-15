<!--
  评测报告（/evaluation）：T05 批次占位页。

  T05 将接入 evaluation_report 产物的结构化渲染（评分维度 / 通过率 / 失败用例），
  当前仅展示占位说明与项目产物库入口。
-->
<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const projectId = typeof route.query.project_id === 'string' ? route.query.project_id : ''

function gotoArtifacts(): void {
  void router.push(projectId ? `/artifacts?project_id=${projectId}` : '/artifacts')
}
</script>

<template>
  <div class="eval-report">
    <el-result icon="info" title="评测报告（T05 规划中）" sub-title="评测流水线将在 T05 批次落地，当前版本可先查看产物库中的评测产物。">
      <template #extra>
        <el-button type="primary" @click="gotoArtifacts">前往产物库</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.eval-report {
  padding: 48px 0;
  background: #fff;
  border-radius: 6px;
}
</style>
