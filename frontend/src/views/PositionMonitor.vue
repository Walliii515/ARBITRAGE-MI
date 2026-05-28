<script setup lang="ts">
import { ref, shallowRef, onMounted, onUnmounted, computed } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
  ColumnState,
} from 'ag-grid-community'
import { ElPopover } from 'element-plus'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { get } from '../utils/request'
import { getToken } from '../utils/auth'

/** 开仓金额，与后端 config.yaml trade.open_amount_usdt 保持一致，用于前端兜底计算 funding_pnl_bps */
const OPEN_AMOUNT_USDT = 500

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
  floating_pnl_total: number | null
  floating_pnl_bps: number | null
  fee_bps: number | null
  risk_relief_bps: number | null
  funding_pnl_bps: number | null
  funding_rate_24h: number | null
  funding_next_apply: string | null
  funding_total_pnl: number | null
  funding_payments_count: number | null
  realized_pnl_bps: number | null
  realized_pnl: number | null
  total_pnl_bps: number | null
  total_pnl: number | null
}

interface WsPositionMessage {
  type: string
  positions?: PositionRow[]
  data?: PositionRow[]
}

/* ───── 状态 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()
const rowData = shallowRef<PositionRow[]>([])
const positionMap = new Map<number, PositionRow>()
let gridApi: GridApi<PositionRow> | null = null
const loading = ref(false)
const statusFilter = ref<string>('')
const wsStatus = ref<'connecting' | 'connected' | 'disconnected'>('disconnected')

/** 列状态持久化 */
const COLUMN_STATE_STORAGE_KEY = 'position_monitor_column_state'

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

function getWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getToken()
  return `${protocol}//${window.location.host}/ws/orderbook?token=${token}`
}

function connectWs() {
  if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) {
    return
  }
  wsStatus.value = 'connecting'
  socket = new WebSocket(getWsUrl())

  socket.onopen = () => {
    wsStatus.value = 'connected'
  }

  socket.onmessage = (ev) => {
    try {
      const msg: WsPositionMessage = JSON.parse(ev.data)
      if (msg.type === 'ping') return
      if (msg.type === 'position_update' && (msg.positions || msg.data)) {
        applyPositionUpdates(msg.positions || msg.data!)
      }
    } catch {
      // ignore parse errors
    }
  }

  socket.onclose = () => {
    wsStatus.value = 'disconnected'
    scheduleReconnect()
  }

  socket.onerror = () => {
    wsStatus.value = 'disconnected'
  }
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectTimer = setTimeout(() => {
    connectWs()
  }, 3000)
}

function applyPositionUpdates(updates: PositionRow[]) {
  // WS 推送的数据也可能缺少 funding_pnl_bps（WS断连时后端未计算），兜底计算
  for (const row of updates) {
    if (row.funding_pnl_bps == null && row.funding_total_pnl != null) {
      row.funding_pnl_bps = Math.round(row.funding_total_pnl / OPEN_AMOUNT_USDT * 10000 * 100) / 100
    }
  }
  if (!gridApi) {
    for (const row of updates) {
      positionMap.set(row.id, row)
    }
    rowData.value = Array.from(positionMap.values())
    return
  }

  const add: PositionRow[] = []
  const update: PositionRow[] = []

  for (const row of updates) {
    const old = positionMap.get(row.id)
    if (!old) {
      add.push(row)
    } else {
      update.push(row)
    }
    positionMap.set(row.id, row)
  }

  if (add.length > 0 || update.length > 0) {
    gridApi.applyTransaction({ add, update })
    // 同步更新 rowData，触发 pinnedBottomRowData 重新计算
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
    headerName: '当前24h资金费率',
    field: 'funding_rate_24h',
    width: 120,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p: ValueFormatterParams) => p.value != null ? (p.value * 100).toFixed(4) + '%' : '',
    cellStyle: (params: any) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      if (value < 0) return { color: '#f56c6c' }
      return { color: '#67c23a' }
    },
  },
  {
    headerName: '下次支付时间',
    field: 'funding_next_apply',
    width: 160,
    valueFormatter: (p: ValueFormatterParams) => {
      if (!p.value) return '—'
      const d = new Date(p.value)
      if (isNaN(d.getTime())) return p.value
      const year = d.getFullYear()
      const month = String(d.getMonth() + 1).padStart(2, '0')
      const day = String(d.getDate()).padStart(2, '0')
      const hour = String(d.getHours()).padStart(2, '0')
      const minute = String(d.getMinutes()).padStart(2, '0')
      return `${year}-${month}-${day} ${hour}:${minute}`
    },
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
    headerName: '平仓基差(bps)',
    field: 'close_spread_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
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

/* ───── 外部过滤 ───── */
function isExternalFilterPresent(): boolean {
  return statusFilter.value !== ''
}

function doesExternalFilterPass(params: any): boolean {
  if (!statusFilter.value) return true
  const data = params.data as PositionRow
  return data?.status === statusFilter.value
}

/* ───── 汇总统计 ───── */
const summaryStats = computed(() => {
  const all = rowData.value
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
    totalPnl,
  }
})

function formatPnl(value: number): string {
  const prefix = value >= 0 ? '+' : ''
  return prefix + value.toFixed(2)
}

/* ───── pinned 汇总行 ───── */
const pinnedBottomRowData = computed<PositionRow[]>(() => {
  const rows = rowData.value
  if (rows.length === 0) return []

  const sumField = (field: keyof PositionRow): number =>
    rows.reduce((acc, r) => acc + ((r[field] as number | null | undefined) ?? 0), 0)

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
    floating_pnl_bps: null,
    floating_pnl_total: sumField('floating_pnl_total'),
    fee_bps: null,
    risk_relief_bps: null,
    funding_pnl_bps: null,
    funding_rate_24h: null,
    funding_next_apply: null,
    funding_total_pnl: sumField('funding_total_pnl'),
    funding_payments_count: null,
    realized_pnl_bps: null,
    realized_pnl: sumField('realized_pnl'),
    total_pnl_bps: null,
    total_pnl: sumField('total_pnl'),
  }]
})
async function fetchPositions() {
  loading.value = true
  try {
    const res = await get('/api/trading/positions')
    if (!res.ok) {
      showError('获取持仓数据失败')
      return
    }
    const data = await res.json()
    const rows: PositionRow[] = Array.isArray(data) ? data : (data.positions ?? [])
    // REST 返回的原始数据可能缺少 funding_pnl_bps（非DB字段），在此兜底计算
    for (const row of rows) {
      if (row.funding_pnl_bps == null && row.funding_total_pnl != null) {
        row.funding_pnl_bps = Math.round(row.funding_total_pnl / OPEN_AMOUNT_USDT * 10000 * 100) / 100
      }
    }
    positionMap.clear()
    for (const row of rows) {
      positionMap.set(row.id, row)
    }
    rowData.value = rows
  } catch {
    showError('请求持仓数据失败')
  } finally {
    loading.value = false
  }
}

/* ───── 状态过滤选项 ───── */
const statusOptions = [
  { label: '全部', value: '' },
  { label: '持仓中', value: 'holding' },
  { label: '已平仓', value: 'closed' },
]

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

function saveColumnState() {
  if (!gridApi) return
  const columnState = gridApi.getColumnState()
  localStorage.setItem(COLUMN_STATE_STORAGE_KEY, JSON.stringify(columnState))
  showSuccess('列配置已保存')
}

function loadColumnState() {
  if (!gridApi) return
  const saved = localStorage.getItem(COLUMN_STATE_STORAGE_KEY)
  if (!saved) return
  try {
    const columnState = JSON.parse(saved) as ColumnState[]
    gridApi.applyColumnState({ state: columnState, applyOrder: true })
  } catch {
    // ignore parse errors
  }
}

/* ───── AG Grid 回调 ───── */
function onGridReady(params: GridReadyEvent<PositionRow>) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)
}

/* ───── 生命周期 ───── */
onMounted(() => {
  fetchPositions()
  connectWs()
})

onUnmounted(() => {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  // WebSocket 保持活跃，不主动关闭
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
          <span class="summary-label">总盈亏</span>
          <span class="summary-value" :class="summaryStats.totalPnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalPnl) }}</span>
        </span>
      </div>
    </div>

    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">状态过滤：</span>
        <el-radio-group v-model="statusFilter" size="small" @change="() => gridApi?.onFilterChanged()">
          <el-radio-button
            v-for="opt in statusOptions"
            :key="opt.value"
            :value="opt.value"
          >
            {{ opt.label }}
          </el-radio-button>
        </el-radio-group>

        <el-button
          size="small"
          type="primary"
          style="margin-left: 16px;"
          :loading="loading"
          @click="fetchPositions"
        >
          刷新
        </el-button>

        <span class="filter-label" style="margin-left: auto;">
          WS：
          <el-tag :type="wsStatus === 'connected' ? 'success' : wsStatus === 'connecting' ? 'warning' : 'danger'" size="small">
            {{ wsStatus === 'connected' ? '已连接' : wsStatus === 'connecting' ? '连接中' : '已断开' }}
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
        :getRowId="getRowId"
        :header-height="32"
        :row-height="32"
        :tooltipShowDelay="300"
        :isExternalFilterPresent="isExternalFilterPresent"
        :doesExternalFilterPass="doesExternalFilterPass"
        @grid-ready="onGridReady"
      />
      </div>
    </el-card>
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
</style>
