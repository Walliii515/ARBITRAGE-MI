<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
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
import { Refresh, Search } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'
import type { OrderBookRow } from './orderbookTypes'

interface ReversePayload {
  server_time?: string
  rows?: OrderBookRow[]
  orderbook_coverage_threshold?: number
  reverse_margin_edge_threshold_bps?: number
  reverse_funding_carry?: {
    enabled?: boolean
    min_24h_bps?: number
    max_next_funding_min?: number
    min_margin_edge_bps?: number
    basis_relax_bps?: number
  }
  borrow_data_available?: boolean
  borrow_data_source?: string
  borrow_cache_age_sec?: number | null
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_arbitrage_opportunities'

const rowData = shallowRef<OrderBookRow[]>([])
const rowsByContract = new Map<string, OrderBookRow>()
const rowVersion = ref(0)
const displayedRowCount = ref(0)
const lastUpdate = ref('--')
const loading = ref(false)
const borrowDataAvailable = ref(false)
const borrowDataSource = ref('none')
const borrowCacheAgeSec = ref<number | null>(null)
const marginEdgeThresholdBps = ref(0)
const orderbookCoverageThreshold = ref(0.6)
const fundingCarryConfig = ref({
  enabled: false,
  min24hBps: 80,
  maxNextFundingMin: 60,
  minMarginEdgeBps: 50,
  basisRelaxBps: 30,
})
const assetFilterKeyword = ref('')
const assetFilterVisible = ref(false)
const filterNegativeFunding = ref(true)
const filterMarginEdge = ref(true)
const filterCoverage = ref(true)
const filterBorrowReady = ref(true)
const columnVisibilities = ref<ColumnVisibility[]>([])
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

let gridApi: GridApi<OrderBookRow> | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

const uniqueAssets = computed(() => {
  void rowVersion.value
  const assets = new Set<string>()
  rowsByContract.forEach((row) => {
    if (row.base_asset) assets.add(row.base_asset)
  })
  return Array.from(assets).sort()
})

const filteredAssetOptions = computed(() => {
  if (!assetFilterKeyword.value) return uniqueAssets.value
  const keyword = assetFilterKeyword.value.toLowerCase()
  return uniqueAssets.value.filter((asset) => asset.toLowerCase().includes(keyword))
})

const totalRowCount = computed(() => {
  void rowVersion.value
  return rowsByContract.size
})

function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function formatBps(value: number | null | undefined): string {
  return value == null ? '—' : Number(value).toFixed(2)
}

function formatUsdt(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function percentFormatter(params: ValueFormatterParams) {
  return params.value == null ? '' : (Number(params.value) * 100).toFixed(4) + '%'
}

function bpsFormatter(params: ValueFormatterParams) {
  return formatBps(params.value as number | null)
}

function statusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'candidate': return '候选'
    case 'missing_open_data': return '缺盘口'
    case 'funding_too_low': return '费率不足'
    case 'missing_borrow_data': return '待接借币'
    case 'borrow_unavailable': return '不可借'
    case 'borrow_capacity_low': return '额度不足'
    case 'margin_edge_too_low': return '边际亏损'
    case 'depth_too_thin': return '深度不足'
    case 'missing_margin_edge': return '缺少边际'
    default: return '未知'
  }
}

function statusType(status: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'candidate': return 'success'
    case 'missing_borrow_data': return 'warning'
    case 'funding_too_low':
    case 'margin_edge_too_low':
    case 'borrow_capacity_low':
    case 'depth_too_thin':
      return 'danger'
    default:
      return 'info'
  }
}

function refreshDisplayedRowCount() {
  displayedRowCount.value = gridApi?.getDisplayedRowCount() ?? rowData.value.length
}

function normalizeRows(rows: unknown): OrderBookRow[] {
  if (!Array.isArray(rows)) return []
  const byContract = new Map<string, OrderBookRow>()
  for (const raw of rows) {
    if (!raw || typeof raw !== 'object') continue
    const row = raw as OrderBookRow
    if (typeof row.contract !== 'string' || !row.contract.trim()) continue
    byContract.set(row.contract, { ...row })
  }
  return Array.from(byContract.values())
}

function applyRows(rows: unknown, serverTime?: string) {
  const normalized = normalizeRows(rows)
  rowsByContract.clear()
  for (const row of normalized) rowsByContract.set(row.contract, row)
  rowVersion.value++
  rowData.value = normalized
  if (gridApi) {
    gridApi.setGridOption('rowData', normalized)
    requestAnimationFrame(refreshDisplayedRowCount)
  } else {
    refreshDisplayedRowCount()
  }
  if (serverTime) lastUpdate.value = serverTime
}

async function fetchOpportunities() {
  if (loading.value) return
  loading.value = true
  try {
    const res = await get('/api/reverse-arbitrage/opportunities')
    const data: ReversePayload = await res.json()
    if (!res.ok) {
      showError('反向机会加载失败')
      return
    }
    borrowDataAvailable.value = !!data.borrow_data_available
    borrowDataSource.value = data.borrow_data_source ?? 'none'
    borrowCacheAgeSec.value = data.borrow_cache_age_sec ?? null
    if (data.reverse_margin_edge_threshold_bps != null) marginEdgeThresholdBps.value = data.reverse_margin_edge_threshold_bps
    if (data.orderbook_coverage_threshold != null) orderbookCoverageThreshold.value = data.orderbook_coverage_threshold
    if (data.reverse_funding_carry) {
      fundingCarryConfig.value = {
        enabled: !!data.reverse_funding_carry.enabled,
        min24hBps: Number(data.reverse_funding_carry.min_24h_bps ?? 80),
        maxNextFundingMin: Number(data.reverse_funding_carry.max_next_funding_min ?? 60),
        minMarginEdgeBps: Number(data.reverse_funding_carry.min_margin_edge_bps ?? 50),
        basisRelaxBps: Number(data.reverse_funding_carry.basis_relax_bps ?? 30),
      }
    }
    applyRows(data.rows ?? [], data.server_time)
  } catch {
    showError('反向机会加载失败')
  } finally {
    loading.value = false
  }
}

function negativeFundingFilter(params: { data?: OrderBookRow }) {
  if (!filterNegativeFunding.value) return true
  return params.data?.reverse_funding_pass === true
}

function marginEdgeFilter(params: { data?: OrderBookRow }) {
  if (!filterMarginEdge.value) return true
  return params.data?.reverse_margin_edge_pass === true
}

function coverageFilter(params: { data?: OrderBookRow }) {
  if (!filterCoverage.value) return true
  return params.data?.reverse_coverage_pass === true
}

function borrowReadyFilter(params: { data?: OrderBookRow }) {
  if (!filterBorrowReady.value) return true
  return params.data?.reverse_borrow_pass === true
}

function assetFilter(params: { data?: OrderBookRow }) {
  if (!assetFilterKeyword.value) return true
  const asset = params.data?.base_asset
  if (!asset) return true
  return asset.toLowerCase().includes(assetFilterKeyword.value.toLowerCase())
}

function combinedFilter(params: { data?: OrderBookRow }) {
  return (
    negativeFundingFilter(params) &&
    marginEdgeFilter(params) &&
    coverageFilter(params) &&
    borrowReadyFilter(params) &&
    assetFilter(params)
  )
}

function selectAsset(asset: string) {
  assetFilterKeyword.value = asset
  assetFilterVisible.value = false
}

function clearAssetFilter() {
  assetFilterKeyword.value = ''
}

function handleOutsideClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('.asset-filter-container')) {
    assetFilterVisible.value = false
  }
}

const drawerVisible = ref(false)
const drawerContract = ref('')
const drawerRow = computed(() => {
  void rowVersion.value
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

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field && col.field !== 'actions')
    .map((col) => {
      const colId = (col.field ?? col.colId) as string
      const state = states.find((s) => s.colId === colId)
      return {
        colId,
        headerName: col.headerName ?? colId,
        visible: state?.hide !== true,
      }
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
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, {
      columnState: gridApi.getColumnState(),
    })
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
    if (Array.isArray(data?.columnState)) {
      gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch {
    /* ignore */
  }
}

const columnDefs = computed<ColDef<OrderBookRow>[]>(() => [
  { headerName: '标的资产', field: 'base_asset', pinned: 'left', width: 90 },
  {
    headerName: '24h资金费率',
    field: 'funding_rate_24h',
    width: 120,
    sort: 'asc',
    sortIndex: 0,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: percentFormatter,
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      return value < 0 ? { color: '#f56c6c' } : { color: '#67c23a' }
    },
  },
  {
    headerName: '可收Funding(bps)',
    field: 'reverse_gross_funding_bps',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => Number(params.value ?? 0) > 0 ? { color: '#67c23a' } : { color: '#909399' },
  },
  {
    headerName: '预期Funding(bps)',
    field: 'reverse_expected_funding_bps',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => Number(params.value ?? 0) > 0 ? { color: '#67c23a' } : { color: '#909399' },
  },
  {
    headerName: '反向开仓基差(bps)',
    field: 'reverse_basis_bps',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      if (value <= 0) return { color: '#67c23a' }
      return { color: '#e6a23c' }
    },
  },
  {
    headerName: '反向开仓P20(bps)',
    field: 'reverse_open_basis_p20',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => {
      const threshold = params.value as number | null
      const basis = params.data?.reverse_basis_bps as number | null | undefined
      if (threshold == null || basis == null) return { color: '#909399' }
      return basis <= threshold ? { color: '#67c23a' } : { color: '#909399' }
    },
  },
  {
    headerName: '反向平仓基差(bps)',
    field: 'reverse_close_basis_bps',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => {
      const value = params.value as number | null
      const threshold = params.data?.reverse_close_basis_p20 as number | null | undefined
      if (value == null) return { color: '#909399' }
      if (threshold != null && value >= threshold) return { color: '#67c23a' }
      return value >= 0 ? { color: '#e6a23c' } : { color: '#f56c6c' }
    },
  },
  {
    headerName: '反向平仓P20(bps)',
    field: 'reverse_close_basis_p20',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => {
      const threshold = params.value as number | null
      const basis = params.data?.reverse_close_basis_bps as number | null | undefined
      if (threshold == null || basis == null) return { color: '#909399' }
      return basis >= threshold ? { color: '#67c23a' } : { color: '#909399' }
    },
  },
  {
    headerName: '边际P20(bps)',
    field: 'reverse_p20_edge_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '边际盈亏(bps)',
    field: 'reverse_margin_edge_bps',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      return value >= marginEdgeThresholdBps.value ? { color: '#67c23a' } : { color: '#f56c6c' }
    },
  },
  {
    headerName: 'Carry入口',
    field: 'reverse_funding_carry_pass',
    width: 100,
    cellRenderer: (params: { value?: boolean | null }) => {
      const span = document.createElement('span')
      span.textContent = params.value === true ? '满足' : '—'
      span.className = params.value === true
        ? 'reverse-status reverse-status-success'
        : 'reverse-status reverse-status-info'
      return span
    },
  },
  {
    headerName: '距资金费(min)',
    field: 'reverse_funding_carry_next_min',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => p.value == null ? '—' : Number(p.value).toFixed(1),
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      return value >= 0 && value <= fundingCarryConfig.value.maxNextFundingMin
        ? { color: '#67c23a' }
        : { color: '#909399' }
    },
  },
  {
    headerName: 'Carry上限(bps)',
    field: 'reverse_funding_carry_basis_ceiling_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
    cellStyle: (params) => {
      const ceiling = params.value as number | null
      const basis = params.data?.reverse_basis_bps as number | null | undefined
      if (ceiling == null || basis == null) return { color: '#909399' }
      return basis <= ceiling ? { color: '#67c23a' } : { color: '#909399' }
    },
  },
  {
    headerName: '开仓盘口覆盖',
    field: 'reverse_open_coverage',
    width: 115,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => p.value == null ? '—' : (Number(p.value) * 100).toFixed(1) + '%',
    cellStyle: (params) => {
      const value = params.value as number | null
      if (value == null) return { color: '#909399' }
      return value <= orderbookCoverageThreshold.value ? { color: '#67c23a' } : { color: '#f56c6c' }
    },
  },
  {
    headerName: '现货卖出VWAP',
    field: 'reverse_spot_open_vwap',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => p.value == null ? '—' : formatDecimal(Number(p.value)),
  },
  {
    headerName: '合约买入VWAP',
    field: 'reverse_future_open_vwap',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => p.value == null ? '—' : formatDecimal(Number(p.value)),
  },
  {
    headerName: '借币小时利率',
    field: 'reverse_borrow_hourly_rate',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => p.value == null ? '—' : (Number(p.value) * 100).toFixed(6) + '%',
  },
  {
    headerName: '借币24h成本(bps)',
    field: 'reverse_borrow_24h_bps',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: bpsFormatter,
  },
  {
    headerName: '借币额度',
    field: 'reverse_borrow_limit',
    width: 110,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => p.value == null ? '—' : formatDecimal(Number(p.value), 4),
  },
  {
    headerName: '可做名义USDT',
    field: 'reverse_capacity_usdt',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    headerClass: 'ag-right-aligned-header',
    valueFormatter: (p) => formatUsdt(p.value as number | null),
  },
  {
    headerName: '状态',
    field: 'reverse_status',
    width: 115,
    cellRenderer: (params: { value?: string }) => {
      const span = document.createElement('span')
      span.textContent = statusLabel(params.value)
      span.className = `reverse-status reverse-status-${statusType(params.value)}`
      return span
    },
  },
  {
    headerName: '下次支付时间',
    field: 'funding_next_apply',
    width: 160,
    valueFormatter: (p) => p.value || '—',
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
])

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
  searchPlaceholder: '搜索...',
  selectAll: '全选',
  unselectAll: '全不选',
  pinLeft: '固定左侧',
  pinRight: '固定右侧',
  noPin: '不固定',
}

const getRowId = (params: GetRowIdParams<OrderBookRow>) => String(params.data?.contract ?? '')

function applyDefaultSort() {
  gridApi?.applyColumnState({
    defaultState: { sort: null },
    state: [{ colId: 'funding_rate_24h', sort: 'asc', sortIndex: 0 }],
  })
}

function onGridReady(params: GridReadyEvent<OrderBookRow>) {
  gridApi = params.api
  setupGridCopy(params.api)
  loadColumnState().finally(applyDefaultSort)
  fetchOpportunities()
  refreshDisplayedRowCount()
}

function restartTimer() {
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = setInterval(fetchOpportunities, 3000)
}

watch(
  [filterNegativeFunding, filterMarginEdge, filterCoverage, filterBorrowReady, assetFilterKeyword],
  () => {
    gridApi?.onFilterChanged()
    refreshDisplayedRowCount()
  },
)

onMounted(() => {
  document.addEventListener('click', handleOutsideClick)
  fetchOpportunities()
  restartTimer()
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  document.removeEventListener('click', handleOutsideClick)
})
</script>

<template>
  <div class="reverse-page">
    <el-card shadow="never" class="status-card">
      <div class="status-row">
        <span class="status-item">最后更新：{{ lastUpdate }}</span>
        <span class="status-item">借币数据：
          <el-tag :type="borrowDataAvailable ? 'success' : 'warning'" size="small">
            {{ borrowDataAvailable ? borrowDataSource : '待接入' }}
          </el-tag>
        </span>
        <span v-if="borrowCacheAgeSec != null" class="status-item">借币缓存：{{ borrowCacheAgeSec }}s</span>
        <span class="status-item">边际盈亏阈值：{{ marginEdgeThresholdBps }} bps</span>
        <span class="status-item">FundingCarry：
          {{ fundingCarryConfig.enabled ? '开' : '关' }} /
          {{ fundingCarryConfig.min24hBps }}bps /
          {{ fundingCarryConfig.maxNextFundingMin }}min /
          边际{{ fundingCarryConfig.minMarginEdgeBps }}bps /
          放宽{{ fundingCarryConfig.basisRelaxBps }}bps
        </span>
        <span class="status-item">盘口覆盖阈值：{{ (orderbookCoverageThreshold * 100).toFixed(1) }}%</span>
        <el-button size="small" :loading="loading" :icon="Refresh" circle @click="fetchOpportunities" />
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <div class="asset-filter-container">
            <el-input
              v-model="assetFilterKeyword"
              placeholder="搜索标的资产"
              clearable
              size="small"
              style="width: 220px"
              @focus="assetFilterVisible = true"
              @clear="clearAssetFilter"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
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
            <div class="row-count">
              <span>显示 {{ displayedRowCount }}</span>
              <span class="row-count-separator">/</span>
              <span>总计 {{ totalRowCount }}</span>
            </div>
            <div class="filter-group">
              <el-switch v-model="filterNegativeFunding" inline-prompt active-text="负费率" inactive-text="负费率" />
              <el-switch v-model="filterMarginEdge" inline-prompt active-text="边际盈亏" inactive-text="边际盈亏" />
              <el-switch v-model="filterCoverage" inline-prompt active-text="盘口" inactive-text="盘口" />
              <el-switch v-model="filterBorrowReady" inline-prompt active-text="借币" inactive-text="借币" />
            </div>
            <div class="column-actions">
              <el-popover placement="bottom-end" :width="260" trigger="click" @before-enter="refreshColumnVisibilities">
                <template #reference>
                  <el-button size="small">列选择</el-button>
                </template>
                <div class="column-picker">
                  <div v-for="col in columnVisibilities" :key="col.colId" class="column-picker-item">
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
        </div>
      </template>
      <div ref="gridContainerRef">
        <ag-grid-vue
          class="reverse-grid"
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
          :isExternalFilterPresent="() => filterNegativeFunding || filterMarginEdge || filterCoverage || filterBorrowReady || !!assetFilterKeyword"
          :doesExternalFilterPass="combinedFilter"
          @grid-ready="onGridReady"
          @model-updated="refreshDisplayedRowCount"
        />
      </div>
    </el-card>

    <ElDrawer
      v-model="drawerVisible"
      :title="drawerRow ? `${drawerRow.contract} 反向开仓盘口` : '反向开仓盘口'"
      direction="rtl"
      :size="480"
      class="ob-drawer"
    >
      <div v-if="drawerRow" class="ob-drawer-body">
        <div class="ob-section">
          <div class="ob-section-title">现货卖出（Binance Spot Bid）</div>
          <table class="ob-table">
            <thead>
              <tr><th>档</th><th>价格</th><th>数量</th><th>USDT</th></tr>
            </thead>
            <tbody>
              <tr v-for="level in getOrderbookLevels(drawerRow, 'spot', 'bid')" :key="`spot-${level.level}`">
                <td>{{ level.level }}</td>
                <td>{{ formatDecimal(level.price) || '—' }}</td>
                <td>{{ formatDecimal(level.volume, 4) || '—' }}</td>
                <td>{{ formatUsdt(level.usdt) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="ob-section">
          <div class="ob-section-title">合约买入（Gate Future Ask）</div>
          <table class="ob-table">
            <thead>
              <tr><th>档</th><th>价格</th><th>数量</th><th>USDT</th></tr>
            </thead>
            <tbody>
              <tr v-for="level in getOrderbookLevels(drawerRow, 'future', 'ask')" :key="`future-${level.level}`">
                <td>{{ level.level }}</td>
                <td>{{ formatDecimal(level.price) || '—' }}</td>
                <td>{{ formatDecimal(level.volume, 4) || '—' }}</td>
                <td>{{ formatUsdt(level.usdt) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </ElDrawer>
  </div>
</template>

<style scoped>
.reverse-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
}

.status-card,
.grid-card {
  border-radius: 4px;
  border-color: var(--app-border);
}

.grid-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.grid-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  padding: 0;
}

.status-row,
.grid-header,
.header-actions,
.filter-group,
.column-actions,
.row-count {
  display: flex;
  align-items: center;
}

.status-row {
  flex-wrap: wrap;
  gap: 14px;
}

.status-item {
  color: var(--app-text-muted);
  font-size: 13px;
}

.grid-header {
  justify-content: space-between;
  gap: 12px;
}

.header-actions {
  gap: 12px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.filter-group,
.column-actions {
  gap: 8px;
}

.row-count {
  gap: 4px;
  color: var(--app-text-muted);
  font-size: 13px;
}

.row-count-separator {
  color: #606266;
}

.reverse-grid {
  width: 100%;
  height: calc(100vh - 220px);
}

.asset-filter-container {
  position: relative;
}

.asset-dropdown {
  position: absolute;
  top: 34px;
  left: 0;
  z-index: 30;
  width: 220px;
  max-height: 280px;
  overflow-y: auto;
  background: var(--app-surface-elevated);
  border: 1px solid var(--app-border);
  border-radius: 4px;
}

.asset-option {
  padding: 7px 10px;
  cursor: pointer;
  font-size: 13px;
}

.asset-option:hover {
  background: #252a2d;
}

.column-picker {
  max-height: 320px;
  overflow-y: auto;
}

.column-picker-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.column-picker-label {
  font-size: 13px;
}

:deep(.reverse-status) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 72px;
  height: 22px;
  border-radius: 4px;
  font-size: 12px;
  border: 1px solid transparent;
}

:deep(.reverse-status-success) {
  color: #67c23a;
  border-color: rgba(103, 194, 58, 0.4);
  background: rgba(103, 194, 58, 0.08);
}

:deep(.reverse-status-warning) {
  color: #e6a23c;
  border-color: rgba(230, 162, 60, 0.4);
  background: rgba(230, 162, 60, 0.08);
}

:deep(.reverse-status-danger) {
  color: #f56c6c;
  border-color: rgba(245, 108, 108, 0.4);
  background: rgba(245, 108, 108, 0.08);
}

:deep(.reverse-status-info) {
  color: #909399;
  border-color: rgba(144, 147, 153, 0.35);
  background: rgba(144, 147, 153, 0.08);
}

:deep(.ob-drawer-btn) {
  height: 24px;
  padding: 0 10px;
  border: 1px solid #2f6fbd;
  border-radius: 4px;
  color: #79bbff;
  background: transparent;
  cursor: pointer;
}

:deep(.ob-drawer-btn:hover) {
  background: rgba(33, 150, 243, 0.12);
}

.ob-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ob-section-title {
  margin-bottom: 8px;
  font-weight: 600;
}

.ob-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.ob-table th,
.ob-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--app-border);
  text-align: right;
}

.ob-table th:first-child,
.ob-table td:first-child {
  text-align: left;
}
</style>
