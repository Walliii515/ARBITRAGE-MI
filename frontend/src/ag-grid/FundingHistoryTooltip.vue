<script setup lang="ts">
import { computed } from 'vue'
import type { ITooltipParams } from 'ag-grid-community'

/**
 * AG Grid 自定义 Tooltip 组件 —— 资金费结算明细
 * 展示每次资金费结算的序号、本次结算费率、金额
 *
 * 使用方式（列定义中配置）：
 *   tooltipComponent: FundingHistoryTooltip,
 *   tooltipValueGetter: (params) => params.data?.funding_history,
 */
const props = defineProps<{ params: ITooltipParams }>()

const totalPnl = computed(() => {
  const history = props.params?.value
  if (!Array.isArray(history)) return 0
  return history.reduce((sum: number, item: any) => sum + (item.pnl || 0), 0)
})

const totalRate = computed(() => {
  const history = props.params?.value
  if (!Array.isArray(history)) return 0
  return history.reduce((sum: number, item: any) => sum + (item.rate || 0), 0)
})

function formatAbsBps(value: unknown): string {
  const rate = Number(value)
  if (!Number.isFinite(rate)) return '—'
  return (Math.abs(rate) * 10000).toFixed(2)
}

function formatPnl(value: unknown): string {
  const pnl = Number(value)
  if (!Number.isFinite(pnl)) return '—'
  return pnl.toFixed(4)
}
</script>

<template>
  <div class="ag-tooltip funding-history-tooltip" v-if="params.value && params.value.length > 0">
    <div class="fh-title">资金费结算明细</div>
    <div class="fh-table-wrap">
      <table class="fh-table">
        <thead>
          <tr>
            <th>次</th>
            <th>本次费率bps</th>
            <th>金额(USDT)</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in params.value" :key="item.seq">
            <td class="fh-seq">{{ item.seq }}</td>
            <td class="fh-rate" :class="{ positive: item.rate > 0, negative: item.rate < 0 }">
              {{ formatAbsBps(item.rate) }}
            </td>
            <td class="fh-pnl" :class="{ positive: item.pnl > 0, negative: item.pnl < 0 }">
              {{ formatPnl(item.pnl) }}
            </td>
            <td class="fh-time">{{ item.time || '—' }}</td>
          </tr>
        </tbody>
        <tfoot>
          <tr class="fh-summary">
            <td class="fh-summary-label">合计 ({{ params.value.length }}次)</td>
            <td class="fh-rate" :class="{ positive: totalRate > 0, negative: totalRate < 0 }">
              {{ formatAbsBps(totalRate) }}
            </td>
            <td class="fh-pnl" :class="{ positive: totalPnl > 0, negative: totalPnl < 0 }">
              {{ formatPnl(totalPnl) }}
            </td>
            <td></td>
          </tr>
        </tfoot>
      </table>
    </div>
  </div>
  <div class="ag-tooltip funding-history-tooltip fh-empty" v-else>
    暂无资金费结算记录
  </div>
</template>

<style>
/* 全局样式 —— AG Grid tooltip 容器挂载在 body 下，scoped 无法穿透 */
.funding-history-tooltip {
  width: 640px;
  max-width: calc(100vw - 48px);
  font-size: 12px;
  color: #e8eaed;
  background: #1e2527;
  border: 1px solid #3a3f44;
  border-radius: 4px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  padding: 10px 12px;
}

.funding-history-tooltip.fh-empty {
  color: #909399;
  padding: 8px 12px;
}

.fh-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: #c9d1d9;
}

.fh-table-wrap {
  max-height: min(36vh, 340px);
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: #4b5560 #1e2527;
}

.fh-table-wrap::-webkit-scrollbar {
  width: 8px;
}

.fh-table-wrap::-webkit-scrollbar-track {
  background: #1e2527;
}

.fh-table-wrap::-webkit-scrollbar-thumb {
  background: #4b5560;
  border-radius: 4px;
}

.fh-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

.fh-table th {
  text-align: right;
  padding: 3px 8px;
  font-size: 11px;
  color: #8b949e;
  border-bottom: 1px solid #30363d;
}

.fh-table thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: #1e2527;
}

.fh-table th:first-child {
  text-align: center;
  width: 54px;
}

.fh-table th:nth-child(2),
.fh-table th:nth-child(3) {
  width: 138px;
}

.fh-table td {
  text-align: right;
  padding: 3px 8px;
  font-size: 12px;
  border-bottom: 1px solid #21262d;
}

.fh-table td:first-child {
  text-align: center;
}

.fh-seq {
  color: #8b949e;
}

.fh-rate.positive { color: #67c23a; }
.fh-rate.negative { color: #f56c6c; }

.fh-pnl.positive { color: #f56c6c; }
.fh-pnl.negative { color: #67c23a; }

.fh-time {
  color: #8b949e;
  font-size: 11px;
}

.fh-summary {
  border-top: 1px solid #30363d;
}

.fh-summary td {
  position: sticky;
  bottom: 0;
  z-index: 1;
  background: #1e2527;
  font-weight: 600;
  padding-top: 6px;
  border-bottom: none;
}

.fh-summary-label {
  text-align: left;
  color: #c9d1d9;
  white-space: nowrap;
}
</style>
