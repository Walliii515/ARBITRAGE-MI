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
import { ElPopover, ElMessageBox } from 'element-plus'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { get, post } from '../utils/request'
import { getToken } from '../utils/auth'

/* ───── 类型 ───── */
interface OrderRow {
  id: number
  order_uuid: string | null
  position_id: number | null
  created_at: string | null
  base_asset: string | null
  market_type: string | null
  trade_direction: string | null
  order_side: string | null  // open/close
  status: string | null  // pending/executed/rejected/failed
  channel: string | null  // Mock/SimTrade/Live
  target_qty: number | null
  target_amount: number | null
  exec_price: number | null
  exec_qty: number | null
  exec_amount: number | null
  coverage_ratio: number | null
  // 开仓时刻风控指标
  open_coverage: number | null
  open_vwap_basis_bps: number | null
  risk_relief_bps: number | null
  open_marginal_basis_bps: number | null
  funding_rate_24h: number | null
  reject_reason: string | null
}

/* ───── 分组展示类型 ───── */
interface OrderGroup {
  position_id: number | null
  base_asset: string
  order_uuid: string | null
  orders: OrderRow[]
  total_amount: number
  executed_amount: number
  status: 'all_executed' | 'partial' | 'pending' | 'rejected'
}

interface DisplayRow {
  // 标识行类型
  isGroupHeader: boolean
  position_id?: number | null
  base_asset?: string | null
  order_uuid?: string | null
  orders?: OrderRow[]
  total_amount?: number
  executed_amount?: number
  groupStatus?: 'all_executed' | 'partial' | 'pending' | 'rejected'
  isExpanded?: boolean  // deprecated, kept for interface compat
  // 明细行字段（继承 OrderRow）
  id?: number
  created_at?: string | null
  market_type?: string | null
  trade_direction?: string | null
  order_side?: string | null
  status?: string | null
  channel?: string | null
  target_qty?: number | null
  target_amount?: number | null
  exec_price?: number | null
  exec_qty?: number | null
  exec_amount?: number | null
  coverage_ratio?: number | null
  // 开仓时刻风控指标
  open_coverage?: number | null
  open_vwap_basis_bps?: number | null
  risk_relief_bps?: number | null
  open_marginal_basis_bps?: number | null
  funding_rate_24h?: number | null
  reject_reason?: string | null
  // 前端计算字段（平仓明细行）
  _close_basis_bps?: number | null
}

/* ───── 状态 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()
const rowData = shallowRef<OrderRow[]>([])
let gridApi: GridApi<DisplayRow> | null = null
const loading = ref(false)
const statusFilter = ref<string>('')
const channelFilter = ref<string>('')
const orderSideFilter = ref<string>('')
const baseAssetFilter = ref<string>('')
const filterDays = ref<number>(1) // 默认今日

// 分页配置
const paginationPageSize = ref<number>(500)
const paginationPageSizeOptions = [500, 1000, 2000, 5000]
const paginationCurrentPage = ref<number>(1)
const paginationTotal = ref<number>(0)

/** 从当前数据中提取唯一标的资产列表，供下拉框选择 */
const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map(r => r.base_asset).filter(Boolean) as string[])
  return Array.from(assets).sort()
})

/** 订单详情弹窗 */
const detailDialogVisible = ref(false)
const detailOrders = ref<OrderRow[]>([])
const detailPositionId = ref<number | null>(null)

/** 列状态持久化（数据库版） */
const PAGE_KEY = 'order_management'

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

function findOrderBySideMarket(orders: OrderRow[] | undefined, side: string, marketType: string): OrderRow | undefined {
  return orders?.find(o => o.order_side === side && o.market_type === marketType)
}

function formatSpotFuturePair(
  spotVal: number | null | undefined,
  futureVal: number | null | undefined,
  formatter: (v: number) => string,
): string {
  const spotStr = spotVal != null && Number.isFinite(spotVal) ? formatter(spotVal) : '-'
  const futureStr = futureVal != null && Number.isFinite(futureVal) ? formatter(futureVal) : '-'
  if (spotStr === '-' && futureStr === '-') return ''
  return `${spotStr}/${futureStr}`
}

/** 计算一组订单中平仓对的 VWAP 基差(bps) */
function computeCloseBasisBps(orders: OrderRow[] | undefined): number | null {
  const closeSpot = findOrderBySideMarket(orders, 'close', 'spot')
  const closeFuture = findOrderBySideMarket(orders, 'close', 'future')
  if (!closeSpot?.exec_price || !closeFuture?.exec_price) return null
  return (closeFuture.exec_price - closeSpot.exec_price) / closeSpot.exec_price * 10000
}

/** 开/平VWAP汇总：开仓spot/future｜平仓spot/future */
function formatSummaryVwap(orders: OrderRow[] | undefined): string {
  const openSpot = findOrderBySideMarket(orders, 'open', 'spot')
  const openFuture = findOrderBySideMarket(orders, 'open', 'future')
  const closeSpot = findOrderBySideMarket(orders, 'close', 'spot')
  const closeFuture = findOrderBySideMarket(orders, 'close', 'future')
  const fmt = (v: number) => formatDecimal(v, 4)

  const openPair = formatSpotFuturePair(openSpot?.exec_price, openFuture?.exec_price, fmt)
  const closePair = formatSpotFuturePair(closeSpot?.exec_price, closeFuture?.exec_price, fmt)

  if (openPair && closePair) return `${openPair}\u2502${closePair}`
  return openPair || closePair || ''
}

/** 开/平盘口覆盖汇总：开仓spot/future｜平仓spot/future */
function formatSummaryCoverage(orders: OrderRow[] | undefined): string {
  const openSpot = findOrderBySideMarket(orders, 'open', 'spot')
  const openFuture = findOrderBySideMarket(orders, 'open', 'future')
  const closeSpot = findOrderBySideMarket(orders, 'close', 'spot')
  const closeFuture = findOrderBySideMarket(orders, 'close', 'future')
  const fmt = (v: number) => (v * 100).toFixed(1) + '%'

  const openPair = formatSpotFuturePair(openSpot?.open_coverage, openFuture?.open_coverage, fmt)
  const closePair = formatSpotFuturePair(closeSpot?.coverage_ratio, closeFuture?.coverage_ratio, fmt)

  if (openPair && closePair) return `${openPair}\u2502${closePair}`
  return openPair || closePair || ''
}

/** 开/平VWAP基差汇总：开仓bps｜平仓bps */
function formatSummaryBasis(orders: OrderRow[] | undefined, field: 'open_vwap_basis_bps' | 'open_marginal_basis_bps'): string {
  const openSpot = findOrderBySideMarket(orders, 'open', 'spot')
  const openVal = openSpot?.[field]
  const closeVal = computeCloseBasisBps(orders)

  const openStr = openVal != null ? Number(openVal).toFixed(2) : null
  const closeStr = closeVal != null ? closeVal.toFixed(2) : null

  if (openStr && closeStr) return `${openStr}\u2502${closeStr} bps`
  if (openStr) return `${openStr} bps`
  if (closeStr) return `${closeStr} bps`
  return ''
}

const priceFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return formatDecimal(params.value, 8)
}

const volumeFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return formatDecimal(params.value, 4)
}

const amountFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const percentFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return (params.value * 100).toFixed(1) + '%'
}

const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toFixed(2) + ' bps'
}

const fundingRateFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return (Number(params.value) * 100).toFixed(4) + '%'
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
    pending: '待执行',
    executed: '已成交',
    rejected: '已拒单',
    failed: '失败',
  }
  return map[params.value] ?? params.value ?? ''
}

const statusCellStyle = (params: ValueFormatterParams) => {
  const colorMap: Record<string, string> = {
    pending: '#e6a23c',
    executed: '#67c23a',
    rejected: '#f56c6c',
    failed: '#f56c6c',
  }
  const color = colorMap[params.value] ?? '#909399'
  return { color }
}

const channelFormatter = (params: ValueFormatterParams) => {
  const map: Record<string, string> = {
    Mock: 'Mock',
    SimTrade: '模拟盘',
    Live: '实盘',
  }
  return map[params.value] ?? params.value ?? ''
}

const orderSideFormatter = (params: ValueFormatterParams) => {
  const map: Record<string, string> = {
    open: '开仓',
    close: '平仓',
  }
  return map[params.value] ?? params.value ?? ''
}

/* ───── 列定义 ───── */
const columnDefs = computed<ColDef<DisplayRow>[]>(() => [
  {
    headerName: '下单时间',
    field: 'created_at',
    width: 180,
    valueFormatter: timeFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        const firstOrder = group.orders?.[0]
        return firstOrder?.created_at ? formatTime(firstOrder.created_at) : ''
      }
      return timeFormatter(params)
    },
  },
  {
    headerName: '标的资产',
    field: 'base_asset',
    width: 100,
    pinned: 'left',
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        const count = group.orders?.length ?? 0
        return `<strong class="group-asset">${group.base_asset} (${count})</strong>`
      }
      return params.value ?? ''
    },
  },
  {
    headerName: '订单方向',
    field: 'order_side',
    width: 90,
    valueFormatter: orderSideFormatter,
    cellStyle: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        const hasClose = group.orders?.some((o: any) => o.order_side === 'close')
        return { color: hasClose ? '#e6a23c' : '#67c23a' }
      }
      if (params.data?.order_side === 'close') return { color: '#e6a23c' }
      if (params.data?.order_side === 'open') return { color: '#67c23a' }
      return null
    },
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        // 汇总行：全开仓显示“开仓”，有平仓显示“平仓”
        const group = params.data as DisplayRow
        const hasClose = group.orders?.some(o => o.order_side === 'close')
        return hasClose ? '平仓' : '开仓'
      }
      return orderSideFormatter(params)
    },
  },
  {
    headerName: '市场',
    field: 'market_type',
    width: 80,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      return params.value ?? ''
    },
  },
  {
    headerName: '交易方向',
    field: 'trade_direction',
    width: 80,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      return params.value ?? ''
    },
  },
  {
    headerName: '状态',
    field: 'status',
    width: 100,
    valueFormatter: statusFormatter,
    cellStyle: (params: any) => {
      if (params.data?.isGroupHeader) {
        // 汇总行状态样式
        const statusMap: Record<string, string> = {
          all_executed: '#67c23a',
          partial: '#e6a23c',
          pending: '#909399',
          rejected: '#f56c6c',
        }
        const color = statusMap[params.data.groupStatus ?? 'pending'] ?? '#909399'
        return { color }
      }
      return statusCellStyle(params)
    },
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const statusMap: Record<string, string> = {
          all_executed: '全部成交',
          partial: '部分成交',
          pending: '待执行',
          rejected: '被拒',
        }
        return statusMap[params.data.groupStatus ?? 'pending'] ?? '待执行'
      }
      return statusFormatter(params)
    },
  },
  {
    headerName: '渠道',
    field: 'channel',
    width: 90,
    valueFormatter: channelFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        const ch = group.orders?.[0]?.channel
        return ch ? channelFormatter({ value: ch } as any) : ''
      }
      return channelFormatter(params)
    },
  },
  {
    headerName: '目标金额',
    field: 'target_amount',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: amountFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        return `<strong>${formatAmount(group.total_amount)}</strong>`
      }
      return amountFormatter(params)
    },
  },
  {
    headerName: '开/平金额',
    field: 'exec_amount',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: amountFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        return `<strong class="group-executed">${formatAmount(group.executed_amount)}</strong>`
      }
      return amountFormatter(params)
    },
  },
  {
    headerName: '开/平VWAP',
    field: 'exec_price',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: priceFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      return priceFormatter(params)
    },
  },
  {
    headerName: '成交数量',
    field: 'exec_qty',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: volumeFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      return volumeFormatter(params)
    },
  },
  {
    headerName: '目标数量',
    field: 'target_qty',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: volumeFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      return volumeFormatter(params)
    },
  },
  {
    headerName: '开/平盘口覆盖',
    field: 'open_coverage',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: percentFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      // 开仓行显示 open_coverage，平仓行显示 coverage_ratio
      const val = params.data?.order_side === 'close'
        ? params.data?.coverage_ratio
        : params.data?.open_coverage
      if (val == null) return ''
      return (Number(val) * 100).toFixed(1) + '%'
    },
  },
  {
    headerName: '开/平VWAP基差(bps)',
    field: 'open_vwap_basis_bps',
    width: 160,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return formatSummaryBasis((params.data as DisplayRow).orders, 'open_vwap_basis_bps')
      }
      // 开仓 spot 行显示 open_vwap_basis_bps，平仓 spot 行显示计算的平仓基差
      if (params.data?.market_type !== 'spot') return ''
      if (params.data?.order_side === 'close') {
        const val = params.data?._close_basis_bps
        return val != null ? val.toFixed(2) + ' bps' : ''
      }
      const val = params.data?.open_vwap_basis_bps
      return val != null ? Number(val).toFixed(2) + ' bps' : ''
    },
  },
  {
    headerName: '风险缓释',
    field: 'risk_relief_bps',
    width: 100,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellRenderer: (params: any) => {
      if (!params.data?.isGroupHeader) return ''
      const val = params.data?.risk_relief_bps
      if (val == null) return ''
      return Number(val).toFixed(2) + ' bps'
    },
  },
  {
    headerName: '开/平边际基差(bps)',
    field: 'open_marginal_basis_bps',
    width: 160,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return formatSummaryBasis((params.data as DisplayRow).orders, 'open_marginal_basis_bps')
      }
      // 开仓 spot 行显示 open_marginal_basis_bps，平仓 spot 行显示计算的平仓基差
      if (params.data?.market_type !== 'spot') return ''
      if (params.data?.order_side === 'close') {
        const val = params.data?._close_basis_bps
        return val != null ? val.toFixed(2) + ' bps' : ''
      }
      const val = params.data?.open_marginal_basis_bps
      return val != null ? Number(val).toFixed(2) + ' bps' : ''
    },
  },
  {
    headerName: '24h资金费率',
    field: 'funding_rate_24h',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: fundingRateFormatter,
    cellRenderer: (params: any) => {
      const isSummary = params.data?.isGroupHeader
      const isFuture = params.data?.market_type === 'future'
      if (!isSummary && !isFuture) return ''
      const val = params.value
      if (val == null) return ''
      return (Number(val) * 100).toFixed(4) + '%'
    },
    cellStyle: (params: any) => {
      const isSummary = params.data?.isGroupHeader
      const isFuture = params.data?.market_type === 'future'
      if ((!isSummary && !isFuture) || params.value == null) {
        return null
      }
      const v = params.value
      if (v > 0) return { color: '#67c23a' }
      if (v < 0) return { color: '#f56c6c' }
      return null
    },
  },
  {
    headerName: '开平仓原因',
    field: 'reject_reason',
    colId: 'trade_reason',
    width: 200,
    tooltipComponent: LongTextTooltip,
    tooltipValueGetter: (params: any) => {
      if (params.data?.isGroupHeader) {
        const reasons = (params.data.orders ?? []).filter((o: OrderRow) => o.market_type === 'spot' && o.status !== 'rejected' && o.reject_reason).map((o: OrderRow) => o.reject_reason)
        return reasons.length ? reasons.join(' | ') : null
      }
      if (params.data?.market_type !== 'spot') return null
      if (params.data?.status === 'rejected') return null
      return params.data?.reject_reason || null
    },
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const reasons = (params.data.orders ?? []).filter((o: OrderRow) => o.market_type === 'spot' && o.status !== 'rejected' && o.reject_reason).map((o: OrderRow) => o.reject_reason)
        return reasons.length ? reasons.join(' | ') : ''
      }
      if (params.data?.market_type !== 'spot') return ''
      if (params.data?.status === 'rejected') return ''
      return params.data?.reject_reason ?? ''
    },
  },
  {
    headerName: '拒单原因',
    field: 'reject_reason',
    colId: 'reject_reason',
    width: 200,
    tooltipComponent: LongTextTooltip,
    tooltipValueGetter: (params: any) => {
      if (params.data?.isGroupHeader) {
        const reasons = (params.data.orders ?? []).filter((o: OrderRow) => o.market_type === 'spot' && o.status === 'rejected' && o.reject_reason).map((o: OrderRow) => o.reject_reason)
        return reasons.length ? reasons.join(' | ') : null
      }
      if (params.data?.market_type !== 'spot') return null
      if (params.data?.status !== 'rejected') return null
      return params.data?.reject_reason ?? null
    },
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const reasons = (params.data.orders ?? []).filter((o: OrderRow) => o.market_type === 'spot' && o.status === 'rejected' && o.reject_reason).map((o: OrderRow) => o.reject_reason)
        return reasons.length ? reasons.join(' | ') : ''
      }
      if (params.data?.market_type !== 'spot') return ''
      if (params.data?.status !== 'rejected') return ''
      return params.data?.reject_reason ?? ''
    },
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
      if (!params.data?.isGroupHeader) return ''
      const group = params.data as DisplayRow
      const hasClose = group.orders?.some((o: any) => o.order_side === 'close')
      const openOrders = group.orders?.filter((o: any) => o.order_side === 'open') ?? []
      const allOpenExecuted = openOrders.length > 0 && openOrders.every((o: any) => o.status === 'executed')
      // 详情按钮始终显示，一键平仓仅开仓成功且未平仓时显示
      let html = `<span class="action-btns">`
      html += `<button class="detail-btn" onclick="window.openDetailDialog(${group.position_id})">详情</button>`
      if (!hasClose && allOpenExecuted) {
        html += `<button class="manual-close-btn" onclick="window.handleManualClose(${group.position_id})">平仓</button>`
      }
      html += `</span>`
      return html
    },
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

const getRowId = (params: GetRowIdParams<DisplayRow>) => {
  // 汇总行用 position_id，明细行用 id
  if (params.data?.isGroupHeader) {
    return `group_${params.data.position_id ?? params.data.order_uuid}`
  }
  return `order_${params.data?.id ?? ''}`
}

/* ───── 分组数据转换 ───── */
/**
 * 将扁平订单转换为分组展示数据
 * 每个 position_id 对应一条主记录行（不再支持展开明细）
 */
const displayRows = computed<DisplayRow[]>(() => {
  const groups = new Map<number, OrderGroup>()
  
  // 按 position_id 分组
  for (const order of rowData.value) {
    const pid = order.position_id
    if (pid == null) continue
    
    if (!groups.has(pid)) {
      groups.set(pid, {
        position_id: pid,
        base_asset: order.base_asset ?? '',
        order_uuid: order.order_uuid,
        orders: [],
        total_amount: 0,
        executed_amount: 0,
        status: 'pending',
      })
    }
    
    const group = groups.get(pid)!
    group.orders.push(order)
    
    // 计算汇总
    group.total_amount += order.target_amount ?? 0
    if (order.status === 'executed') {
      group.executed_amount += order.exec_amount ?? 0
    }
  }
  
  // 计算分组状态
  for (const group of groups.values()) {
    const executedCount = group.orders.filter(o => o.status === 'executed').length
    const rejectedCount = group.orders.filter(o => o.status === 'rejected').length
    if (executedCount === group.orders.length) {
      group.status = 'all_executed'
    } else if (rejectedCount > 0) {
      group.status = 'rejected'
    } else if (executedCount > 0) {
      group.status = 'partial'
    } else {
      group.status = 'pending'
    }
  }
  
  // 生成展示行（每个 position 一行，按 position_id 降序）
  const sortedGroups = Array.from(groups.values()).sort((a, b) => 
    (b.position_id ?? 0) - (a.position_id ?? 0)
  )
  
  return sortedGroups.map(group => ({
    isGroupHeader: true,
    position_id: group.position_id,
    base_asset: group.base_asset,
    order_uuid: group.order_uuid,
    orders: group.orders,
    total_amount: group.total_amount,
    executed_amount: group.executed_amount,
    groupStatus: group.status,
    // 风控指标
    open_vwap_basis_bps: group.orders[0]?.open_vwap_basis_bps ?? null,
    risk_relief_bps: group.orders[0]?.risk_relief_bps ?? null,
    open_marginal_basis_bps: group.orders[0]?.open_marginal_basis_bps ?? null,
    funding_rate_24h: group.orders[0]?.funding_rate_24h ?? null,
  }))
})

/** 打开订单详情弹窗 */
function openDetailDialog(positionId: number | null) {
  if (positionId == null) return
  const row = displayRows.value.find(r => r.position_id === positionId)
  if (!row?.orders?.length) return
  detailPositionId.value = positionId
  detailOrders.value = row.orders
  detailDialogVisible.value = true
}

/** 快捷时间过滤 */
function setDaysFilter(days: number) {
  filterDays.value = days
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchOrders()
}

/** 快捷状态过滤 */
function setStatusFilter(status: string) {
  statusFilter.value = status
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchOrders()
}

/** 快捷渠道过滤 */
function setChannelFilter(channel: string) {
  channelFilter.value = channel
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchOrders()
}

/** 快捷方向过滤 */
function setOrderSideFilter(side: string) {
  orderSideFilter.value = side
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchOrders()
}
async function fetchOrders() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (statusFilter.value) {
      params.set('status', statusFilter.value)
    }
    if (channelFilter.value) {
      params.set('channel', channelFilter.value)
    }
    if (orderSideFilter.value) {
      params.set('order_side', orderSideFilter.value)
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
    rowData.value = data.orders || []
    
    // 更新分页信息
    if (data.pagination) {
      paginationTotal.value = data.pagination.total || 0
    }
  } catch {
    showError('请求订单数据失败')
  } finally {
    loading.value = false
  }
}

/** 页码变化 */
function onPageChange(page: number) {
  paginationCurrentPage.value = page
  fetchOrders()
}

/** 每页条数变化 */
function onPaginationSizeChange() {
  paginationCurrentPage.value = 1 // 切换每页条数时回到第一页
  fetchOrders()
}

/** 计算总页数 */
const totalPages = computed(() => {
  return Math.ceil(paginationTotal.value / paginationPageSize.value) || 1
})

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
function onGridReady(params: GridReadyEvent<DisplayRow>) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)
}

/** 双击行打开详情弹窗 */
function onRowDoubleClicked(params: any) {
  const positionId = params.data?.position_id
  if (positionId != null) {
    openDetailDialog(positionId)
  }
}

/* ───── 状态标签快捷选项 ───── */
const statusOptions = [
  { label: '全部', value: '' },
  { label: '待执行', value: 'pending' },
  { label: '已成交', value: 'executed' },
  { label: '已拒单', value: 'rejected' },
  { label: '失败', value: 'failed' },
]

const channelOptions = [
  { label: '全部', value: '' },
  { label: 'Mock', value: 'Mock' },
  { label: '模拟盘', value: 'SimTrade' },
  { label: '实盘', value: 'Live' },
]

const orderSideOptions = [
  { label: '全部', value: '' },
  { label: '开仓', value: 'open' },
  { label: '平仓', value: 'close' },
]

/* ───── WebSocket 自动刷新 ───── */
let ws: WebSocket | null = null
let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null

function getWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getToken()
  return `${protocol}//${window.location.host}/ws/orderbook?token=${token}`
}

function connectWs() {
  if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return

  ws = new WebSocket(getWsUrl())

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      // 开仓结果、平仓结果、订单变化 → 重新加载订单列表
      if (msg.type === 'order_update' || msg.type === 'open_position_result' || msg.type === 'close_position_result') {
        fetchOrders()
      }
    } catch { /* ignore parse errors */ }
  }

  ws.onclose = () => {
    ws = null
    // 3秒后自动重连
    wsReconnectTimer = setTimeout(connectWs, 3000)
  }

  ws.onerror = () => {
    ws?.close()
  }
}

/* ───── 一键平仓 ───── */
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
      showError(`平仓失败: ${data.message || '未知错误'}`)
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

/* ───── 生命周期 ───── */
onMounted(() => {
  // 注册全局函数供 cellRenderer 使用
  ;(window as any).handleManualClose = handleManualClose
  ;(window as any).openDetailDialog = openDetailDialog
  
  fetchOrders()
  connectWs()
})

onUnmounted(() => {
  if (wsReconnectTimer) clearTimeout(wsReconnectTimer)
  if (ws) {
    ws.onclose = null // 避免触发重连
    ws.close()
    ws = null
  }
})
</script>

<template>
  <div class="monitor-page">
    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">状态：</span>
        <el-button-group size="small">
          <el-button :type="statusFilter === '' ? 'primary' : 'default'" @click="setStatusFilter('')">全部</el-button>
          <el-button :type="statusFilter === 'executed' ? 'primary' : 'default'" @click="setStatusFilter('executed')">已成交</el-button>
          <el-button :type="statusFilter === 'pending' ? 'primary' : 'default'" @click="setStatusFilter('pending')">待执行</el-button>
          <el-button :type="statusFilter === 'rejected' ? 'primary' : 'default'" @click="setStatusFilter('rejected')">被拒</el-button>
          <el-button :type="statusFilter === 'failed' ? 'primary' : 'default'" @click="setStatusFilter('failed')">失败</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">渠道：</span>
        <el-button-group size="small">
          <el-button :type="channelFilter === '' ? 'primary' : 'default'" @click="setChannelFilter('')">全部</el-button>
          <el-button :type="channelFilter === 'Mock' ? 'primary' : 'default'" @click="setChannelFilter('Mock')">Mock</el-button>
          <el-button :type="channelFilter === 'SimTrade' ? 'primary' : 'default'" @click="setChannelFilter('SimTrade')">SimTrade</el-button>
          <el-button :type="channelFilter === 'Live' ? 'primary' : 'default'" @click="setChannelFilter('Live')">Live</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">方向：</span>
        <el-button-group size="small">
          <el-button :type="orderSideFilter === '' ? 'primary' : 'default'" @click="setOrderSideFilter('')">全部</el-button>
          <el-button :type="orderSideFilter === 'open' ? 'primary' : 'default'" @click="setOrderSideFilter('open')">开仓</el-button>
          <el-button :type="orderSideFilter === 'close' ? 'primary' : 'default'" @click="setOrderSideFilter('close')">平仓</el-button>
        </el-button-group>
      </div>

      <div class="filter-row" style="margin-top: 10px;">
        <span class="filter-label">时间：</span>
        <el-button-group size="small">
          <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="setDaysFilter(1)">今日</el-button>
          <el-button :type="filterDays === 3 ? 'primary' : 'default'" @click="setDaysFilter(3)">3天</el-button>
          <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="setDaysFilter(7)">7天</el-button>
          <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="setDaysFilter(30)">30天</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">标的：</span>
        <el-select
          v-model="baseAssetFilter"
          placeholder="标的资产"
          size="small"
          filterable
          clearable
          style="width: 150px;"
          @change="fetchOrders"
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
          @click="fetchOrders"
        >
          刷新
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <span>订单管理</span>
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
        :rowData="displayRows"
        :defaultColDef="defaultColDef"
        :getRowId="getRowId"
        :header-height="32"
        :row-height="32"
        :tooltipShowDelay="300"
        @grid-ready="onGridReady"
        @row-double-clicked="onRowDoubleClicked"
      />
      </div>
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
      <el-table :data="detailOrders" border stripe size="small" style="width: 100%">
        <el-table-column prop="order_side" label="方向" width="70" :formatter="(row: OrderRow) => row.order_side === 'open' ? '开仓' : '平仓'">
          <template #default="{ row }">
            <span :style="{ color: row.order_side === 'close' ? '#e6a23c' : '#67c23a' }">
              {{ row.order_side === 'open' ? '开仓' : '平仓' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="market_type" label="市场" width="70" />
        <el-table-column prop="trade_direction" label="交易方向" width="70" />
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
  gap: 12px;
}

.status-card :deep(.el-card__body) {
  padding: 12px 16px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
  overflow-x: auto;
}

.filter-row :deep(.el-radio-group) {
  flex-wrap: nowrap;
  flex-shrink: 0;
}

.filter-label {
  color: var(--app-text-muted);
  font-size: 13px;
  white-space: nowrap;
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
  height: calc(100vh - 220px);
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
