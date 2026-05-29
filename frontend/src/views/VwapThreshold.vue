<script setup lang="ts">
import { ref, shallowRef, computed, onMounted } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  ColumnState,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
} from 'ag-grid-community'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { showError, showSuccess } from '../utils/message'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'

/* ───── 类型 ───── */
interface ThresholdRow {
  id: string
  base_asset: string
  calc_date: string
  open_sample_count: number | null
  open_basis_max: number | null
  open_basis_min: number | null
  open_basis_mean: number | null
  open_basis_std: number | null
  open_basis_p10: number | null
  open_basis_p20: number | null
  open_basis_p30: number | null
  open_basis_p40: number | null
  close_sample_count: number | null
  close_basis_max: number | null
  close_basis_min: number | null
  close_basis_mean: number | null
  close_basis_std: number | null
  close_basis_p10: number | null
  close_basis_p20: number | null
  close_basis_p30: number | null
  close_basis_p40: number | null
}

/* ───── 状态 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()
const rowData = shallowRef<ThresholdRow[]>([])
let gridApi: GridApi<ThresholdRow> | null = null
const loading = ref(false)
const calculating = ref(false)

/* 过滤条件 */
const selectedDate = ref<string>('')
const selectedAsset = ref<string>('')
const dateOptions = ref<string[]>([])
const assetOptions = ref<string[]>([])

/* 最新计算日期（BTC 基准） */
const latestCalcDate = ref<string>('—')

/** 列状态持久化（数据库版） */
const PAGE_KEY = 'vwap_threshold'

/* ───── 格式化 ───── */
const bpsFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return Number(params.value).toFixed(4)
}

const intFormatter = (params: ValueFormatterParams) => {
  if (params.value == null) return ''
  return String(params.value)
}

/* ───── 列定义 ───── */
const numericCol = (headerName: string, field: string, width: number, formatter = bpsFormatter): ColDef<ThresholdRow> => ({
  headerName,
  field: field as keyof ThresholdRow,
  width,
  type: 'numericColumn',
  cellClass: 'ag-right-aligned-cell',
  headerClass: 'ag-right-aligned-header',
  valueFormatter: formatter,
})

const columnDefs = computed<ColDef<ThresholdRow>[]>(() => [
  {
    headerName: '标的',
    field: 'base_asset',
    width: 100,
    pinned: 'left',
  },
  {
    headerName: '计算日期',
    field: 'calc_date',
    width: 120,
  },
  // ── 开仓统计 ──
  numericCol('开仓样本数', 'open_sample_count', 110, intFormatter),
  numericCol('开仓Max', 'open_basis_max', 110),
  numericCol('开仓Min', 'open_basis_min', 110),
  numericCol('开仓Mean', 'open_basis_mean', 110),
  numericCol('开仓Std', 'open_basis_std', 110),
  numericCol('开仓P10(top10%)', 'open_basis_p10', 145),
  numericCol('开仓P20(top20%)', 'open_basis_p20', 145),
  numericCol('开仓P30(top30%)', 'open_basis_p30', 145),
  numericCol('开仓P40(top40%)', 'open_basis_p40', 145),
  // ── 平仓统计 ──
  numericCol('平仓样本数', 'close_sample_count', 110, intFormatter),
  numericCol('平仓Max', 'close_basis_max', 110),
  numericCol('平仓Min', 'close_basis_min', 110),
  numericCol('平仓Mean', 'close_basis_mean', 110),
  numericCol('平仓Std', 'close_basis_std', 110),
  numericCol('平仓P10(bot10%)', 'close_basis_p10', 145),
  numericCol('平仓P20(bot20%)', 'close_basis_p20', 145),
  numericCol('平仓P30(bot30%)', 'close_basis_p30', 145),
  numericCol('平仓P40(bot40%)', 'close_basis_p40', 145),
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

const localeText = {
  generalMenuTab: '常规',
  filterMenuTab: '筛选',
  columnsMenuTab: '列',
  pinColumn: '固定列',
  autosizeThisColumn: '自适应列',
  autosizeAllColumns: '全部列自适应',
  resetColumns: '重置列',
}

const getRowId = (params: GetRowIdParams<ThresholdRow>) =>
  String(params.data?.id ?? params.data?.base_asset ?? '')

/* ───── 列选择面板 ───── */
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field)
    .map((col) => {
      const state = states.find((s) => s.colId === (col.field ?? col.colId))
      return {
        colId: (col.field ?? col.colId) as string,
        headerName: col.headerName ?? (col.field ?? ''),
        visible: state?.hide !== true,
      }
    })
}

function toggleColumnVisibility(colId: string, visible: boolean) {
  if (!gridApi) return
  gridApi.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) col.visible = visible
}

/** 保存列配置到数据库 */
async function saveColumnState() {
  if (!gridApi) return
  const columnState = gridApi.getColumnState()
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, { columnState })
    const data = await res.json()
    if (data?.success) {
      showSuccess('列配置已保存')
    } else {
      showError(data?.message || '保存列配置失败')
    }
  } catch (e) {
    showError('保存列配置失败')
  }
}

/** 从数据库加载列配置 */
async function loadColumnState() {
  if (!gridApi) return
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEY}`)
    const data = await res.json()
    if (data?.columnState && Array.isArray(data.columnState)) {
      gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch (e) {
    console.warn('Failed to load column config from server:', e)
  }
}

/* ───── 数据加载 ───── */
async function fetchLatestDate() {
  try {
    const res = await get('/api/trading/threshold/latest-date')
    if (!res.ok) return
    const data = await res.json()
    latestCalcDate.value = data.latest_date || '—'
  } catch {
    // ignore
  }
}

async function fetchDates() {
  try {
    const res = await get('/api/trading/threshold/dates')
    if (!res.ok) return
    const dates: string[] = await res.json()
    dateOptions.value = dates
    // 默认选最新日期
    if (dates.length > 0 && !selectedDate.value) {
      selectedDate.value = dates[0]
    }
  } catch {
    // ignore
  }
}

async function fetchAssets() {
  try {
    const res = await get('/api/trading/threshold/assets')
    if (!res.ok) return
    assetOptions.value = await res.json()
  } catch {
    // ignore
  }
}

async function fetchData() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (selectedDate.value) params.set('calc_date', selectedDate.value)
    if (selectedAsset.value) params.set('base_asset', selectedAsset.value)

    const res = await get(`/api/trading/threshold/data?${params}`)
    if (!res.ok) {
      showError('获取阈值数据失败')
      return
    }
    const rows = await res.json()
    // 附加 id 字段供 AG Grid 使用
    rowData.value = rows.map((r: any, i: number) => ({ ...r, id: `${r.base_asset}_${r.calc_date}_${i}` }))
  } catch {
    showError('请求阈值数据失败')
  } finally {
    loading.value = false
  }
}

async function triggerCalculate() {
  calculating.value = true
  try {
    const res = await post('/api/trading/threshold/calculate')
    if (!res.ok) {
      showError('触发计算失败')
      return
    }
    const result = await res.json()
    if (result.success) {
      showSuccess(result.message || '计算完成')
      // 计算完成后刷新
      await fetchLatestDate()
      await fetchDates()
      await fetchAssets()
      await fetchData()
    } else {
      showError(result.message || '计算失败')
    }
  } catch {
    showError('请求失败')
  } finally {
    calculating.value = false
  }
}

/* ───── 查询按钮 ───── */
function onQuery() {
  fetchData()
}

/* ───── AG Grid 回调 ───── */
function onGridReady(params: GridReadyEvent<ThresholdRow>) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)
}

/* ───── 生命周期 ───── */
onMounted(async () => {
  await fetchLatestDate()
  await fetchDates()
  await fetchAssets()
  await fetchData()
})
</script>

<template>
  <div class="monitor-page">
    <!-- 过滤栏 -->
    <el-card shadow="never" class="status-card">
      <div class="filter-row">
        <span class="filter-label">计算日期：</span>
        <el-select
          v-model="selectedDate"
          placeholder="最新日期"
          size="small"
          clearable
          filterable
          style="width: 150px;"
        >
          <el-option
            v-for="d in dateOptions"
            :key="d"
            :label="d"
            :value="d"
          />
        </el-select>

        <span class="filter-label" style="margin-left: 12px;">标的：</span>
        <el-select
          v-model="selectedAsset"
          placeholder="全部"
          size="small"
          clearable
          filterable
          style="width: 130px;"
        >
          <el-option
            v-for="a in assetOptions"
            :key="a"
            :label="a"
            :value="a"
          />
        </el-select>

        <el-button
          size="small"
          type="primary"
          style="margin-left: 16px;"
          :loading="loading"
          @click="onQuery"
        >
          查询
        </el-button>

        <el-button
          size="small"
          type="warning"
          style="margin-left: 8px;"
          :loading="calculating"
          @click="triggerCalculate"
        >
          手动执行计算
        </el-button>

        <span class="filter-label" style="margin-left: auto; font-size: 12px; color: #909399;">
          最新计算日期：{{ latestCalcDate }}
        </span>
      </div>
    </el-card>

    <!-- AG Grid 表格 -->
    <el-card shadow="never" class="grid-card">
      <template #header>
        <div class="grid-header">
          <span>VWAP 基差分位阈值设置</span>
          <div class="header-actions">
            <el-popover
              placement="bottom-end"
              :width="260"
              trigger="click"
              @before-enter="refreshColumnVisibilities"
            >
              <template #reference>
                <el-button size="small">列选择</el-button>
              </template>
              <div class="column-picker">
                <div
                  v-for="col in columnVisibilities"
                  :key="col.colId"
                  class="column-picker-item"
                >
                  <el-checkbox
                    :model-value="col.visible"
                    @change="(val: boolean | string | number) => toggleColumnVisibility(col.colId, !!val)"
                  />
                  <span class="column-picker-label">{{ col.headerName }}</span>
                </div>
              </div>
            </el-popover>
            <el-button size="small" @click="saveColumnState">
              保存列配置
            </el-button>
          </div>
        </div>
      </template>
      <div ref="gridContainerRef">
      <ag-grid-vue
        class="orderbook-grid"
        :theme="orderbookGridTheme"
        :columnDefs="columnDefs"
        :rowData="rowData"
        :defaultColDef="defaultColDef"
        :getRowId="getRowId"
        :localeText="localeText"
        :header-height="32"
        :row-height="32"
        @grid-ready="onGridReady"
      />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.monitor-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ───── 过滤栏 ───── */
.status-card :deep(.el-card__body) {
  padding: 12px 16px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-label {
  color: var(--app-text-muted);
  font-size: 13px;
  white-space: nowrap;
}

/* ───── 表格 ───── */
.grid-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.orderbook-grid {
  width: 100%;
  height: calc(100vh - 220px);
  min-height: 480px;
}

/* 列选择面板 */
.column-picker {
  max-height: 400px;
  overflow-y: auto;
  padding: 4px 8px;
  margin-right: 4px;
}

.column-picker-item {
  display: flex;
  align-items: center;
  padding: 6px 4px;
  gap: 8px;
}

.column-picker-item :deep(.el-checkbox) {
  flex-shrink: 0;
  margin-right: 4px;
}

.column-picker-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
</style>
