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
  BASELINE_HEALTH_UNKNOWN_REASON,
  BASELINE_UNHEALTHY_REASON,
  FORCE_PROMOTE_LABEL,
  HEALTH_UNKNOWN_CONFIRM_TITLE,
  HEALTH_UNKNOWN_CONFIRM_WARNING,
  PROMOTED_UNHEALTHY_CODE,
  PROMOTED_UNKNOWN_CODE,
  STATE_CONFLICT_CODE,
  UNHEALTHY_CONFIRM_TITLE,
  UNHEALTHY_CONFIRM_WARNING,
  forceConfirmReason,
  forceConfirmTitle,
  forceConfirmWarning,
  promoteConfirmLines,
  promoteErrorText,
  promoteResultText,
  promoteWarningText,
  requiresForceConfirm,
  shortRunId,
  withForce,
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

/**
 * v1.4 T2.2（docs/15 §3.2 / A3）：★3 闸门的前端配合。
 *
 * 三件事必须成立，且都断言**具体文案/字段**（不只断言"没抛"）：
 * ① 只有「1003 + ``details.reason`` ∈ {unhealthy, health_unknown}」才算"需要强制确认"
 *    （其它 1003 不能越；**不靠** ``message`` 里找 ``force`` —— v1.4 返修 P1-3）；
 * ② 带 force 的请求体是**新对象**，原请求体不被改写；
 * ③ 留痕短码出现时必须给出显著提示（不静默），且两种短码的文案不同，没出现时返回空串。
 */
describe('utils/promoteBaseline · unhealthy 闸门（v1.4 T2.2）', () => {
  const PAYLOAD = {
    run_id: 'run-abcdef123456',
    stage_id: 'coding',
    artifact_version: 3,
  }

  it('forceConfirmReason / requiresForceConfirm：只认「1003 + details.reason」', () => {
    // 服务端闸门拒绝：reason 决定一切（文案随便写都不影响判定）
    expect(
      requiresForceConfirm({
        code: STATE_CONFLICT_CODE,
        message: '合并树存在阻断项失败（blocking_failed）或相对基线出现回归（regression）：…',
        details: { reason: BASELINE_UNHEALTHY_REASON, status: 'unhealthy' },
      }),
    ).toBe(true)
    expect(
      forceConfirmReason({
        code: STATE_CONFLICT_CODE,
        message: '该运行还没有可用的合并树健康结论（评测报告缺失或不可读）…',
        details: { reason: BASELINE_HEALTH_UNKNOWN_REASON, status: 'unknown' },
      }),
    ).toBe(BASELINE_HEALTH_UNKNOWN_REASON)

    // ★ 文案里**没有** force 也照样是闸门（旧实现靠 message.includes('force') ⇒ 会漏）
    expect(
      requiresForceConfirm({
        code: STATE_CONFLICT_CODE,
        message: '合并树不健康，拒绝提升',
        details: { reason: BASELINE_UNHEALTHY_REASON },
      }),
    ).toBe(true)

    // 其它 1003（指纹不符 / 缺溯源 / 模板已变更）**不能**用 force 越过
    expect(
      requiresForceConfirm({
        code: STATE_CONFLICT_CODE,
        message: '重建结果与当时评测树不一致（合并树指纹不符），拒绝据此新建基线',
      }),
    ).toBe(false)
    expect(
      requiresForceConfirm({
        code: STATE_CONFLICT_CODE,
        message: '模板已变更，请用当前模板重新跑一次',
        details: { reason: 'template_changed' },
      }),
    ).toBe(false)
    // 别的业务码即使 reason 命中也不算（force 越不过 1001）
    expect(
      requiresForceConfirm({
        code: 1001,
        message: '请带 force=true 重试',
        details: { reason: BASELINE_UNHEALTHY_REASON },
      }),
    ).toBe(false)
    // 非对象 / 空值 / 坏 details 一律 false（绝不抛）
    expect(requiresForceConfirm(null)).toBe(false)
    expect(requiresForceConfirm('force')).toBe(false)
    expect(requiresForceConfirm(new Error('boom'))).toBe(false)
    expect(requiresForceConfirm({ code: STATE_CONFLICT_CODE, details: 'unhealthy' })).toBe(false)
  })

  it('forceConfirmWarning / forceConfirmTitle：按 reason 分支，两种处境不共用文案', () => {
    expect(forceConfirmTitle(BASELINE_UNHEALTHY_REASON)).toBe(UNHEALTHY_CONFIRM_TITLE)
    expect(forceConfirmTitle(BASELINE_HEALTH_UNKNOWN_REASON)).toBe(
      HEALTH_UNKNOWN_CONFIRM_TITLE,
    )
    expect(forceConfirmWarning(BASELINE_HEALTH_UNKNOWN_REASON)).toBe(
      HEALTH_UNKNOWN_CONFIRM_WARNING,
    )
    // 两条文案必须**不同**（"评测说它坏了" ≠ "没有评测说它好"）
    expect(HEALTH_UNKNOWN_CONFIRM_WARNING).not.toBe(UNHEALTHY_CONFIRM_WARNING)
    expect(HEALTH_UNKNOWN_CONFIRM_WARNING).toContain('没有可用')
    // 没有原因 ⇒ 空串（调用方不渲染）
    expect(forceConfirmWarning(null)).toBe('')
    expect(forceConfirmTitle(null)).toBe('')
  })

  it('withForce：返回 force=true 的新对象，原请求体不被改写', () => {
    const forced = withForce(PAYLOAD)

    expect(forced.force).toBe(true)
    expect(forced.run_id).toBe(PAYLOAD.run_id)
    expect(forced.stage_id).toBe('coding')
    expect(forced.artifact_version).toBe(3)
    // 纯函数：入参一个字节都没变（少一个 force 键）
    expect(Object.prototype.hasOwnProperty.call(PAYLOAD, 'force')).toBe(false)
  })

  it('promoteWarningText：两种留痕码各自成句，未出现时返回空串', () => {
    expect(PROMOTED_UNHEALTHY_CODE).toBe('baseline_promoted_unhealthy:1')
    expect(PROMOTED_UNKNOWN_CODE).toBe('baseline_promoted_unknown:1')

    const created = promoteWarningText({
      reused: false,
      version: 4,
      warning: PROMOTED_UNHEALTHY_CODE,
    })
    expect(created).toContain('强制提升不健康的合并树')
    expect(created).toContain('v4')

    // 幂等命中（未新建版本）也要说清，且不编造版本号
    const reused = promoteWarningText({
      reused: true,
      version: 4,
      warning: PROMOTED_UNHEALTHY_CODE,
    })
    expect(reused).toContain('未新建版本')

    // ★ 未知态留痕的文案必须与不健康**分开**（否则用户分不清该修代码还是补评测）
    const unknown = promoteWarningText({
      reused: false,
      version: 5,
      warning: PROMOTED_UNKNOWN_CODE,
    })
    expect(unknown).toContain('没有健康结论')
    expect(unknown).toContain('v5')
    expect(unknown).not.toBe(created)

    // 没有降级 ⇒ 空串（调用方据此走常规成功提示）
    expect(promoteWarningText({ reused: false, version: 4 })).toBe('')
    expect(promoteWarningText({ reused: false, version: 4, warning: 'other:warn' })).toBe('')
  })

  it('UNHEALTHY_CONFIRM_WARNING / FORCE_PROMOTE_LABEL：文案说清后果与出口', () => {
    expect(UNHEALTHY_CONFIRM_WARNING).toContain('不健康')
    expect(UNHEALTHY_CONFIRM_WARNING).toContain('污染后续基线')
    expect(FORCE_PROMOTE_LABEL).toContain('force')
  })
})
