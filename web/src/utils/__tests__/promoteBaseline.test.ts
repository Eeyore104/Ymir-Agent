/**
 * 「另存为新基线」纯逻辑测试（v1.3.1 T5.5 前端 DoD，docs/13 §7.3-8 / §2-T5.4）。
 *
 * 四例对应服务端的四类结局，**每条都断言具体文案与版本号**（不只断言"没抛"）：
 * ① 成功（新建版本）；② 指纹不符被拒（1003）；③ 超基线上限（1001）；④ 幂等命中（reused）。
 *
 * 核心纪律：前端**只挡必然失败**的情形（无正文 / 坏 JSON / 非 ``patch``），
 * 指纹 / 模板 / 上限的结论一律原样透出服务端 ``message``，不许改写成「操作失败」。
 */

import { describe, expect, it } from 'vitest'

import {
  CODE_ARTIFACT_NAME,
  PATCH_MANIFEST_NAME,
  PROMOTE_DISABLED_HINT,
  canPromote,
  isPromotableArtifact,
  parsePromoteManifest,
  promoteConfirmLines,
  promoteErrorText,
  promoteResultText,
  shortRunId,
} from '@/utils/promoteBaseline'

/** 真实形态的 ``patch_manifest.json`` 正文（带合入溯源五元组 + 合并树指纹 + 生效策略）。 */
const PATCH_MANIFEST = JSON.stringify({
  mode: 'patch',
  base_content_hash: 'a1b2c3d4e5f6',
  counts: { added: 1, modified: 1, deleted: 0 },
  changed_paths: ['app/api/tasks.py', 'tests/test_tasks.py'],
  deleted: [],
  conflicts: [],
  provenance: {
    base_artifact_id: 'art-baseline-1',
    base_version: 2,
    base_content_hash: 'a1b2c3d4e5f6',
    patch_files_hash: 'f00dbabe',
    template_id: 'fastapi-vue3',
    template_hash: 'deadbeef',
    merged_files_hash: 'cafebabe',
    protected_policy: 'reject',
    symbol_policy: 'reject',
    max_file_bytes: 262144,
  },
})

describe('utils/promoteBaseline', () => {
  it('成功：预检放行 + 确认框三要素齐全 + 提示带新版本号', () => {
    const gate = canPromote(PATCH_MANIFEST)
    expect(gate).toEqual({ ok: true, reason: '' })

    // 确认框的 base v? 只能来自 manifest 正文（版本行本身不带）；取不到就省略
    const facts = parsePromoteManifest(PATCH_MANIFEST)
    expect(facts).not.toBeNull()
    expect(facts?.mode).toBe('patch')
    expect(facts?.baseVersion).toBe(2)
    expect(facts?.templateId).toBe('fastapi-vue3')
    expect(facts?.hasProvenance).toBe(true)

    const lines = promoteConfirmLines({
      runId: 'run-abcdef1234567890',
      baseVersion: facts?.baseVersion ?? null,
      targetVersion: 3,
    })
    expect(lines).toEqual(['运行 run-abcd', '基线 v2', '预计基线版本 v3'])
    expect(shortRunId('run-abcdef1234567890')).toBe('run-abcd')

    // 正文缺失 ⇒ base 行整行省略（不渲染 v- / vundefined）
    expect(
      promoteConfirmLines({ runId: 'run-abcdef1234567890', baseVersion: null, targetVersion: 1 }),
    ).toEqual(['运行 run-abcd', '预计基线版本 v1'])
    // 「预计」而不是「目标」：服务端可能幂等命中而不新建版本，不能读成承诺
    expect(lines[2]).toContain('预计')
    expect(lines.join(' ')).not.toContain('目标基线版本')

    const msg = promoteResultText({ reused: false, version: 3 })
    expect(msg.type).toBe('success')
    expect(msg.text).toBe('已另存为新基线 v3')

    // 唯一动作入口的名字口径：只有 code 产物可提升
    expect(isPromotableArtifact(CODE_ARTIFACT_NAME)).toBe(true)
    expect(isPromotableArtifact('baseline')).toBe(false)
    expect(isPromotableArtifact(null)).toBe(false)
    expect(PATCH_MANIFEST_NAME).toBe('patch_manifest.json')
  })

  it('指纹不符被拒：1003 原文原样上屏，前端不越权预判', () => {
    const serverMessage = '产物内容不完整（补丁文件指纹与记录不一致）'
    const error = Object.assign(new Error(serverMessage), { code: 1003 })

    const text = promoteErrorText(error)
    expect(text).toBe(serverMessage) // 逐字相等
    expect(text).not.toContain('操作失败')
    expect(text).toContain('指纹') // 修复指引没被吞掉

    // 前端**不该**替服务端下"指纹不符"的结论：正文合法就放行，结论由 1003 给
    expect(canPromote(PATCH_MANIFEST).ok).toBe(true)

    // 但"点了必然失败"的那一类要在预检就挡住：非 patch 模式没有可重建的合并树
    const mergedManifest = JSON.stringify({ mode: 'merge', base_content_hash: 'abc' })
    expect(canPromote(mergedManifest)).toEqual({
      ok: false,
      reason: '该产物不是增量产物（mode=merge），没有可重建的合并树',
    })
    // 缺 mode 的老正文同属"必然失败"（mode 缺失 ⇒ 不是增量产物）
    expect(canPromote(JSON.stringify({ base_content_hash: 'abc' })).reason).toBe(
      '该产物不是增量产物（mode=缺失），没有可重建的合并树',
    )
  })

  it('超基线上限：1001 原文原样上屏；坏正文在预检阶段就被挡下', () => {
    const serverMessage = '合并结果超出基线上限（420 文件 / 8388608 字节；上限 300 / 5 MB）'
    const error = Object.assign(new Error(serverMessage), { code: 1001 })

    expect(promoteErrorText(error)).toBe(serverMessage)
    expect(promoteErrorText(error)).not.toContain('操作失败')

    // 无正文 / 坏 JSON / 非对象：一律 ok=false 且给出可展示原因（绝不抛）
    expect(canPromote(null)).toEqual({
      ok: false,
      reason: '该版本没有 patch_manifest.json 正文（非增量产物），无法另存为新基线',
    })
    expect(canPromote('   ').ok).toBe(false)
    expect(canPromote('{"mode": "patch", ').reason).toBe(
      'patch_manifest.json 无法解析（正文损坏），无法确认该版本可重建',
    )
    expect(canPromote('[]').reason).toBe(
      'patch_manifest.json 结构异常（不是对象），无法确认该版本可重建',
    )
    expect(parsePromoteManifest('{"mode": "patch", ')).toBeNull()

    // 兜底文案只在服务端什么都没给时出现（不是把已知原因改写掉）
    expect(promoteErrorText({})).toBe('另存为新基线失败（服务端未返回原因）')
  })

  it('幂等命中：reused=true 只提示"基线已是该内容"，且不显示任何版本号', () => {
    const msg = promoteResultText({ reused: true, version: 5 })
    expect(msg.type).toBe('info') // 不是错误，也不是"新建成功"
    expect(msg.text).toBe('基线已是该内容，未新建版本')
    expect(msg.text).not.toContain('v5') // 不显示新版本号
    expect(msg.text).not.toContain('5')
    expect(msg.text).not.toMatch(/v\d/)

    // 对照：reused=false 才显示版本号（同一份 version 值，文案必须分叉）
    expect(promoteResultText({ reused: false, version: 5 }).text).toBe('已另存为新基线 v5')

    // 按钮禁用的提示文案与后端"该版本无关联运行"口径一致
    expect(PROMOTE_DISABLED_HINT).toBe('该版本无关联运行')
  })
})
