/**
 * EvalSourceNotes 组件测试（v1.2 T4.4 DoD ③）。
 *
 * - 「检查命令来源：模板 / 套件」始终显示；
 * - 「骨架冲突 N 处」**仅在 N > 0** 时显示（0 时不渲染空标注）。
 */

import { describe, expect, it } from 'vitest'

import EvalSourceNotes from '../EvalSourceNotes.vue'
import { mountToHtml } from './mount'

describe('EvalSourceNotes', () => {
  it('有模板来源 + 2 处骨架冲突：两条标注都在，并带保留提示', async () => {
    const html = await mountToHtml(EvalSourceNotes, {
      checksSource: 'template',
      conflictCount: 2,
    })

    expect(html).toContain('检查命令来源：模板')
    expect(html).toContain('骨架冲突 2 处')
    expect(html).toContain('模板固定文件已被保留（模型曾尝试覆盖）')
    expect(html).not.toContain('undefined')
  })

  it('套件来源 + 0 冲突：只显示来源标注，冲突标注整块隐藏', async () => {
    const html = await mountToHtml(EvalSourceNotes, {
      checksSource: 'suite',
      conflictCount: 0,
    })

    expect(html).toContain('检查命令来源：套件')
    expect(html).not.toContain('骨架冲突')
    expect(html).not.toContain('模板固定文件已被保留')
    expect(html).not.toContain('undefined')
  })
})
