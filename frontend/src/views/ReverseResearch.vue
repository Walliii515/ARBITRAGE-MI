<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  CellClassParams,
  ColDef,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
} from 'ag-grid-community'
import { Refresh, Search } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

type ResearchView = 'negative' | 'drain' | 'candidate'

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

interface ReverseResearchRow {
  id?: number
  snapshot_time?: string
  base_asset?: string
  contract?: string
  symbol?: string
  sample_source?: string
  funding_rate_24h?: number | null
  gross_funding_bps?: number | null
  expected_funding_bps?: number | null
  next_funding_time?: string | null
  next_funding_min?: number | null
  borrowable?: number | boolean | null
  max_borrowable_amount?: number | null
  account_borrow_limit?: number | null
  borrow_capacity_usdt?: number | null
  borrow_hourly_rate?: number | null
  borrow_24h_bps?: number | null
  borrow_unavailable_reason?: string | null
  reverse_open_basis_bps?: number | null
  reverse_close_basis_bps?: number | null
  reverse_margin_edge_bps?: number | null
  reverse_open_coverage?: number | null
  spot_spread_bps?: number | null
  future_spread_bps?: number | null
  spot_top_bid_usdt?: number | null
  future_top_ask_usdt?: number | null
  spot_quote_volume_24h?: number | null
  future_volume_24h_settle?: number | null
  reverse_status?: string | null
  borrow_change_5m_pct?: number | null
  borrow_change_15m_pct?: number | null
}

interface ReverseResearchSummary {
  asset_count?: number
  borrowable_count?: number
  zero_borrow_count?: number
  negative_funding_count?: number
  candidate_count?: number
  drain_15m_count?: number
  latest_snapshot_time?: string | null
}

interface ReverseResearchPayload {
  hours?: number
  summary?: ReverseResearchSummary
  top_negative_funding?: ReverseResearchRow[]
  top_borrow_drain?: ReverseResearchRow[]
  top_candidates?: ReverseResearchRow[]
}

const PAGE_KEY = 'reverse_research_analysis'

const activeView = ref<ResearchView>('negative')
const hours = ref(24)
const loading = ref(false)
const collectLoading = ref(false)
const keyword = ref('')
const summary = ref<ReverseResearchSummary>({})
const negativeRows = shallowRef<ReverseResearchRow[]>([])
const drainRows = shallowRef<ReverseResearchRow[]>([])
const candidateRows = shallowRef<ReverseResearchRow[]>([])
const columnVisibilities = ref<ColumnVisibility[]>([])
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

let gridApi: GridApi<ReverseResearchRow> | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

const sourceRows = computed(() => {
  if (activeView.value === 'drain') return drainRows.value
  if (activeView.value === 'candidate') return candidateRows.value
  return negativeRows.value
})

const rowData = computed(() => {
  const term = keyword.value.trim().toLowerCase()
  if (!term) return sourceRows.value
  return sourceRows.value.filter((row) => {
    const asset = String(row.base_asset || '').toLowerCase()
    const contract = String(row.contract || '').toLowerCase()
    return asset.includes(term) || contract.includes(term)
  })
})

const summaryItems = computed(() => [
  { label: '观察标的', value: summary.value.asset_count ?? 0, tone: '' },
  { label: '负费率', value: summary.value.negative_funding_count ?? 0, tone: 'danger' },
  { label: '可借达标', value: summary.value.borrowable_count ?? 0, tone: 'success' },
  { label: '借不到', value: summary.value.zero_borrow_count ?? 0, tone: 'warning' },
  { label: '候选', value: summary.value.candidate_count ?? 0, tone: 'info' },
  { label: '15m额度流失', value: summary.value.drain_15m_count ?? 0, tone: 'danger' },
])

const defaultColDef: ColDef<ReverseResearchRow> = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function formatDecimal(value: unknown, maxDecimals = 8): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function formatBps(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : ''
}

function formatPercent(value: unknown, decimals = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(decimals)}%` : ''
}

function formatFunding(params: ValueFormatterParams<ReverseResearchRow>) {
  const n = Number(params.value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(4)}%` : ''
}

function formatHourlyRate(params: ValueFormatterParams<ReverseResearchRow>) {
  const n = Number(params.value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(6)}%` : ''
}

function formatNumber(params: ValueFormatterParams<ReverseResearchRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function statusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'candidate': return '候选'
    case 'missing_open_data': return '缺盘口'
    case 'funding_too_low': return '费率不足'
    case 'missing_borrow_data': return '待接借币'
    case 'borrow_unavailable': return '不可借'
    case 'borrow_capacity_low': return '额度不足'
    case 'margin_edge_too_low': return '边际不足'
    case 'depth_too_thin': return '深度不足'
    case 'missing_margin_edge': return '缺边际'
    default: return status || ''
  }
}

function signedCellClass(params: CellClassParams<ReverseResearchRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  if (n > 0) return 'value-positive'
  if (n < 0) return 'value-negative'
  return ''
}

const columnDefs = ref<ColDef<ReverseResearchRow>[]>([
  { field: 'base_asset', headerName: '标的资产', minWidth: 96, pinned: 'left' },
  { field: 'snapshot_time', headerName: '快照时间', minWidth: 150 },
  {
    field: 'funding_rate_24h',
    headerName: '24h资金费率',
    minWidth: 120,
    valueFormatter: formatFunding,
    cellClass: signedCellClass,
    sort: 'asc',
  },
  {
    field: 'gross_funding_bps',
    headerName: '可收Funding(bps)',
    minWidth: 132,
    type: 'numericColumn',
    valueFormatter: (p) => formatBps(p.value),
    cellClass: 'value-positive',
  },
  {
    field: 'reverse_margin_edge_bps',
    headerName: '边际盈亏(bps)',
    minWidth: 128,
    type: 'numericColumn',
    valueFormatter: (p) => formatBps(p.value),
    cellClass: signedCellClass,
  },
  {
    field: 'max_borrowable_amount',
    headerName: '真实可借数量',
    minWidth: 130,
    type: 'numericColumn',
    valueFormatter: (p) => formatDecimal(p.value, 6),
  },
  {
    field: 'account_borrow_limit',
    headerName: '账户借币上限',
    minWidth: 130,
    type: 'numericColumn',
    valueFormatter: (p) => formatDecimal(p.value, 6),
  },
  {
    field: 'borrow_capacity_usdt',
    headerName: '可做名义USDT',
    minWidth: 130,
    type: 'numericColumn',
    valueFormatter: formatNumber,
  },
  {
    field: 'borrow_change_5m_pct',
    headerName: '5m额度变化',
    minWidth: 120,
    type: 'numericColumn',
    valueFormatter: (p) => formatPercent(p.value, 1),
    cellClass: signedCellClass,
  },
  {
    field: 'borrow_change_15m_pct',
    headerName: '15m额度变化',
    minWidth: 126,
    type: 'numericColumn',
    valueFormatter: (p) => formatPercent(p.value, 1),
    cellClass: signedCellClass,
  },
  {
    field: 'borrow_hourly_rate',
    headerName: '借币小时利率',
    minWidth: 126,
    type: 'numericColumn',
    valueFormatter: formatHourlyRate,
  },
  {
    field: 'borrow_24h_bps',
    headerName: '借币24h成本(bps)',
    minWidth: 144,
    type: 'numericColumn',
    valueFormatter: (p) => formatBps(p.value),
  },
  {
    field: 'reverse_open_basis_bps',
    headerName: '反向开仓基差(bps)',
    minWidth: 146,
    type: 'numericColumn',
    valueFormatter: (p) => formatBps(p.value),
    cellClass: signedCellClass,
  },
  {
    field: 'reverse_close_basis_bps',
    headerName: '反向平仓基差(bps)',
    minWidth: 146,
    type: 'numericColumn',
    valueFormatter: (p) => formatBps(p.value),
    cellClass: signedCellClass,
  },
  {
    field: 'reverse_open_coverage',
    headerName: '盘口覆盖',
    minWidth: 100,
    type: 'numericColumn',
    valueFormatter: (p) => formatPercent(Number(p.value) * 100, 1),
  },
  {
    field: 'next_funding_time',
    headerName: '下次支付时间',
    minWidth: 150,
  },
  {
    field: 'next_funding_min',
    headerName: '剩余分钟',
    minWidth: 100,
    type: 'numericColumn',
    valueFormatter: (p) => formatDecimal(p.value, 1),
  },
  {
    field: 'reverse_status',
    headerName: '状态',
    minWidth: 96,
    valueFormatter: (p) => statusLabel(p.value as string | null),
  },
  {
    field: 'borrow_unavailable_reason',
    headerName: '借币原因',
    minWidth: 160,
  },
  {
    field: 'sample_source',
    headerName: '来源',
    minWidth: 86,
  },
])

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .map((def) => {
      const colId = String(def.colId || def.field || '')
      const state = states.find((item) => item.colId === colId)
      return {
        colId,
        headerName: String(def.headerName || colId),
        visible: state?.hide !== true,
      }
    })
    .filter((item) => item.colId)
}

function toggleColumnVisibility(colId: string, visible: boolean) {
  if (!gridApi) return
  gridApi.setColumnsVisible([colId], visible)
  refreshColumnVisibilities()
}

async function saveColumnState() {
  if (!gridApi) return
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, {
      columnState: gridApi.getColumnState(),
    })
    const data = await res.json()
    if (data?.ok) showSuccess('列配置已保存')
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
    if (Array.isArray(data?.columnState) && data.columnState.length > 0) {
      gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
    }
    refreshColumnVisibilities()
  } catch {
    refreshColumnVisibilities()
  }
}

async function fetchAnalysis() {
  loading.value = true
  try {
    const res = await get(`/api/reverse-research/analysis?hours=${hours.value}&limit=150`)
    const data = (await res.json()) as ReverseResearchPayload
    summary.value = data.summary || {}
    negativeRows.value = data.top_negative_funding || []
    drainRows.value = data.top_borrow_drain || []
    candidateRows.value = data.top_candidates || []
  } catch {
    showError('加载反向研究分析失败')
  } finally {
    loading.value = false
  }
}

async function collectSnapshot() {
  collectLoading.value = true
  try {
    const res = await post('/api/reverse-research/collect')
    const data = await res.json()
    if (data?.ok) showSuccess(`已采集 ${data.inserted || 0} 条借币快照`)
    await fetchAnalysis()
  } catch {
    showError('手动采集失败')
  } finally {
    collectLoading.value = false
  }
}

function onGridReady(params: GridReadyEvent<ReverseResearchRow>) {
  gridApi = params.api
  setupGridCopy(params.api)
  loadColumnState()
}

onMounted(() => {
  fetchAnalysis()
  refreshTimer = setInterval(fetchAnalysis, 60_000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="reverse-research-page">
    <div class="summary-bar">
      <span v-for="item in summaryItems" :key="item.label" class="summary-item">
        <span class="summary-label">{{ item.label }}</span>
        <span class="summary-value" :class="item.tone ? `summary-${item.tone}` : ''">
          {{ item.value }}
        </span>
      </span>
      <span class="summary-item">
        <span class="summary-label">最近快照</span>
        <span class="summary-value">{{ summary.latest_snapshot_time || '--' }}</span>
      </span>
    </div>

    <div class="filter-bar">
      <el-button-group size="small">
        <el-button :type="activeView === 'negative' ? 'primary' : 'default'" @click="activeView = 'negative'">负费率</el-button>
        <el-button :type="activeView === 'drain' ? 'primary' : 'default'" @click="activeView = 'drain'">借币流失</el-button>
        <el-button :type="activeView === 'candidate' ? 'primary' : 'default'" @click="activeView = 'candidate'">候选观察</el-button>
      </el-button-group>
      <el-input
        v-model="keyword"
        :prefix-icon="Search"
        placeholder="标的资产"
        size="small"
        clearable
        style="width: 150px"
      />
      <el-select v-model="hours" size="small" style="width: 110px" @change="fetchAnalysis">
        <el-option :value="6" label="最近6小时" />
        <el-option :value="24" label="最近24小时" />
        <el-option :value="72" label="最近3天" />
        <el-option :value="168" label="最近7天" />
      </el-select>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchAnalysis">刷新</el-button>
      <el-button size="small" :loading="collectLoading" @click="collectSnapshot">手动采集</el-button>
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
        :getRowId="(params: any) => String(params.data?.id ?? `${params.data?.base_asset}-${params.data?.snapshot_time}`)"
        :header-height="32"
        :row-height="32"
        style="width: 100%; height: 100%"
        @grid-ready="onGridReady"
      />
    </div>
  </div>
</template>

<style scoped>
.reverse-research-page {
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

.summary-success,
:deep(.value-positive) {
  color: #67c23a;
}

.summary-warning {
  color: #e6a23c;
}

.summary-danger,
:deep(.value-negative) {
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
</style>
