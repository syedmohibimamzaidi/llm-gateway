import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    proxy: {
      '/analytics': process.env.GATEWAY_API_PROXY_TARGET || 'http://127.0.0.1:8000',
      '/requests': process.env.GATEWAY_API_PROXY_TARGET || 'http://127.0.0.1:8000',
    },
  },
})
