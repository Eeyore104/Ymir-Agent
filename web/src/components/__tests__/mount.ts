/**
 * 组件挂载助手（仅测试用，**不引入新依赖**）：用 ``vue`` 的 ``createApp`` 在 jsdom 里
 * 真实挂载组件，再读回 ``document.body.innerHTML``。
 *
 * 为什么不用 ``renderToString``（SSR）：
 * - Element Plus 的 ``el-table`` 列在客户端注册，SSR 只输出空 ``<tbody>``；
 * - ``el-dialog`` 的内容在 transition 进入后才渲染，SSR 下为空壳。
 * 因此「行内容 / 按钮禁用态 / 对话正文」这类断言必须在客户端挂载后读取。
 */

import { createApp, nextTick, type Component } from 'vue'

/** 挂载组件并返回渲染后的 body HTML（自动多重 tick 等待子组件注册完成）。 */
export async function mountToHtml(
  component: Component,
  props: Record<string, unknown> = {},
): Promise<string> {
  document.body.innerHTML = ''
  const host = document.createElement('div')
  document.body.appendChild(host)

  const app = createApp(component, props)
  try {
    app.mount(host)
    // el-table / el-dialog 需要若干 tick 完成列注册与 teleport 内容渲染
    await nextTick()
    await nextTick()
    await nextTick()
    return document.body.innerHTML
  } finally {
    app.unmount()
    document.body.innerHTML = ''
  }
}
