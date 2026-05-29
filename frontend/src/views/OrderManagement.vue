<script setup lang="ts">
import { ref, shallowRef, onMounted, computed, nextTick } from 'vue'
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
  status: 'all_executed' | 'partial' | 'pending'
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
  groupStatus?: 'all_executed' | 'partial' | 'pending'
  isExpanded?: boolean
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
const timeRange = ref<[string, string] | null>(null)

/** 分组展开状态：position_id -> isExpanded */
const expandedGroups = ref<Set<number>>(new Set())

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
    headerName: '',
    field: 'isGroupHeader',
    width: 50,
    pinned: 'left',
    lockPosition: true,
    lockPinned: true,
    suppressMovable: true,
    sortable: false,
    filter: false,
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        const group = params.data as DisplayRow
        const icon = group.isExpanded ? '−' : '+'
        return `
          <div class="expand-btn-cell">
            <button class="expand-btn" onclick="window.toggleGroupExpansion(${group.position_id})">
              ${icon}
            </button>
          </div>
        `
      }
      return ''
    },
  },
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
        return `<strong class="group-asset">${group.base_asset}</strong>`
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
        return 'Mock' // 汇总行默认显示 Mock
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
        return formatSummaryVwap((params.data as DisplayRow).orders)
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
        return formatSummaryCoverage((params.data as DisplayRow).orders)
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
    headerName: '开仓/平仓原因',
    field: 'reject_reason',
    width: 300,
    tooltipField: 'reject_reason',
    tooltipComponent: LongTextTooltip,
    tooltipValueGetter: (params: any) => {
      if (params.data?.isGroupHeader) return null
      if (params.data?.market_type !== 'spot') return null
      return params.data?.reject_reason ?? null
    },
    cellRenderer: (params: any) => {
      if (params.data?.isGroupHeader) {
        return ''
      }
      if (params.data?.market_type !== 'spot') return ''
      return params.value ?? ''
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
 * 1. 按 position_id 分组
 * 2. 每组生成汇总行
 * 3. 展开的组追加明细行
 */
const displayRows = computed<DisplayRow[]>(() => {
  const groups = new Map<number, OrderGroup>()
  
  // 按 position_id 分组
  for (const order of rowData.value) {
    const pid = order.position_id
    if (pid == null) continue // 无 position_id 的订单暂不分组
    
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
    if (executedCount === group.orders.length) {
      group.status = 'all_executed'
    } else if (executedCount > 0) {
      group.status = 'partial'
    } else {
      group.status = 'pending'
    }
  }
  
  // 生成展示行
  const rows: DisplayRow[] = []
  
  // 按 position_id 降序排列（最新在前）
  const sortedGroups = Array.from(groups.values()).sort((a, b) => 
    (b.position_id ?? 0) - (a.position_id ?? 0)
  )
  
  for (const group of sortedGroups) {
    const isExpanded = expandedGroups.value.has(group.position_id ?? 0)
    
    // 汇总行
    rows.push({
      isGroupHeader: true,
      position_id: group.position_id,
      base_asset: group.base_asset,
      order_uuid: group.order_uuid,
      orders: group.orders,
      total_amount: group.total_amount,
      executed_amount: group.executed_amount,
      groupStatus: group.status,
      isExpanded,
      // 风控指标：同一组订单共享，仅在汇总行展示
      open_vwap_basis_bps: group.orders[0]?.open_vwap_basis_bps ?? null,
      risk_relief_bps: group.orders[0]?.risk_relief_bps ?? null,
      open_marginal_basis_bps: group.orders[0]?.open_marginal_basis_bps ?? null,
      funding_rate_24h: group.orders[0]?.funding_rate_24h ?? null,
    })
    
    // 明细行（展开时）
    if (isExpanded) {
      const closeBasis = computeCloseBasisBps(group.orders)
      for (const order of group.orders) {
        const row: DisplayRow = {
          isGroupHeader: false,
          ...order,
        }
        // 为平仓 spot 行附加计算的平仓基差
        if (order.order_side === 'close' && order.market_type === 'spot') {
          row._close_basis_bps = closeBasis
        }
        rows.push(row)
      }
    }
  }
  
  return rows
})

/** 切换分组展开状态 */
function toggleGroupExpansion(positionId: number | null) {
  if (positionId == null) return
  const newSet = new Set(expandedGroups.value)
  if (newSet.has(positionId)) {
    newSet.delete(positionId)
  } else {
    newSet.add(positionId)
  }
  expandedGroups.value = newSet
  
  // 强制 AG Grid 刷新受影响的行
  if (gridApi) {
    nextTick(() => {
      gridApi?.redrawRows()
    })
  }
}

/** 全展开/全收起 */
function toggleAllGroups(expand: boolean) {
  if (expand) {
    const allIds = new Set<number>()
    for (const order of rowData.value) {
      if (order.position_id != null) {
        allIds.add(order.position_id)
      }
    }
    expandedGroups.value = allIds
  } else {
    expandedGroups.value = new Set()
  }
}
async function fetchOrders() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (statusFilter.value) {
      params.set('status', statusFilter.value)
    }
    if (channelFilter.value) {
      params.set('channel', channelFilter.value)
    }
    if (timeRange.value && timeRange.value[0]) {
      params.set('start_time', timeRange.value[0])
    }
    if (timeRange.value && timeRange.value[1]) {
      params.set('end_time', timeRange.value[1])
    }
    const query = params.toString()
    const url = `/api/trading/orders${query ? '?' + query : ''}`
    const res = await get(url)
    if (!res.ok) {
      showError('获取订单数据失败')
      return
    }
    const data = await res.json()
    rowData.value = Array.isArray(data) ? data : (data.orders ?? [])
  } catch {
    showError('请求订单数据失败')
  } finally {
    loading.value = false
  }
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
function onGridReady(params: GridReadyEvent<DisplayRow>) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)
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

/* ───── 生命周期 ───── */
onMounted(() => {
  // 注册全局函数供 cellRenderer 使用
  ;(window as any).toggleGroupExpansion = toggleGroupExpansion
  
  fetchOrders()
})
</script>

<template>
  <div class="monitor-page">
    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">状态过滤：</span>
        <el-radio-group v-model="statusFilter" size="small" @change="fetchOrders">
          <el-radio-button
            v-for="opt in statusOptions"
            :key="opt.value"
            :value="opt.value"
          >
            {{ opt.label }}
          </el-radio-button>
        </el-radio-group>

        <span class="filter-label" style="margin-left: 24px;">渠道过滤：</span>
        <el-radio-group v-model="channelFilter" size="small" @change="fetchOrders">
          <el-radio-button
            v-for="opt in channelOptions"
            :key="opt.value"
            :value="opt.value"
          >
            {{ opt.label }}
          </el-radio-button>
        </el-radio-group>

        <span class="filter-label" style="margin-left: 24px;">时间范围：</span>
        <el-date-picker
          v-model="timeRange"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          size="small"
          value-format="YYYY-MM-DDTHH:mm:ss"
          style="width: 360px;"
          @change="fetchOrders"
        />

        <el-button
          size="small"
          type="primary"
          style="margin-left: 16px;"
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
            <el-button size="small" @click="toggleAllGroups(true)">
              全展开
            </el-button>
            <el-button size="small" @click="toggleAllGroups(false)">
              全收起
            </el-button>
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
:deep(.expand-btn-cell) {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

:deep(.expand-btn) {
  width: 20px;
  height: 20px;
  border: 1px solid var(--el-border-color);
  border-radius: 3px;
  background: var(--el-bg-color);
  color: var(--el-text-color-primary);
  font-size: 14px;
  font-weight: bold;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}

:deep(.expand-btn:hover) {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}


:deep(.group-asset) {
  color: var(--el-color-primary);
}

:deep(.group-executed) {
  color: var(--el-color-success);
}

/* 汇总行背景色 */
:deep(.ag-row[data-row-index]) {
  background: transparent;
}
</style>
