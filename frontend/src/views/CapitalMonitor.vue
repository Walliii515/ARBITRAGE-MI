<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { ElMessageBox } from 'element-plus'
import { QuestionFilled } from '@element-plus/icons-vue'
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
  gate_cross_risk_status?: string | null
  gate_cross_risk_status_label?: string | null
  gate_cross_position_count?: number | null
  gate_cross_mmr_pct?: number | null
  gate_cross_available_ratio_pct?: number | null
  gate_cross_margin_usage_pct?: number | null
  gate_cross_initial_margin_usdt?: number | null
  gate_cross_maintenance_margin_usdt?: number | null
  gate_cross_nearest_liq_contract?: string | null
  gate_cross_nearest_liq_distance_bps?: number | null
  gate_cross_health_status?: string | null
  gate_cross_health_label?: string | null
  gate_cross_observed_status?: string | null
  gate_cross_source?: string | null
  gate_cross_error?: string | null
  gate_cross_fetched_at?: string | null
  gate_cross_account_age_sec?: number | null
  gate_cross_positions_age_sec?: number | null
  gate_cross_account_latency_ms?: number | null
  gate_cross_positions_latency_ms?: number | null
  gate_cross_latency_ms?: number | null
}

interface GateCrossRiskSnapshot {
  status?: string | null
  status_label?: string | null
  position_count?: number | null
  account_mmr_pct?: number | null
  available_ratio_pct?: number | null
  margin_usage_pct?: number | null
  initial_margin_usdt?: number | null
  maintenance_margin_usdt?: number | null
  nearest_liq_contract?: string | null
  nearest_liq_distance_bps?: number | null
  health_status?: string | null
  health_label?: string | null
  observed_status?: string | null
  source?: string | null
  error?: string | null
  fetched_at?: string | null
  account_age_sec?: number | null
  positions_age_sec?: number | null
  max_age_sec?: number | null
  account_latency_ms?: number | null
  positions_latency_ms?: number | null
  latency_ms?: number | null
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
  | 'daily_realized_pnl_usdt'
  | 'daily_return_pct'
  | 'gate_cross_mmr_pct'
  | 'gate_cross_available_ratio_pct'
  | 'gate_cross_margin_usage_pct'
  | 'gate_cross_nearest_liq_distance_bps'
type ChartModeKey =
  | 'equity_usdt'
  | 'unrealized_pnl_usdt'
  | 'realized_breakdown'
  | 'gross_total_pnl_usdt'
  | 'daily_return'
  | 'gate_cross_risk'
type ChartSeriesGroup = 'asset' | 'pnl' | 'ratio' | 'bps'
type ChartMetricOption = { key: ChartMetric; label: string; group: ChartSeriesGroup; color: string }
type ChartModeOption = { key: ChartModeKey; label: string }
type OpenRiskConfig = {
  min_available_ratio?: number
  min_binance_available_ratio?: number
  min_gate_available_ratio?: number
}
type ChartLineType = 'solid' | 'dashed'
type ChartSeriesType = 'line' | 'bar'
type ChartSeries = {
  exchange: ExchangeKey
  metric: ChartMetric
  label: string
  color: string
  group: ChartSeriesGroup
  points: Array<{ time: string; value: number }>
  lineType: ChartLineType
  seriesType: ChartSeriesType
}

const latestRows = ref<CapitalRow[]>([])
const historyRows = ref<CapitalRow[]>([])
const liveGateRisk = ref<GateCrossRiskSnapshot | null>(null)
const liveGateRiskRequestError = ref('')
const loading = ref(false)
const running = ref(false)
const selectedWindow = ref<TimeWindowKey>('7d')
const selectedChartMode = ref<ChartModeKey>('equity_usdt')
const selectedExchange = ref<ExchangeKey>('total')
const selectedInterval = ref<HistoryInterval>('10m')
const showSummaryDetails = ref(false)
const bnbBuying = ref(false)
const clearDialogVisible = ref(false)
const clearingRange = ref(false)
const clearRange = ref<[string, string] | null>(null)
const clearDefaultTime = [
  new Date(2000, 0, 1, 0, 0, 0),
  new Date(2000, 0, 1, 23, 59, 59),
]
const openRiskConfig = ref<OpenRiskConfig>({
  min_available_ratio: 0.10,
  min_binance_available_ratio: 0.02,
  min_gate_available_ratio: 0.15,
})
const chartRef = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let gateRiskTimer: ReturnType<typeof setInterval> | null = null

const metricOptions: ChartMetricOption[] = [
  { key: 'equity_usdt', label: '总资产', group: 'asset', color: '#67c23a' },
  { key: 'unrealized_pnl_usdt', label: '未实现盈亏', group: 'pnl', color: '#e6a23c' },
  { key: 'realized_pnl_usdt', label: '平仓盈亏', group: 'pnl', color: '#9b59b6' },
  { key: 'funding_pnl_usdt', label: '资金费收益', group: 'pnl', color: '#00a870' },
  { key: 'total_pnl_usdt', label: '净已实现收益', group: 'pnl', color: '#409eff' },
  { key: 'gross_total_pnl_usdt', label: '总盈亏', group: 'pnl', color: '#f56c6c' },
  { key: 'daily_realized_pnl_usdt', label: '每日净已实现收益', group: 'pnl', color: '#409eff' },
  { key: 'daily_return_pct', label: '每日收益率', group: 'ratio', color: '#14b8a6' },
  { key: 'gate_cross_mmr_pct', label: '全仓MMR', group: 'ratio', color: '#67c23a' },
  { key: 'gate_cross_available_ratio_pct', label: '可用率', group: 'ratio', color: '#1677ff' },
  { key: 'gate_cross_margin_usage_pct', label: '占用率', group: 'ratio', color: '#e6a23c' },
  { key: 'gate_cross_nearest_liq_distance_bps', label: '强平距离', group: 'bps', color: '#f56c6c' },
]

const chartModeOptions: ChartModeOption[] = [
  { key: 'equity_usdt', label: '总资产' },
  { key: 'unrealized_pnl_usdt', label: '未实现盈亏' },
  { key: 'realized_breakdown', label: '收益趋势' },
  { key: 'gross_total_pnl_usdt', label: '总盈亏' },
  { key: 'daily_return', label: '每日收益' },
  { key: 'gate_cross_risk', label: '全仓风险' },
]

const realizedBreakdownMetrics: ChartMetric[] = [
  'realized_pnl_usdt',
  'funding_pnl_usdt',
  'total_pnl_usdt',
]

const gateCrossRiskMetrics: ChartMetric[] = [
  'gate_cross_mmr_pct',
  'gate_cross_available_ratio_pct',
  'gate_cross_margin_usage_pct',
  'gate_cross_nearest_liq_distance_bps',
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

const gateCrossRiskRow = computed(() => latestByExchange.value.gate)
const gateCrossRisk = computed<GateCrossRiskSnapshot>(() => {
  if (liveGateRisk.value) return liveGateRisk.value
  const row = gateCrossRiskRow.value
  return {
    status: row?.gate_cross_risk_status,
    status_label: row?.gate_cross_risk_status_label,
    position_count: row?.gate_cross_position_count,
    account_mmr_pct: row?.gate_cross_mmr_pct,
    available_ratio_pct: row?.gate_cross_available_ratio_pct,
    margin_usage_pct: row?.gate_cross_margin_usage_pct,
    initial_margin_usdt: row?.gate_cross_initial_margin_usdt,
    maintenance_margin_usdt: row?.gate_cross_maintenance_margin_usdt,
    nearest_liq_contract: row?.gate_cross_nearest_liq_contract,
    nearest_liq_distance_bps: row?.gate_cross_nearest_liq_distance_bps,
    health_status: row?.gate_cross_health_status,
    health_label: row?.gate_cross_health_label,
    observed_status: row?.gate_cross_observed_status,
    source: row?.gate_cross_source,
    error: row?.gate_cross_error,
    fetched_at: row?.gate_cross_fetched_at,
    account_age_sec: row?.gate_cross_account_age_sec,
    positions_age_sec: row?.gate_cross_positions_age_sec,
    account_latency_ms: row?.gate_cross_account_latency_ms,
    positions_latency_ms: row?.gate_cross_positions_latency_ms,
    latency_ms: row?.gate_cross_latency_ms,
  }
})
const gateRiskHealthStatus = computed(() => (
  liveGateRiskRequestError.value ? 'unavailable' : gateCrossRisk.value.health_status
))
const gateRiskHealthError = computed(() => (
  liveGateRiskRequestError.value || gateCrossRisk.value.error || ''
))
const chartExchange = computed<ExchangeKey>(() => (
  selectedChartMode.value === 'gate_cross_risk' ? 'gate' : selectedExchange.value
))

const chartSeries = computed<ChartSeries[]>(() => {
  const rows = historyRows.value
    .filter((row) => row.exchange === chartExchange.value)
  if (selectedChartMode.value === 'gate_cross_risk') {
    return gateCrossRiskMetrics.map((metric): ChartSeries => {
      const option = metricOptions.find((item) => item.key === metric)!
      return {
        exchange: 'gate',
        metric,
        label: `Gate ${option.label}`,
        color: option.color,
        group: option.group,
        points: rows.map((row) => ({
          time: row.snapshot_at,
          value: chartMetricValue(row, metric),
        })),
        lineType: 'solid' as const,
        seriesType: 'line' as const,
      }
    })
  }
  if (selectedChartMode.value === 'daily_return') {
    return buildDailyReturnSeries(rows)
  }

  const metrics: ChartMetric[] = selectedChartMode.value === 'realized_breakdown'
    ? realizedBreakdownMetrics
    : [selectedChartMode.value as ChartMetric]
  const latestEquity = latestByExchange.value[chartExchange.value]?.equity_usdt
    ?? rows[rows.length - 1]?.equity_usdt
  const series: ChartSeries[] = metrics.map((metric): ChartSeries => {
    const option = metricOptions.find((item) => item.key === metric)!
    const points = rows.map((row) => ({
      time: row.snapshot_at,
      value: chartMetricValue(row, metric, latestEquity),
    }))
    return {
      exchange: chartExchange.value,
      metric,
      label: `${exchangeLabel(chartExchange.value)} ${option.label}`,
      color: option.color,
      group: option.group,
      points,
      lineType: 'solid' as const,
      seriesType: 'line' as const,
    }
  })
  if (selectedChartMode.value === 'gross_total_pnl_usdt' && series[0]?.points.length > 1) {
    series.push({
      exchange: chartExchange.value,
      metric: 'gross_total_pnl_usdt',
      label: `${exchangeLabel(chartExchange.value)} 总盈亏趋势`,
      color: '#ff8f1f',
      group: 'pnl',
      points: buildTrendPoints(series[0].points),
      lineType: 'dashed' as const,
      seriesType: 'line' as const,
    })
  }
  return series
})

function buildDailyReturnSeries(rows: CapitalRow[]): ChartSeries[] {
  const sortedRows = rows
    .slice()
    .sort((a, b) => new Date(a.snapshot_at).getTime() - new Date(b.snapshot_at).getTime())
  const buckets = new Map<string, CapitalRow[]>()
  for (const row of sortedRows) {
    const dayKey = dayStartKey(row.snapshot_at)
    if (!dayKey) continue
    const items = buckets.get(dayKey) || []
    items.push(row)
    buckets.set(dayKey, items)
  }

  const profitPoints: Array<{ time: string; value: number }> = []
  const returnPoints: Array<{ time: string; value: number }> = []
  for (const [dayKey, items] of buckets) {
    const first = items[0]
    const last = items[items.length - 1]
    const firstPnl = Number(first?.total_pnl_usdt ?? NaN)
    const lastPnl = Number(last?.total_pnl_usdt ?? NaN)
    const baseEquity = Number(first?.equity_usdt ?? NaN)
    if (!Number.isFinite(firstPnl) || !Number.isFinite(lastPnl)) continue
    const dailyPnl = lastPnl - firstPnl
    profitPoints.push({ time: dayKey, value: dailyPnl })
    returnPoints.push({
      time: dayKey,
      value: Number.isFinite(baseEquity) && Math.abs(baseEquity) > 1e-9
        ? (dailyPnl / baseEquity) * 100
        : 0,
    })
  }

  return [
    {
      exchange: chartExchange.value,
      metric: 'daily_realized_pnl_usdt',
      label: `${exchangeLabel(chartExchange.value)} 每日净已实现收益`,
      color: '#409eff',
      group: 'pnl',
      points: profitPoints,
      lineType: 'solid',
      seriesType: 'bar',
    },
    {
      exchange: chartExchange.value,
      metric: 'daily_return_pct',
      label: `${exchangeLabel(chartExchange.value)} 每日收益率`,
      color: '#14b8a6',
      group: 'ratio',
      points: returnPoints,
      lineType: 'solid',
      seriesType: 'line',
    },
  ]
}

function dayStartKey(value: string): string | null {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return null
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day} 00:00:00`
}

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

function formatBps(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return `${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} bps`
}

function hasAmount(value: number | null | undefined): boolean {
  return value != null && Number.isFinite(Number(value))
}

function chartMetricValue(row: CapitalRow, metric: ChartMetric, latestEquity?: number | null): number {
  if (metric === 'daily_return_pct') {
    const equity = Number(latestEquity ?? row.equity_usdt ?? NaN)
    const netRealized = Number(row.total_pnl_usdt ?? NaN)
    if (!Number.isFinite(equity) || Math.abs(equity) <= 1e-9 || !Number.isFinite(netRealized)) return 0
    return (netRealized / equity) * 100
  }
  if (metric === 'daily_realized_pnl_usdt') return Number(row.total_pnl_usdt ?? 0)
  return Number(row[metric] ?? 0)
}

function formatSeriesValue(value: number, group: ChartSeriesGroup): string {
  if (group === 'bps') return formatBps(value)
  return group === 'ratio' ? formatPercent(value) : `${formatAmount(value)} USDT`
}

function chartYAxisIndex(group: ChartSeriesGroup): number {
  if (group === 'asset') return 0
  if (group === 'ratio') return 2
  if (group === 'bps') return 3
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

function availableRatio(row: CapitalRow | undefined): number | null {
  const available = Number(row?.available_usdt ?? NaN)
  const equity = Number(row?.equity_usdt ?? NaN)
  if (!Number.isFinite(available) || !Number.isFinite(equity) || equity <= 0) return null
  return (available / equity) * 100
}

function minAvailableRatio(exchange: string): number | null {
  if (exchange === 'binance') {
    return Number(openRiskConfig.value.min_binance_available_ratio ?? openRiskConfig.value.min_available_ratio ?? 0) * 100
  }
  if (exchange === 'gate') {
    return Number(openRiskConfig.value.min_gate_available_ratio ?? openRiskConfig.value.min_available_ratio ?? 0) * 100
  }
  return null
}

function formatAvailableRatio(exchange: string, row: CapitalRow | undefined): string {
  const ratio = availableRatio(row)
  if (ratio == null) return '-'
  const minRatio = minAvailableRatio(exchange)
  const ratioText = formatPercent(ratio)
  return minRatio == null ? ratioText : `${ratioText} / ${formatPercent(minRatio)}`
}

function availableStatusClass(exchange: string, row: CapitalRow | undefined): string {
  const ratio = availableRatio(row)
  const minRatio = minAvailableRatio(exchange)
  if (ratio == null || minRatio == null || minRatio <= 0) return 'available-ok'
  if (ratio < minRatio) return 'available-danger'
  if (ratio < minRatio * 1.2) return 'available-warning'
  return 'available-ok'
}

function availableHelpText(exchange: string): string {
  if (exchange === 'binance') {
    return `Binance 开仓后可用资金最低保留 ${formatPercent(minAvailableRatio(exchange))}，主要用于手续费、BNB不足和现货腿兜底。`
  }
  if (exchange === 'gate') {
    return [
      `开仓预留: Gate 每笔正向开仓前都会检查“本次下单后可用资金 / Gate净值”是否仍 >= ${formatPercent(minAvailableRatio(exchange))}。低于该值时只拦截新的正向开仓，不会自动切换左侧“暂停正向开仓”，已有持仓和平仓逻辑不受影响。`,
      '系统主动平仓: 持仓监控会刷新 Gate 全仓 MMR 和强平价。MMR <= 300% 或正向空头距强平价 <= 300bps 时进入危险路径，系统按保证金风控全量市价平仓；若已低于 200% 也会作为兜底保证金风控触发。',
      '交易所强平/ADL: 以 Gate 返回的 liq_price 和交易所风控为准。页面在“Gate 全仓风险”里看全仓MMR和最近强平距离；发生交易所强平/ADL 后，Gate风险WS/持仓对账会写入铃铛和交易所风险。'
    ].join('\n\n')
  }
  return '合计可用资金仅用于观察，不参与单交易所开仓预留风控。'
}

function availableHelpWidth(exchange: string): number {
  return exchange === 'gate' ? 520 : 260
}

function gateRiskStatusClass(status: string | null | undefined): string {
  if (status === 'danger') return 'risk-danger'
  if (status === 'warning') return 'risk-warning'
  if (status === 'unknown') return 'risk-warning'
  if (status === 'safe') return 'risk-safe'
  return 'risk-idle'
}

function gateRiskStatusLabel(risk: GateCrossRiskSnapshot): string {
  return risk.status_label || (
    risk.status === 'danger' ? '危险'
      : risk.status === 'warning' ? '预警'
        : risk.status === 'safe' ? '安全'
          : risk.status === 'idle' ? '无持仓'
            : risk.status === 'unknown' ? '未知'
            : '未采集'
  )
}

function gateRiskHealthClass(status: string | null | undefined): string {
  if (status === 'healthy') return 'risk-safe'
  if (status === 'degraded' || status === 'stale') return 'risk-warning'
  if (status === 'unavailable') return 'risk-danger'
  return 'risk-idle'
}

function gateRiskHealthLabel(): string {
  if (liveGateRiskRequestError.value) return '接口异常'
  return gateCrossRisk.value.health_label || (
    gateCrossRisk.value.health_status === 'healthy' ? '正常'
      : gateCrossRisk.value.health_status === 'degraded' ? '部分异常'
        : gateCrossRisk.value.health_status === 'stale' ? '数据陈旧'
          : gateCrossRisk.value.health_status === 'unavailable' ? '不可用'
            : '未采集'
  )
}

function formatAge(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  const seconds = Math.max(Number(value), 0)
  return seconds < 10 ? `${seconds.toFixed(1)}s` : `${seconds.toFixed(0)}s`
}

function formatLatency(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return `${Number(value).toFixed(0)}ms`
}

function formatRiskSource(value: string | null | undefined): string {
  if (value === 'gate_account_api') return 'Gate REST'
  if (value === 'gate_cross_risk_loop') return '风险采集器'
  if (value === 'gate_cross_risk_monitor') return '风险监控器'
  return value || '-'
}

function formatRiskContract(contract: string | null | undefined, fallback = '-'): string {
  if (!contract || contract === 'null') return fallback
  return contract.replace(/_USDT$/i, '')
}

function formatTooltipTime(value: unknown): string {
  const rawValue = Array.isArray(value) ? value[0] : value
  const numericValue = typeof rawValue === 'number'
    ? rawValue
    : typeof rawValue === 'string' && rawValue.trim() !== ''
      ? Number(rawValue)
      : NaN
  const date = Number.isFinite(numericValue)
    ? new Date(numericValue)
    : new Date(String(rawValue))
  if (!Number.isFinite(date.getTime())) return String(value ?? '-')
  if (selectedChartMode.value === 'daily_return') {
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    })
  }
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
    grid: { top: 44, right: 164, bottom: 56, left: 72 },
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
      {
        type: 'value',
        name: 'bps',
        scale: true,
        position: 'right',
        offset: 108,
        axisLabel: {
          color: mutedColor,
          formatter: (value: number) => `${Number(value).toFixed(0)} bps`,
        },
        splitLine: { show: false },
      },
    ],
    series: chartSeries.value.map((series) => ({
      name: series.label,
      type: series.seriesType,
      smooth: series.seriesType === 'line',
      showSymbol: series.seriesType === 'line' && selectedChartMode.value === 'daily_return',
      symbolSize: 7,
      yAxisIndex: chartYAxisIndex(series.group),
      emphasis: { focus: 'series' },
      data: series.points.map((point) => [point.time, point.value]),
      lineStyle: { width: series.lineType === 'dashed' ? 2 : 2.2, type: series.lineType },
      barMaxWidth: series.seriesType === 'bar' ? 34 : undefined,
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

async function fetchLiveGateRisk() {
  try {
    const res = await get('/api/trading/capital/gate-cross-risk/live', { silent: true })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    if (!data?.risk || typeof data.risk !== 'object') {
      throw new Error('实时风险响应缺少risk字段')
    }
    liveGateRisk.value = data.risk
    liveGateRiskRequestError.value = ''
  } catch (e: any) {
    liveGateRiskRequestError.value = e?.message || '实时风险接口不可用'
  }
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

async function fetchOpenRiskConfig() {
  try {
    const res = await get('/api/trading/open/status')
    const data = await res.json()
    openRiskConfig.value = {
      min_available_ratio: Number(data.min_available_ratio ?? openRiskConfig.value.min_available_ratio),
      min_binance_available_ratio: Number(
        data.min_binance_available_ratio ?? data.min_available_ratio ?? openRiskConfig.value.min_binance_available_ratio
      ),
      min_gate_available_ratio: Number(
        data.min_gate_available_ratio ?? data.min_available_ratio ?? openRiskConfig.value.min_gate_available_ratio
      ),
    }
  } catch {
    // 页面展示使用默认阈值兜底；交易风控仍以后端配置为准。
  }
}

async function fetchHistory() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    const window = timeWindowOptions.find((item) => item.key === selectedWindow.value)
      || timeWindowOptions.find((item) => item.key === '7d')!
    if (selectedChartMode.value === 'daily_return' && window.hours != null) params.set('days', '1')
    else if (window.hours != null) params.set('hours', String(window.hours))
    else params.set('days', String(window.days || 7))
    params.set('exchange', chartExchange.value)
    params.set('interval', dailyHistoryInterval())
    const historyRes = await get(`/api/trading/capital/history?${params.toString()}`)
    const history = await historyRes.json()
    historyRows.value = history.rows || []
  } catch (e: any) {
    showError(e?.message || '获取资金曲线失败')
  } finally {
    loading.value = false
  }
}

function dailyHistoryInterval(): HistoryInterval {
  if (selectedChartMode.value !== 'daily_return') return selectedInterval.value
  return selectedWindow.value === '30d' || selectedWindow.value === '90d' ? '1h' : '10m'
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

function formatLocalDateTime(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  const second = String(date.getSeconds()).padStart(2, '0')
  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

function parseLocalDateTime(value: string): number {
  return new Date(value.replace(' ', 'T')).getTime()
}

function openClearDialog() {
  const end = new Date()
  const start = new Date(end.getTime() - 60 * 60 * 1000)
  clearRange.value = [formatLocalDateTime(start), formatLocalDateTime(end)]
  clearDialogVisible.value = true
}

async function clearCapitalRange() {
  const range = clearRange.value
  if (!range || range.length !== 2 || !range[0] || !range[1]) {
    showError('请选择清理时间段')
    return
  }
  const [startAt, endAt] = range
  if (parseLocalDateTime(startAt) > parseLocalDateTime(endAt)) {
    showError('开始时间不能晚于结束时间')
    return
  }

  try {
    await ElMessageBox.confirm(
      `确认清理 ${startAt} 到 ${endAt} 的资金监控数据？`,
      '清理资金监控数据',
      {
        confirmButtonText: '确认清理',
        cancelButtonText: '取消',
        type: 'warning',
      }
    )
  } catch {
    return
  }

  clearingRange.value = true
  try {
    const res = await post('/api/trading/capital/clear-range', {
      start_at: startAt,
      end_at: endAt,
    })
    const data = await res.json()
    if (data.success) {
      const backup = data.backup_table ? `，备份表 ${data.backup_table}` : ''
      showSuccess(`${data.message || '清理完成'}${backup}`)
      clearDialogVisible.value = false
      await fetchCapital()
    } else {
      showError(data.message || '清理失败')
    }
  } catch (e: any) {
    if (e?.message && !e.message.includes('未授权') && !e.message.includes('权限不足')) {
      showError(`清理请求失败: ${e.message}`)
    }
  } finally {
    clearingRange.value = false
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
  await fetchOpenRiskConfig()
  await fetchLiveGateRisk()
  await fetchCapital()
  await initChart()
  gateRiskTimer = setInterval(fetchLiveGateRisk, 2000)
})

watch([historyRows, selectedChartMode, selectedExchange], () => {
  updateChart()
})

watch([selectedExchange, selectedInterval], () => {
  fetchHistory()
})

watch(selectedChartMode, () => {
  fetchHistory()
})

onBeforeUnmount(() => {
  if (gateRiskTimer) clearInterval(gateRiskTimer)
  gateRiskTimer = null
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
      <el-button size="small" type="danger" plain @click="openClearDialog">清理时间段</el-button>
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
          <span class="metric-label-with-help">
            <span>可用资金</span>
            <el-popover trigger="click" placement="top" :width="availableHelpWidth(exchange)">
              <template #reference>
                <el-button class="help-icon-button" text circle size="small" aria-label="可用资金提醒逻辑">
                  <el-icon><QuestionFilled /></el-icon>
                </el-button>
              </template>
              <div class="available-help">{{ availableHelpText(exchange) }}</div>
            </el-popover>
          </span>
          <strong class="available-value" :class="availableStatusClass(exchange, latestByExchange[exchange])">
            <span>{{ formatAmount(latestByExchange[exchange]?.available_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.available_usdt)" class="metric-unit">USDT</span>
            <span
              v-if="hasAmount(latestByExchange[exchange]?.available_usdt)"
              class="available-ratio"
            >
              {{ formatAvailableRatio(exchange, latestByExchange[exchange]) }}
            </span>
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
          <span>总盈亏</span>
          <strong :class="Number(latestByExchange[exchange]?.gross_total_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
            <span>{{ formatAmount(latestByExchange[exchange]?.gross_total_pnl_usdt) }}</span>
            <span v-if="hasAmount(latestByExchange[exchange]?.gross_total_pnl_usdt)" class="metric-unit">USDT</span>
          </strong>
        </div>
      </div>
    </div>

    <div class="gate-risk-panel">
      <div class="gate-risk-header">
        <div class="gate-risk-heading">
          <span class="gate-risk-title">Gate 全仓风险</span>
          <span class="gate-risk-subtitle">{{ gateCrossRisk.fetched_at || '等待实时采集' }}</span>
        </div>
        <div class="gate-risk-badges">
          <span class="risk-badge" :class="gateRiskHealthClass(gateRiskHealthStatus)">
            {{ gateRiskHealthLabel() }}
          </span>
          <span class="risk-badge" :class="gateRiskStatusClass(gateCrossRisk.status)">
            {{ gateRiskStatusLabel(gateCrossRisk) }}
          </span>
        </div>
      </div>
      <div class="gate-risk-grid">
        <div class="risk-metric">
          <span>全仓MMR</span>
          <strong :class="gateRiskStatusClass(gateCrossRisk.status)">
            {{ formatPercent(gateCrossRisk.account_mmr_pct) }}
          </strong>
        </div>
        <div class="risk-metric">
          <span>可用率</span>
          <strong>{{ formatPercent(gateCrossRisk.available_ratio_pct) }}</strong>
        </div>
        <div class="risk-metric">
          <span>占用率</span>
          <strong>{{ formatPercent(gateCrossRisk.margin_usage_pct) }}</strong>
        </div>
        <div class="risk-metric">
          <span>维持保证金</span>
          <strong>
            {{ formatAmount(gateCrossRisk.maintenance_margin_usdt) }}
            <small v-if="hasAmount(gateCrossRisk.maintenance_margin_usdt)">USDT</small>
          </strong>
        </div>
        <div class="risk-metric">
          <span>初始保证金</span>
          <strong>
            {{ formatAmount(gateCrossRisk.initial_margin_usdt) }}
            <small v-if="hasAmount(gateCrossRisk.initial_margin_usdt)">USDT</small>
          </strong>
        </div>
        <div class="risk-metric">
          <span>最近强平距离</span>
          <strong>{{ formatBps(gateCrossRisk.nearest_liq_distance_bps) }}</strong>
          <em>{{ formatRiskContract(gateCrossRisk.nearest_liq_contract) }}</em>
        </div>
        <div class="risk-metric">
          <span>Gate持仓数</span>
          <strong>{{ gateCrossRisk.position_count ?? '-' }}</strong>
        </div>
        <div class="risk-metric">
          <span>数据健康</span>
          <strong :class="gateRiskHealthClass(gateRiskHealthStatus)">{{ gateRiskHealthLabel() }}</strong>
        </div>
        <div class="risk-metric">
          <span>账户数据年龄</span>
          <strong>{{ formatAge(gateCrossRisk.account_age_sec) }}</strong>
          <em>上限 {{ formatAge(gateCrossRisk.max_age_sec) }}</em>
        </div>
        <div class="risk-metric">
          <span>持仓数据年龄</span>
          <strong>{{ formatAge(gateCrossRisk.positions_age_sec) }}</strong>
        </div>
        <div class="risk-metric">
          <span>采集耗时</span>
          <strong>{{ formatLatency(gateCrossRisk.latency_ms) }}</strong>
          <em>账户 {{ formatLatency(gateCrossRisk.account_latency_ms) }} / 持仓 {{ formatLatency(gateCrossRisk.positions_latency_ms) }}</em>
        </div>
        <div class="risk-metric">
          <span>数据源</span>
          <strong>{{ formatRiskSource(gateCrossRisk.source) }}</strong>
        </div>
      </div>
      <div v-if="gateRiskHealthError" class="risk-health-error">
        {{ gateRiskHealthError }}
      </div>
    </div>

    <div class="chart-panel">
      <div class="chart-header">
        <span>{{ selectedChartMode === 'gate_cross_risk' ? 'Gate 全仓风险趋势' : '资金趋势' }}</span>
        <el-radio-group
          v-model="selectedExchange"
          size="small"
          class="exchange-selector"
          :disabled="selectedChartMode === 'gate_cross_risk'"
        >
          <el-radio-button label="binance">binance</el-radio-button>
          <el-radio-button label="gate">gate</el-radio-button>
          <el-radio-button label="total">合计</el-radio-button>
        </el-radio-group>
      </div>
      <div class="metric-selector-row">
        <el-radio-group
          v-if="selectedChartMode !== 'daily_return'"
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

    <el-dialog
      v-model="clearDialogVisible"
      title="清理资金监控数据"
      width="460px"
      append-to-body
    >
      <div class="clear-range-dialog">
        <el-date-picker
          v-model="clearRange"
          type="datetimerange"
          format="YYYY-MM-DD HH:mm:ss"
          value-format="YYYY-MM-DD HH:mm:ss"
          :default-time="clearDefaultTime"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          range-separator="至"
          class="clear-range-picker"
        />
      </div>
      <template #footer>
        <el-button size="small" @click="clearDialogVisible = false">取消</el-button>
        <el-button
          size="small"
          type="danger"
          :loading="clearingRange"
          @click="clearCapitalRange"
        >
          确认清理
        </el-button>
      </template>
    </el-dialog>
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

.gate-risk-panel {
  border: 1px solid var(--app-border);
  background: var(--app-surface);
  border-radius: 6px;
  padding: 12px 14px;
}

.gate-risk-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.gate-risk-title {
  color: var(--app-text);
  font-size: 15px;
  font-weight: 700;
}

.gate-risk-heading {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}

.gate-risk-subtitle {
  color: var(--app-text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.gate-risk-badges {
  display: flex;
  align-items: center;
  gap: 6px;
}

.risk-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 58px;
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  font-size: 12px;
  font-weight: 700;
}

.risk-safe {
  color: #67c23a !important;
  border-color: color-mix(in srgb, #67c23a 45%, var(--app-border));
}

.risk-warning {
  color: #e6a23c !important;
  border-color: color-mix(in srgb, #e6a23c 50%, var(--app-border));
}

.risk-danger {
  color: #f56c6c !important;
  border-color: color-mix(in srgb, #f56c6c 55%, var(--app-border));
}

.risk-idle {
  color: var(--app-text-muted) !important;
}

.gate-risk-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(160px, 1fr));
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  background: var(--app-border);
}

.risk-metric {
  display: grid;
  grid-template-columns: minmax(72px, auto) minmax(0, 1fr);
  align-items: baseline;
  column-gap: 10px;
  row-gap: 2px;
  min-height: 44px;
  padding: 8px 10px;
  background: var(--app-surface);
  color: var(--app-text-muted);
  font-size: 12px;
}

.risk-metric strong {
  min-width: 0;
  color: var(--app-text);
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}

.risk-metric small {
  margin-left: 2px;
  color: var(--app-text-muted);
  font-size: 10px;
  font-weight: 500;
}

.risk-metric em {
  grid-column: 2;
  color: var(--app-text-muted);
  font-size: 11px;
  font-style: normal;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.risk-health-error {
  margin-top: 8px;
  border-left: 3px solid #f56c6c;
  padding: 6px 9px;
  background: color-mix(in srgb, #f56c6c 8%, var(--app-surface));
  color: #f56c6c;
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
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

.metric-label-with-help {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 72px;
}

.help-icon-button {
  width: 18px;
  height: 18px;
  min-height: 18px;
  padding: 0;
  color: var(--app-text-muted);
}

.available-help {
  color: var(--app-text);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-line;
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
  max-width: 72%;
}

.available-ratio {
  width: 100%;
  color: var(--app-text-muted);
  font-size: 11px;
  font-weight: 600;
  line-height: 1.1;
}

.available-value.available-ok {
  color: #1677ff !important;
}

.available-value.available-warning {
  color: #e6a23c !important;
}

.available-value.available-danger {
  color: #f56c6c !important;
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

.clear-range-dialog {
  display: flex;
  width: 100%;
}

.clear-range-picker {
  width: 100%;
}

@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }

  .gate-risk-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .gate-risk-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }

  .gate-risk-grid {
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
