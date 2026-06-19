export interface PopupNotification {
  id: string
  title: string
  message: string
  type?: 'warning' | 'error' | 'success' | 'info'
  source?: string
  dedup_key?: string
  created_at: string
}

const STORAGE_KEY = 'popup_notification_history'
const MAX_HISTORY = 80
export const POPUP_NOTIFICATION_HISTORY_EVENT = 'popup-notification-history-change'

function parseHistory(): PopupNotification[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const rows = raw ? JSON.parse(raw) : []
    return Array.isArray(rows) ? rows : []
  } catch {
    return []
  }
}

export function listPopupNotifications(): PopupNotification[] {
  return parseHistory().sort((a, b) => b.created_at.localeCompare(a.created_at))
}

export function addPopupNotification(input: Omit<PopupNotification, 'id' | 'created_at'>) {
  const current = parseHistory()
  if (input.dedup_key) {
    const existing = current.find((item) => item.dedup_key === input.dedup_key)
    if (existing) return existing
  }
  const item: PopupNotification = {
    ...input,
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    created_at: new Date().toISOString(),
  }
  const next = [item, ...current].slice(0, MAX_HISTORY)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  window.dispatchEvent(new CustomEvent(POPUP_NOTIFICATION_HISTORY_EVENT))
  return item
}

export function clearPopupNotifications() {
  localStorage.removeItem(STORAGE_KEY)
  window.dispatchEvent(new CustomEvent(POPUP_NOTIFICATION_HISTORY_EVENT))
}
