<!--
  项目看板（/）：创建项目 / 项目列表 / 发起运行（选 HITL 模式）。
-->
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { useProjectsStore } from '@/stores/projects'
import { useRunsStore } from '@/stores/runs'
import { RUN_MODE_DESCRIPTIONS, RUN_MODE_LABELS } from '@/constants'
import type { RunMode } from '@/api/types'

const router = useRouter()
const projectsStore = useProjectsStore()
const runsStore = useRunsStore()

onMounted(() => {
  void projectsStore.loadProjects()
})

/** 创建项目对话框。 */
const createDialogVisible = ref(false)
const createForm = reactive({ name: '', description: '' })
const creating = ref(false)

async function onCreateProject(): Promise<void> {
  if (!createForm.name.trim()) {
    ElMessage.warning('请填写项目名称')
    return
  }
  creating.value = true
  try {
    await projectsStore.createProject(createForm.name.trim(), createForm.description.trim() || undefined)
    ElMessage.success('项目已创建')
    createDialogVisible.value = false
    createForm.name = ''
    createForm.description = ''
  } catch (e) {
    ElMessage.error(`创建失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    creating.value = false
  }
}

/** 发起运行对话框。 */
const runDialogVisible = ref(false)
const runForm = reactive<{ projectId: string; projectName: string; requirement: string; mode: RunMode }>({
  projectId: '',
  projectName: '',
  requirement: '',
  mode: 'key_node',
})
const launching = ref(false)

const MODE_OPTIONS: RunMode[] = ['key_node', 'batch', 'debug']

function openRunDialog(projectId: string, projectName: string): void {
  runForm.projectId = projectId
  runForm.projectName = projectName
  runForm.requirement = ''
  runForm.mode = 'key_node'
  runDialogVisible.value = true
}

async function onLaunchRun(): Promise<void> {
  if (!runForm.requirement.trim()) {
    ElMessage.warning('请填写需求描述')
    return
  }
  launching.value = true
  try {
    const runId = await runsStore.startRun(runForm.projectId, {
      requirement: runForm.requirement.trim(),
      mode: runForm.mode,
    })
    runDialogVisible.value = false
    ElMessage.success('运行已发起，正在跳转详情…')
    await router.push(`/runs/${runId}`)
  } catch (e) {
    ElMessage.error(`发起失败：${e instanceof Error ? e.message : String(e)}`)
  } finally {
    launching.value = false
  }
}
</script>

<template>
  <div class="project-board">
    <div class="board-header">
      <h2 class="board-title">项目看板</h2>
      <el-button type="primary" @click="createDialogVisible = true">新建项目</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="board-tip"
      title="一句话需求 -> PRD -> 架构 -> 代码 -> 评测 的四角色流水线；关键节点人工审批，可驳回回退。"
    />

    <el-table :data="projectsStore.items" v-loading="projectsStore.loading" border stripe>
      <el-table-column prop="name" label="项目" min-width="160" />
      <el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip />
      <el-table-column prop="run_count" label="运行数" width="90" align="center">
        <template #default="{ row }">
          {{ row.run_count ?? '-' }}
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">
          {{ new Date(row.created_at).toLocaleString('zh-CN', { hour12: false }) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openRunDialog(row.id, row.name)">发起运行</el-button>
          <el-button link type="primary" @click="router.push(`/artifacts?project_id=${row.id}`)">
            产物库
          </el-button>
          <el-button link type="primary" @click="router.push(`/evaluation?project_id=${row.id}`)">
            评测报告
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无项目，点击「新建项目」开始" :image-size="60" />
      </template>
    </el-table>

    <!-- 创建项目 -->
    <el-dialog v-model="createDialogVisible" title="新建项目" width="480px">
      <el-form label-width="72px">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" maxlength="255" show-word-limit placeholder="项目名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="项目描述（选填）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreateProject">创建</el-button>
      </template>
    </el-dialog>

    <!-- 发起运行 -->
    <el-dialog v-model="runDialogVisible" :title="`发起运行 - ${runForm.projectName}`" width="560px">
      <el-form label-width="72px">
        <el-form-item label="需求" required>
          <el-input
            v-model="runForm.requirement"
            type="textarea"
            :rows="4"
            placeholder="一句话描述要构建的软件，例如：做一个带审批流的任务看板 Web 应用"
          />
        </el-form-item>
        <el-form-item label="HITL 模式" required>
          <el-radio-group v-model="runForm.mode">
            <el-radio-button v-for="mode in MODE_OPTIONS" :key="mode" :value="mode">
              {{ RUN_MODE_LABELS[mode] }}
            </el-radio-button>
          </el-radio-group>
          <div class="mode-desc">{{ RUN_MODE_DESCRIPTIONS[runForm.mode] }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="runDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="launching" @click="onLaunchRun">发起</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.board-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.board-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.board-tip {
  margin-bottom: 12px;
}

.mode-desc {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
}
</style>
