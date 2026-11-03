/**
 * 「另存为新基线」纯函数集（v1.3.1 T5.4/T5.5，docs/13 §7.3-8、§2-T5.4）。
 *
 * 数据来源：增量产物内的 ``patch_manifest.json``（后端 ``artifacts.merge.PatchManifest``）。
 * 服务端动作：``POST /projects/{id}/baselines/from-merge``
 * （``api.routers.artifacts.promote_baseline_from_merge`` →
 * ``artifacts.provenance.promote_merged_to_baseline``）。
 *
 * ★ 职责边界（**重要，别把这里写成第二套校验器**）
 * 前端只挡「点了必然失败」的那一类：**无正文 / 坏 JSON / 非 ``patch`` 模式**。
 * 六道校验里的**指纹不符（1003）、溯源缺失（1003）、模板已变更（1003）、
 * 超出基线上限（1001）** 前端一律不算 —— 这些结论只有拿到合并树的服务端才能得出，
 * 前端算不出来也不该假装算。它们的 ``message`` 由 :func:`promoteErrorText`
 * **原样上屏**，绝不改写成「操作失败」：那句话本身就是给用户看的修复指引。
 *
 * ★ v1.4 T2.2（docs/15 §3.2）：**健康度闸门**同理 —— 前端**无法预估**合并树是否
 * 不健康（那要跑一遍 checks），所以不猜：等 409 回来，用 :func:`requiresForceConfirm`
 * 认出"这条 409 允许 force 越过"，再把确认框切成红色警告 + :data:`FORCE_PROMOTE_LABEL`。
 * 越过闸门必须是**人**的一次点击（:func:`withForce` 只造新请求体，不自动重试）。
 *
 * ★ 为什么版本行不用 :func:`canPromote` 做门控（它不是死代码）
 * 产物库的版本行只带 ``run_id`` / ``stage_id`` / ``version``，**不带文件正文**。
 * 为了拿正文而多发一次 ``GET /artifacts/{id}/versions/{n}/content?path=…``，
 * 会把「打开页面零额外查询」变成「每点一次拉一次」。因此：
 *
 * 1. 版本行按钮的门控 = ``run_id`` 是否为空（见 :data:`PROMOTE_DISABLED_HINT`）；
 * 2. :func:`canPromote` 只用在**确实拿得到正文**的位置 —— 用户已在「内容」页打开过
 *    ``patch_manifest.json``（正文已进 store）、正文已被调用方缓存时，打开确认框前
 *    先跑一遍预检，同时把 ``base v?`` 填进确认框（:func:`parsePromoteManifest`）；
 * 3. 正文拿不到的路径，**一律由服务端 1001 / 1003 的原文兜住** —— 这是刻意取舍，
 *    不是遗漏。
 *
 * 纪律：本模块所有函数**绝不抛**（坏数据降级为 ``ok: false`` / ``null``），
 * 与 ``utils/patchSummary.ts`` 同源。
 */

/** 可被「另存为新基线」的产物名（与后端 ``routers/artifacts.CODE_ARTIFACT_NAME`` 同值）。 */
export const CODE_ARTIFACT_NAME = 'code'

/** 增量产物的自描述簿记文件名（正文即 :func:`canPromote` 的入参）。 */
export const PATCH_MANIFEST_NAME = 'patch_manifest.json'

/** 版本行按钮的统一样式文案（模板与测试共用同一份，避免两处漂移）。 */
export const PROMOTE_ACTION_LABEL = '另存为新基线'

/** ``run_id`` 为空的版本行禁用按钮时的 tooltip 文案。 */
export const PROMOTE_DISABLED_HINT = '该版本无关联运行'

/** 预检结论：``ok=false`` 时 ``reason`` 可直接展示给用户。 */
export interface PromoteGate {
  /** 是否允许发起「另存为新基线」。 */
  ok: boolean
  /** 拒绝原因（``ok=true`` 时为空串）。 */
  reason: string
}

/** 从 ``patch_manifest.json`` 正文里抽出的展示事实（只读，不含任何判定）。 */
export interface PromoteManifestFacts {
  /** 合并模式：``patch`` 才是增量产物。 */
  mode: string
  /** 基线版本号（``provenance.base_version``；未知为 ``null``，**不许**回退到「最新」）。 */
  baseVersion: number | null
  /** 模板 id（无模板的增量运行为空串）。 */
  templateId: string
  /** 是否带合入溯源（缺它就必然被服务端以 1003 拒绝）。 */
  hasProvenance: boolean
}

/** 确认框的三要素入参。 */
export interface PromoteConfirmInput {
  /** 该版本关联的运行 id（原样传入，本函数负责截前 8 位）。 */
  runId: string | null
  /** 基线版本号（取不到就传 ``null`` ⇒ 该行省略）。 */
  baseVersion: number | null
  /** **预计**基线版本号（当前最新基线 + 1；服务端可能幂等复用而不新建）。 */
  targetVersion: number
}

/** 服务端回执里本模块关心的部分（``reused`` 是幂等标记）。 */
export interface PromoteOutcome {
  /** ``true`` = 基线最新版已是该内容（未新建版本）。 */
  reused: boolean
  /** 本次落成 / 命中的基线版本号。 */
  version: number
  /** 降级留痕（v1.4 T2.1）：``baseline_promoted_unhealthy:1`` 或缺省。 */
  warning?: string
}

/** 提示条载荷（交给 ``ElMessage``）。 */
export interface PromoteMessage {
  /** Element Plus 提示类型：成功 = ``success``，幂等命中 / 强制提升 = ``info``（不是错误）。 */
  type: 'success' | 'info'
  /** 展示文案。 */
  text: string
}

/**
 * 业务码「状态冲突」（后端 ``BizCode.STATE_CONFLICT``）。
 *
 * ★ 闸门拒绝（``baseline_unhealthy`` / ``baseline_health_unknown``）走的就是
 * 1003 / 409：语义是"这次状态不允许"，不是参数错，因此前端据此**追问 force**，
 * 而不是把用户堵死。
 */
export const STATE_CONFLICT_CODE = 1003

/**
 * 强制提升留痕短码（后端 ``artifacts.provenance.BASELINE_PROMOTED_UNHEALTHY_CODE``）。
 *
 * 服务端在"合并树探针不健康但用户坚持提升"时返回它，前端必须**显著提示**，
 * 让人知道"后续回归比较的起点本身就不健康"。
 */
export const PROMOTED_UNHEALTHY_CODE = 'baseline_promoted_unhealthy:1'

/**
 * 「没有健康结论却被强制提升」的留痕短码（后端
 * ``artifacts.provenance.BASELINE_PROMOTED_UNKNOWN_CODE``，v1.4 返修 P1-3）。
 *
 * ★ 与上面那个**分开**：一个说"评测说它坏了"，一个说"没有评测说它好"。
 * 混成一个字面量会让用户无法判断该修代码还是该补评测。
 */
export const PROMOTED_UNKNOWN_CODE = 'baseline_promoted_unknown:1'

/**
 * 闸门拒绝原因（后端错误信封 ``details.reason``，v1.4 返修 P1-3）。
 *
 * ★ 这是**唯一**可靠的判定依据：``code`` 都是 1003（指纹不符 / 缺溯源 / 模板已变更
 * 也是 1003，那些**不能**用 force 越过），``message`` 是给人看的文案（会改）。
 */
export const BASELINE_UNHEALTHY_REASON = 'baseline_unhealthy'

/** 闸门拒绝原因：拿不到任何健康结论（fail-closed ⇒ 409）。 */
export const BASELINE_HEALTH_UNKNOWN_REASON = 'baseline_health_unknown'

/** 可用 ``force`` 越过的闸门原因（其余 1003 一律不许越）。 */
export const FORCE_CONFIRM_REASONS: readonly string[] = [
  BASELINE_UNHEALTHY_REASON,
  BASELINE_HEALTH_UNKNOWN_REASON,
]

/** 闸门原因的字面量类型。 */
export type ForceConfirmReason =
  | typeof BASELINE_UNHEALTHY_REASON
  | typeof BASELINE_HEALTH_UNKNOWN_REASON

/**
 * 强制确认框里的红色警告（v1.4 T2.2，docs/15 §3.2；v1.4 返修按 reason 分支）。
 *
 * ★ 文案要同时说清三件事：**现在是什么状态**、**做了会怎样**、**能怎么办**。
 * 只说"操作失败"等于把用户堵死；把两种情况说成同一句话等于骗人
 * （"没有评测"和"评测说它坏了"是两种完全不同的处境）。
 */
export const UNHEALTHY_CONFIRM_WARNING =
  '该合并树不健康（存在阻断项失败或相对基线回归）：提升为基线会污染后续基线' +
  '（下一轮 run 将以一棵跑不起来的树为起点）'

/** 「没有健康结论」时的红色警告文案（fail-closed 闸门）。 */
export const HEALTH_UNKNOWN_CONFIRM_WARNING =
  '该运行没有可用的合并树健康结论（评测报告缺失或不可读）：闸门无法判断这棵树是否跑得起来，' +
  '提升为基线等于拿未评测的树当下一轮的起点'

/** 强制确认框的标题（按 reason 分支；缺省用不健康那条）。 */
export const UNHEALTHY_CONFIRM_TITLE = '合并树不健康（阻断项失败 / 回归）'

/** 「没有健康结论」时的确认框标题。 */
export const HEALTH_UNKNOWN_CONFIRM_TITLE = '没有健康结论'

/** 强制提升按钮的文案（与"取消"并排，措辞带风险）。 */
export const FORCE_PROMOTE_LABEL = '仍要提升（force）'

/**
 * 强制提升成功后的提示文案（``warning`` 命中留痕短码时）。
 *
 * @param result 服务端回执（只看 ``warning`` / ``version`` / ``reused``）
 */
export function promoteWarningText(result: PromoteOutcome): string {
  const warning = result.warning
  if (warning !== PROMOTED_UNHEALTHY_CODE && warning !== PROMOTED_UNKNOWN_CODE) {
    return ''
  }
  const suffix = promoteSuffix(result)
  const head =
    warning === PROMOTED_UNHEALTHY_CODE
      ? '已强制提升不健康的合并树为新基线'
      : '已强制提升「没有健康结论」的合并树为新基线'
  const tail =
    warning === PROMOTED_UNHEALTHY_CODE
      ? '后续回归比较的起点不健康'
      : '该树未经过评测，后续回归比较没有可信起点'
  return `${head}${suffix}：${tail}`
}

/** 版本号后缀（幂等命中 / 已知版本 / 未知三种口径）。 */
function promoteSuffix(result: PromoteOutcome): string {
  if (result.reused) {
    return '（未新建版本：基线已是该内容）'
  }
  const version = asVersion(result.version)
  return version === null ? '' : `（v${version}）`
}

/**
 * 该错误是否属于「闸门拒绝、可用 force 二次确认」（v1.4 返修 P1-3）。
 *
 * 判据是**两件事同时成立**：业务码 1003 + ``details.reason`` 属于
 * :data:`FORCE_CONFIRM_REASONS`。只认 ``1003`` 不行：非增量 run / 指纹不符 /
 * 模板已变更也都是 1003，那些**不能**用 force 越过（服务端也不会放行），
 * 追问只会让用户白点一次。
 *
 * ★ 也**不再**靠 ``message.includes('force')``：文案是给人看的，改一个字就失效；
 * 而"改文案"和"改判定"之间没有任何机制保证同步（首版就是这样静默退化的）。
 *
 * @param error 捕获到的异常（``BizError`` / 任意值）
 * @returns 命中的闸门原因；不是闸门拒绝 ⇒ ``null``
 */
export function forceConfirmReason(error: unknown): ForceConfirmReason | null {
  if (typeof error !== 'object' || error === null) {
    return null
  }
  const record = error as { code?: unknown; details?: unknown }
  if (record.code !== STATE_CONFLICT_CODE) {
    return null
  }
  const details = asRecord(record.details)
  const reason = details === null ? '' : details.reason
  if (typeof reason !== 'string' || !FORCE_CONFIRM_REASONS.includes(reason)) {
    return null
  }
  return reason as ForceConfirmReason
}

/**
 * 该错误是否属于「闸门拒绝、需要 force 二次确认」（布尔便捷形态）。
 *
 * @param error 捕获到的异常（``BizError`` / 任意值）
 */
export function requiresForceConfirm(error: unknown): boolean {
  return forceConfirmReason(error) !== null
}

/**
 * 按闸门原因取确认框文案（``null`` ⇒ 不展示警告）。
 *
 * @param reason :func:`forceConfirmReason` 的返回值
 */
export function forceConfirmWarning(reason: ForceConfirmReason | null): string {
  if (reason === BASELINE_HEALTH_UNKNOWN_REASON) {
    return HEALTH_UNKNOWN_CONFIRM_WARNING
  }
  return reason === BASELINE_UNHEALTHY_REASON ? UNHEALTHY_CONFIRM_WARNING : ''
}

/**
 * 按闸门原因取确认框标题（``null`` ⇒ 空串，调用方不渲染）。
 *
 * @param reason :func:`forceConfirmReason` 的返回值
 */
export function forceConfirmTitle(reason: ForceConfirmReason | null): string {
  if (reason === BASELINE_HEALTH_UNKNOWN_REASON) {
    return HEALTH_UNKNOWN_CONFIRM_TITLE
  }
  return reason === BASELINE_UNHEALTHY_REASON ? UNHEALTHY_CONFIRM_TITLE : ''
}

/**
 * 把请求体改写成「强制提升」形态（服务端据此越过闸门并留痕）。
 *
 * 纯函数、不改入参：``force`` 只在这一份新对象上为 ``true``。
 *
 * @param payload 原请求体（``run_id`` 必填）
 */
export function withForce(payload: {
  run_id: string
  stage_id?: string | null
  artifact_version?: number | null
}): {
  run_id: string
  stage_id?: string | null
  artifact_version?: number | null
  force: true
} {
  return { ...payload, force: true }
}

/** 判定「该产物是否允许另存为新基线」的名字口径。 */
export function isPromotableArtifact(name: string | null | undefined): boolean {
  return typeof name === 'string' && name === CODE_ARTIFACT_NAME
}

/** ``run_id`` 的前 8 位（确认框展示用；空值返回空串）。 */
export function shortRunId(runId: string | null | undefined): string {
  if (typeof runId !== 'string') {
    return ''
  }
  return runId.trim().slice(0, 8)
}

/** 安全取对象（数组 / ``null`` / 原始值均返回 ``null``）。 */
function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/** 安全取正整数（``null`` = 未知，绝不返回 0 冒充版本号）。 */
function asVersion(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return Math.trunc(value)
  }
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : null
  }
  return null
}

/**
 * 解析 ``patch_manifest.json`` 正文里的展示事实。
 *
 * @param manifestRaw 产物内 ``patch_manifest.json`` 的原始文本（可空 / 可损坏）
 * @returns 事实对象；正文为空 / 坏 JSON / 非对象时返回 ``null``（**绝不抛**）
 */
export function parsePromoteManifest(
  manifestRaw: string | null | undefined,
): PromoteManifestFacts | null {
  if (typeof manifestRaw !== 'string' || manifestRaw.trim() === '') {
    return null
  }
  let payload: unknown
  try {
    payload = JSON.parse(manifestRaw)
  } catch {
    // 坏 JSON：交给 canPromote 去拒绝，这里只负责"抽不出事实"
    return null
  }
  const manifest = asRecord(payload)
  if (manifest === null) {
    return null
  }
  const provenance = asRecord(manifest.provenance)
  const mode = typeof manifest.mode === 'string' ? manifest.mode : ''
  const templateId =
    provenance !== null && typeof provenance.template_id === 'string'
      ? provenance.template_id
      : ''
  return {
    mode,
    baseVersion: provenance === null ? null : asVersion(provenance.base_version),
    templateId,
    hasProvenance: provenance !== null,
  }
}

/**
 * 预检：该版本能否发起「另存为新基线」（**只挡必然失败的情形**）。
 *
 * 通过 ≠ 一定成功：指纹 / 模板 / 上限仍由服务端判定（1001 / 1003）。
 *
 * @param manifestRaw 产物内 ``patch_manifest.json`` 的原始文本
 * @returns ``{ok, reason}``；``ok=false`` 时 ``reason`` 可直接展示
 */
export function canPromote(manifestRaw: string | null | undefined): PromoteGate {
  if (typeof manifestRaw !== 'string' || manifestRaw.trim() === '') {
    return {
      ok: false,
      reason: '该版本没有 patch_manifest.json 正文（非增量产物），无法另存为新基线',
    }
  }
  let payload: unknown
  try {
    payload = JSON.parse(manifestRaw)
  } catch {
    return {
      ok: false,
      reason: 'patch_manifest.json 无法解析（正文损坏），无法确认该版本可重建',
    }
  }
  const manifest = asRecord(payload)
  if (manifest === null) {
    return {
      ok: false,
      reason: 'patch_manifest.json 结构异常（不是对象），无法确认该版本可重建',
    }
  }
  const mode = typeof manifest.mode === 'string' ? manifest.mode : ''
  if (mode !== 'patch') {
    return {
      ok: false,
      reason: `该产物不是增量产物（mode=${mode || '缺失'}），没有可重建的合并树`,
    }
  }
  return { ok: true, reason: '' }
}

/**
 * 组装二次确认框的三要素行（``run_id`` 前 8 位 / ``base v?`` / **预计**基线版本号）。
 *
 * ``baseVersion`` 为 ``null`` 时**整行省略**（不渲染 ``v-``/``vundefined``）：
 * 版本行拿不到 manifest 正文时不为此新增查询，缺失就是缺失。
 *
 * ★ 目标版本号用「**预计**」而不是「目标」：它是前端按"当前最新基线 + 1"推出的
 * 展示值，服务端可能因幂等命中（``reused=true``）而不新建版本 —— 写成「目标」会被
 * 读成承诺（v1.3.1 QA 可用性反馈）。
 *
 * @param input 三要素入参
 * @returns 逐行文案（顺序固定，便于测试与人读）
 */
export function promoteConfirmLines(input: PromoteConfirmInput): string[] {
  const lines: string[] = []
  const short = shortRunId(input.runId)
  lines.push(short ? `运行 ${short}` : '运行 未知')
  const base = asVersion(input.baseVersion)
  if (base !== null) {
    lines.push(`基线 v${base}`)
  }
  const target = asVersion(input.targetVersion)
  if (target !== null) {
    lines.push(`预计基线版本 v${target}`)
  }
  return lines
}

/**
 * 成功提示：``reused`` 两种口径分开。
 *
 * ``reused=true`` ⇒ 「基线已是该内容」且**不显示任何版本号**（避免用户误以为
 * 刚新建了一个版本）；``reused=false`` ⇒ 显示服务端回执里的新版本号。
 *
 * @param result 服务端回执（``reused`` + ``version``）
 */
export function promoteResultText(result: PromoteOutcome): PromoteMessage {
  if (result.reused) {
    return { type: 'info', text: '基线已是该内容，未新建版本' }
  }
  const version = asVersion(result.version)
  return {
    type: 'success',
    text: version === null ? '已另存为新基线' : `已另存为新基线 v${version}`,
  }
}

/**
 * 失败提示：**服务端 ``message`` 原样上屏**。
 *
 * 1001（参数 / 超上限）与 1003（状态冲突：指纹不符、溯源缺失、模板已变更）的文案
 * 本身就是修复指引（例如「请用当前模板重新跑一次」），任何改写都会让用户无路可走。
 *
 * @param error 捕获到的异常（``BizError`` / ``Error`` / 任意值）
 */
export function promoteErrorText(error: unknown): string {
  if (typeof error === 'object' && error !== null) {
    const record = error as { message?: unknown }
    if (typeof record.message === 'string' && record.message.trim() !== '') {
      return record.message.trim()
    }
  }
  if (typeof error === 'string' && error.trim() !== '') {
    return error.trim()
  }
  return '另存为新基线失败（服务端未返回原因）'
}
