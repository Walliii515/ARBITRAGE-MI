<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
  account_balance_usdt?: number | null
  account_unrealized_pnl_usdt?: number | null
  bnb_available?: number | null
  bnb_available_usdt?: number | null
  gate_cross_risk_status?: string | null
  gate_cross_risk_status_label?: string | null
  gate_cross_mmr_pct?: number | null
  gate_cross_available_ratio_pct?: number | null
  gate_cross_margin_usage_pct?: number | null
  gate_cross_initial_margin_usdt?: number | null
  gate_cross_maintenance_margin_usdt?: number | null
  gate_cross_nearest_liq_contract?: string | null
  gate_cross_nearest_liq_distance_bps?: number | null
  gate_cross_error?: string | null
  gate_cross_fetched_at?: string | null
}

interface GateCrossRiskSnapshot {
  status?: string | null
  status_label?: string | null
  account_mmr_pct?: number | null
  available_ratio_pct?: number | null
  margin_usage_pct?: number | null
  initial_margin_usdt?: number | null
  maintenance_margin_usdt?: number | null
  nearest_liq_contract?: string | null
  nearest_liq_distance_bps?: number | null
  priority_close_contract?: string | null
  priority_close_reason?: string | null
  error?: string | null
  fetched_at?: string | null
}

interface GateCrossRiskMinimum {
  account_mmr_pct?: number | null
  snapshot_at?: string | null
  primary_risk_contract?: string | null
  primary_risk_asset?: string | null
  primary_risk_pressure_usdt?: number | null
  maintenance_margin_usdt?: number | null
  unrealized_pnl_usdt?: number | null
  liq_distance_bps?: number | null
  attribution?: string | null
}

interface GateCrossRiskSummary {
  period_days: number
  minimum?: GateCrossRiskMinimum | null
}

interface AnnualizedReturnSummary {
  period_days: number
  available_days: number
  sufficient_data: boolean
  annualized_return_pct?: number | null
  period_return_pct?: number | null
  period_pnl_usdt?: number | null
  average_equity_usdt?: number | null
  start_date?: string | null
  end_date?: string | null
}

interface FundTransferTask {
  id: number
  status: string
  step: string
  status_message?: string | null
  coin: string
  network: string
  destination_masked: string
  requested_amount: number
  expected_fee: number
  withdraw_amount: number
  received_amount?: number | null
  binance_transfer_id?: string | null
  binance_withdraw_id?: string | null
  binance_tx_id?: string | null
  gate_deposit_id?: string | null
  gate_transfer_id?: string | null
  attention_required?: boolean | number
  last_error?: string | null
  created_at: string
  updated_at?: string | null
  completed_at?: string | null
}

interface FundTransferPreview {
  coin: string
  network: string
  destination_masked: string
  requested_amount: number
  fee: number
  received_amount: number
  minimum_received_amount: number
  binance_forward_free: number
  minimum_transfer_amount: number
  maximum_transfer_amount: number
}

interface FundTransferLimits {
  coin: string
  network: string
  destination_masked: string
  fee: number
  minimum_received_amount: number
  binance_forward_free: number
  minimum_transfer_amount: number
  maximum_transfer_amount: number
}

type ExchangeKey = 'binance' | 'gate' | 'total'
type TimeWindowKey = '1h' | '3h' | '6h' | '12h' | '1d' | '7d' | '30d' | '90d'
type AnnualizedPeriod = 7 | 30 | 90 | 180 | 365
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
const gateRiskSummary = ref<GateCrossRiskSummary | null>(null)
const gateRiskSummaryRequestError = ref('')
const annualizedReturn = ref<AnnualizedReturnSummary | null>(null)
const annualizedReturnRequestError = ref('')
const annualizedReturnLoading = ref(false)
const selectedAnnualizedPeriod = ref<AnnualizedPeriod>(7)
const loading = ref(false)
const historyLoading = ref(false)
const chartActivated = ref(false)
const running = ref(false)
const selectedWindow = ref<TimeWindowKey>('7d')
const selectedChartMode = ref<ChartModeKey>('equity_usdt')
const selectedExchange = ref<ExchangeKey>('total')
const showSummaryDetails = ref(false)
const bnbBuying = ref(false)
const clearDialogVisible = ref(false)
const clearingRange = ref(false)
const clearRange = ref<[string, string] | null>(null)
const fundTransferDialogVisible = ref(false)
const fundTransferAmount = ref<string>('10')
const fundTransferPassword = ref('')
const fundTransferPreflight = ref<FundTransferPreview | null>(null)
const fundTransferLimits = ref<FundTransferLimits | null>(null)
const fundTransferLimitsLoading = ref(false)
const fundTransferLimitsError = ref('')
const fundTransferPreflighting = ref(false)
const fundTransferCreating = ref(false)
const fundTransferRetrying = ref(false)
const activeFundTransfer = ref<FundTransferTask | null>(null)
const fundTransferHistory = ref<FundTransferTask[]>([])
const clearDefaultTime = [
  new Date(2000, 0, 1, 0, 0, 0),
  new Date(2000, 0, 1, 23, 59, 59),
]
const openRiskConfig = ref<OpenRiskConfig>({
  min_available_ratio: 0.10,
  min_binance_available_ratio: 0.08,
  min_gate_available_ratio: 0.15,
})
const chartRef = ref<HTMLDivElement | null>(null)
const chartPanelRef = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let chartIntersectionObserver: IntersectionObserver | null = null
let historyAbortController: AbortController | null = null
let historyRequestId = 0
let annualizedReturnRequestId = 0
let gateRiskTimer: ReturnType<typeof setInterval> | null = null
let fundTransferTimer: ReturnType<typeof setInterval> | null = null
const historyCache = new Map<string, { rows: CapitalRow[]; cachedAt: number }>()
const HISTORY_CACHE_TTL_MS = 60_000
const annualizedPeriodOptions: Array<{ value: AnnualizedPeriod; label: string }> = [
  { value: 7, label: '7天' },
  { value: 30, label: '1个月' },
  { value: 90, label: '3个月' },
  { value: 180, label: '半年' },
  { value: 365, label: '1年' },
]

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

const fundTransferStatusLabels: Record<string, string> = {
  queued: '等待划到 Binance 主账户',
  binance_transfer_submitted: '核验 Binance 内部划转',
  binance_master_funded: 'Binance 主账户已到账',
  binance_withdraw_submitted: '核验 Binance 提现',
  binance_withdrawing: 'Binance 提现处理中',
  binance_withdraw_completed: '等待 Gate 入账',
  gate_deposit_confirmed: 'Gate 主账户已到账',
  gate_transfer_submitted: '核验 Gate 内部划转',
  gate_transfer_retry_required: 'Gate 内部划转待重试',
  rollback_pending: '准备退回 Binance 子账户',
  rollback_submitted: '核验 Binance 回滚',
  rollback_retry_required: 'Binance 回滚待重试',
  completed: '划转完成',
  rolled_back: '已退回 Binance',
  failed_before_transfer: '未发生资金动作',
  manually_reconciled: '已人工核对',
}

const fundTransferTerminalStatuses = new Set([
  'completed',
  'rolled_back',
  'failed_before_transfer',
  'manually_reconciled',
])

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
    account_mmr_pct: row?.gate_cross_mmr_pct,
    available_ratio_pct: row?.gate_cross_available_ratio_pct,
    margin_usage_pct: row?.gate_cross_margin_usage_pct,
    initial_margin_usdt: row?.gate_cross_initial_margin_usdt,
    maintenance_margin_usdt: row?.gate_cross_maintenance_margin_usdt,
    nearest_liq_contract: row?.gate_cross_nearest_liq_contract,
    nearest_liq_distance_bps: row?.gate_cross_nearest_liq_distance_bps,
    error: row?.gate_cross_error,
    fetched_at: row?.gate_cross_fetched_at,
  }
})
const gateRiskHealthError = computed(() => (
  liveGateRiskRequestError.value || gateCrossRisk.value.error || ''
))
const recentMinimumGateRisk = computed(() => gateRiskSummary.value?.minimum || null)
const gatePriorityAsset = computed(() => (
  formatRiskContract(gateCrossRisk.value.priority_close_contract)
))
const gateRiskPanelError = computed(() => (
  gateRiskSummaryRequestError.value || gateRiskHealthError.value
))
const annualizedReturnValueClass = computed(() => {
  const value = Number(annualizedReturn.value?.annualized_return_pct)
  if (!Number.isFinite(value)) return 'risk-idle'
  return value >= 0 ? 'pnl-positive' : 'pnl-negative'
})
const chartExchange = computed<ExchangeKey>(() => (
  selectedChartMode.value === 'gate_cross_risk' ? 'gate' : selectedExchange.value
))
const displayedFundTransfer = computed<FundTransferTask | null>(() => (
  activeFundTransfer.value || fundTransferHistory.value[0] || null
))
const fundTransferProgress = computed(() => {
  const status = displayedFundTransfer.value?.status || ''
  if (fundTransferTerminalStatuses.has(status)) return status === 'completed' ? 4 : 0
  if (['gate_deposit_confirmed', 'gate_transfer_submitted', 'gate_transfer_retry_required'].includes(status)) return 3
  if (status === 'binance_withdraw_completed') return 2
  if ([
    'binance_master_funded',
    'binance_withdraw_submitted',
    'binance_withdrawing',
    'rollback_pending',
    'rollback_submitted',
    'rollback_retry_required',
  ].includes(status)) return 1
  return 0
})

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

function formatAnnualizedReturn(): string {
  if (annualizedReturnLoading.value && !annualizedReturn.value) return '-'
  if (!annualizedReturn.value?.sufficient_data) return '数据不足'
  return formatPercent(annualizedReturn.value.annualized_return_pct)
}

function annualizedReturnMeta(): string {
  const summary = annualizedReturn.value
  if (!summary) return annualizedReturnRequestError.value || '暂无收益汇总'
  if (!summary.sufficient_data) {
    return `已有 ${summary.available_days} / ${summary.period_days} 天有效数据`
  }
  return [
    `区间收益 ${formatPercent(summary.period_return_pct)}`,
    `总盈亏 ${formatAmount(summary.period_pnl_usdt)} USDT`,
    `日均总资产 ${formatAmount(summary.average_equity_usdt)} USDT`,
  ].join(' · ')
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
      '已有持仓仍会执行普通止盈、负资金费、下架风险和对账兜底平仓。全仓MMR的停开、主动平仓与交易所强平规则，请查看“全仓MMR”后的问号。',
      '发生交易所强平或 ADL 后，Gate 风险WS与持仓对账会写入铃铛和交易所风险。'
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

function gateMmrValueClass(value: number | null | undefined): string {
  const mmr = Number(value)
  if (!Number.isFinite(mmr)) return 'risk-idle'
  if (mmr <= 300) return 'risk-danger'
  if (mmr <= 500) return 'risk-warning'
  return 'risk-safe'
}

function formatRiskContract(contract: string | null | undefined, fallback = '-'): string {
  if (!contract || contract === 'null') return fallback
  return contract.replace(/_USDT$/i, '')
}

function gatePriorityReasonText(): string {
  if (!gateCrossRisk.value.priority_close_contract) return '当前没有可排序的 Gate 正向持仓'
  const reason = gateCrossRisk.value.priority_close_reason === 'liquidation_distance'
    ? '距强平价已进入 300bps 危险区，优先级最高'
    : '按维持保证金占用从高到低排序'
  const mmr = Number(gateCrossRisk.value.account_mmr_pct)
  if (Number.isFinite(mmr) && mmr <= 500) {
    return `${reason}；当前已停开，MMR降至300%时执行`
  }
  return `${reason}；500%停开，MMR降至300%时执行`
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
  if (!chartRef.value || chart) return
  const { init } = await import('../utils/capitalChart')
  if (!chartRef.value) return
  chart = init(chartRef.value)
  updateChart()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartRef.value)
}

async function activateChart() {
  if (chartActivated.value) return
  chartActivated.value = true
  chartIntersectionObserver?.disconnect()
  chartIntersectionObserver = null
  await Promise.all([
    initChart(),
    fetchHistory(),
  ])
}

async function observeChartVisibility() {
  await nextTick()
  if (!chartPanelRef.value) return
  if (typeof IntersectionObserver === 'undefined') {
    await activateChart()
    return
  }
  chartIntersectionObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      void activateChart()
    }
  }, { rootMargin: '160px 0px' })
  chartIntersectionObserver.observe(chartPanelRef.value)
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

async function fetchGateRiskSummary() {
  try {
    const res = await get('/api/trading/capital/gate-cross-risk/summary?days=7', { silent: true })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    gateRiskSummary.value = data
    gateRiskSummaryRequestError.value = ''
  } catch (e: any) {
    gateRiskSummaryRequestError.value = e?.message || 'Gate风险历史摘要不可用'
  }
}

async function fetchAnnualizedReturn() {
  const period = selectedAnnualizedPeriod.value
  const requestId = ++annualizedReturnRequestId
  annualizedReturn.value = null
  annualizedReturnLoading.value = true
  annualizedReturnRequestError.value = ''
  try {
    const res = await get(
      `/api/trading/capital/annualized-return?days=${period}`,
      { silent: true },
    )
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    if (requestId !== annualizedReturnRequestId) return
    annualizedReturn.value = data
  } catch (e: any) {
    if (requestId !== annualizedReturnRequestId) return
    annualizedReturn.value = null
    annualizedReturnRequestError.value = e?.message || '年化收益统计不可用'
  } finally {
    if (requestId === annualizedReturnRequestId) {
      annualizedReturnLoading.value = false
    }
  }
}

async function fetchCapital(forceHistory = true) {
  loading.value = true
  try {
    await Promise.all([
      (async () => {
        const latestRes = await get('/api/trading/capital/latest')
        const latest = await latestRes.json()
        latestRows.value = latest.rows || []
      })(),
      fetchGateRiskSummary(),
      fetchAnnualizedReturn(),
      chartActivated.value ? fetchHistory(forceHistory) : Promise.resolve(),
    ])
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

function historyRequestParams(): URLSearchParams {
  const params = new URLSearchParams()
  const window = timeWindowOptions.find((item) => item.key === selectedWindow.value)
    || timeWindowOptions.find((item) => item.key === '7d')!
  if (selectedChartMode.value === 'daily_return' && window.hours != null) params.set('days', '1')
  else if (window.hours != null) params.set('hours', String(window.hours))
  else params.set('days', String(window.days || 7))
  params.set('exchange', chartExchange.value)
  params.set('metric', selectedChartMode.value)
  return params
}

async function fetchHistory(force = false) {
  if (!chartActivated.value) return
  const params = historyRequestParams()
  const cacheKey = params.toString()
  const requestId = ++historyRequestId
  historyAbortController?.abort()
  historyAbortController = null
  const cached = historyCache.get(cacheKey)
  if (!force && cached && Date.now() - cached.cachedAt < HISTORY_CACHE_TTL_MS) {
    historyLoading.value = false
    historyRows.value = cached.rows
    return
  }

  const controller = new AbortController()
  historyAbortController = controller
  historyRows.value = []
  historyLoading.value = true
  try {
    const historyRes = await get(
      `/api/trading/capital/history?${params.toString()}`,
      { signal: controller.signal, silent: true },
    )
    const history = await historyRes.json()
    if (!historyRes.ok) throw new Error(history?.detail || `HTTP ${historyRes.status}`)
    if (requestId !== historyRequestId) return
    const rows = history.rows || []
    historyCache.set(cacheKey, { rows, cachedAt: Date.now() })
    historyRows.value = rows
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    showError(e?.message || '获取资金曲线失败')
  } finally {
    if (requestId === historyRequestId) historyLoading.value = false
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

function fundTransferStatusLabel(status: string | null | undefined): string {
  if (!status) return '-'
  return fundTransferStatusLabels[status] || status
}

function fundTransferStatusType(status: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'completed') return 'success'
  if (status === 'failed_before_transfer' || status === 'rolled_back') return 'info'
  if (status?.includes('retry_required')) return 'danger'
  return 'warning'
}

function canRetryFundTransfer(task: FundTransferTask | null): boolean {
  if (!task || fundTransferTerminalStatuses.has(task.status)) return false
  return Boolean(task.attention_required)
    || ['gate_transfer_retry_required', 'rollback_retry_required'].includes(task.status)
}

function fundTransferTxUrl(txId: string | null | undefined): string {
  return txId ? `https://bscscan.com/tx/${encodeURIComponent(txId)}` : ''
}

async function fetchFundTransfers(silent = true) {
  const previousActiveId = activeFundTransfer.value?.id
  try {
    const res = await get('/api/trading/capital/fund-transfer?limit=30', { silent })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    activeFundTransfer.value = data.active || null
    fundTransferHistory.value = data.history || []
    if (previousActiveId && !activeFundTransfer.value) {
      try {
        await post('/api/trading/capital/run', undefined, { silent: true })
      } catch {
        // 任务终态不受快照刷新失败影响，随后仍读取已有资金快照。
      }
      await fetchCapital()
    }
  } catch (e: any) {
    if (!silent) showError(e?.message || '读取资金划转状态失败')
  }
}

async function openFundTransferDialog() {
  fundTransferPreflight.value = null
  fundTransferPassword.value = ''
  fundTransferLimitsError.value = ''
  fundTransferDialogVisible.value = true
  await Promise.all([
    fetchFundTransfers(false),
    fetchFundTransferLimits(false),
  ])
}

async function fetchFundTransferLimits(silent = true): Promise<FundTransferLimits | null> {
  fundTransferLimitsLoading.value = true
  fundTransferLimitsError.value = ''
  try {
    const res = await get('/api/trading/capital/fund-transfer/limits', { silent })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    fundTransferLimits.value = data.limits
    return data.limits
  } catch (e: any) {
    fundTransferLimits.value = null
    fundTransferLimitsError.value = e?.message || '读取实时划转额度失败'
    if (!silent) showError(fundTransferLimitsError.value)
    return null
  } finally {
    fundTransferLimitsLoading.value = false
  }
}

async function preflightFundTransfer(): Promise<FundTransferPreview | null> {
  const amount = Number(fundTransferAmount.value)
  if (!Number.isFinite(amount) || amount <= 0) {
    showError('请输入有效的划转金额')
    return null
  }
  const limits = fundTransferLimits.value
  if (limits && amount < Number(limits.minimum_transfer_amount)) {
    showError(`当前最小可划转 ${formatAmount(limits.minimum_transfer_amount)} USDT`)
    return null
  }
  if (limits && amount > Number(limits.maximum_transfer_amount)) {
    showError(`当前最大可划转 ${formatAmount(limits.maximum_transfer_amount)} USDT`)
    return null
  }
  fundTransferPreflighting.value = true
  try {
    const res = await get(
      `/api/trading/capital/fund-transfer/preflight?amount=${encodeURIComponent(String(amount))}`,
      { silent: true },
    )
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    fundTransferPreflight.value = data.preview
    fundTransferLimits.value = {
      coin: data.preview.coin,
      network: data.preview.network,
      destination_masked: data.preview.destination_masked,
      fee: data.preview.fee,
      minimum_received_amount: data.preview.minimum_received_amount,
      binance_forward_free: data.preview.binance_forward_free,
      minimum_transfer_amount: data.preview.minimum_transfer_amount,
      maximum_transfer_amount: data.preview.maximum_transfer_amount,
    }
    return data.preview
  } catch (e: any) {
    fundTransferPreflight.value = null
    showError(e?.message || '资金划转预检失败')
    return null
  } finally {
    fundTransferPreflighting.value = false
  }
}

async function submitFundTransfer() {
  if (activeFundTransfer.value) {
    showError('已有资金划转任务正在处理')
    return
  }
  if (!fundTransferPassword.value) {
    showError('请输入当前登录密码')
    return
  }
  const preview = await preflightFundTransfer()
  if (!preview) return
  try {
    await ElMessageBox.confirm(
      `Binance forward 子账户将减少 ${formatAmount(preview.requested_amount)} ${preview.coin}。\n`
      + `网络 ${preview.network}，手续费 ${formatAmount(preview.fee)} ${preview.coin}，`
      + `预计 Gate 到账 ${formatAmount(preview.received_amount)} ${preview.coin}。\n`
      + `到账地址 ${preview.destination_masked}。确认后任务将自动执行，不能重复发起。`,
      '确认真实资金划转',
      {
        confirmButtonText: '确认划转',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }

  fundTransferCreating.value = true
  try {
    const res = await post('/api/trading/capital/fund-transfer', {
      amount: preview.requested_amount,
      password: fundTransferPassword.value,
    }, { silent: true })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    activeFundTransfer.value = data.task
    fundTransferPassword.value = ''
    fundTransferPreflight.value = null
    showSuccess('资金划转任务已创建，正向开仓已独立暂停')
    await fetchFundTransfers()
  } catch (e: any) {
    showError(e?.message || '创建资金划转任务失败')
  } finally {
    fundTransferCreating.value = false
  }
}

async function retryFundTransfer(task: FundTransferTask) {
  let password = ''
  try {
    const result = await ElMessageBox.prompt(
      '系统只会重新核验，或重试已经确认资金位置的安全步骤。',
      `恢复资金划转 #${task.id}`,
      {
        confirmButtonText: '确认恢复',
        cancelButtonText: '取消',
        inputType: 'password',
        inputPlaceholder: '输入当前登录密码',
        inputValidator: (value: string) => Boolean(value) || '请输入当前登录密码',
      },
    )
    password = result.value
  } catch {
    return
  }
  fundTransferRetrying.value = true
  try {
    const res = await post(
      `/api/trading/capital/fund-transfer/${task.id}/retry`,
      { password },
      { silent: true },
    )
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
    showSuccess('恢复请求已提交，系统将继续按资金位置核验')
    await fetchFundTransfers(false)
  } catch (e: any) {
    showError(e?.message || '资金划转恢复失败')
  } finally {
    fundTransferRetrying.value = false
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
  void fetchHistory()
}

async function refreshCapital() {
  await fetchCapital(true)
}

onMounted(async () => {
  await Promise.all([
    fetchOpenRiskConfig(),
    fetchLiveGateRisk(),
    fetchCapital(false),
    fetchFundTransfers(),
  ])
  await observeChartVisibility()
  gateRiskTimer = setInterval(fetchLiveGateRisk, 2000)
  fundTransferTimer = setInterval(() => {
    if (fundTransferDialogVisible.value || activeFundTransfer.value) {
      fetchFundTransfers()
    }
  }, 3000)
})

watch(historyRows, () => {
  updateChart()
})

watch([selectedExchange, selectedChartMode], () => {
  void fetchHistory()
})

watch(selectedAnnualizedPeriod, () => {
  void fetchAnnualizedReturn()
})

onBeforeUnmount(() => {
  historyAbortController?.abort()
  historyAbortController = null
  chartIntersectionObserver?.disconnect()
  chartIntersectionObserver = null
  if (gateRiskTimer) clearInterval(gateRiskTimer)
  gateRiskTimer = null
  if (fundTransferTimer) clearInterval(fundTransferTimer)
  fundTransferTimer = null
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
      <el-button
        size="small"
        :type="activeFundTransfer ? 'warning' : 'primary'"
        plain
        @click="openFundTransferDialog"
      >
        {{ activeFundTransfer ? `划转中 #${activeFundTransfer.id}` : '资金划转' }}
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
      <el-button size="small" :loading="loading" @click="refreshCapital">刷新</el-button>
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
        <div class="equity-breakdown">
          <div class="equity-breakdown-item">
            <span>账户余额</span>
            <strong>
              <span>{{ formatAmount(latestByExchange[exchange]?.account_balance_usdt) }}</span>
              <span v-if="hasAmount(latestByExchange[exchange]?.account_balance_usdt)" class="metric-unit">USDT</span>
            </strong>
          </div>
          <div class="equity-breakdown-item">
            <span>未实现盈亏</span>
            <strong :class="Number(latestByExchange[exchange]?.account_unrealized_pnl_usdt || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'">
              <span>{{ formatAmount(latestByExchange[exchange]?.account_unrealized_pnl_usdt) }}</span>
              <span v-if="hasAmount(latestByExchange[exchange]?.account_unrealized_pnl_usdt)" class="metric-unit">USDT</span>
            </strong>
          </div>
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
        <div v-if="exchange !== 'gate'" class="metric-row">
          <span>占用</span>
          <strong>
            <span>{{ formatAmount(occupiedAmount(latestByExchange[exchange], exchange)) }}</span>
            <span v-if="hasAmount(occupiedAmount(latestByExchange[exchange], exchange))" class="metric-unit">USDT</span>
          </strong>
        </div>
        <div v-else class="gate-summary-risk">
          <div class="metric-row">
            <span>维持保证金</span>
            <strong>
              <span>{{ formatAmount(gateCrossRisk.maintenance_margin_usdt) }}</span>
              <span v-if="hasAmount(gateCrossRisk.maintenance_margin_usdt)" class="metric-unit">USDT</span>
            </strong>
          </div>
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
      <div class="gate-risk-review-header">
        <span>重点摘要</span>
        <span>MMR历史仅统计有效官方快照</span>
      </div>
      <div class="gate-current-mmr">
        <div class="gate-current-mmr-copy">
          <span class="metric-label-with-help">
            <span>当前全仓MMR</span>
            <el-popover
              trigger="click"
              placement="top"
              width="min(560px, calc(100vw - 24px))"
            >
              <template #reference>
                <el-button
                  class="help-icon-button"
                  text
                  circle
                  size="small"
                  aria-label="全仓MMR风控说明"
                >
                  <el-icon><QuestionFilled /></el-icon>
                </el-button>
              </template>
              <div class="mmr-help">
                <div class="mmr-help-row">
                  <strong class="risk-warning">500%</strong>
                  <span>停止新的正向开仓并写入铃铛告警。已有持仓不会因为 500% 被强平，仍会执行普通止盈、负资金费、下架风险和对账兜底平仓。</span>
                </div>
                <div class="mmr-help-row">
                  <strong class="risk-danger">300%</strong>
                  <span>使用 5 秒内的 Gate 官方账户 MMR，按风险顺序全量退出全部正向持仓：先平距强平价不超过 300bps 的合约，再按维持保证金从高到低处理；每笔先以 reduce-only 市价买回 Gate 空头，再市价卖出 Binance 现货。该路径不依赖盘口、WS 或普通平仓冷却，失败会在下一轮立即重试。</span>
                </div>
                <div class="mmr-help-row">
                  <strong>100%</strong>
                  <span>Gate 交易所强平基准线，最终以 Gate 返回的强平价和交易所风控结果为准；系统目标是在 300% 完成主动退出。</span>
                </div>
                <div class="mmr-help-note">
                  独立危险条件：任一正向空头距强平价不超过 300bps 时，不等待账户 MMR 降至 300%，立即市价平掉该合约对应的完整套利仓位。
                </div>
              </div>
            </el-popover>
          </span>
          <span>{{ gateCrossRisk.status_label || '实时官方账户风险' }}</span>
        </div>
        <strong :class="gateRiskStatusClass(gateCrossRisk.status)">
          {{ formatPercent(gateCrossRisk.account_mmr_pct) }}
        </strong>
      </div>
      <div class="gate-risk-review-grid">
        <div class="gate-risk-review-item annualized-summary">
          <div class="annualized-summary-heading">
            <span class="gate-risk-review-label">策略年化收益率</span>
            <el-radio-group
              v-model="selectedAnnualizedPeriod"
              size="small"
              class="annualized-period-selector"
            >
              <el-radio-button
                v-for="option in annualizedPeriodOptions"
                :key="option.value"
                :label="option.value"
              >
                {{ option.label }}
              </el-radio-button>
            </el-radio-group>
          </div>
          <strong :class="annualizedReturnValueClass">
            {{ formatAnnualizedReturn() }}
          </strong>
          <div class="gate-risk-review-meta annualized-summary-meta">
            <span>{{ annualizedReturnMeta() }}</span>
          </div>
        </div>
        <div class="gate-risk-review-item">
          <span class="gate-risk-review-label">近7天最低全仓MMR</span>
          <strong :class="gateMmrValueClass(recentMinimumGateRisk?.account_mmr_pct)">
            {{ formatPercent(recentMinimumGateRisk?.account_mmr_pct) }}
          </strong>
          <div class="gate-risk-review-meta">
            <span>{{ recentMinimumGateRisk?.snapshot_at || '暂无有效历史快照' }}</span>
            <span v-if="recentMinimumGateRisk?.primary_risk_asset">
              主要风险币 {{ recentMinimumGateRisk.primary_risk_asset }}
            </span>
          </div>
        </div>
        <div class="gate-risk-review-item">
          <span class="gate-risk-review-label">低于500%首平候选</span>
          <strong :class="gateMmrValueClass(gateCrossRisk.account_mmr_pct)">
            {{ gatePriorityAsset }}
          </strong>
          <div class="gate-risk-review-meta">
            <span>{{ gatePriorityReasonText() }}</span>
          </div>
        </div>
      </div>
      <div v-if="gateRiskPanelError" class="risk-health-error">
        {{ gateRiskPanelError }}
      </div>
    </div>

    <div ref="chartPanelRef" class="chart-panel">
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
      <div v-loading="historyLoading" class="chart-wrap">
        <div ref="chartRef" class="echarts-chart"></div>
        <div
          v-if="chartActivated && !historyLoading && !historyRows.length"
          class="empty-text"
        >
          暂无资金快照
        </div>
      </div>
    </div>

    <el-dialog
      v-model="fundTransferDialogVisible"
      title="Binance → Gate 资金划转"
      width="min(820px, 96vw)"
      class="fund-transfer-dialog"
      append-to-body
      destroy-on-close
    >
      <div class="fund-transfer-content">
        <div v-if="activeFundTransfer" class="fund-transfer-active">
          <div class="fund-transfer-status-line">
            <div>
              <strong>任务 #{{ activeFundTransfer.id }}</strong>
              <span>{{ formatAmount(activeFundTransfer.requested_amount) }} {{ activeFundTransfer.coin }}</span>
            </div>
            <el-tag :type="fundTransferStatusType(activeFundTransfer.status)" effect="dark">
              {{ fundTransferStatusLabel(activeFundTransfer.status) }}
            </el-tag>
          </div>
          <el-steps
            :active="fundTransferProgress"
            finish-status="success"
            process-status="process"
            align-center
            class="fund-transfer-steps"
          >
            <el-step title="主账户" />
            <el-step title="提现" />
            <el-step title="Gate入账" />
            <el-step title="合约账户" />
          </el-steps>
          <div class="fund-transfer-message">
            {{ activeFundTransfer.status_message || fundTransferStatusLabel(activeFundTransfer.status) }}
          </div>
          <div class="fund-transfer-detail-grid">
            <span>预计手续费</span>
            <strong>{{ formatAmount(activeFundTransfer.expected_fee) }} {{ activeFundTransfer.coin }}</strong>
            <span>预计到账</span>
            <strong>{{ formatAmount(activeFundTransfer.withdraw_amount) }} {{ activeFundTransfer.coin }}</strong>
            <span>网络 / 地址</span>
            <strong>{{ activeFundTransfer.network }} / {{ activeFundTransfer.destination_masked }}</strong>
            <span>开始时间</span>
            <strong>{{ activeFundTransfer.created_at }}</strong>
          </div>
          <a
            v-if="activeFundTransfer.binance_tx_id"
            class="fund-transfer-tx-link"
            :href="fundTransferTxUrl(activeFundTransfer.binance_tx_id)"
            target="_blank"
            rel="noopener noreferrer"
          >
            在 BscScan 查看链上交易
          </a>
          <div v-if="activeFundTransfer.last_error" class="fund-transfer-error">
            {{ activeFundTransfer.last_error }}
          </div>
          <el-button
            v-if="canRetryFundTransfer(activeFundTransfer)"
            size="small"
            type="warning"
            :loading="fundTransferRetrying"
            @click="retryFundTransfer(activeFundTransfer)"
          >
            重新核验并恢复
          </el-button>
        </div>

        <div v-else class="fund-transfer-form">
          <el-alert
            title="输入金额是 Binance forward 子账户的总减少额，网络手续费包含在该金额内。任务执行期间只暂停正向开仓，平仓和风控不受影响。"
            type="warning"
            :closable="false"
            show-icon
          />
          <div class="fund-transfer-limits" :class="{ loading: fundTransferLimitsLoading }">
            <div>
              <span>当前最小可划转</span>
              <strong>
                {{ fundTransferLimits ? formatAmount(fundTransferLimits.minimum_transfer_amount) : '--' }}
                USDT
              </strong>
            </div>
            <div>
              <span>当前最大可划转</span>
              <strong>
                {{ fundTransferLimits ? formatAmount(fundTransferLimits.maximum_transfer_amount) : '--' }}
                USDT
              </strong>
            </div>
          </div>
          <div v-if="fundTransferLimitsError" class="fund-transfer-limits-error">
            {{ fundTransferLimitsError }}
          </div>
          <el-form label-position="top" @submit.prevent>
            <el-form-item label="划转金额 (USDT)">
              <el-input
                v-model="fundTransferAmount"
                inputmode="decimal"
                placeholder="例如 100"
                @input="fundTransferPreflight = null"
              />
            </el-form-item>
            <el-form-item label="当前登录密码">
              <el-input
                v-model="fundTransferPassword"
                type="password"
                show-password
                autocomplete="current-password"
                placeholder="每次真实划转都需要重新验证"
              />
            </el-form-item>
          </el-form>
          <div v-if="fundTransferPreflight" class="fund-transfer-preview">
            <div><span>Binance forward 可用</span><strong>{{ formatAmount(fundTransferPreflight.binance_forward_free) }} USDT</strong></div>
            <div><span>总扣除</span><strong>{{ formatAmount(fundTransferPreflight.requested_amount) }} USDT</strong></div>
            <div><span>网络手续费</span><strong>{{ formatAmount(fundTransferPreflight.fee) }} USDT</strong></div>
            <div><span>预计 Gate 到账</span><strong>{{ formatAmount(fundTransferPreflight.received_amount) }} USDT</strong></div>
            <div><span>网络 / 地址</span><strong>{{ fundTransferPreflight.network }} / {{ fundTransferPreflight.destination_masked }}</strong></div>
          </div>
          <div class="fund-transfer-actions">
            <el-button
              size="small"
              :loading="fundTransferPreflighting"
              @click="preflightFundTransfer"
            >
              检查金额
            </el-button>
            <el-button
              size="small"
              type="primary"
              :loading="fundTransferCreating"
              @click="submitFundTransfer"
            >
              确认划转
            </el-button>
          </div>
        </div>

        <div class="fund-transfer-history">
          <div class="fund-transfer-section-title">划转记录</div>
          <el-table :data="fundTransferHistory" size="small" max-height="240">
            <el-table-column prop="id" label="任务" width="68">
              <template #default="{ row }">#{{ row.id }}</template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" min-width="150" />
            <el-table-column label="金额" width="110" align="right">
              <template #default="{ row }">{{ formatAmount(row.requested_amount) }} {{ row.coin }}</template>
            </el-table-column>
            <el-table-column label="状态" min-width="150">
              <template #default="{ row }">
                <el-tag :type="fundTransferStatusType(row.status)" size="small">
                  {{ fundTransferStatusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="链上" width="72" align="center">
              <template #default="{ row }">
                <a
                  v-if="row.binance_tx_id"
                  :href="fundTransferTxUrl(row.binance_tx_id)"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="fund-transfer-tx-link"
                >
                  查看
                </a>
                <span v-else>-</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
      <template #footer>
        <el-button size="small" @click="fundTransferDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

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

.gate-risk-review-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  color: var(--app-text);
  font-size: 15px;
  font-weight: 700;
}

.gate-risk-review-header span:last-child {
  color: var(--app-text-muted);
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.gate-current-mmr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  min-height: 70px;
  padding: 8px 12px 14px;
  border-bottom: 1px solid var(--app-border);
}

.gate-current-mmr-copy {
  display: grid;
  gap: 5px;
  color: var(--app-text);
  font-size: 14px;
  font-weight: 650;
}

.gate-current-mmr-copy > span:last-child {
  color: var(--app-text-muted);
  font-size: 11px;
  font-weight: 500;
}

.gate-current-mmr > strong {
  font-size: 28px;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  white-space: nowrap;
}

.risk-safe {
  color: #67c23a !important;
}

.risk-warning {
  color: #e6a23c !important;
}

.risk-danger {
  color: #f56c6c !important;
}

.risk-idle {
  color: var(--app-text-muted) !important;
}

.gate-risk-review-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border-top: 1px solid var(--app-border);
}

.gate-risk-review-item {
  display: grid;
  grid-template-columns: minmax(150px, auto) minmax(0, 1fr);
  align-items: center;
  column-gap: 14px;
  row-gap: 4px;
  min-height: 58px;
  padding: 8px 12px;
  border-left: 1px solid var(--app-border);
}

.gate-risk-review-item:first-child {
  border-left: 0;
}

.gate-risk-review-label {
  color: var(--app-text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.gate-risk-review-item strong {
  min-width: 0;
  color: var(--app-text);
  font-size: 18px;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}

.gate-risk-review-meta {
  grid-column: 1 / -1;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--app-text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.annualized-summary {
  grid-template-columns: minmax(0, 1fr);
  align-content: start;
  gap: 8px;
}

.annualized-summary-heading {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.annualized-period-selector {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.annualized-summary > strong {
  text-align: left;
}

.annualized-summary-meta {
  grid-column: 1;
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

.equity-breakdown {
  display: grid;
  gap: 3px;
  margin: -1px 0 5px;
  padding: 5px 8px;
  border-left: 2px solid var(--app-border);
  color: var(--app-text-muted);
  font-size: 11px;
}

.equity-breakdown-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.equity-breakdown-item strong {
  display: inline-flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 4px;
  min-width: 0;
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.gate-summary-risk {
  border-top: 1px solid var(--app-border);
  margin-top: 5px;
  padding-top: 3px;
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

.mmr-help {
  display: grid;
  gap: 10px;
  color: var(--app-text);
  font-size: 12px;
  line-height: 1.55;
}

.mmr-help-row {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  align-items: start;
  gap: 10px;
}

.mmr-help-row strong {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.mmr-help-note {
  border-top: 1px solid var(--app-border);
  padding-top: 8px;
  color: var(--app-text-muted);
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

:deep(.fund-transfer-dialog .el-dialog__body) {
  max-height: min(70vh, 720px);
  overflow-y: auto;
}

.fund-transfer-content {
  display: grid;
  gap: 18px;
}

.fund-transfer-active,
.fund-transfer-form {
  display: grid;
  gap: 14px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--app-border);
}

.fund-transfer-status-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.fund-transfer-status-line > div {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}

.fund-transfer-status-line strong {
  color: var(--app-text);
  font-size: 15px;
}

.fund-transfer-status-line span {
  color: var(--app-text-muted);
  font-size: 12px;
}

.fund-transfer-steps {
  margin: 4px 0;
}

.fund-transfer-message {
  border-left: 3px solid #e6a23c;
  padding: 7px 10px;
  background: color-mix(in srgb, #e6a23c 8%, transparent);
  color: var(--app-text);
  font-size: 12px;
  line-height: 1.5;
}

.fund-transfer-detail-grid {
  display: grid;
  grid-template-columns: minmax(110px, auto) minmax(0, 1fr);
  gap: 7px 14px;
  font-size: 12px;
}

.fund-transfer-detail-grid span {
  color: var(--app-text-muted);
}

.fund-transfer-detail-grid strong {
  min-width: 0;
  color: var(--app-text);
  font-weight: 600;
  text-align: right;
  overflow-wrap: anywhere;
}

.fund-transfer-preview {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border-top: 1px solid var(--app-border);
  border-bottom: 1px solid var(--app-border);
}

.fund-transfer-limits {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border-top: 1px solid var(--app-border);
  border-bottom: 1px solid var(--app-border);
  opacity: 1;
}

.fund-transfer-limits.loading {
  opacity: 0.62;
}

.fund-transfer-limits > div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 10px;
}

.fund-transfer-limits > div:first-child {
  border-right: 1px solid var(--app-border);
}

.fund-transfer-limits span {
  color: var(--app-text-muted);
  font-size: 12px;
}

.fund-transfer-limits strong {
  color: #409eff;
  font-size: 14px;
  text-align: right;
}

.fund-transfer-limits-error {
  color: #f56c6c;
  font-size: 12px;
}

.fund-transfer-preview > div {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 9px;
  border-bottom: 1px solid var(--app-border);
  color: var(--app-text-muted);
  font-size: 12px;
}

.fund-transfer-preview > div:nth-child(odd) {
  border-right: 1px solid var(--app-border);
}

.fund-transfer-preview strong {
  color: var(--app-text);
  text-align: right;
  overflow-wrap: anywhere;
}

.fund-transfer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.fund-transfer-error {
  border-left: 3px solid #f56c6c;
  padding: 7px 10px;
  color: #f56c6c;
  background: color-mix(in srgb, #f56c6c 8%, transparent);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.fund-transfer-tx-link {
  color: #409eff;
  font-size: 12px;
  text-decoration: none;
}

.fund-transfer-tx-link:hover {
  text-decoration: underline;
}

.fund-transfer-history {
  min-width: 0;
}

.fund-transfer-section-title {
  margin-bottom: 8px;
  color: var(--app-text);
  font-size: 13px;
  font-weight: 600;
}

.clear-range-dialog {
  display: flex;
  width: 100%;
}

.clear-range-picker {
  width: 100%;
}

@media (max-width: 900px) {
  .capital-page {
    padding: 10px;
  }

  .summary-grid {
    grid-template-columns: 1fr;
  }

  .summary-card {
    min-width: 0;
    padding: 10px;
    overflow: hidden;
  }

  .card-header,
  .metric-row,
  .equity-breakdown-item {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }

  .metric-row strong,
  .equity-breakdown-item strong {
    justify-content: flex-start;
    max-width: 100%;
    text-align: left;
  }

  .available-value,
  .bnb-value {
    max-width: 100%;
  }

  .gate-risk-review-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }

  .gate-risk-review-header span:last-child,
  .gate-risk-review-label {
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .gate-risk-review-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .gate-risk-review-item {
    grid-template-columns: minmax(0, 1fr);
    border-top: 1px solid var(--app-border);
    border-left: 0;
  }

  .gate-risk-review-item:first-child {
    border-top: 0;
  }

  .gate-risk-review-item strong {
    font-size: 13px;
    text-align: left;
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .gate-risk-review-meta {
    grid-column: 1;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .gate-current-mmr {
    align-items: flex-start;
  }

  .gate-current-mmr > strong {
    font-size: 24px;
  }

  .annualized-summary-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .annualized-period-selector {
    justify-content: flex-start;
  }

  .fund-transfer-status-line,
  .fund-transfer-status-line > div {
    align-items: flex-start;
    flex-direction: column;
  }

  .fund-transfer-preview {
    grid-template-columns: minmax(0, 1fr);
  }

  .fund-transfer-limits {
    grid-template-columns: minmax(0, 1fr);
  }

  .fund-transfer-limits > div:first-child {
    border-right: 0;
    border-bottom: 1px solid var(--app-border);
  }

  .fund-transfer-preview > div:nth-child(odd) {
    border-right: 0;
  }

  .fund-transfer-detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .fund-transfer-detail-grid strong {
    text-align: left;
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

<style>
.fund-transfer-dialog.el-dialog {
  display: flex;
  flex-direction: column;
  max-height: 96vh;
  margin-top: 2vh;
  margin-bottom: 2vh;
}

.fund-transfer-dialog.el-dialog .el-dialog__body {
  min-height: 0;
  max-height: none;
  overflow-y: auto;
}

@media (max-width: 520px) {
  .fund-transfer-dialog.el-dialog .el-dialog__header,
  .fund-transfer-dialog.el-dialog .el-dialog__body,
  .fund-transfer-dialog.el-dialog .el-dialog__footer {
    padding-left: 16px;
    padding-right: 16px;
  }

  .fund-transfer-dialog.el-dialog .el-step__title {
    font-size: 11px;
  }
}
</style>
