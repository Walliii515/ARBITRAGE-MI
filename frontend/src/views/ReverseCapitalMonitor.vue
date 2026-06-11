<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { get } from '../utils/request'
import { showError } from '../utils/message'

interface MarginAsset {
  asset: string
  free: number
  locked: number
  borrowed: number
  interest: number
  netAsset: number
}

interface ReverseCapitalSnapshot {
  strategy: string
  timestamp: number
  errors?: Record<string, string>
  binance_cross_margin?: {
    borrowEnabled: boolean | null
    tradeEnabled: boolean | null
    marginLevel: number
    totalAssetOfBtc: number
    totalLiabilityOfBtc: number
    totalNetAssetOfBtc: number
    USDT: MarginAsset
    BNB: MarginAsset
    nonzero_assets: MarginAsset[]
  }
  gate_futures?: {
    available: number
    total: number
    unrealised_pnl: number
    position_margin: number
    order_margin: number
  }
}

const loading = ref(false)
const snapshot = ref<ReverseCapitalSnapshot | null>(null)

const margin = computed(() => snapshot.value?.binance_cross_margin)
const gate = computed(() => snapshot.value?.gate_futures)
const nonzeroAssets = computed(() => margin.value?.nonzero_assets ?? [])
const errors = computed(() => snapshot.value?.errors ?? {})

function formatAmount(value: number | null | undefined, decimals = 4): string {
  if (value == null || !Number.isFinite(Number(value))) return '-'
  return Number(value).toLocaleString('en-US', { maximumFractionDigits: decimals })
}

function formatBool(value: boolean | null | undefined): string {
  if (value === true) return '正常'
  if (value === false) return '关闭'
  return '-'
}

function formatTime(ts: number | null | undefined): string {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return '-'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

async function fetchSnapshot() {
  loading.value = true
  try {
    const res = await get('/api/trading/reverse-capital')
    const data = await res.json()
    if (!res.ok) {
      showError(data?.detail || '反向资金加载失败')
      return
    }
    snapshot.value = data
  } catch {
    showError('反向资金加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(fetchSnapshot)
</script>

<template>
  <div class="reverse-capital-page">
    <div class="page-toolbar">
      <span class="updated-at">最后更新：{{ formatTime(snapshot?.timestamp) }}</span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchSnapshot">刷新</el-button>
    </div>

    <div v-if="Object.keys(errors).length" class="error-strip">
      <span v-for="(message, key) in errors" :key="key">{{ key }}: {{ message }}</span>
    </div>

    <section class="section">
      <div class="section-title">Binance Cross Margin</div>
      <div class="metric-grid">
        <div class="metric">
          <span class="metric-label">USDT 可用</span>
          <span class="metric-value">{{ formatAmount(margin?.USDT?.free, 2) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">BNB 可用</span>
          <span class="metric-value">{{ formatAmount(margin?.BNB?.free, 8) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">杠杆风险率</span>
          <span class="metric-value">{{ formatAmount(margin?.marginLevel, 4) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">借币权限</span>
          <span class="metric-value">{{ formatBool(margin?.borrowEnabled) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">交易权限</span>
          <span class="metric-value">{{ formatBool(margin?.tradeEnabled) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">净资产BTC</span>
          <span class="metric-value">{{ formatAmount(margin?.totalNetAssetOfBtc, 8) }}</span>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section-title">Gate Futures</div>
      <div class="metric-grid">
        <div class="metric">
          <span class="metric-label">可用 USDT</span>
          <span class="metric-value">{{ formatAmount(gate?.available, 2) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">总权益</span>
          <span class="metric-value">{{ formatAmount(gate?.total, 2) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">未实现盈亏</span>
          <span class="metric-value">{{ formatAmount(gate?.unrealised_pnl, 4) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">持仓保证金</span>
          <span class="metric-value">{{ formatAmount(gate?.position_margin, 4) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">挂单保证金</span>
          <span class="metric-value">{{ formatAmount(gate?.order_margin, 4) }}</span>
        </div>
      </div>
    </section>

    <section class="section assets-section">
      <div class="section-title">Binance 非零资产</div>
      <el-table :data="nonzeroAssets" size="small" height="100%" class="asset-table" empty-text="暂无非零资产">
        <el-table-column prop="asset" label="资产" min-width="90" />
        <el-table-column label="可用" min-width="120" align="right">
          <template #default="{ row }">{{ formatAmount(row.free, 8) }}</template>
        </el-table-column>
        <el-table-column label="锁定" min-width="120" align="right">
          <template #default="{ row }">{{ formatAmount(row.locked, 8) }}</template>
        </el-table-column>
        <el-table-column label="已借" min-width="120" align="right">
          <template #default="{ row }">{{ formatAmount(row.borrowed, 8) }}</template>
        </el-table-column>
        <el-table-column label="利息" min-width="120" align="right">
          <template #default="{ row }">{{ formatAmount(row.interest, 8) }}</template>
        </el-table-column>
        <el-table-column label="净资产" min-width="120" align="right">
          <template #default="{ row }">{{ formatAmount(row.netAsset, 8) }}</template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.reverse-capital-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
}

.page-toolbar,
.metric-grid {
  display: flex;
  align-items: center;
}

.page-toolbar {
  gap: 12px;
}

.updated-at {
  color: var(--app-text-muted);
  font-size: 14px;
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

.section {
  border: 1px solid var(--app-border);
  border-radius: 4px;
  background: var(--app-surface);
  padding: 14px;
}

.section-title {
  margin-bottom: 12px;
  color: var(--app-text);
  font-size: 15px;
  font-weight: 600;
}

.metric-grid {
  gap: 10px;
  flex-wrap: wrap;
}

.metric {
  min-width: 160px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--app-border);
  border-radius: 4px;
}

.metric-label {
  color: var(--app-text-muted);
  font-size: 12px;
}

.metric-value {
  color: var(--app-text);
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.assets-section {
  min-height: 0;
  flex: 1;
}

.asset-table {
  background: transparent;
}
</style>
