<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent, ValueFormatterParams } from 'ag-grid-community'
import { Refresh, Search } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface PredictionSummary {
  asset_count?: number
  current_high_negative_count?: number
  threshold_rate?: number
  lookback_days?: number
  latest_history_time?: string | null
  avg_p_next_1?: number | null
  avg_p_next_2?: number | null
  avg_p_next_3?: number | null
}

interface PredictionRow {
  base_asset: string
  contract: string
  current_funding_rate_24h?: number | null
  previous_funding_rate_24h?: number | null
  funding_rate_change?: number | null
  current_bucket?: string | null
  current_bucket_label?: string | null
  sample_count?: number | null
  conditional_sample_count?: number | null
  high_negative_count?: number | null
  high_negative_frequency?: number | null
  negative_count?: number | null
  negative_frequency?: number | null
  min_funding_rate_24h?: number | null
  avg_funding_rate_24h?: number | null
  p_next_1?: number | null
  p_next_2?: number | null
  p_next_3?: number | null
  base_p_next_1?: number | null
  base_p_next_2?: number | null
  base_p_next_3?: number | null
  conditional_p_next_1?: number | null
  conditional_p_next_2?: number | null
  conditional_p_next_3?: number | null
  confidence?: number | null
  last_history_time?: string | null
  last_high_negative_time?: string | null
  funding_next_apply?: string | null
  current_updated_at?: string | null
}

interface PredictionPayload {
  summary?: PredictionSummary
  rows?: PredictionRow[]
  pagination?: {
    page: number
    page_size: number
    total: number
    total_pages: number
  }
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_funding_prediction'
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

const rowData = shallowRef<PredictionRow[]>([])
const summary = ref<PredictionSummary>({})
const loading = ref(false)
const keyword = ref('')
const thresholdPct = ref(-1)
const lookbackDays = ref(30)
const autoRefresh = ref(true)
const refreshIntervalSec = ref(300)
const lastLoadedAt = ref('--')
const paginationPageSize = ref(100)
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)
const columnVisibilities = ref<ColumnVisibility[]>([])

let gridApi: GridApi<PredictionRow> | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

const totalPages = computed(() => Math.ceil(paginationTotal.value / paginationPageSize.value) || 1)

const summaryItems = computed(() => [
  { label: '观察标的', value: summary.value.asset_count ?? 0, tone: '' },
  { label: '当前高负', value: summary.value.current_high_negative_count ?? 0, tone: 'danger' },
  { label: '平均P1', value: formatProbability(summary.value.avg_p_next_1), tone: 'info' },
  { label: '平均P2', value: formatProbability(summary.value.avg_p_next_2), tone: 'info' },
  { label: '平均P3', value: formatProbability(summary.value.avg_p_next_3), tone: 'info' },
])

const defaultColDef: ColDef<PredictionRow> = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function formatProbability(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : ''
}

function formatFunding(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(4)}%` : ''
}

function formatDecimal(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : ''
}

const probabilityFormatter = (params: ValueFormatterParams<PredictionRow>) => formatProbability(params.value)
const fundingFormatter = (params: ValueFormatterParams<PredictionRow>) => formatFunding(params.value)
const numberFormatter = (params: ValueFormatterParams<PredictionRow>) => formatDecimal(params.value, 0)

function fundingCellClass(params: ValueFormatterParams<PredictionRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  if (n <= thresholdPct.value / 100) return 'value-danger'
  if (n < 0) return 'value-negative'
  return 'value-positive'
}

function probabilityCellClass(params: ValueFormatterParams<PredictionRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  if (n >= 0.5) return 'value-danger'
  if (n >= 0.2) return 'value-warning'
  if (n > 0) return 'value-info'
  return ''
}

const columnDefs = ref<ColDef<PredictionRow>[]>([
  { field: 'base_asset', headerName: '标的资产', width: 100, pinned: 'left' },
  { field: 'contract', headerName: '合约', width: 120 },
  {
    field: 'current_funding_rate_24h',
    headerName: '当前24h资金费',
    width: 140,
    valueFormatter: fundingFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'p_next_1',
    headerName: '未来1期高负概率',
    width: 145,
    type: 'numericColumn',
    valueFormatter: probabilityFormatter,
    cellClass: probabilityCellClass,
  },
  {
    field: 'p_next_2',
    headerName: '未来2期高负概率',
    width: 145,
    type: 'numericColumn',
    valueFormatter: probabilityFormatter,
    cellClass: probabilityCellClass,
  },
  {
    field: 'p_next_3',
    headerName: '未来3期高负概率',
    width: 145,
    type: 'numericColumn',
    sort: 'desc',
    sortIndex: 0,
    valueFormatter: probabilityFormatter,
    cellClass: probabilityCellClass,
  },
  { field: 'current_bucket_label', headerName: '当前状态', width: 105 },
  {
    field: 'high_negative_frequency',
    headerName: '30天高负频率',
    width: 125,
    type: 'numericColumn',
    valueFormatter: probabilityFormatter,
    cellClass: probabilityCellClass,
  },
  {
    field: 'high_negative_count',
    headerName: '高负次数',
    width: 95,
    type: 'numericColumn',
    valueFormatter: numberFormatter,
  },
  {
    field: 'negative_frequency',
    headerName: '负费率频率',
    width: 115,
    type: 'numericColumn',
    valueFormatter: probabilityFormatter,
  },
  {
    field: 'min_funding_rate_24h',
    headerName: '历史最低24h',
    width: 120,
    valueFormatter: fundingFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'avg_funding_rate_24h',
    headerName: '历史均值24h',
    width: 120,
    valueFormatter: fundingFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'funding_rate_change',
    headerName: '当前变化',
    width: 105,
    valueFormatter: fundingFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'confidence',
    headerName: '置信度',
    width: 95,
    type: 'numericColumn',
    valueFormatter: probabilityFormatter,
  },
  {
    field: 'sample_count',
    headerName: '样本数',
    width: 90,
    type: 'numericColumn',
    valueFormatter: numberFormatter,
  },
  {
    field: 'conditional_sample_count',
    headerName: '同状态样本',
    width: 110,
    type: 'numericColumn',
    valueFormatter: numberFormatter,
  },
  { field: 'last_high_negative_time', headerName: '最近高负时间', width: 160 },
  { field: 'funding_next_apply', headerName: '下次支付时间', width: 160 },
  { field: 'last_history_time', headerName: '历史更新时间', width: 160 },
])

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field)
    .map((col) => {
      const colId = col.field as string
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
    if (Array.isArray(data?.columnState)) {
      gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch {
    /* ignore */
  }
}

async function fetchPredictions(resetPage = false) {
  if (loading.value) return
  if (resetPage) paginationCurrentPage.value = 1
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('threshold', String(thresholdPct.value / 100))
    params.set('lookback_days', String(lookbackDays.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
    const res = await get(`/api/reverse-funding/predictions?${params.toString()}`)
    const data: PredictionPayload = await res.json()
    if (!res.ok) {
      showError('获取Funding预测失败')
      return
    }
    rowData.value = Array.isArray(data.rows) ? data.rows : []
    summary.value = data.summary ?? {}
    paginationTotal.value = Number(data.pagination?.total || 0)
    lastLoadedAt.value = new Date().toLocaleTimeString()
  } catch {
    showError('获取Funding预测失败')
  } finally {
    loading.value = false
  }
}

function onGridReady(params: GridReadyEvent<PredictionRow>) {
  gridApi = params.api
  setupGridCopy(params.api)
  loadColumnState()
}

function onPageChange(page: number | null) {
  paginationCurrentPage.value = Number(page || 1)
  fetchPredictions()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchPredictions()
}

function restartTimer() {
  if (refreshTimer) clearInterval(refreshTimer)
  if (!autoRefresh.value) return
  refreshTimer = setInterval(() => fetchPredictions(), Math.max(refreshIntervalSec.value, 30) * 1000)
}

function applyFilterChange() {
  paginationCurrentPage.value = 1
  fetchPredictions()
  restartTimer()
}

onMounted(() => {
  fetchPredictions()
  restartTimer()
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="prediction-page">
    <el-card shadow="never" class="status-card">
      <div class="summary-row">
        <span
          v-for="item in summaryItems"
          :key="item.label"
          class="summary-item"
        >
          <span class="summary-label">{{ item.label }}</span>
          <span class="summary-value" :class="item.tone">{{ item.value }}</span>
        </span>
        <span class="summary-item">
          <span class="summary-label">阈值</span>
          <span class="summary-value danger">{{ formatFunding(summary.threshold_rate ?? thresholdPct / 100) }}</span>
        </span>
        <span class="summary-item">
          <span class="summary-label">历史更新</span>
          <span class="summary-value">{{ summary.latest_history_time || '--' }}</span>
        </span>
        <span class="summary-item">
          <span class="summary-label">页面刷新</span>
          <span class="summary-value">{{ lastLoadedAt }}</span>
        </span>
      </div>
    </el-card>

    <el-card shadow="never" class="filter-card">
      <div class="filter-row">
        <el-input
          v-model="keyword"
          placeholder="搜索标的/合约"
          clearable
          size="small"
          style="width: 220px"
          @keyup.enter="applyFilterChange"
          @clear="applyFilterChange"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <span class="filter-label">高负阈值(%)</span>
        <el-input-number
          v-model="thresholdPct"
          size="small"
          :min="-50"
          :max="-0.01"
          :step="0.1"
          :precision="2"
          controls-position="right"
          @change="applyFilterChange"
        />
        <span class="filter-label">回看</span>
        <el-select v-model="lookbackDays" size="small" style="width: 100px" @change="applyFilterChange">
          <el-option :value="7" label="7天" />
          <el-option :value="14" label="14天" />
          <el-option :value="30" label="30天" />
          <el-option :value="60" label="60天" />
          <el-option :value="90" label="90天" />
        </el-select>
        <el-switch
          v-model="autoRefresh"
          inline-prompt
          active-text="自动刷新"
          inactive-text="手动刷新"
          @change="restartTimer"
        />
        <el-select v-model="refreshIntervalSec" size="small" style="width: 110px" @change="restartTimer">
          <el-option :value="60" label="1分钟" />
          <el-option :value="180" label="3分钟" />
          <el-option :value="300" label="5分钟" />
          <el-option :value="600" label="10分钟" />
        </el-select>
        <el-button size="small" type="primary" :loading="loading" :icon="Refresh" @click="fetchPredictions(true)">
          刷新
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <span>高负Funding预测</span>
          <div class="header-actions">
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
      </template>
      <div ref="gridContainerRef">
        <AgGridVue
          class="prediction-grid"
          :theme="orderbookGridTheme"
          :columnDefs="columnDefs"
          :rowData="rowData"
          :defaultColDef="defaultColDef"
          :header-height="32"
          :row-height="32"
          @grid-ready="onGridReady"
        />
      </div>
    </el-card>

    <div class="pagination-bar">
      <div class="pagination-info">
        共 {{ paginationTotal }} 条记录，第 {{ paginationCurrentPage }} / {{ totalPages }} 页
      </div>
      <div class="pagination-controls">
        <el-button size="small" :disabled="paginationCurrentPage === 1" @click="onPageChange(paginationCurrentPage - 1)">
          上一页
        </el-button>
        <el-select v-model="paginationPageSize" size="small" style="width: 100px; margin: 0 8px" @change="onPaginationSizeChange">
          <el-option v-for="size in paginationPageSizeOptions" :key="size" :label="`${size}条/页`" :value="size" />
        </el-select>
        <el-button size="small" :disabled="paginationCurrentPage === totalPages" @click="onPageChange(paginationCurrentPage + 1)">
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
.prediction-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
}

.status-card,
.filter-card,
.grid-card {
  border-radius: 4px;
  border-color: var(--app-border);
}

.summary-row,
.filter-row,
.grid-header,
.header-actions,
.pagination-bar,
.pagination-controls {
  display: flex;
  align-items: center;
}

.summary-row,
.filter-row {
  flex-wrap: wrap;
  gap: 12px;
}

.summary-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--app-text-muted);
  font-size: 13px;
}

.summary-value {
  color: var(--app-text);
  font-weight: 700;
}

.summary-value.danger,
:deep(.value-danger) {
  color: #f56c6c;
}

.summary-value.info,
:deep(.value-info) {
  color: #409eff;
}

:deep(.value-warning) {
  color: #e6a23c;
}

:deep(.value-negative) {
  color: #f56c6c;
}

:deep(.value-positive) {
  color: #67c23a;
}

.filter-label {
  color: var(--app-text-muted);
  font-size: 13px;
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

.grid-header {
  justify-content: space-between;
  gap: 12px;
}

.header-actions {
  gap: 8px;
}

.prediction-grid {
  width: 100%;
  height: calc(100vh - 300px);
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

.column-picker-label,
.pagination-info {
  font-size: 13px;
}

.pagination-bar {
  justify-content: space-between;
  color: var(--app-text-muted);
}
</style>
