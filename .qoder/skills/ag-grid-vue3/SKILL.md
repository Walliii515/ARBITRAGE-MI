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

### 0. 数据库表结构（统一列配置管理）

**表名**：`ag_grid_column_config`

```sql
CREATE TABLE IF NOT EXISTS ag_grid_column_config (
  id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
  user_id VARCHAR(64) NOT NULL COMMENT '用户ID（支持多用户个性化配置）',
  page_key VARCHAR(64) NOT NULL COMMENT '页面标识，如 orderbook_monitor, position_monitor, order_management',
  col_id VARCHAR(128) NOT NULL COMMENT '列ID（对应AG Grid column.field 或 column.colId）',
  sort_order INT NOT NULL DEFAULT 0 COMMENT '显示顺序（升序排列）',
  is_visible TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否显示：1=显示，0=隐藏',
  width INT NULL COMMENT '列宽（px）',
  pinned VARCHAR(16) NULL COMMENT '固定位置：left / right / null',
  sort VARCHAR(16) NULL COMMENT '排序状态：asc / desc / null',
  filter_model JSON NULL COMMENT '筛选条件（AG Grid FilterModel 序列化）',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
  UNIQUE KEY uk_user_page_col (user_id, page_key, col_id),
  INDEX idx_user_page (user_id, page_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AG Grid列配置表';
```

**设计说明**：
- `user_id`：支持多用户各自的列配置偏好
- `page_key`：标识不同的AG Grid页面（如 `orderbook_monitor`、`position_monitor`、`order_management`、`vwap_threshold`、`connection_status`）
- `sort_order`：控制列的显示顺序
- `is_visible`：控制列的显隐
- `width` / `pinned` / `sort` / `filter_model`：完整的AG Grid列状态字段

**初始化脚本示例**（可选）：
```sql
-- 为 orderbook_monitor 页面初始化默认列配置
INSERT INTO ag_grid_column_config (user_id, page_key, col_id, sort_order, is_visible, width, pinned)
VALUES 
  ('default', 'orderbook_monitor', 'base_asset', 0, 1, 90, 'left'),
  ('default', 'orderbook_monitor', 'open_amount_usdt', 1, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'spot_qty', 2, 1, 110, NULL),
  ('default', 'orderbook_monitor', 'future_qty', 3, 1, 110, NULL),
  ('default', 'orderbook_monitor', 'funding_rate_24h', 4, 1, 120, NULL)
  -- ... 其他列
ON DUPLICATE KEY UPDATE sort_order = VALUES(sort_order);
```

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

### 4. 列状态持久化（数据库版）

**⚠️ 替换原 localStorage 方案，改用数据库API统一存储**

#### 4.1 后端 API 实现

**新增路由**：`src/api/trading_api.py`

```python
@router.get('/column-config/{page_key}')
def get_column_config(page_key: str, current_user: dict = Depends(get_current_user)):
    """获取指定页面的列配置"""
    user_id = current_user.get('user_id', 'default')
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            "SELECT col_id, sort_order, is_visible, width, pinned, sort, filter_model "
            "FROM ag_grid_column_config "
            "WHERE user_id = %s AND page_key = %s "
            "ORDER BY sort_order ASC",
            (user_id, page_key)
        )
        rows = cursor.fetchall()
    
    # 转换为AG Grid ColumnState格式
    column_state = []
    for row in rows:
        state = {
            'colId': row['col_id'],
            'order': row['sort_order'],
            'hide': not row['is_visible'],
        }
        if row['width'] is not None:
            state['width'] = row['width']
        if row['pinned']:
            state['pinned'] = row['pinned']
        if row['sort']:
            state['sort'] = row['sort']
        if row['filter_model']:
            import json
            state['filterModel'] = json.loads(row['filter_model'])
        column_state.append(state)
    
    return {'columnState': column_state}

@router.post('/column-config/{page_key}')
def save_column_config(page_key: str, payload: dict, current_user: dict = Depends(get_current_user)):
    """保存指定页面的列配置"""
    user_id = current_user.get('user_id', 'default')
    column_state = payload.get('columnState', [])
    
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        try:
            # 批量 upsert
            for item in column_state:
                cursor.execute(
                    "INSERT INTO ag_grid_column_config "
                    "(user_id, page_key, col_id, sort_order, is_visible, width, pinned, sort, filter_model) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "sort_order = VALUES(sort_order), "
                    "is_visible = VALUES(is_visible), "
                    "width = VALUES(width), "
                    "pinned = VALUES(pinned), "
                    "sort = VALUES(sort), "
                    "filter_model = VALUES(filter_model)",
                    (
                        user_id,
                        page_key,
                        item['colId'],
                        item.get('order', 0),
                        not item.get('hide', False),
                        item.get('width'),
                        item.get('pinned'),
                        item.get('sort'),
                        json.dumps(item.get('filterModel')) if item.get('filterModel') else None
                    )
                )
            return {'success': True}
        finally:
            cursor.close()
```

#### 4.2 前端实现

```typescript
import { get, post } from '../utils/request'

/** 从数据库加载列配置 */
async function loadColumnState(pageKey: string) {
  if (!gridApi) return
  try {
    const res = await get(`/api/column-config/${pageKey}`)
    if (res?.columnState && Array.isArray(res.columnState)) {
      gridApi.applyColumnState({ state: res.columnState, applyOrder: true })
    }
  } catch (e) {
    console.warn('Failed to load column config from server:', e)
  }
}

/** 保存列配置到数据库 */
async function saveColumnState(pageKey: string) {
  if (!gridApi) return
  const columnState = gridApi.getColumnState()
  try {
    await post(`/api/column-config/${pageKey}`, { columnState })
    showSuccess('列配置已保存')
  } catch (e) {
    showError('保存列配置失败')
  }
}

// 在 gridReady 时调用
function onGridReady(params: GridReadyEvent) {
  gridApi = params.api
  loadColumnState('orderbook_monitor') // 传入当前页面的 page_key
}
```

**使用示例（各页面）**：
```vue
<!-- OrderBookMonitor.vue -->
<el-button size="small" @click="saveColumnState('orderbook_monitor')">
  保存列配置
</el-button>

<!-- PositionMonitor.vue -->
<el-button size="small" @click="saveColumnState('position_monitor')">
  保存列配置
</el-button>
```

---

### 4.x 列状态持久化（localStorage 旧版 - 已废弃）

⚠️ **以下方案已被数据库方案替代，仅作为历史参考**

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

⚠️ 如果使用了 `cellRenderer`，必须用 `tooltipValueGetter` 确保 tooltip 获取正确的值（**不要**同时设置 `tooltipField`）：
```typescript
{
  field: 'reject_reason',
  // ⚠️ 不要加 tooltipField，会覆盖 tooltipValueGetter
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

**解决**：使用 `tooltipValueGetter`（**不要**同时设置 `tooltipField`，否则 `tooltipField` 优先级更高，会覆盖 `tooltipValueGetter` 的返回值，导致汇总行等自定义行 tooltip 失效）：
```typescript
{
  field: 'reject_reason',
  // ⚠️ 不要加 tooltipField，它会覆盖 tooltipValueGetter
  tooltipValueGetter: (params) => params.data?.reject_reason ?? null,
  tooltipComponent: LongTextTooltip,
  cellRenderer: (params) => params.value ?? '',
}
```

### Q7: 选中单元格的蓝色焦点边框只有 3 面（缺右侧）

**原因**：自定义 CSS 中 `.ag-cell { border-right: none !important }` 把竖分隔线去掉了，但 `!important` 同时覆盖了 AG Grid 的选中焦点边框样式。

**解决**：去掉 `!important`，然后为聚焦态单独恢复右边框：
```css
.orderbook-grid .ag-cell {
  border-right: none;  /* 不加 !important */
}

.orderbook-grid .ag-cell-focus {
  border-right: 1px solid var(--ag-range-selection-border-color, #2196f3) !important;
}
```

## 参考文件

- `frontend/src/views/OrderBookMonitor.vue` - 完整实现示例
- `frontend/src/ag-grid/orderbookGridTheme.ts` - 主题配置
- `frontend/src/main.ts` - 模块注册
- `frontend/src/ag-grid/useGridCopy.ts` - Cmd+C 复制 & 右键菜单组合式函数
- `frontend/src/ag-grid/LongTextTooltip.vue` - 长文本自定义 Tooltip 组件
- `src/api/trading_api.py` - 列配置API端点（GET/POST `/column-config/{page_key}`）
- `src/common/database.py` - 数据库连接管理器

## 迁移指南（从 localStorage 到数据库）

### 步骤 1：创建数据库表

在数据库中执行 SKILL.md 第 0 节中的建表 SQL。

### 步骤 2：后端添加 API

在 `src/api/trading_api.py` 中添加两个路由：
- `GET /api/column-config/{page_key}` - 获取列配置
- `POST /api/column-config/{page_key}` - 保存列配置

### 步骤 3：前端替换持久化逻辑

在每个使用 AG Grid 的页面（共5个）：
1. 将 `loadColumnState()` 替换为异步版本，调用 `GET /api/column-config/{pageKey}`
2. 将 `saveColumnState()` 替换为异步版本，调用 `POST /api/column-config/{pageKey}`
3. 在 `onGridReady` 中传入对应的 `page_key`：
   - `OrderBookMonitor.vue` → `'orderbook_monitor'`
   - `PositionMonitor.vue` → `'position_monitor'`
   - `OrderManagement.vue` → `'order_management'`
   - `VwapThreshold.vue` → `'vwap_threshold'`
   - `ConnectionStatus.vue` → `'connection_status'`

### 步骤 4（可选）：初始化默认配置

为每个页面插入默认列配置到数据库，作为新用户的首次加载默认值。
