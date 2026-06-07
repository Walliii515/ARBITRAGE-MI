import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { ModuleRegistry, AllCommunityModule } from 'ag-grid-community'

import App from './App.vue'
import router from './router'
import './style.css'
import './styles/ag-grid-orderbook.css'
import { markUserActivity } from './utils/auth'

ModuleRegistry.registerModules([AllCommunityModule])

document.documentElement.classList.add('dark')

// 全局用户活动监听 (click, keydown, mousemove, scroll)
const activityEvents = ['click', 'keydown', 'mousemove', 'scroll']
activityEvents.forEach(event => {
  window.addEventListener(event, markUserActivity, { passive: true })
})

createApp(App).use(ElementPlus).use(router).mount('#app')
