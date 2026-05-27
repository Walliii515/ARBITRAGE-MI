<script setup lang="ts">
import { ref, shallowRef, onMounted, onUnmounted, computed, watch } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  RowSelectionOptions,
  ValueFormatterParams,
  ColumnState,
} from 'ag-grid-community'
import { ElDrawer } from 'element-plus'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess, showWarning } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import type { OrderBookRow } from './orderbookTypes'

interface WsMessage {
  type: string
  server_time?: string
  rows?: OrderBookRow[]
  state?: ServiceStatus['state']
  error?: string | null
  gate_ws_connected?: boolean
  binance_ws_connected?: boolean
  snapshot?: ProgressInfo
  subscribe?: ProgressInfo
  funding_threshold_percentile?: string
  orderbook_coverage_threshold?: number
  risk_relief_bps?: number
  open_vwap_basis_threshold_bps?: number
}

interface ProgressInfo {
  current: number
  total: number
  percent: number
}

interface ServiceStatus {
  state: 'idle' | 'starting' | 'running' | 'stopping' | 'error'
  error: string | null
  gate_ws_connected: boolean
  binance_ws_connected: boolean
  snapshot: ProgressInfo
  subscribe: ProgressInfo
  contracts: string[]
  spot_symbols: string[]
}

const rowData = shallowRef<OrderBookRow[]>([])
/** 上一帧快照索引，供 diff → applyTransaction */
const rowsByContract = new Map<string, OrderBookRow>()
let gridApi: GridApi<OrderBookRow> | null = null
const wsStatus = ref<'connecting' | 'connected' | 'disconnected'>('disconnected')
const lastUpdate = ref('--')
const gateWsConnected = ref(false)
const binanceWsConnected = ref(false)
const contracts = ref<string[]>([])
const spotSymbols = ref<string[]>([])

/** 列状态持久化 */
const COLUMN_STATE_STORAGE_KEY = 'orderbook_column_state'

/** 列选择面板：当前列的可见性快照 */
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

const { gridContainerRef, setupGridCopy } = useGridCopy()

/** 打开列选择面板时，刷新列可见性快照 */
function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field && col.field !== 'actions')
    .map((col) => {
      const state = states.find((s) => s.colId === (col.field ?? col.colId))
      return {
        colId: (col.field ?? col.colId) as string,
        headerName: col.headerName ?? (col.field ?? ''),
        visible: state?.hide !== true,
      }
    })
}

/** 切换列可见性 */
function toggleColumnVisibility(colId: string, visible: boolean) {
  if (!gridApi) return
  gridApi.setColumnsVisible([colId], visible)
  // 更新本地状态
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) {
    col.visible = visible
  }
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

const serviceState = ref<ServiceStatus['state']>('idle')
const serviceError = ref<string | null>(null)
const snapshotProgress = ref<ProgressInfo>({ current: 0, total: 0, percent: 0 })
const subscribeProgress = ref<ProgressInfo>({ current: 0, total: 0, percent: 0 })
const serviceBusy = ref(false)
/** 资金费率阈值百分位字段名，从后端动态获取 */
const fundingThresholdPercentile = ref<string>('percentile_30')
/** 资金费率过滤开关（默认开启） */
const filterByFundingRate = ref<boolean>(true)
/** 盘口覆盖阈值，从后端动态获取 */
const orderbookCoverageThreshold = ref<number>(0.8)
/** 盘口覆盖过滤开关（默认开启） */
const filterByCoverage = ref<boolean>(true)
/** 风险缓释 bps，从后端动态获取 */
const riskReliefBps = ref<number>(10)
/** 开仓边际基差阈值 bps，从后端动态获取 */
const openVwapBasisThresholdBps = ref<number>(0)
/** 开仓边际基差过滤开关（默认开启） */
const filterByMarginalBasis = ref<boolean>(true)

/** AG Grid 外部过滤函数：资金费率过滤 */
function fundingRateFilterFunc(params: any): boolean {
  if (!filterByFundingRate.value) {
    return true // 不过滤，显示所有
  }
  
  const data = params.data as OrderBookRow
  if (!data) return true
  
  const fundingRate = data.funding_rate_24h
  const percentileField = fundingThresholdPercentile.value as keyof OrderBookRow
  const threshold = data[percentileField] as number | null | undefined
  
  // 如果任一值为空，不排除（保持显示）
  if (fundingRate == null || threshold == null) {
    return true
  }
  
  return fundingRate >= threshold
}

/** AG Grid 外部过滤函数：盘口覆盖过滤 */
function coverageFilterFunc(params: any): boolean {
  if (!filterByCoverage.value) {
    return true // 不过滤，显示所有
  }
  
  const data = params.data as OrderBookRow
  if (!data) return true
  
  const openCoverage = data.open_coverage
  
  // 如果值为空，不排除（保持显示）
  if (openCoverage == null) {
    return true
  }
  
  return openCoverage <= orderbookCoverageThreshold.value
}

/** AG Grid 外部过滤函数：开仓VWAP基差过滤（按标的阈值） */
function marginalBasisFilterFunc(params: any): boolean {
  if (!filterByMarginalBasis.value) {
    return true // 不过滤，显示所有
  }
  
  const data = params.data as OrderBookRow
  if (!data) return true
  
  const openVwapBasis = data.open_vwap_basis_bps as number | null | undefined
  const vwapThreshold = data.vwap_threshold_bps as number | null | undefined
  
  // 有按标的阈值时：基差必须 >= 阈值
  if (openVwapBasis != null && vwapThreshold != null) {
    return openVwapBasis >= vwapThreshold
  }
  
  // 无按标的阈值时回退全局VWAP基差阈值（统一口径，不用边际基差）
  if (openVwapBasis == null) {
    return false
  }
  return openVwapBasis >= openVwapBasisThresholdBps.value
}

/** 组合过滤函数 */
function combinedFilterFunc(params: any): boolean {
  return fundingRateFilterFunc(params) && coverageFilterFunc(params) && marginalBasisFilterFunc(params)
}

/** WebSocket 实例提升到模块级，避免 HMR 时断开 */
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let statusInterval: ReturnType<typeof setInterval> | null = null
/** 标记是否已初始化过，HMR 时复用连接 */
let wsInitialized = false

/** 按实际精度展示数值，去掉多余尾随 0（不固定 2 位小数） */
function formatDecimal(value: number, maxDecimals = 12): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

const priceFormatter = (params: { value: number | null }) => {
  if (params.value == null) return ''
  return formatDecimal(params.value)
}

const volumeFormatter = (params: { value: number | null }) => {
  if (params.value == null) return ''
  return formatDecimal(params.value, 4)
}

const percentFormatter = (params: ValueFormatterParams) => {
  const n = params.value
  if (n == null) return ''
  return (n * 100).toFixed(1) + '%'
}

// 五档盘口抽屉状态
const drawerVisible = ref(false)
const drawerContract = ref<string>('')
/** 版本号：每次 rowsByContract 变更后递增，供 drawerRow computed 感知更新 */
const rowVersion = ref(0)
const drawerRow = computed(() => {
  void rowVersion.value // 建立响应式依赖
  return rowsByContract.get(drawerContract.value) ?? null
})

function openOrderbookDrawer(row: OrderBookRow) {
  drawerContract.value = row.contract
  drawerVisible.value = true
}

function getOrderbookLevels(row: OrderBookRow, exchange: 'future' | 'spot', side: 'bid' | 'ask') {
  const levels = []
  for (let i = 1; i <= 5; i++) {
    const price = (row as Record<string, number | null>)[`${exchange}_price_${side}_${i}`] as number | null
    const volume = (row as Record<string, number | null>)[`${exchange}_volume_${side}_${i}`] as number | null
    const usdt = (row as Record<string, number | null>)[`${exchange}_usdt_${side}_${i}`] as number | null
    levels.push({ level: i, price, volume, usdt })
  }
  return levels
}

function fmtDrawerPrice(v: number | null): string {
  if (v == null) return '—'
  return formatDecimal(v)
}

function fmtDrawerVolume(v: number | null): string {
  if (v == null) return '—'
  return formatDecimal(v, 4)
}

function fmtDrawerUsdt(usdt: number | null | undefined): string {
  if (usdt == null) return '—'
  return usdt.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtTotalUsdt(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtDepthPercent(v: number | null | undefined): string {
  if (v == null) return '—'
  return (v * 100).toFixed(1) + '%'
}

/** 动态生成列定义，根据配置的资金费率阈值百分位字段 */
const columnDefs = computed<ColDef<OrderBookRow>[]>(() => {
  const percentileField = fundingThresholdPercentile.value
  
  return [
    { headerName: '标的资产', field: 'base_asset', pinned: 'left', width: 90 },
    {
      headerName: '开仓金额(USDT)',
      field: 'open_amount_usdt',
      width: 120,
      type: 'numericColumn',
      cellClass: 'ag-right-aligned-cell',
      headerClass: 'ag-right-aligned-header',
      valueFormatter: (params: { value: number | null }) =>
        params.value == null ? '' : formatDecimal(params.value, 2),
    },
    {
      headerName: '现货数量',
      field: 'spot_qty',
      width: 110,
      type: 'numericColumn',
      enableCellChangeFlash: true,
      cellClass: 'ag-right-aligned-cell',
      headerClass: 'ag-right-aligned-header',
      valueFormatter: (p) => volumeFormatter(p),
    },
    {
      headerName: '合约数量',
      field: 'future_qty',
      width: 110,
      type: 'numericColumn',
      enableCellChangeFlash: true,
      cellClass: 'ag-right-aligned-cell',
      headerClass: 'ag-right-aligned-header',
      valueFormatter: (p) => volumeFormatter(p),
    },
    {
      headerName: '24h资金费率',
      field: 'funding_rate_24h',
      width: 120,
      type: 'numericColumn',
      cellClass: 'ag-right-aligned-cell',
      headerClass: 'ag-right-aligned-header',
      valueFormatter: (p) => p.value != null ? (p.value * 100).toFixed(4) + '%' : '',
      cellStyle: (params) => {
        const value = params.value as number | null
        if (value == null) return { color: '#909399' }
        
        const row = params.data
        if (!row) return { color: '#909399' }
        
        const thresholdField = fundingThresholdPercentile.value as keyof OrderBookRow
        const threshold = row[thresholdField] as number | null | undefined
        
        // >= 阈值：绿色
        if (threshold != null && value >= threshold) {
          return { color: '#67c23a' }
        }
        // < 0：红色
        if (value < 0) {
          return { color: '#f56c6c' }
        }
        // 其他：灰色
        return { color: '#909399' }
      },
    },
    {
      headerName: '下次支付时间',
      field: 'funding_next_apply',
      width: 160,
      valueFormatter: (p) => {
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
      headerName: '费率阈值',
      field: percentileField as string,
      width: 110,
      type: 'numericColumn',
      cellClass: 'ag-right-aligned-cell',
      headerClass: 'ag-right-aligned-header',
      valueFormatter: (p) => {
        if (p.value == null) return '—'
        return (Number(p.value) * 100).toFixed(4) + '%'
      },
    },
  {
    headerName: '数据更新时间',
    field: 'meta_update_time',
    width: 140,
    valueFormatter: (p) => {
      if (!p.value) return '—'
      const d = new Date(p.value)
      if (isNaN(d.getTime())) return p.value
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    },
  },
  {
    headerName: '合约24h成交额',
    field: 'volume_24h_settle',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toLocaleString('en-US', { maximumFractionDigits: 0 })
    },
  },
  {
    headerName: '现货24h成交额',
    field: 'quote_volume',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toLocaleString('en-US', { maximumFractionDigits: 0 })
    },
  },
  {
    headerName: '开仓盘口覆盖',
    field: 'open_coverage',
    width: 100,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: percentFormatter,
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      
      // <= 阈值：绿色，否则：灰色
      return value <= orderbookCoverageThreshold.value 
        ? { color: '#67c23a' } 
        : { color: '#909399' }
    },
  },
  {
    headerName: '平仓盘口覆盖',
    field: 'close_coverage',
    width: 100,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: percentFormatter,
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      
      // <= 阈值：绿色，否则：灰色
      return value <= orderbookCoverageThreshold.value 
        ? { color: '#67c23a' } 
        : { color: '#909399' }
    },
  },
  {
    headerName: '现货开仓VWAP',
    field: 'spot_open_vwap',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '现货平仓VWAP',
    field: 'spot_close_vwap',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '合约开仓VWAP',
    field: 'future_open_vwap',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '合约平仓VWAP',
    field: 'future_close_vwap',
    width: 130,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
  },
  {
    headerName: '开仓VWAP基差(bps)',
    field: 'open_vwap_basis_bps',
    width: 140,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toFixed(2)
    },
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      
      const row = params.data
      if (!row) return { color: '#909399' }
      
      const threshold = row.vwap_threshold_bps as number | null | undefined
      if (threshold != null) {
        // 有按标的阈值：>= 阈值绿色，否则红色
        return value >= threshold
          ? { color: '#67c23a' }
          : { color: '#f56c6c' }
      }
      // 无标的级阈值时，用全局兑底阈值做绿/红判断
      return value >= openVwapBasisThresholdBps.value
        ? { color: '#67c23a' }
        : { color: '#f56c6c' }
    },
  },
  {
    headerName: '开仓vwap基差阈值(bps)',
    field: 'vwap_threshold_bps',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value != null) return Number(p.value).toFixed(2)
      // 无标的级阈值时展示全局兑底值（加 * 表示兑底）
      return openVwapBasisThresholdBps.value.toFixed(2) + ' *'
    },
    cellStyle: (params) => {
      // 有标的级阈值：默认颜色；无标的级：用灰色斜体展示兑底阈值
      return params.value == null ? { color: '#909399', fontStyle: 'italic' } : null
    },
  },
  {
    headerName: '平仓VWAP基差(bps)',
    field: 'close_vwap_basis_bps',
    width: 140,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toFixed(2)
    },
  },
  {
    headerName: '开仓费率(bps)',
    field: 'open_fee_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toFixed(2)
    },
  },
  {
    headerName: '平仓费率(bps)',
    field: 'close_fee_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toFixed(2)
    },
  },
  {
    headerName: '风险缓释(bps)',
    field: 'risk_relief_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toFixed(2)
    },
  },
  {
    headerName: '开仓边际基差(bps)',
    field: 'open_marginal_basis_bps',
    width: 150,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    sort: 'desc',
    valueFormatter: (p) => {
      if (p.value == null) return '—'
      return Number(p.value).toFixed(2)
    },
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      
      const row = params.data
      if (!row) return { color: '#909399' }
      
      // 有按标的VWAP阈值时，用open_vwap_basis_bps与vwap_threshold_bps比较
      const vwapBasis = row.open_vwap_basis_bps as number | null | undefined
      const vwapThreshold = row.vwap_threshold_bps as number | null | undefined
      if (vwapBasis != null && vwapThreshold != null) {
        return vwapBasis >= vwapThreshold
          ? { color: '#67c23a' }
          : { color: '#f56c6c' }
      }
      
      // 无按标的阈值时回退全局阈值（统一用 open_vwap_basis_bps 对比）
      return (vwapBasis ?? null) != null && vwapBasis! >= openVwapBasisThresholdBps.value
        ? { color: '#67c23a' }
        : { color: '#f56c6c' }
    },
  },
  {
    headerName: '操作',
    field: 'actions',
    pinned: 'right',
    width: 100,
    sortable: false,
    filter: false,
    cellRenderer: (params: { data: OrderBookRow }) => {
      const btn = document.createElement('button')
      btn.textContent = '5档盘口'
      btn.className = 'ob-drawer-btn'
      btn.addEventListener('click', () => openOrderbookDrawer(params.data))
      return btn
    },
  },
  ]
})

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

const rowSelection: RowSelectionOptions = {
  mode: 'singleRow',
  checkboxes: false,
  enableClickSelection: true,
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
  columns: '列选择',
  pinLeft: '固定左侧',
  pinRight: '固定右侧',
  noPin: '不固定',
  searchPlaceholder: '搜索...',
  selectAll: '全选',
  unselectAll: '全不选',
}

const getRowId = (params: GetRowIdParams<OrderBookRow>) =>
  String(params.data?.contract ?? '')

function normalizeSnapshotRows(rows: unknown): OrderBookRow[] {
  if (!Array.isArray(rows)) return []
  const byContract = new Map<string, OrderBookRow>()
  for (const raw of rows) {
    if (!raw || typeof raw !== 'object') continue
    const row = raw as OrderBookRow
    const contract = row.contract
    if (typeof contract !== 'string' || !contract.trim()) continue
    byContract.set(contract, { ...row, contract })
  }
  return Array.from(byContract.values())
}

function rowChanged(prev: OrderBookRow, next: OrderBookRow): boolean {
  for (const key of Object.keys(next)) {
    if (prev[key] !== next[key]) return true
  }
  return false
}

function diffSnapshotRows(
  prev: Map<string, OrderBookRow>,
  next: OrderBookRow[],
): { add: OrderBookRow[]; update: OrderBookRow[]; remove: OrderBookRow[] } {
  const nextIds = new Set(next.map((r) => r.contract))
  const add: OrderBookRow[] = []
  const update: OrderBookRow[] = []

  for (const row of next) {
    const old = prev.get(row.contract)
    if (!old) add.push(row)
    else if (rowChanged(old, row)) update.push(row)
  }

  const remove: OrderBookRow[] = []
  for (const [contract, row] of prev) {
    if (!nextIds.has(contract)) remove.push(row)
  }
  return { add, update, remove }
}

function syncRowDataFromIndex() {
  rowData.value = Array.from(rowsByContract.values())
}

function clearGridData() {
  const remove = Array.from(rowsByContract.values())
  rowsByContract.clear()
  rowVersion.value++
  rowData.value = []
  if (!gridApi) return
  if (remove.length > 0) {
    gridApi.applyTransaction({ remove })
  } else {
    gridApi.setGridOption('rowData', [])
  }
}

/** 全量快照 → diff 后 applyTransaction，保留滚动位置；仅清空/首次加载时整表重置 */
function applySnapshotRows(rows: unknown, serverTime?: string, forceFull = false) {
  const normalized = normalizeSnapshotRows(rows)
  if (serverTime) lastUpdate.value = serverTime
  if (normalized.length > 0) {
    contracts.value = normalized.map((r) => r.contract)
  }

  if (!gridApi) {
    rowsByContract.clear()
    for (const row of normalized) rowsByContract.set(row.contract, row)
    rowVersion.value++
    syncRowDataFromIndex()
    return
  }

  const needFullReset = forceFull || rowsByContract.size === 0

  if (needFullReset) {
    const remove = Array.from(rowsByContract.values())
    rowsByContract.clear()
    for (const row of normalized) rowsByContract.set(row.contract, row)
    rowVersion.value++
    syncRowDataFromIndex()
    if (remove.length > 0 || normalized.length > 0) {
      gridApi.applyTransaction({ remove, add: normalized })
    }
    return
  }

  const { add, update, remove } = diffSnapshotRows(rowsByContract, normalized)
  if (add.length === 0 && update.length === 0 && remove.length === 0) return

  for (const row of remove) rowsByContract.delete(row.contract)
  for (const row of normalized) rowsByContract.set(row.contract, row)
  rowVersion.value++
  // 增量路径不更新 rowData，避免 Vue :rowData 绑定触发整表重绘导致滚动回顶
  gridApi.applyTransaction({ add, update, remove })
}

async function fetchOrderbookSnapshot(forceFull = false) {
  try {
    const res = await fetch('/api/orderbook/snapshot')
    if (!res.ok) return
    const data = await res.json()
    if (data.rows?.length) {
      applySnapshotRows(data.rows, data.server_time, forceFull)
    }
  } catch {
    /* ignore */
  }
}

const statusTagType = computed(() => {
  if (wsStatus.value === 'connected') return 'success'
  if (wsStatus.value === 'connecting') return 'warning'
  return 'danger'
})

const statusText = computed(() => {
  const map = {
    connecting: '连接中',
    connected: '已连接',
    disconnected: '已断开',
  }
  return map[wsStatus.value]
})

const canStart = computed(
  () => !serviceBusy.value && (serviceState.value === 'idle' || serviceState.value === 'error'),
)
const canStop = computed(
  () =>
    serviceState.value === 'running' ||
    serviceState.value === 'starting' ||
    serviceState.value === 'stopping' ||
    serviceState.value === 'error',
)

const showProgress = computed(
  () =>
    serviceBusy.value ||
    serviceState.value === 'starting' ||
    serviceState.value === 'stopping' ||
    snapshotProgress.value.total > 0,
)

function formatProgress(p: ProgressInfo): string {
  if (p.total <= 0) return '—'
  return `${p.current}/${p.total} (${p.percent}%)`
}

function progressStatus(p: ProgressInfo): '' | 'success' {
  return p.total > 0 && p.percent >= 100 ? 'success' : ''
}

function getWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/orderbook`
}

function connectWs() {
  if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) {
    return
  }

  wsStatus.value = 'connecting'
  socket = new WebSocket(getWsUrl())

  socket.onopen = () => {
    wsStatus.value = 'connected'
    fetchServiceStatus().then(() => {
      if (serviceState.value === 'running' || serviceState.value === 'starting') {
        fetchOrderbookSnapshot()
      }
    })
  }

  socket.onmessage = (ev) => {
    const msg: WsMessage = JSON.parse(ev.data)
    if (msg.type === 'ping') return
    if (msg.type === 'service_progress') {
      if (msg.state) serviceState.value = msg.state
      if (msg.error !== undefined) serviceError.value = msg.error
      if (msg.gate_ws_connected !== undefined) gateWsConnected.value = msg.gate_ws_connected
      if (msg.binance_ws_connected !== undefined) binanceWsConnected.value = msg.binance_ws_connected
      if (msg.snapshot) snapshotProgress.value = msg.snapshot
      if (msg.subscribe) subscribeProgress.value = msg.subscribe
      serviceBusy.value = msg.state === 'starting' || msg.state === 'stopping'
      return
    }
    if (msg.type === 'snapshot' && msg.rows) {
      // 更新资金费率阈值百分位配置
      if (msg.funding_threshold_percentile) {
        fundingThresholdPercentile.value = msg.funding_threshold_percentile
      }
      // 更新盘口覆盖阈值配置
      if (msg.orderbook_coverage_threshold != null) {
        orderbookCoverageThreshold.value = msg.orderbook_coverage_threshold
      }
      // 更新风险缓释配置
      if (msg.risk_relief_bps != null) {
        riskReliefBps.value = msg.risk_relief_bps
      }
      // 更新开仓边际基差阈值配置
      if (msg.open_vwap_basis_threshold_bps != null) {
        openVwapBasisThresholdBps.value = msg.open_vwap_basis_threshold_bps
      }
      applySnapshotRows(msg.rows, msg.server_time ?? '--')
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

function applyServiceStatus(data: ServiceStatus) {
  const prevState = serviceState.value
  serviceState.value = data.state
  serviceError.value = data.error
  gateWsConnected.value = data.gate_ws_connected
  binanceWsConnected.value = data.binance_ws_connected ?? false
  contracts.value = data.contracts ?? []
  spotSymbols.value = data.spot_symbols ?? []
  snapshotProgress.value = data.snapshot ?? { current: 0, total: 0, percent: 0 }
  subscribeProgress.value = data.subscribe ?? { current: 0, total: 0, percent: 0 }
  serviceBusy.value = data.state === 'starting' || data.state === 'stopping'
  const contractCount = data.contracts?.length ?? 0
  const rowCount = rowData.value.length
  if (
    (data.state === 'running' || data.state === 'starting') &&
    contractCount > 0 &&
    (prevState !== data.state || rowCount === 0 || rowCount < contractCount)
  ) {
    fetchOrderbookSnapshot()
  }
}

async function fetchServiceStatus() {
  try {
    const res = await fetch('/api/service/status')
    if (!res.ok) return
    const data: ServiceStatus = await res.json()
    applyServiceStatus(data)
  } catch {
    gateWsConnected.value = false
    binanceWsConnected.value = false
  }
}

function resetProgress() {
  snapshotProgress.value = { current: 0, total: 0, percent: 0 }
  subscribeProgress.value = { current: 0, total: 0, percent: 0 }
}

async function startService() {
  try {
    serviceBusy.value = true
    serviceState.value = 'starting'
    clearGridData()
    resetProgress()
    const res = await fetch('/api/service/start', { method: 'POST' })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      serviceBusy.value = false
      showError(typeof body.detail === 'string' ? body.detail : '启动失败')
      return
    }
    showSuccess('正在启动订单簿 WS 服务…')
    await fetchServiceStatus()
  } catch {
    serviceBusy.value = false
    showError('启动请求失败')
  }
}

async function stopService() {
  try {
    serviceBusy.value = true
    serviceState.value = 'stopping'
    const res = await fetch('/api/service/stop', { method: 'POST' })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      serviceBusy.value = false
      showError(typeof body.detail === 'string' ? body.detail : '终止失败')
      return
    }
    showWarning('正在终止订单簿 WS 服务…')
    clearGridData()
    await fetchServiceStatus()
  } catch {
    serviceBusy.value = false
    showError('终止请求失败')
  }
}

function restartStatusPolling() {
  if (statusInterval) clearInterval(statusInterval)
  const ms = serviceBusy.value ? 200 : 3000
  statusInterval = setInterval(fetchServiceStatus, ms)
}

watch(serviceBusy, () => {
  restartStatusPolling()
})

/** 监听过滤开关变化，通知 AG Grid 重新过滤 */
watch(filterByFundingRate, () => {
  if (gridApi) {
    gridApi.onFilterChanged()
  }
})

watch(filterByCoverage, () => {
  if (gridApi) {
    gridApi.onFilterChanged()
  }
})

watch(filterByMarginalBasis, () => {
  if (gridApi) {
    gridApi.onFilterChanged()
  }
})

onMounted(() => {
  // HMR 时复用已有的 WebSocket 连接，不重新创建
  if (!wsInitialized || socket?.readyState === WebSocket.CLOSED) {
    connectWs()
    wsInitialized = true
  } else if (socket?.readyState === WebSocket.OPEN) {
    // 复用已有连接，直接更新状态
    wsStatus.value = 'connected'
  }
  
  fetchServiceStatus().then(() => {
    if (serviceState.value === 'running' || serviceState.value === 'starting') {
      fetchOrderbookSnapshot()
    }
  })
  restartStatusPolling()
})

onUnmounted(() => {
  // HMR 时不关闭 WebSocket，保持连接
  // 只有在页面真正关闭/导航时才清理
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (statusInterval) clearInterval(statusInterval)
  // 注释掉 socket?.close()，让 HMR 时连接保持活跃
  // socket?.close()
})

function onGridReady(params: GridReadyEvent<OrderBookRow>) {
  gridApi = params.api
  if (rowsByContract.size > 0) {
    params.api.setGridOption('rowData', Array.from(rowsByContract.values()))
  }
  // 加载保存的列状态
  loadColumnState()
  // 绑定 Cmd+C 复制 & 右键菜单
  setupGridCopy(params.api)
}
</script>

<template>
  <div class="monitor-page">
    <el-card shadow="never" class="status-card">
      <div class="status-row">
        <div class="status-actions">
          <el-button type="primary" size="small" :disabled="!canStart" @click="startService">
            启动 WS 服务
          </el-button>
          <el-button type="danger" size="small" :disabled="!canStop" @click="stopService">
            终止 WS 服务
          </el-button>
        </div>
        <span class="status-item">
          前端 WS：
          <el-tag :type="statusTagType" size="small">{{ statusText }}</el-tag>
        </span>
        <span class="status-item">
          Gate WS：
          <el-tag :type="gateWsConnected ? 'success' : 'danger'" size="small">
            {{ gateWsConnected ? '已连接' : '未连接' }}
          </el-tag>
        </span>
        <span class="status-item">
          Binance WS：
          <el-tag :type="binanceWsConnected ? 'success' : 'danger'" size="small">
            {{ binanceWsConnected ? '已连接' : '未连接' }}
          </el-tag>
        </span>
        <span class="status-item">合约数：{{ contracts.length }}</span>
        <span class="status-item">现货数：{{ spotSymbols.length }}</span>
        <span class="status-item">最后更新：{{ lastUpdate }}</span>
      </div>
      <div v-if="showProgress" class="progress-row">
        <div class="progress-block">
          <div class="progress-label">
            <span>快照加载</span>
            <span :class="{ 'progress-done': progressStatus(snapshotProgress) === 'success' }">
              {{ formatProgress(snapshotProgress) }}
            </span>
          </div>
          <el-progress
            :percentage="snapshotProgress.percent"
            :status="progressStatus(snapshotProgress)"
            :stroke-width="8"
            :show-text="false"
          />
        </div>
        <div class="progress-block">
          <div class="progress-label">
            <span>WS 订阅</span>
            <span :class="{ 'progress-done': progressStatus(subscribeProgress) === 'success' }">
              {{ formatProgress(subscribeProgress) }}
            </span>
          </div>
          <el-progress
            :percentage="subscribeProgress.percent"
            :status="progressStatus(subscribeProgress)"
            :stroke-width="8"
            :show-text="false"
          />
        </div>
        <span v-if="serviceError" class="status-error">{{ serviceError }}</span>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <span>5档盘口（Gate 永续 + Binance 现货，实时）</span>
          <div class="header-actions">
            <div class="filter-group">
              <el-switch
                v-model="filterByFundingRate"
                class="filter-switch"
                inline-prompt
                active-text="资金费率过滤"
                inactive-text="资金费率过滤"
                style="--el-switch-on-color: #13ce66; --el-switch-off-color: #dcdfe6"
              />
              <el-switch
                v-model="filterByCoverage"
                class="filter-switch"
                inline-prompt
                active-text="开仓盘口阈值过滤"
                inactive-text="开仓盘口阈值过滤"
                style="--el-switch-on-color: #13ce66; --el-switch-off-color: #dcdfe6"
              />
              <el-switch
                v-model="filterByMarginalBasis"
                class="filter-switch"
                inline-prompt
                active-text="VWAP基差阈值过滤"
                inactive-text="VWAP基差阈值过滤"
                style="--el-switch-on-color: #13ce66; --el-switch-off-color: #dcdfe6"
              />
            </div>
            <div class="column-actions">
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
        </div>
      </template>
      <div ref="gridContainerRef">
      <ag-grid-vue
        class="orderbook-grid"
        :theme="orderbookGridTheme"
        :columnDefs="columnDefs"
        :rowData="rowData"
        :defaultColDef="defaultColDef"
        :getRowId="getRowId"
        column-menu="new"
        :row-selection="rowSelection"
        :locale-text="localeText"
        :header-height="32"
        :row-height="32"
        :isExternalFilterPresent="() => filterByFundingRate || filterByCoverage || filterByMarginalBasis"
        :doesExternalFilterPass="combinedFilterFunc"
        @grid-ready="onGridReady"
      />
      </div>
    </el-card>

    <!-- 五档盘口抽屉 -->
    <ElDrawer
      v-model="drawerVisible"
      :title="drawerRow ? `${drawerRow.contract} 五档盘口` : '五档盘口'"
      direction="rtl"
      :size="480"
      class="ob-drawer"
    >
      <div v-if="drawerRow" class="ob-drawer-body">
        <!-- 期货盘口 -->
        <div class="ob-section">
          <div class="ob-section-title">期货盘口（Gate 永续）</div>
          <table class="ob-table">
            <thead>
              <tr>
                <th>档</th>
                <th>价格</th>
                <th>数量</th>
                <th>USDT</th>
              </tr>
            </thead>
            <tbody>
              <!-- Ask 卖盘：从 ask_5 到 ask_1 排列（价格从高到低） -->
              <tr
                v-for="lv in getOrderbookLevels(drawerRow, 'future', 'ask').slice().reverse()"
                :key="'fa' + lv.level"
                class="ob-ask-row"
              >
                <td class="ob-level">A{{ lv.level }}</td>
                <td class="ob-ask-price">{{ fmtDrawerPrice(lv.price) }}</td>
                <td>{{ fmtDrawerVolume(lv.volume) }}</td>
                <td>{{ fmtDrawerUsdt(lv.usdt) }}</td>
              </tr>
              <tr class="ob-divider-row">
                <td colspan="4"><div class="ob-mid-divider"></div></td>
              </tr>
              <!-- Bid 买盘：从 bid_1 到 bid_5 排列（价格从低到高） -->
              <tr
                v-for="lv in getOrderbookLevels(drawerRow, 'future', 'bid')"
                :key="'fb' + lv.level"
                class="ob-bid-row"
              >
                <td class="ob-level">B{{ lv.level }}</td>
                <td class="ob-bid-price">{{ fmtDrawerPrice(lv.price) }}</td>
                <td>{{ fmtDrawerVolume(lv.volume) }}</td>
                <td>{{ fmtDrawerUsdt(lv.usdt) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="ob-summary">
            <span>Ask 总 USDT：<em class="ob-ask-price">{{ fmtTotalUsdt(drawerRow.future_usdt_ask_total) }}</em></span>
            <span>Bid 总 USDT：<em class="ob-bid-price">{{ fmtTotalUsdt(drawerRow.future_usdt_bid_total) }}</em></span>
          </div>
        </div>

        <!-- 盘口覆盖 -->
        <div class="ob-section ob-depth-section">
          <div class="ob-section-title">盘口覆盖</div>
          <div class="ob-depth-grid">
            <div class="ob-depth-item">
              <span class="ob-depth-label">现货开仓盘口覆盖</span>
              <span class="ob-depth-value ob-bid-price">{{ fmtDepthPercent(drawerRow.spot_open_coverage) }}</span>
            </div>
            <div class="ob-depth-item">
              <span class="ob-depth-label">合约开仓盘口覆盖</span>
              <span class="ob-depth-value ob-bid-price">{{ fmtDepthPercent(drawerRow.future_open_coverage) }}</span>
            </div>
            <div class="ob-depth-item">
              <span class="ob-depth-label">现货平仓盘口覆盖</span>
              <span class="ob-depth-value ob-ask-price">{{ fmtDepthPercent(drawerRow.spot_close_coverage) }}</span>
            </div>
            <div class="ob-depth-item">
              <span class="ob-depth-label">合约平仓盘口覆盖</span>
              <span class="ob-depth-value ob-ask-price">{{ fmtDepthPercent(drawerRow.future_close_coverage) }}</span>
            </div>
          </div>
        </div>

        <!-- 现货盘口 -->
        <div class="ob-section">
          <div class="ob-section-title">现货盘口（Binance 现货）</div>
          <table class="ob-table">
            <thead>
              <tr>
                <th>档</th>
                <th>价格</th>
                <th>数量</th>
                <th>USDT</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="lv in getOrderbookLevels(drawerRow, 'spot', 'ask').slice().reverse()"
                :key="'sa' + lv.level"
                class="ob-ask-row"
              >
                <td class="ob-level">A{{ lv.level }}</td>
                <td class="ob-ask-price">{{ fmtDrawerPrice(lv.price) }}</td>
                <td>{{ fmtDrawerVolume(lv.volume) }}</td>
                <td>{{ fmtDrawerUsdt(lv.usdt) }}</td>
              </tr>
              <tr class="ob-divider-row">
                <td colspan="4"><div class="ob-mid-divider"></div></td>
              </tr>
              <tr
                v-for="lv in getOrderbookLevels(drawerRow, 'spot', 'bid')"
                :key="'sb' + lv.level"
                class="ob-bid-row"
              >
                <td class="ob-level">B{{ lv.level }}</td>
                <td class="ob-bid-price">{{ fmtDrawerPrice(lv.price) }}</td>
                <td>{{ fmtDrawerVolume(lv.volume) }}</td>
                <td>{{ fmtDrawerUsdt(lv.usdt) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="ob-summary">
            <span>Ask 总 USDT：<em class="ob-ask-price">{{ fmtTotalUsdt(drawerRow.spot_usdt_ask_total) }}</em></span>
            <span>Bid 总 USDT：<em class="ob-bid-price">{{ fmtTotalUsdt(drawerRow.spot_usdt_bid_total) }}</em></span>
          </div>
        </div>
      </div>
    </ElDrawer>
  </div>
</template>

<style scoped>
.monitor-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.status-card :deep(.el-card__body) {
  padding: 12px 16px;
}

.status-row {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

.status-actions {
  display: flex;
  gap: 8px;
}

.progress-row {
  display: flex;
  align-items: flex-start;
  gap: 32px;
  flex-wrap: wrap;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--app-border);
}

.progress-block {
  flex: 1;
  min-width: 220px;
  max-width: 360px;
}

.progress-label {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--app-text-muted);
}

.progress-label .progress-done {
  color: #67c23a;
  font-weight: 500;
}

.status-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.4;
}

.status-error {
  color: #f56c6c;
  font-size: 13px;
}

.grid-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}

.filter-group {
  display: flex;
  align-items: center;
  gap: 16px;
}

.column-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-switch {
  margin-left: 0;
}

.orderbook-grid {
  width: 100%;
  height: calc(100vh - 220px);
  min-height: 420px;
}

/* 操作列按钮 */
:global(.ob-drawer-btn) {
  padding: 2px 10px;
  font-size: 12px;
  color: #7eb8f7;
  background: transparent;
  border: 1px solid #3a5a8a;
  border-radius: 3px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
}
:global(.ob-drawer-btn:hover) {
  background: #1e3a5a;
  color: #a8d0f8;
}

/* 抽屉整体风格 */
.ob-drawer :deep(.el-drawer) {
  background: #181d1f;
  color: #e8eaed;
}
.ob-drawer :deep(.el-drawer__header) {
  background: #1e2527;
  color: #e8eaed;
  margin-bottom: 0;
  padding: 14px 20px;
  border-bottom: 1px solid #333;
  font-size: 15px;
  font-weight: 600;
}
.ob-drawer :deep(.el-drawer__body) {
  padding: 0;
  background: #181d1f;
}
.ob-drawer :deep(.el-drawer__close-btn) {
  color: #9aa0a6;
}
.ob-drawer :deep(.el-drawer__close-btn:hover) {
  color: #e8eaed;
}

/* 抽屉内容区 */
.ob-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 0;
  height: 100%;
  overflow-y: auto;
}

.ob-section {
  padding: 16px 20px;
  border-bottom: 1px solid #2a2f31;
}

.ob-section-title {
  font-size: 13px;
  font-weight: 600;
  color: #9aa0a6;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}

.ob-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.ob-table th {
  text-align: right;
  color: #6b7280;
  font-weight: 500;
  font-size: 11px;
  padding: 0 6px 6px 6px;
  border-bottom: 1px solid #2a2f31;
}

.ob-table th:first-child {
  text-align: left;
}

.ob-table td {
  text-align: right;
  padding: 4px 6px;
  color: #c8cdd0;
  font-size: 13px;
}

.ob-table td:first-child {
  text-align: left;
}

.ob-level {
  color: #6b7280;
  font-size: 11px;
  font-weight: 600;
  width: 30px;
}

.ob-ask-price {
  color: #f56c6c !important;
  font-weight: 500;
}

.ob-bid-price {
  color: #67c23a !important;
  font-weight: 500;
}

.ob-ask-row td {
  background: rgba(245, 108, 108, 0.04);
}

.ob-bid-row td {
  background: rgba(103, 194, 58, 0.04);
}

.ob-divider-row td {
  padding: 3px 0;
  background: transparent;
}

.ob-mid-divider {
  height: 1px;
  background: #2a2f31;
  margin: 0 6px;
}

.ob-summary {
  display: flex;
  justify-content: space-between;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #2a2f31;
  font-size: 12px;
  color: #9aa0a6;
}

.ob-summary em {
  font-style: normal;
  font-weight: 600;
}

.ob-depth-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 16px;
}

.ob-depth-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}

.ob-depth-label {
  font-size: 12px;
  color: #9aa0a6;
}

.ob-depth-value {
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
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
  font-size: 13px;
  color: #e8eaed;
  text-align: left;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
