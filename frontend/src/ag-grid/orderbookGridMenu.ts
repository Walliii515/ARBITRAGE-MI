import type { DefaultMenuItem, GetMainMenuItemsParams, MenuItemDef } from 'ag-grid-community'
import type { OrderBookRow } from '../views/orderbookTypes'

/** 列头菜单：固定列 / 自适应列宽 / 重置 / 展开折叠（与参考图一致） */
export function getOrderbookMainMenuItems(
  params: GetMainMenuItemsParams<OrderBookRow>
): (DefaultMenuItem | MenuItemDef<OrderBookRow>)[] {
  const column = params.column
  if (!column) {
    return []
  }

  return [
    {
      name: '固定列',
      subMenu: [
        {
          name: '固定左侧',
          action: () => params.api.setColumnsPinned([column.getColId()], 'left'),
        },
        {
          name: '固定右侧',
          action: () => params.api.setColumnsPinned([column.getColId()], 'right'),
        },
        {
          name: '取消固定',
          action: () => params.api.setColumnsPinned([column.getColId()], null),
        },
      ],
    },
    {
      name: '自适应列',
      action: () => params.api.autoSizeColumns([column.getColId()]),
    },
    {
      name: '全部列自适应',
      action: () => params.api.autoSizeAllColumns(),
    },
    'separator',
    {
      name: '重置列',
      action: () => params.api.resetColumnState(),
    },
    {
      name: '展开所有行',
      action: () => params.api.expandAll(),
    },
    {
      name: '折叠所有行',
      action: () => params.api.collapseAll(),
    },
  ]
}
