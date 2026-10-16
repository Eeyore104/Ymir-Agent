/**
 * client 单测（L7）：统一响应信封解包与 BizError 转换（§6.3）。
 *
 * 做法：mock axios 实例并**捕获响应拦截器**，直接驱动其成功 / 失败分支，
 * 断言业务码透传、HTTP 错误降级与 -1（超时 / 网络）约定。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  onFulfilled: null as unknown,
  onRejected: null as unknown,
  request: vi.fn(),
}))

vi.mock('axios', () => {
  class FakeAxiosError extends Error {
    static readonly ECONNABORTED = 'ECONNABORTED'
    code?: string
    response?: unknown

    constructor(message: string) {
      super(message)
      this.name = 'AxiosError'
    }
  }
  return {
    default: {
      create: () => ({
        interceptors: {
          response: {
            use: (fulfilled: unknown, rejected: unknown) => {
              h.onFulfilled = fulfilled
              h.onRejected = rejected
            },
          },
        },
        request: h.request,
      }),
    },
    AxiosError: FakeAxiosError,
  }
})

// vi.mock 已提升，静态导入即可拿到被 mock 的 axios
import { BizError, request } from './client'

type Handler = (value: unknown) => unknown
const onFulfilled = (): Handler => h.onFulfilled as Handler
const onRejected = (): Handler => h.onRejected as Handler

describe('api client：信封解包', () => {
  beforeEach(() => {
    h.request.mockReset()
  })

  it('成功：返回解包后的 data', async () => {
    h.request.mockResolvedValue({ data: { code: 0, data: { id: 'p1' } } })
    await expect(request<{ id: string }>({ method: 'get', url: '/projects' })).resolves.toEqual({
      id: 'p1',
    })
    expect(h.request).toHaveBeenCalledWith({ method: 'get', url: '/projects' })
  })

  it('成功：信封无 data 字段时返回 undefined（不抛）', async () => {
    h.request.mockResolvedValue({ data: { code: 0 } })
    await expect(request({ method: 'get', url: '/x' })).resolves.toBeUndefined()
  })
})

describe('api client：业务错误转换', () => {
  beforeEach(() => {
    h.request.mockReset()
  })

  it('code≠0 -> BizError(业务码, message)', async () => {
    await expect(onFulfilled()({ data: { code: 1002, message: '项目不存在' } })).rejects.toMatchObject(
      { name: 'BizError', code: 1002, message: '项目不存在' },
    )
  })

  it('code≠0 且无 message -> 兜底文案带业务码', async () => {
    await expect(onFulfilled()({ data: { code: 1003 } })).rejects.toMatchObject({
      code: 1003,
      message: '业务错误 1003',
    })
  })

  it('HTTP 错误：优先取响应体业务码', async () => {
    const err = { response: { status: 404, data: { code: 1002, message: '运行不存在' } } }
    await expect(onRejected()(err)).rejects.toMatchObject({ code: 1002, message: '运行不存在' })
  })

  it('HTTP 错误：响应体无业务码 -> 回落 HTTP 状态码', async () => {
    const err = { response: { status: 503, data: {} } }
    await expect(onRejected()(err)).rejects.toMatchObject({ code: 503, message: '请求失败（HTTP 503）' })
  })

  it('超时（ECONNABORTED）-> BizError(-1) 固定文案', async () => {
    const err = { code: 'ECONNABORTED', message: 'timeout of 30000ms exceeded' }
    await expect(onRejected()(err)).rejects.toMatchObject({
      code: -1,
      message: '请求超时，请检查后端服务',
    })
  })

  it('网络错误 -> BizError(-1) 且带原始 message', async () => {
    const err = { code: 'ERR_NETWORK', message: 'Network Error' }
    await expect(onRejected()(err)).rejects.toMatchObject({ code: -1, message: '网络错误：Network Error' })
  })

  it('BizError 保留业务码（ instanceof 校验）', async () => {
    const outcome = onFulfilled()({
      data: { code: 1004, message: '预算超限' },
    }) as Promise<unknown>
    const error = await outcome.catch((e: unknown) => e)
    expect(error).toBeInstanceOf(BizError)
    expect((error as BizError).code).toBe(1004)
  })
})
