<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { Coin } from '@element-plus/icons-vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridReadyEvent, ICellRendererParams } from 'ag-grid-community'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface ReconRow {
  id: number
  snapshot_at: string
  exchange: 'binance' | 'gate' | string
  base_asset: string
  dimension: string
  local_value: number | null
  exchange_value: number | null
  diff_value: number | null
  diff_ratio: number | null
  is_match: boolean | number
  detail: Record<string, unknown> | string | null
  quanto_multiplier?: number | null
}

interface ExposureRow {
  base_asset: string
  snapshot_at: string
  binance_exchange_value: number | null
  gate_exchange_contracts: number | null
  gate_exchange_value: number | null
  gate_quanto_multiplier: number | null
  exchange_diff: number | null
  exposure_side: 'balanced' | 'spot_long' | 'gate_short' | 'missing_leg'
  binance_local_value: number | null
  gate_local_contracts: number | null
  gate_local_value: number | null
  local_diff: number | null
  binance_match: boolean
  gate_match: boolean
}

const rowData = shallowRef<ReconRow[]>([])
const latestRows = shallowRef<ReconRow[]>([])
const loading = ref(false)
const running = ref(false)
const dustCleaning = ref(false)
const activeTab = ref<'raw' | 'exposure'>('raw')
const filterDays = ref(1)
const mismatchesOnly = ref(false)
const paginationPageSize = ref(50)
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)
const detailDialogVisible = ref(false)
const detailRow = ref<ReconRow | null>(null)

const { setupGridCopy } = useGridCopy()

function formatNumber(value: number | null | undefined, maxDecimals = 10): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  const n = Number(value)
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function toNumber(value: number | null | undefined): number | null {
  if (value == null) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function positiveNumberOrDefault(value: number | null | undefined, fallback = 1): number {
  const n = toNumber(value)
  return n != null && n > 0 ? n : fallback
}

function isRowMatch(value: ReconRow['is_match']): boolean {
  return value === true || value === 1
}

function formatDetail(value: ReconRow['detail']): string {
  if (!value) return ''
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

function exchangeRenderer(params: ICellRendererParams<ReconRow>) {
  const value = String(params.value || '')
  const color = value === 'binance' ? '#f0b90b' : value === 'gate' ? '#409eff' : '#909399'
  return `<span style="color:${color};font-weight:600">${value}</span>`
}

function matchRenderer(params: ICellRendererParams<ReconRow>) {
  const matched = params.value === true || params.value === 1
  return matched
    ? '<span style="color:#67c23a;font-weight:600">一致</span>'
    : '<span style="color:#f56c6c;font-weight:600">差异</span>'
}

function exposureRenderer(params: ICellRendererParams<ExposureRow>) {
  const value = params.value as ExposureRow['exposure_side']
  if (value === 'balanced') return '<span style="color:#67c23a;font-weight:600">平衡</span>'
  if (value === 'spot_long') return '<span style="color:#e6a23c;font-weight:600">Binance现货多余</span>'
  if (value === 'gate_short') return '<span style="color:#f56c6c;font-weight:600">Gate空头多余</span>'
  return '<span style="color:#f56c6c;font-weight:600">缺腿</span>'
}

function diffStyle(params: any) {
  const value = Number(params.value || 0)
  if (!Number.isFinite(value) || Math.abs(value) === 0) return null
  return { color: '#f56c6c', fontWeight: '600' }
}

const rawColumnDefs = computed<ColDef<ReconRow>[]>(() => [
  { headerName: '对账时间', field: 'snapshot_at', width: 165, sort: 'desc' },
  { headerName: '交易所', field: 'exchange', width: 100, cellRenderer: exchangeRenderer },
  { headerName: '标的', field: 'base_asset', width: 105, pinned: 'left' },
  { headerName: '维度', field: 'dimension', width: 100 },
  {
    headerName: '本地值',
    field: 'local_value',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: '交易所值',
    field: 'exchange_value',
    width: 140,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: '差异',
    field: 'diff_value',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
    cellStyle: diffStyle,
  },
  {
    headerName: '差异占比',
    field: 'diff_ratio',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => p.value == null ? '' : `${(Number(p.value) * 100).toFixed(4)}%`,
  },
  { headerName: '是否一致', field: 'is_match', width: 100, cellRenderer: matchRenderer },
  {
    headerName: '详情',
    field: 'detail',
    minWidth: 220,
    flex: 1,
    valueFormatter: (p) => formatDetail(p.value).replace(/\s+/g, ' ').slice(0, 180),
    tooltipValueGetter: (p) => formatDetail(p.value),
    tooltipComponent: LongTextTooltip,
  },
])

const exposureColumnDefs = computed<ColDef<ExposureRow>[]>(() => [
  { headerName: '标的', field: 'base_asset', width: 110, pinned: 'left', sort: 'asc' },
  { headerName: '对账时间', field: 'snapshot_at', width: 165 },
  { headerName: '敞口状态', field: 'exposure_side', width: 130, cellRenderer: exposureRenderer },
  {
    headerName: 'Binance实仓',
    field: 'binance_exchange_value',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: 'Gate实仓(标的)',
    field: 'gate_exchange_value',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: 'Gate张数',
    field: 'gate_exchange_contracts',
    width: 110,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: 'Gate乘数',
    field: 'gate_quanto_multiplier',
    width: 105,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: '敞口(Binance-Gate)',
    field: 'exchange_diff',
    width: 170,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
    cellStyle: diffStyle,
  },
  {
    headerName: 'Binance本地',
    field: 'binance_local_value',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: 'Gate本地(标的)',
    field: 'gate_local_value',
    width: 135,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: 'Gate本地张数',
    field: 'gate_local_contracts',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
  },
  {
    headerName: '本地差(Binance-Gate)',
    field: 'local_diff',
    width: 170,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => formatNumber(p.value),
    cellStyle: diffStyle,
  },
  { headerName: 'Binance对账', field: 'binance_match', width: 115, cellRenderer: matchRenderer },
  { headerName: 'Gate对账', field: 'gate_match', width: 105, cellRenderer: matchRenderer },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
}

const totalPages = computed(() =>
  Math.ceil(paginationTotal.value / paginationPageSize.value) || 1
)

const exposureRows = computed<ExposureRow[]>(() => {
  const grouped = new Map<string, { binance?: ReconRow; gate?: ReconRow; snapshot_at: string }>()
  for (const row of latestRows.value) {
    if (row.dimension !== 'position') continue
    const exchange = String(row.exchange || '').toLowerCase()
    if (exchange !== 'binance' && exchange !== 'gate') continue
    const asset = String(row.base_asset || '').toUpperCase()
    if (!asset) continue
    const item = grouped.get(asset) || { snapshot_at: row.snapshot_at }
    item.snapshot_at = item.snapshot_at || row.snapshot_at
    if (exchange === 'binance') item.binance = row
    if (exchange === 'gate') item.gate = row
    grouped.set(asset, item)
  }

  return Array.from(grouped.entries()).map(([baseAsset, item]) => {
    const binanceExchange = toNumber(item.binance?.exchange_value)
    const gateExchangeContracts = toNumber(item.gate?.exchange_value)
    const gateMultiplier = positiveNumberOrDefault(item.gate?.quanto_multiplier)
    const gateExchange = gateExchangeContracts != null
      ? gateExchangeContracts * gateMultiplier
      : null
    const binanceLocal = toNumber(item.binance?.local_value)
    const gateLocalContracts = toNumber(item.gate?.local_value)
    const gateLocal = gateLocalContracts != null
      ? gateLocalContracts * gateMultiplier
      : null
    const exchangeDiff = binanceExchange != null && gateExchange != null
      ? binanceExchange - gateExchange
      : null
    const localDiff = binanceLocal != null && gateLocal != null
      ? binanceLocal - gateLocal
      : null
    const tolerance = 1e-8
    let exposureSide: ExposureRow['exposure_side'] = 'balanced'
    if (binanceExchange == null || gateExchange == null) {
      exposureSide = 'missing_leg'
    } else if (exchangeDiff != null && exchangeDiff > tolerance) {
      exposureSide = 'spot_long'
    } else if (exchangeDiff != null && exchangeDiff < -tolerance) {
      exposureSide = 'gate_short'
    }
    return {
      base_asset: baseAsset,
      snapshot_at: item.snapshot_at,
      binance_exchange_value: binanceExchange,
      gate_exchange_contracts: gateExchangeContracts,
      gate_exchange_value: gateExchange,
      gate_quanto_multiplier: item.gate ? gateMultiplier : null,
      exchange_diff: exchangeDiff,
      exposure_side: exposureSide,
      binance_local_value: binanceLocal,
      gate_local_contracts: gateLocalContracts,
      gate_local_value: gateLocal,
      local_diff: localDiff,
      binance_match: item.binance ? isRowMatch(item.binance.is_match) : false,
      gate_match: item.gate ? isRowMatch(item.gate.is_match) : false,
    }
  })
})

const exposureSummary = computed(() => {
  const rows = exposureRows.value
  const exposed = rows.filter((row) => row.exposure_side !== 'balanced').length
  return {
    total: rows.length,
    exposed,
    balanced: rows.length - exposed,
  }
})

async function fetchRows() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    params.set('mismatches_only', String(mismatchesOnly.value))
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    const res = await get(`/api/trading/reconciliation/history?${params.toString()}`)
    const data = await res.json()
    rowData.value = data.rows || []
    paginationTotal.value = data.pagination?.total || 0
  } catch (e: any) {
    showError(e?.message || '获取对账数据失败')
  } finally {
    loading.value = false
  }
}

async function fetchLatestRows() {
  const res = await get('/api/trading/reconciliation/latest')
  const data = await res.json()
  latestRows.value = Array.isArray(data.rows) ? data.rows : []
}

async function refreshCurrentTab() {
  if (activeTab.value === 'exposure') {
    loading.value = true
    try {
      await fetchLatestRows()
    } catch (e: any) {
      showError(e?.message || '获取敞口对照数据失败')
    } finally {
      loading.value = false
    }
    return
  }
  await fetchRows()
}

async function runReconciliation() {
  running.value = true
  try {
    const res = await post('/api/trading/reconciliation/run')
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || '对账完成')
      paginationCurrentPage.value = 1
      await fetchRows()
      await fetchLatestRows()
    } else {
      showError(data.message || '对账失败')
    }
  } catch (e: any) {
    showError(e?.message || '对账请求失败')
  } finally {
    running.value = false
  }
}

async function cleanupDust() {
  dustCleaning.value = true
  try {
    const res = await post('/api/trading/reconciliation/dust/cleanup')
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || '小额残余清理完成')
      paginationCurrentPage.value = 1
      await fetchRows()
      await fetchLatestRows()
    } else {
      showError(data.message || '小额残余清理失败')
    }
  } catch (e: any) {
    showError(e?.message || '小额残余清理请求失败')
  } finally {
    dustCleaning.value = false
  }
}

function resetAndFetch() {
  paginationCurrentPage.value = 1
  fetchRows()
}

function onPageChange(page: number | null) {
  paginationCurrentPage.value = Number(page || 1)
  fetchRows()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchRows()
}

function onGridReady(params: GridReadyEvent<ReconRow>) {
  setupGridCopy(params.api)
}

function onExposureGridReady(params: GridReadyEvent<ExposureRow>) {
  setupGridCopy(params.api)
}

function onRowDoubleClicked(params: any) {
  detailRow.value = params.data || null
  detailDialogVisible.value = true
}

onMounted(async () => {
  await Promise.all([
    fetchRows(),
    fetchLatestRows().catch((e: any) => {
      showError(e?.message || '获取敞口对照数据失败')
    }),
  ])
})
</script>

<template>
  <div class="recon-page">
    <div class="toolbar">
      <el-button
        size="small"
        type="primary"
        :loading="running"
        @click="runReconciliation"
      >
        立即对账
      </el-button>

      <el-popconfirm
        width="360"
        title="仅清理订单账本和两家交易所都能完整解释、且低于最小成交额的平仓残余。确认执行？"
        confirm-button-text="确认清理"
        cancel-button-text="取消"
        @confirm="cleanupDust"
      >
        <template #reference>
          <el-button
            size="small"
            type="warning"
            plain
            :icon="Coin"
            :loading="dustCleaning"
          >
            小额兑换
          </el-button>
        </template>
      </el-popconfirm>

      <div v-if="activeTab === 'raw'" class="filter-group">
        <span class="filter-label">时间：</span>
        <el-button-group size="small">
          <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="filterDays = 1; resetAndFetch()">24小时</el-button>
          <el-button :type="filterDays === 3 ? 'primary' : 'default'" @click="filterDays = 3; resetAndFetch()">3天</el-button>
          <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="filterDays = 7; resetAndFetch()">7天</el-button>
          <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="filterDays = 30; resetAndFetch()">30天</el-button>
        </el-button-group>
      </div>

      <el-switch
        v-if="activeTab === 'raw'"
        v-model="mismatchesOnly"
        active-text="仅显示差异"
        inactive-text="显示全部"
        @change="resetAndFetch"
      />

      <el-button size="small" :loading="loading" @click="refreshCurrentTab">刷新</el-button>
    </div>

    <el-tabs v-model="activeTab" class="recon-tabs">
      <el-tab-pane label="明细" name="raw">
        <div class="grid-container">
          <AgGridVue
            :theme="orderbookGridTheme"
            :rowData="rowData"
            :columnDefs="rawColumnDefs"
            :defaultColDef="defaultColDef"
            :getRowId="(params: any) => String(params.data.id)"
            :tooltipShowDelay="300"
            style="width: 100%; height: 100%"
            @grid-ready="onGridReady"
            @row-double-clicked="onRowDoubleClicked"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane label="币种敞口" name="exposure">
        <div class="exposure-summary">
          <span>标的 {{ exposureSummary.total }}</span>
          <span class="summary-ok">平衡 {{ exposureSummary.balanced }}</span>
          <span class="summary-bad">有敞口 {{ exposureSummary.exposed }}</span>
        </div>
        <div class="grid-container">
          <AgGridVue
            :theme="orderbookGridTheme"
            :rowData="exposureRows"
            :columnDefs="exposureColumnDefs"
            :defaultColDef="defaultColDef"
            :getRowId="(params: any) => String(params.data.base_asset)"
            style="width: 100%; height: 100%"
            @grid-ready="onExposureGridReady"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <div v-if="activeTab === 'raw'" class="pagination-bar">
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

    <el-dialog
      v-model="detailDialogVisible"
      title="对账详情"
      width="720px"
    >
      <pre class="detail-json">{{ formatDetail(detailRow?.detail || null) }}</pre>
    </el-dialog>
  </div>
</template>

<style scoped>
.recon-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.filter-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-label {
  font-size: 13px;
  color: var(--el-text-color-secondary, #909399);
  white-space: nowrap;
}

.grid-container {
  flex: 1;
  min-height: 420px;
}

.recon-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.recon-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.recon-tabs :deep(.el-tab-pane) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.exposure-summary {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 0 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary, #909399);
}

.summary-ok {
  color: #67c23a;
  font-weight: 600;
}

.summary-bad {
  color: #f56c6c;
  font-weight: 600;
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

.detail-json {
  max-height: 520px;
  overflow: auto;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  background: var(--app-bg);
  color: var(--app-text);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
