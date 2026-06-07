<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Monitor, List, TrendCharts, DataAnalysis, Setting, SwitchButton, Fold, Expand, Connection, Stopwatch, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { removeToken } from './utils/auth'
import { ElMessage } from 'element-plus'
import { post, get } from './utils/request'

const route = useRoute()
const router = useRouter()
const isCollapsed = ref(false)
let openPauseStatusTimer: ReturnType<typeof setInterval> | null = null

// 判断是否为登录页
const isLoginPage = computed(() => route.name === 'login')

// ───── 成交引擎模式标识 ─────
type TradingMode = 'virtual' | 'testnet' | 'mainnet' | 'unknown'
const tradingMode = ref<TradingMode>('unknown')

const tradingModeLabel = computed(() => {
  switch (tradingMode.value) {
    case 'mainnet': return '实盘'
    case 'testnet': return '模拟盘'
    case 'virtual': return '虚拟盘'
    default: return ''
  }
})

const tradingModeColor = computed(() => {
  switch (tradingMode.value) {
    case 'mainnet': return '#f56c6c'   // 红色
    case 'testnet': return '#e6a23c'   // 橙色
    case 'virtual': return '#909399'   // 灰色
    default: return 'transparent'
  }
})

// ───── 开仓暂停开关 ─────
const openPaused = ref(false)
const openPauseLoading = ref(false)

const openPauseTitle = computed(() => (openPaused.value ? '恢复开仓' : '暂停开仓'))
const openPauseIcon = computed(() => (openPaused.value ? VideoPlay : VideoPause))

async function fetchTradingMode() {
  try {
    const resp = await get('/api/service/exchange-connectivity')
    if (resp.ok) {
      const data = await resp.json()
      if (!data.is_real) {
        tradingMode.value = 'virtual'
      } else {
        tradingMode.value = data.detail?.env === 'mainnet' ? 'mainnet' : 'testnet'
      }
    }
  } catch {
    // 服务未启动时静默失败
  }
}

async function fetchOpenPausedStatus() {
  try {
    const res = await get('/api/trading/open/status')
    const data = await res.json()
    openPaused.value = !!data.open_paused
  } catch {
    // 服务未启动时静默失败
  }
}

async function toggleOpenPause() {
  openPauseLoading.value = true
  try {
    const url = openPaused.value ? '/api/trading/open/resume' : '/api/trading/open/pause'
    const res = await post(url)
    const data = await res.json()
    if (data.ok) {
      openPaused.value = data.open_paused
      ElMessage.success(data.open_paused ? '开仓已暂停，平仓不受影响' : '开仓已恢复')
    }
  } catch (e: any) {
    ElMessage.error(`操作失败: ${e.message || '未知错误'}`)
  } finally {
    openPauseLoading.value = false
  }
}

function startOpenPauseStatusTimer() {
  fetchTradingMode()
  fetchOpenPausedStatus()
  if (!openPauseStatusTimer) {
    openPauseStatusTimer = setInterval(fetchOpenPausedStatus, 10000)
  }
}

function stopOpenPauseStatusTimer() {
  if (openPauseStatusTimer) {
    clearInterval(openPauseStatusTimer)
    openPauseStatusTimer = null
  }
}

onMounted(() => {
  if (!isLoginPage.value) {
    startOpenPauseStatusTimer()
  }
})

watch(isLoginPage, (loginPage) => {
  if (loginPage) {
    stopOpenPauseStatusTimer()
  } else {
    startOpenPauseStatusTimer()
  }
})

onUnmounted(() => {
  stopOpenPauseStatusTimer()
})

async function handleLogout() {
  try {
    // 调用后端登出接口 (可选)
    await post('/api/auth/logout')
  } catch {
    // 忽略错误
  }
  
  removeToken()
  ElMessage.success('已登出')
  router.push({ name: 'login' })
}

function toggleMenu() {
  isCollapsed.value = !isCollapsed.value
}
</script>

<template>
  <!-- 登录页不显示侧边栏 -->
  <router-view v-if="isLoginPage" />
  
  <!-- 其他页面显示完整布局 -->
  <el-container v-else class="app-container">
    <el-aside :width="isCollapsed ? '64px' : '200px'" class="app-aside">
      <div class="logo">
        <div class="logo-title">
          <span v-if="!isCollapsed">Arbitrage-Mi</span>
          <span v-else>Ai</span>
        </div>
        <div v-if="tradingMode !== 'unknown'" class="trading-mode-badge" :style="{ backgroundColor: tradingModeColor }">
          <span v-if="!isCollapsed">{{ tradingModeLabel }}</span>
          <span v-else>{{ tradingModeLabel[0] }}</span>
        </div>
      </div>
      
      <el-menu
        :default-active="$route.path"
        router
        :collapse="isCollapsed"
        class="app-menu"
        background-color="transparent"
        text-color="#9aa0a6"
        active-text-color="#2196f3"
      >
        <el-menu-item index="/">
          <el-icon><Monitor /></el-icon>
          <template #title>订单簿监控</template>
        </el-menu-item>
        <el-menu-item index="/signals">
          <el-icon><Stopwatch /></el-icon>
          <template #title>交易信号</template>
        </el-menu-item>
        <el-menu-item index="/orders">
          <el-icon><List /></el-icon>
          <template #title>订单管理</template>
        </el-menu-item>
        <el-menu-item index="/positions">
          <el-icon><TrendCharts /></el-icon>
          <template #title>持仓监控</template>
        </el-menu-item>
        <el-menu-item index="/capital">
          <el-icon><TrendCharts /></el-icon>
          <template #title>资金监控</template>
        </el-menu-item>
        <el-menu-item index="/reconciliation">
          <el-icon><DataAnalysis /></el-icon>
          <template #title>持仓对账</template>
        </el-menu-item>
        <el-menu-item index="/connections">
          <el-icon><Connection /></el-icon>
          <template #title>连接状态</template>
        </el-menu-item>
        <el-sub-menu index="settings">
          <template #title>
            <el-icon><Setting /></el-icon>
            <span>参数设置</span>
          </template>
          <el-menu-item index="/settings/threshold">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>VWAP基差阈值设置</template>
          </el-menu-item>
        </el-sub-menu>
      </el-menu>

      <el-tooltip
        :content="openPauseTitle"
        placement="right"
        :disabled="!isCollapsed"
      >
        <div
          class="open-pause-control"
          :class="{ 'is-paused': openPaused, 'is-loading': openPauseLoading }"
          @click="toggleOpenPause"
        >
          <el-icon><component :is="openPauseIcon" /></el-icon>
          <span v-if="!isCollapsed" class="open-pause-text">{{ openPauseTitle }}</span>
        </div>
      </el-tooltip>
      
      <!-- 折叠/展开按钮 -->
      <div class="collapse-btn" @click="toggleMenu">
        <el-icon><Fold v-if="!isCollapsed" /><Expand v-else /></el-icon>
        <span v-if="!isCollapsed" class="collapse-text">收起菜单</span>
        <span v-else class="collapse-text">展开</span>
      </div>
      
      <!-- 登出按钮 -->
      <div class="logout-section">
        <el-button
          type="danger"
          :icon="SwitchButton"
          @click="handleLogout"
          plain
          size="small"
        >
          <span v-if="!isCollapsed">退出登录</span>
        </el-button>
      </div>
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
  transition: width 0.3s ease;
  position: relative;
  display: flex;
  flex-direction: column;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--app-text);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
  border-bottom: 1px solid var(--app-border);
  overflow: hidden;
  white-space: nowrap;
  padding: 0 12px;
}

.logo-title {
  flex-shrink: 0;
}

.trading-mode-badge {
  font-size: 10px;
  font-weight: 500;
  color: #fff;
  padding: 2px 6px;
  border-radius: 3px;
  line-height: 1.2;
  letter-spacing: 0;
}

.collapse-btn {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  cursor: pointer;
  color: #9aa0a6;
  border-top: 1px solid var(--app-border);
  border-bottom: 1px solid var(--app-border);
  transition: all 0.3s ease;
  user-select: none;
}

.collapse-btn:hover {
  background-color: rgba(33, 150, 243, 0.12);
  color: #2196f3;
}

.collapse-text {
  font-size: 13px;
  white-space: nowrap;
}

.app-menu {
  border-right: none;
  flex: 1;
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

.open-pause-control {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #e6a23c;
  border-top: 1px solid var(--app-border);
  cursor: pointer;
  user-select: none;
  transition: all 0.3s ease;
}

.open-pause-control:hover {
  background-color: rgba(230, 162, 60, 0.12);
  color: #f3b760;
}

.open-pause-control.is-paused {
  color: #67c23a;
}

.open-pause-control.is-paused:hover {
  background-color: rgba(103, 194, 58, 0.12);
  color: #85ce61;
}

.open-pause-control.is-loading {
  pointer-events: none;
  opacity: 0.7;
}

.open-pause-control.is-loading :deep(.el-icon) {
  animation: open-pause-spin 0.9s linear infinite;
}

.open-pause-text {
  font-size: 13px;
  white-space: nowrap;
}

@keyframes open-pause-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.app-main {
  padding: 16px;
  background-color: var(--app-bg);
  overflow: auto;
}

.logout-section {
  padding: 16px;
  border-top: 1px solid var(--app-border);
}

.logout-section .el-button {
  width: 100%;
}
</style>
