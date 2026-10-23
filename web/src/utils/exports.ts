/**
 * 推送目标与回执的纯函数（v1.2 T4.3，docs/09 §4.3）。
 *
 * 三条硬纪律在此集中实现，便于单测覆盖：
 * 1. **预演优先**：``dry_run`` 缺省为 ``true``（只有显式 ``false`` 才真推）；
 * 2. **分支保护**：目标是 ``main`` / ``master`` 时默认拒绝，需显式确认后
 *    才把 ``target.allow_protected_branch = true`` 发出去（后端 K5 校验）；
 * 3. **不带凭证**：请求体永不包含 token / pat（后端 ``extra="forbid"``）。
 */

import type { ExportCreatePayload, ExportTargetPayload } from '@/api/types'

/** 受保护分支（与后端 ``PROTECTED_BRANCHES`` 对齐）。 */
export const PROTECTED_BRANCHES: readonly string[] = ['main', 'master']

/** 默认新分支前缀（``ymir/<artifact>-v<n>``）。 */
export const DEFAULT_BRANCH_PREFIX = 'ymir/'

/** ``true`` = 该分支受保护（``main`` / ``master``）。 */
export function isProtectedBranch(branch: string | null | undefined): boolean {
  const name = (branch ?? '').trim()
  if (!name) return false
  return PROTECTED_BRANCHES.includes(name)
}

/**
 * 默认新分支名：``ymir/<artifact>-v<n>``；无版本号时退化为 ``ymir/<artifact>``。
 */
export function buildDefaultBranch(
  artifactName: string | null | undefined,
  version?: number | null,
): string {
  const name = (artifactName ?? '').trim() || 'artifact'
  if (typeof version === 'number' && Number.isFinite(version) && version > 0) {
    return `${DEFAULT_BRANCH_PREFIX}${name}-v${Math.trunc(version)}`
  }
  return `${DEFAULT_BRANCH_PREFIX}${name}`
}

/** 生效分支：用户输入优先，空输入回落到默认新分支。 */
export function resolveBranch(
  input: string | null | undefined,
  artifactName: string | null | undefined,
  version?: number | null,
): string {
  const raw = (input ?? '').trim()
  return raw || buildDefaultBranch(artifactName, version)
}

/**
 * 「真推」按钮是否可用：受保护分支必须显式勾选确认。
 *
 * @param branch 生效分支（已 resolve）
 * @param allowProtectedBranch 用户是否勾选「我确认推送到受保护分支」
 */
export function canRealPush(
  branch: string | null | undefined,
  allowProtectedBranch: boolean,
): boolean {
  return !isProtectedBranch(branch) || allowProtectedBranch
}

/** 提交 sha 短链（缺省 7 位，与 GitHub 一致）；无 sha 返回空串。 */
export function shortSha(sha: string | null | undefined, length = 7): string {
  const raw = (sha ?? '').trim()
  if (!raw) return ''
  return raw.slice(0, length > 0 ? length : 0)
}

/** 推送目标构造入参（表单字段）。 */
export interface PushTargetInput {
  repo: string
  /** 空 = 使用默认新分支（``branch: null`` 交给后端生成）。 */
  branch?: string | null
  pathPrefix?: string
  tree?: 'files' | 'merged'
  commitMessage?: string | null
  allowProtectedBranch?: boolean
  strictSecretCheck?: boolean
}

/** 推送请求构造入参。 */
export interface ExportPayloadInput extends PushTargetInput {
  artifactId: string
  artifactVersion?: number | null
  /** 缺省 ``true`` = 预演；只有显式 ``false`` 才真推。 */
  dryRun?: boolean
}

/**
 * 构造 ``target``（严格对齐后端 ``ExportTarget``：不多发任何字段）。
 *
 * ``branch`` 为空时发 ``null`` —— 后端据此生成 ``ymir/<artifact>-v<n>``。
 */
export function buildExportTarget(input: PushTargetInput): ExportTargetPayload {
  const branch = (input.branch ?? '').trim()
  return {
    kind: 'github',
    repo: (input.repo ?? '').trim(),
    branch: branch || null,
    path_prefix: (input.pathPrefix ?? '').trim(),
    allow_protected_branch: Boolean(input.allowProtectedBranch),
    strict_secret_check: Boolean(input.strictSecretCheck),
  }
}

/** 构造 ``POST /projects/{id}/exports`` 请求体（**绝不含 token 字段**）。 */
export function buildExportPayload(input: ExportPayloadInput): ExportCreatePayload {
  const payload: ExportCreatePayload = {
    target: buildExportTarget(input),
    artifact_id: input.artifactId,
    tree: input.tree ?? 'merged',
    dry_run: input.dryRun !== false,
  }
  if (typeof input.artifactVersion === 'number' && input.artifactVersion > 0) {
    payload.artifact_version = Math.trunc(input.artifactVersion)
  }
  return payload
}

/** 推送失败的业务码 -> 可操作指引（前端兜底文案，不臆造后端语义）。 */
export function exportErrorHint(code: number | null | undefined): string {
  switch (code) {
    case 1001:
      return '参数被拒绝：多为目标是受保护分支（main/master）却未勾选确认、repo 不是 owner/name，或分支名首尾有空白。'
    case 1002:
      return '目标不存在：请确认产物版本仍然存在，以及目标仓库已在 GitHub 上创建（可勾选「仓库不存在时新建」）。'
    case 1003:
      return '状态冲突：目标分支已被他人推进或已存在同名分支，请换一个分支名后重试。'
    case 1007:
      return '推送失败：请检查服务端 GitHub token 环境变量是否配置且具备 repo 写权限，或稍后重试。'
    case -1:
      return '请求没有到达后端（网络/超时），请确认后端服务可用后重试。'
    default:
      return '推送未完成：请查看拦截清单与回执信息后重试。'
  }
}
