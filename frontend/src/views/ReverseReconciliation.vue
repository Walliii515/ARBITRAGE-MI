<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent, ValueFormatterParams } from 'ag-grid-community'
import { ElPopover } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface ReconciliationRow {
  position_id: number
  base_asset: string
  contract: string
  status: string | null
  local_borrow_qty: number
  exchange_borrowed_qty: number
  exchange_interest_qty: number
  local_future_qty: number
  exchange_future_size: number
  exchange_margin_free: number
  exchange_margin_net_asset: number
  is_match: boolean
}

interface ReconciliationSummary {
  local_holding: number
  mismatch_count: number
  match_count: number
  marginLevel: number
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_reconciliation'
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

const gridApi = shallowRef<GridApi<ReconciliationRow> | null>(null)
const rowData = shallowRef<ReconciliationRow[]>([])
const loading = ref(false)
const days = ref(365)
const timestamp = ref<number | null>(null)
const errors = ref<Record<string, string>>({})
const summary = ref<ReconciliationSummary>({
  local_holding: 0,
  mismatch_count: 0,
  match_count: 0,
  marginLevel: 0,
})
const columnVisibilities = ref<ColumnVisibility[]>([])

const updatedAt = computed(() => {
  if (!timestamp.value) return '-'
  const d = new Date(timestamp.value * 1000)
  if (Number.isNaN(d.getTime())) return '-'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})

function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  const n = Number(value)
  return Number.isInteger(n) ? String(n) : n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function decimalFormatter(params: ValueFormatterParams) {
  return formatDecimal(params.value as number | null)
}

function statusRenderer(params: { value?: boolean }) {
  const span = document.createElement('span')
  span.textContent = params.value ? '一致' : '异常'
  span.className = params.value ? 'match-ok' : 'match-bad'
  return span
}

async function fetchRows() {
  loading.value = true
  try {
    const query = new URLSearchParams({ days: String(days.value) })
    const res = await get(`/api/trading/reverse-reconciliation?${query.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '反向持仓对账加载失败')
      return
    }
    rowData.value = Array.isArray(data.rows) ? data.rows : []
    summary.value = {
      local_holding: Number(data.summary?.local_holding ?? 0),
      mismatch_count: Number(data.summary?.mismatch_count ?? 0),
      match_count: Number(data.summary?.match_count ?? 0),
      marginLevel: Number(data.summary?.marginLevel ?? 0),
    }
    timestamp.value = Number(data.timestamp ?? 0) || null
    errors.value = data.errors ?? {}
  } catch {
    showError('反向持仓对账加载失败')
  } finally {
    loading.value = false
  }
}

function refreshColumnVisibilities() {
  if (!gridApi.value) return
  const states = gridApi.value.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field || col.colId)
    .map((col) => {
      const colId = (col.field ?? col.colId) as string
      const state = states.find((item) => item.colId === colId)
      return { colId, headerName: col.headerName ?? colId, visible: state?.hide !== true }
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
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, { columnState: gridApi.value.getColumnState() })
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
    if (Array.isArray(data?.columnState)) gridApi.value.applyColumnState({ state: data.columnState, applyOrder: true })
  } catch {
    /* ignore */
  }
}

const columnDefs = ref<ColDef<ReconciliationRow>[]>([
  { headerName: '状态', field: 'is_match', width: 90, pinned: 'left', cellRenderer: statusRenderer },
  { headerName: '标的资产', field: 'base_asset', width: 95, pinned: 'left' },
  { headerName: '合约', field: 'contract', width: 120 },
  { headerName: '持仓ID', field: 'position_id', width: 95 },
  { headerName: '本地借币数量', field: 'local_borrow_qty', width: 135, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '交易所已借', field: 'exchange_borrowed_qty', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '交易所利息', field: 'exchange_interest_qty', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '本地合约数量', field: 'local_future_qty', width: 135, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: 'Gate持仓Size', field: 'exchange_future_size', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '杠杆可用', field: 'exchange_margin_free', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '杠杆净资产', field: 'exchange_margin_net_asset', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function onGridReady(event: GridReadyEvent<ReconciliationRow>) {
  gridApi.value = event.api
  setupGridCopy(event.api)
  loadColumnState()
}

onMounted(fetchRows)
</script>

<template>
  <div class="reverse-reconciliation-page">
    <div class="summary-bar">
      <span class="summary-item"><span class="summary-label">最后更新</span><span class="summary-value">{{ updatedAt }}</span></span>
      <span class="summary-item"><span class="summary-label">本地持仓</span><span class="summary-value">{{ summary.local_holding }}</span></span>
      <span class="summary-item"><span class="summary-label">一致</span><span class="summary-value summary-ok">{{ summary.match_count }}</span></span>
      <span class="summary-item"><span class="summary-label">异常</span><span class="summary-value summary-bad">{{ summary.mismatch_count }}</span></span>
      <span class="summary-item"><span class="summary-label">Binance风险率</span><span class="summary-value">{{ formatDecimal(summary.marginLevel, 4) }}</span></span>
    </div>

    <div v-if="Object.keys(errors).length" class="error-strip">
      <span v-for="(message, key) in errors" :key="key">{{ key }}: {{ message }}</span>
    </div>

    <div class="page-toolbar">
      <el-select v-model="days" size="small" style="width: 110px" @change="fetchRows">
        <el-option :value="7" label="最近7天" />
        <el-option :value="30" label="最近30天" />
        <el-option :value="90" label="最近90天" />
        <el-option :value="365" label="最近1年" />
      </el-select>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchRows">刷新</el-button>
      <el-popover placement="bottom-end" :width="260" trigger="click" @before-enter="refreshColumnVisibilities">
        <template #reference>
          <el-button size="small">列选择</el-button>
        </template>
        <div class="column-picker">
          <div v-for="col in columnVisibilities" :key="col.colId" class="column-picker-item">
            <el-checkbox :model-value="col.visible" @change="(val: boolean | string | number) => toggleColumnVisibility(col.colId, !!val)" />
            <span>{{ col.headerName }}</span>
          </div>
        </div>
      </el-popover>
      <el-button size="small" @click="saveColumnState">保存列配置</el-button>
    </div>

    <div ref="gridContainerRef" class="grid-container">
      <AgGridVue
        :theme="orderbookGridTheme"
        :rowData="rowData"
        :columnDefs="columnDefs"
        :defaultColDef="defaultColDef"
        :getRowId="(params: any) => String(params.data.position_id)"
        :header-height="32"
        :row-height="32"
        @grid-ready="onGridReady"
        style="width: 100%; height: 100%"
      />
    </div>
  </div>
</template>

<style scoped>
.reverse-reconciliation-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
}

.summary-bar,
.summary-item,
.page-toolbar {
  display: flex;
  align-items: center;
}

.summary-bar {
  gap: 22px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  background: var(--app-surface);
}

.summary-item {
  gap: 4px;
}

.summary-label {
  color: var(--app-text-muted);
  font-size: 12px;
}

.summary-value {
  color: var(--app-text);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.summary-ok {
  color: #67c23a;
}

.summary-bad {
  color: #f56c6c;
}

.error-strip {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid rgba(245, 108, 108, 0.45);
  border-radius: 4px;
  color: #f56c6c;
  background: rgba(245, 108, 108, 0.08);
  font-size: 13px;
}

.page-toolbar {
  gap: 10px;
  flex-wrap: wrap;
}

.grid-container {
  min-height: 0;
  flex: 1;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  overflow: hidden;
}

.column-picker {
  max-height: 360px;
  overflow: auto;
}

.column-picker-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

:deep(.match-ok) {
  color: #67c23a;
  font-weight: 600;
}

:deep(.match-bad) {
  color: #f56c6c;
  font-weight: 600;
}
</style>
