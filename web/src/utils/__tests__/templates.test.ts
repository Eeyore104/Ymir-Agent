/**
 * 模板选择 / 摘要纯函数测试（v1.2 T4.2 DoD ① ②）。
 *
 * 覆盖：缺省「不使用模板」、坏模板置灰并带原因、摘要卡片的
 * 「目录规范 / 依赖白名单 / 检查命令条数」抽取、空数据不渲染 undefined。
 */

import { describe, expect, it } from 'vitest'

import type { TemplateSummary } from '@/api/types'
import {
  NO_TEMPLATE_VALUE,
  buildTemplateOptions,
  hasTemplateSummary,
  summarizeTemplate,
} from '@/utils/templates'

function makeTemplate(overrides: Partial<TemplateSummary> = {}): TemplateSummary {
  return {
    id: 'fastapi-vue3',
    version: '1.0',
    name: 'FastAPI + Vue3 全栈',
    description: '全栈工程规范包',
    supports: ['from_scratch', 'patch'],
    stack: {
      language: 'python',
      dependencies: ['fastapi', 'uvicorn', 'sqlalchemy'],
    },
    layout: {
      required_dirs: ['app', 'tests', 'web/src'],
      required_files: ['pyproject.toml'],
    },
    conventions: ['README.md', '.gitignore'],
    reserved_paths: ['.env.example', 'pyproject.toml', 'ruff.toml'],
    seed_paths: ['app/main.py', 'requirements.txt'],
    checks: [
      { type: 'command', run: 'python -m compileall -q app' },
      { type: 'command', run: 'python -m pytest -q' },
      { type: 'command', run: 'ruff check .' },
    ],
    prompting: { engineer: '严格遵守模板目录规范' },
    prompting_roles: ['engineer'],
    loading_error: null,
    ...overrides,
  }
}

describe('utils/templates', () => {
  it('下拉首项固定为「不使用模板」，且可选（默认即通用模式）', () => {
    const options = buildTemplateOptions([makeTemplate()])

    expect(options).toHaveLength(2)
    expect(options[0].value).toBe(NO_TEMPLATE_VALUE)
    expect(options[0].value).toBe('')
    expect(options[0].disabled).toBe(false)
    expect(options[0].label).toContain('不使用模板')
  })

  it('坏模板（loading_error 非空）置灰不可选，并透出原因', () => {
    const options = buildTemplateOptions([
      makeTemplate({ id: 'broken', name: '坏模板', loading_error: '模板定义非法: skeleton 超限' }),
      makeTemplate(),
    ])

    const broken = options[1]
    expect(broken.value).toBe('broken')
    expect(broken.disabled).toBe(true)
    expect(broken.reason).toBe('模板定义非法: skeleton 超限')
    expect(broken.label).toContain('不可用')

    // 好模板不受影响
    expect(options[2].disabled).toBe(false)
    expect(options[2].reason).toBe('')
    expect(options[2].label).toContain('v1.0')
  })

  it('摘要卡片抽取目录规范 / 依赖白名单 / 检查命令条数与路径清单', () => {
    const view = summarizeTemplate(makeTemplate())

    expect(view.requiredDirs).toEqual(['app', 'tests', 'web/src'])
    expect(view.dependencies).toEqual(['fastapi', 'uvicorn', 'sqlalchemy'])
    expect(view.checkCount).toBe(3)
    expect(view.reservedPaths).toEqual(['.env.example', 'pyproject.toml', 'ruff.toml'])
    expect(view.seedPaths).toEqual(['app/main.py', 'requirements.txt'])
    expect(hasTemplateSummary(view)).toBe(true)
  })

  it('模板缺省 / 字段缺失时降级为空（不把 undefined 渲染进模板）', () => {
    const empty = summarizeTemplate(null)
    expect(empty.requiredDirs).toEqual([])
    expect(empty.dependencies).toEqual([])
    expect(empty.checkCount).toBe(0)
    expect(hasTemplateSummary(empty)).toBe(false)

    // 后端字段缺失/类型异常时不抛错
    const weird = summarizeTemplate(
      makeTemplate({ layout: {}, stack: { dependencies: 'not-an-array' }, checks: [] }),
    )
    expect(weird.requiredDirs).toEqual([])
    expect(weird.dependencies).toEqual([])
    expect(weird.checkCount).toBe(0)
  })
})
