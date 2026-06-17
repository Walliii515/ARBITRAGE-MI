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

interface ReverseOrderRow {
  id: number
  created_at: string | null
  order_uuid: string | null
  position_id: number | null
  signal_id: number | null
  base_asset: string
  order_side: string | null
  market_type: string | null
  trade_direction: string | null
  status: string | null
  target_qty: number | null
  target_amount: number | null
  exec_price: number | null
  exec_qty: number | null
  exec_amount: number | null
  fee_amount_usdt: number | null
  reduce_only: number | boolean | null
  execution_style: string | null
  exchange_order_id: string | null
  reject_reason: string | null
}

interface ReversePositionRow {
  id: number
  order_uuid: string | null
  signal_id: number | null
  base_asset: string
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
  exchange_risk_status: string | null
  exchange_risk_type: string | null
  exchange_risk_at: string | null
  exchange_risk_detail: string | null
  order_count?: number | null
}

interface ReverseOrderSummary {
  total: number
  open: number
  close: number
  exchange_risk: number
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_order_management'
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

const rowData = shallowRef<ReversePositionRow[]>([])
let gridApi: GridApi<ReversePositionRow> | null = null
const loading = ref(false)
const orderSideFilter = ref('')
const exchangeRiskOnly = ref(false)
const baseAssetFilter = ref('')
const filterDays = ref(90)
const orderSummary = ref<ReverseOrderSummary>({ total: 0, open: 0, close: 0, exchange_risk: 0 })

const paginationPageSize = ref(50)
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)
const columnVisibilities = ref<ColumnVisibility[]>([])

const detailDialogVisible = ref(false)
const detailOrders = ref<ReverseOrderRow[]>([])
const detailPositionId = ref<number | null>(null)
const detailLoading = ref(false)

const totalPages = computed(() => Math.ceil(paginationTotal.value / paginationPageSize.value) || 1)

const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map((row) => row.base_asset).filter(Boolean) as string[])
  return Array.from(assets).sort()
})

function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  const n = Number(value)
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function formatTime(val: string | null | undefined): string {
  if (!val) return '-'
  const d = new Date(val)
  if (Number.isNaN(d.getTime())) return '-'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function formatAmount(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(Number(val))) return '-'
  return Number(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatRateValue(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  return `${(Number(value) * 100).toFixed(6)}%`
}

const timeFormatter = (params: ValueFormatterParams) => formatTime(params.value)

const amountFormatter = (params: ValueFormatterParams) => {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return Number(params.value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const decimalFormatter = (params: ValueFormatterParams) => formatDecimal(params.value as number | null)

const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return `${Number(params.value).toFixed(2)} bps`
}

const rateFormatter = (params: ValueFormatterParams) => formatRateValue(params.value as number | null)

function positionStatusLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    holding: '持仓中',
    closing: '平仓中',
    closed: '已平仓',
    risk: '风险中',
    desynced: '对账异常',
  }
  return value ? (map[value] || value) : ''
}

function orderStatusLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    pending: '待执行',
    filled: '已成交',
    partial: '部分成交',
    failed: '失败',
    cancelled: '已取消',
    skipped: '跳过',
  }
  return value ? (map[value] || value) : ''
}

function sideLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    open: '开仓',
    close: '平仓',
    repay: '还币',
    unwind: '解腿',
  }
  return value ? (map[value] || value) : ''
}

function marketLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    margin_spot: '杠杆现货',
    future: '合约',
    margin_repay: '杠杆还币',
  }
  return value ? (map[value] || value) : ''
}

function riskLabel(row: ReversePositionRow | null | undefined): string {
  if (!row?.exchange_risk_status || row.exchange_risk_status === 'normal') return ''
  return row.exchange_risk_type ? `${row.exchange_risk_status}/${row.exchange_risk_type}` : row.exchange_risk_status
}

function signedCellClass(params: { value?: unknown }) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  if (n > 0) return 'value-positive'
  if (n < 0) return 'value-negative'
  return ''
}

const columnDefs = computed((): ColDef<ReversePositionRow>[] => [
  { headerName: '开仓时间', field: 'opened_at', width: 180, valueFormatter: timeFormatter },
  { headerName: '平仓时间', field: 'closed_at', width: 180, valueFormatter: timeFormatter },
  {
    headerName: '标的资产',
    field: 'base_asset',
    width: 120,
    pinned: 'left',
    cellRenderer: (params: any) => {
      const row = params.data as ReversePositionRow
      const count = row?.order_count ?? 0
      return `<strong class="group-asset">${row?.base_asset ?? ''} (${count})</strong>`
    },
  },
  {
    headerName: '方向',
    field: 'status',
    width: 80,
    cellStyle: (params: any) => ({ color: params.value === 'closed' ? '#e6a23c' : '#67c23a' }),
    cellRenderer: (params: any) => (params.value === 'closed' ? '平仓' : '开仓'),
  },
  {
    headerName: '状态',
    field: 'status',
    width: 100,
    valueFormatter: (params) => positionStatusLabel(params.value),
  },
  {
    headerName: '交易所风险',
    field: 'exchange_risk_type',
    width: 135,
    valueFormatter: (params) => riskLabel(params.data as ReversePositionRow),
    cellStyle: (params) => {
      const row = params.data as ReversePositionRow | undefined
      if (row?.exchange_risk_status && row.exchange_risk_status !== 'normal') {
        return { color: '#f56c6c', fontWeight: '700' }
      }
      return { color: '#909399', fontWeight: '400' }
    },
    tooltipValueGetter: (params: any) => params.data?.exchange_risk_detail || null,
    tooltipComponent: LongTextTooltip,
  },
  {
    headerName: '开仓金额',
    field: 'open_amount_usdt',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: amountFormatter,
  },
  {
    headerName: '借币资产',
    field: 'borrow_asset',
    width: 95,
  },
  {
    headerName: '借币数量',
    field: 'borrow_qty',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: decimalFormatter,
  },
  {
    headerName: '已还数量',
    field: 'borrow_repaid_qty',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: decimalFormatter,
  },
  {
    headerName: '借币小时利率',
    field: 'borrow_hourly_rate',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: rateFormatter,
  },
  {
    headerName: '借币24h成本',
    field: 'open_borrow_24h_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '开仓24h Funding',
    field: 'open_funding_rate_24h',
    width: 145,
    type: 'numericColumn',
    cellClass: signedCellClass,
    headerClass: 'ag-right-aligned-header',
    valueFormatter: rateFormatter,
  },
  {
    headerName: '借币利息',
    field: 'borrow_interest_usdt',
    width: 115,
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
      const row = params.data as ReversePositionRow
      const fmt = (v: number | null | undefined) => (v != null ? formatDecimal(v, 6) : '-')
      return `${fmt(row?.spot_open_price)}/${fmt(row?.future_open_price)}`
    },
  },
  {
    headerName: '平仓VWAP(S/F)',
    colId: 'close_vwap',
    width: 160,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    cellRenderer: (params: any) => {
      const row = params.data as ReversePositionRow
      const fmt = (v: number | null | undefined) => (v != null ? formatDecimal(v, 6) : '-')
      return `${fmt(row?.spot_close_price)}/${fmt(row?.future_close_price)}`
    },
  },
  {
    headerName: '实际开仓基差',
    colId: 'display_open_basis_bps',
    valueGetter: (params: ValueGetterParams<ReversePositionRow>) => params.data?.actual_basis_bps ?? params.data?.reverse_open_basis_bps,
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '开仓VWAP阈值',
    field: 'reverse_open_basis_p20',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '平仓基差',
    field: 'reverse_close_basis_bps',
    width: 115,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '平仓VWAP阈值',
    field: 'reverse_close_basis_p20',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
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
  {
    headerName: '成交滑点',
    field: 'execution_drift_bps',
    width: 115,
    type: 'numericColumn',
    cellClass: signedCellClass,
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: 'Funding收益',
    field: 'funding_pnl_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: signedCellClass,
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '手续费',
    field: 'fee_total_bps',
    width: 105,
    type: 'numericColumn',
    cellClass: signedCellClass,
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '实现盈亏',
    field: 'realized_pnl_bps',
    width: 115,
    type: 'numericColumn',
    cellClass: signedCellClass,
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '平仓原因',
    field: 'close_reason',
    width: 180,
    tooltipComponent: LongTextTooltip,
    tooltipValueGetter: (params: any) => params.data?.close_reason || null,
  },
  {
    headerName: '操作',
    colId: 'action',
    width: 95,
    pinned: 'right',
    lockPosition: true,
    lockPinned: true,
    suppressMovable: true,
    sortable: false,
    filter: false,
    cellRenderer: (params: any) => {
      const row = params.data as ReversePositionRow
      if (!row) return ''
      return `<span class="action-btns"><button class="detail-btn" onclick="window.openReverseDetailDialog(${row.id})">详情</button></span>`
    },
  },
])

const defaultColDef: ColDef<ReversePositionRow> = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

const getRowId = (params: GetRowIdParams<ReversePositionRow>) => `reverse_pos_${params.data?.id ?? ''}`

async function openDetailDialog(positionId: number | null) {
  if (positionId == null) return
  detailPositionId.value = positionId
  detailOrders.value = []
  detailDialogVisible.value = true
  detailLoading.value = true
  try {
    const res = await get(`/api/trading/reverse-positions/${positionId}/orders`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '加载反向订单明细失败')
      return
    }
    detailOrders.value = Array.isArray(data.orders) ? data.orders : []
  } catch {
    showError('加载反向订单明细失败')
  } finally {
    detailLoading.value = false
  }
}

function setDaysFilter(days: number) {
  filterDays.value = days
  paginationCurrentPage.value = 1
  fetchOrders()
}

function setExchangeRiskOnly(enabled: boolean) {
  exchangeRiskOnly.value = enabled
  paginationCurrentPage.value = 1
  fetchOrders()
}

function setOrderSideFilter(side: string) {
  orderSideFilter.value = side
  paginationCurrentPage.value = 1
  fetchOrders()
}

async function fetchOrders() {
  if (loading.value) return
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (orderSideFilter.value) params.set('order_side', orderSideFilter.value)
    if (exchangeRiskOnly.value) params.set('exchange_risk', 'true')
    if (baseAssetFilter.value) params.set('base_asset', baseAssetFilter.value.trim())

    const res = await get(`/api/trading/reverse-positions?${params.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '获取反向订单数据失败')
      return
    }
    rowData.value = Array.isArray(data.positions) ? data.positions : []
    orderSummary.value = {
      total: Number(data.summary?.total || 0),
      open: Number(data.summary?.open || 0),
      close: Number(data.summary?.close || 0),
      exchange_risk: Number(data.summary?.exchange_risk || 0),
    }
    paginationTotal.value = Number(data.pagination?.total || 0)
  } catch {
    showError('请求反向订单数据失败')
  } finally {
    loading.value = false
  }
}

function onPageChange(page: number | null) {
  paginationCurrentPage.value = Number(page || 1)
  fetchOrders()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchOrders()
}

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field || col.colId)
    .filter((col) => col.colId !== 'action')
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
  loadColumnState()
  setupGridCopy(params.api)
}

function onRowDoubleClicked(params: any) {
  const positionId = params.data?.id
  if (positionId != null) openDetailDialog(positionId)
}

let autoRefreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  ;(window as any).openReverseDetailDialog = openDetailDialog
  fetchOrders()
  autoRefreshTimer = setInterval(fetchOrders, 2000)
})

onUnmounted(() => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
  delete (window as any).openReverseDetailDialog
})
</script>

<template>
  <div class="monitor-page">
    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">方向：</span>
        <el-button-group size="small">
          <el-button :type="orderSideFilter === '' ? 'primary' : 'default'" @click="setOrderSideFilter('')">全部({{ orderSummary.total }})</el-button>
          <el-button :type="orderSideFilter === 'open' ? 'primary' : 'default'" @click="setOrderSideFilter('open')">开仓({{ orderSummary.open }})</el-button>
          <el-button :type="orderSideFilter === 'close' ? 'primary' : 'default'" @click="setOrderSideFilter('close')">平仓({{ orderSummary.close }})</el-button>
        </el-button-group>

        <span class="filter-label" style="margin-left: 24px;">交易所风险：</span>
        <el-button-group size="small">
          <el-button :type="!exchangeRiskOnly ? 'primary' : 'default'" @click="setExchangeRiskOnly(false)">全部</el-button>
          <el-button :type="exchangeRiskOnly ? 'primary' : 'default'" @click="setExchangeRiskOnly(true)">有风险({{ orderSummary.exchange_risk }})</el-button>
        </el-button-group>
      </div>

      <div class="filter-row" style="margin-top: 10px;">
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
          <span>反向订单管理</span>
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

    <el-dialog
      v-model="detailDialogVisible"
      :title="`反向订单详情 - 持仓 #${detailPositionId}`"
      width="1100px"
      destroy-on-close
    >
      <div class="detail-section-title">订单明细</div>
      <el-table :data="detailOrders" v-loading="detailLoading" border stripe size="small" style="width: 100%">
        <el-table-column prop="order_side" label="方向" width="80">
          <template #default="{ row }">
            <span :style="{ color: row.order_side === 'close' || row.order_side === 'repay' ? '#e6a23c' : '#67c23a' }">
              {{ sideLabel(row.order_side) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="market_type" label="市场" width="100" :formatter="(row: ReverseOrderRow) => marketLabel(row.market_type)" />
        <el-table-column prop="trade_direction" label="交易动作" width="90" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <span :style="{ color: row.status === 'filled' ? '#67c23a' : row.status === 'failed' || row.status === 'cancelled' ? '#f56c6c' : '#e6a23c' }">
              {{ orderStatusLabel(row.status) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="target_amount" label="目标金额" width="100" align="right" :formatter="(row: ReverseOrderRow) => formatAmount(row.target_amount)" />
        <el-table-column prop="target_qty" label="目标数量" width="110" align="right" :formatter="(row: ReverseOrderRow) => formatDecimal(row.target_qty, 8)" />
        <el-table-column prop="exec_price" label="成交价" width="110" align="right" :formatter="(row: ReverseOrderRow) => formatDecimal(row.exec_price, 8)" />
        <el-table-column prop="exec_qty" label="成交数量" width="110" align="right" :formatter="(row: ReverseOrderRow) => formatDecimal(row.exec_qty, 8)" />
        <el-table-column prop="exec_amount" label="成交金额" width="100" align="right" :formatter="(row: ReverseOrderRow) => formatAmount(row.exec_amount)" />
        <el-table-column prop="fee_amount_usdt" label="手续费" width="90" align="right" :formatter="(row: ReverseOrderRow) => formatAmount(row.fee_amount_usdt)" />
        <el-table-column prop="execution_style" label="执行方式" width="100" />
        <el-table-column prop="exchange_order_id" label="交易所订单ID" width="150" show-overflow-tooltip />
        <el-table-column prop="reject_reason" label="原因" min-width="170" show-overflow-tooltip />
        <el-table-column prop="created_at" label="时间" width="160" :formatter="(row: ReverseOrderRow) => formatTime(row.created_at)" />
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

:deep(.group-asset) {
  color: var(--el-color-primary);
}

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

.detail-section-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
}

:deep(.value-positive) {
  color: #67c23a;
  text-align: right;
}

:deep(.value-negative) {
  color: #f56c6c;
  text-align: right;
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
