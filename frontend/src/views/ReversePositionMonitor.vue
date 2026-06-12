<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent, ValueFormatterParams } from 'ag-grid-community'
import { ElPopover } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess } from '../utils/message'

interface ReversePositionRow {
  id: number
  order_uuid: string | null
  signal_id: number | null
  base_asset: string
  spot_symbol: string | null
  future_contract: string | null
  status: string | null
  opened_at: string | null
  closed_at: string | null
  open_amount_usdt: number | null
  close_amount_usdt: number | null
  borrow_asset: string | null
  borrow_qty: number | null
  borrow_repaid_qty: number | null
  borrow_hourly_rate: number | null
  borrow_interest_usdt: number | null
  borrow_interest_bps: number | null
  spot_open_qty: number | null
  spot_open_price: number | null
  future_open_qty: number | null
  future_open_price: number | null
  reverse_open_basis_bps: number | null
  reverse_close_basis_bps: number | null
  funding_pnl_usdt: number | null
  funding_pnl_bps: number | null
  fee_total_usdt: number | null
  fee_total_bps: number | null
  realized_pnl_usdt: number | null
  realized_pnl_bps: number | null
  exchange_risk_status: string | null
  exchange_risk_type: string | null
  exchange_risk_at: string | null
  close_reason: string | null
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_position_monitor'
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

const gridApi = shallowRef<GridApi<ReversePositionRow> | null>(null)
const rowData = shallowRef<ReversePositionRow[]>([])
const loading = ref(false)
const baseAsset = ref('')
const status = ref('')
const days = ref(365)
const paginationPageSize = ref(100)
const paginationPageSizeOptions = [50, 100, 500, 1000, 5000]
const paginationCurrentPage = ref(1)
const paginationTotal = ref(0)
const columnVisibilities = ref<ColumnVisibility[]>([])

const totalPages = computed(() => Math.ceil(paginationTotal.value / paginationPageSize.value) || 1)

function formatDecimal(value: number | null | undefined, maxDecimals = 12): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  const n = Number(value)
  return Number.isInteger(n) ? String(n) : n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

function amountFormatter(params: ValueFormatterParams) {
  if (params.value == null || !Number.isFinite(Number(params.value))) return ''
  return Number(params.value).toLocaleString('en-US', { maximumFractionDigits: 4 })
}

function decimalFormatter(params: ValueFormatterParams) {
  return formatDecimal(params.value as number | null)
}

function bpsFormatter(params: ValueFormatterParams) {
  return params.value == null || !Number.isFinite(Number(params.value)) ? '' : `${Number(params.value).toFixed(2)} bps`
}

function rateFormatter(params: ValueFormatterParams) {
  return params.value == null || !Number.isFinite(Number(params.value)) ? '' : `${(Number(params.value) * 100).toFixed(6)}%`
}

function statusLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    holding: '持仓中',
    closing: '平仓中',
    closed: '已平仓',
    risk: '风险中',
    desynced: '对账异常',
  }
  return value ? (map[value] || value) : ''
}

function riskLabel(row: ReversePositionRow | null | undefined): string {
  if (!row?.exchange_risk_status || row.exchange_risk_status === 'normal') return ''
  return row.exchange_risk_type ? `${row.exchange_risk_status}/${row.exchange_risk_type}` : row.exchange_risk_status
}

async function fetchRows(resetPage = false) {
  if (loading.value) return
  if (resetPage) paginationCurrentPage.value = 1
  loading.value = true
  try {
    const query = new URLSearchParams({
      days: String(days.value),
      page: String(paginationCurrentPage.value),
      page_size: String(paginationPageSize.value),
    })
    if (baseAsset.value.trim()) query.set('base_asset', baseAsset.value.trim())
    if (status.value) query.set('status', status.value)
    const res = await get(`/api/trading/reverse-positions?${query.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '反向持仓加载失败')
      return
    }
    rowData.value = Array.isArray(data.positions) ? data.positions : []
    paginationTotal.value = Number(data.pagination?.total ?? rowData.value.length)
  } catch {
    showError('反向持仓加载失败')
  } finally {
    loading.value = false
  }
}

function onPageChange(page: number | null) {
  paginationCurrentPage.value = Number(page || 1)
  fetchRows()
}

function onPaginationSizeChange() {
  paginationCurrentPage.value = 1
  fetchRows()
}

function refreshColumnVisibilities() {
  if (!gridApi.value) return
  const states = gridApi.value.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field)
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

const columnDefs = ref<ColDef<ReversePositionRow>[]>([
  { headerName: '开仓时间', field: 'opened_at', width: 160, sort: 'desc' },
  { headerName: '标的资产', field: 'base_asset', width: 95, pinned: 'left' },
  { headerName: '状态', field: 'status', width: 95, valueFormatter: (p) => statusLabel(p.value) },
  { headerName: '开仓金额', field: 'open_amount_usdt', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: amountFormatter },
  { headerName: '平仓金额', field: 'close_amount_usdt', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: amountFormatter },
  { headerName: '借币资产', field: 'borrow_asset', width: 95 },
  { headerName: '借币数量', field: 'borrow_qty', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '已还数量', field: 'borrow_repaid_qty', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '借币小时利率', field: 'borrow_hourly_rate', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: rateFormatter },
  { headerName: '借币利息USDT', field: 'borrow_interest_usdt', width: 130, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: amountFormatter },
  { headerName: '开仓基差', field: 'reverse_open_basis_bps', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: bpsFormatter },
  { headerName: '平仓基差', field: 'reverse_close_basis_bps', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: bpsFormatter },
  { headerName: 'Funding收益', field: 'funding_pnl_bps', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: bpsFormatter },
  { headerName: '手续费', field: 'fee_total_bps', width: 105, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: bpsFormatter },
  { headerName: '实现盈亏', field: 'realized_pnl_bps', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: bpsFormatter },
  { headerName: '现货开仓价', field: 'spot_open_price', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '合约开仓价', field: 'future_open_price', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '合约数量', field: 'future_open_qty', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '交易所风险', colId: 'exchange_risk', width: 125, valueGetter: (p) => riskLabel(p.data) },
  { headerName: '风险时间', field: 'exchange_risk_at', width: 160 },
  { headerName: '平仓时间', field: 'closed_at', width: 160 },
  { headerName: '平仓原因', field: 'close_reason', width: 260, tooltipField: 'close_reason' },
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

function onGridReady(event: GridReadyEvent<ReversePositionRow>) {
  gridApi.value = event.api
  setupGridCopy(event.api)
  loadColumnState()
}

onMounted(() => {
  fetchRows()
})
</script>

<template>
  <div class="reverse-page">
    <div class="page-toolbar">
      <el-input
        v-model="baseAsset"
        :prefix-icon="Search"
        placeholder="标的资产"
        size="small"
        clearable
        style="width: 150px"
        @change="fetchRows(true)"
        @clear="fetchRows(true)"
      />
      <el-select v-model="status" size="small" placeholder="状态" clearable style="width: 115px" @change="fetchRows(true)">
        <el-option value="holding" label="持仓中" />
        <el-option value="closing" label="平仓中" />
        <el-option value="closed" label="已平仓" />
        <el-option value="risk" label="风险中" />
        <el-option value="desynced" label="对账异常" />
      </el-select>
      <el-select v-model="days" size="small" style="width: 110px" @change="fetchRows(true)">
        <el-option :value="7" label="最近7天" />
        <el-option :value="30" label="最近30天" />
        <el-option :value="90" label="最近90天" />
        <el-option :value="365" label="最近1年" />
      </el-select>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchRows()">刷新</el-button>
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
.reverse-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
}

.page-toolbar {
  display: flex;
  align-items: center;
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
</style>
