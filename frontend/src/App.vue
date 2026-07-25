<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Monitor, List, TrendCharts, DataAnalysis, Setting, SwitchButton, Fold, Expand, Connection, Stopwatch, VideoPause, VideoPlay, Cpu, Bell } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { removeToken } from './utils/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { post, get } from './utils/request'
import {
  addPopupNotification,
  listPopupNotifications,
  markAllPopupNotificationsRead,
  markPopupNotificationRead,
  POPUP_NOTIFICATION_HISTORY_EVENT,
  type PopupNotification,
} from './utils/notificationHistory'

const route = useRoute()
const router = useRouter()
const isCollapsed = ref(false)
const notificationHistory = ref<PopupNotification[]>([])
const notificationUnreadCount = ref(0)
const notificationFilter = ref<'unread' | 'read' | 'all'>('unread')
const notificationLoading = ref(false)
const notificationRefreshing = ref(false)
const notificationLoadingMore = ref(false)
const notificationPagination = ref({ page: 1, page_size: 50, total: 0, total_pages: 0 })
let openPauseStatusTimer: ReturnType<typeof setInterval> | null = null
let listingAlertTimer: ReturnType<typeof setInterval> | null = null
let riskAlertTimer: ReturnType<typeof setInterval> | null = null
const LISTING_ALERT_STORAGE_KEY = 'listing_event_alert'
const LISTING_ALERT_SNOOZE_MS = 6 * 60 * 60 * 1000
const LISTING_ALERT_INTERVAL_MS = 15 * 60 * 1000
const RISK_ALERT_SEEN_STORAGE_KEY = 'exchange_risk_notification_seen'
const RISK_ALERT_INTERVAL_MS = 60 * 1000
const RISK_ALERT_LOOKBACK_HOURS = 24
const RISK_ALERT_MAX_SEEN = 500
const NOTIFICATION_PAGE_SIZE = 50

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

const notificationCount = computed(() => notificationUnreadCount.value)
const notificationHasMore = computed(() => notificationPagination.value.page < notificationPagination.value.total_pages)

async function refreshNotificationHistory(syncRecent = true, showLoading = false) {
  if (isLoginPage.value || notificationRefreshing.value) return
  notificationRefreshing.value = true
  const useLoadingState = showLoading && notificationHistory.value.length === 0
  if (useLoadingState) notificationLoading.value = true
  try {
    const data = await listPopupNotifications({
      readStatus: notificationFilter.value,
      page: 1,
      pageSize: NOTIFICATION_PAGE_SIZE,
      syncRecent,
    })
    notificationHistory.value = data.items
    notificationUnreadCount.value = data.unread_count
    notificationPagination.value = data.pagination
  } catch {
    // request.ts 会提示错误；这里避免影响主界面。
  } finally {
    if (useLoadingState) notificationLoading.value = false
    notificationRefreshing.value = false
  }
}

async function loadMoreNotifications() {
  if (isLoginPage.value || notificationLoading.value || notificationLoadingMore.value || !notificationHasMore.value) return
  notificationLoadingMore.value = true
  try {
    const data = await listPopupNotifications({
      readStatus: notificationFilter.value,
      page: notificationPagination.value.page + 1,
      pageSize: NOTIFICATION_PAGE_SIZE,
      syncRecent: false,
    })
    const existing = new Set(notificationHistory.value.map((item) => item.id))
    notificationHistory.value = [
      ...notificationHistory.value,
      ...data.items.filter((item) => !existing.has(item.id)),
    ]
    notificationUnreadCount.value = data.unread_count
    notificationPagination.value = data.pagination
  } catch {
    // request.ts 会提示错误
  } finally {
    notificationLoadingMore.value = false
  }
}

async function markAllNotificationsRead() {
  try {
    const data = await markAllPopupNotificationsRead()
    notificationUnreadCount.value = Number(data?.unread_count ?? 0)
    if (notificationFilter.value === 'unread') {
      notificationHistory.value = []
      notificationPagination.value = { ...notificationPagination.value, total: 0, total_pages: 0 }
      return
    }
    const now = new Date().toISOString()
    notificationHistory.value = notificationHistory.value.map((item) => ({ ...item, read_at: item.read_at || now }))
  } catch {
    // request.ts 会提示错误
  }
}

async function markNotificationRead(id: number) {
  try {
    const data = await markPopupNotificationRead(id)
    notificationUnreadCount.value = Number(data?.unread_count ?? Math.max(notificationUnreadCount.value - 1, 0))
    if (notificationFilter.value === 'unread') {
      notificationHistory.value = notificationHistory.value.filter((item) => item.id !== id)
      notificationPagination.value = {
        ...notificationPagination.value,
        total: Math.max(notificationPagination.value.total - 1, 0),
      }
      return
    }
    const now = new Date().toISOString()
    notificationHistory.value = notificationHistory.value.map((item) => (
      item.id === id ? { ...item, read_at: item.read_at || now } : item
    ))
  } catch {
    // request.ts 会提示错误
  }
}

function handleNotificationFilterChange() {
  void refreshNotificationHistory(false, true)
}

function handleNotificationPopoverShow() {
  void refreshNotificationHistory(true, false)
}

function notificationTypeClass(type: PopupNotification['type']) {
  return `notification-${type || 'info'}`
}

function formatNotificationTime(value: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

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
    const title = `交易对上新候选 ${items.length} 个`
    const eventAt = items
      .map((item: any) => item.last_seen_at)
      .filter(Boolean)
      .sort()
      .pop()
    await addPopupNotification({
      title,
      message: preview,
      type: 'warning',
      source: 'listing_events',
      dedup_key: `listing_events:${fingerprint}`,
      event_at: eventAt,
      payload: { items },
    })
    await refreshNotificationHistory(false)
    ElMessageBox.confirm(preview, title, {
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

function loadSeenRiskNotificationKeys() {
  try {
    const rows = JSON.parse(localStorage.getItem(RISK_ALERT_SEEN_STORAGE_KEY) || '[]')
    return new Set(Array.isArray(rows) ? rows.map((item) => String(item)) : [])
  } catch {
    return new Set<string>()
  }
}

function saveSeenRiskNotificationKeys(keys: Set<string>) {
  localStorage.setItem(
    RISK_ALERT_SEEN_STORAGE_KEY,
    JSON.stringify(Array.from(keys).slice(-RISK_ALERT_MAX_SEEN)),
  )
}

function buildRiskNotificationMessage(item: any) {
  const lines = [String(item.message || '')]
  if (item.event_at) lines.push(`时间: ${item.event_at}`)
  if (item.detail?.contract) lines.push(`合约: ${item.detail.contract}`)
  return lines.filter(Boolean).join('\n')
}

async function maybeShowRiskAlerts() {
  if (isLoginPage.value) return
  try {
    const res = await get(`/api/trading/risk-notifications/recent?hours=${RISK_ALERT_LOOKBACK_HOURS}&limit=50`)
    if (!res.ok) return
    const data = await res.json()
    const items = Array.isArray(data.items) ? data.items : []
    if (!items.length) return

    const seen = loadSeenRiskNotificationKeys()
    const unseen = items
      .slice()
      .reverse()
      .filter((item: any) => item?.dedup_key && !seen.has(String(item.dedup_key)))

    if (!unseen.length) return

    for (const item of unseen) {
      const key = String(item.dedup_key)
      await addPopupNotification({
        title: String(item.title || '交易风险通知'),
        message: buildRiskNotificationMessage(item),
        type: item.severity === 'error' ? 'error' : 'warning',
        source: String(item.source || 'risk'),
        dedup_key: key,
        event_at: item.event_at,
        payload: item,
      })
      seen.add(key)
    }
    saveSeenRiskNotificationKeys(seen)
    await refreshNotificationHistory(false)
    ElMessage.warning(`新增 ${unseen.length} 条交易风险通知`)
  } catch {
    // 风险提醒失败不影响主界面
  }
}

function startRiskAlertTimer() {
  setTimeout(maybeShowRiskAlerts, 5000)
  if (!riskAlertTimer) {
    riskAlertTimer = setInterval(maybeShowRiskAlerts, RISK_ALERT_INTERVAL_MS)
  }
}

function stopRiskAlertTimer() {
  if (riskAlertTimer) {
    clearInterval(riskAlertTimer)
    riskAlertTimer = null
  }
}

const notificationHistoryChangeHandler = () => void refreshNotificationHistory(false)

onMounted(() => {
  void refreshNotificationHistory(true, true)
  window.addEventListener(POPUP_NOTIFICATION_HISTORY_EVENT, notificationHistoryChangeHandler)
  if (!isLoginPage.value) {
    startOpenPauseStatusTimer()
    startListingAlertTimer()
    startRiskAlertTimer()
  }
})

watch(isLoginPage, (loginPage) => {
  if (loginPage) {
    stopOpenPauseStatusTimer()
    stopListingAlertTimer()
    stopRiskAlertTimer()
  } else {
    startOpenPauseStatusTimer()
    startListingAlertTimer()
    startRiskAlertTimer()
  }
})

onUnmounted(() => {
  stopOpenPauseStatusTimer()
  stopListingAlertTimer()
  stopRiskAlertTimer()
  window.removeEventListener(POPUP_NOTIFICATION_HISTORY_EVENT, notificationHistoryChangeHandler)
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
        <el-popover
          placement="right-start"
          :width="380"
          trigger="click"
          popper-class="notification-history-popper"
          @show="handleNotificationPopoverShow"
        >
          <template #reference>
            <button class="notification-bell" title="弹窗消息">
              <el-badge
                :value="notificationCount"
                :hidden="notificationCount === 0"
                :max="99"
                type="danger"
              >
                <el-icon><Bell /></el-icon>
              </el-badge>
            </button>
          </template>
          <div class="notification-history">
            <div class="notification-history-header">
              <span>弹窗消息 <b v-if="notificationUnreadCount > 0">{{ notificationUnreadCount }}</b></span>
              <button
                class="notification-clear"
                :disabled="notificationUnreadCount === 0"
                @click="markAllNotificationsRead"
              >
                全部已读
              </button>
            </div>
            <el-radio-group
              v-model="notificationFilter"
              size="small"
              class="notification-filter"
              @change="handleNotificationFilterChange"
            >
              <el-radio-button value="unread">未读</el-radio-button>
              <el-radio-button value="read">已读</el-radio-button>
              <el-radio-button value="all">全部</el-radio-button>
            </el-radio-group>
            <div v-if="notificationLoading" class="notification-empty">
              加载中...
            </div>
            <div v-else-if="notificationHistory.length === 0" class="notification-empty">
              暂无弹窗消息
            </div>
            <div v-else class="notification-list">
              <div
                v-for="item in notificationHistory"
                :key="item.id"
                class="notification-item"
                :class="[notificationTypeClass(item.type), { unread: !item.read_at }]"
              >
                <div class="notification-item-top">
                  <span class="notification-title">
                    <i v-if="!item.read_at" class="notification-unread-dot"></i>
                    {{ item.title }}
                  </span>
                  <span class="notification-time">{{ formatNotificationTime(item.event_at || item.created_at) }}</span>
                </div>
                <div class="notification-message">{{ item.message }}</div>
                <div v-if="!item.read_at" class="notification-item-actions">
                  <button class="notification-read-btn" @click="markNotificationRead(item.id)">标记已读</button>
                </div>
              </div>
            </div>
            <div v-if="notificationHistory.length > 0" class="notification-pagination">
              <span>{{ notificationHistory.length }} / {{ notificationPagination.total }}</span>
              <button
                v-if="notificationHasMore"
                class="notification-load-more"
                :disabled="notificationLoadingMore"
                @click="loadMoreNotifications"
              >
                {{ notificationLoadingMore ? '加载中...' : '加载更多' }}
              </button>
            </div>
          </div>
        </el-popover>
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
          <el-menu-item index="/reverse-arbitrage/funding-prediction">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>Funding预测</template>
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
  min-height: 0;
  overflow: hidden;
}

.logo {
  height: 60px;
  flex: 0 0 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--app-text);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
  border-bottom: 1px solid var(--app-border);
  overflow: hidden;
  white-space: nowrap;
  padding: 0 8px;
}

.logo-title {
  flex-shrink: 0;
}

.notification-bell {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: #9aa0a6;
  cursor: pointer;
}

.notification-bell:hover {
  background: rgba(33, 150, 243, 0.12);
  color: #2196f3;
}

.notification-bell :deep(.el-badge__content) {
  transform: translateY(-45%) translateX(60%);
}

.notification-history {
  max-height: 420px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.notification-history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text, #e5e7eb);
}

.notification-history-header b {
  margin-left: 4px;
  color: #f56c6c;
  font-size: 12px;
}

.notification-clear {
  border: 0;
  background: transparent;
  color: #409eff;
  cursor: pointer;
  font-size: 12px;
}

.notification-clear:disabled {
  color: #606266;
  cursor: not-allowed;
}

.notification-filter {
  width: 100%;
}

.notification-filter :deep(.el-radio-button) {
  flex: 1;
}

.notification-filter :deep(.el-radio-button__inner) {
  width: 100%;
}

.notification-empty {
  color: #909399;
  font-size: 13px;
  padding: 16px 0;
  text-align: center;
}

.notification-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 360px;
  overflow-y: auto;
}

.notification-item {
  --notification-color: #409eff;
  padding: 8px 10px;
  border: 1px solid var(--app-border, #2d2d3d);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}

.notification-item.notification-success {
  --notification-color: #67c23a;
}

.notification-item.notification-warning {
  --notification-color: #e6a23c;
}

.notification-item.notification-error {
  --notification-color: #f56c6c;
}

.notification-item.unread {
  border-color: color-mix(in srgb, var(--notification-color) 48%, transparent);
  background: color-mix(in srgb, var(--notification-color) 9%, transparent);
}

.notification-item.unread .notification-title {
  color: var(--notification-color);
}

.notification-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}

.notification-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--app-text, #e5e7eb);
  font-size: 13px;
  font-weight: 600;
  min-width: 0;
}

.notification-time {
  color: #909399;
  flex-shrink: 0;
  font-size: 12px;
}

.notification-unread-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
  background: var(--notification-color);
}

.notification-message {
  color: #c0c4cc;
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.notification-item-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}

.notification-read-btn {
  border: 0;
  border-radius: 4px;
  background: rgba(64, 158, 255, 0.14);
  color: #409eff;
  cursor: pointer;
  font-size: 12px;
  padding: 3px 8px;
}

.notification-read-btn:hover {
  background: rgba(64, 158, 255, 0.24);
}

.notification-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #909399;
  font-size: 12px;
  padding-top: 2px;
}

.notification-load-more {
  border: 1px solid rgba(64, 158, 255, 0.36);
  border-radius: 4px;
  background: rgba(64, 158, 255, 0.1);
  color: #409eff;
  cursor: pointer;
  font-size: 12px;
  padding: 4px 10px;
}

.notification-load-more:disabled {
  border-color: rgba(144, 147, 153, 0.24);
  background: rgba(144, 147, 153, 0.08);
  color: #909399;
  cursor: wait;
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
  flex: 0 0 48px;
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
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: rgba(154, 160, 166, 0.35) transparent;
}

.app-menu::-webkit-scrollbar {
  width: 6px;
}

.app-menu::-webkit-scrollbar-thumb {
  background: rgba(154, 160, 166, 0.35);
  border-radius: 999px;
}

.app-menu::-webkit-scrollbar-track {
  background: transparent;
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
  flex-shrink: 0;
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
  flex-shrink: 0;
  padding: 16px;
  border-top: 1px solid var(--app-border);
}

.logout-section .el-button {
  width: 100%;
}
</style>
