/**
 * 增量 patch 摘要解析（v1.3 T1.3，docs/11 §7.4）。
 *
 * 数据来源：产物内的 `patch_manifest.json`（由 `artifacts.merge.PatchManifest` 序列化）。
 *
 * ★ 容错纪律（docs/11 §7.5-2）：字段缺失 / `counts` 汇总形态 / 坏 JSON **一律降级为空值，
 * 绝不抛** —— 这是审批卡片上的辅助信息，不能因为它让整张卡片白屏。
 */

/** 改动面告警阈值（与服务端 `DEFAULT_MAX_CHANGE_RATIO` 同源；仅在前端做兜底判断） */
export const DEFAULT_CHANGE_RATIO_THRESHOLD = 0.5

/** manifest 的 warnings 短码前缀（docs/11 §7.5-1） */
export const CHANGE_RATIO_WARNING_PREFIX = 'change_ratio_exceeded:'
export const PROTECTED_HIT_WARNING_PREFIX = 'protected_path_ignored:'
export const PROTECTED_FORCED_IGNORE_PREFIX = 'protected_path_forced_ignore:'

/** 解析结果（全部字段都有确定的空值形态） */
export interface PatchSummary {
  /** 变更文件数（`changed_paths` 缺失时由 `counts` 汇总推出） */
  changedCount: number
  /** 改动面（0-1 小数；无数据为 null，前端负责 ×100 与 %） */
  changeRatio: number | null
  /** 受保护路径被拦截条数 */
  protectedHits: number
  /** 机器短码告警（原样保留，渲染时翻译） */
  warnings: string[]
  /** 改动面是否超阈值（服务端短码优先，缺省时按比例兜底判断） */
  exceeded: boolean
}

/** 空值形态（每次返回新对象：`warnings` 数组不可被调用方意外共享） */
function emptySummary(): PatchSummary {
  return { changedCount: 0, changeRatio: null, protectedHits: 0, warnings: [], exceeded: false }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function asNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return []
  }
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0)
}

/** `counts` 汇总形态的变更文件数（`changed_paths` 缺失时的兜底来源） */
function countFromCounts(counts: Record<string, unknown> | null): number {
  if (counts === null) {
    return 0
  }
  const keys = ['added', 'modified', 'deleted', 'skeleton_added']
  return keys.reduce((sum, key) => {
    const value = asNumber(counts[key])
    return sum + (value === null || value < 0 ? 0 : Math.trunc(value))
  }, 0)
}

/** 受保护路径命中条数（`counts.protected_hits` 优先，其次数 conflicts） */
function protectedHitsOf(
  counts: Record<string, unknown> | null,
  conflicts: unknown,
): number {
  const fromCounts = counts === null ? null : asNumber(counts.protected_hits)
  if (fromCounts !== null && fromCounts > 0) {
    return Math.trunc(fromCounts)
  }
  if (!Array.isArray(conflicts)) {
    return 0
  }
  return conflicts.filter((item) => asRecord(item)?.reason === 'protected_path').length
}

/**
 * 解析 `patch_manifest.json` 正文。
 *
 * @param raw 产物内 `patch_manifest.json` 的原始文本（可为 null / 空串 / 坏 JSON）
 * @param threshold 改动面阈值（缺省 0.5；仅用于服务端短码缺失时的兜底判断）
 */
export function parsePatchSummary(
  raw: string | null | undefined,
  threshold: number = DEFAULT_CHANGE_RATIO_THRESHOLD,
): PatchSummary {
  if (typeof raw !== 'string' || raw.trim() === '') {
    return emptySummary()
  }
  let payload: unknown
  try {
    payload = JSON.parse(raw)
  } catch {
    // 坏 JSON：静默降级（绝不抛，卡片照常展示）
    return emptySummary()
  }
  const manifest = asRecord(payload)
  if (manifest === null) {
    return emptySummary()
  }

  const counts = asRecord(manifest.counts)
  const changedPaths = asStringArray(manifest.changed_paths)
  const changedCount = changedPaths.length > 0 ? changedPaths.length : countFromCounts(counts)
  const changeRatio = asNumber(manifest.change_ratio)
  const warnings = asStringArray(manifest.warnings)
  const protectedHits = protectedHitsOf(counts, manifest.conflicts)
  const warned = warnings.some((item) => item.startsWith(CHANGE_RATIO_WARNING_PREFIX))
  const exceeded =
    warned || (changeRatio !== null && changeRatio > (Number.isFinite(threshold) ? threshold : 0.5))

  return { changedCount, changeRatio, protectedHits, warnings, exceeded }
}

/** 改动面渲染文本（`0.78` -> `78%`；无数据 -> 空串） */
export function formatChangeRatio(ratio: number | null): string {
  if (ratio === null || !Number.isFinite(ratio)) {
    return ''
  }
  const percent = ratio * 100
  const rounded = Math.round(percent * 10) / 10
  return Number.isInteger(rounded) ? `${rounded}%` : `${rounded.toFixed(1)}%`
}

/** 是否有任何可展示的信息（无数据时整块隐藏） */
export function hasPatchSummary(summary: PatchSummary): boolean {
  return summary.changedCount > 0 || summary.changeRatio !== null || summary.protectedHits > 0
}
