export const TOKEN_KEY = 'auth_token'
export const LAST_ACTIVITY_KEY = 'last_activity_time'
export const SESSION_TIMEOUT = 60 * 60 * 1000 // 1小时 (毫秒)

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function removeToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(LAST_ACTIVITY_KEY)
}

export function updateActivityTime(): void {
  localStorage.setItem(LAST_ACTIVITY_KEY, Date.now().toString())
}

export function getLastActivityTime(): number {
  const time = localStorage.getItem(LAST_ACTIVITY_KEY)
  return time ? parseInt(time, 10) : 0
}

export function isSessionExpired(): boolean {
  const lastActivity = getLastActivityTime()
  if (!lastActivity) return true
  return Date.now() - lastActivity > SESSION_TIMEOUT
}

export function isLoggedIn(): boolean {
  return !!getToken() && !isSessionExpired()
}
