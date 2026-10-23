/**
 * 推送目标 / 回执纯函数测试（v1.2 T4.3 DoD ⑤）。
 *
 * 覆盖：默认新分支 ``ymir/<artifact>-v<n>``、受保护分支判定、
 * 「未勾选 -> 不可真推」、请求体字段纪律（不发 token、allow_protected_branch 只在勾选后为 true）、
 * 失败码指引。
 */

import { describe, expect, it } from 'vitest'

import {
  PROTECTED_BRANCHES,
  buildDefaultBranch,
  buildExportPayload,
  buildExportTarget,
  canRealPush,
  exportErrorHint,
  isProtectedBranch,
  resolveBranch,
  shortSha,
} from '@/utils/exports'

describe('utils/exports', () => {
  it('默认新分支为 ymir/<artifact>-v<n>，无版本时退化为 ymir/<artifact>', () => {
    expect(buildDefaultBranch('code', 3)).toBe('ymir/code-v3')
    expect(buildDefaultBranch('code', null)).toBe('ymir/code')
    expect(buildDefaultBranch('', 0)).toBe('ymir/artifact')
    // 用户输入优先，空输入回落默认分支
    expect(resolveBranch('release/1.0', 'code', 3)).toBe('release/1.0')
    expect(resolveBranch('  ', 'code', 3)).toBe('ymir/code-v3')
  })

  it('受保护分支判定：main / master 为 true，其余 false', () => {
    expect(PROTECTED_BRANCHES).toEqual(['main', 'master'])
    expect(isProtectedBranch('main')).toBe(true)
    expect(isProtectedBranch(' master ')).toBe(true)
    expect(isProtectedBranch('ymir/code-v3')).toBe(false)
    expect(isProtectedBranch('')).toBe(false)
    expect(isProtectedBranch(null)).toBe(false)
  })

  it('受保护分支未勾选时不可真推，勾选后才可', () => {
    // DoD ⑤：main 未勾选 -> 真推禁用
    expect(canRealPush('main', false)).toBe(false)
    expect(canRealPush('master', false)).toBe(false)
    expect(canRealPush('main', true)).toBe(true)
    // 非受保护分支默认可推
    expect(canRealPush('ymir/code-v3', false)).toBe(true)
  })

  it('请求体：allow_protected_branch 只在勾选后为 true，且永不携带凭证', () => {
    const normal = buildExportPayload({
      repo: ' acme/demo ',
      branch: 'ymir/code-v3',
      artifactId: 'art-1',
      artifactVersion: 3,
      dryRun: true,
    })
    expect(normal.dry_run).toBe(true)
    expect(normal.tree).toBe('merged')
    expect(normal.artifact_version).toBe(3)
    expect(normal.target.repo).toBe('acme/demo')
    expect(normal.target.allow_protected_branch).toBe(false)
    expect(Object.keys(normal)).not.toContain('token')
    expect(Object.keys(normal.target)).not.toContain('token')

    const protectedPush = buildExportPayload({
      repo: 'acme/demo',
      branch: 'main',
      artifactId: 'art-1',
      allowProtectedBranch: true,
      dryRun: false,
    })
    expect(protectedPush.dry_run).toBe(false)
    expect(protectedPush.target.allow_protected_branch).toBe(true)
    expect(protectedPush.target.branch).toBe('main')
  })

  it('空分支发 null（后端生成默认新分支），dry_run 缺省为 true（安全默认）', () => {
    const target = buildExportTarget({ repo: 'acme/demo', branch: '  ' })
    expect(target.branch).toBeNull()
    expect(target.path_prefix).toBe('')
    expect(target.kind).toBe('github')

    const payload = buildExportPayload({ repo: 'acme/demo', artifactId: 'art-1' })
    // 未显式传 dryRun -> 预演（绝不静默真推）
    expect(payload.dry_run).toBe(true)
    expect(payload.artifact_version).toBeUndefined()
  })

  it('commit sha 短链取前 7 位；失败码有可操作指引', () => {
    expect(shortSha('abcdef1234567890')).toBe('abcdef1')
    expect(shortSha(null)).toBe('')
    expect(shortSha('')).toBe('')

    expect(exportErrorHint(1001)).toContain('受保护分支')
    expect(exportErrorHint(1003)).toContain('冲突')
    expect(exportErrorHint(1007)).toContain('token')
    expect(exportErrorHint(99)).toContain('未完成')
  })
})
