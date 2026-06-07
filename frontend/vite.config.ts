import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('/vue/') || id.includes('/vue-router/')) return 'vendor-vue'
          if (id.includes('/element-plus/') || id.includes('/@element-plus/')) return 'vendor-element'
          if (id.includes('/ag-grid-community/') || id.includes('/ag-grid-vue3/')) return 'vendor-ag-grid'
          if (id.includes('/echarts/')) return 'vendor-echarts'
          return 'vendor'
        },
      },
    },
  },
  server: {
    proxy: {
      '/ws': {
        target: 'ws://127.0.0.1:19876',
        ws: true,
        // 后端未启动 / 重启 / 浏览器断开都会触发 EPIPE，在这里静默处理，避免堆栈噪音
        configure: (proxy) => {
          proxy.on('error', (err) => {
            console.warn('[ws proxy]', err.message)
          })
        },
      },
      '/api': {
        target: 'http://127.0.0.1:19876',
        configure: (proxy) => {
          proxy.on('error', (err) => {
            console.warn('[api proxy]', err.message)
          })
        },
      },
    },
  },
})
