<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { ElMessageBox } from 'element-plus'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface CapitalRow {
  id: number
  snapshot_at: string
  exchange: 'binance' | 'gate' | 'total'
  equity_usdt: number | null
  available_usdt: number | null
  locked_usdt: number | null
  position_value_usdt: number | null
  margin_used_usdt: number | null
  unrealized_pnl_usdt: number | null
  realized_pnl_usdt: number | null
  funding_pnl_usdt: number | null
  fee_cost_usdt: number | null
  total_pnl_usdt: number | null
  gross_total_pnl_usdt: number | null
  bnb_available?: number | null
  bnb_available_usdt?: number | null
}

type ExchangeKey = 'binance' | 'gate' | 'total'
type HistoryInterval = '1m' | '10m' | '1h'
type TimeWindowKey = '1h' | '3h' | '6h' | '12h' | '1d' | '7d' | '30d' | '90d'
type ChartMetric =
  | 'equity_usdt'
  | 'unrealized_pnl_usdt'
  | 'realized_pnl_usdt'
  | 'funding_pnl_usdt'
  | 'total_pnl_usdt'
  | 'gross_total_pnl_usdt'
  | 'net_realized_return_pct'
type ChartModeKey =
  | 'equity_usdt'
  | 'unrealized_pnl_usdt'
  | 'realized_breakdown'
  | 'gross_total_pnl_usdt'
  | 'net_realized_return_pct'
type ChartSeriesGroup = 'asset' | 'pnl' | 'ratio'
type ChartMetricOption = { key: ChartMetric; label: string; group: ChartSeriesGroup; color: string }
type ChartModeOption = { key: ChartModeKey; label: string }
type ChartLineType = 'solid' | 'dashed'
type ChartSeries = {
  exchange: ExchangeKey
  metric: ChartMetric
  label: string
  color: string
  group: ChartSeriesGroup
  points: Array<{ time: string; value: number }>
  lineType: ChartLineType
}

const latestRows = ref<CapitalRow[]>([])
const historyRows = ref<CapitalRow[]>([])
const loading = ref(false)
const running = ref(false)
const selectedWindow = ref<TimeWindowKey>('7d')
const selectedChartMode = ref<ChartModeKey>('equity_usdt')
const selectedExchange = ref<ExchangeKey>('total')
const selectedInterval = ref<HistoryInterval>('10m')
const showSummaryDetails = ref(false)
const bnbBuying = ref(false)
const chartRef = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null

const metricOptions: ChartMetricOption[] = [
  { key: 'equity_usdt', label: '总资产', group: 'asset', color: '#67c23a' },
  { key: 'unrealized_pnl_usdt', label: '未实现盈亏', group: 'pnl', color: '#e6a23c' },
  { key: 'realized_pnl_usdt', label: '平仓盈亏', group: 'pnl', color: '#9b59b6' },
  { key: 'funding_pnl_usdt', label: '资金费收益', group: 'pnl', color: '#00a870' },
  { key: 'total_pnl_usdt', label: '净已实现收益', group: 'pnl', color: '#409eff' },
  { key: 'gross_total_pnl_usdt', label: '总盈亏', group: 'pnl', color: '#f56c6c' },
  { key: 'net_realized_return_pct', label: '净实现收益率', group: 'ratio', color: '#14b8a6' },
]

const chartModeOptions: ChartModeOption[] = [
  { key: 'equity_usdt', label: '总资产' },
  { key: 'unrealized_pnl_usdt', label: '未实现盈亏' },
  { key: 'realized_breakdown', label: '收益趋势' },
  { key: 'gross_total_pnl_usdt', label: '总盈亏' },
  { key: 'net_realized_return_pct', label: '净实现收益率' },
]

const realizedBreakdownMetrics: ChartMetric[] = [
  'realized_pnl_usdt',
  'funding_pnl_usdt',
  'total_pnl_usdt',
]

const timeWindowOptions: Array<{ key: TimeWindowKey; label: string; hours?: number; days?: number }> = [
  { key: '1h', label: '1小时', hours: 1 },
  { key: '3h', label: '3小时', hours: 3 },
  { key: '6h', label: '6小时', hours: 6 },
  { key: '12h', label: '12小时', hours: 12 },
  { key: '1d', label: '24小时', days: 1 },
  { key: '7d', label: '7天', days: 7 },
  { key: '30d', label: '30天', days: 30 },
  { key: '90d', label: '90天', days: 90 },
]

const latestByExchange = computed(() => {
  const result: Record<string, CapitalRow | undefined> = {}
  for (const row of latestRows.value) result[row.exchange] = row
  return result
})

const chartSeries = computed<ChartSeries[]>(() => {
  const metrics: ChartMetric[] = selectedChartMode.value === 'realized_breakdown'
    ? realizedBreakdownMetrics
    : [selectedChartMode.value]
  const rows = historyRows.value
    .filter((row) => row.exchange === selectedExchange.value)
  const series: ChartSeries[] = metrics.map((metric): ChartSeries => {
    const option = metricOptions.find((item) => item.key === metric)!
    const points = rows.map((row) => ({
      time: row.snapshot_at,
      value: chartMetricValue(row, metric),
    }))
    return {
      exchange: selectedExchange.value,
      metric,
      label: `${exchangeLabel(selectedExchange.value)} ${option.label}`,
      color: option.color,
      group: option.group,
      points,
      lineType: 'solid' as const,
    }
  })
  if (selectedChartMode.value === 'gross_total_pnl_usdt' && series[0]?.points.length > 1) {
    series.push({
      exchange: selectedExchange.value,
      metric: 'gross_total_pnl_usdt',
      label: `${exchangeLabel(selectedExchange.value)} 总盈亏趋势`,
      color: '#ff8f1f',
      group: 'pnl',
      points: buildTrendPoints(series[0].points),
      lineType: 'dashed' as const,
    })
  }
  return series
})

function buildTrendPoints(points: Array<{ time: string; value: number }>): Array<{ time: string; value: number }> {
  const samples = points
    .map((point) => ({
      time: point.time,
      x: new Date(point.time).getTime(),
      y: point.value,
    }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
  if (samples.length < 2) return []

  const firstX = samples[0].x
  const normalized = samples.map((point) => ({
    time: point.time,
    x: point.x - firstX,
    y: point.y,
  }))
  const count = normalized.length
  const sumX = normalized.reduce((sum, point) => sum + point.x, 0)
  const sumY = normalized.reduce((sum, point) => sum + point.y, 0)
  const sumXX = normalized.reduce((sum, point) => sum + point.x * point.x, 0)
  const sumXY = normalized.reduce((sum, point) => sum + point.x * point.y, 0)
  const denominator = count * sumXX - sumX * sumX
  if (denominator === 0) {
    const average = sumY / count
    return normalized.map((point) => ({ time: point.time, value: average }))
  }

  const slope = (count * sumXY - sumX * sumY) / denominator
  const intercept = (sumY - slope * sumX) / count
  return normalized.map((point) => ({
    time: point.time,
    value: intercept + slope * point.x,
  }))
}

function formatAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return `${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`
}

function hasAmount(value: number | null | undefined): boolean {
  return value != null && Number.isFinite(Number(value))
}

function netRealizedReturnPct(row: CapitalRow | undefined): number | null {
  const equity = Number(row?.equity_usdt ?? NaN)
  const netRealized = Number(row?.total_pnl_usdt ?? NaN)
  if (!Number.isFinite(equity) || Math.abs(equity) <= 1e-9 || !Number.isFinite(netRealized)) return null
  return (netRealized / equity) * 100
}

function chartMetricValue(row: CapitalRow, metric: ChartMetric): number {
  if (metric === 'net_realized_return_pct') return netRealizedReturnPct(row) ?? 0
  return Number(row[metric] ?? 0)
}

function formatSeriesValue(value: number, group: ChartSeriesGroup): string {
  return group === 'ratio' ? formatPercent(value) : `${formatAmount(value)} USDT`
}

function chartYAxisIndex(group: ChartSeriesGroup): number {
  if (group === 'asset') return 0
  if (group === 'ratio') return 2
  return 1
}

function formatToken(value: number | null | undefined, digits = 6): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toLocaleString('en-US', { maximumFractionDigits: digits })
}

function formatBnbFeeAsset(row: CapitalRow | undefined): string {
  if (!row) return '-'
  const amount = formatToken(row.bnb_available, 6)
  const value = formatAmount(row.bnb_available_usdt)
  if (amount === '-' && value === '-') return '-'
  return `${amount} BNB / ≈ ${value} USDT`
}

function exchangeLabel(exchange: string): string {
  return exchange === 'total' ? '合计' : exchange
}

function occupiedAmount(row: CapitalRow | undefined, exchange: string): number | null | undefined {
  if (exchange === 'gate') return row?.margin_used_usdt
  return row?.position_value_usdt
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

function buildChartOption(): EChartsOption {
  const textColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--app-text')
    .trim() || '#303133'
  const mutedColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--app-text-muted')
    .trim() || '#909399'
  const borderColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--app-border')
    .trim() || '#dcdfe6'
  const surfaceColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--app-surface')
    .trim() || '#ffffff'

  return {
    color: chartSeries.value.map((series) => series.color),
    animation: false,
    grid: { top: 44, right: 112, bottom: 56, left: 72 },
    legend: {
      type: 'scroll',
      top: 0,
      textStyle: { color: mutedColor },
      pageIconColor: textColor,
      pageTextStyle: { color: mutedColor },
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: surfaceColor,
      borderColor,
      textStyle: { color: textColor },
      axisPointer: { type: 'cross', label: { backgroundColor: '#606266' } },
      formatter: (params) => {
        const items: any[] = Array.isArray(params) ? params : [params]
        const title = formatTooltipTime(items[0]?.axisValue)
        const rows = items.map((item: any) => {
          const value = Array.isArray(item.value) ? item.value[1] : item.value
          const meta = chartSeries.value.find((series) => series.label === item.seriesName)
          return `${item.marker}${item.seriesName}: ${formatSeriesValue(Number(value), meta?.group || 'pnl')}`
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
    yAxis: [
      {
        type: 'value',
        name: '资产',
        scale: true,
        axisLabel: {
          color: mutedColor,
          formatter: (value: number) => formatAmount(value),
        },
        splitLine: { lineStyle: { color: borderColor, type: 'dashed' } },
      },
      {
        type: 'value',
        name: '收益',
        scale: true,
        axisLabel: {
          color: mutedColor,
          formatter: (value: number) => formatAmount(value),
        },
        splitLine: { show: false },
      },
      {
        type: 'value',
        name: '收益率',
        scale: true,
        position: 'right',
        offset: 54,
        axisLabel: {
          color: mutedColor,
          formatter: (value: number) => formatPercent(value),
        },
        splitLine: { show: false },
      },
    ],
    series: chartSeries.value.map((series) => ({
      name: series.label,
      type: 'line',
      smooth: true,
      showSymbol: false,
      symbolSize: 7,
      yAxisIndex: chartYAxisIndex(series.group),
      emphasis: { focus: 'series' },
      data: series.points.map((point) => [point.time, point.value]),
      lineStyle: { width: series.lineType === 'dashed' ? 2 : 2.2, type: series.lineType },
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
    const latestRes = await get('/api/trading/capital/latest')
    const latest = await latestRes.json()
    latestRows.value = latest.rows || []

    await fetchHistory()
  } catch (e: any) {
    showError(e?.message || '获取资金数据失败')
  } finally {
    loading.value = false
  }
}

async function fetchHistory() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    const window = timeWindowOptions.find((item) => item.key === selectedWindow.value)
      || timeWindowOptions.find((item) => item.key === '7d')!
    if (window.hours != null) params.set('hours', String(window.hours))
    else params.set('days', String(window.days || 7))
    params.set('exchange', selectedExchange.value)
    params.set('interval', selectedInterval.value)
    const historyRes = await get(`/api/trading/capital/history?${params.toString()}`)
    const history = await historyRes.json()
    historyRows.value = history.rows || []
  } catch (e: any) {
    showError(e?.message || '获取资金曲线失败')
  } finally {
    loading.value = false
  }
}

async function runSnapshot() {
  running.value = true
  try {
    const res = await post('/api/trading/capital/run')
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || '资金采集完成')
      await fetchCapital()
    } else {
      showError(data.message || '资金采集失败')
    }
  } catch (e: any) {
    showError(e?.message || '资金采集请求失败')
  } finally {
    running.value = false
  }
}

async function buyBnbFeeAsset() {
  const binance = latestByExchange.value.binance
  let amount = 0
  try {
    const { value } = await ElMessageBox.prompt(
      `当前 BNB 可用: ${formatBnbFeeAsset(binance)}\nBinance USDT 可用: ${formatAmount(binance?.available_usdt)}`,
      '买入 BNB',
      {
        confirmButtonText: '确认买入',
        cancelButtonText: '取消',
        inputValue: '20',
        inputPlaceholder: '输入 USDT 金额',
        inputPattern: /^\d+(\.\d{1,2})?$/,
        inputErrorMessage: '请输入有效金额，最多 2 位小数',
        type: 'warning',
      }
    )
    amount = Number(value)
  } catch {
    return
  }

  if (!Number.isFinite(amount) || amount < 5) {
    showError('买入金额至少 5 USDT')
    return
  }
  if (amount > 200) {
    showError('单次买入金额不能超过 200 USDT')
    return
  }
  const available = Number(binance?.available_usdt ?? 0)
  if (amount > available) {
    showError(`Binance USDT 可用余额不足: ${formatAmount(available)}`)
    return
  }

  bnbBuying.value = true
  try {
    const res = await post('/api/trading/capital/binance-bnb/buy', { amount_usdt: amount })
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || 'BNB 买入成功')
      await fetchCapital()
    } else {
      showError(data.message || 'BNB 买入失败')
    }
  } catch (e: any) {
    if (e?.message && !e.message.includes('未授权') && !e.message.includes('权限不足')) {
      showError(`BNB 买入请求失败: ${e.message}`)
    }
  } finally {
    bnbBuying.value = false
  }
}

function setWindow(window: TimeWindowKey) {
  selectedWindow.value = window
  fetchCapital()
}

onMounted(async () => {
  await fetchCapital()
  await initChart()
})

watch([historyRows, selectedChartMode, selectedExchange], () => {
  updateChart()
})

watch([selectedExchange, selectedInterval], () => {
  fetchHistory()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="capital-page">
    <div class="toolbar">
      <el-button size="small" type="primary" :loading="running" @click="runSnapshot">
        立即采集
      </el-button>
      <el-button-group size="small">
        <el-button
          v-for="window in timeWindowOptions"
          :key="window.key"
          :type="selectedWindow === window.key ? 'primary' : 'default'"
          @click="setWindow(window.key)"
        >
          {{ window.label }}
        </el-button>
      </el-button-group>
      <el-button size="small" :loading="loading" @click="fetchCapital">刷新</el-button>
      <el-button size="small" @click="showSummaryDetails = !showSummaryDetails">
        {{ showSummaryDetails ? '收起详情' : '详细' }}
      </el-button>
    </div>

    <div class="summary-grid">
      <div
        v-for="exchange in ['binance', 'gate', 'total']"
        :key="exchange"
        class="summary-card"
      >
        <div class="card-header">
          <div class="card-title">{{ exchange === 'total' ? '合计' : exchange }}</div>
          <el-button
            v-if="exchange === 'binance'"
            size="small"
            type="primary"
            :loading="bnbBuying"
            @click="buyBnbFeeAsset"
          >
            买BNB
          </el-button>
        </div>
        <div class="metric-row">
          <span>总资产</span>
          <strong>
            <span>{{ formatAmount(latestByExchange[exchange]?.equity_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.equity_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div class="metric-row available-row">
          <span>可用资金</span>
          <strong class="available-value">
            <span>{{ formatAmount(latestByExchange[exchange]?.available_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.available_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div class="metric-row">
          <span>占用</span>
          <strong>
            <span>{{ formatAmount(occupiedAmount(latestByExchange[exchange], exchange)) }}</span>
            <span v-if="hasAmount(occupiedAmount(latestByExchange[exchange], exchange))" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="exchange === 'binance'" class="metric-row bnb-metric-row">
          <span>BNB可用</span>
          <strong class="bnb-value">
            <span>{{ formatToken(latestByExchange.binance?.bnb_available, 6) }}</span>
            <span v-if="hasAmount(latestByExchange.binance?.bnb_available)" class="metric-unit">BNB</span>
            <span class="metric-separator">/ ≈</span>
            <span>{{ formatAmount(latestByExchange.binance?.bnb_available_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange.binance?.bnb_available_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>未实现盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.unrealized_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.unrealized_pnl_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.unrealized_pnl_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>已实现盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.realized_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.realized_pnl_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.realized_pnl_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>资金费收益</span>
          <strong :class="Number(latestByExchange[exchange]?.funding_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.funding_pnl_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.funding_pnl_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>手续费成本</span>
          <strong :class="Number(latestByExchange[exchange]?.fee_cost_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.fee_cost_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.fee_cost_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>净已实现收益</span>
          <strong :class="Number(latestByExchange[exchange]?.total_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.total_pnl_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.total_pnl_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>净实现收益率</span>
          <strong :class="Number(netRealizedReturnPct(latestByExchange[exchange]) || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatPercent(netRealizedReturnPct(latestByExchange[exchange])) }}</span>
          </strong>
        </div>
        <div v-if="showSummaryDetails" class="metric-row">
          <span>总盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.gross_total_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.gross_total_pnl_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.gross_total_pnl_usdt)" class="metric-unit">USDT</span>
          </strong>
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
          <el-radio-button label="binance">binance</el-radio-button>
          <el-radio-button label="gate">gate</el-radio-button>
          <el-radio-button label="total">合计</el-radio-button>
        </el-radio-group>
      </div>
      <div class="metric-selector-row">
        <el-radio-group
          v-model="selectedInterval"
          size="small"
          class="interval-selector"
        >
          <el-radio-button label="1m">1分钟</el-radio-button>
          <el-radio-button label="10m">10分钟</el-radio-button>
          <el-radio-button label="1h">1小时</el-radio-button>
        </el-radio-group>
        <el-radio-group
          v-model="selectedChartMode"
          size="small"
          class="metric-selector"
        >
          <el-radio-button
            v-for="metric in chartModeOptions"
            :key="metric.key"
            :label="metric.key"
          >
            {{ metric.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div class="chart-wrap">
        <div ref="chartRef" class="echarts-chart"></div>
        <div v-if="!historyRows.length" class="empty-text">暂无资金快照</div>
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

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
  min-height: 28px;
}

.card-title {
  color: var(--app-text);
  font-size: 15px;
  font-weight: 600;
  text-transform: uppercase;
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
  display: inline-flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 4px;
  flex-wrap: wrap;
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.metric-unit,
.metric-separator {
  color: var(--app-text-muted) !important;
  font-size: 11px;
  font-weight: 500;
}

.bnb-metric-row {
  align-items: center;
}

.bnb-value {
  max-width: 72%;
}

.available-row {
  background: color-mix(in srgb, var(--app-primary, #409eff) 8%, transparent);
  border-radius: 4px;
  margin: 2px -6px;
  padding-left: 6px;
  padding-right: 6px;
}

.available-value {
  color: #1677ff !important;
  font-size: 14px;
  font-weight: 700;
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
.interval-selector,
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
  .interval-selector,
  .metric-selector {
    justify-content: flex-start;
  }
}
</style>
