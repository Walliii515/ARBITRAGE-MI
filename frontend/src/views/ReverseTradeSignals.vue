<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent } from 'ag-grid-community'
import { Refresh } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'
import type { OrderBookRow } from './orderbookTypes'

interface ReversePayload {
  server_time?: string
  rows?: OrderBookRow[]
  reverse_margin_edge_threshold_bps?: number
  borrow_data_source?: string
}

interface ReverseSignalRow {
  id: string
  contract: string
  base_asset: string
  signal_time: string
  status: string
  funding_rate_24h: number | null
  reverse_gross_funding_bps: number | null
  reverse_expected_funding_bps: number | null
  reverse_borrow_hourly_rate: number | null
  reverse_borrow_24h_bps: number | null
  reverse_borrow_limit: number | null
  reverse_basis_bps: number | null
  reverse_open_basis_p20: number | null
  reverse_close_basis_bps: number | null
  reverse_close_basis_p20: number | null
  reverse_p20_edge_bps: number | null
  reverse_open_coverage: number | null
  reverse_capacity_usdt: number | null
  reverse_margin_edge_bps: number | null
  funding_next_apply: string | null
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

interface Summary {
  total: number
  candidate: number
  missingBorrow: number
  rejected: number
  latestSignalTime: string
}

const PAGE_KEY = 'reverse_arbitrage_signals'

const gridApi = shallowRef<GridApi<ReverseSignalRow> | null>(null)
const rowData = shallowRef<ReverseSignalRow[]>([])
const loading = ref(false)
const lastUpdate = ref('--')
const marginEdgeThresholdBps = ref(0)
const borrowDataSource = ref('none')
const filterStatus = ref('')
const filterAsset = ref('')
const columnVisibilities = ref<ColumnVisibility[]>([])
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

let refreshTimer: ReturnType<typeof setInterval> | null = null

const summary = computed<Summary>(() => {
  const rows = rowData.value
  const candidate = rows.filter((row) => row.status === 'candidate').length
  const missingBorrow = rows.filter((row) => row.status === 'missing_borrow_data').length
  return {
    total: rows.length,
    candidate,
    missingBorrow,
    rejected: rows.length - candidate - missingBorrow,
    latestSignalTime: lastUpdate.value,
  }
})

const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map((row) => row.base_asset).filter(Boolean))
  return Array.from(assets).sort()
})

function formatBps(value: number | null | undefined): string {
  return value == null || !Number.isFinite(Number(value)) ? '' : Number(value).toFixed(2)
}

function formatUsdt(value: number | null | undefined): string {
  return value == null || !Number.isFinite(Number(value))
    ? ''
    : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function percentFormatter(value: number | null | undefined): string {
  return value == null || !Number.isFinite(Number(value)) ? '' : `${(Number(value) * 100).toFixed(4)}%`
}

function formatTime(timeStr: string | null | undefined): string {
  if (!timeStr) return '--'
  if (timeStr.includes(' ')) return timeStr.split(' ')[1] || timeStr
  return timeStr
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

const statusClassMap: Record<string, string> = {
  candidate: 'reverse-signal-success',
  missing_borrow_data: 'reverse-signal-warning',
  missing_open_data: 'reverse-signal-danger',
  funding_too_low: 'reverse-signal-danger',
  borrow_unavailable: 'reverse-signal-danger',
  borrow_capacity_low: 'reverse-signal-danger',
  margin_edge_too_low: 'reverse-signal-danger',
  missing_margin_edge: 'reverse-signal-danger',
  depth_too_thin: 'reverse-signal-danger',
}

function mapRows(rows: unknown, serverTime: string): ReverseSignalRow[] {
  if (!Array.isArray(rows)) return []
  return rows
    .filter((raw): raw is OrderBookRow => !!raw && typeof raw === 'object')
    .filter((row) => typeof row.contract === 'string' && !!row.contract)
    .map((row) => ({
      id: row.contract,
      contract: row.contract,
      base_asset: row.base_asset,
      signal_time: serverTime,
      status: row.reverse_status || 'missing_margin_edge',
      funding_rate_24h: row.funding_rate_24h ?? null,
      reverse_gross_funding_bps: row.reverse_gross_funding_bps ?? null,
      reverse_expected_funding_bps: row.reverse_expected_funding_bps ?? null,
      reverse_borrow_hourly_rate: row.reverse_borrow_hourly_rate ?? null,
      reverse_borrow_24h_bps: row.reverse_borrow_24h_bps ?? null,
      reverse_borrow_limit: row.reverse_borrow_limit ?? null,
      reverse_basis_bps: row.reverse_basis_bps ?? null,
      reverse_open_basis_p20: row.reverse_open_basis_p20 ?? null,
      reverse_close_basis_bps: row.reverse_close_basis_bps ?? null,
      reverse_close_basis_p20: row.reverse_close_basis_p20 ?? null,
      reverse_p20_edge_bps: row.reverse_p20_edge_bps ?? null,
      reverse_open_coverage: row.reverse_open_coverage ?? null,
      reverse_capacity_usdt: row.reverse_capacity_usdt ?? null,
      reverse_margin_edge_bps: row.reverse_margin_edge_bps ?? null,
      funding_next_apply: row.funding_next_apply ?? null,
    }))
    .sort((a, b) => Number(b.reverse_margin_edge_bps ?? -Infinity) - Number(a.reverse_margin_edge_bps ?? -Infinity))
}

async function fetchSignals() {
  if (loading.value) return
  loading.value = true
  try {
    const res = await get('/api/reverse-arbitrage/opportunities')
    const data: ReversePayload = await res.json()
    if (!res.ok) {
      showError('反向信号加载失败')
      return
    }
    const serverTime = data.server_time || new Date().toLocaleString()
    lastUpdate.value = serverTime
    borrowDataSource.value = data.borrow_data_source || 'none'
    if (data.reverse_margin_edge_threshold_bps != null) marginEdgeThresholdBps.value = data.reverse_margin_edge_threshold_bps
    rowData.value = mapRows(data.rows ?? [], serverTime)
  } catch {
    showError('反向信号加载失败')
  } finally {
    loading.value = false
  }
}

function setStatusFilter(status: string) {
  filterStatus.value = status
  gridApi.value?.onFilterChanged()
}

function externalFilterPresent() {
  return !!filterStatus.value || !!filterAsset.value
}

function externalFilterPass(params: { data?: ReverseSignalRow }) {
  const row = params.data
  if (!row) return true
  if (filterStatus.value && row.status !== filterStatus.value) return false
  if (filterAsset.value && row.base_asset !== filterAsset.value) return false
  return true
}

function refreshColumnVisibilities() {
  if (!gridApi.value) return
  const states = gridApi.value.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field)
    .map((col) => {
      const colId = (col.field ?? col.colId) as string
      const state = states.find((item) => item.colId === colId)
      return {
        colId,
        headerName: col.headerName ?? colId,
        visible: state?.hide !== true,
      }
    })
}

function toggleColumnVisibility(colId: string, visible: boolean) {
  if (!gridApi.value) return
  gridApi.value.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((item) => item.colId === colId)
  if (col) col.visible = visible
}

async function saveColumnState() {
  if (!gridApi.value) return
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, {
      columnState: gridApi.value.getColumnState(),
    })
    const data = await res.json()
    if (data?.success) showSuccess('列配置已保存')
    else showError(data?.message || '保存列配置失败')
  } catch {
    showError('保存列配置失败')
  }
}

async function loadColumnState() {
  if (!gridApi.value) return
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEY}`)
    const data = await res.json()
    if (Array.isArray(data?.columnState)) {
      gridApi.value.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch {
    /* ignore */
  }
}

const columnDefs = ref<ColDef<ReverseSignalRow>[]>([
  { headerName: '标的资产', field: 'base_asset', width: 100, pinned: 'left' },
  { headerName: '信号时间', field: 'signal_time', width: 165 },
  {
    headerName: '状态',
    field: 'status',
    width: 105,
    cellRenderer: (params: { value?: string }) => {
      const span = document.createElement('span')
      span.textContent = statusLabel(params.value)
      span.className = `reverse-signal-status ${statusClassMap[params.value || ''] || 'reverse-signal-info'}`
      return span
    },
  },
  {
    headerName: '24h资金费率',
    field: 'funding_rate_24h',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => percentFormatter(p.value as number | null),
  },
  {
    headerName: '可收Funding(bps)',
    field: 'reverse_gross_funding_bps',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '预期Funding(bps)',
    field: 'reverse_expected_funding_bps',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '借币小时利率',
    field: 'reverse_borrow_hourly_rate',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => p.value == null ? '' : `${(Number(p.value) * 100).toFixed(6)}%`,
  },
  {
    headerName: '借币24h成本(bps)',
    field: 'reverse_borrow_24h_bps',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '借币额度',
    field: 'reverse_borrow_limit',
    width: 110,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => p.value == null ? '' : Number(p.value).toLocaleString('en-US', { maximumFractionDigits: 4 }),
  },
  {
    headerName: '反向开仓基差(bps)',
    field: 'reverse_basis_bps',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '反向开仓P20(bps)',
    field: 'reverse_open_basis_p20',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '开仓盘口覆盖',
    field: 'reverse_open_coverage',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => p.value == null ? '' : `${(Number(p.value) * 100).toFixed(1)}%`,
  },
  {
    headerName: '反向平仓基差(bps)',
    field: 'reverse_close_basis_bps',
    width: 150,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '反向平仓P20(bps)',
    field: 'reverse_close_basis_p20',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '边际P20(bps)',
    field: 'reverse_p20_edge_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '边际盈亏(bps)',
    field: 'reverse_margin_edge_bps',
    width: 140,
    sort: 'desc',
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
    cellStyle: (params) => Number(params.value ?? -Infinity) >= marginEdgeThresholdBps.value
      ? { color: '#67c23a' }
      : { color: '#f56c6c' },
  },
  {
    headerName: '可做名义USDT',
    field: 'reverse_capacity_usdt',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatUsdt(p.value as number | null),
  },
  { headerName: '下次支付时间', field: 'funding_next_apply', width: 160 },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
}

function onGridReady(event: GridReadyEvent<ReverseSignalRow>) {
  gridApi.value = event.api
  setupGridCopy(event.api)
  loadColumnState()
}

onMounted(() => {
  fetchSignals()
  refreshTimer = setInterval(fetchSignals, 3000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="reverse-signals-page">
    <div class="summary-bar">
      <span class="summary-item">
        <span class="summary-label">总信号</span>
        <span class="summary-value">{{ summary.total }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">候选</span>
        <span class="summary-value summary-success">{{ summary.candidate }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">待接借币</span>
        <span class="summary-value summary-warning">{{ summary.missingBorrow }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">未通过</span>
        <span class="summary-value summary-danger">{{ summary.rejected }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">最近更新</span>
        <span class="summary-value">{{ formatTime(summary.latestSignalTime) }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">借币数据</span>
        <span class="summary-value">{{ borrowDataSource }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">边际盈亏阈值</span>
        <span class="summary-value">{{ marginEdgeThresholdBps }} bps</span>
      </span>
    </div>

    <div class="filter-bar">
      <el-button-group size="small">
        <el-button :type="filterStatus === '' ? 'primary' : 'default'" @click="setStatusFilter('')">全部</el-button>
        <el-button :type="filterStatus === 'candidate' ? 'primary' : 'default'" @click="setStatusFilter('candidate')">候选</el-button>
        <el-button :type="filterStatus === 'funding_too_low' ? 'primary' : 'default'" @click="setStatusFilter('funding_too_low')">费率不足</el-button>
        <el-button :type="filterStatus === 'missing_borrow_data' ? 'primary' : 'default'" @click="setStatusFilter('missing_borrow_data')">待接借币</el-button>
        <el-button :type="filterStatus === 'borrow_unavailable' ? 'primary' : 'default'" @click="setStatusFilter('borrow_unavailable')">不可借</el-button>
        <el-button :type="filterStatus === 'borrow_capacity_low' ? 'primary' : 'default'" @click="setStatusFilter('borrow_capacity_low')">额度不足</el-button>
        <el-button :type="filterStatus === 'margin_edge_too_low' ? 'primary' : 'default'" @click="setStatusFilter('margin_edge_too_low')">边际亏损</el-button>
        <el-button :type="filterStatus === 'depth_too_thin' ? 'primary' : 'default'" @click="setStatusFilter('depth_too_thin')">深度不足</el-button>
      </el-button-group>
      <el-select
        v-model="filterAsset"
        placeholder="标的资产"
        size="small"
        filterable
        clearable
        style="width: 150px"
        @change="gridApi?.onFilterChanged()"
      >
        <el-option v-for="asset in assetOptions" :key="asset" :label="asset" :value="asset" />
      </el-select>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchSignals">刷新</el-button>
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

    <div ref="gridContainerRef" class="grid-container">
      <AgGridVue
        :theme="orderbookGridTheme"
        :rowData="rowData"
        :columnDefs="columnDefs"
        :defaultColDef="defaultColDef"
        :getRowId="(params: any) => String(params.data.id)"
        :isExternalFilterPresent="externalFilterPresent"
        :doesExternalFilterPass="externalFilterPass"
        :header-height="32"
        :row-height="32"
        @grid-ready="onGridReady"
        style="width: 100%; height: 100%"
      />
    </div>
  </div>
</template>

<style scoped>
.reverse-signals-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
}

.summary-bar,
.filter-bar,
.column-actions,
.summary-item {
  display: flex;
  align-items: center;
}

.summary-bar {
  gap: 22px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 10px 18px;
  flex-wrap: wrap;
}

.summary-item {
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

.summary-success {
  color: #67c23a;
}

.summary-warning {
  color: #e6a23c;
}

.summary-danger {
  color: #f56c6c;
}

.filter-bar {
  gap: 12px;
  flex-wrap: wrap;
}

.column-actions {
  gap: 8px;
  margin-left: auto;
}

.grid-container {
  flex: 1;
  min-height: 0;
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

:deep(.reverse-signal-status) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 72px;
  height: 22px;
  border: 1px solid transparent;
  border-radius: 4px;
  font-size: 12px;
}

:deep(.reverse-signal-success) {
  color: #67c23a;
  border-color: rgba(103, 194, 58, 0.4);
  background: rgba(103, 194, 58, 0.08);
}

:deep(.reverse-signal-warning) {
  color: #e6a23c;
  border-color: rgba(230, 162, 60, 0.4);
  background: rgba(230, 162, 60, 0.08);
}

:deep(.reverse-signal-danger) {
  color: #f56c6c;
  border-color: rgba(245, 108, 108, 0.4);
  background: rgba(245, 108, 108, 0.08);
}

:deep(.reverse-signal-info) {
  color: #909399;
  border-color: rgba(144, 147, 153, 0.35);
  background: rgba(144, 147, 153, 0.08);
}
</style>
