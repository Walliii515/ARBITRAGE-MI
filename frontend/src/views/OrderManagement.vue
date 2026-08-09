<script setup lang="ts">
import { ref, shallowRef, reactive, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
} from 'ag-grid-community'
import { ElPopover, ElMessageBox } from 'element-plus'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { get, post } from '../utils/request'

/* ───── 类型 ───── */
interface OrderRow {
  id: number
  order_uuid: string | null
  position_id: number | null
  created_at: string | null
  base_asset: string | null
  market_type: string | null
  trade_direction: string | null
  leverage: number | null
  order_side: string | null
  status: string | null
  channel: string | null
  target_qty: number | null
  target_amount: number | null
  exec_price: number | null
  exec_qty: number | null
  exec_amount: number | null
  coverage_ratio: number | null
  open_coverage: number | null
  open_vwap_basis_bps: number | null
  risk_relief_bps: number | null
  open_marginal_basis_bps: number | null
  funding_rate_24h: number | null
  reject_reason: string | null
}

/** 持仓行类型（来自 mi_trade_position 表） */
interface PositionRow {
  id: number
  order_uuid: string | null
  base_asset: string
  market_profile: string | null
  status: string  // holding / closed
  opened_at: string | null
  closed_at: string | null
  spot_open_qty: number | null
  spot_open_price: number | null
  spot_open_amount: number | null
  future_open_qty: number | null
  future_open_price: number | null
  future_open_contracts: number | null
  open_spread_bps: number | null
  open_vwap_threshold_bps: number | null
  close_vwap_threshold_bps: number | null
  open_reason: string | null
  spot_close_price: number | null
  spot_close_amount: number | null
  future_close_price: number | null
  future_close_amount: number | null
  close_spread_bps: number | null
  open_funding_rate_24h: number | null
  close_funding_rate_24h: number | null
  close_reason: string | null
  exchange_risk_status: string | null
  exchange_risk_type: string | null
  exchange_risk_at: string | null
  exchange_risk_detail: string | null
  delist_risks?: Array<Record<string, any>>
  delist_risk_level?: string | null
  delist_risk_summary?: string | null
  // 子查询注入字段
  channel: string | null
  gate_leverage: number | null
  order_count: number | null
}

type OrderView = 'open' | 'close'

/* ───── 状态 ───── */
const { gridContainerRef: openGridContainerRef, setupGridCopy: setupOpenGridCopy } = useGridCopy()
const { gridContainerRef: closeGridContainerRef, setupGridCopy: setupCloseGridCopy } = useGridCopy()
void openGridContainerRef
void closeGridContainerRef
const openRowData = shallowRef<PositionRow[]>([])
const closeRowData = shallowRef<PositionRow[]>([])
let openGridApi: GridApi<PositionRow> | null = null
let closeGridApi: GridApi<PositionRow> | null = null
const activeTab = ref<OrderView>('open')
const loadingByView = reactive<Record<OrderView, boolean>>({ open: false, close: false })
const exchangeRiskOnly = ref<boolean>(false)
const baseAssetFilter = ref<string>('')
const filterDays = ref<number>(90) // 默认90天，与持仓监控一致

// 一键全部平仓
const closeAllLoading = ref(false)

// 分页配置
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
const paginationByView = reactive<Record<OrderView, {
  pageSize: number
  currentPage: number
  total: number
}>>({
  open: { pageSize: 50, currentPage: 1, total: 0 },
  close: { pageSize: 50, currentPage: 1, total: 0 },
})

const activeLoading = computed(() => loadingByView[activeTab.value])
const activePagination = computed(() => paginationByView[activeTab.value])
const paginationCurrentPage = computed({
  get: () => activePagination.value.currentPage,
  set: (value: number) => { activePagination.value.currentPage = value },
})
const paginationPageSize = computed({
  get: () => activePagination.value.pageSize,
  set: (value: number) => { activePagination.value.pageSize = value },
})
const paginationTotal = computed(() => activePagination.value.total)

/** 从当前数据中提取唯一标的资产列表，供下拉框选择 */
const assetOptions = computed(() => {
  const rows = activeTab.value === 'open' ? openRowData.value : closeRowData.value
  const assets = new Set(rows.map(r => r.base_asset).filter(Boolean) as string[])
  return Array.from(assets).sort()
})

/** 订单详情弹窗 */
const detailDialogVisible = ref(false)
const detailOrders = ref<OrderRow[]>([])
const detailPositionId = ref<number | null>(null)
const detailLoading = ref(false)

/** 列状态持久化（数据库版） */
const PAGE_KEYS: Record<OrderView, string> = {
  open: 'order_management_open',
  close: 'order_management_close',
}

/** 列选择面板 */
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

/* ───── 格式化 ───── */
function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  if (value == null || !Number.isFinite(value)) return ''
  if (Number.isInteger(value)) return String(value)
  return value.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

/* ───── 简单格式化函数（用于汇总行） ───── */
function formatTime(val: string | null | undefined): string {
  if (!val) return '-'
  const d = new Date(val)
  if (isNaN(d.getTime())) return '-'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function formatAmount(val: number | null | undefined): string {
  if (val == null) return '-'
  return val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}





const amountFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toFixed(2) + ' bps'
}

const fundingBpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return (Number(params.value) * 10000).toFixed(2) + ' bps'
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

const channelFormatter = (params: ValueFormatterParams) => {
  const map: Record<string, string> = {
    Mock: 'Mock',
    SimTrade: '模拟盘',
    Live: '实盘',
  }
  return map[params.value] ?? params.value ?? ''
}

function formatLeverage(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  const n = Number(value)
  if (Math.abs(n) < 1e-9) return '全仓'
  return `${Number.isInteger(n) ? n.toFixed(0) : formatDecimal(n, 2)}x`
}

function formatExchangeRisk(row: PositionRow | null | undefined): string {
  if (!row) return ''
  const hasDelistRisk = !!(row.delist_risks && row.delist_risks.length > 0)
  if (
    (!row.exchange_risk_status || ['normal', 'resolved'].includes(row.exchange_risk_status))
    && !hasDelistRisk
  ) return ''
  const typeMap: Record<string, string> = {
    adl: 'ADL自动减仓',
    delist_risk: '下架风险',
    missing_gate_position: 'Gate缺腿',
    qty_mismatch: '数量不匹配',
    unknown: '交易所风险',
  }
  const primary = typeMap[row.exchange_risk_type || 'unknown'] || row.exchange_risk_type || row.exchange_risk_status || ''
  if (hasDelistRisk && primary !== '下架风险') return `${primary} + 下架风险`
  return primary
}

function exchangeRiskTooltip(row: PositionRow | null | undefined): string | null {
  if (!row) return null
  const parts = [row.exchange_risk_detail, row.delist_risk_summary ? `下架风险: ${row.delist_risk_summary}` : null]
    .filter(Boolean) as string[]
  return parts.length ? Array.from(new Set(parts)).join(' | ') : null
}

const profileColorMap: Record<string, string> = {
  normal: '#67c23a',
  thin_bursty: '#e6a23c',
  illiquid_blocked: '#f56c6c',
}

/* ───── 列定义 ───── */
const baseColumnDefs = computed((): ColDef[] => [
  {
    headerName: '开仓时间',
    field: 'opened_at',
    width: 180,
    valueFormatter: timeFormatter,
  },
  {
    headerName: '平仓时间',
    field: 'closed_at',
    width: 180,
    valueFormatter: timeFormatter,
  },
  {
    headerName: '标的资产',
    field: 'base_asset',
    width: 120,
    pinned: 'left',
    cellRenderer: (params: any) => {
      const row = params.data as PositionRow
      const count = row?.order_count ?? 0
      return `<strong class="group-asset">${row?.base_asset ?? ''} (${count})</strong>`
    },
  },
  {
    headerName: '画像',
    field: 'market_profile',
    width: 115,
    pinned: 'left',
    cellRenderer: (params: any) => {
      const profile = params.value || 'normal'
      const color = profileColorMap[profile] || '#909399'
      return `<span style="color:${color};font-weight:700">${profile}</span>`
    },
  },
  {
    headerName: '交易所风险',
    field: 'exchange_risk_type',
    width: 130,
    valueFormatter: (params) => formatExchangeRisk(params.data as PositionRow),
    cellStyle: (params) => {
      const row = params.data as PositionRow | undefined
      if (row?.exchange_risk_status === 'desynced') return { color: '#f56c6c', fontWeight: '700' }
      if (row?.delist_risk_level === 'critical') return { color: '#f56c6c', fontWeight: '700' }
      if (row?.delist_risk_level === 'warning') return { color: '#e6a23c', fontWeight: '700' }
      return { color: '#909399', fontWeight: '400' }
    },
    tooltipValueGetter: (params: any) => exchangeRiskTooltip(params.data as PositionRow),
    tooltipComponent: LongTextTooltip,
  },
  {
    headerName: '渠道',
    field: 'channel',
    width: 90,
    valueFormatter: channelFormatter,
  },
  {
    headerName: 'Gate杠杆',
    field: 'gate_leverage',
    width: 95,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (params) => formatLeverage(params.value),
  },
  {
    headerName: '开仓金额',
    field: 'spot_open_amount',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: amountFormatter,
  },
  {
    headerName: '开仓VWAP(S/F)',
    colId: 'open_vwap',
    width: 160,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    cellRenderer: (params: any) => {
      const row = params.data as PositionRow
      const sp = row?.spot_open_price
      const fp = row?.future_open_price
      const fmt = (v: number | null) => v != null ? formatDecimal(v, 4) : '-'
      return `${fmt(sp)}/${fmt(fp)}`
    },
  },
  {
    headerName: '平仓金额',
    field: 'spot_close_amount',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: amountFormatter,
  },
  {
    headerName: '平仓VWAP(S/F)',
    colId: 'close_vwap',
    width: 160,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    cellRenderer: (params: any) => {
      const row = params.data as PositionRow
      const sp = row?.spot_close_price
      const fp = row?.future_close_price
      const fmt = (v: number | null) => v != null ? formatDecimal(v, 4) : '-'
      return `${fmt(sp)}/${fmt(fp)}`
    },
  },
  {
    headerName: '开仓基差(bps)',
    field: 'open_spread_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '开仓VWAP阈值(bps)',
    field: 'open_vwap_threshold_bps',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '开仓24h资金费率',
    field: 'open_funding_rate_24h',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: fundingBpsFormatter,
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
    headerName: '平仓VWAP阈值(bps)',
    field: 'close_vwap_threshold_bps',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '平仓24h资金费率',
    field: 'close_funding_rate_24h',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: fundingBpsFormatter,
  },

  {
    headerName: '开仓原因',
    field: 'open_reason',
    width: 160,
    tooltipComponent: LongTextTooltip,
    tooltipValueGetter: (params: any) => params.data?.open_reason || null,
  },
  {
    headerName: '平仓原因',
    field: 'close_reason',
    width: 160,
    tooltipComponent: LongTextTooltip,
    tooltipValueGetter: (params: any) => params.data?.close_reason || null,
  },
  {
    headerName: '操作',
    colId: 'action',
    width: 160,
    pinned: 'right',
    lockPosition: true,
    lockPinned: true,
    suppressMovable: true,
    sortable: false,
    filter: false,
    cellRenderer: (params: any) => {
      const row = params.data as PositionRow
      if (!row) return ''
      let html = `<span class="action-btns">`
      html += `<button class="detail-btn" onclick="window.openDetailDialog(${row.id})">详情</button>`
      if (row.status === 'holding') {
        html += `<button class="manual-close-btn" onclick="window.handleManualClose(${row.id})">平仓</button>`
      }
      html += `</span>`
      return html
    },
  },
])

const openColumnIds = new Set([
  'opened_at', 'base_asset', 'market_profile', 'exchange_risk_type', 'channel',
  'gate_leverage', 'spot_open_amount', 'open_vwap', 'open_spread_bps',
  'open_vwap_threshold_bps', 'open_funding_rate_24h', 'open_reason', 'action',
])

const openColumnDefs = computed((): ColDef[] => (
  baseColumnDefs.value.filter((col) => openColumnIds.has(String(col.field || col.colId || '')))
))

const closeColumnIds = new Set([
  'closed_at', 'opened_at', 'base_asset', 'market_profile', 'exchange_risk_type',
  'channel', 'gate_leverage', 'spot_close_amount', 'close_vwap', 'open_spread_bps',
  'open_vwap_threshold_bps', 'close_spread_bps', 'close_vwap_threshold_bps',
  'close_funding_rate_24h', 'close_reason', 'action',
])

const closeColumnDefs = computed((): ColDef[] => {
  const defs = baseColumnDefs.value.filter((col) => (
    closeColumnIds.has(String(col.field || col.colId || ''))
  ))
  const closeTime = defs.find((col) => col.field === 'closed_at')
  return closeTime
    ? [closeTime, ...defs.filter((col) => col !== closeTime)]
    : defs
})

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

const getRowId = (params: GetRowIdParams<PositionRow>) => {
  return `pos_${params.data?.id ?? ''}`
}

/** 打开订单详情弹窗（通过API加载订单明细） */
async function openDetailDialog(positionId: number | null) {
  if (positionId == null) return
  detailPositionId.value = positionId
  detailOrders.value = []
  detailDialogVisible.value = true
  detailLoading.value = true
  try {
    const res = await get(`/api/trading/positions/${positionId}/orders`)
    const data = await res.json()
    detailOrders.value = data.orders || []
  } catch {
    showError('加载订单明细失败')
  } finally {
    detailLoading.value = false
  }
}

/** 快捷时间过滤 */
function resetFilterPagination() {
  paginationByView.open.currentPage = 1
  paginationByView.close.currentPage = 1
}

function setDaysFilter(days: number) {
  filterDays.value = days
  resetFilterPagination()
  fetchOrders()
}

/** 交易所风险过滤 */
function setExchangeRiskOnly(enabled: boolean) {
  exchangeRiskOnly.value = enabled
  resetFilterPagination()
  fetchOrders()
}

function onBaseAssetChange() {
  resetFilterPagination()
  fetchOrders()
}

function fetchActiveOrders() {
  fetchOrders(activeTab.value)
}

async function onTabChange(tab: string | number) {
  const view = String(tab) as OrderView
  if (view !== 'open' && view !== 'close') return
  activeTab.value = view
  await nextTick()
  fetchOrders(view)
}

async function fetchOrders(view: OrderView = activeTab.value) {
  loadingByView[view] = true
  try {
    const pagination = paginationByView[view]
    const params = new URLSearchParams()
    params.set('view', view)
    params.set('days', String(filterDays.value))
    params.set('page', String(pagination.currentPage))
    params.set('page_size', String(pagination.pageSize))
    if (exchangeRiskOnly.value) {
      params.set('exchange_risk', 'true')
    }
    if (baseAssetFilter.value) {
      params.set('base_asset', baseAssetFilter.value.trim())
    }
    const query = params.toString()
    const url = `/api/trading/orders${query ? '?' + query : ''}`
    const res = await get(url)
    if (!res.ok) {
      showError('获取订单数据失败')
      return
    }
    const data = await res.json()
    if (view === 'open') openRowData.value = data.orders || []
    else closeRowData.value = data.orders || []
    
    // 更新分页信息
    if (data.pagination) {
      pagination.total = data.pagination.total || 0
    }
  } catch {
    showError('请求订单数据失败')
  } finally {
    loadingByView[view] = false
  }
}

/** 页码变化 */
function onPageChange(page: number) {
  activePagination.value.currentPage = page
  fetchOrders()
}

/** 每页条数变化 */
function onPaginationSizeChange() {
  activePagination.value.currentPage = 1
  fetchOrders()
}

/** 计算总页数 */
const totalPages = computed(() => {
  return Math.ceil(activePagination.value.total / activePagination.value.pageSize) || 1
})

function activeGridApi(): GridApi<PositionRow> | null {
  return activeTab.value === 'open' ? openGridApi : closeGridApi
}

function activeColumnDefs(): ColDef[] {
  return activeTab.value === 'open' ? openColumnDefs.value : closeColumnDefs.value
}

/* ───── 列选择面板 ───── */
function refreshColumnVisibilities() {
  const api = activeGridApi()
  if (!api) return
  const states = api.getColumnState()
  columnVisibilities.value = activeColumnDefs()
    .filter((col) => col.field || col.colId)
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
  const api = activeGridApi()
  if (!api) return
  api.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) col.visible = visible
}

/** 保存列配置到数据库 */
async function saveColumnState() {
  const api = activeGridApi()
  if (!api) return
  const columnState = api.getColumnState()
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEYS[activeTab.value]}`, { columnState })
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
async function loadColumnState(view: OrderView, api: GridApi<PositionRow>) {
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEYS[view]}`)
    const data = await res.json()
    if (data?.columnState && Array.isArray(data.columnState)) {
      api.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch (e) {
    console.warn('Failed to load column config from server:', e)
  }
}

/* ───── AG Grid 回调 ───── */
function onOpenGridReady(params: GridReadyEvent<PositionRow>) {
  openGridApi = params.api
  loadColumnState('open', params.api)
  setupOpenGridCopy(params.api)
}

function onCloseGridReady(params: GridReadyEvent<PositionRow>) {
  closeGridApi = params.api
  loadColumnState('close', params.api)
  setupCloseGridCopy(params.api)
}

/** 双击行打开详情弹窗 */
function onRowDoubleClicked(params: any) {
  const positionId = params.data?.id
  if (positionId != null) {
    openDetailDialog(positionId)
  }
}

/* ───── 定时自动刷新 ───── */
let autoRefreshTimer: ReturnType<typeof setInterval> | null = null

/* ───── 一键平仓（单个持仓） ───── */
const closingPositionId = ref<number | null>(null)

async function handleManualClose(positionId: number) {
  try {
    await ElMessageBox.confirm(
      `确认对持仓 #${positionId} 执行一键平仓？\n系统将同时发送现货卖单和期货买单。`,
      '一键平仓确认',
      {
        confirmButtonText: '确认平仓',
        cancelButtonText: '取消',
        type: 'warning',
      }
    )
  } catch {
    return // 用户取消
  }

  closingPositionId.value = positionId
  try {
    const res = await post(`/api/trading/positions/${positionId}/manual-close`)
    const data = await res.json()
    if (data.success) {
      showSuccess(`平仓成功: ${data.base_asset}`)
    } else {
      showError(`平仓失败: ${data.message || data.detail || '未知错误'}`)
    }
  } catch (e: any) {
    // 处理 HTTP 错误状态码
    if (e?.message && !e.message.includes('未授权') && !e.message.includes('权限不足')) {
      showError(`平仓请求失败: ${e.message}`)
    }
  } finally {
    closingPositionId.value = null
  }
}

/* ───── 一键全部平仓 ───── */
async function handleCloseAll() {
  try {
    await ElMessageBox.confirm(
      '确认对所有持仓中的仓位执行一键平仓？\n系统将逐个发送现货卖单和期货买单。',
      '❗ 一键全部平仓',
      {
        confirmButtonText: '确认全部平仓',
        cancelButtonText: '取消',
        type: 'error',
      }
    )
  } catch {
    return // 用户取消
  }

  closeAllLoading.value = true
  try {
    const res = await post('/api/trading/positions/close-all')
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || `已平仓 ${data.closed}/${data.total} 个持仓`)
      fetchOrders()
    } else {
      showError(`平仓失败: ${data.message || '未知错误'}`)
    }
  } catch (e: any) {
    if (e?.message && !e.message.includes('未授权') && !e.message.includes('权限不足')) {
      showError(`平仓请求失败: ${e.message}`)
    }
  } finally {
    closeAllLoading.value = false
  }
}

/* ───── 生命周期 ───── */
onMounted(() => {
  // 注册全局函数供 cellRenderer 使用
  ;(window as any).handleManualClose = handleManualClose
  ;(window as any).openDetailDialog = openDetailDialog
  
  fetchOrders()
  autoRefreshTimer = setInterval(fetchOrders, 2000)
})

onUnmounted(() => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
})
</script>

<template>
  <div class="monitor-page">
    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">交易所风险：</span>
        <el-button-group size="small">
          <el-button :type="!exchangeRiskOnly ? 'primary' : 'default'" @click="setExchangeRiskOnly(false)">全部</el-button>
          <el-button :type="exchangeRiskOnly ? 'primary' : 'default'" @click="setExchangeRiskOnly(true)">有风险</el-button>
        </el-button-group>

        <span class="filter-label">时间：</span>
        <el-button-group size="small">
          <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="setDaysFilter(1)">今日</el-button>
          <el-button :type="filterDays === 3 ? 'primary' : 'default'" @click="setDaysFilter(3)">3天</el-button>
          <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="setDaysFilter(7)">7天</el-button>
          <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="setDaysFilter(30)">30天</el-button>
          <el-button :type="filterDays === 90 ? 'primary' : 'default'" @click="setDaysFilter(90)">90天</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">标的：</span>
        <el-select
          v-model="baseAssetFilter"
          placeholder="标的资产"
          size="small"
          filterable
          clearable
          style="width: 150px;"
          @change="onBaseAssetChange"
        >
          <el-option
            v-for="asset in assetOptions"
            :key="asset"
            :label="asset"
            :value="asset"
          />
        </el-select>

        <div class="filter-actions">
          <el-button
            size="small"
            type="primary"
            :loading="activeLoading"
            @click="fetchActiveOrders"
          >
            刷新
          </el-button>

          <el-button
            size="small"
            type="danger"
            :loading="closeAllLoading"
            @click="handleCloseAll"
          >
            ✖ 一键平仓
          </el-button>
        </div>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <div class="tab-actions">
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
      <el-tabs v-model="activeTab" class="order-tabs" @tab-change="onTabChange">
        <el-tab-pane label="开仓" name="open">
          <div ref="openGridContainerRef">
            <ag-grid-vue
              class="orderbook-grid"
              :theme="orderbookGridTheme"
              :columnDefs="openColumnDefs"
              :rowData="openRowData"
              :defaultColDef="defaultColDef"
              :getRowId="getRowId"
              :header-height="32"
              :row-height="32"
              :tooltipShowDelay="300"
              @grid-ready="onOpenGridReady"
              @row-double-clicked="onRowDoubleClicked"
            />
          </div>
        </el-tab-pane>
        <el-tab-pane label="平仓" name="close" lazy>
          <div ref="closeGridContainerRef">
            <ag-grid-vue
              class="orderbook-grid"
              :theme="orderbookGridTheme"
              :columnDefs="closeColumnDefs"
              :rowData="closeRowData"
              :defaultColDef="defaultColDef"
              :getRowId="getRowId"
              :header-height="32"
              :row-height="32"
              :tooltipShowDelay="300"
              @grid-ready="onCloseGridReady"
              @row-double-clicked="onRowDoubleClicked"
            />
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 底部分页控件 -->
    <div class="pagination-bar">
      <div class="pagination-info">
        共 {{ paginationTotal }} 条持仓，第 {{ paginationCurrentPage }} / {{ totalPages }} 页
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

    <!-- 订单详情弹窗 -->
    <el-dialog
      v-model="detailDialogVisible"
      :title="`订单详情 - 持仓 #${detailPositionId}`"
      width="1100px"
      destroy-on-close
    >
      <div class="detail-section-title">订单明细</div>
      <el-table :data="detailOrders" v-loading="detailLoading" border stripe size="small" style="width: 100%">
        <el-table-column prop="order_side" label="方向" width="70" :formatter="(row: OrderRow) => row.order_side === 'open' ? '开仓' : '平仓'">
          <template #default="{ row }">
            <span :style="{ color: row.order_side === 'close' ? '#e6a23c' : '#67c23a' }">
              {{ row.order_side === 'open' ? '开仓' : '平仓' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="market_type" label="市场" width="70" />
        <el-table-column prop="trade_direction" label="交易方向" width="70" />
        <el-table-column prop="leverage" label="杠杆" width="70" align="right" :formatter="(row: OrderRow) => formatLeverage(row.leverage)" />
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }">
            <span :style="{ color: row.status === 'executed' ? '#67c23a' : row.status === 'rejected' || row.status === 'failed' ? '#f56c6c' : '#e6a23c' }">
              {{ { pending: '待执行', executed: '已成交', rejected: '已拒单', failed: '失败' }[row.status as string] || row.status }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="target_amount" label="目标金额" width="100" align="right" :formatter="(row: OrderRow) => formatAmount(row.target_amount)" />
        <el-table-column prop="exec_price" label="成交价" width="110" align="right" :formatter="(row: OrderRow) => formatDecimal(row.exec_price, 6)" />
        <el-table-column prop="exec_qty" label="成交数量" width="100" align="right" :formatter="(row: OrderRow) => formatDecimal(row.exec_qty, 4)" />
        <el-table-column prop="exec_amount" label="成交金额" width="100" align="right" :formatter="(row: OrderRow) => formatAmount(row.exec_amount)" />
        <el-table-column prop="reject_reason" label="原因" min-width="140" show-overflow-tooltip />
        <el-table-column prop="created_at" label="时间" width="160" :formatter="(row: OrderRow) => formatTime(row.created_at)" />
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.monitor-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.status-card :deep(.el-card__body) {
  padding: 10px 14px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
  overflow-x: auto;
}

.filter-row :deep(.el-button-group),
.filter-row :deep(.el-select) {
  flex-shrink: 0;
}

.filter-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  margin-left: auto;
  padding-left: 16px;
}

.filter-label {
  color: var(--app-text-muted);
  font-size: 13px;
  white-space: nowrap;
}

.grid-card {
  position: relative;
}

.grid-card :deep(.el-card__body) {
  padding: 0;
}

.tab-actions {
  position: absolute;
  z-index: 2;
  top: 6px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.order-tabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 0 180px 0 14px;
}

.order-tabs :deep(.el-tabs__content) {
  padding-top: 8px;
}

.orderbook-grid {
  width: 100%;
  height: calc(100vh - 156px);
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

/* 分组汇总行样式 */
:deep(.group-asset) {
  color: var(--el-color-primary);
}

:deep(.group-executed) {
  color: var(--el-color-success);
}

/* 操作列按钮组 */
:deep(.action-btns) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

:deep(.detail-btn) {
  padding: 2px 10px;
  font-size: 12px;
  border: 1px solid var(--el-color-primary);
  border-radius: 3px;
  background: transparent;
  color: var(--el-color-primary);
  cursor: pointer;
  transition: all 0.2s;
  line-height: 20px;
  white-space: nowrap;
}

:deep(.detail-btn:hover) {
  background: var(--el-color-primary);
  color: #fff;
}

/* 一键平仓按钮 */
:deep(.manual-close-btn) {
  padding: 2px 10px;
  font-size: 12px;
  border: 1px solid #f56c6c;
  border-radius: 3px;
  background: transparent;
  color: #f56c6c;
  cursor: pointer;
  transition: all 0.2s;
  line-height: 20px;
  white-space: nowrap;
}

:deep(.manual-close-btn:hover) {
  background: #f56c6c;
  color: #fff;
}

.detail-section-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
}

/* 汇总行背景色 */
:deep(.ag-row[data-row-index]) {
  background: transparent;
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
