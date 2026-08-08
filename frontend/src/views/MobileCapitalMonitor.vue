<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ECharts, EChartsOption } from 'echarts'
import { get } from '../utils/request'
import {
  getPopupNotificationUnreadCount,
  listPopupNotifications,
  markAllPopupNotificationsRead,
  markPopupNotificationRead,
  type PopupNotification,
} from '../utils/notificationHistory'

type Exchange = 'binance' | 'gate' | 'total'
type PeriodDays = 1 | 3 | 7 | 30 | 90

interface CapitalRow {
  snapshot_at: string
  exchange: Exchange
  equity_usdt: number | null
  available_usdt: number | null
  bnb_available?: number | null
  bnb_available_usdt?: number | null
  gate_cross_mmr_pct?: number | null
  gate_cross_risk_status?: string | null
  daily_realized_pnl_usdt?: number | null
  daily_return_pct?: number | null
}

interface GateCrossRisk {
  status?: string | null
  status_label?: string | null
  account_mmr_pct?: number | null
  error?: string | null
  fetched_at?: string | null
}

interface AnnualizedReturn {
  period_days: number
  realized_sufficient_data?: boolean | null
  realized_data_available?: boolean | null
  realized_annualized_return_pct?: number | null
  realized_available_days?: number | null
}

const latestRows = ref<CapitalRow[]>([])
const equityRows = ref<CapitalRow[]>([])
const dailyRows = ref<CapitalRow[]>([])
const gateRisk = ref<GateCrossRisk | null>(null)
const annualized = ref<AnnualizedReturn | null>(null)
const selectedDays = ref<PeriodDays>(7)
const loading = ref(true)
const refreshing = ref(false)
const errorMessage = ref('')
const lastUpdatedAt = ref<Date | null>(null)
const notificationOpen = ref(false)
const notificationItems = ref<PopupNotification[]>([])
const notificationUnreadCount = ref(0)
const notificationLoading = ref(false)
const notificationRefreshing = ref(false)
const notificationError = ref('')
const equityChartRef = ref<HTMLDivElement | null>(null)
const dailyChartRef = ref<HTMLDivElement | null>(null)
let equityChart: ECharts | null = null
let dailyChart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let summaryTimer: ReturnType<typeof setInterval> | null = null
let riskTimer: ReturnType<typeof setInterval> | null = null
let chartTimer: ReturnType<typeof setInterval> | null = null
let notificationTimer: ReturnType<typeof setInterval> | null = null
let requestVersion = 0
let previousBodyOverflow = ''

const periodOptions: Array<{ value: PeriodDays; label: string }> = [
  { value: 1, label: '1天' },
  { value: 3, label: '3天' },
  { value: 7, label: '7天' },
  { value: 30, label: '30天' },
  { value: 90, label: '90天' },
]

const latestByExchange = computed(() => {
  const result: Partial<Record<Exchange, CapitalRow>> = {}
  for (const row of latestRows.value) result[row.exchange] = row
  return result
})

const displayedMmr = computed(() => (
  gateRisk.value?.account_mmr_pct ?? latestByExchange.value.gate?.gate_cross_mmr_pct ?? null
))

const displayedRiskStatus = computed(() => (
  gateRisk.value?.status ?? latestByExchange.value.gate?.gate_cross_risk_status ?? 'unknown'
))

const displayedRiskLabel = computed(() => {
  if (gateRisk.value?.status_label) return gateRisk.value.status_label
  return ({ safe: '安全', warning: '预警', danger: '危险', idle: '无持仓', unknown: '未知' } as Record<string, string>)[displayedRiskStatus.value]
    || '未知'
})

const snapshotTime = computed(() => {
  const snapshot = latestRows.value[0]?.snapshot_at
  if (!snapshot) return '等待数据'
  const date = parseDate(snapshot)
  if (!date) return snapshot
  return `快照 ${formatTime(date)}`
})

const refreshTime = computed(() => (
  lastUpdatedAt.value ? `更新 ${formatTime(lastUpdatedAt.value)}` : snapshotTime.value
))

const annualizedValue = computed(() => {
  if (!annualized.value?.realized_data_available) return null
  return annualized.value.realized_annualized_return_pct ?? null
})

const annualizedHint = computed(() => {
  if (!annualized.value?.realized_data_available) return '暂无已实现收益数据'
  if (!annualized.value.realized_sufficient_data) {
    return `${annualized.value.realized_available_days || 0}/${annualized.value.period_days} 天有效数据`
  }
  return `近 ${annualized.value.period_days} 天`
})

const todayRealizedValue = computed(() => {
  const today = localDateKey(new Date())
  const row = dailyRows.value
    .filter((item) => item.exchange === 'total' && localDateKey(parseDate(item.snapshot_at)) === today)
    .at(-1)
  return row?.daily_realized_pnl_usdt ?? null
})

function isFiniteNumber(value: unknown): boolean {
  return value !== null && value !== undefined && Number.isFinite(Number(value))
}

function formatAmount(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return '--'
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function formatBnb(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return '--'
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 6 })
}

function formatPercent(value: number | null | undefined, digits = 2): string {
  if (!isFiniteNumber(value)) return '--'
  return `${Number(value).toFixed(digits)}%`
}

function signedAmount(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return '--'
  const amount = Number(value)
  return `${amount > 0 ? '+' : ''}${formatAmount(amount)}`
}

function parseDate(value: string): Date | null {
  const date = new Date(value.includes('T') ? value : value.replace(' ', 'T'))
  return Number.isNaN(date.getTime()) ? null : date
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function localDateKey(date: Date | null): string {
  if (!date) return ''
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-')
}

function axisTime(value: string): string {
  const date = parseDate(value)
  if (!date) return value
  if (selectedDays.value <= 7) {
    return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:00`
  }
  return `${date.getMonth() + 1}/${date.getDate()}`
}

function dayLabel(value: string): string {
  const date = parseDate(value)
  return date ? `${date.getMonth() + 1}/${date.getDate()}` : value.slice(5, 10)
}

function valueClass(value: number | null | undefined): string {
  if (!isFiniteNumber(value) || Number(value) === 0) return ''
  return Number(value) > 0 ? 'positive' : 'negative'
}

function mmrClass(status: string): string {
  if (status === 'danger') return 'danger'
  if (status === 'warning') return 'warning'
  if (status === 'safe') return 'safe'
  return 'muted'
}

function notificationTypeClass(type: PopupNotification['type']): string {
  return `notification-${type || 'info'}`
}

function formatNotificationTime(value: string | null | undefined): string {
  if (!value) return ''
  const date = parseDate(value)
  if (!date) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

async function fetchMobileNotifications(syncRecent = false, showLoading = false) {
  if (notificationRefreshing.value) return
  notificationRefreshing.value = true
  if (showLoading && notificationItems.value.length === 0) notificationLoading.value = true
  try {
    const data = await listPopupNotifications({
      readStatus: 'all',
      page: 1,
      pageSize: 50,
      syncRecent,
    })
    notificationItems.value = data.items
    notificationUnreadCount.value = data.unread_count
    notificationError.value = ''
  } catch (error: any) {
    notificationError.value = error?.message || '消息加载失败'
  } finally {
    notificationLoading.value = false
    notificationRefreshing.value = false
  }
}

async function syncNotificationUnreadCount() {
  try {
    const count = await getPopupNotificationUnreadCount()
    if (count !== notificationUnreadCount.value) {
      notificationUnreadCount.value = count
      if (notificationOpen.value) await fetchMobileNotifications(false)
    }
  } catch {
    // 未读数轮询失败不影响资金监控。
  }
}

async function openNotifications() {
  notificationOpen.value = true
  await fetchMobileNotifications(true, true)
}

async function markMobileNotificationRead(item: PopupNotification) {
  if (item.read_at) return
  try {
    const data = await markPopupNotificationRead(item.id)
    item.read_at = new Date().toISOString()
    notificationUnreadCount.value = Number(data?.unread_count ?? Math.max(notificationUnreadCount.value - 1, 0))
  } catch {
    // request.ts 已显示失败原因。
  }
}

async function markAllMobileNotificationsRead() {
  if (notificationUnreadCount.value === 0) return
  try {
    const data = await markAllPopupNotificationsRead()
    const now = new Date().toISOString()
    notificationItems.value = notificationItems.value.map((item) => ({ ...item, read_at: item.read_at || now }))
    notificationUnreadCount.value = Number(data?.unread_count ?? 0)
  } catch {
    // request.ts 已显示失败原因。
  }
}

async function readJson(url: string): Promise<any> {
  const response = await get(url, { silent: true })
  const data = await response.json()
  if (!response.ok) throw new Error(data?.detail || `HTTP ${response.status}`)
  return data
}

async function fetchSummary(silent = false) {
  try {
    const [latest, liveRisk, annualizedData] = await Promise.all([
      readJson('/api/trading/capital/latest'),
      readJson('/api/trading/capital/gate-cross-risk/live'),
      readJson('/api/trading/capital/annualized-return?days=7'),
    ])
    latestRows.value = latest.rows || []
    gateRisk.value = liveRisk.risk || null
    annualized.value = annualizedData
    lastUpdatedAt.value = new Date()
    if (!silent) errorMessage.value = ''
  } catch (error: any) {
    if (!silent) errorMessage.value = error?.message || '资金数据加载失败'
  }
}

async function fetchRisk() {
  try {
    const data = await readJson('/api/trading/capital/gate-cross-risk/live')
    gateRisk.value = data.risk || null
  } catch {
    // 保留最后一次有效 MMR；完整刷新时会展示错误。
  }
}

async function fetchPeriodData() {
  const days = selectedDays.value
  const version = ++requestVersion
  try {
    const [equityData, dailyData] = await Promise.all([
      readJson(`/api/trading/capital/history?days=${days}&exchange=total&metric=equity_usdt`),
      readJson(`/api/trading/capital/history?days=${days}&exchange=total&metric=daily_return`),
    ])
    if (version !== requestVersion) return
    equityRows.value = equityData.rows || []
    dailyRows.value = dailyData.rows || []
    errorMessage.value = ''
  } catch (error: any) {
    if (version !== requestVersion) return
    errorMessage.value = error?.message || '收益数据加载失败'
  }
}

async function refreshAll() {
  refreshing.value = true
  await Promise.all([fetchSummary(), fetchPeriodData()])
  refreshing.value = false
  loading.value = false
}

function baseChartOption(): EChartsOption {
  return {
    animationDuration: 280,
    backgroundColor: 'transparent',
    textStyle: { fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif', color: '#8b98aa' },
    grid: { top: 18, right: 10, bottom: 34, left: 54 },
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: 'rgba(16, 22, 31, .96)',
      borderColor: '#2b3748',
      textStyle: { color: '#f4f7fb', fontSize: 12 },
    },
    dataZoom: [{ type: 'inside', zoomOnMouseWheel: false, moveOnMouseMove: true, moveOnMouseWheel: false }],
    xAxis: {
      type: 'category',
      boundaryGap: false,
      axisLine: { lineStyle: { color: '#2b3748' } },
      axisTick: { show: false },
      axisLabel: { color: '#728095', fontSize: 10, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitNumber: 4,
      axisLabel: { color: '#728095', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(112, 128, 149, .14)' } },
    },
  }
}

function updateEquityChart() {
  if (!equityChart) return
  const rows = equityRows.value.filter((row) => row.exchange === 'total' && isFiniteNumber(row.equity_usdt))
  const option = baseChartOption()
  option.xAxis = {
    ...(option.xAxis as object),
    data: rows.map((row) => axisTime(row.snapshot_at)),
  }
  option.yAxis = {
    ...(option.yAxis as object),
    axisLabel: { color: '#728095', fontSize: 10, formatter: (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) },
  }
  option.series = [{
    name: '总资产',
    type: 'line',
    smooth: true,
    showSymbol: false,
    lineStyle: { color: '#4da3ff', width: 2.5 },
    areaStyle: { color: 'rgba(77, 163, 255, .16)' },
    data: rows.map((row) => Number(row.equity_usdt)),
  }]
  equityChart.setOption(option, true)
}

function updateDailyChart() {
  if (!dailyChart) return
  const rows = dailyRows.value
    .filter((row) => row.exchange === 'total')
    .slice(-selectedDays.value)
  const option = baseChartOption()
  option.grid = { top: 28, right: 48, bottom: 34, left: 48 }
  option.legend = { top: 0, right: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: '#8b98aa', fontSize: 10 } }
  option.xAxis = {
    ...(option.xAxis as object),
    boundaryGap: true,
    data: rows.map((row) => dayLabel(row.snapshot_at)),
  }
  option.yAxis = [
    {
      type: 'value',
      scale: true,
      splitNumber: 4,
      axisLabel: { color: '#728095', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(112, 128, 149, .14)' } },
    },
    {
      type: 'value',
      scale: true,
      splitNumber: 4,
      axisLabel: { color: '#728095', fontSize: 10, formatter: '{value}%' },
      splitLine: { show: false },
    },
  ]
  option.series = [
    {
      name: '收益 USDT',
      type: 'bar',
      barMaxWidth: 18,
      itemStyle: {
        color: (params: any) => Number(params.value) >= 0 ? '#20c997' : '#ff6b6b',
        borderRadius: [3, 3, 0, 0],
      },
      data: rows.map((row) => Number(row.daily_realized_pnl_usdt || 0)),
    },
    {
      name: '收益率',
      type: 'line',
      yAxisIndex: 1,
      smooth: true,
      showSymbol: rows.length <= 14,
      symbolSize: 5,
      lineStyle: { color: '#f5b942', width: 2 },
      itemStyle: { color: '#f5b942' },
      data: rows.map((row) => isFiniteNumber(row.daily_return_pct) ? Number(row.daily_return_pct) : null),
    },
  ]
  dailyChart.setOption(option, true)
}

async function initCharts() {
  await nextTick()
  if (!equityChartRef.value || !dailyChartRef.value) return
  const { init } = await import('../utils/capitalChart')
  equityChart = init(equityChartRef.value)
  dailyChart = init(dailyChartRef.value)
  resizeObserver = new ResizeObserver(() => {
    equityChart?.resize()
    dailyChart?.resize()
  })
  resizeObserver.observe(equityChartRef.value)
  resizeObserver.observe(dailyChartRef.value)
  updateEquityChart()
  updateDailyChart()
}

function handleVisibilityChange() {
  if (document.visibilityState !== 'visible') return
  void fetchSummary(true)
  void syncNotificationUnreadCount()
}

watch(selectedDays, () => void fetchPeriodData())
watch(equityRows, updateEquityChart)
watch(dailyRows, updateDailyChart)
watch(notificationOpen, (open) => {
  if (open) {
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  } else {
    document.body.style.overflow = previousBodyOverflow
  }
})

onMounted(async () => {
  await Promise.all([refreshAll(), initCharts(), fetchMobileNotifications(true)])
  summaryTimer = setInterval(() => void fetchSummary(true), 30_000)
  riskTimer = setInterval(() => void fetchRisk(), 5_000)
  chartTimer = setInterval(() => void fetchPeriodData(), 60_000)
  notificationTimer = setInterval(() => void syncNotificationUnreadCount(), 10_000)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onBeforeUnmount(() => {
  if (summaryTimer) clearInterval(summaryTimer)
  if (riskTimer) clearInterval(riskTimer)
  if (chartTimer) clearInterval(chartTimer)
  if (notificationTimer) clearInterval(notificationTimer)
  if (notificationOpen.value) document.body.style.overflow = previousBodyOverflow
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  resizeObserver?.disconnect()
  equityChart?.dispose()
  dailyChart?.dispose()
})
</script>

<template>
  <main class="mobile-capital-page">
    <header class="mobile-header">
      <div>
        <h1>资金监控</h1>
        <p>{{ refreshTime }}</p>
      </div>
      <div class="header-actions">
        <button class="bell-button" aria-label="查看推送消息" @click="openNotifications">
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" />
          </svg>
          <span v-if="notificationUnreadCount > 0" class="bell-badge">
            {{ notificationUnreadCount > 99 ? '99+' : notificationUnreadCount }}
          </span>
        </button>
        <button class="refresh-button" :class="{ spinning: refreshing }" :disabled="refreshing" aria-label="刷新资金数据" @click="refreshAll">
          <span aria-hidden="true">↻</span>
        </button>
      </div>
    </header>

    <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>

    <section class="total-card" :aria-busy="loading">
      <div class="total-card-title">
        <span class="exchange-dot total"></span>
        <h2>总计</h2>
      </div>
      <div class="total-primary-metric">
        <span>总资产</span>
        <strong>{{ formatAmount(latestByExchange.total?.equity_usdt) }}</strong>
        <small>USDT</small>
      </div>
      <div class="total-secondary-grid">
        <div>
          <span>今日已实现</span>
          <strong :class="valueClass(todayRealizedValue)">{{ signedAmount(todayRealizedValue) }}</strong>
          <small>USDT</small>
        </div>
        <div>
          <span>已实现年化</span>
          <strong :class="valueClass(annualizedValue)">{{ formatPercent(annualizedValue) }}</strong>
          <small>{{ annualizedHint }}</small>
        </div>
      </div>
    </section>

    <section class="exchange-grid" :aria-busy="loading">
      <article v-for="exchange in (['binance', 'gate'] as const)" :key="exchange" class="exchange-card">
        <div class="exchange-title">
          <span class="exchange-dot" :class="exchange"></span>
          <h2>{{ exchange === 'binance' ? 'Binance' : 'Gate' }}</h2>
        </div>
        <div class="primary-metric">
          <span>总资产</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.equity_usdt) }}</strong>
          <small>USDT</small>
        </div>
        <div class="secondary-metric">
          <span>可用资金</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.available_usdt) }} <small>USDT</small></strong>
        </div>
        <div v-if="exchange === 'gate'" class="secondary-metric gate-mmr-metric">
          <span>全仓 MMR <i :class="mmrClass(displayedRiskStatus)">{{ displayedRiskLabel }}</i></span>
          <strong :class="mmrClass(displayedRiskStatus)">{{ formatPercent(displayedMmr, 1) }}</strong>
        </div>
        <div v-if="exchange === 'binance'" class="secondary-metric">
          <span>BNB 可用</span>
          <strong>{{ formatBnb(latestByExchange.binance?.bnb_available) }} <small>BNB</small></strong>
        </div>
      </article>
    </section>

    <nav class="period-selector" aria-label="图表时间范围">
      <button
        v-for="option in periodOptions"
        :key="option.value"
        :class="{ active: selectedDays === option.value }"
        @click="selectedDays = option.value"
      >
        {{ option.label }}
      </button>
    </nav>

    <section class="chart-card">
      <div class="chart-heading">
        <div>
          <h2>总资产曲线</h2>
          <p>Binance + Gate 合计</p>
        </div>
        <strong>{{ formatAmount(latestByExchange.total?.equity_usdt) }}</strong>
      </div>
      <div ref="equityChartRef" class="mobile-chart"></div>
      <p v-if="!loading && equityRows.length === 0" class="empty-state">暂无资产曲线数据</p>
    </section>

    <section class="chart-card daily-card">
      <div class="chart-heading">
        <div>
          <h2>每日收益</h2>
          <p>净已实现收益 · USDT / 收益率</p>
        </div>
        <strong v-if="dailyRows.length" :class="valueClass(dailyRows[dailyRows.length - 1]?.daily_realized_pnl_usdt)">
          {{ signedAmount(dailyRows[dailyRows.length - 1]?.daily_realized_pnl_usdt) }}
        </strong>
      </div>
      <div ref="dailyChartRef" class="mobile-chart"></div>
      <p v-if="!loading && dailyRows.length === 0" class="empty-state">暂无每日收益数据</p>
    </section>

    <footer>{{ snapshotTime }} · MMR 每 5 秒，资产每 30 秒自动更新</footer>

    <Teleport to="body">
      <div v-if="notificationOpen" class="notification-backdrop" @click.self="notificationOpen = false">
        <section class="notification-sheet" role="dialog" aria-modal="true" aria-label="推送消息">
          <div class="notification-sheet-handle"></div>
          <header class="notification-sheet-header">
            <div>
              <h2>推送消息</h2>
              <p>{{ notificationUnreadCount > 0 ? `${notificationUnreadCount} 条未读` : '暂无未读消息' }}</p>
            </div>
            <div class="notification-sheet-actions">
              <button :disabled="notificationUnreadCount === 0" @click="markAllMobileNotificationsRead">全部已读</button>
              <button class="notification-close" aria-label="关闭消息" @click="notificationOpen = false">×</button>
            </div>
          </header>

          <div v-if="notificationLoading" class="notification-state">消息加载中...</div>
          <div v-else-if="notificationError" class="notification-state notification-state-error">
            <span>{{ notificationError }}</span>
            <button @click="fetchMobileNotifications(true, true)">重试</button>
          </div>
          <div v-else-if="notificationItems.length === 0" class="notification-state">暂无推送消息</div>
          <div v-else class="notification-list">
            <article
              v-for="item in notificationItems"
              :key="item.id"
              class="notification-item"
              :class="[notificationTypeClass(item.type), { unread: !item.read_at }]"
            >
              <div class="notification-item-heading">
                <div class="notification-item-title">
                  <i v-if="!item.read_at"></i>
                  <strong>{{ item.title }}</strong>
                </div>
                <time>{{ formatNotificationTime(item.event_at || item.created_at) }}</time>
              </div>
              <p>{{ item.message }}</p>
              <button v-if="!item.read_at" @click="markMobileNotificationRead(item)">标记已读</button>
            </article>
          </div>
        </section>
      </div>
    </Teleport>
  </main>
</template>

<style scoped>
.mobile-capital-page {
  --mobile-bg: #0b1017;
  --mobile-card: #121a24;
  --mobile-border: #223044;
  --mobile-text: #f4f7fb;
  --mobile-muted: #8290a4;
  width: 100%;
  min-height: 100dvh;
  padding: max(14px, env(safe-area-inset-top)) 14px max(24px, env(safe-area-inset-bottom));
  overflow-x: hidden;
  background:
    radial-gradient(circle at 12% -5%, rgba(55, 132, 255, .18), transparent 30%),
    var(--mobile-bg);
  color: var(--mobile-text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", sans-serif;
  -webkit-font-smoothing: antialiased;
}

.mobile-header {
  position: sticky;
  z-index: 10;
  top: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 58px;
  margin: calc(-1 * max(14px, env(safe-area-inset-top))) -14px 14px;
  padding: max(12px, env(safe-area-inset-top)) 16px 10px;
  border-bottom: 1px solid rgba(34, 48, 68, .72);
  background: rgba(11, 16, 23, .82);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  backdrop-filter: blur(18px) saturate(140%);
}

.mobile-header h1 {
  font-size: 21px;
  font-weight: 720;
  letter-spacing: -.02em;
}

.mobile-header p,
.chart-heading p,
footer {
  margin-top: 3px;
  color: var(--mobile-muted);
  font-size: 11px;
}

.header-actions { display: flex; align-items: center; gap: 7px; }

.bell-button,
.refresh-button {
  position: relative;
  display: grid;
  width: 44px;
  height: 44px;
  place-items: center;
  border: 1px solid var(--mobile-border);
  border-radius: 50%;
  background: rgba(24, 35, 49, .86);
  color: #76b4ff;
  touch-action: manipulation;
}

.bell-button svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
.bell-button:active,
.refresh-button:active { transform: scale(.94); }
.bell-badge { position: absolute; top: -3px; right: -4px; display: grid; min-width: 17px; height: 17px; place-items: center; padding: 0 4px; border: 2px solid #0b1017; border-radius: 9px; background: #ff5f6d; color: #fff; font-size: 9px; font-weight: 750; line-height: 1; }

.refresh-button {
  font-size: 25px;
  line-height: 1;
}

.refresh-button:disabled { opacity: .7; }
.refresh-button.spinning span { animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.error-banner {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(255, 107, 107, .35);
  border-radius: 10px;
  background: rgba(255, 107, 107, .08);
  color: #ff9494;
  font-size: 12px;
  line-height: 1.45;
}

.exchange-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.total-card,
.exchange-card,
.chart-card {
  min-width: 0;
  border: 1px solid var(--mobile-border);
  border-radius: 16px;
  background: linear-gradient(145deg, rgba(20, 30, 42, .97), rgba(15, 23, 33, .97));
  box-shadow: 0 10px 30px rgba(0, 0, 0, .13);
}

.total-card { padding: 15px; }
.total-card-title { display: flex; align-items: center; gap: 7px; }
.total-card-title h2 { font-size: 14px; font-weight: 680; }
.exchange-dot.total { background: #76b4ff; box-shadow: 0 0 10px rgba(118, 180, 255, .55); }
.total-primary-metric { margin: 15px 0 13px; }
.total-primary-metric > span,
.total-secondary-grid span { display: block; color: var(--mobile-muted); font-size: 11px; }
.total-primary-metric > strong { display: inline-block; margin-top: 4px; font-size: clamp(29px, 8vw, 36px); font-weight: 760; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.total-primary-metric > small { margin-left: 5px; color: var(--mobile-muted); font-size: 9px; }
.total-secondary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid rgba(43, 55, 72, .7); }
.total-secondary-grid > div { min-width: 0; padding-top: 11px; }
.total-secondary-grid > div + div { margin-left: 13px; padding-left: 13px; border-left: 1px solid rgba(43, 55, 72, .7); }
.total-secondary-grid strong { display: block; margin-top: 5px; overflow: hidden; font-size: 17px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.total-secondary-grid small { display: block; min-height: 14px; margin-top: 2px; color: var(--mobile-muted); font-size: 9px; line-height: 1.4; }

.exchange-grid { margin-top: 10px; }
.exchange-card { padding: 14px 13px 12px; }
.exchange-title { display: flex; align-items: center; gap: 7px; }
.exchange-title h2 { font-size: 13px; font-weight: 650; }
.exchange-dot { width: 7px; height: 7px; border-radius: 50%; }
.exchange-dot.binance { background: #f5b942; box-shadow: 0 0 9px rgba(245, 185, 66, .55); }
.exchange-dot.gate { background: #38d6b4; box-shadow: 0 0 9px rgba(56, 214, 180, .55); }

.primary-metric { margin: 16px 0 13px; }
.primary-metric > span,
.secondary-metric > span,
.highlight-label > span { display: block; color: var(--mobile-muted); font-size: 11px; }
.primary-metric strong { display: block; margin-top: 5px; overflow: hidden; font-size: clamp(20px, 5.6vw, 25px); font-weight: 740; letter-spacing: -.035em; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
.primary-metric small { color: var(--mobile-muted); font-size: 9px; }
.secondary-metric { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; padding-top: 9px; border-top: 1px solid rgba(43, 55, 72, .7); }
.secondary-metric + .secondary-metric { margin-top: 8px; }
.secondary-metric strong { min-width: 0; overflow: hidden; font-size: 12px; text-align: right; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.secondary-metric small { color: var(--mobile-muted); font-size: 8px; font-weight: 500; }
.gate-mmr-metric > span { display: inline-flex; align-items: center; gap: 4px; }
.gate-mmr-metric i { font-size: 9px; font-style: normal; }

.safe,
.positive { color: #35d39a !important; }
.warning { color: #f5b942 !important; }
.danger,
.negative { color: #ff6b6b !important; }
.muted { color: var(--mobile-muted) !important; }

.period-selector {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 4px;
  margin: 16px 0 10px;
  padding: 4px;
  border: 1px solid rgba(34, 48, 68, .9);
  border-radius: 13px;
  background: #0f1721;
}

.period-selector button {
  min-height: 44px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--mobile-muted);
  font-family: inherit;
  font-size: 13px;
  font-weight: 620;
  touch-action: manipulation;
}

.period-selector button.active { background: #263a53; color: #eaf3ff; box-shadow: 0 2px 8px rgba(0, 0, 0, .2); }

.chart-card { position: relative; margin-top: 10px; padding: 14px 8px 8px; }
.chart-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 0 8px; }
.chart-heading h2 { font-size: 14px; font-weight: 680; }
.chart-heading strong { max-width: 48%; overflow: hidden; color: #76b4ff; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.mobile-chart { width: 100%; height: 235px; margin-top: 4px; touch-action: pan-y; }
.daily-card .mobile-chart { height: 245px; }
.empty-state { position: absolute; inset: 90px 0 0; display: grid; place-items: center; color: var(--mobile-muted); font-size: 12px; pointer-events: none; }

footer { padding: 18px 0 4px; text-align: center; }

.notification-backdrop { position: fixed; z-index: 3000; inset: 0; display: flex; align-items: flex-end; justify-content: center; padding-top: max(24px, env(safe-area-inset-top)); background: rgba(2, 6, 12, .7); -webkit-backdrop-filter: blur(5px); backdrop-filter: blur(5px); }
.notification-sheet { display: flex; width: min(100%, 560px); max-height: min(82dvh, 760px); flex-direction: column; padding: 7px 14px max(14px, env(safe-area-inset-bottom)); overflow: hidden; border: 1px solid #2a394d; border-bottom: 0; border-radius: 22px 22px 0 0; background: #101823; color: #f4f7fb; box-shadow: 0 -18px 50px rgba(0, 0, 0, .38); font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", sans-serif; }
.notification-sheet-handle { width: 38px; height: 4px; flex: 0 0 4px; margin: 1px auto 11px; border-radius: 2px; background: #435268; }
.notification-sheet-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 2px 12px; border-bottom: 1px solid #253247; }
.notification-sheet-header h2 { font-size: 18px; font-weight: 720; }
.notification-sheet-header p { margin-top: 3px; color: #8290a4; font-size: 11px; }
.notification-sheet-actions { display: flex; align-items: center; gap: 7px; }
.notification-sheet-actions button,
.notification-state button,
.notification-item > button { min-height: 34px; border: 0; border-radius: 9px; padding: 0 10px; background: rgba(76, 153, 255, .13); color: #76b4ff; font-family: inherit; font-size: 11px; font-weight: 650; touch-action: manipulation; }
.notification-sheet-actions button:disabled { opacity: .4; }
.notification-sheet-actions .notification-close { width: 38px; min-height: 38px; padding: 0; background: #202c3c; color: #c8d2df; font-size: 24px; font-weight: 400; line-height: 1; }
.notification-list { display: grid; gap: 9px; min-height: 0; padding: 12px 1px 4px; overflow-y: auto; overscroll-behavior: contain; }
.notification-item { --notification-color: #76b4ff; position: relative; padding: 12px; border: 1px solid #273549; border-radius: 13px; background: #151f2b; }
.notification-item.notification-warning { --notification-color: #f5b942; }
.notification-item.notification-error { --notification-color: #ff6b6b; }
.notification-item.notification-success { --notification-color: #35d39a; }
.notification-item.unread { border-color: color-mix(in srgb, var(--notification-color) 48%, #273549); background: color-mix(in srgb, var(--notification-color) 8%, #151f2b); }
.notification-item-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.notification-item-title { display: flex; align-items: center; gap: 7px; min-width: 0; }
.notification-item-title i { width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: var(--notification-color); box-shadow: 0 0 8px color-mix(in srgb, var(--notification-color) 65%, transparent); }
.notification-item-title strong { color: #eaf0f8; font-size: 13px; font-weight: 680; overflow-wrap: anywhere; }
.notification-item time { flex-shrink: 0; color: #748297; font-size: 10px; white-space: nowrap; }
.notification-item > p { margin-top: 8px; color: #b3bfce; font-size: 12px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.notification-item > button { display: block; min-height: 30px; margin: 9px 0 0 auto; }
.notification-state { display: grid; min-height: 210px; place-items: center; gap: 10px; color: #8290a4; font-size: 12px; text-align: center; }
.notification-state-error { color: #ff8585; }

@media (max-width: 350px) {
  .mobile-capital-page { padding-inline: 10px; }
  .mobile-header { margin-inline: -10px; }
  .exchange-grid { gap: 7px; }
  .exchange-card { padding-inline: 10px; }
}

@media (min-width: 700px) {
  .mobile-capital-page { max-width: 560px; margin: 0 auto; border-inline: 1px solid var(--mobile-border); }
}
</style>
