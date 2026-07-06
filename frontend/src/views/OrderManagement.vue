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
  order_count: number | null
}

interface OrderSummary {
  total: number
  open: number
  close: number
  exchange_risk: number
}

/* ───── 状态 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef
const rowData = shallowRef<PositionRow[]>([])
let gridApi: GridApi<PositionRow> | null = null
const loading = ref(false)
const orderSideFilter = ref<string>('')
const exchangeRiskOnly = ref<boolean>(false)
const baseAssetFilter = ref<string>('')
const filterDays = ref<number>(90) // 默认90天，与持仓监控一致
const orderSummary = ref<OrderSummary>({ total: 0, open: 0, close: 0, exchange_risk: 0 })

// 一键全部平仓
const closeAllLoading = ref(false)

// 分页配置
const paginationPageSize = ref<number>(50)
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
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
const detailLoading = ref(false)

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





const amountFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toFixed(2) + ' bps'
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
  return `${Number.isInteger(n) ? n.toFixed(0) : formatDecimal(n, 2)}x`
}

function formatExchangeRisk(row: PositionRow | null | undefined): string {
  if (!row) return ''
  const hasDelistRisk = !!(row.delist_risks && row.delist_risks.length > 0)
  if ((!row.exchange_risk_status || row.exchange_risk_status === 'normal') && !hasDelistRisk) return ''
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
const columnDefs = computed((): ColDef[] => [
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
    headerName: '方向',
    field: 'status',
    width: 80,
    cellStyle: (params: any) => {
      return { color: params.value === 'closed' ? '#e6a23c' : '#67c23a' }
    },
    cellRenderer: (params: any) => {
      return params.value === 'closed' ? '平仓' : '开仓'
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
function setDaysFilter(days: number) {
  filterDays.value = days
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchOrders()
}

/** 交易所风险过滤 */
function setExchangeRiskOnly(enabled: boolean) {
  exchangeRiskOnly.value = enabled
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
    if (orderSideFilter.value) {
      params.set('order_side', orderSideFilter.value)
    }
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
    rowData.value = data.orders || []
    orderSummary.value = {
      total: Number(data.summary?.total || 0),
      open: Number(data.summary?.open || 0),
      close: Number(data.summary?.close || 0),
      exchange_risk: Number(data.summary?.exchange_risk || 0),
    }
    
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
function onGridReady(params: GridReadyEvent) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)
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

        <el-button
          size="small"
          type="danger"
          :loading="closeAllLoading"
          @click="handleCloseAll"
        >
          ✖ 一键平仓
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
