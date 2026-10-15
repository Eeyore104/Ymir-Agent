/**
 * 统一 API 客户端（docs/02 §6.3 信封约定）。
 *
 * - 成功：``{code: 0, data: ...}``；列表：``{code: 0, data: {items, total}}``；
 * - 错误：``{code: <业务码>, message}``（1001 参数 / 1002 不存在 / 1003 状态冲突 / 500 内部）。
 *
 * 本模块是前端访问后端的唯一入口：所有请求经 vite 代理（``/api`` -> 8000），
 * 响应在此统一解包，业务错误转为 :class:`BizError`（保留业务码供调用方判定）。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

/** 统一响应信封。 */
export interface ApiEnvelope<T> {
  code: number
  data: T
  message?: string
}

/** 列表分页数据体。 */
export interface PageData<T> {
  items: T[]
  total: number
}

/** 业务错误（HTTP 层可达，但 code ≠ 0）：保留业务码供上层判定。 */
export class BizError extends Error {
  readonly code: number

  constructor(code: number, message: string) {
    super(message)
    this.name = 'BizError'
    this.code = code
  }
}

const client: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.response.use(
  (response) => {
    // SSE / 非信封响应原样透出（本客户端仅用于 JSON 接口）
    const body = response.data as ApiEnvelope<unknown> | undefined
    if (body && typeof body === 'object' && 'code' in body && body.code !== 0) {
      return Promise.reject(
        new BizError(body.code, body.message ?? `业务错误 ${body.code}`),
      )
    }
    return response
  },
  (error: AxiosError<{ code?: number; message?: string }>) => {
    if (error.response) {
      const body = error.response.data
      const code = body?.code ?? error.response.status
      const message = body?.message ?? `请求失败（HTTP ${error.response.status}）`
      return Promise.reject(new BizError(code, message))
    }
    if (error.code === AxiosError.ECONNABORTED) {
      return Promise.reject(new BizError(-1, '请求超时，请检查后端服务'))
    }
    return Promise.reject(new BizError(-1, `网络错误：${error.message}`))
  },
)

/**
 * 泛型请求助手：直接返回解包后的 ``data``。
 *
 * @param config axios 请求配置（``baseURL`` 可按需覆盖，如 ``/health`` 传 ``'/'``）
 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await client.request<ApiEnvelope<T>>(config)
  return response.data.data as T
}

export default client
