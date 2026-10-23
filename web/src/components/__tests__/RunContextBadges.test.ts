/**
 * RunContextBadges 组件测试（v1.2 T4.4 DoD ① ② ④）。
 *
 * 挂载真实 DOM（jsdom）后断言：
 * - 有模板 + 有基线 -> 两个徽标都在；
 * - 无模板（v1.1 老帧）-> 不渲染模板徽标，也不出现 undefined / 空壳徽标；
 * - degraded -> 「模板未生效」出现且 title 带原文原因。
 */

import { describe, expect, it } from 'vitest'

import RunContextBadges from '../RunContextBadges.vue'
import { mountToHtml } from './mount'

/** v1.1 形态 payload：没有 template / baseline / degraded。 */
const V11_CONTEXT: Record<string, unknown> = {
  context_refs: [],
  requirement: '做一个任务看板',
}

/** 取所有 el-tag 的文本（去标签与注释），用于「无空壳徽标」断言。 */
function tagTexts(html: string): string[] {
  const texts: string[] = []
  const pattern = /class="el-tag__content">([\s\S]*?)<\/span>/g
  let match = pattern.exec(html)
  while (match !== null) {
    texts.push(
      match[1]
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/<[^>]+>/g, '')
        .trim(),
    )
    match = pattern.exec(html)
  }
  return texts
}

describe('RunContextBadges', () => {
  it('有模板 + 有基线：模板徽标与「增量 · 基线 v2」同时出现', async () => {
    const html = await mountToHtml(RunContextBadges, {
      context: {
        context_refs: [],
        template: { id: 'fastapi-vue3', version: '1.0' },
        baseline: { artifact_id: 'art-baseline', version: 2 },
      },
    })

    expect(html).toContain('模板 fastapi-vue3 v1.0')
    expect(html).toContain('增量 · 基线 v2')
    // 有基线时不再显示「从零」
    expect(tagTexts(html)).not.toContain('从零')
    expect(html).not.toContain('undefined')
    expect(tagTexts(html).every((text) => text.length > 0)).toBe(true)
  })

  it('无模板无基线（v1.2 从零）：只显示「从零」，不出现模板/增量徽标', async () => {
    const html = await mountToHtml(RunContextBadges, { context: { context_refs: [] } })

    expect(tagTexts(html)).toEqual(['从零'])
    expect(html).not.toContain('模板')
    expect(html).not.toContain('增量')
  })

  it('模板降级：出现「模板未生效」，且 title 带 degraded 原文原因', async () => {
    const html = await mountToHtml(RunContextBadges, {
      context: {
        template: { id: 'broken', version: '0.9', degraded: '模板定义非法: skeleton 超限' },
      },
    })

    expect(html).toContain('模板未生效')
    expect(html).toContain('模板定义非法: skeleton 超限')
    // 降级徽标用危险色（视觉上显著，不静默）
    expect(html).toContain('el-tag--danger')
    expect(html).toContain('模板 broken v0.9')
  })

  it('v1.1 老帧兼容：不抛错、无空壳徽标、无 undefined', async () => {
    const html = await mountToHtml(RunContextBadges, { context: V11_CONTEXT })

    // 不出现 v1.2 专属徽标
    expect(html).not.toContain('模板')
    expect(html).not.toContain('模板未生效')
    expect(html).not.toContain('增量')
    // 无 undefined 文本
    expect(html).not.toContain('undefined')
    // 每个徽标都有实际文本（无空壳）
    const texts = tagTexts(html)
    expect(texts.length).toBeGreaterThan(0)
    expect(texts.every((text) => text.length > 0)).toBe(true)
    // 老帧没有基线 -> 从零
    expect(texts).toContain('从零')
  })

  it('run 尚未加载（context 为 null）：整块不渲染任何徽标', async () => {
    const html = await mountToHtml(RunContextBadges, { context: null })

    expect(tagTexts(html)).toEqual([])
    expect(html).not.toContain('undefined')
    expect(html).not.toContain('从零')
  })
})
