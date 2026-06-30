import { removeToken, getToken } from './auth'
import { ElMessage } from 'element-plus'
import router from '../router'

// 开发环境走 localhost:19876，生产环境留空走 Nginx 反代
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

interface RequestOptions extends RequestInit {
  baseURL?: string
}

export async function request(url: string, options: RequestOptions = {}): Promise<Response> {
  const token = getToken()
  
  // 构建完整 URL
  const fullUrl = options.baseURL ? `${options.baseURL}${url}` : `${API_BASE}${url}`
  
  // 添加 Authorization header
  const headers = new Headers(options.headers || {})
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  headers.set('Content-Type', 'application/json')
  
  try {
    const response = await fetch(fullUrl, {
      ...options,
      headers,
    })
    
    // 处理 401 未授权
    if (response.status === 401) {
      removeToken()
      ElMessage.error('登录已过期,请重新登录')
      router.push({ name: 'login' })
      throw new Error('未授权')
    }
    
    // 处理 403 禁止访问
    if (response.status === 403) {
      ElMessage.error('权限不足')
      throw new Error('权限不足')
    }
    
    return response
  } catch (error: any) {
    if (error.message !== '未授权' && error.message !== '权限不足') {
      ElMessage.error(`请求失败: ${error.message}`)
    }
    throw error
  }
}

// 便捷方法
export async function get(url: string, options?: RequestOptions): Promise<Response> {
  return request(url, { ...options, method: 'GET' })
}

export async function post(url: string, data?: any, options?: RequestOptions): Promise<Response> {
  return request(url, {
    ...options,
    method: 'POST',
    body: data ? JSON.stringify(data) : undefined,
  })
}
