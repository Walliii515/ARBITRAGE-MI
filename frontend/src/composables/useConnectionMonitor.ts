import { computed, ref } from 'vue'
import { get } from '../utils/request'

export interface ConnectionRow {
  base_asset: string
  contract: string
  symbol: string
  gate_ws_subscribed: boolean
  gate_receiving_data: boolean
  gate_last_update: number
  gate_stale_sec: number | null
  binance_ws_subscribed: boolean
  binance_receiving_data: boolean
  binance_last_update: number
  binance_stale_sec: number | null
  asset_is_valid?: boolean
  asset_status?: 'enabled' | 'disabled' | string | null
  delist_risk_level?: string | null
  delist_risk_summary?: string | null
  delist_risks?: DelistRiskItem[]
}

export interface DelistRiskItem {
  risk_key: string
  base_asset: string
  exchange: string
  market_type: string
  symbol: string
  risk_type: string
  risk_level: string
  status?: string | null
  delist_at?: string | null
  days_left?: number | null
  message?: string | null
}

export interface DelistRiskReport {
  items: DelistRiskItem[]
  summary: {
    total: number
    critical: number
    warning: number
  }
  source_errors?: Record<string, string>
  checked_at?: string
  lookahead_days?: number
}

export interface ExchangeRiskMonitorStatus {
  enabled: boolean
  connected: boolean
  channels: Record<string, string>
  last_message_at?: number | null
  last_event_at?: number | null
  last_close_at?: number | null
  last_error?: string | null
  message_count?: number
  event_count?: number
  queue_size?: number
  worker_alive?: boolean
  ws_thread_alive?: boolean
  message_age_sec?: number | null
  event_age_sec?: number | null
}

const DELIST_REFRESH_MS = 15 * 60 * 1000
let delistFetchedAt = 0
let delistReportCache: DelistRiskReport = {
  items: [],
  summary: { total: 0, critical: 0, warning: 0 },
}

export function isConnectionAssetEnabled(row: ConnectionRow) {
  return row.asset_status !== 'disabled' && row.asset_is_valid !== false
}

export function buildConnectionStats(rows: ConnectionRow[]) {
  const total = rows.length
  const gateSubscribed = rows.filter((r) => r.gate_ws_subscribed).length
  const gateReceiving = rows.filter((r) => r.gate_receiving_data).length
  const binanceSubscribed = rows.filter((r) => r.binance_ws_subscribed).length
  const binanceReceiving = rows.filter((r) => r.binance_receiving_data).length
  const delistRisk = rows.filter((r) => isConnectionAssetEnabled(r) && r.delist_risks && r.delist_risks.length > 0).length

  return {
    total,
    gateSubscribed,
    gateReceiving,
    binanceSubscribed,
    binanceReceiving,
    delistRisk,
  }
}

function mergeDelistRisks(rows: ConnectionRow[], report: DelistRiskReport): ConnectionRow[] {
  const byAsset = new Map<string, DelistRiskItem[]>()
  for (const item of report.items || []) {
    const key = (item.base_asset || '').toUpperCase()
    if (!key) continue
    const list = byAsset.get(key) || []
    list.push(item)
    byAsset.set(key, list)
  }
  return rows.map((row) => {
    if (!isConnectionAssetEnabled(row)) {
      return {
        ...row,
        delist_risks: [],
        delist_risk_level: null,
        delist_risk_summary: null,
      }
    }
    const risks = byAsset.get((row.base_asset || '').toUpperCase()) || []
    const level = risks.some((r) => r.risk_level === 'critical')
      ? 'critical'
      : risks.some((r) => r.risk_level === 'warning') ? 'warning' : null
    const summary = risks.map((risk) => {
      const due = risk.delist_at ? ` ${risk.delist_at}` : ''
      return `${risk.exchange}:${risk.message || risk.status || risk.risk_type}${due}`
    }).join(' | ')
    return {
      ...row,
      delist_risks: risks,
      delist_risk_level: level,
      delist_risk_summary: summary || null,
    }
  })
}

export function useConnectionMonitor() {
  const connectionRows = ref<ConnectionRow[]>([])
  const serviceState = ref('idle')
  const gateWsConnected = ref(false)
  const binanceWsConnected = ref(false)
  const gateWsLatencyMs = ref<number | null>(null)
  const binanceWsLatencyMs = ref<number | null>(null)
  const exchangeRiskMonitor = ref<ExchangeRiskMonitorStatus | null>(null)
  const delistRiskReport = ref<DelistRiskReport>(delistReportCache)

  const connectionStats = computed(() => buildConnectionStats(connectionRows.value))

  async function fetchDelistRisks(force = false) {
    const now = Date.now()
    if (!force && now - delistFetchedAt < DELIST_REFRESH_MS) {
      return delistReportCache
    }
    const res = await get('/api/trading/delist-risks?lookahead_days=30')
    if (!res.ok) throw new Error('获取下架风险失败')
    const data = await res.json()
    delistReportCache = {
      items: Array.isArray(data.items) ? data.items : [],
      summary: data.summary || { total: 0, critical: 0, warning: 0 },
      source_errors: data.source_errors || {},
      checked_at: data.checked_at,
      lookahead_days: data.lookahead_days,
    }
    delistFetchedAt = now
    delistRiskReport.value = delistReportCache
    return delistReportCache
  }

  async function fetchConnectionStatus() {
    const res = await get('/api/service/connections')
    if (!res.ok) throw new Error('获取连接状态失败')
    const data = await res.json()
    const report = await fetchDelistRisks().catch(() => delistReportCache)
    connectionRows.value = mergeDelistRisks(data.items || [], report)
    serviceState.value = data.state || 'idle'
    gateWsConnected.value = data.gate_ws_connected || false
    binanceWsConnected.value = data.binance_ws_connected || false
    gateWsLatencyMs.value = data.gate_ws_latency_ms ?? null
    binanceWsLatencyMs.value = data.binance_ws_latency_ms ?? null
    exchangeRiskMonitor.value = data.exchange_risk_monitor || null
    return data
  }

  return {
    connectionRows,
    connectionStats,
    serviceState,
    gateWsConnected,
    binanceWsConnected,
    gateWsLatencyMs,
    binanceWsLatencyMs,
    exchangeRiskMonitor,
    delistRiskReport,
    fetchDelistRisks,
    fetchConnectionStatus,
  }
}
