<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <span class="app-title">ymir-agent</span>
      <span class="app-subtitle">多 Agent 协作的软件开发平台</span>
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
  align-items: baseline;
  gap: 12px;
  background: #1f2d3d;
  color: #fff;
}

.app-title {
  font-size: 20px;
  font-weight: 600;
}

.app-subtitle {
  font-size: 13px;
  color: #b7c4d3;
}

.app-main {
  background: #f5f7fa;
}
</style>
