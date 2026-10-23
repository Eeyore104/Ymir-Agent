/**
 * ``run.context`` 解析与徽标文案的纯函数（v1.2 T4.4）。
 *
 * **兼容纪律**（v1.1 缺陷 #6 的教训）：v1.1 的 ``run.context`` 里**没有**
 * ``template`` / ``baseline`` / ``degraded`` 字段。所有解析函数在字段缺失时
 * 一律返回 ``null`` / 空串，由组件用 ``v-if`` **整块隐藏** —— 绝不返回空串
 * 再渲染成「空壳徽标」。
 */

/** 产物内的机器簿记文件名（与后端 ``merge.PATCH_MANIFEST_NAME`` 对齐）。 */
export const PATCH_MANIFEST_FILE = 'patch_manifest.json'

/** 模板引用快照（``context.template``）。 */
export interface TemplateRef {
  id: string
  /** 模板版本（后端可能给空串）。 */
  version: string
}

/** 基线引用快照（``context.baseline``）。 */
export interface BaselineRef {
  artifactId: string | null
  version: number | null
}

/** 安全取「对象」：数组 / 标量 / null 一律视为不存在。 */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/** 安全取文本：非字符串/数字 -> 空串。 */
function asText(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

/**
 * 解析 ``context.template``（后端快照形状 ``{"id", "version"}``）。
 *
 * @returns ``null`` = 本次运行**没有**用模板（含 v1.1 老帧），此时调用方应整块隐藏徽标。
 */
export function parseTemplateRef(
  context: Record<string, unknown> | null | undefined,
): TemplateRef | null {
  const template = asRecord(context?.['template'])
  if (!template) return null
  const id = asText(template['id'])
  if (!id) return null
  return { id, version: asText(template['version']) }
}

/**
 * 模板降级原因（``context.template.degraded`` 的**原文**）。
 *
 * r5 硬约束：模板加载失败时 run 会降级为通用模式继续跑，**不能静默** ——
 * 用户以为有模板约束、实际没有，比报错更危险。因此这里把原文取出来给 tooltip。
 *
 * 兼容多种形态：``degraded: "原因"`` / ``degraded: true`` / ``degraded: {reason}``。
 *
 * @returns 空串 = 未降级（或字段不存在，含 v1.1 老帧）。
 */
export function parseTemplateDegraded(
  context: Record<string, unknown> | null | undefined,
): string {
  const template = asRecord(context?.['template'])
  if (!template) return ''
  const raw = template['degraded']
  if (typeof raw === 'string') return raw.trim()
  if (raw === true) return '模板加载失败，已降级为通用模式继续运行'
  const nested = asRecord(raw)
  if (nested) {
    const reason = asText(nested['reason']) || asText(nested['message'])
    if (reason) return reason
    return '模板加载失败，已降级为通用模式继续运行'
  }
  return ''
}

/**
 * 解析 ``context.baseline``（后端快照形状 ``{"artifact_id","version",…}``）。
 *
 * @returns ``null`` = 从零生成（含 v1.1 老帧）。
 */
export function parseBaselineRef(
  context: Record<string, unknown> | null | undefined,
): BaselineRef | null {
  const baseline = asRecord(context?.['baseline'])
  if (!baseline) return null
  const rawVersion = baseline['version']
  const version =
    typeof rawVersion === 'number' && Number.isFinite(rawVersion) && rawVersion > 0
      ? Math.trunc(rawVersion)
      : null
  const artifactId = asText(baseline['artifact_id']) || null
  if (version === null && artifactId === null) return null
  return { artifactId, version }
}

/** 模板徽标文案：``模板 {id} v{version}``；无模板返回空串（调用方 v-if 隐藏）。 */
export function templateBadgeLabel(ref: TemplateRef | null): string {
  if (!ref) return ''
  return ref.version ? `模板 ${ref.id} v${ref.version}` : `模板 ${ref.id}`
}

/** 模式徽标文案：``增量 · 基线 v{n}``；无基线返回空串。 */
export function baselineBadgeLabel(ref: BaselineRef | null): string {
  if (!ref) return ''
  return ref.version !== null ? `增量 · 基线 v${ref.version}` : '增量 · 已挂基线'
}

/**
 * 解析 ``patch_manifest.json`` 文本里的 ``reserved_conflicts`` 条数。
 *
 * 语义（docs/09 §4.2.2 优先级 2）：模型试图覆盖模板固定（reserved）文件时，
 * 模型内容被丢弃、骨架被保留，并记一条 ``reserved_conflicts`` —— 不阻断交付，
 * 但必须让用户看见。
 *
 * @returns 条数（解析失败 / 非数组 / 缺字段一律 0，不抛错）。
 */
export function countReservedConflicts(raw: string | null | undefined): number {
  const text = (raw ?? '').trim()
  if (!text) return 0
  try {
    const parsed: unknown = JSON.parse(text)
    const record = asRecord(parsed)
    if (!record) return 0
    const conflicts = record['reserved_conflicts']
    if (Array.isArray(conflicts)) return conflicts.length
    // 兼容只有 counts 汇总的形态
    const counts = asRecord(record['counts'])
    const value = counts?.['reserved_conflicts']
    if (typeof value === 'number' && Number.isFinite(value) && value > 0) return Math.trunc(value)
    return 0
  } catch {
    return 0
  }
}

/**
 * 检查命令来源判定。
 *
 * 报告 JSON **没有**显式记录「checks 来自哪里」，因此判据取 ``run.context.template``
 * 是否存在：有用模板快照 -> 模板 ``checks``；没有 -> 评测套件默认检查。
 *
 * @param hasTemplate 该 run 是否带着模板快照（由 :func:`parseTemplateRef` 判定）
 */
export function checksSourceOf(hasTemplate: boolean): 'template' | 'suite' {
  return hasTemplate ? 'template' : 'suite'
}

/** 检查命令来源文案。 */
export function checksSourceLabel(source: 'template' | 'suite'): string {
  return source === 'template' ? '检查命令来源：模板' : '检查命令来源：套件'
}
