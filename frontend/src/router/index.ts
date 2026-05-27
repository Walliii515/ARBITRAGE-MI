import { createRouter, createWebHistory } from 'vue-router'
import OrderBookMonitor from '../views/OrderBookMonitor.vue'
import OrderManagement from '../views/OrderManagement.vue'
import PositionMonitor from '../views/PositionMonitor.vue'
import VwapThreshold from '../views/VwapThreshold.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'orderbook',
      component: OrderBookMonitor,
    },
    {
      path: '/orders',
      name: 'orders',
      component: OrderManagement,
    },
    {
      path: '/positions',
      name: 'positions',
      component: PositionMonitor,
    },
    {
      path: '/settings/threshold',
      name: 'threshold',
      component: VwapThreshold,
    },
  ],
})

export default router
