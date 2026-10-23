<!--
  推送导出对话框（v1.2 T4.3，docs/09 §4.3.1 / §4.3.4）。

  三步流程：
  1. **预演**（``dry_run: true``）：只读展示「将推送 N 个 / 被拦截 M 个 / 跳过 K 个」，
     拦截清单可展开折叠 —— 预演不产生任何网络写；
  2. **真推**（``dry_run: false``）：二次确认按钮；默认推送到新分支
     ``ymir/<artifact>-v<n>``；目标为 ``main`` / ``master`` 时按钮默认禁用，
     需勾选「我确认推送到受保护分支」后才发 ``target.allow_protected_branch = true``；
  3. **结果**：``commit_sha`` 短链（可复制）、拦截清单、失败时展示 ``error_code`` 与指引。

  安全：请求体**绝不含 token**（后端 ``extra="forbid"``），凭证只从服务端环境变量读取。
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import { createExport } from '@/api/exports'
import type { Artifact, BlockedFile, ExportReceipt } from '@/api/types'
import { messageOf } from '@/stores/projects'
import {
  buildDefaultBranch,
  buildExportPayload,
  canRealPush,
  exportErrorHint,
  isProtectedBranch,
  resolveBranch,
  shortSha,
} from '@/utils/exports'

interface Props {
  modelValue: boolean
  projectId: string
  /** 待推送产物（``null`` = 未选中，按钮全部禁用）。 */
  artifact: Artifact | null
  /** 初始分支名（缺省空串 = 使用默认新分支；供测试/深链预填）。 */
  initialBranch?: string
}

const props = withDefaults(defineProps<Props>(), {
  modelValue: false,
  projectId: '',
  artifact: null,
  initialBranch: '',
})

const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'pushed', receipt: ExportReceipt): void
}>()

/** 推送表单（repo 必填；branch 为空 -> 默认新分支）。 */
interface PushForm {
  repo: string
  branch: string
  pathPrefix: string
  tree: 'files' | 'merged'
  commitMessage: string
}

const form = reactive<PushForm>({
  repo: '',
  branch: props.initialBranch,
  pathPrefix: '',
  tree: 'merged',
  commitMessage: '',
})

const allowProtected = ref(false)
const busy = ref(false)
/** 0 = 预演 / 1 = 真推确认 / 2 = 结果。 */
const step = ref<0 | 1 | 2>(0)
const dryReceipt = ref<ExportReceipt | null>(null)
const receipt = ref<ExportReceipt | null>(null)
const errorMessage = ref<string | null>(null)
const errorCode = ref<number | null>(null)

/** 默认新分支（``ymir/<artifact>-v<n>``，仅用于提示文案）。 */
const defaultBranch = computed<string>(() =>
  buildDefaultBranch(props.artifact?.name ?? '', props.artifact?.current_version ?? null),
)

/** 实际生效分支：用户输入优先，空 -> 默认新分支。 */
const effectiveBranch = computed<string>(() =>
  resolveBranch(form.branch, props.artifact?.name ?? '', props.artifact?.current_version ?? null),
)

/** 目标是否为受保护分支（main / master）。 */
const protectedBranch = computed<boolean>(() => isProtectedBranch(effectiveBranch.value))

/** 受保护分支 + 未勾选确认 -> 不可真推（K5 前端侧拦截）。 */
const realPushAllowed = computed<boolean>(() =>
  canRealPush(effectiveBranch.value, allowProtected.value),
)

const repoReady = computed<boolean>(() => form.repo.trim().length > 0)

/** 「真推」可用：已选中产物 + 填了 repo + 已预演 + 分支保护已确认。 */
const canSubmitRealPush = computed<boolean>(
  () =>
    !busy.value &&
    props.artifact !== null &&
    repoReady.value &&
    dryReceipt.value !== null &&
    realPushAllowed.value,
)

/** 「预演」可用。 */
const canSubmitDryRun = computed<boolean>(
  () => !busy.value && props.artifact !== null && repoReady.value,
)

const blockedFiles = computed<BlockedFile[]>(() => {
  const source = receipt.value ?? dryReceipt.value
  if (!source) return []
  return [...(source.blocked_files ?? []), ...(source.skipped_files ?? [])]
})

const failed = computed<boolean>(() => errorMessage.value !== null)

/** 步骤高亮：结果页 -> 3（全部完成）。 */
const activeStep = computed<number>(() => {
  if (receipt.value || failed.value) return 3
  if (dryReceipt.value) return 1
  return 0
})

const progressText = computed<string>(() => {
  const source = dryReceipt.value
  const willPush = source?.file_count ?? 0
  const blocked = source?.blocked_count ?? 0
  const skipped = source?.skipped_count ?? 0
  return `将推送 ${willPush} 个 / 被拦截 ${blocked} 个 / 跳过 ${skipped} 个`
})

const shortCommit = computed<string>(() => shortSha(receipt.value?.commit_sha))

const errorHint = computed<string>(() => (failed.value ? exportErrorHint(errorCode.value) : ''))

/** 每次打开都重置到第 1 步（预演永远在前）。 */
function resetState(): void {
  form.repo = ''
  form.branch = props.initialBranch
  form.pathPrefix = ''
  form.tree = 'merged'
  form.commitMessage = ''
  allowProtected.value = false
  busy.value = false
  step.value = 0
  dryReceipt.value = null
  receipt.value = null
  errorMessage.value = null
  errorCode.value = null
}

watch(
  () => props.modelValue,
  (open: boolean) => {
    if (open) resetState()
  },
)

function close(): void {
  emit('update:modelValue', false)
}

function payloadOf(dryRun: boolean) {
  return buildExportPayload({
    repo: form.repo,
    branch: form.branch,
    pathPrefix: form.pathPrefix,
    tree: form.tree,
    commitMessage: form.commitMessage.trim() || null,
    // 只有受保护分支且用户显式确认，才把 allow_protected_branch 发出去
    allowProtectedBranch: protectedBranch.value && allowProtected.value,
    artifactId: props.artifact?.id ?? '',
    artifactVersion: props.artifact?.current_version ?? null,
    dryRun,
  })
}

/** 第 1 步：预演（dry_run=true，不产生任何网络写）。 */
async function onDryRun(): Promise<void> {
  if (!props.artifact) {
    ElMessage.warning('请先选择要推送的产物')
    return
  }
  if (!repoReady.value) {
    ElMessage.warning('请填写目标仓库（owner/name）')
    return
  }
  busy.value = true
  errorMessage.value = null
  errorCode.value = null
  receipt.value = null
  try {
    const result = await createExport(props.projectId, payloadOf(true))
    dryReceipt.value = result
    step.value = 1
    ElMessage.success('预演完成，请确认后真推')
  } catch (e) {
    dryReceipt.value = null
    errorCode.value = e instanceof Error && 'code' in e ? Number((e as { code: number }).code) : null
    errorMessage.value = messageOf(e)
    step.value = 2
  } finally {
    busy.value = false
  }
}

/** 第 2 步：真推（dry_run=false，二次确认后才可能被点到）。 */
async function onRealPush(): Promise<void> {
  if (!canSubmitRealPush.value) {
    ElMessage.warning('请先完成预演；受保护分支还需勾选确认')
    return
  }
  busy.value = true
  errorMessage.value = null
  errorCode.value = null
  try {
    const result = await createExport(props.projectId, payloadOf(false))
    receipt.value = result
    step.value = 2
    emit('pushed', result)
    ElMessage.success('推送完成')
  } catch (e) {
    errorCode.value = e instanceof Error && 'code' in e ? Number((e as { code: number }).code) : null
    errorMessage.value = messageOf(e)
    step.value = 2
  } finally {
    busy.value = false
  }
}

/** 复制 commit sha 短链。 */
async function onCopySha(): Promise<void> {
  const sha = shortCommit.value
  if (!sha) return
  try {
    await navigator.clipboard.writeText(sha)
    ElMessage.success(`已复制 ${sha}`)
  } catch {
    ElMessage.warning(`复制失败，请手动选择 ${sha}`)
  }
}

function reasonOf(file: BlockedFile): string {
  return file.reason || '未提供原因'
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="推送到 Git 仓库"
    width="720px"
    :close-on-click-modal="false"
    @update:model-value="close"
  >
    <el-steps :active="activeStep" finish-status="success" align-center class="push-steps">
      <el-step title="① 预演" description="dry_run=true，只看不推" />
      <el-step title="② 真推" description="二次确认后推送" />
      <el-step title="③ 结果" description="commit 与拦截清单" />
    </el-steps>

    <!-- 固定文案：.env 黑白名单（拦截规则必须对用户可见） -->
    <el-alert type="warning" :closable="false" show-icon class="env-tip">
      <template #title>
        <div class="env-tip-line">.env 类文件永不推送</div>
        <div class="env-tip-line">
          .env.example / .env.sample / .env.template 属文档模板，允许推送
        </div>
      </template>
    </el-alert>

    <el-form label-width="96px" class="push-form" :disabled="busy">
      <el-form-item label="目标仓库" required>
        <el-input v-model="form.repo" placeholder="owner/name" maxlength="200" />
      </el-form-item>
      <el-form-item label="目标分支">
        <el-input v-model="form.branch" :placeholder="`留空 = 默认推送到新分支 ${defaultBranch}`" maxlength="200" />
        <div class="field-hint">
          默认推送到新分支 <code>{{ defaultBranch }}</code>；受保护分支（main / master）需显式确认。
        </div>
      </el-form-item>
      <el-form-item label="子目录前缀">
        <el-input v-model="form.pathPrefix" placeholder="monorepo 子目录，留空 = 仓库根" maxlength="200" />
      </el-form-item>
      <el-form-item label="推送内容">
        <el-radio-group v-model="form.tree">
          <el-radio-button value="merged">基线 ⊕ 补丁（推荐）</el-radio-button>
          <el-radio-button value="files">仅原产物</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="提交信息">
        <el-input v-model="form.commitMessage" placeholder="留空使用默认提交信息" maxlength="200" />
      </el-form-item>
      <el-form-item v-if="protectedBranch" label="分支保护">
        <el-checkbox v-model="allowProtected">
          我确认推送到受保护分支（{{ effectiveBranch }}）
        </el-checkbox>
      </el-form-item>
    </el-form>

    <!-- 第 1 步结果：预演统计 + 拦截清单（可展开折叠） -->
    <div v-if="dryReceipt" class="dry-result">
      <div class="dry-title">预演结果（未产生任何写入）</div>
      <div class="dry-counts">{{ progressText }}</div>
      <el-collapse v-if="blockedFiles.length" class="blocked-collapse">
        <el-collapse-item :title="`查看拦截 / 跳过清单（${blockedFiles.length} 个）`" name="blocked">
          <div v-for="file in blockedFiles" :key="`${file.path}-${file.reason}`" class="blocked-row">
            <span class="blocked-path">{{ file.path }}</span>
            <span class="blocked-reason">{{ reasonOf(file) }}</span>
          </div>
        </el-collapse-item>
      </el-collapse>
      <div v-else class="dry-clean">没有被拦截或跳过的文件</div>
    </div>

    <!-- 第 3 步：结果 -->
    <div v-if="receipt || failed" class="push-result">
      <div v-if="receipt && !failed" class="result-ok">
        <div class="result-line">
          已推送 {{ receipt.file_count }} 个文件到
          <code>{{ receipt.repo }}@{{ receipt.branch }}</code>
        </div>
        <div v-if="shortCommit" class="result-line">
          commit：<code>{{ shortCommit }}</code>
          <el-button link type="primary" size="small" @click="onCopySha">复制</el-button>
        </div>
        <div class="result-line">
          被拦截 {{ receipt.blocked_count }} 个 / 跳过 {{ receipt.skipped_count }} 个
        </div>
      </div>

      <div v-else class="result-fail">
        <div class="result-line result-error">
          推送失败<span v-if="errorCode !== null">（error_code: {{ errorCode }}）</span>
          <span v-else-if="receipt?.error_code !== null && receipt?.error_code !== undefined">
            （error_code: {{ receipt.error_code }}）
          </span>
        </div>
        <div class="result-line">{{ errorMessage || receipt?.error_message || '未提供错误信息' }}</div>
        <div class="result-line result-hint">指引：{{ errorHint }}</div>
      </div>

      <el-collapse v-if="blockedFiles.length" class="blocked-collapse">
        <el-collapse-item :title="`被拦截 / 跳过的文件（${blockedFiles.length} 个）`" name="blocked-result">
          <div v-for="file in blockedFiles" :key="`r-${file.path}-${file.reason}`" class="blocked-row">
            <span class="blocked-path">{{ file.path }}</span>
            <span class="blocked-reason">{{ reasonOf(file) }}</span>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
      <el-button :loading="busy" :disabled="!canSubmitDryRun" @click="onDryRun">
        开始预演（dry_run）
      </el-button>
      <el-button type="primary" :loading="busy" :disabled="!canSubmitRealPush" @click="onRealPush">
        确认真推（dry_run=false）
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.push-steps {
  margin-bottom: 14px;
}

.env-tip {
  margin-bottom: 14px;
}

.env-tip-line {
  font-size: 13px;
  line-height: 1.7;
}

.push-form {
  margin-bottom: 4px;
}

.field-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}

.dry-result,
.push-result {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 10px 12px;
  margin-top: 10px;
  background: #fafbfc;
}

.dry-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 6px;
}

.dry-counts {
  font-size: 13px;
  color: #606266;
}

.dry-clean {
  font-size: 12px;
  color: #909399;
  margin-top: 6px;
}

.result-line {
  font-size: 13px;
  color: #303133;
  line-height: 1.8;
}

.result-error {
  color: #d54941;
  font-weight: 600;
}

.result-hint {
  color: #909399;
  font-size: 12px;
}

.blocked-collapse {
  margin-top: 8px;
}

.blocked-row {
  display: flex;
  gap: 10px;
  font-size: 12px;
  line-height: 1.8;
}

.blocked-path {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  color: #303133;
  min-width: 220px;
}

.blocked-reason {
  color: #909399;
}
</style>
