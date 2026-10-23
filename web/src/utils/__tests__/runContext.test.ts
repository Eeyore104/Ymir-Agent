/**
 * run.context 解析纯函数测试（v1.2 T4.4 DoD ① ② ③ ④ 的判定层）。
 *
 * 重点：v1.1 老帧（context 里没有 template / baseline / degraded）必须返回
 * ``null`` / 空串，让组件用 v-if 整块隐藏 —— 不产生空壳徽标。
 */

import { describe, expect, it } from 'vitest'

import {
  PATCH_MANIFEST_FILE,
  baselineBadgeLabel,
  checksSourceLabel,
  checksSourceOf,
  countReservedConflicts,
  parseBaselineRef,
  parseTemplateDegraded,
  parseTemplateRef,
  templateBadgeLabel,
} from '@/utils/runContext'

/** v1.1 形态的 run.context（**没有** template / baseline / degraded）。 */
const V11_CONTEXT: Record<string, unknown> = {
  context_refs: [],
  requirement: '做一个任务看板',
}

/** v1.2 形态：模板 + 基线。 */
const V12_CONTEXT: Record<string, unknown> = {
  context_refs: [],
  template: { id: 'fastapi-vue3', version: '1.0' },
  baseline: { artifact_id: 'art-baseline', version: 2, content_hash: 'abc', file_count: 12 },
}

describe('utils/runContext', () => {
  it('v1.2 上下文：解析出模板与基线，徽标文案正确', () => {
    const template = parseTemplateRef(V12_CONTEXT)
    expect(template).toEqual({ id: 'fastapi-vue3', version: '1.0' })
    expect(templateBadgeLabel(template)).toBe('模板 fastapi-vue3 v1.0')

    const baseline = parseBaselineRef(V12_CONTEXT)
    expect(baseline).toEqual({ artifactId: 'art-baseline', version: 2 })
    expect(baselineBadgeLabel(baseline)).toBe('增量 · 基线 v2')

    // 未降级
    expect(parseTemplateDegraded(V12_CONTEXT)).toBe('')
  })

  it('v1.1 老帧：所有解析都返回空（null / 空串），不会造出空壳徽标', () => {
    expect(parseTemplateRef(V11_CONTEXT)).toBeNull()
    expect(parseBaselineRef(V11_CONTEXT)).toBeNull()
    expect(parseTemplateDegraded(V11_CONTEXT)).toBe('')

    expect(templateBadgeLabel(null)).toBe('')
    expect(baselineBadgeLabel(null)).toBe('')

    // context 整个缺失（run 尚未加载）同样安全
    expect(parseTemplateRef(null)).toBeNull()
    expect(parseBaselineRef(undefined)).toBeNull()
    expect(parseTemplateDegraded(null)).toBe('')
  })

  it('degraded 兼容三种形态（字符串 / true / 对象），原文原因可取出', () => {
    expect(
      parseTemplateDegraded({ template: { id: 't', degraded: '模板定义非法: skeleton 超限' } }),
    ).toBe('模板定义非法: skeleton 超限')

    expect(parseTemplateDegraded({ template: { id: 't', degraded: true } })).toContain(
      '降级为通用模式',
    )

    expect(
      parseTemplateDegraded({ template: { id: 't', degraded: { reason: 'YAML 解析失败' } } }),
    ).toBe('YAML 解析失败')

    // degraded 为 false / 空串 -> 未降级
    expect(parseTemplateDegraded({ template: { id: 't', degraded: false } })).toBe('')
    expect(parseTemplateDegraded({ template: { id: 't', degraded: '  ' } })).toBe('')
  })

  it('骨架冲突数：解析 patch_manifest.json 的 reserved_conflicts，坏输入一律 0', () => {
    expect(PATCH_MANIFEST_FILE).toBe('patch_manifest.json')

    const manifest = JSON.stringify({
      mode: 'patch',
      changed_paths: ['app/main.py'],
      reserved_conflicts: ['pyproject.toml', 'ruff.toml'],
      counts: { reserved_conflicts: 2 },
    })
    expect(countReservedConflicts(manifest)).toBe(2)

    // 只有 counts 汇总的形态
    expect(countReservedConflicts(JSON.stringify({ counts: { reserved_conflicts: 3 } }))).toBe(3)
    // 无冲突 -> 0（组件据此整块隐藏）
    expect(countReservedConflicts(JSON.stringify({ reserved_conflicts: [] }))).toBe(0)
    // 非法/空输入不抛错
    expect(countReservedConflicts('')).toBe(0)
    expect(countReservedConflicts(null)).toBe(0)
    expect(countReservedConflicts('not-json')).toBe(0)
    expect(countReservedConflicts('[]')).toBe(0)
  })

  it('检查命令来源：有模板 -> 模板，否则套件', () => {
    expect(checksSourceOf(true)).toBe('template')
    expect(checksSourceOf(false)).toBe('suite')
    expect(checksSourceLabel('template')).toBe('检查命令来源：模板')
    expect(checksSourceLabel('suite')).toBe('检查命令来源：套件')
  })
})
