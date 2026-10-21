/**
 * RepairTimeline 组件测试（v1.1 T5.1）：修复时间线的渲染契约。
 *
 * 说明：本仓库前端测试不引入 ``@vue/test-utils``（避免新增依赖），组件为纯展示型，
 * 用 ``@vue/server-renderer`` 直接渲染成 HTML 断言即可覆盖「徽标/状态/分数/空态」。
 */

import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'

import RepairTimeline from '../RepairTimeline.vue'
import type { RepairRound } from '@/stores/runs'

async function render(rounds: RepairRound[]): Promise<string> {
  const app = createSSRApp({ render: () => h(RepairTimeline, { rounds }) })
  return renderToString(app)
}

const ROUNDS: RepairRound[] = [
  {
    round: 0,
    status: 'completed',
    maxRounds: 2,
    total: 81.3,
    runnability: 80.0,
    artifactVersion: 2,
    durationMs: 120_000,
    costCny: 0.3172,
    trigger: {
      total: 62.4,
      runnability: 48.0,
      failureClass: 'check_failed',
      failedChecks: ['python -m pytest -q', 'ruff check .'],
    },
  },
  {
    round: 1,
    status: 'exhausted',
    maxRounds: 2,
    total: 71.0,
    runnability: 58.0,
    artifactVersion: null,
    stopReason: 'rounds_exhausted',
  },
]

describe('RepairTimeline', () => {
  it('渲染两轮：轮次徽标 / 状态标签 / 分数 / 触发原因', async () => {
    const html = await render(ROUNDS)

    expect(html).toContain('修复时间线')
    expect(html).toContain('第 0 轮')
    expect(html).toContain('第 1 轮')
    expect(html).toContain('已通过')
    expect(html).toContain('已耗尽')
    // 分数：完成轮取本轮的 total/runnability
    expect(html).toContain('81.3')
    expect(html).toContain('80.0')
    // 触发原因（来自 evaluation.failed）：分数 + 失败分类 + 失败检查前 3 条
    expect(html).toContain('62.4')
    expect(html).toContain('check_failed')
    expect(html).toContain('python -m pytest -q')
    // 成本与耗时
    expect(html).toContain('0.3172')
    expect(html).toContain('120.0s')
  })

  it('空数组不渲染；exhausted 显示终止原因且不出现产物跳转', async () => {
    const empty = await render([])
    // v-if 为假时 SSR 只留一个注释占位（<!--v-if-->），去掉注释后应为空
    expect(empty.replace(/<!--[\s\S]*?-->/g, '').trim()).toBe('')

    const exhaustedOnly = await render([ROUNDS[1]])
    // 轮次耗尽：显示可操作提示（accept-best / extend-repair）
    expect(exhaustedOnly).toContain('轮次耗尽')
    // 耗尽轮没有产物版本 -> 不出现「查看 v」跳转按钮
    expect(exhaustedOnly).not.toContain('查看 v')
    // 完成轮（artifactVersion=2）保留产物跳转
    const completedOnly = await render([ROUNDS[0]])
    expect(completedOnly).toContain('查看 v2')
  })
})
