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

export function buildConnectionStats(rows: ConnectionRow[]) {
  const total = rows.length
  const gateSubscribed = rows.filter((r) => r.gate_ws_subscribed).length
  const gateReceiving = rows.filter((r) => r.gate_receiving_data).length
  const binanceSubscribed = rows.filter((r) => r.binance_ws_subscribed).length
  const binanceReceiving = rows.filter((r) => r.binance_receiving_data).length

  return {
    total,
    gateSubscribed,
    gateReceiving,
    binanceSubscribed,
    binanceReceiving,
  }
}

export function useConnectionMonitor() {
  const connectionRows = ref<ConnectionRow[]>([])
  const serviceState = ref('idle')
  const gateWsConnected = ref(false)
  const binanceWsConnected = ref(false)
  const gateWsLatencyMs = ref<number | null>(null)
  const binanceWsLatencyMs = ref<number | null>(null)
  const exchangeRiskMonitor = ref<ExchangeRiskMonitorStatus | null>(null)

  const connectionStats = computed(() => buildConnectionStats(connectionRows.value))

  async function fetchConnectionStatus() {
    const res = await get('/api/service/connections')
    if (!res.ok) throw new Error('获取连接状态失败')
    const data = await res.json()
    connectionRows.value = data.items || []
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
    fetchConnectionStatus,
  }
}
