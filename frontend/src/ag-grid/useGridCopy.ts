import { ref, onUnmounted, type Ref } from 'vue'
import type { GridApi } from 'ag-grid-community'

/**
 * AG Grid 复制 & 导出组合式函数
 * - Cmd+C / Ctrl+C：复制当前聚焦单元格的显示值到剪贴板
 * - 右键菜单：复制单元格 / 导出 CSV
 *
 * 用法：
 *   const { gridContainerRef, setupGridCopy } = useGridCopy()
 *   function onGridReady(params: GridReadyEvent) {
 *     gridApi = params.api
 *     setupGridCopy(params.api)
 *   }
 *   // 模板中：
 *   <div ref="gridContainerRef">
 *     <ag-grid-vue ... />
 *   </div>
 */
export function useGridCopy() {
  const gridContainerRef: Ref<HTMLElement | null> = ref(null)
  let _api: GridApi | null = null
  const _cleanups: (() => void)[] = []

  /* ── 获取聚焦单元格的显示值 ── */
  function getFocusedCellDisplayValue(): string {
    if (!_api) return ''
    const cell = _api.getFocusedCell()
    if (!cell) return ''
    const column = cell.column
    const rowIndex = cell.rowIndex

    // 获取 valueFormatter 格式化后的显示值
    const colDef = column.getColDef()
    const rowNode = _api.getDisplayedRowAtIndex(rowIndex)
    if (!rowNode) return ''

    const rawValue = rowNode.data?.[column.getColId()]
    if (rawValue == null) return ''

    if (typeof colDef.valueFormatter === 'function') {
      try {
        return String(
          colDef.valueFormatter({
            value: rawValue,
            data: rowNode.data,
            node: rowNode,
            colDef,
            column,
            api: _api,
            context: undefined,
          } as any),
        )
      } catch {
        // fallback to raw value
      }
    }
    return String(rawValue)
  }

  /* ── 复制到剪贴板（兼容 HTTP 非安全上下文） ── */
  function copyToClipboard(text: string) {
    if (!text) return

    // Clipboard API 仅在安全上下文（HTTPS / localhost）下可用
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard.writeText(text).catch(() => fallbackCopy(text))
    } else {
      fallbackCopy(text)
    }
  }

  function fallbackCopy(text: string) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    try {
      document.execCommand('copy')
    } catch (_) {
      // 静默失败
    }
    document.body.removeChild(ta)
  }

  /* ── Cmd+C / Ctrl+C 键盘监听 ── */
  function handleKeyDown(e: KeyboardEvent) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'c') {
      const text = getFocusedCellDisplayValue()
      if (text) {
        e.preventDefault()
        e.stopPropagation()
        copyToClipboard(text)
      }
    }
  }

  /* ── 右键菜单 ── */
  function handleContextMenu(e: MouseEvent) {
    // 只在网格区域内生效（排除表头）
    const target = e.target as HTMLElement
    if (target.closest('.ag-header-container')) return

    e.preventDefault()

    // 移除旧菜单
    removeMenu()

    // 创建菜单
    const menu = document.createElement('div')
    menu.className = 'ag-copy-context-menu'

    // 获取单元格显示文本（用于"复制单元格"）
    const cellText = getFocusedCellDisplayValue()

    // -- 复制单元格 --
    const copyItem = document.createElement('div')
    copyItem.className = 'ag-copy-context-menu-item'
    copyItem.innerHTML = '<span class="ag-copy-context-menu-icon">📋</span> 复制单元格'
    if (!cellText) {
      copyItem.classList.add('disabled')
    }
    copyItem.addEventListener('click', () => {
      if (cellText) copyToClipboard(cellText)
      removeMenu()
    })
    menu.appendChild(copyItem)

    // -- 分隔线 --
    const sep = document.createElement('div')
    sep.className = 'ag-copy-context-menu-separator'
    menu.appendChild(sep)

    // -- 导出 CSV --
    const exportItem = document.createElement('div')
    exportItem.className = 'ag-copy-context-menu-item'
    exportItem.innerHTML = '<span class="ag-copy-context-menu-icon">📄</span> 导出 CSV'
    exportItem.addEventListener('click', () => {
      exportCsv()
      removeMenu()
    })
    menu.appendChild(exportItem)

    // 定位：确保不超出视口
    document.body.appendChild(menu)
    const menuRect = menu.getBoundingClientRect()
    let left = e.pageX
    let top = e.pageY
    if (left + menuRect.width > window.innerWidth) {
      left = window.innerWidth - menuRect.width - 4
    }
    if (top + menuRect.height > window.innerHeight) {
      top = window.innerHeight - menuRect.height - 4
    }
    menu.style.left = `${left}px`
    menu.style.top = `${top}px`

    // 点击其他区域关闭菜单
    const closeHandler = () => {
      removeMenu()
      document.removeEventListener('click', closeHandler, true)
      document.removeEventListener('contextmenu', closeHandler, true)
    }
    setTimeout(() => {
      document.addEventListener('click', closeHandler, true)
      document.addEventListener('contextmenu', closeHandler, true)
    }, 0)
  }

  function removeMenu() {
    document.querySelectorAll('.ag-copy-context-menu').forEach((el) => el.remove())
  }

  /* ── 导出 CSV ── */
  function exportCsv() {
    if (!_api) return
    _api.exportDataAsCsv({
      fileName: `grid-export-${new Date().toISOString().slice(0, 10)}.csv`,
    })
  }

  /* ── 在 gridReady 中调用，绑定监听器 ── */
  function setupGridCopy(api: GridApi) {
    _api = api
    const el = gridContainerRef.value
    if (!el) return

    el.addEventListener('keydown', handleKeyDown, true)
    el.addEventListener('contextmenu', handleContextMenu)

    _cleanups.push(() => {
      el.removeEventListener('keydown', handleKeyDown, true)
      el.removeEventListener('contextmenu', handleContextMenu)
    })
  }

  onUnmounted(() => {
    removeMenu()
    _cleanups.forEach((fn) => fn())
    _cleanups.length = 0
  })

  return {
    gridContainerRef,
    setupGridCopy,
    exportCsv,
  }
}
