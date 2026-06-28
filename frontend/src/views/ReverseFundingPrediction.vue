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
  generated_at?: string | null
  model_version?: string | null
  source?: string | null
  avg_follow_score?: number | null
  avg_borrow_pressure_score?: number | null
  follow_candidate_count?: number
  borrow_drop_count?: number
  funding_down_count?: number
  filter_steps?: FilterStep[]
  filter_options?: Record<string, unknown>
}

interface PredictionRow {
  base_asset: string
  contract: string
  strategy_tier?: string | null
  expected_funding_bps?: number | null
  borrowable?: number | boolean | null
  borrow_capacity_usdt?: number | null
  borrow_hourly_rate?: number | null
  borrow_24h_bps?: number | null
  max_borrowable_amount?: number | null
  borrow_snapshot_time?: string | null
  follow_score?: number | null
  follow_reason?: string | null
  funding_change_1h_bps?: number | null
  funding_change_4h_bps?: number | null
  funding_change_12h_bps?: number | null
  borrow_capacity_drop_1h_pct?: number | null
  borrow_capacity_drop_4h_pct?: number | null
  borrow_capacity_drop_12h_pct?: number | null
  borrow_capacity_drop_max_pct?: number | null
  borrow_capacity_drop_1h_usdt?: number | null
  borrow_capacity_drop_4h_usdt?: number | null
  borrow_capacity_drop_12h_usdt?: number | null
  borrow_capacity_change_1h_usdt?: number | null
  borrow_capacity_change_4h_usdt?: number | null
  borrow_capacity_change_12h_usdt?: number | null
  borrow_capacity_24h_high_usdt?: number | null
  borrow_capacity_drawdown_24h_pct?: number | null
  borrow_availability_1h_pct?: number | null
  borrow_availability_4h_pct?: number | null
  borrow_availability_12h_pct?: number | null
  borrow_pressure_score?: number | null
  borrow_pressure_filter_pass?: boolean | null
  follow_score_filter_pass?: boolean | null
  funding_down_filter_pass?: boolean | null
  borrow_drop_filter_pass?: boolean | null
  history_high_negative_filter_pass?: boolean | null
  probability_filter_pass?: boolean | null
  confidence_filter_pass?: boolean | null
  negative_funding_filter_pass?: boolean | null
  borrowable_filter_pass?: boolean | null
  capacity_filter_pass?: boolean | null
  borrow_cost_filter_pass?: boolean | null
  preborrow_filter_pass?: boolean | null
  preborrow_filter_reason?: string | null
  current_funding_rate_24h?: number | null
  previous_funding_rate_24h?: number | null
  funding_rate_change?: number | null
  current_bucket?: string | null
  current_bucket_label?: string | null
  sample_count?: number | null
  conditional_sample_count?: number | null
  high_negative_count?: number | null
  high_negative_frequency?: number | null
  avg_funding_rate_24h?: number | null
  confidence?: number | null
  last_history_time?: string | null
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

interface FilterStep {
  key: string
  label: string
  enabled: boolean
  count: number
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
const recomputing = ref(false)
const keyword = ref('')
const thresholdPct = ref(-0.6)
const lookbackDays = ref(30)
const followScoreFilter = ref(false)
const minFollowScore = ref(50)
const fundingDownFilter = ref(false)
const minFundingDropBps = ref(5)
const borrowDropFilter = ref(false)
const minBorrowPressureScore = ref(12)
const minCapacityDrawdownPct = ref(2)
const minCapacityDropUsdt = ref(5)
const historyHighNegativeFilter = ref(false)
const borrowableFilter = ref(false)
const minBorrowCapacityUsdt = ref(100)
const borrowCostFilter = ref(false)
const maxBorrowCostRatio = ref(1)
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
  { label: '跟随候选', value: summary.value.follow_candidate_count ?? 0, tone: 'danger' },
  { label: '资金费下行', value: summary.value.funding_down_count ?? 0, tone: 'info' },
  { label: '额度压力', value: summary.value.borrow_drop_count ?? 0, tone: 'warning' },
  { label: '当前高负', value: summary.value.current_high_negative_count ?? 0, tone: 'danger' },
  { label: '平均跟随分', value: formatDecimal(summary.value.avg_follow_score, 1), tone: 'info' },
  { label: '平均压力分', value: formatDecimal(summary.value.avg_borrow_pressure_score, 1), tone: 'info' },
])

const filterSteps = computed(() => summary.value.filter_steps ?? [])

const sourceLabel = computed(() => {
  if (summary.value.source === 'stored') return '落库模型'
  if (summary.value.source === 'live') return '即时计算'
  return '--'
})

const defaultColDef: ColDef<PredictionRow> = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function formatFunding(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(4)}%` : ''
}

function formatDecimal(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : ''
}

function formatBps(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)} bps` : ''
}

function formatUsdt(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)} U` : ''
}

function formatPercentValue(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : ''
}

function formatBool(value: unknown): string {
  if (value === true || value === 1) return '是'
  if (value === false || value === 0) return '否'
  return ''
}

const fundingFormatter = (params: ValueFormatterParams<PredictionRow>) => formatFunding(params.value)
const numberFormatter = (params: ValueFormatterParams<PredictionRow>) => formatDecimal(params.value, 0)
const bpsFormatter = (params: ValueFormatterParams<PredictionRow>) => formatBps(params.value)
const usdtFormatter = (params: ValueFormatterParams<PredictionRow>) => formatUsdt(params.value)
const boolFormatter = (params: ValueFormatterParams<PredictionRow>) => formatBool(params.value)
const percentValueFormatter = (params: ValueFormatterParams<PredictionRow>) => formatPercentValue(params.value)

function fundingCellClass(params: ValueFormatterParams<PredictionRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  if (n <= thresholdPct.value / 100) return 'value-danger'
  if (n < 0) return 'value-negative'
  return 'value-positive'
}

function scoreCellClass(params: ValueFormatterParams<PredictionRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  if (n >= 80) return 'value-danger'
  if (n >= 50) return 'value-warning'
  if (n > 0) return 'value-info'
  return ''
}

function dropCellClass(params: ValueFormatterParams<PredictionRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n >= 5) return 'value-danger'
  if (n >= 2) return 'value-warning'
  return 'value-info'
}

function usdtDropCellClass(params: ValueFormatterParams<PredictionRow>) {
  const n = Number(params.value)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n >= 20) return 'value-danger'
  if (n >= 5) return 'value-warning'
  return 'value-info'
}

function passCellClass(params: ValueFormatterParams<PredictionRow>) {
  if (params.value === true || params.value === 1) return 'value-positive'
  if (params.value === false || params.value === 0) return 'value-negative'
  return ''
}

const columnDefs = ref<ColDef<PredictionRow>[]>([
  { field: 'base_asset', headerName: '标的资产', width: 100, pinned: 'left' },
  { field: 'strategy_tier', headerName: '分层', width: 80 },
  {
    field: 'follow_score',
    headerName: '跟随分',
    width: 95,
    type: 'numericColumn',
    sort: 'desc',
    sortIndex: 0,
    valueFormatter: (params) => formatDecimal(params.value, 1),
    cellClass: scoreCellClass,
  },
  {
    field: 'preborrow_filter_pass',
    headerName: '候选通过',
    width: 95,
    valueFormatter: boolFormatter,
    cellClass: passCellClass,
  },
  {
    field: 'follow_reason',
    headerName: '跟随解释',
    width: 360,
    tooltipField: 'follow_reason',
  },
  {
    field: 'current_funding_rate_24h',
    headerName: '当前24h资金费',
    width: 140,
    valueFormatter: fundingFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'funding_change_1h_bps',
    headerName: '资金费1h变化',
    width: 125,
    type: 'numericColumn',
    valueFormatter: bpsFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'funding_change_4h_bps',
    headerName: '资金费4h变化',
    width: 125,
    type: 'numericColumn',
    valueFormatter: bpsFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'funding_change_12h_bps',
    headerName: '资金费12h变化',
    width: 130,
    type: 'numericColumn',
    valueFormatter: bpsFormatter,
    cellClass: fundingCellClass,
  },
  {
    field: 'borrow_capacity_usdt',
    headerName: '当前可借额度',
    width: 125,
    type: 'numericColumn',
    valueFormatter: usdtFormatter,
  },
  {
    field: 'borrow_pressure_score',
    headerName: '额度压力分',
    width: 115,
    type: 'numericColumn',
    valueFormatter: (params) => formatDecimal(params.value, 1),
    cellClass: scoreCellClass,
  },
  {
    field: 'borrow_capacity_drop_4h_usdt',
    headerName: '4h下降U',
    width: 105,
    type: 'numericColumn',
    valueFormatter: usdtFormatter,
    cellClass: usdtDropCellClass,
  },
  {
    field: 'borrow_capacity_24h_high_usdt',
    headerName: '24h高点额度',
    width: 125,
    type: 'numericColumn',
    valueFormatter: usdtFormatter,
  },
  {
    field: 'borrow_capacity_drawdown_24h_pct',
    headerName: '高点回撤',
    width: 105,
    type: 'numericColumn',
    valueFormatter: percentValueFormatter,
    cellClass: dropCellClass,
  },
  {
    field: 'borrow_availability_4h_pct',
    headerName: '4h可借占比',
    width: 115,
    type: 'numericColumn',
    valueFormatter: percentValueFormatter,
  },
  {
    field: 'borrowable',
    headerName: '当前可借',
    width: 95,
    valueFormatter: boolFormatter,
    cellClass: passCellClass,
  },
  {
    field: 'high_negative_count',
    headerName: '历史高负次数',
    width: 120,
    type: 'numericColumn',
    valueFormatter: numberFormatter,
  },
  {
    field: 'expected_funding_bps',
    headerName: '当前预期Funding',
    width: 135,
    type: 'numericColumn',
    valueFormatter: bpsFormatter,
    cellClass: scoreCellClass,
  },
  {
    field: 'borrow_24h_bps',
    headerName: '借币24h成本',
    width: 120,
    type: 'numericColumn',
    valueFormatter: bpsFormatter,
  },
  {
    field: 'preborrow_filter_reason',
    headerName: '过滤逻辑',
    width: 360,
    tooltipField: 'preborrow_filter_reason',
  },
  { field: 'funding_next_apply', headerName: '下次支付时间', width: 160 },
  { field: 'borrow_snapshot_time', headerName: '借币更新时间', width: 160 },
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
    params.set('prefer_stored', 'true')
    params.set('follow_score_filter', String(followScoreFilter.value))
    params.set('min_follow_score', String(minFollowScore.value))
    params.set('funding_down_filter', String(fundingDownFilter.value))
    params.set('min_funding_drop_bps', String(minFundingDropBps.value))
    params.set('borrow_drop_filter', String(borrowDropFilter.value))
    params.set('min_borrow_pressure_score', String(minBorrowPressureScore.value))
    params.set('min_capacity_drawdown_pct', String(minCapacityDrawdownPct.value))
    params.set('min_capacity_drop_usdt', String(minCapacityDropUsdt.value))
    params.set('history_high_negative_filter', String(historyHighNegativeFilter.value))
    params.set('borrowable_filter', String(borrowableFilter.value))
    params.set('min_borrow_capacity_usdt', String(minBorrowCapacityUsdt.value))
    params.set('borrow_cost_filter', String(borrowCostFilter.value))
    params.set('max_borrow_cost_ratio', String(maxBorrowCostRatio.value))
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

async function recomputePredictions() {
  if (recomputing.value) return
  recomputing.value = true
  try {
    const params = new URLSearchParams()
    params.set('threshold', String(thresholdPct.value / 100))
    params.set('lookback_days', String(lookbackDays.value))
    const res = await post(`/api/reverse-funding/predictions/refresh?${params.toString()}`)
    const data = await res.json()
    if (!res.ok || data?.success === false) {
      showError(data?.message || '重算模型失败')
      return
    }
    showSuccess(`模型已重算，写入 ${data?.inserted ?? 0} 条`)
    await fetchPredictions(true)
  } catch {
    showError('重算模型失败')
  } finally {
    recomputing.value = false
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
          <span class="summary-label">模型更新</span>
          <span class="summary-value">{{ summary.generated_at || '--' }}</span>
        </span>
        <span class="summary-item">
          <span class="summary-label">模型来源</span>
          <span class="summary-value">{{ sourceLabel }}</span>
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
        <el-button size="small" :loading="recomputing" @click="recomputePredictions">
          重算模型
        </el-button>
      </div>
      <div class="filter-row rule-row">
        <el-switch
          v-model="followScoreFilter"
          inline-prompt
          active-text="跟随分"
          inactive-text="跟随分"
          @change="applyFilterChange"
        />
        <span class="filter-label">≥</span>
        <el-input-number
          v-model="minFollowScore"
          size="small"
          :min="0"
          :max="200"
          :step="5"
          :precision="0"
          controls-position="right"
          @change="applyFilterChange"
        />
        <el-switch
          v-model="fundingDownFilter"
          inline-prompt
          active-text="资金费下行"
          inactive-text="资金费下行"
          @change="applyFilterChange"
        />
        <span class="filter-label">≥</span>
        <el-input-number
          v-model="minFundingDropBps"
          size="small"
          :min="0"
          :max="500"
          :step="1"
          :precision="1"
          controls-position="right"
          @change="applyFilterChange"
        />
        <span class="filter-label">bps</span>
        <el-switch
          v-model="borrowDropFilter"
          inline-prompt
          active-text="额度压力"
          inactive-text="额度压力"
          @change="applyFilterChange"
        />
        <span class="filter-label">分≥</span>
        <el-input-number
          v-model="minBorrowPressureScore"
          size="small"
          :min="0"
          :max="100"
          :step="1"
          :precision="0"
          controls-position="right"
          @change="applyFilterChange"
        />
        <span class="filter-label">回撤≥</span>
        <el-input-number
          v-model="minCapacityDrawdownPct"
          size="small"
          :min="0"
          :max="100"
          :step="0.5"
          :precision="1"
          controls-position="right"
          @change="applyFilterChange"
        />
        <span class="filter-label">%</span>
        <span class="filter-label">下降≥</span>
        <el-input-number
          v-model="minCapacityDropUsdt"
          size="small"
          :min="0"
          :max="10000"
          :step="1"
          :precision="1"
          controls-position="right"
          @change="applyFilterChange"
        />
        <span class="filter-label">U</span>
        <el-switch
          v-model="historyHighNegativeFilter"
          inline-prompt
          active-text="历史高负"
          inactive-text="历史高负"
          @change="applyFilterChange"
        />
        <el-switch
          v-model="borrowableFilter"
          inline-prompt
          active-text="当前可借"
          inactive-text="当前可借"
          @change="applyFilterChange"
        />
        <span class="filter-label">≥</span>
        <el-input-number
          v-model="minBorrowCapacityUsdt"
          size="small"
          :min="0"
          :step="10"
          :precision="0"
          controls-position="right"
          @change="applyFilterChange"
        />
        <el-switch
          v-model="borrowCostFilter"
          inline-prompt
          active-text="成本"
          inactive-text="成本"
          @change="applyFilterChange"
        />
        <span class="filter-label">≤预期×</span>
        <el-input-number
          v-model="maxBorrowCostRatio"
          size="small"
          :min="0"
          :max="10"
          :step="0.1"
          :precision="1"
          controls-position="right"
          @change="applyFilterChange"
        />
      </div>
      <div v-if="filterSteps.length" class="filter-steps">
        <span
          v-for="step in filterSteps"
          :key="step.key"
          class="filter-step"
          :class="{ disabled: !step.enabled && step.key !== 'all' }"
        >
          {{ step.label }} {{ step.count }}
        </span>
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

.rule-row {
  margin-top: 10px;
  gap: 8px;
}

.rule-row :deep(.el-input-number) {
  width: 92px;
}

.filter-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.filter-step {
  border: 1px solid var(--app-border);
  border-radius: 4px;
  color: var(--app-text);
  font-size: 12px;
  line-height: 24px;
  padding: 0 8px;
}

.filter-step.disabled {
  color: var(--app-text-muted);
  opacity: 0.65;
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
