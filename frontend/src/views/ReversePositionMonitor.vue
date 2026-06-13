<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
  ValueGetterParams,
} from 'ag-grid-community'
import { ElPopover } from 'element-plus'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'
import { getToken } from '../utils/auth'

interface ReversePositionRow {
  id: number
  order_uuid: string | null
  signal_id: number | null
  base_asset: string | null
  spot_symbol: string | null
  future_contract: string | null
  status: string | null
  opened_at: string | null
  closed_at: string | null
  close_reason: string | null
  open_amount_usdt: number | null
  close_amount_usdt: number | null
  borrow_asset: string | null
  borrow_qty: number | null
  borrow_repaid_qty: number | null
  borrow_hourly_rate: number | null
  open_borrow_24h_bps: number | null
  borrow_interest_usdt: number | null
  borrow_interest_bps: number | null
  borrow_interest_realtime_usdt: number | null
  borrow_interest_realtime_bps: number | null
  spot_open_qty: number | null
  spot_open_price: number | null
  spot_open_amount: number | null
  spot_close_qty: number | null
  spot_close_price: number | null
  spot_close_amount: number | null
  future_open_qty: number | null
  future_open_price: number | null
  future_open_amount: number | null
  future_close_qty: number | null
  future_close_price: number | null
  future_close_amount: number | null
  reverse_open_basis_bps: number | null
  reverse_close_basis_bps: number | null
  reverse_open_basis_p20: number | null
  reverse_close_basis_p20: number | null
  signal_basis_bps: number | null
  pre_gate_basis_bps: number | null
  actual_basis_bps: number | null
  execution_drift_bps: number | null
  open_funding_rate_24h: number | null
  funding_pnl_usdt: number | null
  funding_pnl_bps: number | null
  fee_total_usdt: number | null
  fee_total_bps: number | null
  realized_pnl_usdt: number | null
  realized_pnl_bps: number | null
  current_spot_price: number | null
  current_future_price: number | null
  current_spread_bps: number | null
  floating_spot_pnl: number | null
  floating_future_pnl: number | null
  floating_pnl_total: number | null
  floating_pnl_bps: number | null
  funding_total_pnl: number | null
  fee_cost: number | null
  total_pnl: number | null
  total_pnl_bps: number | null
  exchange_risk_status: string | null
  exchange_risk_type: string | null
  exchange_risk_at: string | null
  exchange_risk_detail: string | null
}

interface PositionSummary {
  total: number
  holding: number
  closed: number
  exchange_risk: number
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_position_monitor'
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

const rowData = shallowRef<ReversePositionRow[]>([])
let gridApi: GridApi<ReversePositionRow> | null = null
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let pingInterval: ReturnType<typeof setInterval> | null = null
const loading = ref(false)
const wsStatus = ref<'connecting' | 'connected' | 'disconnected'>('disconnected')
const wsLatencyMs = ref<number | null>(null)
const statusFilter = ref('')
const baseAssetFilter = ref('')
const exchangeRiskOnly = ref(false)
const filterDays = ref(90)
const positionSummary = ref<PositionSummary>({ total: 0, holding: 0, closed: 0, exchange_risk: 0 })

const paginationPageSize = ref(100)
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)
const columnVisibilities = ref<ColumnVisibility[]>([])

const totalPages = computed(() => Math.ceil(paginationTotal.value / paginationPageSize.value) || 1)

const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map((row) => row.base_asset).filter(Boolean) as string[])
  return Array.from(assets).sort()
})

const filteredRows = computed(() => {
  let rows = rowData.value
  if (statusFilter.value) rows = rows.filter((row) => row.status === statusFilter.value)
  if (baseAssetFilter.value) rows = rows.filter((row) => row.base_asset === baseAssetFilter.value)
  if (exchangeRiskOnly.value) {
    rows = rows.filter((row) => row.exchange_risk_status && row.exchange_risk_status !== 'normal')
  }
  return rows
})

const summaryStats = computed(() => {
  const rows = filteredRows.value
  const holdingCount = rows.filter((row) => row.status !== 'closed').length
  const closedCount = rows.filter((row) => row.status === 'closed').length
  const riskCount = rows.filter((row) => row.exchange_risk_status && row.exchange_risk_status !== 'normal').length
  const totalFundingPnl = sumRows(rows, 'funding_pnl_usdt')
  const totalBorrowInterest = sumRows(rows, 'borrow_interest_usdt')
  const totalFees = sumRows(rows, 'fee_cost')
  const totalRealizedPnl = sumRows(rows, 'realized_pnl_usdt')
  const totalFloatingPnl = sumRows(rows, 'floating_pnl_total')
  const totalPnl = rows.reduce((sum, row) => sum + Number(row.total_pnl ?? 0), 0)
  return {
    holdingCount,
    closedCount,
    riskCount,
    totalCount: rows.length,
    totalFloatingPnl,
    totalFundingPnl,
    totalBorrowInterest,
    totalFees,
    totalRealizedPnl,
    totalPnl,
  }
})

function sumRows(rows: ReversePositionRow[], field: keyof ReversePositionRow): number {
  return rows.reduce((sum, row) => {
    const value = row[field]
    return sum + (typeof value === 'number' ? value : Number(value || 0))
  }, 0)
}

function rowNotional(row: ReversePositionRow): number {
  return Number(row.open_amount_usdt || row.spot_open_amount || row.future_open_amount || 0)
}

function bpsFromTotalAmount(rows: ReversePositionRow[], field: keyof ReversePositionRow): number {
  const notional = rows.reduce((sum, row) => sum + rowNotional(row), 0)
  if (!Number.isFinite(notional) || notional <= 0) return 0
  return sumRows(rows, field) / notional * 10000
}

function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  const n = Number(value)
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function formatAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatPnl(value: number): string {
  const prefix = value >= 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}`
}

const amountFormatter = (params: ValueFormatterParams) => {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return Number(params.value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const decimalFormatter = (params: ValueFormatterParams) => formatDecimal(params.value as number | null)

const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return Number(params.value).toFixed(2)
}

const pnlFormatter = (params: ValueFormatterParams) => {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return Number(params.value).toFixed(4)
}

const rateFormatter = (params: ValueFormatterParams) => {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return `${(Number(params.value) * 100).toFixed(6)}%`
}

const timeFormatter = (params: ValueFormatterParams) => {
  if (!params.value) return ''
  const d = new Date(params.value)
  if (Number.isNaN(d.getTime())) return params.value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function statusLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    holding: '持仓中',
    closing: '平仓中',
    closed: '已平仓',
    risk: '风险中',
    desynced: '对账异常',
  }
  return value ? (map[value] || value) : ''
}

const statusCellStyle = (params: ValueFormatterParams) => {
  if (params.value === 'holding') return { color: '#67c23a' }
  if (params.value === 'closing') return { color: '#e6a23c' }
  if (params.value === 'closed') return { color: '#909399' }
  if (params.value === 'risk' || params.value === 'desynced') return { color: '#f56c6c', fontWeight: '700' }
  return { color: '#909399' }
}

function riskLabel(row: ReversePositionRow | null | undefined): string {
  if (!row?.exchange_risk_status || row.exchange_risk_status === 'normal') return ''
  const typeMap: Record<string, string> = {
    reverse_open_partial_or_failed: '反向开仓缺腿',
    missing_margin_position: '杠杆缺腿',
    missing_gate_position: 'Gate缺腿',
    qty_mismatch: '数量不匹配',
    unknown: '交易所风险',
  }
  return typeMap[row.exchange_risk_type || 'unknown'] || row.exchange_risk_type || row.exchange_risk_status
}

const exchangeRiskCellStyle = (params: ValueFormatterParams) => {
  const row = params.data as ReversePositionRow | undefined
  if (row?.exchange_risk_status && row.exchange_risk_status !== 'normal') {
    return { color: '#f56c6c', fontWeight: '700' }
  }
  return { color: '#909399', fontWeight: '400' }
}

const pnlCellStyle = (params: ValueFormatterParams) => {
  const value = Number(params.value)
  if (!Number.isFinite(value)) return { color: '#909399' }
  if (value > 0) return { color: '#f56c6c' }
  if (value < 0) return { color: '#67c23a' }
  return { color: '#e8eaed' }
}

const fundingCellStyle = (params: ValueFormatterParams) => {
  const value = Number(params.value)
  if (!Number.isFinite(value)) return { color: '#909399' }
  if (value < 0) return { color: '#67c23a' }
  if (value > 0) return { color: '#f56c6c' }
  return { color: '#e8eaed' }
}

const columnDefs = computed<ColDef<ReversePositionRow>[]>(() => [
  { headerName: '开仓时间', field: 'opened_at', width: 180, valueFormatter: timeFormatter },
  { headerName: '标的资产', field: 'base_asset', width: 100, pinned: 'left' },
  { headerName: '现货', field: 'spot_symbol', width: 105 },
  { headerName: '期货', field: 'future_contract', width: 115 },
  { headerName: '状态', field: 'status', width: 90, valueFormatter: (p) => statusLabel(p.value), cellStyle: statusCellStyle },
  {
    headerName: '交易所风险',
    field: 'exchange_risk_type',
    width: 135,
    valueFormatter: (params) => riskLabel(params.data as ReversePositionRow),
    cellStyle: exchangeRiskCellStyle,
    tooltipValueGetter: (params: any) => params.data?.exchange_risk_detail || null,
    tooltipComponent: LongTextTooltip,
  },
  { headerName: '开仓金额', field: 'open_amount_usdt', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: amountFormatter },
  { headerName: '实时现货平仓VWAP', field: 'current_spot_price', width: 145, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '实时合约平仓VWAP', field: 'current_future_price', width: 145, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '实时平仓VWAP基差(bps)', field: 'current_spread_bps', width: 165, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter },
  { headerName: '实时浮动盈亏', field: 'floating_pnl_total', width: 125, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: pnlFormatter, cellStyle: pnlCellStyle },
  { headerName: '实时浮动盈亏(bps)', field: 'floating_pnl_bps', width: 145, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter, cellStyle: pnlCellStyle },
  { headerName: '总盈亏(bps)', field: 'total_pnl_bps', width: 120, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter, cellStyle: pnlCellStyle },
  { headerName: '总盈亏', field: 'total_pnl', width: 115, type: 'numericColumn', enableCellChangeFlash: true, cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: pnlFormatter, cellStyle: pnlCellStyle },
  { headerName: '借币资产', field: 'borrow_asset', width: 95 },
  { headerName: '借币数量', field: 'borrow_qty', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '已还数量', field: 'borrow_repaid_qty', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '借币小时利率', field: 'borrow_hourly_rate', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: rateFormatter },
  { headerName: '借币24h成本(bps)', field: 'open_borrow_24h_bps', width: 140, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter },
  {
    headerName: '实际借币费(bps)',
    colId: 'borrow_interest_cost_bps',
    valueGetter: (params: ValueGetterParams<ReversePositionRow>) => -Number(params.data?.borrow_interest_bps ?? 0),
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: pnlCellStyle,
  },
  {
    headerName: '实际借币费',
    colId: 'borrow_interest_cost',
    valueGetter: (params: ValueGetterParams<ReversePositionRow>) => -Number(params.data?.borrow_interest_usdt ?? 0),
    width: 115,
    type: 'numericColumn',
    enableCellChangeFlash: true,
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: pnlFormatter,
    cellStyle: pnlCellStyle,
  },
  { headerName: '开仓24h资金费', field: 'open_funding_rate_24h', width: 135, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: rateFormatter, cellStyle: fundingCellStyle },
  { headerName: '实际资金费(bps)', field: 'funding_pnl_bps', width: 135, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter, cellStyle: pnlCellStyle },
  { headerName: '实际资金费', field: 'funding_pnl_usdt', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: pnlFormatter, cellStyle: pnlCellStyle },
  { headerName: '手续费(bps)', field: 'fee_total_bps', width: 110, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter, cellStyle: pnlCellStyle },
  { headerName: '手续费', field: 'fee_cost', width: 100, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: pnlFormatter, cellStyle: pnlCellStyle },
  { headerName: '实现盈亏(bps)', field: 'realized_pnl_bps', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter, cellStyle: pnlCellStyle },
  { headerName: '实现盈亏', field: 'realized_pnl_usdt', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: pnlFormatter, cellStyle: pnlCellStyle },
  { headerName: '现货开仓VWAP', field: 'spot_open_price', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '合约开仓VWAP', field: 'future_open_price', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '成交现货平仓VWAP', field: 'spot_close_price', width: 145, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  { headerName: '成交合约平仓VWAP', field: 'future_close_price', width: 145, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: decimalFormatter },
  {
    headerName: '实际开仓基差(bps)',
    colId: 'display_open_basis_bps',
    valueGetter: (params: ValueGetterParams<ReversePositionRow>) => params.data?.actual_basis_bps ?? params.data?.reverse_open_basis_bps,
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  { headerName: '开仓VWAP阈值', field: 'reverse_open_basis_p20', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter },
  { headerName: '平仓基差(bps)', field: 'reverse_close_basis_bps', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter },
  { headerName: '平仓VWAP阈值', field: 'reverse_close_basis_p20', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter },
  {
    headerName: '信号/旁路/成交基差',
    colId: 'basis_flow',
    width: 190,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    cellRenderer: (params: any) => {
      const row = params.data as ReversePositionRow
      const fmt = (v: number | null | undefined) => (v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(2) : '-')
      return `${fmt(row?.signal_basis_bps)}/${fmt(row?.pre_gate_basis_bps)}/${fmt(row?.actual_basis_bps)}`
    },
  },
  { headerName: '成交滑点(bps)', field: 'execution_drift_bps', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', headerClass: 'ag-right-aligned-header', valueFormatter: bpsFormatter, cellStyle: pnlCellStyle },
  {
    headerName: '合约数量',
    field: 'future_open_qty',
    width: 115,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: decimalFormatter,
  },
  {
    headerName: '未还借币',
    colId: 'borrow_unrepaid_qty',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueGetter: (params: ValueGetterParams<ReversePositionRow>) => {
      const row = params.data
      if (!row) return null
      return Number(row.borrow_qty || 0) - Number(row.borrow_repaid_qty || 0)
    },
    valueFormatter: decimalFormatter,
  },
  {
    headerName: '平仓原因',
    field: 'close_reason',
    width: 260,
    tooltipField: 'close_reason',
    tooltipComponent: LongTextTooltip,
  },
  { headerName: '平仓时间', field: 'closed_at', width: 180, valueFormatter: timeFormatter },
  { headerName: '订单UUID', field: 'order_uuid', width: 170 },
])

const defaultColDef: ColDef<ReversePositionRow> = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

const initialSortModel = [{ colId: 'opened_at', sort: 'desc' as const }]

const getRowId = (params: GetRowIdParams<ReversePositionRow>) => String(params.data?.id ?? '')

const getRowClass = (params: any) => {
  if (params.data?.exchange_risk_status && params.data.exchange_risk_status !== 'normal') return 'position-row-exchange-risk'
  return ''
}

const pinnedBottomRowData = computed<ReversePositionRow[]>(() => {
  const rows = filteredRows.value
  if (rows.length === 0) return []
  const borrowInterest = sumRows(rows, 'borrow_interest_usdt')
  const fundingPnl = sumRows(rows, 'funding_pnl_usdt')
  const feeCost = sumRows(rows, 'fee_cost')
  const feeAmount = sumRows(rows, 'fee_total_usdt')
  const realizedPnl = sumRows(rows, 'realized_pnl_usdt')
  const floatingPnl = sumRows(rows, 'floating_pnl_total')
  const totalPnl = sumRows(rows, 'total_pnl')
  return [{
    id: -1,
    order_uuid: null,
    signal_id: null,
    base_asset: '汇总',
    spot_symbol: null,
    future_contract: null,
    status: null,
    opened_at: null,
    closed_at: null,
    close_reason: null,
    open_amount_usdt: sumRows(rows, 'open_amount_usdt'),
    close_amount_usdt: sumRows(rows, 'close_amount_usdt'),
    borrow_asset: null,
    borrow_qty: sumRows(rows, 'borrow_qty'),
    borrow_repaid_qty: sumRows(rows, 'borrow_repaid_qty'),
    borrow_hourly_rate: null,
    open_borrow_24h_bps: null,
    borrow_interest_usdt: borrowInterest,
    borrow_interest_bps: bpsFromTotalAmount(rows, 'borrow_interest_usdt'),
    borrow_interest_realtime_usdt: rows.reduce((sum, row) => sum + Number(row.borrow_interest_realtime_usdt ?? row.borrow_interest_usdt ?? 0), 0),
    borrow_interest_realtime_bps: sumRows(rows, 'borrow_interest_realtime_bps'),
    spot_open_qty: sumRows(rows, 'spot_open_qty'),
    spot_open_price: null,
    spot_open_amount: sumRows(rows, 'spot_open_amount'),
    spot_close_qty: sumRows(rows, 'spot_close_qty'),
    spot_close_price: null,
    spot_close_amount: sumRows(rows, 'spot_close_amount'),
    future_open_qty: sumRows(rows, 'future_open_qty'),
    future_open_price: null,
    future_open_amount: sumRows(rows, 'future_open_amount'),
    future_close_qty: sumRows(rows, 'future_close_qty'),
    future_close_price: null,
    future_close_amount: sumRows(rows, 'future_close_amount'),
    reverse_open_basis_bps: null,
    reverse_close_basis_bps: null,
    reverse_open_basis_p20: null,
    reverse_close_basis_p20: null,
    signal_basis_bps: null,
    pre_gate_basis_bps: null,
    actual_basis_bps: null,
    execution_drift_bps: null,
    open_funding_rate_24h: null,
    funding_pnl_usdt: fundingPnl,
    funding_pnl_bps: bpsFromTotalAmount(rows, 'funding_pnl_usdt'),
    fee_total_usdt: feeAmount,
    fee_total_bps: bpsFromTotalAmount(rows, 'fee_cost'),
    realized_pnl_usdt: realizedPnl,
    realized_pnl_bps: bpsFromTotalAmount(rows, 'realized_pnl_usdt'),
    current_spot_price: null,
    current_future_price: null,
    current_spread_bps: null,
    floating_spot_pnl: sumRows(rows, 'floating_spot_pnl'),
    floating_future_pnl: sumRows(rows, 'floating_future_pnl'),
    floating_pnl_total: floatingPnl,
    floating_pnl_bps: bpsFromTotalAmount(rows, 'floating_pnl_total'),
    funding_total_pnl: sumRows(rows, 'funding_total_pnl'),
    fee_cost: feeCost,
    total_pnl: totalPnl,
    total_pnl_bps: bpsFromTotalAmount(rows, 'total_pnl'),
    exchange_risk_status: null,
    exchange_risk_type: null,
    exchange_risk_at: null,
    exchange_risk_detail: null,
  }]
})

function getWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getToken()
  return `${protocol}//${window.location.host}/ws/orderbook?token=${token}&mode=events`
}

function matchesActiveFilter(row: ReversePositionRow): boolean {
  if (statusFilter.value && row.status !== statusFilter.value) return false
  if (baseAssetFilter.value && row.base_asset !== baseAssetFilter.value) return false
  if (exchangeRiskOnly.value && (!row.exchange_risk_status || row.exchange_risk_status === 'normal')) return false
  return true
}

function applyRealtimeUpdates(updates: ReversePositionRow[], summary?: any) {
  const filtered = updates.filter(matchesActiveFilter)
  paginationTotal.value = filtered.length
  if (summary) {
    positionSummary.value = {
      total: Number(summary.total || 0),
      holding: Number(summary.open || summary.holding || 0),
      closed: Number(summary.close || summary.closed || 0),
      exchange_risk: Number(summary.exchange_risk || 0),
    }
  }
  const offset = (paginationCurrentPage.value - 1) * paginationPageSize.value
  rowData.value = filtered.slice(offset, offset + paginationPageSize.value)
}

function connectWs() {
  if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return
  wsStatus.value = 'connecting'
  socket = new WebSocket(getWsUrl())

  socket.onopen = () => {
    wsStatus.value = 'connected'
    wsLatencyMs.value = null
    if (pingInterval) clearInterval(pingInterval)
    pingInterval = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
    }, 10000)
    socket?.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
  }

  socket.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'pong' && msg.ts) {
        wsLatencyMs.value = Date.now() - msg.ts
        return
      }
      if (msg.type === 'reverse_position_update' && Array.isArray(msg.positions)) {
        applyRealtimeUpdates(msg.positions, msg.summary)
      }
    } catch {
      /* ignore */
    }
  }

  socket.onclose = () => {
    wsStatus.value = 'disconnected'
    wsLatencyMs.value = null
    if (pingInterval) {
      clearInterval(pingInterval)
      pingInterval = null
    }
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(connectWs, 3000)
  }

  socket.onerror = () => {
    wsStatus.value = 'disconnected'
    wsLatencyMs.value = null
  }
}

async function fetchPositions(resetPage = false) {
  if (loading.value) return
  if (resetPage) paginationCurrentPage.value = 1
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (statusFilter.value) params.set('status', statusFilter.value)
    if (baseAssetFilter.value) params.set('base_asset', baseAssetFilter.value.trim())
    if (exchangeRiskOnly.value) params.set('exchange_risk', 'true')

    const res = await get(`/api/trading/reverse-positions/realtime?${params.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '获取反向持仓数据失败')
      return
    }
    rowData.value = Array.isArray(data.positions) ? data.positions : []
    paginationTotal.value = Number(data.pagination?.total || 0)
    positionSummary.value = {
      total: Number(data.summary?.total || 0),
      holding: Number(data.summary?.open || data.summary?.holding || 0),
      closed: Number(data.summary?.close || data.summary?.closed || 0),
      exchange_risk: Number(data.summary?.exchange_risk || 0),
    }
  } catch {
    showError('请求反向持仓数据失败')
  } finally {
    loading.value = false
  }
}

function onPageChange(page: number | null) {
  paginationCurrentPage.value = Number(page || 1)
  fetchPositions()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchPositions()
}

function setStatusFilter(status: string) {
  statusFilter.value = status
  paginationCurrentPage.value = 1
  fetchPositions()
}

function setDaysFilter(days: number) {
  filterDays.value = days
  paginationCurrentPage.value = 1
  fetchPositions()
}

function setExchangeRiskOnly(enabled: boolean) {
  exchangeRiskOnly.value = enabled
  paginationCurrentPage.value = 1
  fetchPositions()
}

function onBaseAssetFilterChange() {
  paginationCurrentPage.value = 1
  fetchPositions()
}

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field || col.colId)
    .map((col) => {
      const colId = (col.field ?? col.colId) as string
      const state = states.find((item) => item.colId === colId)
      return { colId, headerName: col.headerName ?? colId, visible: state?.hide !== true }
    })
}

function toggleColumnVisibility(colId: string, visible: boolean) {
  if (!gridApi) return
  gridApi.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((item) => item.colId === colId)
  if (col) col.visible = visible
}

async function saveColumnState() {
  if (!gridApi) return
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, { columnState: gridApi.getColumnState() })
    const data = await res.json()
    if (data?.success) showSuccess('列配置已保存')
    else showError(data?.message || '保存列配置失败')
  } catch {
    showError('保存列配置失败')
  }
}

async function loadColumnState() {
  if (!gridApi) return
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEY}`)
    const data = await res.json()
    if (Array.isArray(data?.columnState)) gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
  } catch {
    /* ignore */
  }
}

function onGridReady(params: GridReadyEvent<ReversePositionRow>) {
  gridApi = params.api
  setupGridCopy(params.api)
  loadColumnState()
}

onMounted(() => {
  fetchPositions()
  connectWs()
})

onUnmounted(() => {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (pingInterval) clearInterval(pingInterval)
  if (socket) {
    socket.onclose = null
    socket.close()
  }
})
</script>

<template>
  <div class="monitor-page">
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
          <span class="summary-label">风险</span>
          <span class="summary-value warning">{{ summaryStats.riskCount }}</span>
        </span>
        <span class="summary-divider">/</span>
        <span class="summary-item">
          <span class="summary-label">总持仓</span>
          <span class="summary-value">{{ summaryStats.totalCount }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">WS</span>
          <span class="summary-value" :class="wsStatus === 'connected' ? 'pnl-positive' : 'warning'">
            {{ wsStatus === 'connected' ? '已连接' : wsStatus === 'connecting' ? '连接中' : '未连接' }}
          </span>
          <span v-if="wsLatencyMs != null" class="summary-label">{{ wsLatencyMs }}ms</span>
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
          <span class="summary-label">累计资金费</span>
          <span class="summary-value" :class="summaryStats.totalFundingPnl >= 0 ? 'pnl-positive' : 'pnl-negative'">{{ formatPnl(summaryStats.totalFundingPnl) }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">借币利息</span>
          <span class="summary-value pnl-negative">{{ formatPnl(-summaryStats.totalBorrowInterest) }}</span>
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
        <span class="filter-label">状态：</span>
        <el-button-group size="small">
          <el-button :type="statusFilter === '' ? 'primary' : 'default'" @click="setStatusFilter('')">全部({{ positionSummary.total }})</el-button>
          <el-button :type="statusFilter === 'holding' ? 'primary' : 'default'" @click="setStatusFilter('holding')">持仓中({{ positionSummary.holding }})</el-button>
          <el-button :type="statusFilter === 'closed' ? 'primary' : 'default'" @click="setStatusFilter('closed')">已平仓({{ positionSummary.closed }})</el-button>
          <el-button :type="statusFilter === 'closing' ? 'primary' : 'default'" @click="setStatusFilter('closing')">平仓中</el-button>
          <el-button :type="statusFilter === 'risk' ? 'primary' : 'default'" @click="setStatusFilter('risk')">风险中</el-button>
          <el-button :type="statusFilter === 'desynced' ? 'primary' : 'default'" @click="setStatusFilter('desynced')">对账异常</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">交易所风险：</span>
        <el-button-group size="small">
          <el-button :type="!exchangeRiskOnly ? 'primary' : 'default'" @click="setExchangeRiskOnly(false)">全部</el-button>
          <el-button :type="exchangeRiskOnly ? 'primary' : 'default'" @click="setExchangeRiskOnly(true)">有风险({{ positionSummary.exchange_risk }})</el-button>
        </el-button-group>
      </div>

      <div class="filter-row" style="margin-top: 10px;">
        <span class="filter-label">时间：</span>
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
          @click="fetchPositions()"
        >
          刷新
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <span>反向持仓监控</span>
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
            <el-button size="small" @click="saveColumnState">保存列配置</el-button>
          </div>
        </div>
      </template>
      <div ref="gridContainerRef">
        <AgGridVue
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
          :tooltipShowDelay="300"
          @grid-ready="onGridReady"
        />
      </div>
    </el-card>

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
}

.summary-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.summary-label {
  color: var(--app-text-muted);
  font-size: 13px;
}

.summary-value {
  font-weight: 600;
  color: var(--app-text);
}

.summary-value.warning {
  color: #e6a23c;
}

.pnl-positive {
  color: #f56c6c;
}

.pnl-negative {
  color: #67c23a;
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
  height: calc(100vh - 270px);
  min-height: 420px;
}

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

.column-picker-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

:deep(.position-row-exchange-risk) {
  background-color: rgba(245, 108, 108, 0.08) !important;
}

:deep(.ag-row-pinned) {
  font-weight: 600;
  background: rgba(64, 158, 255, 0.08) !important;
}

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
