/**
 * 基线展示纯函数测试（v1.2 T4.3 DoD ① ②）。
 *
 * 覆盖：hash 前 8 位、``null`` 安全、``diff-across`` 的 ``name@version`` 引用构造。
 */

import { describe, expect, it } from 'vitest'

import { HASH_DISPLAY_LENGTH, buildDiffRef, formatDateTime, shortHash } from '@/utils/baseline'

describe('utils/baseline', () => {
  it('hash 取前 8 位，缺失返回空串（不渲染 undefined）', () => {
    expect(HASH_DISPLAY_LENGTH).toBe(8)
    expect(shortHash('0123456789abcdef')).toBe('01234567')
    expect(shortHash('  abcdef0123456789  ')).toBe('abcdef01')
    expect(shortHash(null)).toBe('')
    expect(shortHash(undefined)).toBe('')
    expect(shortHash('')).toBe('')
  })

  it('diff-across 引用：有版本 -> name@version，无版本 -> name；非法时间不抛错', () => {
    expect(buildDiffRef('baseline', 3)).toBe('baseline@3')
    expect(buildDiffRef('code', 7)).toBe('code@7')
    expect(buildDiffRef('code', null)).toBe('code')
    expect(buildDiffRef('code', 0)).toBe('code')
    expect(buildDiffRef('', 3)).toBe('')

    expect(formatDateTime('')).toBe('')
    expect(formatDateTime('not-a-date')).toBe('not-a-date')
  })
})
