<!--
  应用外壳：顶部导航（el-menu，六页面）+ 待审批角标。

  角标数据来自审批 Store 的 refreshPendingCount（与审批中心的列表 filter 解耦），
  每 20s 轻量轮询；页面内的实时刷新由各视图自身负责。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute } from 'vue-router'

import { useApprovalsStore } from '@/stores/approvals'

const route = useRoute()
const approvalsStore = useApprovalsStore()

/** 导航项（index 即路由路径，el-menu router 模式直接跳转）。 */
const MENUS: Array<{ path: string; label: string }> = [
  { path: '/', label: '项目看板' },
  { path: '/approvals', label: '审批中心' },
  { path: '/artifacts', label: '产物库' },
  { path: '/evaluation', label: '评测报告' },
  { path: '/settings', label: '设置' },
]

const pendingCount = computed(() => approvalsStore.pendingCount)
const activePath = computed(() => route.path)

let badgeTimer: number | undefined
const BADGE_POLL_MS = 20_000

onMounted(() => {
  void approvalsStore.refreshPendingCount()
  badgeTimer = window.setInterval(() => {
    void approvalsStore.refreshPendingCount()
  }, BADGE_POLL_MS)
})

onBeforeUnmount(() => {
  window.clearInterval(badgeTimer)
})
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <span class="app-title">ymir-agent</span>
      <el-menu
        :default-active="activePath"
        mode="horizontal"
        router
        background-color="#1f2d3d"
        text-color="#b7c4d3"
        active-text-color="#ffffff"
        class="app-menu"
      >
        <el-menu-item v-for="menu in MENUS" :key="menu.path" :index="menu.path">
          {{ menu.label }}
          <el-badge
            v-if="menu.path === '/approvals' && pendingCount > 0"
            :value="pendingCount"
            :max="99"
            type="warning"
            class="menu-badge"
          />
        </el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 24px;
  background: #1f2d3d;
  color: #fff;
  padding: 0 16px;
}

.app-title {
  font-size: 20px;
  font-weight: 600;
  white-space: nowrap;
}

.app-menu {
  flex: 1;
  border-bottom: none;
}

.menu-badge {
  margin-left: 6px;
}

.app-main {
  background: #f5f7fa;
  padding: 16px;
}
</style>
