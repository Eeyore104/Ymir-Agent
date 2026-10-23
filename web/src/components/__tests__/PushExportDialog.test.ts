/**
 * PushExportDialog 组件测试（v1.2 T4.3 DoD ③ ④ ⑤）。
 *
 * 渲染契约：
 * - 三步流程齐备且「预演」在「真推」之前；
 * - 拦截规则文案必须含「.env 类文件永不推送」与
 *   「.env.example / .env.sample / .env.template 属文档模板，允许推送」；
 * - 默认推送到新分支 ``ymir/<artifact>-v<n>``；
 * - 目标为 main/master 时出现「我确认推送到受保护分支」，未勾选时真推按钮禁用。
 *
 * 说明：分支保护的**判定逻辑**另由 ``utils/__tests__/exports.test.ts`` 的
 * ``canRealPush`` 用例精确覆盖；此处覆盖「UI 是否按判定置灰 + 文案是否齐备」。
 */

import { describe, expect, it } from 'vitest'

import PushExportDialog from '../PushExportDialog.vue'
import type { Artifact } from '@/api/types'
import { mountToHtml } from './mount'

const ARTIFACT: Artifact = {
  id: 'art-code',
  project_id: 'proj-1',
  name: 'code',
  type: 'code',
  current_version: 3,
  created_at: '2026-02-01T08:00:00Z',
}

/** 取包含某文案的按钮片段（用于断言 disabled 状态）。 */
function buttonSegment(html: string, label: string): string {
  const index = html.indexOf(label)
  if (index < 0) return ''
  const start = html.lastIndexOf('<button', index)
  const end = html.indexOf('</button>', index)
  return html.slice(start < 0 ? 0 : start, end < 0 ? html.length : end)
}

describe('PushExportDialog', () => {
  it('三步齐备（预演 -> 真推 -> 结果）且拦截文案含 .env 黑白名单两句', async () => {
    const html = await mountToHtml(PushExportDialog, {
      modelValue: true,
      projectId: 'proj-1',
      artifact: ARTIFACT,
    })

    // 三步顺序：① 预演 必须先于 ② 真推、③ 结果
    const dryIndex = html.indexOf('① 预演')
    const pushIndex = html.indexOf('② 真推')
    const resultIndex = html.indexOf('③ 结果')
    expect(dryIndex).toBeGreaterThanOrEqual(0)
    expect(pushIndex).toBeGreaterThan(dryIndex)
    expect(resultIndex).toBeGreaterThan(pushIndex)

    // DoD ④：两句固定文案
    expect(html).toContain('.env 类文件永不推送')
    expect(html).toContain(
      '.env.example / .env.sample / .env.template 属文档模板，允许推送',
    )

    // 默认新分支提示
    expect(html).toContain('ymir/code-v3')
    expect(html).not.toContain('undefined')
  })

  it('受保护分支 main：出现确认勾选框，未勾选时真推按钮禁用', async () => {
    const html = await mountToHtml(PushExportDialog, {
      modelValue: true,
      projectId: 'proj-1',
      artifact: ARTIFACT,
      initialBranch: 'main',
    })

    expect(html).toContain('我确认推送到受保护分支')
    // 真推按钮存在但被禁用（未勾选 + 未预演）
    const realPushButton = buttonSegment(html, '确认真推')
    expect(realPushButton).not.toBe('')
    expect(realPushButton).toContain('disabled')
  })

  it('非受保护分支：不出现分支保护勾选框，真推仍需先预演（默认禁用）', async () => {
    const html = await mountToHtml(PushExportDialog, {
      modelValue: true,
      projectId: 'proj-1',
      artifact: ARTIFACT,
    })

    expect(html).not.toContain('我确认推送到受保护分支')
    // 未预演前真推不可点（预演永远在前）
    expect(buttonSegment(html, '确认真推')).toContain('disabled')
    // 预演按钮同样因未填 repo 而禁用，但不渲染 undefined
    expect(buttonSegment(html, '开始预演')).toContain('disabled')
    expect(html).not.toContain('undefined')
  })
})
