<script setup lang="ts">
import { Monitor, List, TrendCharts, DataAnalysis, Setting } from '@element-plus/icons-vue'
</script>

<template>
  <el-container class="app-container">
    <el-aside width="200px" class="app-aside">
      <div class="logo">Arbitrage-Mi</div>
      <el-menu
        :default-active="$route.path"
        router
        class="app-menu"
        background-color="transparent"
        text-color="#9aa0a6"
        active-text-color="#2196f3"
      >
        <el-menu-item index="/">
          <el-icon><Monitor /></el-icon>
          <span>订单簿监控</span>
        </el-menu-item>
        <el-menu-item index="/orders">
          <el-icon><List /></el-icon>
          <span>订单管理</span>
        </el-menu-item>
        <el-menu-item index="/positions">
          <el-icon><TrendCharts /></el-icon>
          <span>持仓监控</span>
        </el-menu-item>
        <el-sub-menu index="settings">
          <template #title>
            <el-icon><Setting /></el-icon>
            <span>参数设置</span>
          </template>
          <el-menu-item index="/settings/threshold">
            <el-icon><DataAnalysis /></el-icon>
            <span>VWAP基差阈值设置</span>
          </el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.app-container {
  height: 100vh;
  background-color: var(--app-bg);
}

.app-aside {
  background-color: var(--app-sidebar);
  border-right: 1px solid var(--app-border);
}

.logo {
  height: 60px;
  line-height: 60px;
  text-align: center;
  color: var(--app-text);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
  border-bottom: 1px solid var(--app-border);
}

.app-menu {
  border-right: none;
}

.app-menu :deep(.el-menu-item) {
  height: 48px;
  line-height: 48px;
}

.app-menu :deep(.el-sub-menu__title) {
  height: 48px;
  line-height: 48px;
}

.app-menu :deep(.el-sub-menu .el-menu-item) {
  height: 44px;
  line-height: 44px;
  min-width: auto;
  padding-left: 52px !important;
}

.app-menu :deep(.el-menu-item.is-active) {
  background-color: rgba(33, 150, 243, 0.12) !important;
}

.app-menu :deep(.el-menu-item:hover) {
  background-color: rgba(255, 255, 255, 0.04) !important;
}

.app-main {
  padding: 16px;
  background-color: var(--app-bg);
  overflow: auto;
}
</style>
