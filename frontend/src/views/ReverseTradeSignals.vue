<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent, ValueFormatterParams } from 'ag-grid-community'
import { Refresh } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface ReverseSignalRow {
  id: number
  base_asset: string
  signal_time: string
  resolved_time: string | null
  status: 'candidate' | 'rejected' | 'conditions_lost' | string
  reverse_status: string
  reject_reason: string | null
  funding_rate_2h: number | null
  reverse_expected_funding_bps: number | null
  reverse_basis_bps: number | null
  reverse_p20_edge_bps: number | null
  reverse_margin_edge_bps: number | null
  reverse_open_coverage: number | null
  reverse_borrow_24h_bps: number | null
  reverse_borrow_limit: number | null
  reverse_capacity_usdt: number | null
}

interface ReverseSignalSummary {
  total: number
  candidate: number
  rejected: number
  conditions_lost: number
  latest_signal_time: string | null
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_trade_signals'

const gridApi = shallowRef<GridApi<ReverseSignalRow> | null>(null)
const rowData = ref<ReverseSignalRow[]>([])
const summary = ref<ReverseSignalSummary>({
  total: 0,
  candidate: 0,
  rejected: 0,
  conditions_lost: 0,
  latest_signal_time: null,
})
const loading = ref(false)

const filterStatus = ref('')
const filterRejectReason = ref('')
const filterDays = ref(1)
const filterAsset = ref('')

const paginationPageSize = ref(100)
const paginationPageSizeOptions = [100, 500, 1000, 5000]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)

const columnVisibilities = ref<ColumnVisibility[]>([])
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

let autoRefreshTimer: ReturnType<typeof setInterval> | null = null

const statusMap: Record<string, { label: string; className: string }> = {
  candidate: { label: '候选', className: 'reverse-signal-success' },
  rejected: { label: '被拒', className: 'reverse-signal-danger' },
  conditions_lost: { label: '条件消失', className: 'reverse-signal-info' },
}

const reverseStatusMap: Record<string, string> = {
  candidate: '满足条件',
  missing_open_data: '缺盘口',
  funding_too_low: '费率不足',
  missing_borrow_data: '缺借币数据',
  borrow_unavailable: '不可借',
  borrow_capacity_low: '额度不足',
  depth_too_thin: '深度不足',
  missing_margin_edge: '缺少边际',
  margin_edge_too_low: '边际亏损',
}

const rejectReasonOptions = [
  { label: '反向开仓盘口数据不完整', value: '盘口数据不完整' },
  { label: 'Funding 非负', value: '非负' },
  { label: '借币数据缺失', value: '借币数据缺失' },
  { label: '币种不可借', value: '不可借' },
  { label: '借币额度不足', value: '借币额度不足' },
  { label: '开仓盘口覆盖不足', value: '盘口覆盖不足' },
  { label: '边际盈亏无法计算', value: '边际盈亏无法计算' },
  { label: '边际盈亏小于 0', value: '边际盈亏小于 0' },
  { label: '信号消失', value: '信号消失' },
]

const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map((row) => row.base_asset).filter(Boolean))
  return Array.from(assets).sort()
})

const totalPages = computed(() => Math.max(1, Math.ceil(paginationTotal.value / paginationPageSize.value)))

function formatBps(value: unknown): string {
  if (value == null || value === '') return ''
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : ''
}

function formatPercent(value: unknown, digits = 4): string {
  if (value == null || value === '') return ''
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : ''
}

function formatDecimal(value: unknown, maxDecimals = 8): string {
  if (value == null || value === '') return ''
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function formatUsdt(value: unknown): string {
  if (value == null || value === '') return ''
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 2 }) : ''
}

function formatTime(timeStr: string | null | undefined): string {
  if (!timeStr) return '无'
  if (timeStr.includes(' ')) return timeStr.split(' ')[1] || timeStr
  return timeStr
}

function statusLabel(status: string | null | undefined): string {
  if (!status) return ''
  return statusMap[status]?.label ?? status
}

function reverseStatusLabel(status: string | null | undefined): string {
  if (!status) return ''
  return reverseStatusMap[status] ?? status
}

function statusRenderer(params: { value?: string }) {
  const span = document.createElement('span')
  const meta = statusMap[params.value || ''] ?? { label: params.value || '', className: 'reverse-signal-info' }
  span.textContent = meta.label
  span.className = `reverse-signal-status ${meta.className}`
  return span
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

function applyDefaultSort() {
  gridApi.value?.applyColumnState({
    defaultState: { sort: null },
    state: [{ colId: 'signal_time', sort: 'desc', sortIndex: 0 }],
  })
}

async function fetchSignals() {
  if (loading.value) return
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (filterStatus.value) params.set('status', filterStatus.value)
    if (filterRejectReason.value) params.set('reject_reason', filterRejectReason.value)
    if (filterAsset.value.trim()) params.set('base_asset', filterAsset.value.trim())

    const res = await get(`/api/reverse-arbitrage/signals?${params.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError('反向交易信号加载失败')
      return
    }
    rowData.value = data.signals || []
    summary.value = data.summary || summary.value
    paginationTotal.value = data.pagination?.total || 0
  } catch {
    showError('反向交易信号加载失败')
  } finally {
    loading.value = false
  }
}

function resetPageAndFetch() {
  paginationCurrentPage.value = 1
  fetchSignals()
}

function setStatusFilter(status: string) {
  filterStatus.value = status
  resetPageAndFetch()
}

function setDaysFilter(days: number) {
  filterDays.value = days
  resetPageAndFetch()
}

function setRejectReasonFilter(reason: string) {
  filterRejectReason.value = reason
  resetPageAndFetch()
}

function onPageChange(page: number | undefined) {
  const nextPage = Math.min(Math.max(Number(page || 1), 1), totalPages.value)
  paginationCurrentPage.value = nextPage
  fetchSignals()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchSignals()
}

function onGridReady(event: GridReadyEvent<ReverseSignalRow>) {
  gridApi.value = event.api
  setupGridCopy(event.api)
  loadColumnState().finally(applyDefaultSort)
}

const columnDefs = ref<ColDef<ReverseSignalRow>[]>([
  { headerName: '标的资产', field: 'base_asset', width: 105, pinned: 'left' },
  { headerName: '信号时间', field: 'signal_time', width: 165, sort: 'desc', sortIndex: 0 },
  {
    headerName: '状态',
    field: 'status',
    width: 95,
    cellRenderer: statusRenderer,
    valueFormatter: (p: ValueFormatterParams<ReverseSignalRow>) => statusLabel(p.value as string),
  },
  {
    headerName: '判断结果',
    field: 'reverse_status',
    width: 115,
    valueFormatter: (p: ValueFormatterParams<ReverseSignalRow>) => reverseStatusLabel(p.value as string),
  },
  {
    headerName: '2h资金费率',
    field: 'funding_rate_2h',
    width: 115,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatPercent(p.value, 4),
  },
  {
    headerName: '预期Funding(bps)',
    field: 'reverse_expected_funding_bps',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value),
  },
  {
    headerName: '边际盈亏(bps)',
    field: 'reverse_margin_edge_bps',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value),
  },
  {
    headerName: '开仓基差(bps)',
    field: 'reverse_basis_bps',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value),
  },
  {
    headerName: '边际P20(bps)',
    field: 'reverse_p20_edge_bps',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value),
  },
  {
    headerName: '盘口覆盖',
    field: 'reverse_open_coverage',
    width: 105,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => p.value == null ? '' : `${(Number(p.value) * 100).toFixed(1)}%`,
  },
  {
    headerName: '借币24h成本(bps)',
    field: 'reverse_borrow_24h_bps',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatBps(p.value),
  },
  {
    headerName: '借币额度',
    field: 'reverse_borrow_limit',
    width: 115,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatDecimal(p.value, 4),
  },
  {
    headerName: '可做名义USDT',
    field: 'reverse_capacity_usdt',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatUsdt(p.value),
  },
  {
    headerName: '拒绝/结束原因',
    field: 'reject_reason',
    width: 260,
    tooltipField: 'reject_reason',
    tooltipComponent: LongTextTooltip,
  },
  { headerName: '结束时间', field: 'resolved_time', width: 165 },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

onMounted(() => {
  fetchSignals()
  autoRefreshTimer = setInterval(fetchSignals, 10000)
})

onUnmounted(() => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
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
        <span class="summary-label">被拒</span>
        <span class="summary-value summary-danger">{{ summary.rejected }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">条件消失</span>
        <span class="summary-value summary-info">{{ summary.conditions_lost }}</span>
      </span>
      <span class="summary-item">
        <span class="summary-label">最近信号</span>
        <span class="summary-value">{{ formatTime(summary.latest_signal_time) }}</span>
      </span>
    </div>

    <div class="filter-bar">
      <div class="filter-group">
        <span class="filter-label">状态：</span>
        <el-button-group size="small">
          <el-button :type="filterStatus === '' ? 'primary' : 'default'" @click="setStatusFilter('')">全部</el-button>
          <el-button :type="filterStatus === 'candidate' ? 'primary' : 'default'" @click="setStatusFilter('candidate')">候选</el-button>
          <el-button :type="filterStatus === 'rejected' ? 'primary' : 'default'" @click="setStatusFilter('rejected')">被拒</el-button>
          <el-button :type="filterStatus === 'conditions_lost' ? 'primary' : 'default'" @click="setStatusFilter('conditions_lost')">条件消失</el-button>
        </el-button-group>
      </div>

      <div class="filter-group">
        <span class="filter-label">时间：</span>
        <el-button-group size="small">
          <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="setDaysFilter(1)">今日</el-button>
          <el-button :type="filterDays === 3 ? 'primary' : 'default'" @click="setDaysFilter(3)">3天</el-button>
          <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="setDaysFilter(7)">7天</el-button>
          <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="setDaysFilter(30)">30天</el-button>
        </el-button-group>
      </div>

      <el-select
        v-model="filterAsset"
        placeholder="标的资产"
        size="small"
        filterable
        allow-create
        clearable
        style="width: 150px"
        @change="resetPageAndFetch"
      >
        <el-option v-for="asset in assetOptions" :key="asset" :label="asset" :value="asset" />
      </el-select>

      <el-select
        v-model="filterRejectReason"
        placeholder="原因"
        size="small"
        filterable
        clearable
        style="width: 180px"
        @change="(val: string) => setRejectReasonFilter(val || '')"
      >
        <el-option label="全部" value="" />
        <el-option
          v-for="option in rejectReasonOptions"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
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
        :tooltipShowDelay="300"
        :header-height="32"
        :row-height="32"
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
          controls-position="right"
          @change="onPageChange"
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
.summary-item,
.filter-group,
.pagination-bar,
.pagination-controls {
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

.summary-item,
.filter-group {
  gap: 6px;
}

.summary-label,
.filter-label,
.pagination-info {
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

.summary-danger {
  color: #f56c6c;
}

.summary-info {
  color: #909399;
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

.pagination-bar {
  justify-content: space-between;
  padding: 8px 4px 0;
}

.pagination-controls {
  gap: 0;
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
  min-width: 64px;
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
