<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Monitor, List, TrendCharts, DataAnalysis, Setting, SwitchButton, Fold, Expand, Connection, Stopwatch, VideoPause, VideoPlay, Cpu, Bell } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { removeToken } from './utils/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { post, get } from './utils/request'

const route = useRoute()
const router = useRouter()
const isCollapsed = ref(false)
let openPauseStatusTimer: ReturnType<typeof setInterval> | null = null
let listingAlertTimer: ReturnType<typeof setInterval> | null = null
const LISTING_ALERT_STORAGE_KEY = 'listing_event_alert'
const LISTING_ALERT_SNOOZE_MS = 6 * 60 * 60 * 1000
const LISTING_ALERT_INTERVAL_MS = 15 * 60 * 1000

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
const forwardOpenPaused = ref(true)
const reverseOpenPaused = ref(true)
const forwardOpenPauseLoading = ref(false)
const reverseOpenPauseLoading = ref(false)

const forwardOpenPauseTitle = computed(() => (forwardOpenPaused.value ? '恢复正向开仓' : '暂停正向开仓'))
const reverseOpenPauseTitle = computed(() => (reverseOpenPaused.value ? '恢复反向开仓' : '暂停反向开仓'))
const forwardOpenPauseIcon = computed(() => (forwardOpenPaused.value ? VideoPlay : VideoPause))
const reverseOpenPauseIcon = computed(() => (reverseOpenPaused.value ? VideoPlay : VideoPause))

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
    forwardOpenPaused.value = !!data.open_paused
    reverseOpenPaused.value = !!data.reverse_open_paused
  } catch {
    // 服务未启动时静默失败
  }
}

async function toggleForwardOpenPause() {
  forwardOpenPauseLoading.value = true
  try {
    const url = forwardOpenPaused.value ? '/api/trading/open/resume' : '/api/trading/open/pause'
    const res = await post(url)
    const data = await res.json()
    if (data.ok) {
      forwardOpenPaused.value = data.open_paused
      ElMessage.success(data.open_paused ? '正向开仓已暂停，平仓不受影响' : '正向开仓已恢复')
    }
  } catch (e: any) {
    ElMessage.error(`操作失败: ${e.message || '未知错误'}`)
  } finally {
    forwardOpenPauseLoading.value = false
  }
}

async function toggleReverseOpenPause() {
  reverseOpenPauseLoading.value = true
  try {
    const url = reverseOpenPaused.value ? '/api/trading/reverse-open/resume' : '/api/trading/reverse-open/pause'
    const res = await post(url)
    const data = await res.json()
    if (data.ok) {
      reverseOpenPaused.value = data.reverse_open_paused
      ElMessage.success(data.reverse_open_paused ? '反向开仓已暂停，正向不受影响' : '反向开仓已恢复')
    }
  } catch (e: any) {
    ElMessage.error(`操作失败: ${e.message || '未知错误'}`)
  } finally {
    reverseOpenPauseLoading.value = false
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

async function maybeShowListingAlert() {
  if (isLoginPage.value) return
  try {
    const res = await get('/api/trading/listing-events/summary')
    if (!res.ok) return
    const data = await res.json()
    const items = Array.isArray(data.items) ? data.items : []
    if (!items.length) return

    const fingerprint = items
      .map((item: any) => `${item.base_asset}:${item.gate_contract || ''}:${item.binance_symbol || ''}:${item.last_seen_at || ''}`)
      .sort()
      .join('|')
    const previous = JSON.parse(localStorage.getItem(LISTING_ALERT_STORAGE_KEY) || '{}')
    const now = Date.now()
    if (previous.fingerprint === fingerprint && now - Number(previous.at || 0) < LISTING_ALERT_SNOOZE_MS) return

    localStorage.setItem(LISTING_ALERT_STORAGE_KEY, JSON.stringify({ fingerprint, at: now }))
    const preview = items.slice(0, 8).map((item: any) => {
      const gateVol = Number(item.gate_volume_24h_settle || 0)
      const spotVol = Number(item.binance_quote_volume || 0)
      return `${item.base_asset} Gate:${item.gate_contract || '-'} Binance:${item.binance_symbol || '-'} 24h=${gateVol.toFixed(0)}/${spotVol.toFixed(0)}`
    }).join('\n')
    ElMessageBox.confirm(preview, `交易对上新候选 ${items.length} 个`, {
      type: 'warning',
      confirmButtonText: '去处理',
      cancelButtonText: '稍后',
    }).then(() => {
      router.push('/settings/listings')
    }).catch(() => {})
  } catch {
    // 上新提醒不影响主界面
  }
}

function startListingAlertTimer() {
  setTimeout(maybeShowListingAlert, 3000)
  if (!listingAlertTimer) {
    listingAlertTimer = setInterval(maybeShowListingAlert, LISTING_ALERT_INTERVAL_MS)
  }
}

function stopListingAlertTimer() {
  if (listingAlertTimer) {
    clearInterval(listingAlertTimer)
    listingAlertTimer = null
  }
}

onMounted(() => {
  if (!isLoginPage.value) {
    startOpenPauseStatusTimer()
    startListingAlertTimer()
  }
})

watch(isLoginPage, (loginPage) => {
  if (loginPage) {
    stopOpenPauseStatusTimer()
    stopListingAlertTimer()
  } else {
    startOpenPauseStatusTimer()
    startListingAlertTimer()
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
        <el-sub-menu index="forward-arbitrage">
          <template #title>
            <el-icon><Monitor /></el-icon>
            <span>正向套利</span>
          </template>
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
        </el-sub-menu>

        <el-sub-menu index="reverse-arbitrage-group">
          <template #title>
            <el-icon><TrendCharts /></el-icon>
            <span>反向套利</span>
          </template>
          <el-menu-item index="/reverse-arbitrage/orderbook">
            <el-icon><Monitor /></el-icon>
            <template #title>订单簿监控</template>
          </el-menu-item>
          <el-menu-item index="/reverse-arbitrage/signals">
            <el-icon><Stopwatch /></el-icon>
            <template #title>交易信号</template>
          </el-menu-item>
          <el-menu-item index="/reverse-arbitrage/research">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>研究分析</template>
          </el-menu-item>
          <el-menu-item index="/reverse-arbitrage/orders">
            <el-icon><List /></el-icon>
            <template #title>订单管理</template>
          </el-menu-item>
          <el-menu-item index="/reverse-arbitrage/positions">
            <el-icon><TrendCharts /></el-icon>
            <template #title>持仓监控</template>
          </el-menu-item>
          <el-menu-item index="/reverse-arbitrage/capital">
            <el-icon><TrendCharts /></el-icon>
            <template #title>资金监控</template>
          </el-menu-item>
          <el-menu-item index="/reverse-arbitrage/reconciliation">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>持仓对账</template>
          </el-menu-item>
        </el-sub-menu>

        <el-sub-menu index="settings">
          <template #title>
            <el-icon><Setting /></el-icon>
            <span>设置</span>
          </template>
          <el-menu-item index="/settings/threshold">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>VWAP基差阈值设置</template>
          </el-menu-item>
          <el-menu-item index="/settings/server-status">
            <el-icon><Cpu /></el-icon>
            <template #title>服务器状态</template>
          </el-menu-item>
          <el-menu-item index="/settings/listings">
            <el-icon><Bell /></el-icon>
            <template #title>交易对上新</template>
          </el-menu-item>
          <el-menu-item index="/connections">
            <el-icon><Connection /></el-icon>
            <template #title>连接状态</template>
          </el-menu-item>
        </el-sub-menu>
      </el-menu>

      <div class="open-pause-controls">
        <el-tooltip
          :content="forwardOpenPauseTitle"
          placement="right"
          :disabled="!isCollapsed"
        >
          <div
            class="open-pause-control"
            :class="{ 'is-paused': forwardOpenPaused, 'is-loading': forwardOpenPauseLoading }"
            @click="toggleForwardOpenPause"
          >
            <el-icon><component :is="forwardOpenPauseIcon" /></el-icon>
            <span v-if="!isCollapsed" class="open-pause-text">{{ forwardOpenPauseTitle }}</span>
          </div>
        </el-tooltip>

        <el-tooltip
          :content="reverseOpenPauseTitle"
          placement="right"
          :disabled="!isCollapsed"
        >
          <div
            class="open-pause-control"
            :class="{ 'is-paused': reverseOpenPaused, 'is-loading': reverseOpenPauseLoading }"
            @click="toggleReverseOpenPause"
          >
            <el-icon><component :is="reverseOpenPauseIcon" /></el-icon>
            <span v-if="!isCollapsed" class="open-pause-text">{{ reverseOpenPauseTitle }}</span>
          </div>
        </el-tooltip>
      </div>
      
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

.open-pause-controls {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 12px;
  border-top: 1px solid var(--app-border);
}

.open-pause-control {
  min-height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #e6a23c;
  border: 1px solid rgba(230, 162, 60, 0.35);
  border-radius: 4px;
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
  border-color: rgba(103, 194, 58, 0.35);
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
  font-size: 12px;
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
