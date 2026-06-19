<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { ElMessageBox } from 'element-plus'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GetRowIdParams, GridApi, GridReadyEvent, ICellRendererParams, ValueFormatterParams } from 'ag-grid-community'
import { orderbookGridTheme } from '../ag-grid/orderbookGridTheme'
import { useGridCopy } from '../ag-grid/useGridCopy'
import { get, post } from '../utils/request'
import { showError, showSuccess, showWarning } from '../utils/message'

type CandidateStatus = 'matched' | 'gate_only' | 'binance_only'
type CandidateFilter = CandidateStatus | 'added_to_monitor' | 'all'
type ActionStatus = 'pending' | 'acknowledged' | 'ignored' | 'disabled' | 'added_to_monitor'

interface ListingEventRow {
  id: number
  base_asset: string
  gate_contract: string | null
  binance_symbol: string | null
  candidate_status: CandidateStatus
  action_status: ActionStatus
  is_actionable: number | boolean
  gate_status: string | null
  binance_status: string | null
  gate_volume_24h_settle: number | null
  binance_quote_volume: number | null
  gate_funding_rate_24h: number | null
  first_seen_at: string
  last_seen_at: string
  acknowledged_at: string | null
  action_at: string | null
  action_reason: string | null
  base_asset_is_valid: string | null
  strategy_tier: string | null
  calculated_strategy_tier: string | null
}

const PAGE_KEY = 'listing_events'
const gridApi = shallowRef<GridApi | null>(null)
const loading = ref(false)
const actionLoading = ref(false)
const rowData = ref<ListingEventRow[]>([])
const summary = ref<Record<string, any>>({})
const actionFilter = ref<ActionStatus | 'all'>('pending')
const candidateFilter = ref<CandidateFilter>('matched')
const { gridContainerRef, setupGridCopy } = useGridCopy()
void gridContainerRef

interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

const pendingActionable = computed(() => Number(summary.value.pending_actionable || 0))

function candidateText(value: string) {
  if (value === 'matched') return '双边候选'
  if (value === 'gate_only') return '仅Gate'
  if (value === 'binance_only') return '仅Binance'
  return value || ''
}

function actionText(value: string) {
  if (value === 'pending') return '待处理'
  if (value === 'acknowledged') return '已读'
  if (value === 'ignored') return '已忽略'
  if (value === 'disabled') return '已失效'
  if (value === 'added_to_monitor') return '已处理'
  return value || ''
}

function displayCandidateText(data?: ListingEventRow | null) {
  if (data?.action_status === 'added_to_monitor') return '已加入'
  return candidateText(data?.candidate_status || '')
}

function displayCandidateColor(data?: ListingEventRow | null) {
  if (data?.action_status === 'added_to_monitor') return '#67c23a'
  if (data?.candidate_status === 'matched') return '#409eff'
  return '#e6a23c'
}

function formatNumber(value: unknown) {
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Math.abs(n) >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(2)}K`
  return n.toFixed(0)
}

function formatFundingBps(params: ValueFormatterParams) {
  const n = Number(params.value)
  if (!Number.isFinite(n)) return ''
  return `${(n * 10000).toFixed(2)}`
}

function findListingRow(asset: string) {
  const normalized = String(asset || '').trim().toUpperCase()
  return rowData.value.find((row) => String(row.base_asset || '').trim().toUpperCase() === normalized)
}

const columnDefs: ColDef<ListingEventRow>[] = [
  { headerName: '标的', field: 'base_asset', width: 100, pinned: 'left', filter: 'agTextColumnFilter' },
  {
    headerName: '候选状态',
    field: 'candidate_status',
    width: 110,
    cellRenderer: (params: ICellRendererParams<ListingEventRow>) => {
      const color = displayCandidateColor(params.data)
      return `<span style="color:${color}">${displayCandidateText(params.data)}</span>`
    },
  },
  {
    headerName: '处理状态',
    field: 'action_status',
    width: 105,
    cellRenderer: (params: ICellRendererParams<ListingEventRow>) => {
      const value = String(params.value || '')
      const color = value === 'pending' ? '#f56c6c' : value === 'added_to_monitor' ? '#67c23a' : '#909399'
      return `<span style="color:${color}">${actionText(value)}</span>`
    },
  },
  { headerName: 'Gate合约', field: 'gate_contract', width: 130 },
  { headerName: 'Gate状态', field: 'gate_status', width: 95 },
  {
    headerName: 'Gate 24h成交额',
    field: 'gate_volume_24h_settle',
    width: 130,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (params) => formatNumber(params.value),
  },
  {
    headerName: '24h资金费(bps)',
    field: 'gate_funding_rate_24h',
    width: 125,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: formatFundingBps,
  },
  { headerName: 'Binance交易对', field: 'binance_symbol', width: 130 },
  { headerName: 'Binance状态', field: 'binance_status', width: 110 },
  {
    headerName: 'Binance 24h成交额',
    field: 'binance_quote_volume',
    width: 145,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (params) => formatNumber(params.value),
  },
  { headerName: '首次发现', field: 'first_seen_at', width: 155 },
  { headerName: '最近存在', field: 'last_seen_at', width: 155 },
  { headerName: '原因', field: 'action_reason', width: 180 },
  {
    headerName: '操作',
    colId: 'actions',
    width: 220,
    pinned: 'right',
    sortable: false,
    filter: false,
    cellRenderer: (params: ICellRendererParams<ListingEventRow>) => {
      const data = params.data
      if (!data) return ''
      const asset = String(data.base_asset || '').replace(/"/g, '&quot;')
      const buttons: string[] = []
      const actionable = data.action_status === 'pending' || data.action_status === 'acknowledged'
      if (actionable && data.candidate_status === 'matched') {
        buttons.push(`<button class="listing-add-btn" data-asset="${asset}">加入监控</button>`)
      }
      if (data.action_status === 'pending') {
        buttons.push(`<button class="listing-ack-btn" data-asset="${asset}">已读</button>`)
      }
      if (actionable) {
        buttons.push(`<button class="listing-disable-btn" data-asset="${asset}">设失效</button>`)
      }
      return buttons.join('')
    },
    onCellClicked: (params: any) => {
      const target = params.event?.target as HTMLElement
      const asset = target?.getAttribute?.('data-asset')
      if (!asset) return
      if (target.classList.contains('listing-add-btn')) addToMonitor(asset)
      if (target.classList.contains('listing-ack-btn')) markRead(asset)
      if (target.classList.contains('listing-disable-btn')) disableAsset(asset)
    },
  },
]

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,
  enablePivot: false,
  enableValue: false,
}

function refreshColumnVisibilities() {
  if (!gridApi.value) return
  const states = gridApi.value.getColumnState()
  columnVisibilities.value = columnDefs
    .filter((col) => col.field || col.colId)
    .map((col) => {
      const colId = (col.colId ?? col.field) as string
      const state = states.find((s) => s.colId === colId)
      return {
        colId,
        headerName: col.headerName ?? colId,
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
  try {
    const res = await post(`/api/trading/column-config/${PAGE_KEY}`, { columnState: gridApi.value.getColumnState() })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.success === false) {
      showError(data?.message || data?.detail || '保存列配置失败')
      return
    }
    showSuccess('列配置已保存')
  } catch (e: any) {
    showError(e?.message || '保存列配置失败')
  }
}

async function loadColumnState() {
  if (!gridApi.value) return
  try {
    const res = await get(`/api/trading/column-config/${PAGE_KEY}`)
    const data = await res.json().catch(() => ({}))
    if (Array.isArray(data?.columnState)) {
      gridApi.value.applyColumnState({ state: data.columnState, applyOrder: true })
      refreshColumnVisibilities()
    }
  } catch (e) {
    console.warn('Failed to load listing events column config:', e)
  }
}

async function fetchRows() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (actionFilter.value !== 'all') params.set('action_status', actionFilter.value)
    if (candidateFilter.value !== 'all') params.set('candidate_status', candidateFilter.value)
    params.set('limit', '500')
    const res = await get(`/api/trading/listing-events?${params.toString()}`)
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '获取上新事件失败')
      return
    }
    rowData.value = data.items || []
    summary.value = data.summary || {}
  } catch (e: any) {
    showError(e?.message || '获取上新事件失败')
  } finally {
    loading.value = false
  }
}

function handleCandidateFilterChange() {
  if (candidateFilter.value === 'added_to_monitor') {
    actionFilter.value = 'all'
  }
  fetchRows()
}

async function refreshEvents() {
  loading.value = true
  try {
    const res = await post('/api/trading/listing-events/refresh')
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.success === false) {
      showError(data?.detail || data?.message || '刷新上新事件失败')
      return
    }
    showSuccess('上新事件已刷新')
    await fetchRows()
  } catch (e: any) {
    showError(e?.message || '刷新上新事件失败')
  } finally {
    loading.value = false
  }
}

async function markRead(asset: string) {
  if (actionLoading.value) return
  actionLoading.value = true
  try {
    const res = await post(`/api/trading/listing-events/${encodeURIComponent(asset)}/ack`, { reason: 'listing_event_read' })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.success === false) {
      showError(data?.detail || '标记已读失败')
      return
    }
    await fetchRows()
  } finally {
    actionLoading.value = false
  }
}

async function addToMonitor(asset: string) {
  if (actionLoading.value) return
  const row = findListingRow(asset)
  const tier = row?.calculated_strategy_tier || row?.strategy_tier || '-'
  const gateVolume = formatNumber(row?.gate_volume_24h_settle)
  const binanceVolume = formatNumber(row?.binance_quote_volume)
  try {
    await ElMessageBox.confirm(`${asset} 将加入监控候选，计算分层为 ${tier}（Gate 24h成交额 ${gateVolume || '-'}，Binance 24h成交额 ${binanceVolume || '-'}）；后续开仓仍受策略过滤控制。`, '加入监控', {
      type: 'warning',
      confirmButtonText: '确认加入',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  actionLoading.value = true
  try {
    const res = await post(`/api/trading/listing-events/${encodeURIComponent(asset)}/add-to-monitor`)
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.success === false) {
      showError(data?.detail || data?.message || '加入监控失败')
      return
    }
    showSuccess(data.message || `${asset} 已加入监控`)
    await fetchRows()
  } finally {
    actionLoading.value = false
  }
}

async function disableAsset(asset: string) {
  if (actionLoading.value) return
  try {
    await ElMessageBox.confirm(`${asset} 将写入失效标的，后续不再弹窗提醒，也不会进入常规订阅候选。`, '设为失效', {
      type: 'warning',
      confirmButtonText: '确认失效',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  actionLoading.value = true
  try {
    const res = await post(`/api/trading/listing-events/${encodeURIComponent(asset)}/disable`, {
      reason: 'listing_event_disabled',
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.success === false) {
      showError(data?.detail || data?.message || '设为失效失败')
      return
    }
    showWarning(data.message || `${asset} 已设为失效`)
    await fetchRows()
  } finally {
    actionLoading.value = false
  }
}

function onGridReady(params: GridReadyEvent<ListingEventRow>) {
  gridApi.value = params.api
  setupGridCopy(params.api)
  loadColumnState()
  refreshColumnVisibilities()
}

function getRowId(params: GetRowIdParams<ListingEventRow>) {
  return String(params.data?.base_asset || params.data?.id || '')
}

onMounted(fetchRows)
</script>

<template>
  <div class="listing-events-page">
    <div class="toolbar">
      <div class="summary">
        <span>待处理: <b :class="{ warn: pendingActionable > 0 }">{{ summary.pending || 0 }}</b></span>
        <span>可提醒: <b :class="{ fail: pendingActionable > 0 }">{{ pendingActionable }}</b></span>
        <span>已加入: <b>{{ summary.added_to_monitor || 0 }}</b></span>
        <span>已失效: <b>{{ summary.disabled || 0 }}</b></span>
      </div>
      <div class="filters">
        <el-radio-group v-model="candidateFilter" size="small" @change="handleCandidateFilterChange">
          <el-radio-button value="matched">双边候选</el-radio-button>
          <el-radio-button value="gate_only">仅Gate</el-radio-button>
          <el-radio-button value="binance_only">仅Binance</el-radio-button>
          <el-radio-button value="added_to_monitor">已加入</el-radio-button>
          <el-radio-button value="all">全部候选</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="actionFilter" size="small" @change="fetchRows">
          <el-radio-button value="all">全部处理</el-radio-button>
          <el-radio-button value="pending">待处理</el-radio-button>
          <el-radio-button value="acknowledged">已读</el-radio-button>
          <el-radio-button value="ignored">已忽略</el-radio-button>
          <el-radio-button value="disabled">已失效</el-radio-button>
        </el-radio-group>
        <el-button size="small" :loading="loading" @click="fetchRows">刷新</el-button>
        <el-button size="small" type="primary" :loading="loading" @click="refreshEvents">扫描上新</el-button>
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

    <div ref="gridContainerRef" class="grid-wrapper">
      <AgGridVue
        class="ag-theme-quartz-dark"
        :theme="orderbookGridTheme"
        :columnDefs="columnDefs"
        :defaultColDef="defaultColDef"
        :rowData="rowData"
        :getRowId="getRowId"
        :pagination="true"
        :paginationPageSize="50"
        :paginationPageSizeSelector="[20, 50, 100, 200]"
        :animateRows="false"
        :suppressCellFocus="true"
        @grid-ready="onGridReady"
        style="width: 100%; height: 100%"
      />
    </div>
  </div>
</template>

<style scoped>
.listing-events-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 12px;
}

.toolbar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
  background: var(--app-sidebar, #1e1e2e);
  border: 1px solid var(--app-border, #2d2d3d);
  border-radius: 8px;
}

.summary,
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.summary span {
  color: var(--app-text-secondary, #9aa0a6);
  font-size: 13px;
}

.summary b {
  color: var(--app-text, #e5e7eb);
}

.summary b.warn {
  color: #e6a23c;
}

.summary b.fail {
  color: #f56c6c;
}

.grid-wrapper {
  flex: 1;
  min-height: 0;
}

.column-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 360px;
  overflow: auto;
}

.column-picker-item {
  display: flex;
  align-items: center;
  gap: 8px;
  line-height: 22px;
}

.column-picker-label {
  color: var(--app-text, #e5e7eb);
  font-size: 13px;
}

:deep(.listing-add-btn),
:deep(.listing-ack-btn),
:deep(.listing-disable-btn) {
  margin-right: 6px;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: 4px;
  color: #fff;
  cursor: pointer;
  font-size: 12px;
}

:deep(.listing-add-btn) {
  background: #409eff;
}

:deep(.listing-ack-btn) {
  background: #606266;
}

:deep(.listing-disable-btn) {
  background: #f56c6c;
}
</style>
