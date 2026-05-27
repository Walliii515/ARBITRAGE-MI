# AG Grid Vue3 使用指南

> 适用于 Arbitrage-Mi 项目的 AG Grid 社区版实现规范

## 核心能力

- **高性能表格渲染**：支持大数据量实时更新（diff + applyTransaction）
- **列状态持久化**：列宽、排序、可见性保存到 localStorage
- **外部过滤**：多条件组合过滤，支持动态开关
- **自定义列选择面板**：替代企业版 columnsMenu，复选框控制列显隐
- **单元格格式化**：数值、百分比、日期等自定义显示
- **单元格样式动态化**：根据阈值变色（资金费率、盘口覆盖、边际基差）

## 关键实现

### 1. 模块注册（main.ts）

```typescript
import { ModuleRegistry, AllCommunityModule } from 'ag-grid-community'

ModuleRegistry.registerModules([AllCommunityModule])
```

⚠️ **社区版限制**：不要使用 `menuTabs`、`getMainMenuItems` 等企业版功能，会报 error #200

### 2. 列定义（columnDefs）

```typescript
const columnDefs = computed<ColDef<OrderBookRow>[]>(() => [
  { headerName: 'base_asset', field: 'base_asset', pinned: 'left', width: 90 },
  {
    headerName: '24h资金费率',
    field: 'funding_rate_24h',
    width: 120,
    type: 'numericColumn',
    cellClass: 'ag-right-aligned-cell',
    valueFormatter: (p) => p.value != null ? (p.value * 100).toFixed(4) + '%' : '',
    cellStyle: (params) => {
      // 动态样式：根据阈值变色
      const value = params.value as number | null
      const threshold = params.data?.thresholdField
      if (value == null) return { color: '#909399' }
      return value >= threshold ? { color: '#67c23a' } : { color: '#f56c6c' }
    },
  },
])
```

### 3. 默认列配置（defaultColDef）

```typescript
const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  enableRowGroup: false,  // 禁用企业版功能
  enablePivot: false,
  enableValue: false,
}
```

### 4. 列状态持久化

```typescript
const COLUMN_STATE_STORAGE_KEY = 'orderbook_column_state'

function saveColumnState() {
  if (!gridApi) return
  const columnState = gridApi.getColumnState()
  localStorage.setItem(COLUMN_STATE_STORAGE_KEY, JSON.stringify(columnState))
}

function loadColumnState() {
  if (!gridApi) return
  const saved = localStorage.getItem(COLUMN_STATE_STORAGE_KEY)
  if (!saved) return
  try {
    const columnState = JSON.parse(saved) as ColumnState[]
    gridApi.applyColumnState({ state: columnState, applyOrder: true })
  } catch {
    // ignore parse errors
  }
}

// 在 gridReady 时加载
function onGridReady(params: GridReadyEvent) {
  gridApi = params.api
  loadColumnState()
}
```

### 5. 自定义列选择面板（替代企业版）

**模板**：
```vue
<el-popover placement="bottom-end" :width="260" trigger="click">
  <template #reference>
    <el-button size="small">列选择</el-button>
  </template>
  <div class="column-picker">
    <div v-for="col in columnVisibilities" :key="col.colId" class="column-picker-item">
      <el-checkbox
        :model-value="col.visible"
        @change="(val) => toggleColumnVisibility(col.colId, !!val)"
      />
      <span class="column-picker-label">{{ col.headerName }}</span>
    </div>
  </div>
</el-popover>
```

**逻辑**：
```typescript
interface ColumnVisibility {
  colId: string
  headerName: string
  visible: boolean
}

const columnVisibilities = ref<ColumnVisibility[]>([])

function refreshColumnVisibilities() {
  if (!gridApi) return
  const states = gridApi.getColumnState()
  columnVisibilities.value = columnDefs.value
    .filter((col) => col.field && col.field !== 'actions')
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
  if (!gridApi) return
  gridApi.setColumnsVisible([colId], visible)
  // 更新本地状态（关键：保持复选框同步）
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) col.visible = visible
}
```

**样式**：
```css
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
```

### 6. 外部过滤（External Filter）

```typescript
// 过滤开关
const filterByFundingRate = ref<boolean>(true)
const filterByCoverage = ref<boolean>(true)

// 单个过滤函数
function fundingRateFilterFunc(params: any): boolean {
  if (!filterByFundingRate.value) return true
  const data = params.data as OrderBookRow
  if (!data) return true
  const fundingRate = data.funding_rate_24h
  const threshold = data.thresholdField as number | null | undefined
  if (fundingRate == null || threshold == null) return true
  return fundingRate >= threshold
}

function coverageFilterFunc(params: any): boolean {
  if (!filterByCoverage.value) return true
  const data = params.data as OrderBookRow
  if (!data) return true
  if (data.open_coverage == null) return true
  return data.open_coverage <= threshold
}

// 组合过滤
function combinedFilterFunc(params: any): boolean {
  return fundingRateFilterFunc(params) && coverageFilterFunc(params)
}

// 监听开关变化，通知 AG Grid 重新过滤
watch(filterByFundingRate, () => {
  if (gridApi) gridApi.onFilterChanged()
})
```

**模板绑定**：
```vue
<ag-grid-vue
  :isExternalFilterPresent="() => filterByFundingRate || filterByCoverage"
  :doesExternalFilterPass="combinedFilterFunc"
/>
```

### 7. 高效数据更新（diff + applyTransaction）

```typescript
// 维护索引
const rowsByContract = new Map<string, OrderBookRow>()
const rowVersion = ref(0) // 版本号：供 computed 感知更新

function applySnapshotRows(rows: OrderBookRow[], forceFull = false) {
  if (!gridApi) {
    // 首次加载
    rowsByContract.clear()
    for (const row of rows) rowsByContract.set(row.contract, row)
    rowVersion.value++
    return
  }

  const needFullReset = forceFull || rowsByContract.size === 0
  if (needFullReset) {
    const remove = Array.from(rowsByContract.values())
    rowsByContract.clear()
    for (const row of rows) rowsByContract.set(row.contract, row)
    rowVersion.value++
    if (remove.length > 0 || rows.length > 0) {
      gridApi.applyTransaction({ remove, add: rows })
    }
    return
  }

  // 增量更新：diff
  const { add, update, remove } = diffSnapshotRows(rowsByContract, rows)
  if (add.length === 0 && update.length === 0 && remove.length === 0) return

  for (const row of remove) rowsByContract.delete(row.contract)
  for (const row of rows) rowsByContract.set(row.contract, row)
  rowVersion.value++
  gridApi.applyTransaction({ add, update, remove })
}

function diffSnapshotRows(
  prev: Map<string, OrderBookRow>,
  next: OrderBookRow[],
): { add: OrderBookRow[]; update: OrderBookRow[]; remove: OrderBookRow[] } {
  const nextIds = new Set(next.map((r) => r.contract))
  const add: OrderBookRow[] = []
  const update: OrderBookRow[] = []
  for (const row of next) {
    const old = prev.get(row.contract)
    if (!old) add.push(row)
    else if (rowChanged(old, row)) update.push(row)
  }
  const remove: OrderBookRow[] = []
  for (const [contract, row] of prev) {
    if (!nextIds.has(contract)) remove.push(row)
  }
  return { add, update, remove }
}

function rowChanged(prev: OrderBookRow, next: OrderBookRow): boolean {
  for (const key of Object.keys(next)) {
    if (prev[key] !== next[key]) return true
  }
  return false
}
```

### 8. 行 ID 配置

```typescript
const getRowId = (params: GetRowIdParams<OrderBookRow>) =>
  String(params.data?.contract ?? '')

// 模板
<ag-grid-vue :getRowId="getRowId" />
```

### 9. 响应式行数据（抽屉等场景）

```typescript
// 使用 shallowRef 避免深度响应式开销
const rowData = shallowRef<OrderBookRow[]>([])

// computed 依赖版本号感知更新
const drawerRow = computed(() => {
  void rowVersion.value // 建立响应式依赖
  return rowsByContract.get(drawerContract.value) ?? null
})
```

### 10. 数值格式化

```typescript
// 按实际精度展示，去掉多余尾随 0
function formatDecimal(value: number, maxDecimals = 12): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(maxDecimals).replace(/\.?0+$/, '')
}

const priceFormatter = (params: { value: number | null }) => {
  if (params.value == null) return ''
  return formatDecimal(params.value)
}

const percentFormatter = (params: ValueFormatterParams) => {
  const n = params.value
  if (n == null) return ''
  return (n * 100).toFixed(1) + '%'
}
```

### 11. Cmd+C 复制 & 右键菜单（复制单元格 / 导出 CSV）

使用 `useGridCopy` 组合式函数，为任意 AG Grid 表格添加：
- **Cmd+C / Ctrl+C**：复制当前聚焦单元格的 `valueFormatter` 格式化显示值到剪贴板
- **右键菜单**：复制单元格、导出 CSV

```typescript
import { useGridCopy } from '../ag-grid/useGridCopy'

// 在 setup 中调用
const { gridContainerRef, setupGridCopy } = useGridCopy()

// 在 onGridReady 中绑定
function onGridReady(params: GridReadyEvent) {
  gridApi = params.api
  loadColumnState()
  setupGridCopy(params.api)  // 绑定键盘和右键监听
}
```

**模板**：用 `ref="gridContainerRef"` 包裹 `<ag-grid-vue>`：
```vue
<div ref="gridContainerRef">
  <ag-grid-vue ... />
</div>
```

**关键实现细节**：
- 监听器通过 capture 阶段拦截 `keydown`，确保在浏览器默认复制前处理
- 复制内容使用列的 `valueFormatter` 格式化结果，确保复制值与显示值一致
- 右键菜单通过 `document.body.appendChild` 挂载，避免父元素 `overflow: hidden` 截断
- `onUnmounted` 自动清理监听器和菜单 DOM
- **导出 CSV 使用 AG Grid 内置的 `api.exportDataAsCsv()`**

### 12. 长文本悬浮提示（自定义 Tooltip）

对于「开仓原因」「平仓原因」等文本较长、单元格无法完整展示的列，使用自定义 Tooltip 组件：

**组件**：`frontend/src/ag-grid/LongTextTooltip.vue`（`max-width: 480px`、`white-space: pre-wrap`、自动换行）

```vue
<script setup lang="ts">
import type { ITooltipParams } from 'ag-grid-community'

defineProps<{ params: ITooltipParams }>()
</script>

<template>
  <!-- ⚠️ 必须带 ag-tooltip 类！AG Grid v35+ 以此判断是否为标准 tooltip -->
  <div class="ag-tooltip long-text-tooltip">
    {{ params.value }}
  </div>
</template>

<style>
.long-text-tooltip {
  max-width: 480px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  background: #1e2527;
  border: 1px solid #3a3f44;
  border-radius: 4px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  padding: 10px 14px;
}
</style>
```

**列定义中配置**：
```typescript
import LongTextTooltip from '../ag-grid/LongTextTooltip.vue'

{
  headerName: '开仓原因',
  field: 'open_reason',
  width: 280,
  tooltipField: 'open_reason',          // 指定 tooltip 显示的字段
  tooltipComponent: LongTextTooltip,     // 自定义组件
}
```

**表格级配置**：
```vue
<ag-grid-vue :tooltipShowDelay="300" ... />
```

⚠️ 如果使用了 `cellRenderer`，还需配合 `tooltipValueGetter` 确保 tooltip 获取正确的值：
```typescript
{
  field: 'reject_reason',
  tooltipComponent: LongTextTooltip,
  tooltipValueGetter: (params) => params.data?.reject_reason ?? null,
  cellRenderer: (params) => params.value ?? '',
}
```

## 常见问题

### Q1: 报错 `error #200 Unable to use menuTabs as ColumnMenuModule is not registered`

**原因**：使用了企业版功能，但项目只注册了社区版模块

**解决**：
- 删除 `defaultColDef` 中的 `menuTabs` 配置
- 删除 `:getMainMenuItems` 属性
- 使用自定义列选择面板（见第 5 节）

### Q2: 列选择复选框状态不同步

**原因**：调用 `setColumnsVisible` 后未更新本地状态

**解决**：
```typescript
function toggleColumnVisibility(colId: string, visible: boolean) {
  gridApi.setColumnsVisible([colId], visible)
  const col = columnVisibilities.value.find((c) => c.colId === colId)
  if (col) col.visible = visible // 必须同步更新
}
```

### Q3: 滚动条与复选框重叠

**原因**：容器 padding 不足，flex 布局未正确处理

**解决**：
```css
.column-picker {
  padding: 4px 8px;
  margin-right: 4px; /* 给滚动条留空间 */
}
.column-picker-item :deep(.el-checkbox) {
  flex-shrink: 0;
  margin-right: 4px; /* 复选框固定宽度 */
}
```

### Q4: 数据更新后滚动位置丢失

**原因**：使用 `setGridOption('rowData', ...)` 会整表重绘

**解决**：使用 `applyTransaction` 增量更新（见第 7 节）

### Q5: 自定义 tooltip 没有背景/边框，文字叠在一起

**原因**：AG Grid v35+ 检查自定义 tooltip 组件根元素是否带有 `ag-tooltip` class。
如果没有，会额外添加 `ag-tooltip-custom`，导致默认容器样式不生效。

**解决**：
```vue
<!-- 必须同时声明 ag-tooltip 和自定义 class -->
<div class="ag-tooltip long-text-tooltip">
  {{ params.value }}
</div>
```

### Q6: 使用了 cellRenderer 的列，tooltip 显示空值

**原因**：`tooltipField` 依赖原始字段值，而 `cellRenderer` 可能改变了渲染内容，两者不联动。

**解决**：同时配置 `tooltipValueGetter`：
```typescript
{
  field: 'reject_reason',
  tooltipField: 'reject_reason',
  tooltipValueGetter: (params) => params.data?.reject_reason ?? null,
  tooltipComponent: LongTextTooltip,
  cellRenderer: (params) => params.value ?? '',
}
```

## 参考文件

- `frontend/src/views/OrderBookMonitor.vue` - 完整实现示例
- `frontend/src/ag-grid/orderbookGridTheme.ts` - 主题配置
- `frontend/src/main.ts` - 模块注册
- `frontend/src/ag-grid/useGridCopy.ts` - Cmd+C 复制 & 右键菜单组合式函数
- `frontend/src/ag-grid/LongTextTooltip.vue` - 长文本自定义 Tooltip 组件
