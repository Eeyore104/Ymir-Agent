<!--
  设置（/settings）：后端健康探测 + 模型网关视图（脱敏）+ token / 成本用量。
-->
<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { useSystemStore } from '@/stores/system'

const systemStore = useSystemStore()

/** 用量口径（global / run / project）。 */
const usageScope = ref<'global' | 'run' | 'project'>('global')
const usageRunId = ref('')
const usageProjectId = ref('')

onMounted(async () => {
  await systemStore.probeHealth()
  await systemStore.loadModels()
  await refreshUsage()
})

/** 按当前口径刷新用量（global 无需附加参数；run / project 传对应 ID）。 */
async function refreshUsage(): Promise<void> {
  await systemStore.loadUsage(usageScope.value, {
    runId: usageRunId.value || undefined,
    projectId: usageProjectId.value || undefined,
  })
}

function formatCost(cost: number): string {
  return `¥${cost.toFixed(4)}`
}
</script>

<template>
  <div class="settings">
    <h2 class="settings-title">设置</h2>

    <!-- 健康检查 -->
    <el-card shadow="never" class="panel">
      <template #header>
        <div class="card-header">
          <strong>后端健康</strong>
          <el-button size="small" :loading="systemStore.healthLoading" @click="systemStore.probeHealth()">
            重新探测
          </el-button>
        </div>
      </template>
      <div v-if="systemStore.health" class="health-grid">
        <el-tag :type="systemStore.health.status === 'ok' ? 'success' : 'danger'" size="large">
          服务：{{ systemStore.health.status }}
        </el-tag>
        <el-tag :type="systemStore.health.database === 'ok' ? 'success' : 'danger'" size="large">
          数据库：{{ systemStore.health.database }}
        </el-tag>
        <el-tag :type="systemStore.health.gateway === 'ready' ? 'success' : 'warning'" size="large">
          模型网关：{{ systemStore.health.gateway }}
        </el-tag>
        <el-tag type="info" size="large">版本：{{ systemStore.health.version }}</el-tag>
      </div>
      <el-alert
        v-else-if="systemStore.healthError"
        type="error"
        :closable="false"
        show-icon
        :title="`探测失败：${systemStore.healthError}`"
        description="请确认后端 uvicorn 已在 8000 端口启动（npm run dev 的 /api 代理指向 127.0.0.1:8000）"
      />
      <span v-else class="muted">尚未探测</span>
    </el-card>

    <!-- 模型网关（脱敏） -->
    <el-card shadow="never" class="panel">
      <template #header><strong>模型网关（脱敏视图）</strong></template>
      <el-empty v-if="!systemStore.models" description="暂无网关配置" :image-size="60" />
      <template v-else>
        <div class="muted gateway-version">配置版本：{{ systemStore.models.version }}</div>
        <el-table :data="systemStore.models.providers" border size="small">
          <el-table-column prop="name" label="Provider" width="160" />
          <el-table-column prop="base_url" label="Endpoint" min-width="220" show-overflow-tooltip />
          <el-table-column label="启用" width="80" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
                {{ row.enabled ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="密钥就绪" width="100" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="row.key_ready ? 'success' : 'warning'">
                {{ row.key_ready ? '就绪' : '缺失' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="模型" min-width="260">
            <template #default="{ row }">
              <el-tag
                v-for="model in row.models"
                :key="model.name"
                size="small"
                effect="plain"
                class="model-tag"
              >
                {{ model.name }}（{{ model.tier }}，in ¥{{ model.price_in }} / out ¥{{ model.price_out }}）
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <!-- 用量 -->
    <el-card shadow="never" class="panel">
      <template #header><strong>Token / 成本用量</strong></template>
      <div class="usage-controls">
        <el-radio-group v-model="usageScope" @change="refreshUsage">
          <el-radio-button value="global">全局</el-radio-button>
          <el-radio-button value="run">按运行</el-radio-button>
          <el-radio-button value="project">按项目</el-radio-button>
        </el-radio-group>
        <el-input
          v-if="usageScope === 'run'"
          v-model="usageRunId"
          placeholder="run_id"
          style="width: 300px"
          clearable
        />
        <el-input
          v-if="usageScope === 'project'"
          v-model="usageProjectId"
          placeholder="project_id"
          style="width: 300px"
          clearable
        />
        <el-button size="small" type="primary" @click="refreshUsage">查询</el-button>
      </div>
      <div v-if="systemStore.usage" class="usage-grid">
        <div class="usage-item">
          <div class="usage-value">{{ systemStore.usage.calls }}</div>
          <div class="usage-label">调用次数</div>
        </div>
        <div class="usage-item">
          <div class="usage-value">{{ systemStore.usage.prompt_tokens }}</div>
          <div class="usage-label">输入 tokens</div>
        </div>
        <div class="usage-item">
          <div class="usage-value">{{ systemStore.usage.completion_tokens }}</div>
          <div class="usage-label">输出 tokens</div>
        </div>
        <div class="usage-item">
          <div class="usage-value">{{ systemStore.usage.total_tokens }}</div>
          <div class="usage-label">总 tokens</div>
        </div>
        <div class="usage-item">
          <div class="usage-value cost">{{ formatCost(systemStore.usage.cost_cny) }}</div>
          <div class="usage-label">成本（{{ systemStore.usage.scope }}）</div>
        </div>
      </div>
      <el-empty v-else description="暂无用量数据" :image-size="60" />
    </el-card>
  </div>
</template>

<style scoped>
.settings-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 12px;
}

.panel {
  margin-bottom: 12px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.health-grid {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.muted {
  font-size: 12px;
  color: #909399;
}

.gateway-version {
  margin-bottom: 8px;
}

.model-tag {
  margin: 2px 4px 2px 0;
}

.usage-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.usage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}

.usage-item {
  padding: 14px;
  background: #f5f7fa;
  border-radius: 6px;
  text-align: center;
}

.usage-value {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
}

.usage-value.cost {
  color: #e6a23c;
}

.usage-label {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}
</style>
