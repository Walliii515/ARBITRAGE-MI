<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
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
}

const rowData = shallowRef<ReconRow[]>([])
const loading = ref(false)
const running = ref(false)
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

function diffStyle(params: any) {
  const value = Number(params.value || 0)
  if (!Number.isFinite(value) || Math.abs(value) === 0) return null
  return { color: '#f56c6c', fontWeight: '600' }
}

const columnDefs = computed<ColDef<ReconRow>[]>(() => [
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

const defaultColDef: ColDef<ReconRow> = {
  sortable: true,
  resizable: true,
  filter: true,
}

const totalPages = computed(() =>
  Math.ceil(paginationTotal.value / paginationPageSize.value) || 1
)

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

async function runReconciliation() {
  running.value = true
  try {
    const res = await post('/api/trading/reconciliation/run')
    const data = await res.json()
    if (data.success) {
      showSuccess(data.message || '对账完成')
      paginationCurrentPage.value = 1
      await fetchRows()
    } else {
      showError(data.message || '对账失败')
    }
  } catch (e: any) {
    showError(e?.message || '对账请求失败')
  } finally {
    running.value = false
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

function onRowDoubleClicked(params: any) {
  detailRow.value = params.data || null
  detailDialogVisible.value = true
}

onMounted(fetchRows)
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

      <div class="filter-group">
        <span class="filter-label">时间：</span>
        <el-button-group size="small">
          <el-button :type="filterDays === 1 ? 'primary' : 'default'" @click="filterDays = 1; resetAndFetch()">24小时</el-button>
          <el-button :type="filterDays === 3 ? 'primary' : 'default'" @click="filterDays = 3; resetAndFetch()">3天</el-button>
          <el-button :type="filterDays === 7 ? 'primary' : 'default'" @click="filterDays = 7; resetAndFetch()">7天</el-button>
          <el-button :type="filterDays === 30 ? 'primary' : 'default'" @click="filterDays = 30; resetAndFetch()">30天</el-button>
        </el-button-group>
      </div>

      <el-switch
        v-model="mismatchesOnly"
        active-text="仅显示差异"
        inactive-text="显示全部"
        @change="resetAndFetch"
      />

      <el-button size="small" :loading="loading" @click="fetchRows">刷新</el-button>
    </div>

    <div class="grid-container">
      <AgGridVue
        :theme="orderbookGridTheme"
        :rowData="rowData"
        :columnDefs="columnDefs"
        :defaultColDef="defaultColDef"
        :getRowId="(params: any) => String(params.data.id)"
        :tooltipShowDelay="300"
        style="width: 100%; height: 100%"
        @grid-ready="onGridReady"
        @row-double-clicked="onRowDoubleClicked"
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
