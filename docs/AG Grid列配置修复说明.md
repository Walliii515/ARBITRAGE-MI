# AG Grid 列配置保存修复

## 问题描述

AG Grid 保存列配置功能无法保存列的顺序，用户拖动调整列顺序后，保存并刷新页面，列顺序恢复默认。

## 根本原因

AG Grid v35+ 的 `applyColumnState()` 方法在恢复列顺序时，**依赖的是数组中元素的排列顺序，而不是每个列对象中的 `order` 字段**。

### 错误逻辑（修复前）

```python
# 后端加载时
for row in rows:  # rows 已经按 sort_order ASC 排序
    state = {
        'colId': row['col_id'],
        'order': row['sort_order'],  # ❌ 这个字段不会被 applyColumnState 使用
        'hide': not bool(row['is_visible']),
    }
    column_state.append(state)

# 前端应用时
gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
```

虽然数据库查询时按 `sort_order ASC` 排序了，但由于每个列对象中包含了 `order` 字段，AG Grid 可能会混淆，导致列顺序应用不正确。

### 正确逻辑（修复后）

```python
# 后端加载时
for row in rows:  # rows 已经按 sort_order ASC 排序
    state = {
        'colId': row['col_id'],
        # ✅ 不添加 order 字段，依赖数组的自然顺序
        'hide': not bool(row['is_visible']),
    }
    column_state.append(state)

# 前端应用时
gridApi.applyColumnState({ state: data.columnState, applyOrder: true })
```

## 修复内容

### 1. 后端 API 修改

**文件**: `src/api/trading_api.py` - `get_column_config()` 函数

**修改**:
- 移除了 `state` 对象中的 `'order': row['sort_order']` 字段
- 添加了注释说明 AG Grid 依赖数组顺序而非 order 字段

### 2. 数据流程

```
保存流程:
用户拖动列 → getColumnState() → 数组索引即顺序 → 保存到数据库 sort_order

加载流程:
数据库按 sort_order ASC 查询 → 构造数组（顺序已正确） → applyColumnState → 列顺序恢复
```

## AG Grid 列状态说明

### getColumnState() 返回的对象包含

```typescript
interface ColumnState {
  colId: string;           // 列ID
  hide: boolean;           // 是否隐藏
  width?: number;          // 列宽
  pinned?: 'left' | 'right' | null;  // 固定位置
  sort?: 'asc' | 'desc' | null;      // 排序状态
  filterModel?: any;       // 筛选条件
  // 注意：虽然可能有 order 字段，但 applyColumnState 不使用它来确定顺序
}
```

### applyColumnState 如何确定列顺序

AG Grid 的 `applyColumnState()` 方法：
- 当 `applyOrder: true` 时，会根据**数组中元素的顺序**来排列列
- **不会**读取每个列对象中的 `order` 字段
- 数组的第一个元素对应第一列，第二个元素对应第二列，依此类推

## 验证方法

1. 打开订单簿监控页面
2. 拖动调整列顺序（例如将"标的资产"拖到第3列）
3. 点击"保存列配置"
4. 刷新页面
5. 验证列顺序是否保持为用户设置的顺序

## 相关代码

- 前端保存: `frontend/src/views/OrderBookMonitor.vue` → `saveColumnState()`
- 前端加载: `frontend/src/views/OrderBookMonitor.vue` → `loadColumnState()`
- 后端保存: `src/api/trading_api.py` → `save_column_config()`
- 后端加载: `src/api/trading_api.py` → `get_column_config()`
- 数据库表: `ag_grid_column_config` (sort_order 字段)

## 注意事项

1. **数据库仍然需要保存 sort_order**: 虽然前端不读取 `order` 字段，但数据库查询时需要按 `sort_order ASC` 排序
2. **保存时仍然发送 order**: `getColumnState()` 返回的对象中可能包含 `order` 字段，后端会保存到数据库，这是正常的
3. **关键是加载时不返回 order**: 这样 AG Grid 就会完全依赖数组顺序

## 修复日期

2026-05-29
