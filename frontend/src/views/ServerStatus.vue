<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { Refresh } from '@element-plus/icons-vue'
import { get } from '../utils/request'
import { showError } from '../utils/message'

interface ServerMetricRow {
  id: number
  snapshot_at: string
  hostname: string
  cpu_usage_percent: number | null
  load1: number | null
  load5: number | null
  load15: number | null
  cpu_count: number | null
  memory_total_bytes: number | null
  memory_used_bytes: number | null
  memory_usage_percent: number | null
  disk_path: string
  disk_total_bytes: number | null
  disk_used_bytes: number | null
  disk_usage_percent: number | null
  uptime_sec: number | null
  detail?: Record<string, unknown>
}

interface DiskFilesystem {
  source: string
  mount_point: string
  mount_points?: string[]
  fstype?: string
  total_bytes: number | null
  used_bytes: number | null
  free_bytes: number | null
  usage_percent: number | null
}

const rows = ref<ServerMetricRow[]>([])
const latest = ref<ServerMetricRow | null>(null)
const loading = ref(false)
const sampleIntervalSec = ref(3600)
const usageChartRef = ref<HTMLDivElement | null>(null)
const loadChartRef = ref<HTMLDivElement | null>(null)
let usageChart: ECharts | null = null
let loadChart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

const hasRows = computed(() => rows.value.length > 0)
const lastUpdatedAt = computed(() => latest.value?.snapshot_at || '-')
const hostLabel = computed(() => latest.value?.hostname || '-')
const sampleCount = computed(() => rows.value.length)
const diskFilesystems = computed<DiskFilesystem[]>(() => {
  const filesystems = latest.value?.detail?.disk_filesystems
  if (!Array.isArray(filesystems)) return []
  return filesystems
    .map((item) => item as DiskFilesystem)
    .filter((item) => item?.mount_point && item?.total_bytes != null)
})
const diskScopeLabel = computed(() => {
  if (latest.value?.disk_path === 'all') return '全局本地磁盘'
  return latest.value?.disk_path || '/'
})
const systemDisk = computed(() => diskFilesystems.value.find((item) => item.mount_point === '/') || null)
const dataDisk = computed(() =>
  diskFilesystems.value.find((item) =>
    item.mount_point === '/data' || item.mount_points?.includes('/var/lib/mysql')
  ) || null
)

function diskFromRow(row: ServerMetricRow, mountPoint: string): DiskFilesystem | null {
  const filesystems = row.detail?.disk_filesystems
  if (!Array.isArray(filesystems)) return null
  return (filesystems as DiskFilesystem[]).find((item) => item.mount_point === mountPoint) || null
}

const statusItems = computed(() => {
  const item = latest.value
  return [
    {
      label: 'CPU使用率',
      value: formatPercent(item?.cpu_usage_percent),
      raw: item?.cpu_usage_percent,
      tone: usageTone(item?.cpu_usage_percent),
      sub: item?.cpu_count ? `${item.cpu_count} 核 / Load ${formatNumber(item.load1, 2)}` : 'Load -',
    },
    {
      label: '内存使用率',
      value: formatPercent(item?.memory_usage_percent),
      raw: item?.memory_usage_percent,
      tone: usageTone(item?.memory_usage_percent),
      sub: `${formatBytes(item?.memory_used_bytes)} / ${formatBytes(item?.memory_total_bytes)}`,
    },
    {
      label: '系统盘',
      value: formatPercent(systemDisk.value?.usage_percent),
      raw: systemDisk.value?.usage_percent,
      tone: usageTone(systemDisk.value?.usage_percent),
      sub: `/ · ${formatBytes(systemDisk.value?.used_bytes)} / ${formatBytes(systemDisk.value?.total_bytes)}`,
    },
    {
      label: '数据盘',
      value: formatPercent(dataDisk.value?.usage_percent),
      raw: dataDisk.value?.usage_percent,
      tone: usageTone(dataDisk.value?.usage_percent),
      sub: `/data · ${formatBytes(dataDisk.value?.used_bytes)} / ${formatBytes(dataDisk.value?.total_bytes)}`,
    },
    {
      label: '全局磁盘',
      value: formatPercent(item?.disk_usage_percent),
      raw: item?.disk_usage_percent,
      tone: usageTone(item?.disk_usage_percent),
      sub: `${diskScopeLabel.value} · ${formatBytes(item?.disk_used_bytes)} / ${formatBytes(item?.disk_total_bytes)}`,
    },
    {
      label: '运行时长',
      value: formatUptime(item?.uptime_sec),
      raw: null,
      tone: 'neutral',
      sub: String(item?.detail?.platform || '-'),
    },
  ]
})

function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return `${Number(value).toFixed(1)}%`
}

function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toFixed(digits)
}

function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = Number(value)
  let unit = 0
  while (n >= 1024 && unit < units.length - 1) {
    n /= 1024
    unit += 1
  }
  return `${n.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

function formatUptime(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  const days = Math.floor(Number(value) / 86400)
  const hours = Math.floor((Number(value) % 86400) / 3600)
  if (days > 0) return `${days}天${hours}小时`
  return `${hours}小时`
}

function formatTooltipTime(value: unknown): string {
  const date = new Date(String(value))
  if (!Number.isFinite(date.getTime())) return String(value ?? '-')
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function usageTone(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return 'neutral'
  if (Number(value) >= 90) return 'danger'
  if (Number(value) >= 75) return 'warning'
  return 'ok'
}

function chartColors() {
  const styles = getComputedStyle(document.documentElement)
  return {
    text: styles.getPropertyValue('--app-text').trim() || '#e8eaed',
    muted: styles.getPropertyValue('--app-text-muted').trim() || '#9aa0a6',
    border: styles.getPropertyValue('--app-border').trim() || '#333333',
    surface: styles.getPropertyValue('--app-surface').trim() || '#181d1f',
  }
}

function buildUsageChartOption(): EChartsOption {
  const colors = chartColors()
  const series = [
    { name: 'CPU', color: '#4dabf7', value: (row: ServerMetricRow) => row.cpu_usage_percent },
    { name: '内存', color: '#63e6be', value: (row: ServerMetricRow) => row.memory_usage_percent },
    { name: '系统盘', color: '#ffd43b', value: (row: ServerMetricRow) => diskFromRow(row, '/')?.usage_percent ?? null },
    { name: '数据盘', color: '#ffa94d', value: (row: ServerMetricRow) => diskFromRow(row, '/data')?.usage_percent ?? null },
    { name: '全局磁盘', color: '#adb5bd', value: (row: ServerMetricRow) => row.disk_usage_percent },
  ]

  return {
    color: series.map((item) => item.color),
    animation: false,
    grid: { top: 42, right: 34, bottom: 42, left: 54 },
    legend: { top: 0, textStyle: { color: colors.muted } },
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: colors.surface,
      borderColor: colors.border,
      textStyle: { color: colors.text },
      formatter: (params) => {
        const items: any[] = Array.isArray(params) ? params : [params]
        const title = formatTooltipTime(items[0]?.axisValue)
        const lines = items.map((item) => {
          const value = Array.isArray(item.value) ? item.value[1] : item.value
          return `${item.marker}${item.seriesName}: ${formatPercent(Number(value))}`
        })
        return [title, ...lines].join('<br/>')
      },
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: colors.border } },
      axisTick: { show: false },
      axisLabel: { color: colors.muted },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { color: colors.muted, formatter: '{value}%' },
      splitLine: { lineStyle: { color: colors.border, type: 'dashed' } },
    },
    series: series.map((item) => ({
      name: item.name,
      type: 'line',
      smooth: true,
      showSymbol: false,
      emphasis: { focus: 'series' },
      data: rows.value.map((row) => [row.snapshot_at, item.value(row)]),
      lineStyle: { width: 2.2 },
    })),
  }
}

function buildLoadChartOption(): EChartsOption {
  const colors = chartColors()
  const series = [
    { name: 'Load 1m', key: 'load1', color: '#ff8787' },
    { name: 'Load 5m', key: 'load5', color: '#b197fc' },
    { name: 'Load 15m', key: 'load15', color: '#74c0fc' },
  ] as const

  return {
    color: series.map((item) => item.color),
    animation: false,
    grid: { top: 42, right: 34, bottom: 42, left: 54 },
    legend: { top: 0, textStyle: { color: colors.muted } },
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: colors.surface,
      borderColor: colors.border,
      textStyle: { color: colors.text },
      formatter: (params) => {
        const items: any[] = Array.isArray(params) ? params : [params]
        const title = formatTooltipTime(items[0]?.axisValue)
        const lines = items.map((item) => {
          const value = Array.isArray(item.value) ? item.value[1] : item.value
          return `${item.marker}${item.seriesName}: ${formatNumber(Number(value), 2)}`
        })
        return [title, ...lines].join('<br/>')
      },
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: colors.border } },
      axisTick: { show: false },
      axisLabel: { color: colors.muted },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: 0,
      scale: true,
      axisLabel: { color: colors.muted },
      splitLine: { lineStyle: { color: colors.border, type: 'dashed' } },
    },
    series: series.map((item) => ({
      name: item.name,
      type: 'line',
      smooth: true,
      showSymbol: false,
      emphasis: { focus: 'series' },
      data: rows.value.map((row) => [row.snapshot_at, row[item.key] ?? null]),
      lineStyle: { width: 2.2 },
    })),
  }
}

function updateCharts() {
  usageChart?.setOption(buildUsageChartOption(), true)
  loadChart?.setOption(buildLoadChartOption(), true)
}

async function initCharts() {
  await nextTick()
  if (usageChartRef.value) usageChart = echarts.init(usageChartRef.value)
  if (loadChartRef.value) loadChart = echarts.init(loadChartRef.value)
  updateCharts()
  resizeObserver = new ResizeObserver(() => {
    usageChart?.resize()
    loadChart?.resize()
  })
  if (usageChartRef.value) resizeObserver.observe(usageChartRef.value)
  if (loadChartRef.value) resizeObserver.observe(loadChartRef.value)
}

async function fetchMetrics() {
  loading.value = true
  try {
    const res = await get('/api/service/server-metrics?days=7')
    const data = await res.json()
    if (!res.ok || data?.ok === false) {
      showError(data?.detail || '服务器状态加载失败')
      return
    }
    rows.value = Array.isArray(data.items) ? data.items : []
    latest.value = data.latest || rows.value.at(-1) || null
    sampleIntervalSec.value = Number(data.sample_interval_sec || 3600)
  } catch (e: any) {
    showError(e?.message || '服务器状态加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await fetchMetrics()
  await initCharts()
  refreshTimer = setInterval(fetchMetrics, 5 * 60_000)
})

watch(rows, () => updateCharts())

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  resizeObserver?.disconnect()
  resizeObserver = null
  usageChart?.dispose()
  loadChart?.dispose()
  usageChart = null
  loadChart = null
})
</script>

<template>
  <div class="server-status-page">
    <div class="toolbar">
      <div class="page-title">
        <h2>服务器状态</h2>
        <span>{{ hostLabel }}</span>
      </div>
      <div class="toolbar-actions">
        <span class="meta-text">最近7天 · {{ sampleCount }} 个采样点 · {{ Math.round(sampleIntervalSec / 60) }}分钟/次</span>
        <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchMetrics">刷新</el-button>
      </div>
    </div>

    <div class="summary-grid">
      <div v-for="item in statusItems" :key="item.label" class="metric-card" :class="`tone-${item.tone}`">
        <div class="metric-head">
          <span>{{ item.label }}</span>
          <i></i>
        </div>
        <strong>{{ item.value }}</strong>
        <p>{{ item.sub }}</p>
      </div>
    </div>

    <div class="chart-grid">
      <section class="chart-panel">
        <div class="chart-header">
          <h3>资源使用率</h3>
          <span>最后更新：{{ lastUpdatedAt }}</span>
        </div>
        <div class="chart-wrap">
          <div ref="usageChartRef" class="echarts-chart"></div>
          <div v-if="!hasRows" class="empty-text">暂无服务器指标快照</div>
        </div>
      </section>

      <section class="chart-panel">
        <div class="chart-header">
          <h3>系统负载</h3>
          <span>CPU核心数：{{ latest?.cpu_count || '-' }}</span>
        </div>
        <div class="chart-wrap">
          <div ref="loadChartRef" class="echarts-chart"></div>
          <div v-if="!hasRows" class="empty-text">暂无服务器指标快照</div>
        </div>
      </section>
    </div>

    <section class="disk-panel">
      <div class="chart-header">
        <h3>磁盘明细</h3>
        <span>按本地块设备去重汇总，bind mount 不重复计入总量</span>
      </div>
      <div class="disk-list">
        <div v-for="fs in diskFilesystems" :key="`${fs.source}-${fs.mount_point}`" class="disk-row">
          <div class="disk-row-main">
            <strong>{{ fs.mount_point }}</strong>
            <span>{{ fs.source }} {{ fs.fstype || '' }}</span>
          </div>
          <div class="disk-row-meter">
            <div class="disk-bar">
              <i :style="{ width: `${Math.min(100, Math.max(0, Number(fs.usage_percent || 0)))}%` }"></i>
            </div>
            <span>{{ formatPercent(fs.usage_percent) }}</span>
          </div>
          <div class="disk-row-size">
            {{ formatBytes(fs.used_bytes) }} / {{ formatBytes(fs.total_bytes) }}
          </div>
        </div>
        <div v-if="!diskFilesystems.length" class="disk-empty">暂无磁盘明细</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.server-status-page {
  min-height: 100%;
  padding: 20px;
  background: var(--app-bg);
  color: var(--app-text);
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.page-title {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
}

.page-title h2 {
  font-size: 20px;
  font-weight: 600;
}

.page-title span,
.meta-text,
.chart-header span {
  color: var(--app-text-muted);
  font-size: 13px;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.metric-card,
.chart-panel {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

.metric-card {
  padding: 14px;
  min-width: 0;
}

.metric-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--app-text-muted);
  font-size: 13px;
}

.metric-head i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #909399;
}

.metric-card strong {
  display: block;
  margin-top: 10px;
  font-size: 25px;
  line-height: 1.1;
  font-weight: 650;
}

.metric-card p {
  margin-top: 8px;
  min-height: 18px;
  overflow: hidden;
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tone-ok .metric-head i {
  background: #63e6be;
}

.tone-warning .metric-head i {
  background: #ffd43b;
}

.tone-danger .metric-head i {
  background: #ff6b6b;
}

.chart-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.chart-panel {
  min-width: 0;
  padding: 14px;
}

.disk-panel {
  margin-top: 16px;
  padding: 14px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.chart-header h3 {
  font-size: 15px;
  font-weight: 600;
}

.chart-wrap {
  position: relative;
  height: 340px;
}

.echarts-chart {
  width: 100%;
  height: 100%;
}

.empty-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--app-text-muted);
  font-size: 13px;
  pointer-events: none;
}

.disk-list {
  display: grid;
  gap: 10px;
}

.disk-row {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) minmax(180px, 280px) minmax(140px, auto);
  align-items: center;
  gap: 16px;
  padding: 10px 12px;
  background: var(--app-surface-elevated);
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

.disk-row-main {
  min-width: 0;
}

.disk-row-main strong {
  display: block;
  font-size: 14px;
  font-weight: 600;
}

.disk-row-main span,
.disk-empty {
  display: block;
  margin-top: 4px;
  overflow: hidden;
  color: var(--app-text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.disk-row-meter {
  display: grid;
  grid-template-columns: 1fr 52px;
  align-items: center;
  gap: 10px;
  color: var(--app-text-muted);
  font-size: 12px;
}

.disk-bar {
  height: 8px;
  overflow: hidden;
  background: #2b3134;
  border-radius: 999px;
}

.disk-bar i {
  display: block;
  height: 100%;
  background: #ffd43b;
  border-radius: inherit;
}

.disk-row-size {
  color: var(--app-text);
  font-size: 13px;
  text-align: right;
  white-space: nowrap;
}

@media (max-width: 1180px) {
  .summary-grid,
  .chart-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .disk-row {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .disk-row-size {
    text-align: left;
  }
}

@media (max-width: 760px) {
  .server-status-page {
    padding: 12px;
  }

  .toolbar,
  .toolbar-actions,
  .chart-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .summary-grid,
  .chart-grid {
    grid-template-columns: 1fr;
  }

  .chart-wrap {
    height: 300px;
  }
}
</style>
