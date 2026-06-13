<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { get } from '../utils/request'
import { showError } from '../utils/message'

interface MarginAsset {
  asset: string
  free: number
  locked: number
  borrowed: number
  interest: number
  netAsset: number
}

interface ReverseCapitalSnapshot {
  strategy: string
  timestamp: number
  errors?: Record<string, string>
  binance_cross_margin?: {
    borrowEnabled: boolean | null
    tradeEnabled: boolean | null
    marginLevel: number
    totalAssetOfBtc: number
    totalLiabilityOfBtc: number
    totalNetAssetOfBtc: number
    USDT: MarginAsset
    BNB: MarginAsset
  }
  gate_futures?: {
    available: number
    total: number
    unrealised_pnl: number
    position_margin: number
    order_margin: number
  }
}

interface CapitalRow {
  id: number
  snapshot_at: string
  exchange: ExchangeKey
  equity_usdt: number | null
  available_usdt: number | null
  locked_usdt: number | null
  margin_used_usdt: number | null
  unrealized_pnl_usdt: number | null
  liability_usdt: number | null
  margin_level: number | null
  bnb_available: number | null
}

type ExchangeKey = 'binance' | 'gate' | 'total'
type ChartMetric =
  | 'equity_usdt'
  | 'available_usdt'
  | 'margin_used_usdt'
  | 'unrealized_pnl_usdt'
  | 'liability_usdt'

const latestRows = ref<CapitalRow[]>([])
const historyRows = ref<CapitalRow[]>([])
const loading = ref(false)
const filterDays = ref(1)
const selectedMetric = ref<ChartMetric>('equity_usdt')
const selectedExchange = ref<ExchangeKey>('total')
const chartRef = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

const metricOptions: Array<{ key: ChartMetric; label: string; group: 'asset' | 'pnl'; color: string }> = [
  { key: 'equity_usdt', label: '总资产', group: 'asset', color: '#67c23a' },
  { key: 'available_usdt', label: '可用资金', group: 'asset', color: '#409eff' },
  { key: 'margin_used_usdt', label: '占用资金', group: 'asset', color: '#e6a23c' },
  { key: 'unrealized_pnl_usdt', label: '未实现盈亏', group: 'pnl', color: '#f56c6c' },
  { key: 'liability_usdt', label: '借款/负债', group: 'asset', color: '#9b59b6' },
]

const latestByExchange = computed(() => {
  const result: Record<string, CapitalRow | undefined> = {}
  for (const row of latestRows.value) result[row.exchange] = row
  return result
})

const errors = ref<Record<string, string>>({})
const lastUpdatedAt = computed(() => latestRows.value[0]?.snapshot_at || '-')

const chartRows = computed(() => {
  const start = Date.now() - filterDays.value * 24 * 60 * 60 * 1000
  return historyRows.value.filter((row) => new Date(row.snapshot_at).getTime() >= start)
})

const chartSeries = computed(() => {
  const option = metricOptions.find((item) => item.key === selectedMetric.value)!
  const points = chartRows.value
    .filter((row) => row.exchange === selectedExchange.value)
    .map((row) => ({
      time: row.snapshot_at,
      value: Number(row[selectedMetric.value] ?? 0),
    }))
  return [{
    exchange: selectedExchange.value,
    metric: selectedMetric.value,
    label: `${exchangeLabel(selectedExchange.value)} ${option.label}`,
    color: option.color,
    group: option.group,
    points,
  }]
})

function formatAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatSmall(value: number | null | undefined, decimals = 8): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toLocaleString('en-US', { maximumFractionDigits: decimals })
}

function exchangeLabel(exchange: string): string {
  if (exchange === 'total') return '合计'
  if (exchange === 'binance') return 'Binance'
  if (exchange === 'gate') return 'Gate'
  return exchange
}

function formatTooltipTime(value: unknown): string {
  const date = new Date(String(value))
  if (!Number.isFinite(date.getTime())) return String(value ?? '-')
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function snapshotTime(snapshot: ReverseCapitalSnapshot): string {
  const d = new Date(Number(snapshot.timestamp || Date.now() / 1000) * 1000)
  return d.toISOString()
}

function rowsFromSnapshot(snapshot: ReverseCapitalSnapshot): CapitalRow[] {
  const time = snapshotTime(snapshot)
  const margin = snapshot.binance_cross_margin
  const gate = snapshot.gate_futures
  const usdt = margin?.USDT
  const bnb = margin?.BNB
  const binanceEquity = Number(usdt?.netAsset ?? 0)
  const binanceAvailable = Number(usdt?.free ?? 0)
  const binanceLocked = Number(usdt?.locked ?? 0)
  const binanceLiability = Number(usdt?.borrowed ?? 0) + Number(usdt?.interest ?? 0)
  const gateEquity = Number(gate?.total ?? 0)
  const gateAvailable = Number(gate?.available ?? 0)
  const gateMargin = Number(gate?.position_margin ?? 0) + Number(gate?.order_margin ?? 0)
  const gateUnrealized = Number(gate?.unrealised_pnl ?? 0)

  return [
    {
      id: Number(snapshot.timestamp || Date.now()),
      snapshot_at: time,
      exchange: 'binance',
      equity_usdt: binanceEquity,
      available_usdt: binanceAvailable,
      locked_usdt: binanceLocked,
      margin_used_usdt: binanceLocked,
      unrealized_pnl_usdt: null,
      liability_usdt: binanceLiability,
      margin_level: Number(margin?.marginLevel ?? 0),
      bnb_available: Number(bnb?.free ?? 0),
    },
    {
      id: Number(snapshot.timestamp || Date.now()) + 1,
      snapshot_at: time,
      exchange: 'gate',
      equity_usdt: gateEquity,
      available_usdt: gateAvailable,
      locked_usdt: null,
      margin_used_usdt: gateMargin,
      unrealized_pnl_usdt: gateUnrealized,
      liability_usdt: null,
      margin_level: null,
      bnb_available: null,
    },
    {
      id: Number(snapshot.timestamp || Date.now()) + 2,
      snapshot_at: time,
      exchange: 'total',
      equity_usdt: binanceEquity + gateEquity,
      available_usdt: binanceAvailable + gateAvailable,
      locked_usdt: binanceLocked,
      margin_used_usdt: binanceLocked + gateMargin,
      unrealized_pnl_usdt: gateUnrealized,
      liability_usdt: binanceLiability,
      margin_level: Number(margin?.marginLevel ?? 0),
      bnb_available: Number(bnb?.free ?? 0),
    },
  ]
}

function appendHistory(rows: CapitalRow[]) {
  const existingKeys = new Set(historyRows.value.map((row) => `${row.snapshot_at}-${row.exchange}`))
  const additions = rows.filter((row) => !existingKeys.has(`${row.snapshot_at}-${row.exchange}`))
  if (!additions.length) return
  const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000
  historyRows.value = [...historyRows.value, ...additions]
    .filter((row) => new Date(row.snapshot_at).getTime() >= cutoff)
    .sort((a, b) => new Date(a.snapshot_at).getTime() - new Date(b.snapshot_at).getTime())
}

function buildChartOption(): EChartsOption {
  const textColor = getComputedStyle(document.documentElement).getPropertyValue('--app-text').trim() || '#303133'
  const mutedColor = getComputedStyle(document.documentElement).getPropertyValue('--app-text-muted').trim() || '#909399'
  const borderColor = getComputedStyle(document.documentElement).getPropertyValue('--app-border').trim() || '#dcdfe6'
  const surfaceColor = getComputedStyle(document.documentElement).getPropertyValue('--app-surface').trim() || '#ffffff'

  return {
    color: chartSeries.value.map((series) => series.color),
    animation: false,
    grid: { top: 42, right: 76, bottom: 52, left: 72 },
    legend: {
      top: 0,
      textStyle: { color: mutedColor },
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: surfaceColor,
      borderColor,
      textStyle: { color: textColor },
      valueFormatter: (value) => `${formatAmount(Number(value))} USDT`,
      axisPointer: { type: 'cross', label: { backgroundColor: '#606266' } },
      formatter: (params) => {
        const items: any[] = Array.isArray(params) ? params : [params]
        const title = formatTooltipTime(items[0]?.axisValue)
        const rows = items.map((item: any) => {
          const value = Array.isArray(item.value) ? item.value[1] : item.value
          return `${item.marker}${item.seriesName}: ${formatAmount(Number(value))} USDT`
        })
        return [title, ...rows].join('<br/>')
      },
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: borderColor } },
      axisTick: { show: false },
      axisLabel: { color: mutedColor },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: {
        color: mutedColor,
        formatter: (value: number) => formatAmount(value),
      },
      splitLine: { lineStyle: { color: borderColor, type: 'dashed' } },
    },
    series: chartSeries.value.map((series) => ({
      name: series.label,
      type: 'line',
      smooth: true,
      showSymbol: false,
      symbolSize: 7,
      emphasis: { focus: 'series' },
      data: series.points.map((point) => [point.time, point.value]),
      lineStyle: { width: 2.2 },
    })),
  }
}

function updateChart() {
  if (!chart) return
  chart.setOption(buildChartOption(), true)
}

async function initChart() {
  await nextTick()
  if (!chartRef.value) return
  chart = echarts.init(chartRef.value)
  updateChart()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartRef.value)
}

async function fetchCapital() {
  loading.value = true
  try {
    const res = await get('/api/trading/reverse-capital')
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '反向资金加载失败')
      return
    }
    errors.value = data.errors || {}
    const rows = rowsFromSnapshot(data)
    latestRows.value = rows
    appendHistory(rows)
  } catch (e: any) {
    showError(e?.message || '反向资金加载失败')
  } finally {
    loading.value = false
  }
}

function setDays(days: number) {
  filterDays.value = days
}

onMounted(async () => {
  await fetchCapital()
  await initChart()
  refreshTimer = setInterval(fetchCapital, 60_000)
})

watch([historyRows, selectedMetric, selectedExchange, filterDays], () => {
  updateChart()
})

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="capital-page">
    <div class="toolbar">
      <el-button-group size="small">
        <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="setDays(1)">24小时</el-button>
        <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="setDays(7)">7天</el-button>
        <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="setDays(30)">30天</el-button>
        <el-button :type="filterDays === 90 ? 'primary' : 'default'" @click="setDays(90)">90天</el-button>
      </el-button-group>
      <el-button size="small" :loading="loading" @click="fetchCapital">刷新</el-button>
      <span class="updated-at">最后更新：{{ lastUpdatedAt }}</span>
    </div>

    <div v-if="Object.keys(errors).length" class="error-strip">
      <span v-for="(message, key) in errors" :key="key">{{ key }}: {{ message }}</span>
    </div>

    <div class="summary-grid">
      <div
        v-for="exchange in ['binance', 'gate', 'total']"
        :key="exchange"
        class="summary-card"
      >
        <div class="card-title">{{ exchangeLabel(exchange) }}</div>
        <div class="metric-row">
          <span>总资产</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.equity_usdt) }}</strong>
        </div>
        <div class="metric-row">
          <span>可用资金</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.available_usdt) }}</strong>
        </div>
        <div class="metric-row">
          <span>占用</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.margin_used_usdt) }}</strong>
        </div>
        <div v-if="exchange !== 'binance'" class="metric-row">
          <span>未实现盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.unrealized_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.unrealized_pnl_usdt) }}
          </strong>
        </div>
        <div v-if="exchange !== 'gate'" class="metric-row">
          <span>借款/利息</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.liability_usdt) }}</strong>
        </div>
        <div v-if="exchange !== 'gate'" class="metric-row">
          <span>保证金率</span>
          <strong>{{ formatSmall(latestByExchange[exchange]?.margin_level, 4) }}</strong>
        </div>
        <div v-if="exchange !== 'gate'" class="metric-row">
          <span>BNB 可用</span>
          <strong>{{ formatSmall(latestByExchange[exchange]?.bnb_available, 8) }}</strong>
        </div>
      </div>
    </div>

    <div class="chart-panel">
      <div class="chart-header">
        <span>资金趋势</span>
        <el-radio-group
          v-model="selectedExchange"
          size="small"
          class="exchange-selector"
        >
          <el-radio-button label="binance">Binance</el-radio-button>
          <el-radio-button label="gate">Gate</el-radio-button>
          <el-radio-button label="total">合计</el-radio-button>
        </el-radio-group>
      </div>
      <div class="metric-selector-row">
        <el-radio-group
          v-model="selectedMetric"
          size="small"
          class="metric-selector"
        >
          <el-radio-button
            v-for="metric in metricOptions"
            :key="metric.key"
            :label="metric.key"
          >
            {{ metric.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div class="chart-wrap">
        <div ref="chartRef" class="echarts-chart"></div>
        <div v-if="!chartRows.length" class="empty-text">暂无资金快照</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.capital-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.updated-at {
  color: var(--app-text-muted);
  font-size: 13px;
}

.error-strip {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid rgba(245, 108, 108, 0.45);
  border-radius: 6px;
  color: #f56c6c;
  background: rgba(245, 108, 108, 0.08);
  font-size: 13px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(220px, 1fr));
  gap: 12px;
}

.summary-card {
  border: 1px solid var(--app-border);
  background: var(--app-surface);
  border-radius: 6px;
  padding: 12px 14px;
}

.card-title {
  color: var(--app-text);
  font-size: 15px;
  font-weight: 600;
  text-transform: uppercase;
  margin-bottom: 8px;
}

.metric-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 5px 0;
  font-size: 13px;
  color: var(--app-text-muted);
}

.metric-row strong {
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
}

.pnl-positive {
  color: #67c23a !important;
}

.pnl-negative {
  color: #f56c6c !important;
}

.chart-panel {
  border: 1px solid var(--app-border);
  background: var(--app-surface);
  border-radius: 6px;
  padding: 12px;
}

.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--app-text);
  font-weight: 600;
  margin-bottom: 8px;
}

.metric-selector-row {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 8px;
}

.exchange-selector,
.metric-selector {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.chart-wrap {
  position: relative;
  width: 100%;
  overflow-x: auto;
}

.echarts-chart {
  width: 100%;
  min-width: 720px;
  height: 320px;
}

.empty-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  color: var(--app-text-muted);
  font-size: 13px;
}

@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }

  .chart-header,
  .metric-selector-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .exchange-selector,
  .metric-selector {
    justify-content: flex-start;
  }
}
</style>
