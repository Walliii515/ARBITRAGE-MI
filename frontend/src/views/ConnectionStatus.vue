<script setup lang="ts">
import { computed, ref, shallowRef, onMounted, onUnmounted } from 'vue'
import { ElMessageBox } from 'element-plus'
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
import { showError, showSuccess, showWarning } from '../utils/message'
import { get, post } from '../utils/request'
import { useConnectionMonitor, type ConnectionRow } from '../composables/useConnectionMonitor'

/* ───── 响应式状态 ───── */
const gridApi = shallowRef<GridApi | null>(null)
const loading = ref(false)
const serviceBusy = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null
const {
  connectionRows: rowData,
  connectionStats: stats,
  serviceState,
  gateWsConnected,
  binanceWsConnected,
  gateWsLatencyMs,
  binanceWsLatencyMs,
  exchangeRiskMonitor,
  delistRiskReport,
  fetchConnectionStatus,
} = useConnectionMonitor()

const riskWsConnected = computed(() => !!exchangeRiskMonitor.value?.connected)
const riskWsEnabled = computed(() => exchangeRiskMonitor.value?.enabled !== false)
const riskWsChannels = computed(() => exchangeRiskMonitor.value?.channels || {})
const riskWsAdlStatus = computed(() => riskWsChannels.value['futures.auto_deleverages'] || 'unknown')
const riskWsLiquidationStatus = computed(() => riskWsChannels.value['futures.liquidates'] || 'unknown')
const riskWsHealthy = computed(() =>
  riskWsConnected.value
  && riskWsAdlStatus.value === 'success'
  && riskWsLiquidationStatus.value === 'success'
)
const canStartService = computed(
  () => !serviceBusy.value && (serviceState.value === 'idle' || serviceState.value === 'error'),
)
const canStopService = computed(
  () =>
    !serviceBusy.value &&
    (
      serviceState.value === 'running' ||
      serviceState.value === 'starting' ||
      serviceState.value === 'stopping' ||
      serviceState.value === 'error'
    ),
)

function formatRiskWsStatus(status: string) {
  if (status === 'success') return '成功'
  if (status === 'pending') return '订阅中'
  if (status === 'fail') return '失败'
  return status || '未知'
}

function delistRiskText(data: ConnectionRow) {
  const risks = data.delist_risks || []
  if (!risks.length) return ''
  return risks.map((risk) => {
    const due = risk.delist_at ? ` ${risk.delist_at}` : ''
    return `${risk.exchange}:${risk.message || risk.status || risk.risk_type}${due}`
  }).join(' | ')
}

function maybeShowDelistRiskAlert() {
  const risks = delistRiskReport.value.items || []
  if (!risks.length) return
  const fingerprint = risks
    .map((risk) => `${risk.risk_key}:${risk.delist_at || ''}:${risk.status || ''}`)
    .sort()
    .join('|')
  const storageKey = 'connection_delist_risk_alert'
  const now = Date.now()
  const previous = JSON.parse(localStorage.getItem(storageKey) || '{}')
  if (previous.fingerprint === fingerprint && now - Number(previous.at || 0) < 24 * 60 * 60 * 1000) return

  localStorage.setItem(storageKey, JSON.stringify({ fingerprint, at: now }))
  const preview = risks.slice(0, 8).map((risk) => {
    const due = risk.delist_at ? `，时间 ${risk.delist_at}` : ''
    return `${risk.base_asset} ${risk.exchange} ${risk.message || risk.status || risk.risk_type}${due}`
  }).join('\n')
  ElMessageBox.alert(preview, `监控标的下架风险 ${risks.length} 个`, {
    type: 'warning',
    confirmButtonText: '知道了',
  }).catch(() => {})
}

/* ───── 复制功能 ───── */
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

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

/* ───── 过滤 ───── */
type FilterType = 'all' | 'gate_no_data' | 'gate_ws_unsub' | 'binance_no_data' | 'delist_risk' | 'any_issue'
const activeFilter = ref<FilterType>('any_issue')
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
      case 'gate_no_data': return !data.gate_receiving_data
      case 'gate_ws_unsub': return !data.gate_ws_subscribed
      case 'binance_no_data': return !data.binance_receiving_data
      case 'delist_risk': return !!(data.delist_risks && data.delist_risks.length > 0)
      case 'any_issue': return !data.gate_receiving_data || !data.binance_receiving_data || !!(data.delist_risks && data.delist_risks.length > 0)
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
    headerName: 'Gate新鲜度(s)',
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
    headerName: 'Binance新鲜度(s)',
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
    headerName: '下架风险',
    field: 'delist_risk_summary',
    width: 240,
    cellRenderer: (params: ICellRendererParams) => {
      const data = params.data as ConnectionRow
      const text = delistRiskText(data)
      if (!text) return '<span style="color:#67c23a">正常</span>'
      const color = data.delist_risk_level === 'critical' ? '#f56c6c' : '#e6a23c'
      return `<span title="${text.replace(/"/g, '&quot;')}" style="color:${color}">${text}</span>`
    },
  },
  {
    headerName: '操作',
    field: '_action',
    width: 170,
    pinned: 'right',
    sortable: false,
    cellRenderer: (params: ICellRendererParams) => {
      const data = params.data as ConnectionRow
      const asset = String(data.base_asset || '').replace(/"/g, '&quot;')
      const buttons: string[] = []
      if (!data.gate_receiving_data || !data.binance_receiving_data || !data.gate_ws_subscribed || !data.binance_ws_subscribed) {
        buttons.push(`<button class="retry-btn" data-asset="${asset}">重试</button>`)
      }
      if (data.delist_risks && data.delist_risks.length > 0) {
        buttons.push(`<button class="disable-btn" data-asset="${asset}">设失效</button>`)
      }
      return buttons.join('')
    },
    onCellClicked: (params: any) => {
      const target = params.event?.target as HTMLElement
      if (target?.classList?.contains('retry-btn')) {
        const asset = target.getAttribute('data-asset')
        if (asset) retryConnection(asset)
      }
      if (target?.classList?.contains('disable-btn')) {
        const asset = target.getAttribute('data-asset')
        if (asset) disableBaseAsset(asset)
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
const disablingAssets = ref<Set<string>>(new Set())

async function retryConnection(baseAsset: string) {
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

async function disableBaseAsset(baseAsset: string) {
  if (disablingAssets.value.has(baseAsset)) return
  try {
    await ElMessageBox.confirm(
      `确认将 ${baseAsset} 设为失效？之后它不会再进入常规订阅和监控候选；如仍有持仓，系统会保留必要持仓风险监控直到平仓。`,
      '设为失效',
      {
        type: 'warning',
        confirmButtonText: '确认',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  disablingAssets.value.add(baseAsset)
  try {
    const res = await post(`/api/trading/base-assets/${encodeURIComponent(baseAsset)}/disable`, {
      reason: 'connection_status_delist_risk',
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || !data?.success) {
      showError(data?.detail || data?.message || '设为失效失败')
      return
    }
    showSuccess(data.message || `${baseAsset} 已设为失效`)
    await fetchData()
  } catch (e: any) {
    // request.ts 已处理
  } finally {
    disablingAssets.value.delete(baseAsset)
  }
}

async function fetchData() {
  try {
    loading.value = true
    await fetchConnectionStatus()
    serviceBusy.value = serviceState.value === 'starting' || serviceState.value === 'stopping'
    maybeShowDelistRiskAlert()
  } catch (e: any) {
    showError(e?.message || '获取连接状态失败')
  } finally {
    loading.value = false
  }
}

async function startService() {
  try {
    serviceBusy.value = true
    serviceState.value = 'starting'
    const res = await post('/api/service/start')
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      serviceBusy.value = false
      showError(typeof body.detail === 'string' ? body.detail : '启动失败')
      return
    }
    showSuccess('正在启动后端 WS 服务…')
    await fetchData()
  } catch {
    serviceBusy.value = false
    showError('启动请求失败')
  }
}

async function stopService() {
  try {
    serviceBusy.value = true
    serviceState.value = 'stopping'
    const res = await post('/api/service/stop')
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      serviceBusy.value = false
      showError(typeof body.detail === 'string' ? body.detail : '终止失败')
      return
    }
    showWarning('正在终止后端 WS 服务…')
    await fetchData()
  } catch {
    serviceBusy.value = false
    showError('终止请求失败')
  }
}

function onGridReady(params: GridReadyEvent) {
  gridApi.value = params.api
  setupGridCopy(params.api)
  loadColumnState()
  applyFilter()
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
          Gate WS p50: {{ gateWsConnected ? (gateWsLatencyMs != null ? `${gateWsLatencyMs}ms` : '已连接') : '未连接' }}
        </span>
        <span class="ws-badge" :class="{ connected: binanceWsConnected }">
          Binance WS p50: {{ binanceWsConnected ? (binanceWsLatencyMs != null ? `${binanceWsLatencyMs}ms` : '已连接') : '未连接' }}
        </span>
        <span
          class="ws-badge"
          :class="{ connected: riskWsHealthy, warning: riskWsEnabled && riskWsConnected && !riskWsHealthy }"
        >
          Gate风险WS:
          {{ !riskWsEnabled ? '关闭' : riskWsConnected ? '已连接' : '未连接' }}
        </span>
        <div class="service-actions">
          <el-button
            type="primary"
            size="small"
            :disabled="!canStartService"
            :loading="serviceBusy && serviceState === 'starting'"
            @click="startService"
          >
            启动后端 WS 服务
          </el-button>
          <el-button
            type="danger"
            size="small"
            :disabled="!canStopService"
            :loading="serviceBusy && serviceState === 'stopping'"
            @click="stopService"
          >
            终止后端 WS 服务
          </el-button>
        </div>
      </div>

      <div class="stats-row">
        <span>总数: <b>{{ stats.total }}</b></span>
        <span>Gate接收中: <b class="ok">{{ stats.gateReceiving }}</b></span>
        <span>Binance接收中: <b class="ok">{{ stats.binanceReceiving }}</b></span>
        <span>
          ADL订阅:
          <b :class="{ ok: riskWsAdlStatus === 'success', warn: riskWsAdlStatus !== 'success' }">
            {{ formatRiskWsStatus(riskWsAdlStatus) }}
          </b>
        </span>
        <span>
          强平订阅:
          <b :class="{ ok: riskWsLiquidationStatus === 'success', warn: riskWsLiquidationStatus !== 'success' }">
            {{ formatRiskWsStatus(riskWsLiquidationStatus) }}
          </b>
        </span>
        <span>风险事件: <b>{{ exchangeRiskMonitor?.event_count ?? 0 }}</b></span>
        <span>队列: <b>{{ exchangeRiskMonitor?.queue_size ?? 0 }}</b></span>
        <span>
          下架风险:
          <b :class="{ fail: stats.delistRisk > 0 }">{{ stats.delistRisk }}</b>
        </span>
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
          <el-radio-button value="gate_no_data">Gate无数据</el-radio-button>
          <el-radio-button value="gate_ws_unsub">Gate未订阅</el-radio-button>
          <el-radio-button value="binance_no_data">Binance无数据</el-radio-button>
          <el-radio-button value="delist_risk">下架风险</el-radio-button>
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
  flex-wrap: wrap;
}

.service-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
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
.ws-badge.warning {
  background: rgba(230, 162, 60, 0.12);
  color: #e6a23c;
}

.stats-row {
  display: flex;
  gap: 16px;
  font-size: 13px;
  color: #b0b0b0;
}
.stats-row b { color: #e0e0e0; }
.stats-row b.ok { color: #67c23a; }
.stats-row b.warn { color: #e6a23c; }
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

:deep(.retry-btn),
:deep(.disable-btn) {
  padding: 2px 10px;
  margin-right: 6px;
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
:deep(.disable-btn) {
  border-color: #f56c6c;
  background: rgba(245, 108, 108, 0.12);
  color: #f56c6c;
}
:deep(.disable-btn:hover) {
  background: rgba(245, 108, 108, 0.25);
}
</style>
