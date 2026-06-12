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

interface ReverseOrderRow {
  id: number
  created_at: string | null
  order_uuid: string | null
  position_id: number | null
  signal_id: number | null
  base_asset: string
  order_side: string | null
  market_type: string | null
  trade_direction: string | null
  status: string | null
  target_qty: number | null
  target_amount: number | null
  exec_price: number | null
  exec_qty: number | null
  exec_amount: number | null
  fee_amount_usdt: number | null
  reduce_only: number | boolean | null
  execution_style: string | null
  exchange_order_id: string | null
  reject_reason: string | null
}

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const PAGE_KEY = 'reverse_order_management'
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

const gridApi = shallowRef<GridApi<ReverseOrderRow> | null>(null)
const rowData = shallowRef<ReverseOrderRow[]>([])
const loading = ref(false)
const baseAsset = ref('')
const status = ref('')
const orderSide = ref('')
const marketType = ref('')
const days = ref(30)
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

function statusLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    pending: '待执行',
    filled: '已成交',
    partial: '部分成交',
    failed: '失败',
    cancelled: '已取消',
    skipped: '跳过',
  }
  return value ? (map[value] || value) : ''
}

function sideLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    open: '开仓',
    close: '平仓',
    repay: '还币',
    unwind: '解腿',
  }
  return value ? (map[value] || value) : ''
}

function marketLabel(value: string | null | undefined): string {
  const map: Record<string, string> = {
    margin_spot: '杠杆现货',
    future: '合约',
    margin_repay: '杠杆还币',
  }
  return value ? (map[value] || value) : ''
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
    if (orderSide.value) query.set('order_side', orderSide.value)
    if (marketType.value) query.set('market_type', marketType.value)
    const res = await get(`/api/trading/reverse-orders?${query.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '反向订单加载失败')
      return
    }
    rowData.value = Array.isArray(data.orders) ? data.orders : []
    paginationTotal.value = Number(data.pagination?.total ?? rowData.value.length)
  } catch {
    showError('反向订单加载失败')
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

const columnDefs = ref<ColDef<ReverseOrderRow>[]>([
  { headerName: '时间', field: 'created_at', width: 160, sort: 'desc' },
  { headerName: '标的资产', field: 'base_asset', width: 95, pinned: 'left' },
  { headerName: '方向', field: 'order_side', width: 90, valueFormatter: (p) => sideLabel(p.value) },
  { headerName: '市场', field: 'market_type', width: 115, valueFormatter: (p) => marketLabel(p.value) },
  { headerName: '交易动作', field: 'trade_direction', width: 95 },
  { headerName: '状态', field: 'status', width: 95, valueFormatter: (p) => statusLabel(p.value) },
  { headerName: '目标数量', field: 'target_qty', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '目标金额', field: 'target_amount', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: amountFormatter },
  { headerName: '成交价', field: 'exec_price', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '成交数量', field: 'exec_qty', width: 125, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: decimalFormatter },
  { headerName: '成交金额', field: 'exec_amount', width: 115, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: amountFormatter },
  { headerName: '手续费USDT', field: 'fee_amount_usdt', width: 120, type: 'numericColumn', cellClass: 'ag-right-aligned-cell', valueFormatter: amountFormatter },
  { headerName: '只减仓', field: 'reduce_only', width: 90, valueFormatter: (p) => (p.value ? '是' : '') },
  { headerName: '执行方式', field: 'execution_style', width: 110 },
  { headerName: '持仓ID', field: 'position_id', width: 95 },
  { headerName: '信号ID', field: 'signal_id', width: 95 },
  { headerName: '交易所订单ID', field: 'exchange_order_id', width: 150 },
  { headerName: '订单UUID', field: 'order_uuid', width: 170 },
  { headerName: '拒绝原因', field: 'reject_reason', width: 260, tooltipField: 'reject_reason' },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function onGridReady(event: GridReadyEvent<ReverseOrderRow>) {
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
      <el-select v-model="orderSide" size="small" placeholder="方向" clearable style="width: 105px" @change="fetchRows(true)">
        <el-option value="open" label="开仓" />
        <el-option value="close" label="平仓" />
        <el-option value="repay" label="还币" />
        <el-option value="unwind" label="解腿" />
      </el-select>
      <el-select v-model="status" size="small" placeholder="状态" clearable style="width: 115px" @change="fetchRows(true)">
        <el-option value="pending" label="待执行" />
        <el-option value="filled" label="已成交" />
        <el-option value="partial" label="部分成交" />
        <el-option value="failed" label="失败" />
        <el-option value="cancelled" label="已取消" />
        <el-option value="skipped" label="跳过" />
      </el-select>
      <el-select v-model="marketType" size="small" placeholder="市场" clearable style="width: 125px" @change="fetchRows(true)">
        <el-option value="margin_spot" label="杠杆现货" />
        <el-option value="future" label="合约" />
        <el-option value="margin_repay" label="杠杆还币" />
      </el-select>
      <el-select v-model="days" size="small" style="width: 110px" @change="fetchRows(true)">
        <el-option :value="1" label="最近1天" />
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
