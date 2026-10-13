<script setup lang="ts">
import { ref } from 'vue'
import axios from 'axios'

interface HealthPayload {
  code: number
  data?: {
    status: string
    database: string
    version: string
  }
  message?: string
}

const loading = ref(false)
const message = ref('尚未检测')
const status = ref<'idle' | 'ok' | 'error'>('idle')

async function probe(): Promise<void> {
  loading.value = true
  try {
    const { data } = await axios.get<HealthPayload>('/health')
    if (data.code === 0) {
      status.value = 'ok'
      message.value = `后端连接正常（version ${data.data?.version ?? '-'}，DB ${data.data?.database ?? '-'}）`
    } else {
      status.value = 'error'
      message.value = `后端返回异常：code ${data.code}`
    }
  } catch {
    status.value = 'error'
    message.value = '后端不可达（请确认 uvicorn 已在 8000 端口启动）'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-card class="hello-card" shadow="hover">
    <template #header>
      <strong>ymir-agent 控制台已就绪</strong>
    </template>
    <p class="hello-desc">
      这是一句需求 → PRD → 架构 → 代码 → 评测 的多 Agent 协作平台。当前为 T01 前端骨架，
      完整控制台（项目看板 / 运行详情 / 产物库 / 审批中心 / 评测报告）将在后续批次落地。
    </p>
    <div class="hello-actions">
      <el-button type="primary" :loading="loading" @click="probe">
        检测后端 /health
      </el-button>
      <el-tag v-if="status === 'ok'" type="success">{{ message }}</el-tag>
      <el-tag v-else-if="status === 'error'" type="danger">{{ message }}</el-tag>
      <el-tag v-else type="info">{{ message }}</el-tag>
    </div>
  </el-card>
</template>

<style scoped>
.hello-card {
  max-width: 720px;
}

.hello-desc {
  color: #4b5563;
  line-height: 1.7;
}

.hello-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
