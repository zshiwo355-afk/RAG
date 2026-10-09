import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/',
  server: {
    port: 5174,
    proxy: {
      '/api': 'http://127.0.0.1:8001',
      '/portal/knowledge': process.env.KNOWLEDGE_PORTAL_PROXY_TARGET || 'http://127.0.0.1:9001'
    }
  }
})
