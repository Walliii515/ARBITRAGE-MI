<script setup lang="ts">
import { ref, shallowRef, onMounted, onUnmounted, computed } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
} from 'ag-grid-community'
import { ElPopover } from 'element-plus'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import FundingHistoryTooltip from '../ag-grid/FundingHistoryTooltip.vue'
import { get, post } from '../utils/request'
import { getToken } from '../utils/auth'

/** 标准开仓金额（USDT）：从后端 REST/WS 推送动态读取，与后端 config.yaml trade.open.amount_usdt 保持一致，
 *  避免前后端硬编码漂移导致 funding_pnl_bps 兑底计算偏差。
 *  接收顺序：REST /api/trading/positions 响应 → WS position_update 推送。
 *  兑底默认 10，仅防后端未推送时出现 NaN。
 */
const openAmountUsdt = ref<number>(10)

/** 兑底补充 funding_pnl_bps（后端未注入时由前端根据金额反算） */
function ensureFundingBps(rows: PositionRow[]) {
  const base = openAmountUsdt.value || 10
  for (const row of rows) {
    if (row.funding_pnl_bps == null && row.funding_total_pnl != null) {
      row.funding_pnl_bps = Math.round(row.funding_total_pnl / base * 10000 * 100) / 100
    }
  }
}

/* ───── 类型 ───── */
interface PositionRow {
  id: number
  opened_at: string | null
  closed_at: string | null
  base_asset: string | null
  spot_symbol: string | null
  future_contract: string | null
  status: string | null
  open_reason: string | null
  close_reason: string | null
  spot_open_price: number | null
  future_open_price: number | null
  open_spread_bps: number | null
  close_spread_bps: number | null
  current_spot_price: number | null
  current_future_price: number | null
  current_spread_bps: number | null
  spot_open_amount: number | null
  floating_pnl_total: number | null
  floating_pnl_bps: number | null
  fee_bps: number | null
  risk_relief_bps: number | null
  funding_pnl_bps: number | null
  funding_rate: number | null
  funding_rate_24h: number | null
  funding_interval: number | null
  funding_interval_hours: number | null
  funding_last_apply: string | null
  funding_next_apply: string | null
  funding_total_pnl: number | null
  funding_payments_count: number | null
  funding_history: Array<{ seq: number; rate: number; rate_24h: number | null; pnl: number; notional: number | null; time: string | null }> | null
  realized_pnl_bps: number | null
  realized_pnl: number | null
  total_pnl_bps: number | null
  total_pnl: number | null
  fee_cost: number | null
  margin_topup_count: number | null
  margin_topup_total: number | null
  margin_topup_last_at: string | null
  margin_initial: number | null
  current_margin: number | null
  liq_price: number | null
  liq_distance_pct: number | null
}

interface WsPositionMessage {
  type: string
  positions?: PositionRow[]
  data?: PositionRow[]
  account_summary?: AccountSummary
}

interface AccountExchange {
  initial?: number
  capital_used?: number
  margin_used?: number
  floating_value: number
  realized_pnl: number
  fees: number
  available: number
  net_value: number
}

interface AccountTotal {
  initial?: number
  used: number
  floating_pnl: number
  realized_pnl: number
  funding_pnl: number
  fee_cost: number
  total_pnl: number
  fees: number
  available: number
  net_value: number
}

interface AccountSummary {
  binance: AccountExchange
  gate: AccountExchange
  total: AccountTotal
}

/* ───── 状态 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef
const rowData = shallowRef<PositionRow[]>([])
const positionMap = new Map<number, PositionRow>()
let gridApi: GridApi<PositionRow> | null = null
const loading = ref(false)
const statusFilter = ref<string>('')
const baseAssetFilter = ref<string>('')
const filterDays = ref<number>(90) // 默认90天
const wsStatus = ref<'connecting' | 'connected' | 'disconnected'>('disconnected')
const wsLatencyMs = ref<number | null>(null)
const accountSummary = ref<AccountSummary | null>(null)

// 分页配置
const paginationPageSize = ref<number>(100)
const paginationPageSizeOptions = [100, 500, 1000, 5000]
const paginationCurrentPage = ref<number>(1)
const paginationTotal = ref<number>(0)

/** 列状态持久化（数据库版） */
const PAGE_KEY = 'position_monitor'

/** 列选择面板 */
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

/* ───── WebSocket ───── */
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let pingInterval: ReturnType<typeof setInterval> | null = null
/** 页面可见性：隐藏时跳过消息处理和 ping */
let pageVisible = true

function getWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getToken()
  return `${protocol}//${window.location.host}/ws/orderbook?token=${token}&mode=events`
}

function connectWs() {
  if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) {
    return
  }
  wsStatus.value = 'connecting'
  socket = new WebSocket(getWsUrl())

  socket.onopen = () => {
    wsStatus.value = 'connected'
    wsLatencyMs.value = null
    // 每 10 秒发送一次 ping 测量延迟
    if (pingInterval) clearInterval(pingInterval)
    pingInterval = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
      }
    }, 10000)
    socket!.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
  }

  socket.onmessage = (ev) => {
    // 页面隐藏时跳过消息处理，降低 CPU 开销
    if (!pageVisible) return
    try {
      const msg: WsPositionMessage = JSON.parse(ev.data)
      if (msg.type === 'ping') return
      if (msg.type === 'pong' && (msg as any).ts) {
        wsLatencyMs.value = Date.now() - (msg as any).ts
        return
      }
      if (msg.type === 'position_update' && (msg.positions || msg.data)) {
        // 同步标准开仓金额（后端 config.yaml trade.open.amount_usdt）后再走 ensureFundingBps
        const oa = (msg as any).open_amount_usdt
        if (typeof oa === 'number' && oa > 0) openAmountUsdt.value = oa
        applyPositionUpdates(msg.positions || msg.data!)
        // 提取资金汇总
        if ((msg as any).account_summary) {
          accountSummary.value = (msg as any).account_summary
        }
      }
      // 资金费结算后的一次性历史更新事件
      if (msg.type === 'funding_history_update' && (msg as any).funding_histories) {
        applyFundingHistoryUpdate((msg as any).funding_histories)
      }
    } catch {
      // ignore parse errors
    }
  }

  socket.onclose = () => {
    wsStatus.value = 'disconnected'
    wsLatencyMs.value = null
    if (pingInterval) { clearInterval(pingInterval); pingInterval = null }
    scheduleReconnect()
  }

  socket.onerror = () => {
    wsStatus.value = 'disconnected'
    wsLatencyMs.value = null
  }
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectTimer = setTimeout(() => {
    connectWs()
  }, 3000)
}

/** 判断 WS 行是否匹配当前过滤条件，未通过的行不进入 grid，避免 WS 持续推送把已过滤的行带回页面 */
function matchesActiveFilter(row: PositionRow): boolean {
  if (statusFilter.value && row.status !== statusFilter.value) return false
  if (baseAssetFilter.value && row.base_asset !== baseAssetFilter.value) return false
  return true
}

function applyPositionUpdates(updates: PositionRow[]) {
  ensureFundingBps(updates)
  // WS 推送不携带 funding_history（后端为压缩消息体不下发），需保留 REST 初始加载/funding_history_update 已注入的字段，避免被覆盖
  for (const row of updates) {
    if (row.funding_history === undefined) {
      const existing = positionMap.get(row.id)
      if (existing && existing.funding_history != null) {
        row.funding_history = existing.funding_history
      }
    }
  }

  // 仅保留通过当前过滤条件的行（防止 WS 把已被过滤的状态/标的带回 grid）
  const filtered = updates.filter(matchesActiveFilter)

  if (!gridApi) {
    for (const row of filtered) {
      positionMap.set(row.id, row)
    }
    rowData.value = Array.from(positionMap.values())
    return
  }

  const add: PositionRow[] = []
  const update: PositionRow[] = []
  const remove: PositionRow[] = []

  for (const row of updates) {
    const old = positionMap.get(row.id)
    const passes = matchesActiveFilter(row)
    if (!passes) {
      // 已不匹配过滤条件：从 grid 中移除（例如某行从 holding → closed，而当前选了 holding）
      if (old) {
        remove.push(old)
        positionMap.delete(row.id)
      }
      continue
    }
    if (!old) {
      add.push(row)
    } else {
      update.push(row)
    }
    positionMap.set(row.id, row)
  }

  if (add.length > 0 || update.length > 0 || remove.length > 0) {
    gridApi.applyTransaction({ add, update, remove })
    // 同步更新 rowData，触发 pinnedBottomRowData 重新计算
    rowData.value = Array.from(positionMap.values())
  }
}

/**
 * 处理资金费结算历史更新事件（仅在结算后推送一次）
 * 将 funding_histories 按 position_id 分配到对应行
 */
function applyFundingHistoryUpdate(histories: Record<number, any[]>) {
  if (!histories) return
  let changed = false
  for (const [pidStr, history] of Object.entries(histories)) {
    const pid = Number(pidStr)
    const row = positionMap.get(pid)
    if (row) {
      row.funding_history = history
      changed = true
    }
  }
  if (changed && gridApi) {
    // 触发列刷新
    gridApi.refreshCells({ columns: ['funding_history'] })
    rowData.value = Array.from(positionMap.values())
  }
}

/* ───── 格式化 ───── */
function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  if (value == null || !Number.isFinite(value)) return ''
  if (Number.isInteger(value)) return String(value)
  return value.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

const priceFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return formatDecimal(params.value)
}

const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toFixed(2)
}

const pnlFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toFixed(4)
}

const percentFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return (Number(params.value) * 100).toFixed(4) + '%'
}

const fundingIntervalFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  const value = Number(params.value)
  if (!Number.isFinite(value)) return ''
  return Number.isInteger(value) ? `${value}h` : `${value.toFixed(2)}h`
}

const intFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return String(params.value)
}

const timeFormatter = (params: ValueFormatterParams) => {
  if (!params.value) return ''
  const d = new Date(params.value)
  if (isNaN(d.getTime())) return params.value
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const hour = String(d.getHours()).padStart(2, '0')
  const minute = String(d.getMinutes()).padStart(2, '0')
  const second = String(d.getSeconds()).padStart(2, '0')
  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

const statusFormatter = (params: ValueFormatterParams) => {
  const map: Record<string, string> = {
    holding: '持仓中',
    closed: '已平仓',
  }
  return map[params.value] ?? params.value ?? ''
}

const statusCellStyle = (params: ValueFormatterParams) => {
  if (params.value === 'holding') return { color: '#67c23a' }
  if (params.value === 'closed') return { color: '#909399' }
  return { color: '#909399' }
}

const pnlCellStyle = (params: ValueFormatterParams) => {
  const value = params.value as number | null
  if (value == null) return { color: '#909399' }
  if (value > 0) return { color: '#f56c6c' }  // 盈利红色（A股惯例）
  if (value < 0) return { color: '#67c23a' }  // 亏损绿色
  return { color: '#e8eaed' }
}

/* ───── 列定义 ───── */
const columnDefs = computed<ColDef<PositionRow>[]>(() => [
  {
    headerName: '开仓时间',
    field: 'opened_at',
    width: 180,
    valueFormatter: timeFormatter,
  },
  {
    headerName: '标的资产',
    field: 'base_asset',
    width: 100,
    pinned: 'left',
  },
  {
    headerName: '现货',
    field: 'spot_symbol',
    width: 100,
  },
  {
    headerName: '期货',
    field: 'future_contract',
    width: 110,
  },
  {
    headerName: '状态',
    field: 'status',
    width: 80,
    valueFormatter: statusFormatter,
    cellStyle: statusCellStyle,
  },
  {
    headerName: '现货开仓VWAP',
    field: 'spot_open_price',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '期货开仓VWAP',
    field: 'future_open_price',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '开仓VWAP基差(bps)',
    field: 'open_spread_bps',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '现货平仓VWAP',
    field: 'current_spot_price',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '合约平仓VWAP',
    field: 'current_future_price',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '实时平仓VWAP基差(bps)',
    field: 'current_spread_bps',
    width: 140,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '实时浮动盈亏',
    field: 'floating_pnl_total',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '实时浮动盈亏(bps)',
    field: 'floating_pnl_bps',
    width: 140,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '费率(bps)',
    field: 'fee_bps',
    width: 100,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '风险缓释(bps)',
    field: 'risk_relief_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '实时24h资金费率',
    field: 'funding_rate_24h',
    width: 135,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: percentFormatter,
    cellStyle: (params: any) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      if (value < 0) return { color: '#f56c6c' }
      return { color: '#67c23a' }
    },
  },
  {
    headerName: '单次资金费率',
    field: 'funding_rate',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: percentFormatter,
  },
  {
    headerName: '资金费间隔',
    field: 'funding_interval_hours',
    width: 105,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: fundingIntervalFormatter,
  },
  {
    headerName: '上次支付时间',
    field: 'funding_last_apply',
    width: 170,
    valueFormatter: timeFormatter,
  },
  {
    headerName: '下次支付时间',
    field: 'funding_next_apply',
    width: 170,
    valueFormatter: timeFormatter,
  },
  {
    headerName: '资金费收益(bps)',
    field: 'funding_pnl_bps',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '资金费收益',
    field: 'funding_total_pnl',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '资金费次数',
    field: 'funding_payments_count',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: intFormatter,
  },
  {
    headerName: '资金费明细',
    field: 'funding_history',
    width: 130,
    tooltipComponent: FundingHistoryTooltip,
    tooltipValueGetter: (params: any) => params.data?.funding_history,
    valueFormatter: (params: ValueFormatterParams) => {
      const history = params.value as any[] | null
      if (!history || history.length === 0) return '—'
      const totalPnl = history.reduce((sum: number, item: any) => sum + (item.pnl || 0), 0)
      return `${history.length}次 / ${totalPnl.toFixed(4)}`
    },
  },
  {
    headerName: '已实现盈亏(bps)',
    field: 'realized_pnl_bps',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '已实现盈亏',
    field: 'realized_pnl',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '手续费',
    field: 'fee_cost',
    width: 100,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '总盈亏(bps)',
    field: 'total_pnl_bps',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '总盈亏',
    field: 'total_pnl',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '追加次数',
    field: 'margin_topup_count',
    width: 100,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (params: ValueFormatterParams) => {
      if (params.value == null) return '0'
      return String(params.value)
    },
  },
  {
    headerName: '追加金额',
    field: 'margin_topup_total',
    width: 110,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
  },
  {
    headerName: '当前保证金',
    field: 'current_margin',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
  },
  {
    headerName: '平仓基差(bps)',
    field: 'close_spread_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '爆仓价',
    field: 'liq_price',
    width: 110,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '距爆仓(%)',
    field: 'liq_distance_pct',
    width: 110,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (params: ValueFormatterParams) => {
      if (params.value == null) return ''
      return Number(params.value).toFixed(2) + '%'
    },
    cellStyle: (params: any) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      if (value > 8) return { color: '#67c23a' }       // > warning_pct: 绿色(安全)
      if (value > 5) return { color: '#e6a23c' }       // warning ~ close: 橙色(警告)
      return { color: '#f56c6c' }                       // < close_threshold: 红色(危险)
    },
  },
  {
    headerName: '开仓原因',
    field: 'open_reason',
    width: 280,
    tooltipField: 'open_reason',
    tooltipComponent: LongTextTooltip,
  },
  {
    headerName: '平仓原因',
    field: 'close_reason',
    width: 280,
    tooltipField: 'close_reason',
    tooltipComponent: LongTextTooltip,
  },
  {
    headerName: '平仓时间',
    field: 'closed_at',
    width: 180,
    valueFormatter: timeFormatter,
  },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

/** 默认排序：开仓时间降序 */
const initialSortModel = [{ colId: 'opened_at', sort: 'desc' as const }]

const localeText = {
  generalMenuTab: '常规',
  filterMenuTab: '筛选',
  columnsMenuTab: '列',
  pinColumn: '固定列',
  autosizeThisColumn: '自适应列',
  autosizeAllColumns: '全部列自适应',
  resetColumns: '重置列',
  expandAll: '展开所有行',
  contractAll: '折叠所有行',
}

const getRowId = (params: GetRowIdParams<PositionRow>) =>
  String(params.data?.id ?? '')

const getRowClass = (params: any) => {
  const count = Number(params.data?.margin_topup_count || 0)
  return count > 0 ? 'position-row-topup' : ''
}

/* ───── 外部过滤 ───── */
function isExternalFilterPresent(): boolean {
  return statusFilter.value !== '' || baseAssetFilter.value !== ''
}

function doesExternalFilterPass(params: any): boolean {
  const data = params.data as PositionRow
  if (statusFilter.value && data?.status !== statusFilter.value) return false
  if (baseAssetFilter.value && data?.base_asset !== baseAssetFilter.value) return false
  return true
}

/* ───── 过滤后的数据（用于汇总行） ───── */
/** 从当前数据中提取唯一标的资产列表，供下拉框选择 */
const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map(r => r.base_asset).filter(Boolean) as string[])
  return Array.from(assets).sort()
})

const filteredRows = computed(() => {
  let rows = rowData.value
  if (statusFilter.value) {
    rows = rows.filter(r => r.status === statusFilter.value)
  }
  if (baseAssetFilter.value) {
    rows = rows.filter(r => r.base_asset === baseAssetFilter.value)
  }
  return rows
})

/* ───── 汇总统计 ───── */
const summaryStats = computed(() => {
  const all = filteredRows.value
  const holdingCount = all.filter((r) => r.status === 'holding').length
  const closedCount = all.filter((r) => r.status === 'closed').length
  const totalCount = all.length
  const totalFloatingPnl = all.reduce(
    (sum, r) => sum + (r.floating_pnl_total ?? 0),
    0,
  )
  const totalRealizedPnl = all.reduce(
    (sum, r) => sum + (r.realized_pnl ?? 0),
    0,
  )
  const totalFundingPnl = all.reduce(
    (sum, r) => sum + (r.funding_total_pnl ?? 0),
    0,
  )
  const totalFees = all.reduce(
    (sum, r) => sum + (r.fee_cost ?? 0),
    0,
  )
  const totalPnl = all.reduce(
    (sum, r) => sum + (r.total_pnl ?? 0),
    0,
  )
  return {
    holdingCount,
    closedCount,
    totalCount,
    totalFloatingPnl,
    totalRealizedPnl,
    totalFundingPnl,
    totalFees,
    totalPnl,
  }
})

function formatPnl(value: number): string {
  const prefix = value >= 0 ? '+' : ''
  return prefix + value.toFixed(2)
}

function formatAmount(value: number | undefined | null): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/* ───── pinned 汇总行 ───── */
const pinnedBottomRowData = computed<PositionRow[]>(() => {
  const rows = filteredRows.value
  if (rows.length === 0) return []

  const sumField = (field: keyof PositionRow): number =>
    rows.reduce((acc, r) => acc + ((r[field] as number | null | undefined) ?? 0), 0)

  const totalOpenAmount = rows.reduce((acc, r) => {
    const amount = Number(r.spot_open_amount)
    return acc + (Number.isFinite(amount) && amount > 0 ? amount : openAmountUsdt.value)
  }, 0)
  const toPortfolioBps = (amount: number): number | null =>
    totalOpenAmount > 0 ? Math.round((amount / totalOpenAmount) * 10000 * 100) / 100 : null

  const floatingPnlTotal = sumField('floating_pnl_total')
  const realizedPnl = sumField('realized_pnl')
  const fundingTotalPnl = sumField('funding_total_pnl')
  const totalPnl = sumField('total_pnl')

  return [{
    id: -1,
    base_asset: '汇总',
    opened_at: null,
    closed_at: null,
    spot_symbol: null,
    future_contract: null,
    status: null,
    open_reason: null,
    close_reason: null,
    spot_open_price: null,
    future_open_price: null,
    open_spread_bps: null,
    close_spread_bps: null,
    current_spot_price: null,
    current_future_price: null,
    current_spread_bps: null,
    spot_open_amount: totalOpenAmount,
    floating_pnl_bps: toPortfolioBps(floatingPnlTotal),
    floating_pnl_total: floatingPnlTotal,
    fee_bps: null,
    fee_cost: sumField('fee_cost'),
    risk_relief_bps: null,
    funding_pnl_bps: toPortfolioBps(fundingTotalPnl),
    funding_rate: null,
    funding_rate_24h: null,
    funding_interval: null,
    funding_interval_hours: null,
    funding_last_apply: null,
    funding_next_apply: null,
    funding_total_pnl: fundingTotalPnl,
    funding_payments_count: null,
    funding_history: null,
    realized_pnl_bps: toPortfolioBps(realizedPnl),
    realized_pnl: realizedPnl,
    total_pnl_bps: toPortfolioBps(totalPnl),
    total_pnl: totalPnl,
    margin_topup_count: null,
    margin_topup_total: sumField('margin_topup_total'),
    margin_topup_last_at: null,
    margin_initial: sumField('margin_initial'),
    current_margin: sumField('current_margin'),
    liq_price: null,
    liq_distance_pct: null,
  }]
})
async function fetchPositions() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (statusFilter.value) {
      params.set('status', statusFilter.value)
    }
    if (baseAssetFilter.value) {
      params.set('base_asset', baseAssetFilter.value.trim())
    }
    const query = params.toString()
    const url = `/api/trading/positions${query ? '?' + query : ''}`
    const res = await get(url)
    if (!res.ok) {
      showError('获取持仓数据失败')
      return
    }
    const data = await res.json()
    // 同步标准开仓金额（后端 config.yaml trade.open.amount_usdt）后再走 ensureFundingBps
    if (typeof data.open_amount_usdt === 'number' && data.open_amount_usdt > 0) {
      openAmountUsdt.value = data.open_amount_usdt
    }
    const rows: PositionRow[] = data.positions || []
    ensureFundingBps(rows)
    positionMap.clear()
    for (const row of rows) {
      positionMap.set(row.id, row)
    }
    rowData.value = rows
    
    // 更新分页信息
    if (data.pagination) {
      paginationTotal.value = data.pagination.total || 0
    }
  } catch {
    showError('请求持仓数据失败')
  } finally {
    loading.value = false
  }
}

/** 页码变化 */
function onPageChange(page: number) {
  paginationCurrentPage.value = page
  fetchPositions()
}

/** 每页条数变化 */
function onPaginationSizeChange() {
  paginationCurrentPage.value = 1 // 切换每页条数时回到第一页
  fetchPositions()
}

/** 计算总页数 */
const totalPages = computed(() => {
  return Math.ceil(paginationTotal.value / paginationPageSize.value) || 1
})

/** 快捷状态过滤 */
function setStatusFilter(status: string) {
  statusFilter.value = status
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  // 通知 AG Grid 外部过滤器状态已变更（否则 WS 后续推送的行不会被重新评估）
  gridApi?.onFilterChanged()
  fetchPositions()
}

/** 快捷时间过滤 */
function setDaysFilter(days: number) {
  filterDays.value = days
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchPositions()
}

/** 标的资产过滤变更：同样需要通知 grid 外部过滤器状态变更 */
function onBaseAssetFilterChange() {
  paginationCurrentPage.value = 1
  gridApi?.onFilterChanged()
  fetchPositions()
}

/* ───── 列选择面板 ───── */
function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field)
    .map((col) => {
      const state = states.find((s) => s.colId === (col.field ?? col.colId))
      return {
        colId: (col.field ?? col.colId) as string,
        headerName: col.headerName ?? (col.field ?? ''),
        visible: state?.hide !== true,
      }
    })
}

function toggleColumnVisibility(colId: string, visible: boolean) {
  if (!gridApi) return
  gridApi.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) col.visible = visible
}

/** 保存列配置到数据库 */
async function saveColumnState() {
  if (!gridApi) return
  const columnState = gridApi.getColumnState()
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, { columnState })
    const data = await res.json()
    if (data?.success) {
      showSuccess('列配置已保存')
    } else {
      showError(data?.message || '保存列配置失败')
    }
  } catch (e) {
    showError('保存列配置失败')
  }
}

/** 从数据库加载列配置 */
async function loadColumnState() {
  if (!gridApi) return
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEY}`)
    const data = await res.json()
    if (data?.columnState && Array.isArray(data.columnState)) {
      gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch (e) {
    console.warn('Failed to load column config from server:', e)
  }
}

/* ───── AG Grid 回调 ───── */
function onGridReady(params: GridReadyEvent<PositionRow>) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)
}

/* ───── 生命周期 ───── */
function handleVisibilityChange() {
  pageVisible = !document.hidden
  if (pageVisible) {
    // 恢复可见时重启 ping
    if (pingInterval) clearInterval(pingInterval)
    pingInterval = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
      }
    }, 10000)
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
    }
  } else {
    // 隐藏时停止 ping
    if (pingInterval) { clearInterval(pingInterval); pingInterval = null }
  }
}

onMounted(() => {
  fetchPositions()
  connectWs()
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (pingInterval) clearInterval(pingInterval)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  socket?.close()
  socket = null
})
</script>

<template>
  <div class="monitor-page">
    <!-- 汇总统计栏 -->
    <div class="summary-bar">
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">持仓中</span>
          <span class="summary-value">{{ summaryStats.holdingCount }}</span>
        </span>
        <span class="summary-divider">/</span>
        <span class="summary-item">
          <span class="summary-label">已平仓</span>
          <span class="summary-value">{{ summaryStats.closedCount }}</span>
        </span>
        <span class="summary-divider">/</span>
        <span class="summary-item">
          <span class="summary-label">总持仓</span>
          <span class="summary-value">{{ summaryStats.totalCount }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">实时浮动盈亏</span>
          <span class="summary-value" :class="summaryStats.totalFloatingPnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalFloatingPnl) }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">已实现盈亏</span>
          <span class="summary-value" :class="summaryStats.totalRealizedPnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalRealizedPnl) }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">累计资金费</span>
          <span class="summary-value" :class="summaryStats.totalFundingPnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalFundingPnl) }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">手续费</span>
          <span class="summary-value" :class="summaryStats.totalFees >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalFees) }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">总盈亏</span>
          <span class="summary-value" :class="summaryStats.totalPnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalPnl) }}</span>
        </span>
      </div>
    </div>

    <!-- 资金汇总栏 -->
    <div v-if="accountSummary" class="capital-bar">
      <div class="capital-section">
        <span class="capital-title">Binance</span>
        <span class="capital-item">
          <span class="capital-label">资金占用</span>
          <span class="capital-value">{{ formatAmount(accountSummary.binance.capital_used) }}</span>
        </span>
        <span class="capital-item">
          <span class="capital-label">可用</span>
          <span class="capital-value">{{ formatAmount(accountSummary.binance.available) }}</span>
        </span>
        <span class="capital-item">
          <span class="capital-label">净值</span>
          <span class="capital-value">{{ formatAmount(accountSummary.binance.net_value) }}</span>
        </span>
      </div>
      <div class="capital-divider"></div>
      <div class="capital-section">
        <span class="capital-title">Gate</span>
        <span class="capital-item">
          <span class="capital-label">保证金占用</span>
          <span class="capital-value">{{ formatAmount(accountSummary.gate.margin_used) }}</span>
        </span>
        <span class="capital-item">
          <span class="capital-label">可用</span>
          <span class="capital-value">{{ formatAmount(accountSummary.gate.available) }}</span>
        </span>
        <span class="capital-item">
          <span class="capital-label">净值</span>
          <span class="capital-value">{{ formatAmount(accountSummary.gate.net_value) }}</span>
        </span>
      </div>
      <div class="capital-divider"></div>
      <div class="capital-section">
        <span class="capital-title">合计</span>
        <span class="capital-item">
          <span class="capital-label">总占用</span>
          <span class="capital-value">{{ formatAmount(accountSummary.total.used) }}</span>
        </span>
        <span class="capital-item">
          <span class="capital-label">总可用</span>
          <span class="capital-value">{{ formatAmount(accountSummary.total.available) }}</span>
        </span>
        <span class="capital-item">
          <span class="capital-label">总净值</span>
          <span class="capital-value" :class="accountSummary.total.total_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatAmount(accountSummary.total.net_value) }}</span>
        </span>
      </div>
    </div>

    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">状态：</span>
        <el-button-group size="small">
          <el-button :type="statusFilter === '' ? 'primary' : 'default'" @click="setStatusFilter('')">全部</el-button>
          <el-button :type="statusFilter === 'holding' ? 'primary' : 'default'" @click="setStatusFilter('holding')">持仓中</el-button>
          <el-button :type="statusFilter === 'closed' ? 'primary' : 'default'" @click="setStatusFilter('closed')">已平仓</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">时间：</span>
        <el-button-group size="small">
          <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="setDaysFilter(7)">7天</el-button>
          <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="setDaysFilter(30)">30天</el-button>
          <el-button :type="filterDays === 90 ? 'primary' : 'default'" @click="setDaysFilter(90)">90天</el-button>
          <el-button :type="filterDays === 365 ? 'primary' : 'default'" @click="setDaysFilter(365)">1年</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">标的：</span>
        <el-select
          v-model="baseAssetFilter"
          placeholder="标的资产"
          size="small"
          filterable
          clearable
          style="width: 150px;"
          @change="onBaseAssetFilterChange"
        >
          <el-option
            v-for="asset in assetOptions"
            :key="asset"
            :label="asset"
            :value="asset"
          />
        </el-select>

        <el-button
          size="small"
          type="primary"
          style="margin-left: auto;"
          :loading="loading"
          @click="fetchPositions"
        >
          刷新
        </el-button>

        <span class="filter-label" style="margin-left: auto;">
          WS：
          <el-tag v-if="wsStatus === 'connected'" type="success" size="small">
            {{ wsLatencyMs != null ? `${wsLatencyMs}ms` : '已连接' }}
          </el-tag>
          <el-tag v-else :type="wsStatus === 'connecting' ? 'warning' : 'danger'" size="small">
            {{ wsStatus === 'connecting' ? '连接中' : '已断开' }}
          </el-tag>
        </span>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <span>持仓监控</span>
          <div class="header-actions">
            <el-popover
              placement="bottom-end"
              :width="260"
              trigger="click"
              @before-enter="refreshColumnVisibilities"
            >
              <template #reference>
                <el-button size="small">列选择</el-button>
              </template>
              <div class="column-picker">
                <div
                  v-for="col in columnVisibilities"
                  :key="col.colId"
                  class="column-picker-item"
                >
                  <el-checkbox
                    :model-value="col.visible"
                    @change="(val: boolean | string | number) => toggleColumnVisibility(col.colId, !!val)"
                  />
                  <span class="column-picker-label">{{ col.headerName }}</span>
                </div>
              </div>
            </el-popover>
            <el-button size="small" @click="saveColumnState">
              保存列配置
            </el-button>
          </div>
        </div>
      </template>
      <div ref="gridContainerRef">
      <ag-grid-vue
        class="orderbook-grid"
        :theme="orderbookGridTheme"
        :columnDefs="columnDefs"
        :rowData="rowData"
        :pinnedBottomRowData="pinnedBottomRowData"
        :defaultColDef="defaultColDef"
        :initialState="{ sort: { sortModel: initialSortModel } }"
        :getRowId="getRowId"
        :getRowClass="getRowClass"
        :header-height="32"
        :row-height="32"
        :localeText="localeText"
        :tooltipShowDelay="300"
        :isExternalFilterPresent="isExternalFilterPresent"
        :doesExternalFilterPass="doesExternalFilterPass"
        @grid-ready="onGridReady"
      />
      </div>
    </el-card>

    <!-- 底部分页控件 -->
    <div class="pagination-bar">
      <div class="pagination-info">
        共 {{ paginationTotal }} 条记录，第 {{ paginationCurrentPage }} / {{ totalPages }} 页
      </div>
      <div class="pagination-controls">
        <el-button
          size="small"
          :disabled="paginationCurrentPage === 1"
          @click="onPageChange(paginationCurrentPage - 1)"
        >
          上一页
        </el-button>
        <el-select
          v-model="paginationPageSize"
          size="small"
          style="width: 100px; margin: 0 8px"
          @change="onPaginationSizeChange"
        >
          <el-option
            v-for="size in paginationPageSizeOptions"
            :key="size"
            :label="`${size}条/页`"
            :value="size"
          />
        </el-select>
        <el-button
          size="small"
          :disabled="paginationCurrentPage === totalPages"
          @click="onPageChange(paginationCurrentPage + 1)"
        >
          下一页
        </el-button>
        <el-input-number
          v-model="paginationCurrentPage"
          :min="1"
          :max="totalPages"
          size="small"
          style="width: 100px; margin-left: 8px"
          @change="onPageChange"
          controls-position="right"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.monitor-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ───── 汇总栏 ───── */
.summary-bar {
  display: flex;
  align-items: center;
  gap: 24px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 10px 18px;
  flex-wrap: wrap;
}

.summary-group {
  display: flex;
  align-items: center;
  gap: 4px;
}

.summary-divider {
  color: var(--app-text-muted);
  font-size: 12px;
  margin: 0 2px;
  opacity: 0.5;
}

.summary-item {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
}

.summary-label {
  font-size: 12px;
  color: var(--app-text-muted);
}

.summary-value {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
}

.summary-value.pnl-positive {
  color: #f56c6c;
}

.summary-value.pnl-negative {
  color: #67c23a;
}

/* ───── 过滤栏 ───── */
.status-card :deep(.el-card__body) {
  padding: 12px 16px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-label {
  color: var(--app-text-muted);
  font-size: 13px;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.grid-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.orderbook-grid {
  width: 100%;
  height: calc(100vh - 340px);
  min-height: 420px;
}

.orderbook-grid :deep(.position-row-topup) {
  background-color: rgba(230, 162, 60, 0.08);
}

/* 列选择面板 */
.column-picker {
  max-height: 400px;
  overflow-y: auto;
  padding: 4px 8px;
  margin-right: 4px;
}

.column-picker-item {
  display: flex;
  align-items: center;
  padding: 6px 4px;
  gap: 8px;
}

.column-picker-item :deep(.el-checkbox) {
  flex-shrink: 0;
  margin-right: 4px;
}

.column-picker-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

/* ───── 资金汇总栏 ───── */
.capital-bar {
  display: flex;
  align-items: center;
  gap: 0;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 8px 18px;
  flex-wrap: wrap;
}

.capital-section {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
}

.capital-section:first-child {
  padding-left: 0;
}

.capital-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
  margin-right: 4px;
}

.capital-item {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
}

.capital-label {
  font-size: 11px;
  color: var(--app-text-muted);
}

.capital-value {
  font-size: 13px;
  font-weight: 500;
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
}

.capital-value.pnl-positive {
  color: #f56c6c;
}

.capital-value.pnl-negative {
  color: #67c23a;
}

.capital-value.fee-value {
  color: #e6a23c;
}

.capital-divider {
  width: 1px;
  height: 20px;
  background: var(--app-border);
  margin: 0 4px;
}

/* 底部分页控件 */
.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 0;
  gap: 16px;
}

.pagination-info {
  font-size: 13px;
  color: var(--el-text-color-secondary, #909399);
  white-space: nowrap;
}

.pagination-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
