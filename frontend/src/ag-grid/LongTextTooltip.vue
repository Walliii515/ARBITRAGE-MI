<script setup lang="ts">
import type { ITooltipParams } from 'ag-grid-community'

/**
 * AG Grid 自定义 Tooltip 组件 —— 长文本自动换行
 * 用于「开仓原因」「平仓原因」等长文本列
 *
 * 使用方式（列定义中配置）：
 *   tooltipComponent: LongTextTooltip,
 *   tooltipShowDelay: 300,
 *   tooltipField: 'open_reason',
 */
defineProps<{ params: ITooltipParams }>()
</script>

<template>
  <div class="ag-tooltip long-text-tooltip">
    {{ params.value }}
  </div>
</template>

<style>
/* 全局样式 —— AG Grid tooltip 容器挂载在 body 下，scoped 无法穿透 */
.long-text-tooltip {
  max-width: 480px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  font-size: 13px;
  color: #e8eaed;
  background: #1e2527;
  border: 1px solid #3a3f44;
  border-radius: 4px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  padding: 10px 14px;
}
</style>
