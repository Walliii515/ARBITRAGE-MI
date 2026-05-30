<script setup lang="ts">
import { ref, shallowRef, onMounted, computed } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent } from 'ag-grid-community'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'
import { get, post } from '../utils/request'
import { showSuccess, showError } from '../utils/message'

/* ───── 类型 ───── */
interface SignalRow {
  id: number
  base_asset: string
  signal_time: string
  resolved_time: string | null
  status: 'monitoring' | 'opened' | 'conditions_lost' | 'rejected'
  entry_basis_bps: number | null
  peak_basis_bps: number | null
  exit_basis_bps: number | null
  exit_reason: string | null
  duration_sec: number | null
  trigger_type: string | null
  order_uuid: string | null
}

interface Summary {
  total: number
  opened: number
  rejected: number
  conditions_lost: number
  monitoring: number
  conversion_rate: number
  avg_duration_sec: number
}

/* ───── 状态 ───── */
const gridApi = shallowRef<GridApi | null>(null)
const rowData = ref<SignalRow[]>([])
const summary = ref<Summary>({ total: 0, opened: 0, rejected: 0, conditions_lost: 0, monitoring: 0, conversion_rate: 0, avg_duration_sec: 0 })
const loading = ref(false)

// 筛选条件
const filterStatus = ref<string>('')
const filterDays = ref<number>(3)
const filterAsset = ref<string>('')

/** 列状态持久化 */
const PAGE_KEY = 'trade_signals'

/** 列选择面板 */
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}
const columnVisibilities = ref<ColumnVisibility[]>([])

function refreshColumnVisibilities() {
  if (!gridApi.value) return
  const states = gridApi.value.getColumnState()
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
  if (!gridApi.value) return
  gridApi.value.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) col.visible = visible
}

async function saveColumnState() {
  if (!gridApi.value) return
  const columnState = gridApi.value.getColumnState()
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

async function loadColumnState() {
  if (!gridApi.value) return
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEY}`)
    const data = await res.json()
    if (data?.columnState && Array.isArray(data.columnState)) {
      gridApi.value.applyColumnState({ state: data.columnState, applyOrder: true })
    }
  } catch (e) {
    console.warn('Failed to load column config from server:', e)
  }
}

/** 从当前数据中提取唯一标的资产列表，供下拉快速选择 */
const assetOptions = computed(() => {
  const assets = new Set(rowData.value.map(r => r.base_asset))
  return Array.from(assets).sort()
})

/* ───── 列配置 ───── */
const statusMap: Record<string, { label: string; color: string }> = {
  monitoring: { label: '监控中', color: '#409eff' },
  opened: { label: '已开仓', color: '#67c23a' },
  conditions_lost: { label: '条件消失', color: '#909399' },
  rejected: { label: '被拒', color: '#f56c6c' },
}

const columnDefs = ref<ColDef[]>([
  { headerName: '标的资产', field: 'base_asset', width: 110, pinned: 'left' },
  { headerName: '信号时间', field: 'signal_time', width: 165 },
  {
    headerName: '状态',
    field: 'status',
    width: 100,
    cellRenderer: (params: any) => {
      const s = statusMap[params.value] || { label: params.value, color: '#606266' }
      return `<span style="color:${s.color};font-weight:600">${s.label}</span>`
    },
  },
  {
    headerName: '入场基差',
    field: 'entry_basis_bps',
    width: 105,
    valueFormatter: (p: any) => p.value != null ? `${p.value.toFixed(1)}` : '',
  },
  {
    headerName: '峰值基差',
    field: 'peak_basis_bps',
    width: 105,
    valueFormatter: (p: any) => p.value != null ? `${p.value.toFixed(1)}` : '',
  },
  {
    headerName: '退出基差',
    field: 'exit_basis_bps',
    width: 105,
    valueFormatter: (p: any) => p.value != null ? `${p.value.toFixed(1)}` : '',
  },
  {
    headerName: '持续时长',
    field: 'duration_sec',
    width: 100,
    valueFormatter: (p: any) => {
      if (p.value == null) return ''
      if (p.value < 60) return `${p.value}s`
      return `${Math.floor(p.value / 60)}m ${p.value % 60}s`
    },
  },
  {
    headerName: '触发方式',
    field: 'trigger_type',
    width: 100,
    valueFormatter: (p: any) => {
      if (!p.value) return ''
      return p.value === 'pullback' ? '回落确认' : p.value === 'timeout' ? '超时' : p.value
    },
  },
  {
    headerName: '结束原因',
    field: 'exit_reason',
    width: 280,
    tooltipField: 'exit_reason',
    tooltipComponent: LongTextTooltip,
  },
  { headerName: '结束时间', field: 'resolved_time', width: 165 },
  { headerName: '订单UUID', field: 'order_uuid', width: 140 },
])

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
}

/* ───── 数据加载 ───── */
async function fetchSignals() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    params.set('days', String(filterDays.value))
    if (filterStatus.value) params.set('status', filterStatus.value)
    if (filterAsset.value.trim()) params.set('base_asset', filterAsset.value.trim())

    const res = await get(`/api/trading/signals?${params.toString()}`)
    if (!res.ok) return
    const json = await res.json()
    rowData.value = json.signals || []
    summary.value = json.summary || summary.value
  } catch (e: any) {
    console.error('加载信号失败:', e)
  } finally {
    loading.value = false
  }
}

function onGridReady(event: GridReadyEvent) {
  gridApi.value = event.api
  setupGridCopy(event.api)
  loadColumnState()
}

/* ───── 快捷过滤 ───── */
function setStatusFilter(status: string) {
  filterStatus.value = status
  fetchSignals()
}

function setDaysFilter(days: number) {
  filterDays.value = days
  fetchSignals()
}

function onAssetSearch() {
  fetchSignals()
}

/* ───── 复制 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()

/* ───── 格式化 ───── */
function formatDuration(sec: number): string {
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m ${sec % 60}s`
}

onMounted(() => {
  fetchSignals()
})
</script>

<template>
  <div class="signals-page">
    <!-- 顶部统计卡片 -->
    <div class="summary-cards">
      <div class="card">
        <div class="card-value">{{ summary.total }}</div>
        <div class="card-label">总信号</div>
      </div>
      <div class="card card-success">
        <div class="card-value">{{ summary.opened }}</div>
        <div class="card-label">已开仓</div>
      </div>
      <div class="card card-danger">
        <div class="card-value">{{ summary.rejected }}</div>
        <div class="card-label">被拒</div>
      </div>
      <div class="card card-info">
        <div class="card-value">{{ summary.conditions_lost }}</div>
        <div class="card-label">条件消失</div>
      </div>
      <div class="card card-primary">
        <div class="card-value">{{ summary.monitoring }}</div>
        <div class="card-label">监控中</div>
      </div>
      <div class="card">
        <div class="card-value">{{ summary.conversion_rate }}%</div>
        <div class="card-label">转化率</div>
      </div>
      <div class="card">
        <div class="card-value">{{ formatDuration(summary.avg_duration_sec) }}</div>
        <div class="card-label">平均时长</div>
      </div>
    </div>

    <!-- 筛选区 -->
    <div class="filter-bar">
      <div class="filter-group">
        <span class="filter-label">状态：</span>
        <el-button-group size="small">
          <el-button :type="filterStatus === '' ? 'primary' : 'default'" @click="setStatusFilter('')">全部</el-button>
          <el-button :type="filterStatus === 'monitoring' ? 'primary' : 'default'" @click="setStatusFilter('monitoring')">监控中</el-button>
          <el-button :type="filterStatus === 'opened' ? 'primary' : 'default'" @click="setStatusFilter('opened')">已开仓</el-button>
          <el-button :type="filterStatus === 'conditions_lost' ? 'primary' : 'default'" @click="setStatusFilter('conditions_lost')">条件消失</el-button>
          <el-button :type="filterStatus === 'rejected' ? 'primary' : 'default'" @click="setStatusFilter('rejected')">被拒</el-button>
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

      <div class="filter-group">
        <el-select
          v-model="filterAsset"
          placeholder="标的资产"
          size="small"
          filterable
          clearable
          style="width: 150px"
          @change="fetchSignals"
        >
          <el-option
            v-for="asset in assetOptions"
            :key="asset"
            :label="asset"
            :value="asset"
          />
        </el-select>
      </div>

      <el-button size="small" :loading="loading" @click="fetchSignals">刷新</el-button>

      <div class="filter-group" style="margin-left: auto">
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

    <!-- AG Grid 表格 -->
    <div class="grid-container" ref="gridContainerRef">
      <AgGridVue
        :theme="orderbookGridTheme"
        :rowData="rowData"
        :columnDefs="columnDefs"
        :defaultColDef="defaultColDef"
        :getRowId="(params: any) => String(params.data.id)"
        :tooltipShowDelay="300"
        @grid-ready="onGridReady"
        style="width: 100%; height: 100%"
      />
    </div>
  </div>
</template>

<style scoped>
.signals-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
}

.summary-cards {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.card {
  background: var(--app-card, #1e1e2e);
  border: 1px solid var(--app-border, #2d2d3d);
  border-radius: 8px;
  padding: 12px 18px;
  min-width: 100px;
  text-align: center;
}

.card-value {
  font-size: 20px;
  font-weight: 700;
  color: var(--el-text-color-primary, #e0e0e0);
}

.card-label {
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
  margin-top: 4px;
}

.card-success .card-value { color: #67c23a; }
.card-danger .card-value { color: #f56c6c; }
.card-info .card-value { color: #909399; }
.card-primary .card-value { color: #409eff; }

.filter-bar {
  display: flex;
  align-items: center;
  gap: 16px;
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
}

.grid-container {
  flex: 1;
  min-height: 0;
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
}
</style>
