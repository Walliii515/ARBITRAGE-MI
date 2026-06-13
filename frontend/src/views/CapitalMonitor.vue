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
  binance_cross_margin?: BinanceCrossMargin | string | null
}

interface MarginAsset {
  asset: string
  free: number | null
  locked: number | null
  borrowed: number | null
  interest: number | null
  netAsset: number | null
}

interface BinanceCrossMargin {
  enabled?: boolean
  status?: 'ok' | 'warning' | 'blocked' | 'unknown' | 'disabled'
  open_allowed?: boolean
  marginLevel?: number | null
  warning_margin_level?: number | null
  min_open_margin_level?: number | null
  borrowEnabled?: boolean | null
  tradeEnabled?: boolean | null
  totalAssetOfBtc?: number | null
  totalLiabilityOfBtc?: number | null
  totalNetAssetOfBtc?: number | null
  USDT?: MarginAsset
  error?: string
}

type ExchangeKey = 'binance' | 'gate' | 'total'
type HistoryInterval = '1m' | '10m' | '1h'
type MarginAction = 'borrow' | 'repay' | 'margin_to_spot' | 'spot_to_margin'
type ChartMetric =
  | 'equity_usdt'
  | 'available_usdt'
  | 'unrealized_pnl_usdt'
  | 'realized_pnl_usdt'
  | 'funding_pnl_usdt'
  | 'total_pnl_usdt'
  | 'gross_total_pnl_usdt'

const latestRows = ref<CapitalRow[]>([])
const historyRows = ref<CapitalRow[]>([])
const loading = ref(false)
const running = ref(false)
const filterDays = ref(7)
const selectedMetric = ref<ChartMetric>('equity_usdt')
const selectedExchange = ref<ExchangeKey>('total')
const selectedInterval = ref<HistoryInterval>('10m')
const marginAction = ref<MarginAction>('borrow')
const marginAmount = ref<number | undefined>(50)
const marginOperating = ref(false)
const chartRef = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null

const metricOptions: Array<{ key: ChartMetric; label: string; group: 'asset' | 'pnl'; color: string }> = [
  { key: 'equity_usdt', label: '总资产', group: 'asset', color: '#67c23a' },
  { key: 'available_usdt', label: '可用资金', group: 'asset', color: '#409eff' },
  { key: 'unrealized_pnl_usdt', label: '未实现盈亏', group: 'pnl', color: '#e6a23c' },
  { key: 'realized_pnl_usdt', label: '平仓盈亏', group: 'pnl', color: '#9b59b6' },
  { key: 'funding_pnl_usdt', label: '资金费收益', group: 'pnl', color: '#00a870' },
  { key: 'total_pnl_usdt', label: '净已实现收益', group: 'pnl', color: '#303133' },
  { key: 'gross_total_pnl_usdt', label: '总盈亏', group: 'pnl', color: '#f56c6c' },
]

const marginActionOptions: Array<{ key: MarginAction; label: string; endpoint: string; direction?: string }> = [
  { key: 'borrow', label: '借入 USDT', endpoint: '/api/trading/capital/binance-margin/borrow' },
  { key: 'repay', label: '还款 USDT', endpoint: '/api/trading/capital/binance-margin/repay' },
  {
    key: 'margin_to_spot',
    label: '杠杆转现货',
    endpoint: '/api/trading/capital/binance-margin/transfer',
    direction: 'margin_to_spot',
  },
  {
    key: 'spot_to_margin',
    label: '现货转杠杆',
    endpoint: '/api/trading/capital/binance-margin/transfer',
    direction: 'spot_to_margin',
  },
]

const latestByExchange = computed(() => {
  const result: Record<string, CapitalRow | undefined> = {}
  for (const row of latestRows.value) result[row.exchange] = row
  return result
})

const chartSeries = computed(() => {
  const option = metricOptions.find((item) => item.key === selectedMetric.value)!
  const points = historyRows.value
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

function formatRatio(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toFixed(3)
}

function marginInfo(exchange: string): BinanceCrossMargin | null {
  const raw = latestByExchange.value[exchange]?.binance_cross_margin
  if (!raw) return null
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      return parsed && typeof parsed === 'object' ? parsed : null
    } catch {
      return null
    }
  }
  return raw
}

function marginStatusText(info: BinanceCrossMargin | null): string {
  if (!info) return '-'
  if (info.error) return '读取失败'
  if (info.enabled === false) return '未启用'
  if (info.status === 'blocked') return '暂停开仓'
  if (info.status === 'warning') return '预警'
  if (info.status === 'ok') return '正常'
  return '未知'
}

function marginStatusClass(info: BinanceCrossMargin | null): string {
  if (!info || info.enabled === false) return ''
  if (info.error || info.status === 'blocked') return 'risk-danger'
  if (info.status === 'warning' || info.status === 'unknown') return 'risk-warning'
  return 'risk-ok'
}

function selectedMarginAction() {
  return marginActionOptions.find((item) => item.key === marginAction.value) || marginActionOptions[0]
}

function exchangeLabel(exchange: string): string {
  return exchange === 'total' ? '合计' : exchange
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
    grid: { top: 44, right: 82, bottom: 56, left: 72 },
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
    ],
    series: chartSeries.value.map((series) => ({
      name: series.label,
      type: 'line',
      smooth: true,
      showSymbol: false,
      symbolSize: 7,
      yAxisIndex: series.group === 'asset' ? 0 : 1,
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
    params.set('days', String(filterDays.value))
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

async function runMarginAction() {
  const amount = Number(marginAmount.value)
  if (!Number.isFinite(amount) || amount <= 0) {
    showError('请输入有效金额')
    return
  }
  const action = selectedMarginAction()
  const margin = marginInfo('binance')
  const marginLevelText = formatRatio(margin?.marginLevel)
  const borrowedText = formatAmount(margin?.USDT?.borrowed)
  try {
    await ElMessageBox.confirm(
      `确认执行 ${action.label} ${amount} USDT？\n当前 Margin Level: ${marginLevelText}\n当前 USDT 借款: ${borrowedText}`,
      'Binance Margin 操作确认',
      {
        confirmButtonText: '确认执行',
        cancelButtonText: '取消',
        type: action.key === 'borrow' ? 'warning' : 'info',
      }
    )
  } catch {
    return
  }

  marginOperating.value = true
  try {
    const payload = action.direction ? { amount, direction: action.direction } : { amount }
    const res = await post(action.endpoint, payload)
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || `${action.label}成功`)
      await fetchCapital()
    } else {
      showError(data.message || `${action.label}失败`)
    }
  } catch (e: any) {
    if (e?.message && !e.message.includes('未授权') && !e.message.includes('权限不足')) {
      showError(`${action.label}请求失败: ${e.message}`)
    }
  } finally {
    marginOperating.value = false
  }
}

function setDays(days: number) {
  filterDays.value = days
  fetchCapital()
}

onMounted(async () => {
  await fetchCapital()
  await initChart()
})

watch([historyRows, selectedMetric, selectedExchange], () => {
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
        <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="setDays(1)">24小时</el-button>
        <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="setDays(7)">7天</el-button>
        <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="setDays(30)">30天</el-button>
        <el-button :type="filterDays === 90 ? 'primary' : 'default'" @click="setDays(90)">90天</el-button>
      </el-button-group>
      <el-button size="small" :loading="loading" @click="fetchCapital">刷新</el-button>
    </div>

    <div class="summary-grid">
      <div
        v-for="exchange in ['binance', 'gate', 'total']"
        :key="exchange"
        class="summary-card"
      >
        <div class="card-title">{{ exchange === 'total' ? '合计' : exchange }}</div>
        <div class="metric-row">
          <span>总资产</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.equity_usdt) }}</strong>
        </div>
        <div class="metric-row">
          <span>可用资金</span>
          <strong>{{ formatAmount(latestByExchange[exchange]?.available_usdt) }}</strong>
        </div>
        <div class="metric-row">
          <span>{{ exchange === 'gate' ? '保证金占用' : '持仓/占用' }}</span>
          <strong>{{ formatAmount(exchange === 'gate' ? latestByExchange[exchange]?.margin_used_usdt : latestByExchange[exchange]?.position_value_usdt) }}</strong>
        </div>
        <div class="metric-row">
          <span>未实现盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.unrealized_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.unrealized_pnl_usdt) }}
          </strong>
        </div>
        <div class="metric-row">
          <span>已实现盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.realized_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.realized_pnl_usdt) }}
          </strong>
        </div>
        <div class="metric-row">
          <span>资金费收益</span>
          <strong :class="Number(latestByExchange[exchange]?.funding_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.funding_pnl_usdt) }}
          </strong>
        </div>
        <div class="metric-row">
          <span>手续费成本</span>
          <strong :class="Number(latestByExchange[exchange]?.fee_cost_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.fee_cost_usdt) }}
          </strong>
        </div>
        <div class="metric-row">
          <span>净已实现收益</span>
          <strong :class="Number(latestByExchange[exchange]?.total_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.total_pnl_usdt) }}
          </strong>
        </div>
        <div class="metric-row">
          <span>总盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.gross_total_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            {{ formatAmount(latestByExchange[exchange]?.gross_total_pnl_usdt) }}
          </strong>
        </div>
        <template v-if="exchange !== 'gate' && marginInfo(exchange)">
          <div class="metric-divider"></div>
          <div class="metric-row">
            <span>Margin 状态</span>
            <strong :class="marginStatusClass(marginInfo(exchange))">
              {{ marginStatusText(marginInfo(exchange)) }}
            </strong>
          </div>
          <div class="metric-row">
            <span>Margin Level</span>
            <strong :class="marginStatusClass(marginInfo(exchange))">
              {{ formatRatio(marginInfo(exchange)?.marginLevel) }}
            </strong>
          </div>
          <div class="metric-row">
            <span>USDT 借款</span>
            <strong>{{ formatAmount(marginInfo(exchange)?.USDT?.borrowed) }}</strong>
          </div>
          <div class="metric-row">
            <span>USDT 利息</span>
            <strong>{{ formatAmount(marginInfo(exchange)?.USDT?.interest) }}</strong>
          </div>
          <div class="metric-row">
            <span>开仓阈值</span>
            <strong>{{ formatRatio(marginInfo(exchange)?.min_open_margin_level) }}</strong>
          </div>
        </template>
      </div>
    </div>

    <div class="margin-action-panel">
      <div class="chart-header">
        <span>Binance Margin 操作</span>
        <span class="margin-state" :class="marginStatusClass(marginInfo('binance'))">
          {{ marginStatusText(marginInfo('binance')) }} / {{ formatRatio(marginInfo('binance')?.marginLevel) }}
        </span>
      </div>
      <div class="margin-action-row">
        <el-radio-group v-model="marginAction" size="small" class="margin-action-selector">
          <el-radio-button
            v-for="action in marginActionOptions"
            :key="action.key"
            :label="action.key"
          >
            {{ action.label }}
          </el-radio-button>
        </el-radio-group>
        <el-input-number
          v-model="marginAmount"
          :min="0"
          :precision="2"
          :step="10"
          size="small"
          controls-position="right"
          class="margin-amount-input"
        />
        <el-button
          size="small"
          type="primary"
          :loading="marginOperating"
          @click="runMarginAction"
        >
          执行
        </el-button>
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

.metric-divider {
  border-top: 1px solid var(--app-border);
  margin: 8px 0 4px;
}

.pnl-positive {
  color: #67c23a !important;
}

.pnl-negative {
  color: #f56c6c !important;
}

.risk-ok {
  color: #67c23a !important;
}

.risk-warning {
  color: #e6a23c !important;
}

.risk-danger {
  color: #f56c6c !important;
}

.margin-action-panel {
  border: 1px solid var(--app-border);
  background: var(--app-surface);
  border-radius: 6px;
  padding: 12px;
}

.margin-state {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.margin-action-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.margin-action-selector {
  display: flex;
  flex-wrap: wrap;
}

.margin-amount-input {
  width: 160px;
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
  .metric-selector,
  .margin-action-selector {
    justify-content: flex-start;
  }

  .margin-action-row {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
