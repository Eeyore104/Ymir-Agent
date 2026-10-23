/**
 * 模板选择与摘要的纯函数（v1.2 T4.2）。
 *
 * 抽出为纯函数的原因：模板下拉的「置灰 + 原因」与摘要卡片的「目录规范 / 依赖白名单 /
 * 检查命令条数」是**契约层判定**，用单测直接覆盖，避免把判定散落在模板表达式里。
 */

import type { TemplateSummary } from '@/api/types'

/** 「不使用模板」的选项值（空串 = 不传 ``template_id``）。 */
export const NO_TEMPLATE_VALUE = ''

/** 模板下拉选项（``disabled`` + ``reason`` 用于坏模板置灰并展示原因）。 */
export interface TemplateOption {
  value: string
  label: string
  /** ``true`` = 不可选（``loading_error`` 非空）。 */
  disabled: boolean
  /** 置灰原因（可用模板为空串）。 */
  reason: string
}

/** 模板摘要卡片的展示数据。 */
export interface TemplateSummaryView {
  /** 目录规范：``layout.required_dirs``。 */
  requiredDirs: string[]
  /** 依赖白名单：``stack.dependencies``。 */
  dependencies: string[]
  /** 检查命令条数：``checks.length``。 */
  checkCount: number
  /** 保留路径（逐字节要求精确，模型不得改动）。 */
  reservedPaths: string[]
  /** 种子路径（允许整文件重写）。 */
  seedPaths: string[]
}

/** 从任意 ``unknown`` 里安全提取字符串数组（后端字段缺失/类型异常时降级为空数组）。 */
export function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0)
}

/** 从 ``Record<string, unknown>`` 里安全取数组字段。 */
export function pickStringList(record: Record<string, unknown> | null | undefined, key: string): string[] {
  if (!record || typeof record !== 'object') return []
  return toStringList(record[key])
}

/**
 * 构造模板下拉选项：**首项固定为「不使用模板」且可选中**，
 * 随后按后端顺序追加模板；``loading_error`` 非空的模板置灰并带上原因。
 */
export function buildTemplateOptions(templates: readonly TemplateSummary[]): TemplateOption[] {
  const options: TemplateOption[] = [
    { value: NO_TEMPLATE_VALUE, label: '不使用模板（通用模式）', disabled: false, reason: '' },
  ]
  for (const template of templates) {
    const reason = (template.loading_error ?? '').trim()
    const name = (template.name || template.id || '').trim() || '(未命名模板)'
    const version = (template.version ?? '').trim()
    options.push({
      value: template.id,
      label: reason
        ? `${name}（不可用）`
        : version
          ? `${name} v${version}`
          : name,
      disabled: reason.length > 0,
      reason,
    })
  }
  return options
}

/**
 * 汇总模板摘要卡片数据。
 *
 * 注意：``GET /templates/{id}`` **不含骨架正文**，只有 ``reserved_paths`` /
 * ``seed_paths`` 路径清单 —— 卡片因此只展示路径与计数，永不展示文件内容。
 */
export function summarizeTemplate(template: TemplateSummary | null | undefined): TemplateSummaryView {
  if (!template) {
    return { requiredDirs: [], dependencies: [], checkCount: 0, reservedPaths: [], seedPaths: [] }
  }
  return {
    requiredDirs: pickStringList(template.layout, 'required_dirs'),
    dependencies: pickStringList(template.stack, 'dependencies'),
    checkCount: Array.isArray(template.checks) ? template.checks.length : 0,
    reservedPaths: toStringList(template.reserved_paths),
    seedPaths: toStringList(template.seed_paths),
  }
}

/** 摘要卡片是否有可展示内容（避免渲染一个空卡片）。 */
export function hasTemplateSummary(view: TemplateSummaryView): boolean {
  return (
    view.requiredDirs.length > 0 ||
    view.dependencies.length > 0 ||
    view.checkCount > 0 ||
    view.reservedPaths.length > 0 ||
    view.seedPaths.length > 0
  )
}
