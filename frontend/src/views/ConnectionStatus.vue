<script setup lang="ts">
import { ref, shallowRef, onMounted, onUnmounted, computed } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type {
  ColDef,
  GetRowIdParams,
  GridApi,
  GridReadyEvent,
  ValueFormatterParams,
  ICellRendererParams,
} from 'ag-grid-community'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { showError, showSuccess } from '../utils/message'
import { get, post } from '../utils/request'

/* ───── 类型 ───── */
interface ConnectionRow {
  base_asset: string
  contract: string
  symbol: string
  gate_snapshot_status: 'pending' | 'success' | 'failed'
  gate_snapshot_error: string | null
  gate_ws_subscribed: boolean
  gate_receiving_data: boolean
  gate_last_update: number
  gate_stale_sec: number | null
  binance_ws_subscribed: boolean
  binance_receiving_data: boolean
  binance_last_update: number
  binance_stale_sec: number | null
}

/* ───── 响应式状态 ───── */
const gridApi = shallowRef<GridApi | null>(null)
const rowData = ref<ConnectionRow[]>([])
const loading = ref(false)
const serviceState = ref('idle')
const gateWsConnected = ref(false)
const binanceWsConnected = ref(false)
const gateWsLatencyMs = ref<number | null>(null)
const binanceWsLatencyMs = ref<number | null>(null)
let refreshTimer: ReturnType<typeof setInterval> | null = null

/* ───── 复制功能 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()

/* ───── 列状态持久化 ───── */
const PAGE_KEY = 'connection_status'

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}
const columnVisibilities = ref<ColumnVisibility[]>([])

function refreshColumnVisibilities() {
  if (!gridApi.value) return
  const states = gridApi.value.getColumnState()
  columnVisibilities.value = columnDefs
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

/* ───── 统计 ───── */
const stats = computed(() => {
  const total = rowData.value.length
  const gateSnapshotOk = rowData.value.filter(r => r.gate_snapshot_status === 'success').length
  const gateSnapshotFail = rowData.value.filter(r => r.gate_snapshot_status === 'failed').length
  const gateWsSub = rowData.value.filter(r => r.gate_ws_subscribed).length
  const gateReceiving = rowData.value.filter(r => r.gate_receiving_data).length
  const binanceWsSub = rowData.value.filter(r => r.binance_ws_subscribed).length
  const binanceReceiving = rowData.value.filter(r => r.binance_receiving_data).length
  return { total, gateSnapshotOk, gateSnapshotFail, gateWsSub, gateReceiving, binanceWsSub, binanceReceiving }
})

/* ───── 过滤 ───── */
type FilterType = 'all' | 'gate_failed' | 'gate_no_data' | 'gate_ws_unsub' | 'binance_no_data' | 'any_issue'
const activeFilter = ref<FilterType>('all')
const searchKeyword = ref('')

function applyFilter(filter?: FilterType) {
  if (filter !== undefined) activeFilter.value = filter
  if (!gridApi.value) return
  const f = activeFilter.value
  const kw = searchKeyword.value.trim().toUpperCase()
  gridApi.value.setGridOption('isExternalFilterPresent', () => f !== 'all' || kw.length > 0)
  gridApi.value.setGridOption('doesExternalFilterPass', (node: any) => {
    const data = node.data as ConnectionRow
    // 关键词过滤
    if (kw && !data.base_asset.toUpperCase().includes(kw) && !data.contract.toUpperCase().includes(kw)) {
      return false
    }
    // 状态过滤
    switch (f) {
      case 'gate_failed': return data.gate_snapshot_status === 'failed'
      case 'gate_no_data': return !data.gate_receiving_data
      case 'gate_ws_unsub': return !data.gate_ws_subscribed
      case 'binance_no_data': return !data.binance_receiving_data
      case 'any_issue': return data.gate_snapshot_status === 'failed' || !data.gate_receiving_data || !data.binance_receiving_data
      default: return true
    }
  })
  gridApi.value.onFilterChanged()
}

/* ───── 列定义 ───── */
const columnDefs: ColDef[] = [
  {
    headerName: '标的资产',
    field: 'base_asset',
    width: 100,
    pinned: 'left',
    filter: 'agTextColumnFilter',
  },
  {
    headerName: 'Gate合约',
    field: 'contract',
    width: 130,
  },
  {
    headerName: 'Binance交易对',
    field: 'symbol',
    width: 130,
  },
  {
    headerName: 'Gate快照',
    field: 'gate_snapshot_status',
    width: 110,
    cellRenderer: (params: ICellRendererParams) => {
      const v = params.value
      if (v === 'success') return '<span style="color:#67c23a">✓ 成功</span>'
      if (v === 'failed') return '<span style="color:#f56c6c">✗ 失败</span>'
      return '<span style="color:#909399">⏳ 等待中</span>'
    },
  },
  {
    headerName: '快照失败原因',
    field: 'gate_snapshot_error',
    width: 200,
    cellStyle: { color: '#f56c6c' },
  },
  {
    headerName: 'Gate WS订阅',
    field: 'gate_ws_subscribed',
    width: 110,
    cellRenderer: (params: ICellRendererParams) => {
      return params.value
        ? '<span style="color:#67c23a">✓ 已订阅</span>'
        : '<span style="color:#909399">✗ 未订阅</span>'
    },
  },
  {
    headerName: 'Gate实时数据',
    field: 'gate_receiving_data',
    width: 120,
    cellRenderer: (params: ICellRendererParams) => {
      return params.value
        ? '<span style="color:#67c23a">● 正常</span>'
        : '<span style="color:#f56c6c">○ 无数据</span>'
    },
  },
  {
    headerName: 'Gate延迟(s)',
    field: 'gate_stale_sec',
    width: 110,
    valueFormatter: (params: ValueFormatterParams) => {
      if (params.value == null) return '-'
      return params.value.toFixed(1)
    },
    cellStyle: (params: any) => {
      if (params.value != null && params.value > 10) return { color: '#e6a23c' }
      if (params.value != null && params.value > 30) return { color: '#f56c6c' }
      return null
    },
  },
  {
    headerName: 'Binance WS订阅',
    field: 'binance_ws_subscribed',
    width: 130,
    cellRenderer: (params: ICellRendererParams) => {
      return params.value
        ? '<span style="color:#67c23a">✓ 已订阅</span>'
        : '<span style="color:#909399">✗ 未订阅</span>'
    },
  },
  {
    headerName: 'Binance实时数据',
    field: 'binance_receiving_data',
    width: 130,
    cellRenderer: (params: ICellRendererParams) => {
      return params.value
        ? '<span style="color:#67c23a">● 正常</span>'
        : '<span style="color:#f56c6c">○ 无数据</span>'
    },
  },
  {
    headerName: 'Binance延迟(s)',
    field: 'binance_stale_sec',
    width: 120,
    valueFormatter: (params: ValueFormatterParams) => {
      if (params.value == null) return '-'
      return params.value.toFixed(1)
    },
    cellStyle: (params: any) => {
      if (params.value != null && params.value > 10) return { color: '#e6a23c' }
      if (params.value != null && params.value > 30) return { color: '#f56c6c' }
      return null
    },
  },
  {
    headerName: '操作',
    field: '_action',
    width: 100,
    pinned: 'right',
    sortable: false,
    cellRenderer: (params: ICellRendererParams) => {
      const data = params.data as ConnectionRow
      // 只有快照失败或无数据时显示重试按钮
      if (data.gate_snapshot_status === 'failed' || (!data.gate_receiving_data && data.gate_snapshot_status !== 'pending')) {
        return `<button class="retry-btn" data-asset="${data.base_asset}">重试</button>`
      }
      return ''
    },
    onCellClicked: (params: any) => {
      const target = params.event?.target as HTMLElement
      if (target?.classList?.contains('retry-btn')) {
        const asset = target.getAttribute('data-asset')
        if (asset) retrySnapshot(asset)
      }
    },
  },
]

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  suppressMovable: true,
}

/* ───── 数据加载 ───── */
const retrying = ref<Set<string>>(new Set())
const retryingAll = ref(false)

async function retrySnapshot(baseAsset: string) {
  if (retrying.value.has(baseAsset)) return
  retrying.value.add(baseAsset)
  try {
    const res = await post('/api/service/retry-snapshot', { base_asset: baseAsset })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: '重试失败' }))
      showError(err.detail || '重试失败')
    } else {
      // 刷新数据
      await fetchData()
    }
  } catch (e: any) {
    // request.ts 已处理
  } finally {
    retrying.value.delete(baseAsset)
  }
}

async function retryAllFailed() {
  if (retryingAll.value) return
  retryingAll.value = true
  try {
    const res = await post('/api/service/retry-all-failed', {})
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: '一键重连失败' }))
      showError(err.detail || '一键重连失败')
    } else {
      const data = await res.json()
      showSuccess(data.message || '重连完成')
      await fetchData()
    }
  } catch (e: any) {
    // request.ts 已处理
  } finally {
    retryingAll.value = false
  }
}

async function fetchData() {
  try {
    loading.value = true
    const res = await get('/api/service/connections')
    if (!res.ok) {
      showError('获取连接状态失败')
      return
    }
    const data = await res.json()
    rowData.value = data.items || []
    serviceState.value = data.state || 'idle'
    gateWsConnected.value = data.gate_ws_connected || false
    binanceWsConnected.value = data.binance_ws_connected || false
    gateWsLatencyMs.value = data.gate_ws_latency_ms ?? null
    binanceWsLatencyMs.value = data.binance_ws_latency_ms ?? null
  } catch (e: any) {
    // request.ts 已处理错误提示
  } finally {
    loading.value = false
  }
}

function onGridReady(params: GridReadyEvent) {
  gridApi.value = params.api
  setupGridCopy(params.api)
  loadColumnState()
}

function getRowId(params: GetRowIdParams) {
  return params.data.base_asset
}

onMounted(() => {
  fetchData()
  // 每 5 秒自动刷新
  refreshTimer = setInterval(fetchData, 5000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="connection-status-page">
    <!-- 顶部状态栏 -->
    <div class="status-header">
      <div class="status-info">
        <span class="status-badge" :class="serviceState">
          服务: {{ serviceState === 'running' ? '运行中' : serviceState === 'idle' ? '未启动' : serviceState }}
        </span>
        <span class="ws-badge" :class="{ connected: gateWsConnected }">
          Gate WS: {{ gateWsConnected ? (gateWsLatencyMs != null ? `${gateWsLatencyMs}ms` : '已连接') : '未连接' }}
        </span>
        <span class="ws-badge" :class="{ connected: binanceWsConnected }">
          Binance WS: {{ binanceWsConnected ? (binanceWsLatencyMs != null ? `${binanceWsLatencyMs}ms` : '已连接') : '未连接' }}
        </span>
      </div>

      <div class="stats-row">
        <span>总数: <b>{{ stats.total }}</b></span>
        <span>Gate快照成功: <b class="ok">{{ stats.gateSnapshotOk }}</b></span>
        <span>Gate快照失败: <b class="fail">{{ stats.gateSnapshotFail }}</b></span>
        <span>Gate接收中: <b class="ok">{{ stats.gateReceiving }}</b></span>
        <span>Binance接收中: <b class="ok">{{ stats.binanceReceiving }}</b></span>
      </div>

      <div class="filter-row">
        <el-input
          v-model="searchKeyword"
          placeholder="搜索标的..."
          size="small"
          clearable
          style="width: 140px; margin-right: 12px"
          @input="applyFilter()"
        />
        <el-radio-group v-model="activeFilter" size="small" @change="applyFilter">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="any_issue">异常</el-radio-button>
          <el-radio-button value="gate_failed">Gate快照失败</el-radio-button>
          <el-radio-button value="gate_no_data">Gate无数据</el-radio-button>
          <el-radio-button value="gate_ws_unsub">Gate未订阅</el-radio-button>
          <el-radio-button value="binance_no_data">Binance无数据</el-radio-button>
        </el-radio-group>
        <el-button size="small" @click="fetchData" :loading="loading" style="margin-left: 12px">
          刷新
        </el-button>
        <el-button
          size="small"
          type="warning"
          @click="retryAllFailed"
          :loading="retryingAll"
          :disabled="serviceState !== 'running'"
          style="margin-left: 8px"
        >
          一键重连
        </el-button>
        <el-popover placement="bottom-end" :width="260" trigger="click" @before-enter="refreshColumnVisibilities" style="margin-left: auto">
          <template #reference>
            <el-button size="small" style="margin-left: auto">列选择</el-button>
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
    <div ref="gridContainerRef" class="grid-wrapper">
      <AgGridVue
        class="ag-theme-quartz-dark"
        :theme="orderbookGridTheme"
        :columnDefs="columnDefs"
        :defaultColDef="defaultColDef"
        :rowData="rowData"
        :getRowId="getRowId"
        :animateRows="false"
        :suppressCellFocus="true"
        @grid-ready="onGridReady"
        style="width: 100%; height: 100%"
      />
    </div>
  </div>
</template>

<style scoped>
.connection-status-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 12px;
}

.status-header {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
  background: var(--app-sidebar, #1e1e2e);
  border-radius: 8px;
  border: 1px solid var(--app-border, #2d2d3d);
}

.status-info {
  display: flex;
  gap: 12px;
  align-items: center;
}

.status-badge {
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}
.status-badge.running { background: rgba(103, 194, 58, 0.15); color: #67c23a; }
.status-badge.idle { background: rgba(144, 147, 153, 0.15); color: #909399; }
.status-badge.starting { background: rgba(230, 162, 60, 0.15); color: #e6a23c; }
.status-badge.error { background: rgba(245, 108, 108, 0.15); color: #f56c6c; }

.ws-badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  background: rgba(245, 108, 108, 0.12);
  color: #f56c6c;
}
.ws-badge.connected {
  background: rgba(103, 194, 58, 0.12);
  color: #67c23a;
}

.stats-row {
  display: flex;
  gap: 16px;
  font-size: 13px;
  color: #b0b0b0;
}
.stats-row b { color: #e0e0e0; }
.stats-row b.ok { color: #67c23a; }
.stats-row b.fail { color: #f56c6c; }

.filter-row {
  display: flex;
  align-items: center;
}

.grid-wrapper {
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

:deep(.retry-btn) {
  padding: 2px 10px;
  border: 1px solid #e6a23c;
  border-radius: 4px;
  background: rgba(230, 162, 60, 0.12);
  color: #e6a23c;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}
:deep(.retry-btn:hover) {
  background: rgba(230, 162, 60, 0.25);
}
</style>
