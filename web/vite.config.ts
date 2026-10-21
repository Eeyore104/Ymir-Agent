import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// docker-compose 场景经 YMIR_API_TARGET 指向 backend 服务；未设置时同本地默认（行为不变）
const API_TARGET = process.env.YMIR_API_TARGET ?? 'http://127.0.0.1:8000'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // L1：Element Plus 按需引入 —— 模板组件（Components）、函数式组件 ElMessage /
    // 指令（AutoImport + Components 的 resolver）均按需注入（含样式），
    // 替代 main.ts 全量注册，避免打入整包（原 element-plus ~939kB）。
    AutoImport({ resolvers: [ElementPlusResolver()], dts: 'src/auto-imports.d.ts' }),
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    // 分包优化（T5-5②）：按模块路径把体积大的第三方库拆为独立 vendor chunk。
    // 注意（L1）：element-plus **故意不做 manualChunks** —— 对 element-plus 的
    // barrel（es/index 全量再导出）指定 manualChunk 会把整个依赖图强制并入单 chunk、
    // 压掉 rollup 的 tree-shaking（实测仍产出 ~939kB 整包）；移出 manualChunks 后
    // 按需引入生效，整包消失、总 JS 由 ~1255kB 降至 ~700kB。vue-vendor / markdown
    // 两个分包常量体积、缓存收益稳定，保留不动。
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('markdown-it')) return 'markdown'
          if (
            id.includes('/vue/') ||
            id.includes('vue-router') ||
            id.includes('@vue/') ||
            id.includes('pinia')
          ) {
            return 'vue-vendor'
          }
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/health': { target: API_TARGET, changeOrigin: true },
    },
  },
  // L7：前端单测（vitest，纯逻辑模块级；不碰 DOM 渲染）。
  // 配置并入 vite.config.ts（复用 alias / resolve），避免额外配置文件与重复维护。
  test: {
    environment: 'jsdom',
    globals: false,
    // 默认 include 已覆盖 src/**/*.test.ts；显式声明便于阅读
    include: ['src/**/*.test.ts'],
    // 顺序执行：并行 worker 会并发写同一份 vite/vitest 缓存文件，在部分环境
    // （沙箱 fs 拦截）下间歇 EPERM，并导致收集阶段丢文件（实测并行 8/10）。
    fileParallelism: false,
    // T5.1：组件测试用 ``@vue/server-renderer`` 渲染 Element Plus 组件，
    // 需让 vitest 内联处理 element-plus（否则其 ``style/css`` 子路径的 .css
    // 会以原生 ESM 方式加载，报 Unknown file extension ".css"）。
    server: { deps: { inline: ['element-plus'] } },
    css: false,
  },
})
