import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { ModuleRegistry, AllCommunityModule } from 'ag-grid-community'

import App from './App.vue'
import router from './router'
import './style.css'
import './styles/ag-grid-orderbook.css'

ModuleRegistry.registerModules([AllCommunityModule])

document.documentElement.classList.add('dark')

createApp(App).use(ElementPlus).use(router).mount('#app')
