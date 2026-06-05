<script setup lang="ts">
import { ref, shallowRef, onMounted, onUnmounted, computed } from 'vue'
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
  status: 'monitoring' | 'opened' | 'conditions_lost' | 'rejected' | 'gate_rejected'
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
  latest_signal_time: string | null
}

/* ───── 状态 ───── */
const gridApi = shallowRef<GridApi | null>(null)
const rowData = ref<SignalRow[]>([])
const summary = ref<Summary>({ total: 0, opened: 0, rejected: 0, conditions_lost: 0, monitoring: 0, conversion_rate: 0, latest_signal_time: null })
const loading = ref(false)

// 筛选条件
const filterStatus = ref<string>('')
const filterExitReason = ref<string>('')
const filterDays = ref<number>(1) // 默认今日
const filterAsset = ref<string>('')

// 分页配置
const paginationPageSize = ref<number>(100)
const paginationPageSizeOptions = [100, 500, 1000, 5000]
const paginationCurrentPage = ref<number>(1)
const paginationTotal = ref<number>(0)

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
  gate_rejected: { label: '被拒', color: '#f56c6c' },
}

const exitReasonOptions = [
  { label: '盈利性守卫', value: '盈利性守卫' },
  { label: 'resiliency', value: 'resiliency' },
  { label: '基差跌回阈值下', value: '基差跌回阈值下' },
  { label: '旁路风控', value: '旁路' },
  { label: '保证金风控', value: '保证金风控' },
  { label: '盘口覆盖超限', value: '盘口覆盖超限' },
  { label: '资金费率不达标', value: '资金费率不达标' },
  { label: '最小名义值', value: '最小名义值' },
  { label: '成交量不足', value: '成交量不足' },
  { label: '盘口中断', value: '盘口中断' },
]

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
    params.set('page', String(paginationCurrentPage.value))
    params.set('page_size', String(paginationPageSize.value))
    if (filterStatus.value) params.set('status', filterStatus.value)
    if (filterExitReason.value) params.set('exit_reason', filterExitReason.value)
    if (filterAsset.value.trim()) params.set('base_asset', filterAsset.value.trim())

    const res = await get(`/api/trading/signals?${params.toString()}`)
    if (!res.ok) return
    const json = await res.json()
    rowData.value = json.signals || []
    summary.value = json.summary || summary.value
    
    // 更新分页信息
    if (json.pagination) {
      paginationTotal.value = json.pagination.total || 0
    }
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

/** 页码变化 */
function onPageChange(page: number) {
  paginationCurrentPage.value = page
  fetchSignals()
}

/** 每页条数变化 */
function onPaginationSizeChange() {
  paginationCurrentPage.value = 1 // 切换每页条数时回到第一页
  fetchSignals()
}

/* ───── 快捷过滤 ───── */
function setStatusFilter(status: string) {
  filterStatus.value = status
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchSignals()
}

function setExitReasonFilter(reason: string) {
  filterExitReason.value = reason
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchSignals()
}

function setDaysFilter(days: number) {
  filterDays.value = days
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchSignals()
}

function onAssetSearch() {
  paginationCurrentPage.value = 1 // 切换筛选条件时回到第一页
  fetchSignals()
}

/* ───── 复制 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()

/* ───── 格式化 ───── */
function formatDuration(sec: number): string {
  if (!sec || sec === 0) return '0s'
  if (sec < 60) return `${sec}s`
  const minutes = Math.floor(sec / 60)
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  const seconds = sec % 60
  if (hours > 0) {
    return `${hours}h ${remainingMinutes}m ${seconds}s`
  }
  return `${remainingMinutes}m ${seconds}s`
}

/** 格式化时间 */
function formatTime(timeStr: string | null): string {
  if (!timeStr) return '无'
  // 如果是完整的时间格式，提取时间部分
  if (timeStr.includes(' ')) {
    const parts = timeStr.split(' ')
    return parts[1] || timeStr
  }
  return timeStr
}

/** 计算总页数 */
const totalPages = computed(() => {
  return Math.ceil(paginationTotal.value / paginationPageSize.value) || 1
})

/* ───── 定时自动刷新 ───── */
let autoRefreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  fetchSignals()
  autoRefreshTimer = setInterval(fetchSignals, 2000)
})

onUnmounted(() => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
})
</script>

<template>
  <div class="signals-page">
    <!-- 顶部统计栏 -->
    <div class="summary-bar">
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">总信号</span>
          <span class="summary-value">{{ summary.total }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">已开仓</span>
          <span class="summary-value summary-success">{{ summary.opened }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">被拒</span>
          <span class="summary-value summary-danger">{{ summary.rejected }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">条件消失</span>
          <span class="summary-value summary-info">{{ summary.conditions_lost }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">监控中</span>
          <span class="summary-value summary-primary">{{ summary.monitoring }}</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">转化率</span>
          <span class="summary-value">{{ summary.conversion_rate }}%</span>
        </span>
      </div>
      <div class="summary-group">
        <span class="summary-item">
          <span class="summary-label">最近更新</span>
          <span class="summary-value">{{ formatTime(summary.latest_signal_time) }}</span>
        </span>
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
          <el-button :type="filterStatus === 'gate_rejected' ? 'primary' : 'default'" @click="setStatusFilter('gate_rejected')">被拒</el-button>
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

    <div class="filter-bar reason-filter-bar">
      <div class="filter-group filter-group-wide">
        <span class="filter-label">结束原因：</span>
        <el-button-group size="small" class="reason-button-group">
          <el-button :type="filterExitReason === '' ? 'primary' : 'default'" @click="setExitReasonFilter('')">全部</el-button>
          <el-button
            v-for="option in exitReasonOptions"
            :key="option.value"
            :type="filterExitReason === option.value ? 'primary' : 'default'"
            @click="setExitReasonFilter(option.value)"
          >
            {{ option.label }}
          </el-button>
        </el-button-group>
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

    <!-- 底部分页控件 -->
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
.signals-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
}

/* ───── 汇总栏 ───── */
.summary-bar {
  display: flex;
  align-items: center;
  gap: 24px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 10px 18px;
  flex-wrap: wrap;
}

.summary-group {
  display: flex;
  align-items: center;
  gap: 4px;
}

.summary-item {
  display: inline-flex;
  align-items: baseline;
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

.summary-value.summary-success {
  color: #67c23a;
}

.summary-value.summary-danger {
  color: #f56c6c;
}

.summary-value.summary-info {
  color: #909399;
}

.summary-value.summary-primary {
  color: #409eff;
}

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

.filter-group-wide {
  align-items: flex-start;
  flex-wrap: wrap;
}

.filter-label {
  font-size: 13px;
  color: var(--el-text-color-secondary, #909399);
  line-height: 24px;
  white-space: nowrap;
}

.reason-filter-bar {
  gap: 8px;
}

.reason-button-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.reason-button-group :deep(.el-button) {
  margin-left: 0;
  border-radius: var(--el-border-radius-base);
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

/* 底部分页控件 */
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
