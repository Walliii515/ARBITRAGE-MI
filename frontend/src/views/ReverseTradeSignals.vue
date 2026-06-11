<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent, ValueFormatterParams } from 'ag-grid-community'
import { Refresh, Search } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface ReverseSignalRow {
  id: number
  base_asset: string
  contract: string | null
  symbol: string | null
  status: string
  signal_time: string
  resolved_time: string | null
  duration_sec: number | null
  trigger_type: string | null
  reject_reason: string | null
  order_uuid: string | null
  funding_rate_24h: number | null
  reverse_open_basis_bps: number | null
  signal_basis_bps: number | null
  valley_basis_bps: number | null
  rebound_basis_bps: number | null
  pre_gate_basis_bps: number | null
  actual_basis_bps: number | null
  reverse_open_basis_p20: number | null
  reverse_close_basis_p20: number | null
  margin_edge_bps: number | null
  borrow_hourly_rate: number | null
  borrow_24h_bps: number | null
  borrow_limit: number | null
  borrow_capacity_usdt: number | null
  open_coverage: number | null
  capacity_usdt: number | null
  open_amount_usdt: number | null
  strategy_tier?: string | null
}

interface ReverseSignalsResponse {
  signals?: ReverseSignalRow[]
  pagination?: {
    page: number
    page_size: number
    total: number
    total_pages: number
  }
  summary?: {
    total: number
    monitoring: number
    opened: number
    conditions_lost: number
    rejected: number
    monitor_timeout: number
    conversion_rate: number
    latest_signal_time: string | null
  }
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_arbitrage_signals'

const gridApi = shallowRef<GridApi<ReverseSignalRow> | null>(null)
const rowData = shallowRef<ReverseSignalRow[]>([])
const loading = ref(false)
const statusFilter = ref('')
const assetKeyword = ref('')
const days = ref(3)
const paginationPageSize = ref(100)
const paginationPageSizeOptions = [50, 100, 200, 500]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)
const columnVisibilities = ref<ColumnVisibility[]>([])
const summary = ref({
  total: 0,
  monitoring: 0,
  opened: 0,
  conditions_lost: 0,
  rejected: 0,
  monitor_timeout: 0,
  conversion_rate: 0,
  latest_signal_time: null as string | null,
})
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

let refreshTimer: ReturnType<typeof setInterval> | null = null

const totalPages = computed(() => Math.ceil(paginationTotal.value / paginationPageSize.value) || 1)

function formatBps(value: number | null | undefined): string {
  return value == null || !Number.isFinite(Number(value)) ? '' : Number(value).toFixed(2)
}

function formatUsdt(value: number | null | undefined): string {
  return value == null || !Number.isFinite(Number(value))
    ? ''
    : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function percentFormatter(params: ValueFormatterParams) {
  return params.value == null || !Number.isFinite(Number(params.value))
    ? ''
    : `${(Number(params.value) * 100).toFixed(4)}%`
}

function rateFormatter(params: ValueFormatterParams) {
  return params.value == null || !Number.isFinite(Number(params.value))
    ? ''
    : `${(Number(params.value) * 100).toFixed(6)}%`
}

function coverageFormatter(params: ValueFormatterParams) {
  return params.value == null || !Number.isFinite(Number(params.value))
    ? ''
    : `${(Number(params.value) * 100).toFixed(1)}%`
}

function durationFormatter(params: ValueFormatterParams) {
  const value = Number(params.value)
  if (!Number.isFinite(value)) return ''
  if (value < 60) return `${Math.round(value)}s`
  const min = Math.floor(value / 60)
  const sec = Math.round(value % 60)
  return `${min}m ${sec}s`
}

function statusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'monitoring': return '监控中'
    case 'opened': return '已开仓'
    case 'conditions_lost': return '条件丢失'
    case 'monitor_timeout': return '监控超时'
    case 'gate_rejected': return '风控拒绝'
    case 'rejected': return '执行失败'
    default: return '未知'
  }
}

function triggerLabel(trigger: string | null | undefined): string {
  switch (trigger) {
    case 'funding_carry': return 'FundingCarry'
    case 'valley_rebound': return '触底反弹'
    case 'manual': return '手动'
    default: return ''
  }
}

const statusClassMap: Record<string, string> = {
  monitoring: 'reverse-signal-info',
  opened: 'reverse-signal-success',
  conditions_lost: 'reverse-signal-warning',
  monitor_timeout: 'reverse-signal-warning',
  gate_rejected: 'reverse-signal-danger',
  rejected: 'reverse-signal-danger',
}

function statusRenderer(params: { value?: string }) {
  const span = document.createElement('span')
  span.textContent = statusLabel(params.value)
  span.className = `reverse-signal-status ${statusClassMap[params.value || ''] || 'reverse-signal-info'}`
  return span
}

function triggerRenderer(params: { value?: string | null }) {
  return triggerLabel(params.value)
}

async function fetchSignals(resetPage = false) {
  if (loading.value) return
  if (resetPage) paginationCurrentPage.value = 1
  loading.value = true
  try {
    const query = new URLSearchParams({
      days: String(days.value),
      page: String(paginationCurrentPage.value),
      page_size: String(paginationPageSize.value),
    })
    if (statusFilter.value) query.set('status', statusFilter.value)
    if (assetKeyword.value.trim()) query.set('base_asset', assetKeyword.value.trim())

    const res = await get(`/api/trading/reverse-signals?${query.toString()}`)
    const data: ReverseSignalsResponse = await res.json()
    if (!res.ok) {
      showError('反向信号加载失败')
      return
    }
    rowData.value = Array.isArray(data.signals) ? data.signals : []
    paginationTotal.value = Number(data.pagination?.total ?? rowData.value.length)
    summary.value = {
      total: Number(data.summary?.total ?? 0),
      monitoring: Number(data.summary?.monitoring ?? 0),
      opened: Number(data.summary?.opened ?? 0),
      conditions_lost: Number(data.summary?.conditions_lost ?? 0),
      rejected: Number(data.summary?.rejected ?? 0),
      monitor_timeout: Number(data.summary?.monitor_timeout ?? 0),
      conversion_rate: Number(data.summary?.conversion_rate ?? 0),
      latest_signal_time: data.summary?.latest_signal_time ?? null,
    }
  } catch {
    showError('反向信号加载失败')
  } finally {
    loading.value = false
  }
}

function setStatus(status: string) {
  statusFilter.value = status
  fetchSignals(true)
}

function onPageChange(page: number | null) {
  paginationCurrentPage.value = Number(page || 1)
  fetchSignals()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchSignals()
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
  { headerName: '标的资产', field: 'base_asset', width: 95, pinned: 'left' },
  {
    headerName: '状态',
    field: 'status',
    width: 110,
    cellRenderer: statusRenderer,
  },
  {
    headerName: '信号时间',
    field: 'signal_time',
    width: 165,
    sort: 'desc',
  },
  {
    headerName: '持续时长',
    field: 'duration_sec',
    width: 105,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: durationFormatter,
  },
  {
    headerName: '触发方式',
    field: 'trigger_type',
    width: 110,
    cellRenderer: triggerRenderer,
  },
  {
    headerName: '24h资金费率',
    field: 'funding_rate_24h',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: percentFormatter,
  },
  {
    headerName: '边际盈亏(bps)',
    field: 'margin_edge_bps',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '入场基差(bps)',
    field: 'signal_basis_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '低点基差(bps)',
    field: 'valley_basis_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '反弹基差(bps)',
    field: 'rebound_basis_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '旁路基差(bps)',
    field: 'pre_gate_basis_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '开仓P20(bps)',
    field: 'reverse_open_basis_p20',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '平仓P20(bps)',
    field: 'reverse_close_basis_p20',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '借币小时利率',
    field: 'borrow_hourly_rate',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: rateFormatter,
  },
  {
    headerName: '借币24h成本(bps)',
    field: 'borrow_24h_bps',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value as number | null),
  },
  {
    headerName: '借币额度USDT',
    field: 'borrow_capacity_usdt',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatUsdt(p.value as number | null),
  },
  {
    headerName: '盘口覆盖',
    field: 'open_coverage',
    width: 105,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: coverageFormatter,
  },
  {
    headerName: '拒绝/结束原因',
    field: 'reject_reason',
    width: 260,
    tooltipField: 'reject_reason',
  },
  { headerName: '订单UUID', field: 'order_uuid', width: 170 },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function onGridReady(event: GridReadyEvent<ReverseSignalRow>) {
  gridApi.value = event.api
  setupGridCopy(event.api)
  loadColumnState()
}

onMounted(() => {
  fetchSignals()
  refreshTimer = setInterval(() => fetchSignals(), 5000)
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
        <span class="summary-label">监控中</span>
        <span class="summary-value summary-info">{{ summary.monitoring }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">已开仓</span>
        <span class="summary-value summary-success">{{ summary.opened }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">条件丢失</span>
        <span class="summary-value summary-warning">{{ summary.conditions_lost }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">风控/执行拒绝</span>
        <span class="summary-value summary-danger">{{ summary.rejected }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">监控超时</span>
        <span class="summary-value summary-warning">{{ summary.monitor_timeout }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">最近信号</span>
        <span class="summary-value">{{ summary.latest_signal_time || '--' }}</span>
      </span>
    </div>

    <div class="filter-bar">
      <el-button-group size="small">
        <el-button :type="statusFilter === '' ? 'primary' : 'default'" @click="setStatus('')">全部</el-button>
        <el-button :type="statusFilter === 'monitoring' ? 'primary' : 'default'" @click="setStatus('monitoring')">监控中</el-button>
        <el-button :type="statusFilter === 'opened' ? 'primary' : 'default'" @click="setStatus('opened')">已开仓</el-button>
        <el-button :type="statusFilter === 'conditions_lost' ? 'primary' : 'default'" @click="setStatus('conditions_lost')">条件丢失</el-button>
        <el-button :type="statusFilter === 'gate_rejected' ? 'primary' : 'default'" @click="setStatus('gate_rejected')">风控拒绝</el-button>
        <el-button :type="statusFilter === 'monitor_timeout' ? 'primary' : 'default'" @click="setStatus('monitor_timeout')">监控超时</el-button>
      </el-button-group>
      <el-input
        v-model="assetKeyword"
        :prefix-icon="Search"
        placeholder="标的资产"
        size="small"
        clearable
        style="width: 150px"
        @change="fetchSignals(true)"
        @clear="fetchSignals(true)"
      />
      <el-select v-model="days" size="small" style="width: 110px" @change="fetchSignals(true)">
        <el-option :value="1" label="最近1天" />
        <el-option :value="3" label="最近3天" />
        <el-option :value="7" label="最近7天" />
        <el-option :value="14" label="最近14天" />
        <el-option :value="30" label="最近30天" />
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
        :header-height="32"
        :row-height="32"
        :tooltip-show-delay="300"
        @grid-ready="onGridReady"
        style="width: 100%; height: 100%"
      />
    </div>

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

.summary-info {
  color: #409eff;
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
  color: #409eff;
  border-color: rgba(64, 158, 255, 0.35);
  background: rgba(64, 158, 255, 0.08);
}
</style>
