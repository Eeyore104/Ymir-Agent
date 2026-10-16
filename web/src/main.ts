import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

// L1：Element Plus 按需引入（见 vite.config.ts 的 AutoImport / Components + resolver），
// 不再全量注册组件与样式，显著缩小产物体积。
const app = createApp(App)

app.use(createPinia())
app.use(router)
app.mount('#app')
