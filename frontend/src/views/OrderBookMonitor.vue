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
} from 'ag-grid-community'
import { ElDrawer } from 'element-plus'
import { Search, Refresh } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess, showWarning } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import type { OrderBookRow } from './orderbookTypes'
import { get, post } from '../utils/request'
import { getToken } from '../utils/auth'
import { useConnectionMonitor } from '../composables/useConnectionMonitor'

interface WsMessage {
  type: string
  server_time?: string
  rows?: OrderBookRow[]
  state?: ServiceStatus['state']
  error?: string | null
  gate_ws_connected?: boolean
  binance_ws_connected?: boolean
  gate_ws_latency_ms?: number | null
  binance_ws_latency_ms?: number | null
  funding_threshold_percentile?: string
  min_funding_rate_bps?: number
  orderbook_coverage_threshold?: number
  risk_relief_bps?: number
  open_vwap_basis_threshold_bps?: number
  min_spot_volume_24h_usdt?: number
  min_future_volume_24h_usdt?: number
  ts?: number  // pong 回复的时间戳
}

interface ServiceStatus {
  state: 'idle' | 'starting' | 'running' | 'stopping' | 'error'
  error: string | null
  gate_ws_connected: boolean
  binance_ws_connected: boolean
  contracts: string[]
}

const rowData = shallowRef<OrderBookRow[]>([])
/** 上一帧快照索引，供 diff → applyTransaction */
const rowsByContract = new Map<string, OrderBookRow>()
let gridApi: GridApi<OrderBookRow> | null = null
const wsStatus = ref<'connecting' | 'connected' | 'disconnected'>('disconnected')
const lastUpdate = ref('--')
const gateWsConnected = ref(false)
const binanceWsConnected = ref(false)
const wsLatencyMs = ref<number | null>(null)
const gateWsLatencyMs = ref<number | null>(null)
const binanceWsLatencyMs = ref<number | null>(null)
const {
  connectionStats,
  fetchConnectionStatus,
} = useConnectionMonitor()

/** 列状态持久化（数据库版） */
const PAGE_KEY = 'orderbook_monitor'

/** 列选择面板：当前列的可见性快照 */
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

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

const serviceState = ref<ServiceStatus['state']>('idle')
const serviceError = ref<string | null>(null)
const serviceBusy = ref(false)
/** 资金费率下限(bps)，从后端动态获取 */
const minFundingRateBps = ref<number>(-6)
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
/** 成交量过滤开关（默认开启） */
const filterByVolume = ref<boolean>(true)
/** 盈利性守卫过滤开关（默认开启） */
const filterByProfitability = ref<boolean>(true)
/** 现货24h成交量阈值（USDT） */
const minSpotVolume24h = ref<number>(5000000)
/** 期货24h成交量阈值（USDT） */
const minFutureVolume24h = ref<number>(3000000)

/** 标的资产过滤 */
const assetFilterKeyword = ref<string>('')
const assetFilterVisible = ref<boolean>(false)

/** 获取所有唯一的标的资产列表 */
const uniqueAssets = computed(() => {
  const assets = new Set<string>()
  rowData.value.forEach(row => {
    if (row.base_asset) {
      assets.add(row.base_asset)
    }
  })
  return Array.from(assets).sort()
})

/** 过滤后的标的资产选项 */
const filteredAssetOptions = computed(() => {
  if (!assetFilterKeyword.value) {
    return uniqueAssets.value
  }
  const keyword = assetFilterKeyword.value.toLowerCase()
  return uniqueAssets.value.filter(asset => 
    asset.toLowerCase().includes(keyword)
  )
})

/** 选择标的资产 */
function selectAsset(asset: string) {
  assetFilterKeyword.value = asset
  assetFilterVisible.value = false
}

/** 清除标的资产过滤 */
function clearAssetFilter() {
  assetFilterKeyword.value = ''
}

/** 点击外部关闭下拉框 */
function handleOutsideClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('.asset-filter-container')) {
    assetFilterVisible.value = false
  }
}

/** AG Grid 外部过滤函数：资金费率过滤 */
function fundingRateFilterFunc(params: any): boolean {
  if (!filterByFundingRate.value) {
    return true // 不过滤，显示所有
  }
  
  const data = params.data as OrderBookRow
  if (!data) return true
  
  const fundingRate = data.funding_rate_24h
  
  // 如果值为空，不排除（保持显示）
  if (fundingRate == null) {
    return true
  }
  
  // 资金费率(bps) >= 下限
  return fundingRate * 10000 >= minFundingRateBps.value
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

/** AG Grid 外部过滤函数：24小时成交量过滤 */
function volumeFilterFunc(params: any): boolean {
  if (!filterByVolume.value) {
    return true // 不过滤，显示所有
  }
  
  const data = params.data as OrderBookRow
  if (!data) return true
  
  // 现货24h成交量检查
  const quoteVolume = data.quote_volume
  if (quoteVolume != null && minSpotVolume24h.value > 0) {
    if (quoteVolume < minSpotVolume24h.value) return false
  }
  
  // 期货24h成交量检查
  const volume24hSettle = data.volume_24h_settle
  if (volume24hSettle != null && minFutureVolume24h.value > 0) {
    if (volume24hSettle < minFutureVolume24h.value) return false
  }
  
  return true
}

/** AG Grid 外部过滤函数：盈利性守卫过滤 */
function profitabilityGuardFilterFunc(params: any): boolean {
  if (!filterByProfitability.value) {
    return true // 不过滤，显示所有
  }
  
  const data = params.data as OrderBookRow
  if (!data) return true
  
  const openVwapBasis = data.open_vwap_basis_bps as number | null | undefined
  const closeThreshold = data.close_vwap_threshold_bps as number | null | undefined
  
  // 没有平仓阈值数据时不过滤（新标的可能无历史数据）
  if (closeThreshold == null || openVwapBasis == null) {
    return true
  }
  
  // 盈利性守卫: 开仓基差 > 平仓基差阈值 + 全程手续费
  // fee_cost_bps = -(open_fee_bps + close_fee_bps)
  const openFeeBps = data.open_fee_bps ?? 0
  const closeFeeBps = data.close_fee_bps ?? 0
  const feeCostBps = -(openFeeBps + closeFeeBps)
  
  return openVwapBasis > closeThreshold + feeCostBps
}

/** 标的资产过滤函数 */
function assetFilterFunc(params: any): boolean {
  if (!assetFilterKeyword.value) return true
  const data = params.data as OrderBookRow
  if (!data) return true
  const baseAsset = data.base_asset
  if (!baseAsset) return true
  return baseAsset.toLowerCase().includes(assetFilterKeyword.value.toLowerCase())
}

/** 组合过滤函数 */
function combinedFilterFunc(params: any): boolean {
  return fundingRateFilterFunc(params) && coverageFilterFunc(params) && marginalBasisFilterFunc(params) && volumeFilterFunc(params) && profitabilityGuardFilterFunc(params) && assetFilterFunc(params)
}

/** WebSocket 实例提升到模块级，避免 HMR 时断开 */
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let statusInterval: ReturnType<typeof setInterval> | null = null
let pingInterval: ReturnType<typeof setInterval> | null = null
/** 标记是否已初始化过，HMR 时复用连接 */
let wsInitialized = false
/** 页面可见性：页面隐藏时跳过 WS 消息处理和 ping，降低 CPU 开销 */
let pageVisible = true

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

/** 动态生成列定义 */
const columnDefs = computed<ColDef<OrderBookRow>[]>(() => {
  return [
    { headerName: '标的资产', field: 'base_asset', pinned: 'left', width: 90, sort: 'asc' },
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
        
        // >= 下限(bps)：绿色
        if (value * 10000 >= minFundingRateBps.value) {
          return { color: '#67c23a' }
        }
        // < 下限：红色
        return { color: '#f56c6c' }
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
      headerName: '费率下限(bps)',
      width: 110,
      type: 'numericColumn',
      cellClass: 'ag-right-aligned-cell',
      headerClass: 'ag-right-aligned-header',
      valueGetter: () => minFundingRateBps.value,
      valueFormatter: (p) => p.value != null ? p.value.toFixed(1) : '—',
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
    headerName: '平仓vwap基差阈值(bps)',
    field: 'close_vwap_threshold_bps',
    width: 160,
    type: 'numericColumn',
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
    const res = await get('/api/orderbook/snapshot')
    if (!res.ok) return
    const data = await res.json()
    if (data.rows?.length) {
      applySnapshotRows(data.rows, data.server_time, forceFull)
    }
  } catch {
    /* ignore */
  }
}

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
    wsLatencyMs.value = null
    // 每 10 秒发送一次 ping 测量延迟（仅页面可见时）
    if (pingInterval) clearInterval(pingInterval)
    if (pageVisible) {
      pingInterval = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
        }
      }, 10000)
      // 立即发一次 ping
      socket!.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
    }
    fetchServiceStatus().then(() => {
      if (serviceState.value === 'running' || serviceState.value === 'starting') {
        fetchOrderbookSnapshot()
      }
    })
  }

  socket.onmessage = (ev) => {
    // 页面隐藏时跳过所有快照消息处理，避免无用 CPU 开销
    if (!pageVisible) return
    const msg: WsMessage = JSON.parse(ev.data)
    if (msg.type === 'ping') return
    if (msg.type === 'pong' && msg.ts) {
      wsLatencyMs.value = Date.now() - msg.ts
      return
    }
    if (msg.type === 'service_progress') {
      if (msg.state) serviceState.value = msg.state
      if (msg.error !== undefined) serviceError.value = msg.error
      if (msg.gate_ws_connected !== undefined) gateWsConnected.value = msg.gate_ws_connected
      if (msg.binance_ws_connected !== undefined) binanceWsConnected.value = msg.binance_ws_connected
      if (msg.gate_ws_latency_ms !== undefined) gateWsLatencyMs.value = msg.gate_ws_latency_ms
      if (msg.binance_ws_latency_ms !== undefined) binanceWsLatencyMs.value = msg.binance_ws_latency_ms
      serviceBusy.value = msg.state === 'starting' || msg.state === 'stopping'
      return
    }
    if (msg.type === 'snapshot' && msg.rows) {
      // 更新交易所 WS 延迟
      if (msg.gate_ws_latency_ms !== undefined) gateWsLatencyMs.value = msg.gate_ws_latency_ms
      if (msg.binance_ws_latency_ms !== undefined) binanceWsLatencyMs.value = msg.binance_ws_latency_ms
      // 更新资金费率下限配置
      if (msg.min_funding_rate_bps != null) {
        minFundingRateBps.value = msg.min_funding_rate_bps
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
      // 更新成交量阈值配置
      if (msg.min_spot_volume_24h_usdt != null) {
        minSpotVolume24h.value = msg.min_spot_volume_24h_usdt
      }
      if (msg.min_future_volume_24h_usdt != null) {
        minFutureVolume24h.value = msg.min_future_volume_24h_usdt
      }
      applySnapshotRows(msg.rows, msg.server_time ?? '--')
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

function applyServiceStatus(data: ServiceStatus) {
  const prevState = serviceState.value
  serviceState.value = data.state
  serviceError.value = data.error
  gateWsConnected.value = data.gate_ws_connected
  binanceWsConnected.value = data.binance_ws_connected ?? false
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
    const res = await get('/api/service/status')
    if (!res.ok) return
    const data: ServiceStatus = await res.json()
    applyServiceStatus(data)
    await fetchConnectionStatus()
  } catch {
    gateWsConnected.value = false
    binanceWsConnected.value = false
  }
}

async function startService() {
  try {
    serviceBusy.value = true
    serviceState.value = 'starting'
    clearGridData()
    const res = await post('/api/service/start')
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
    const res = await post('/api/service/stop')
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

watch(filterByVolume, () => {
  if (gridApi) {
    gridApi.onFilterChanged()
  }
})

watch(filterByProfitability, () => {
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
  
  // 点击外部关闭资产下拉框
  document.addEventListener('click', handleOutsideClick)
  
  // 页面可见性监听：隐藏时停止 ping，可见时恢复
  document.addEventListener('visibilitychange', handleVisibilityChange)
  
  fetchServiceStatus().then(() => {
    if (serviceState.value === 'running' || serviceState.value === 'starting') {
      fetchOrderbookSnapshot()
    }
  })
  restartStatusPolling()
})

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

onUnmounted(() => {
  // HMR 时不关闭 WebSocket，保持连接
  // 只有在页面真正关闭/导航时才清理
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (statusInterval) clearInterval(statusInterval)
  if (pingInterval) clearInterval(pingInterval)
  // 注释掉 socket?.close()，让 HMR 时连接保持活跃
  // socket?.close()
  
  // 清理事件监听
  document.removeEventListener('click', handleOutsideClick)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
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
          <el-tag v-if="wsStatus === 'connected'" type="success" size="small">
            {{ wsLatencyMs != null ? `${wsLatencyMs}ms` : '已连接' }}
          </el-tag>
          <el-tag v-else :type="wsStatus === 'connecting' ? 'warning' : 'danger'" size="small">
            {{ wsStatus === 'connecting' ? '连接中' : '已断开' }}
          </el-tag>
        </span>
        <span class="status-item">
          Gate WS：
          <el-tag v-if="gateWsConnected" type="success" size="small">
            {{ gateWsLatencyMs != null ? `${gateWsLatencyMs}ms` : '已连接' }}
          </el-tag>
          <el-tag v-else type="danger" size="small">未连接</el-tag>
        </span>
        <span class="status-item">
          Binance WS：
          <el-tag v-if="binanceWsConnected" type="success" size="small">
            {{ binanceWsLatencyMs != null ? `${binanceWsLatencyMs}ms` : '已连接' }}
          </el-tag>
          <el-tag v-else type="danger" size="small">未连接</el-tag>
        </span>
        <span class="status-item">Gate接收中：{{ connectionStats.gateReceiving }}</span>
        <span class="status-item">Binance接收中：{{ connectionStats.binanceReceiving }}</span>
        <span class="status-item">最后更新：{{ lastUpdate }}</span>
        <el-button size="small" @click="fetchServiceStatus" :icon="Refresh" circle />
      </div>
      <div v-if="serviceError" class="status-message-row">
        <span class="status-error">{{ serviceError }}</span>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <!-- 标的资产模糊搜索下拉框 -->
          <div class="asset-filter-container">
            <el-input
              v-model="assetFilterKeyword"
              placeholder="搜索标的资产 (如: BTC, ETH)"
              clearable
              size="small"
              style="width: 240px"
              @focus="assetFilterVisible = true"
              @clear="clearAssetFilter"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
            <!-- 下拉选项列表 -->
            <div v-if="assetFilterVisible && filteredAssetOptions.length > 0" class="asset-dropdown">
              <div
                v-for="asset in filteredAssetOptions.slice(0, 10)"
                :key="asset"
                class="asset-option"
                @click="selectAsset(asset)"
              >
                {{ asset }}
              </div>
            </div>
          </div>
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
              <el-switch
                v-model="filterByVolume"
                class="filter-switch"
                inline-prompt
                active-text="成交量过滤"
                inactive-text="成交量过滤"
                style="--el-switch-on-color: #13ce66; --el-switch-off-color: #dcdfe6"
              />
              <el-switch
                v-model="filterByProfitability"
                class="filter-switch"
                inline-prompt
                active-text="盈利性守卫"
                inactive-text="盈利性守卫"
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
        :isExternalFilterPresent="() => filterByFundingRate || filterByCoverage || filterByMarginalBasis || filterByVolume"
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

.status-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.4;
}

.status-message-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.status-error {
  color: #f56c6c;
  font-size: 13px;
}

.grid-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.asset-filter-container {
  position: relative;
  flex-shrink: 0;
}

.asset-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  margin-top: 4px;
  background: var(--app-sidebar);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  max-height: 300px;
  overflow-y: auto;
  z-index: 1000;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}

.asset-option {
  padding: 8px 12px;
  cursor: pointer;
  color: var(--app-text);
  font-size: 13px;
  transition: background-color 0.2s;
}

.asset-option:hover {
  background-color: rgba(33, 150, 243, 0.12);
  color: #2196f3;
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
