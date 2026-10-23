/**
 * 基线展示与跨产物 diff 引用的纯函数（v1.2 T4.3）。
 *
 * - 基线版本行需要「hash 前 8 位」与可读时间；
 * - ``GET /projects/{id}/diff-across`` 的 ``base`` / ``target`` 形如
 *   ``name`` 或 ``name@version``（后端 ``_split_ref``）。
 */

/** hash 展示长度（需求：hash 前 8 位）。 */
export const HASH_DISPLAY_LENGTH = 8

/** 取 hash 前 N 位；``null`` / 空串返回空串（避免把 ``undefined`` 渲染进模板）。 */
export function shortHash(hash: string | null | undefined, length = HASH_DISPLAY_LENGTH): string {
  const raw = (hash ?? '').trim()
  if (!raw) return ''
  const size = length > 0 ? length : 0
  return raw.slice(0, size)
}

/** ISO 8601 -> 本地可读时间；非法输入原样返回（不抛异常）。 */
export function formatDateTime(iso: string | null | undefined): string {
  const raw = (iso ?? '').trim()
  if (!raw) return ''
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  return date.toLocaleString('zh-CN', { hour12: false })
}

/**
 * 构造 ``diff-across`` 的产物引用：有版本 -> ``name@version``，无版本 -> ``name``。
 *
 * @param name 产物名（如 ``baseline`` / ``code``）
 * @param version 版本号（缺省或非法 -> 只回名字，后端按最新版本处理）
 */
export function buildDiffRef(name: string | null | undefined, version?: number | null): string {
  const raw = (name ?? '').trim()
  if (!raw) return ''
  if (typeof version === 'number' && Number.isFinite(version) && version > 0) {
    return `${raw}@${Math.trunc(version)}`
  }
  return raw
}
