import { createRouter, createWebHistory } from 'vue-router'
import { isLoggedIn, isSessionExpired, removeToken } from '../utils/auth'
import OrderBookMonitor from '../views/OrderBookMonitor.vue'
import OrderManagement from '../views/OrderManagement.vue'
import PositionMonitor from '../views/PositionMonitor.vue'
import VwapThreshold from '../views/VwapThreshold.vue'

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
      path: '/positions',
      name: 'positions',
      component: () => import('../views/PositionMonitor.vue'),
    },
    {
      path: '/settings/threshold',
      name: 'threshold',
      component: () => import('../views/VwapThreshold.vue'),
    },
  ],
})

// 全局路由守卫
router.beforeEach((to, _from, next) => {
  const loggedIn = isLoggedIn()
  
  if (!loggedIn && to.name !== 'login') {
    // 未登录且访问的不是登录页 -> 跳转登录
    next({ name: 'login' })
  } else if (loggedIn && to.name === 'login') {
    // 已登录且访问登录页 -> 跳转首页
    next({ path: '/' })
  } else if (loggedIn && isSessionExpired()) {
    // session 已过期 -> 清除 token 并跳转登录
    removeToken()
    next({ name: 'login' })
  } else {
    next()
  }
})

export default router
