<script setup lang="ts">
import { computed } from 'vue'
import type { ITooltipParams } from 'ag-grid-community'

/**
 * AG Grid 自定义 Tooltip 组件 —— 资金费结算明细
 * 展示每次资金费结算的序号、费率、金额
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
</script>

<template>
  <div class="funding-history-tooltip" v-if="params.value && params.value.length > 0">
    <div class="fh-title">资金费结算明细</div>
    <table class="fh-table">
      <thead>
        <tr>
          <th>次</th>
          <th>8h费率</th>
          <th>金额(USDT)</th>
          <th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in params.value" :key="item.seq">
          <td class="fh-seq">{{ item.seq }}</td>
          <td class="fh-rate" :class="{ positive: item.rate > 0, negative: item.rate < 0 }">
            {{ (item.rate * 100).toFixed(4) }}%
          </td>
          <td class="fh-pnl" :class="{ positive: item.pnl > 0, negative: item.pnl < 0 }">
            {{ item.pnl.toFixed(4) }}
          </td>
          <td class="fh-time">{{ item.time || '—' }}</td>
        </tr>
      </tbody>
      <tfoot>
        <tr class="fh-summary">
          <td colspan="2">合计 ({{ params.value.length }}次)</td>
          <td class="fh-pnl" :class="{ positive: totalPnl > 0, negative: totalPnl < 0 }">
            {{ totalPnl.toFixed(4) }}
          </td>
          <td></td>
        </tr>
      </tfoot>
    </table>
  </div>
  <div class="funding-history-tooltip fh-empty" v-else>
    暂无资金费结算记录
  </div>
</template>

<style>
/* 全局样式 —— AG Grid tooltip 容器挂载在 body 下，scoped 无法穿透 */
.funding-history-tooltip {
  max-width: 520px;
  max-height: 400px;
  overflow-y: auto;
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

.fh-table {
  width: 100%;
  border-collapse: collapse;
}

.fh-table th {
  text-align: right;
  padding: 3px 8px;
  font-size: 11px;
  color: #8b949e;
  border-bottom: 1px solid #30363d;
}

.fh-table th:first-child {
  text-align: center;
  width: 30px;
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
  font-weight: 600;
  padding-top: 6px;
  border-bottom: none;
}

.fh-summary td:first-child {
  text-align: left;
  color: #c9d1d9;
}
</style>
