/**
 * BaselinePanel 组件测试（v1.2 T4.3 DoD ①）。
 *
 * 渲染契约：基线版本行展示 vN / 文件数 / hash 前 8 位 / 上传时间，
 * 并提供「与基线对比」入口；无基线时给可操作空态，且不出现 undefined。
 */

import { describe, expect, it } from 'vitest'

import BaselinePanel from '../BaselinePanel.vue'
import type { ArtifactDetail } from '@/api/types'
import { mountToHtml } from './mount'

const DETAIL: ArtifactDetail = {
  id: 'art-baseline',
  project_id: 'proj-1',
  name: 'baseline',
  type: 'baseline',
  current_version: 2,
  created_at: '2026-02-01T08:00:00Z',
  versions: [
    {
      id: 'v-1',
      artifact_id: 'art-baseline',
      version: 1,
      run_id: null,
      stage_id: null,
      storage_path: 'artifacts/art-baseline/v1',
      content_hash: '0123456789abcdef0123456789abcdef',
      file_count: 12,
      size_bytes: 4096,
      message: '用户上传基线（12 文件 / 4096 字节）',
      parent_version_id: null,
      created_by: 'user',
      created_at: '2026-02-01T08:00:00Z',
    },
    {
      id: 'v-2',
      artifact_id: 'art-baseline',
      version: 2,
      run_id: null,
      stage_id: null,
      storage_path: 'artifacts/art-baseline/v2',
      content_hash: 'fedcba9876543210fedcba9876543210',
      file_count: 15,
      size_bytes: 8192,
      message: '用户上传基线（15 文件 / 8192 字节）',
      parent_version_id: 'v-1',
      created_by: 'user',
      created_at: '2026-02-02T09:30:00Z',
    },
  ],
}

describe('BaselinePanel', () => {
  it('展示基线版本：vN / 文件数 / hash 前 8 位 / 上传时间 / 对比入口', async () => {
    const html = await mountToHtml(BaselinePanel, { detail: DETAIL })

    expect(html).toContain('项目基线')
    expect(html).toContain('v1')
    expect(html).toContain('v2')
    // 文件数
    expect(html).toContain('12')
    expect(html).toContain('15')
    // hash 前 8 位（完整 32 位不应出现）
    expect(html).toContain('01234567')
    expect(html).toContain('fedcba98')
    expect(html).not.toContain('0123456789abcdef0123456789abcdef')
    // 上传时间与对比入口
    expect(html).toContain('2026')
    expect(html).toContain('与基线对比')
    expect(html).toContain('diff-across')
  })

  it('无基线时展示空态提示，且不渲染 undefined', async () => {
    const html = await mountToHtml(BaselinePanel, { detail: null })

    expect(html).toContain('还没有基线')
    expect(html).toContain('无基线')
    expect(html).not.toContain('undefined')
    // 无版本时不出现在表格行级的对比按钮
    expect(html).not.toContain('与基线对比</button>')
  })

  it('hash 缺失时显示占位符而非空串', async () => {
    const detail: ArtifactDetail = {
      ...DETAIL,
      versions: [{ ...DETAIL.versions[0], content_hash: null }],
    }
    const html = await mountToHtml(BaselinePanel, { detail })

    expect(html).toContain('—')
    expect(html).not.toContain('undefined')
  })
})
