/**
 * 增量改动面解析测试（v1.3 T1.3 DoD）。
 *
 * 覆盖：旧 manifest（无新字段）/ 新 manifest / 坏 JSON / `counts` 汇总形态。
 * 核心纪律：**坏数据一律降级为空值，绝不抛**。
 */

import { describe, expect, it } from 'vitest'

import {
  CHANGE_RATIO_WARNING_PREFIX,
  DEFAULT_CHANGE_RATIO_THRESHOLD,
  formatChangeRatio,
  hasPatchSummary,
  parsePatchSummary,
} from '@/utils/patchSummary'

/** 旧 manifest（v1.2：没有 change_ratio / warnings / counts.protected_hits） */
const LEGACY_MANIFEST = JSON.stringify({
  mode: 'patch',
  base_content_hash: 'abc123',
  counts: {
    added: 2,
    modified: 1,
    noop_dropped: 0,
    deleted: 0,
    skeleton_added: 0,
    reserved_conflicts: 0,
    skipped_existing: 3,
  },
  changed_paths: ['app/api/tasks.py', 'app/models/task.py', 'tests/test_tasks.py'],
  deleted: [],
  conflicts: [],
  skeleton_added: [],
  reserved_conflicts: [],
  skipped_existing: ['pyproject.toml'],
})

/** 新 manifest（v1.3：带 change_ratio / warnings / protected_hits） */
const NEW_MANIFEST = JSON.stringify({
  mode: 'patch',
  base_content_hash: 'def456',
  counts: {
    added: 1,
    modified: 2,
    protected_hits: 1,
  },
  changed_paths: ['app/api/tasks.py', 'app/api/reports.py', 'tests/test_reports.py'],
  conflicts: [{ path: 'tests/conftest.py', reason: 'protected_path' }],
  change_ratio: 0.78,
  warnings: [`${CHANGE_RATIO_WARNING_PREFIX}0.78`, 'protected_path_ignored:1'],
})

describe('utils/patchSummary', () => {
  it('旧 manifest：无新字段时取 counts 汇总，不报超阈值', () => {
    const summary = parsePatchSummary(LEGACY_MANIFEST)
    expect(summary.changedCount).toBe(3) // changed_paths 长度优先
    expect(summary.changeRatio).toBeNull()
    expect(summary.protectedHits).toBe(0)
    expect(summary.warnings).toEqual([])
    expect(summary.exceeded).toBe(false)
    expect(hasPatchSummary(summary)).toBe(true)
  })

  it('新 manifest：解析改动面、受保护命中与告警短码', () => {
    const summary = parsePatchSummary(NEW_MANIFEST)
    expect(summary.changedCount).toBe(3)
    expect(summary.changeRatio).toBeCloseTo(0.78, 5)
    expect(summary.protectedHits).toBe(1)
    expect(summary.exceeded).toBe(true)
    expect(summary.warnings).toContain('protected_path_ignored:1')
    expect(formatChangeRatio(summary.changeRatio)).toBe('78%')
  })

  it('坏 JSON / 空值 / 非对象：一律降级为空值且不抛', () => {
    for (const raw of ['', '   ', 'not-json{', '[]', 'null', undefined, null]) {
      const summary = parsePatchSummary(raw as string | null | undefined)
      expect(summary.changedCount).toBe(0)
      expect(summary.changeRatio).toBeNull()
      expect(summary.protectedHits).toBe(0)
      expect(summary.warnings).toEqual([])
      expect(summary.exceeded).toBe(false)
      expect(hasPatchSummary(summary)).toBe(false)
    }
  })

  it('counts 汇总形态：changed_paths 缺失时用 added+modified+deleted+skeleton_added', () => {
    const raw = JSON.stringify({
      mode: 'patch',
      counts: { added: 4, modified: 2, deleted: 1, skeleton_added: 1, protected_hits: 2 },
    })
    const summary = parsePatchSummary(raw)
    expect(summary.changedCount).toBe(8)
    expect(summary.protectedHits).toBe(2)
    expect(summary.changeRatio).toBeNull()
  })

  it('比较与格式化：阈值可覆盖，比例边界不误判超阈值', () => {
    expect(DEFAULT_CHANGE_RATIO_THRESHOLD).toBe(0.5)
    const atThreshold = parsePatchSummary(JSON.stringify({ change_ratio: 0.5 }), 0.5)
    expect(atThreshold.exceeded).toBe(false) // 等于阈值不算超
    const custom = parsePatchSummary(JSON.stringify({ change_ratio: 0.6 }), 0.8)
    expect(custom.exceeded).toBe(false) // 阈值调高后不告警
    expect(formatChangeRatio(null)).toBe('')
    expect(formatChangeRatio(0.5)).toBe('50%')
    expect(formatChangeRatio(0.125)).toBe('12.5%')
  })
})
