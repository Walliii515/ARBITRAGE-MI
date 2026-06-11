import { createRouter, createWebHistory } from 'vue-router'
import { isLoggedIn, isSessionExpired, removeToken } from '../utils/auth'

const ROUTE_CHUNK_RELOAD_KEY = 'route_chunk_reload_attempted'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/Login.vue'),
    },
    {
      path: '/',
      name: 'orderbook',
      component: () => import('../views/OrderBookMonitor.vue'),
    },
    {
      path: '/orders',
      name: 'orders',
      component: () => import('../views/OrderManagement.vue'),
    },
    {
      path: '/signals',
      name: 'signals',
      component: () => import('../views/TradeSignals.vue'),
    },
    {
      path: '/reverse-arbitrage',
      redirect: '/reverse-arbitrage/orderbook',
    },
    {
      path: '/reverse-arbitrage/orderbook',
      name: 'reverse-arbitrage-orderbook',
      component: () => import('../views/ReverseArbitrage.vue'),
    },
    {
      path: '/reverse-arbitrage/signals',
      name: 'reverse-arbitrage-signals',
      component: () => import('../views/ReverseTradeSignals.vue'),
    },
    {
      path: '/reverse-arbitrage/orders',
      name: 'reverse-arbitrage-orders',
      component: () => import('../views/ReverseOrderManagement.vue'),
    },
    {
      path: '/reverse-arbitrage/positions',
      name: 'reverse-arbitrage-positions',
      component: () => import('../views/ReversePositionMonitor.vue'),
    },
    {
      path: '/reverse-arbitrage/capital',
      name: 'reverse-arbitrage-capital',
      component: () => import('../views/ReverseCapitalMonitor.vue'),
    },
    {
      path: '/reverse-arbitrage/reconciliation',
      name: 'reverse-arbitrage-reconciliation',
      component: () => import('../views/ReverseReconciliation.vue'),
    },
    {
      path: '/positions',
      name: 'positions',
      component: () => import('../views/PositionMonitor.vue'),
    },
    {
      path: '/capital',
      name: 'capital',
      component: () => import('../views/CapitalMonitor.vue'),
    },
    {
      path: '/connections',
      name: 'connections',
      component: () => import('../views/ConnectionStatus.vue'),
    },
    {
      path: '/reconciliation',
      name: 'reconciliation',
      component: () => import('../views/Reconciliation.vue'),
    },
    {
      path: '/settings/threshold',
      name: 'threshold',
      component: () => import('../views/VwapThreshold.vue'),
    },
  ],
})

// 全局路由守卫
router.beforeEach((to) => {
  const loggedIn = isLoggedIn()
  
  if (!loggedIn && to.name !== 'login') {
    // 未登录且访问的不是登录页 -> 跳转登录
    return { name: 'login' }
  }

  if (loggedIn && to.name === 'login') {
    // 已登录且访问登录页 -> 跳转首页
    return { path: '/' }
  }

  if (loggedIn && isSessionExpired()) {
    // session 已过期 -> 清除 token 并跳转登录
    removeToken()
    return { name: 'login' }
  }
})

router.afterEach(() => {
  sessionStorage.removeItem(ROUTE_CHUNK_RELOAD_KEY)
})

router.onError((error, to) => {
  const message = error?.message || ''
  const isChunkLoadError =
    message.includes('Failed to fetch dynamically imported module') ||
    message.includes('Importing a module script failed') ||
    message.includes('Loading chunk')

  if (!isChunkLoadError) return

  const target = to?.fullPath || window.location.pathname
  if (sessionStorage.getItem(ROUTE_CHUNK_RELOAD_KEY) === target) return

  sessionStorage.setItem(ROUTE_CHUNK_RELOAD_KEY, target)
  window.location.assign(target)
})

export default router
