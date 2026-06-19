import { get, post } from './request'

export interface PopupNotification {
  id: number
  title: string
  message: string
  type?: 'warning' | 'error' | 'success' | 'info'
  source?: string | null
  dedup_key?: string | null
  event_at?: string | null
  read_at?: string | null
  created_at: string
  updated_at?: string | null
  payload?: any
}

export interface PopupNotificationListResult {
  items: PopupNotification[]
  pagination: {
    page: number
    page_size: number
    total: number
    total_pages: number
  }
  unread_count: number
}

export const POPUP_NOTIFICATION_HISTORY_EVENT = 'popup-notification-history-change'

type ReadStatus = 'unread' | 'read' | 'all'

function emitHistoryChange() {
  window.dispatchEvent(new CustomEvent(POPUP_NOTIFICATION_HISTORY_EVENT))
}

export async function listPopupNotifications(options: {
  readStatus?: ReadStatus
  page?: number
  pageSize?: number
  syncRecent?: boolean
} = {}): Promise<PopupNotificationListResult> {
  const params = new URLSearchParams()
  params.set('read_status', options.readStatus || 'unread')
  params.set('page', String(options.page || 1))
  params.set('page_size', String(options.pageSize || 50))
  if (options.syncRecent === false) params.set('sync_recent', 'false')
  const res = await get(`/api/trading/notifications?${params.toString()}`)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data?.detail || '获取弹窗消息失败')
  }
  return {
    items: Array.isArray(data.items) ? data.items : [],
    pagination: data.pagination || { page: 1, page_size: 50, total: 0, total_pages: 0 },
    unread_count: Number(data.unread_count || 0),
  }
}

export async function addPopupNotification(input: Omit<PopupNotification, 'id' | 'created_at' | 'updated_at' | 'read_at'>) {
  try {
    const res = await post('/api/trading/notifications', input)
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.success === false) {
      throw new Error(data?.detail || data?.message || '保存弹窗消息失败')
    }
    emitHistoryChange()
    return data.item as PopupNotification
  } catch (e) {
    console.warn('Failed to persist popup notification:', e)
    return null
  }
}

export async function markPopupNotificationRead(id: number) {
  const res = await post(`/api/trading/notifications/${id}/read`)
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data?.success === false) {
    throw new Error(data?.detail || data?.message || '标记已读失败')
  }
  emitHistoryChange()
  return data
}

export async function markAllPopupNotificationsRead() {
  const res = await post('/api/trading/notifications/mark-read', {})
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data?.success === false) {
    throw new Error(data?.detail || data?.message || '全部标记已读失败')
  }
  emitHistoryChange()
  return data
}
