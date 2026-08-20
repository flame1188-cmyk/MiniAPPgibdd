import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  // Mini App раздаётся из /app/ (StaticFiles mount в main.py).
  // Без base: '/app/' Vite генерирует абсолютные пути /assets/...,
  // а браузер запрашивает их от корня домена — FastAPI отдаёт 404.
  // С base: '/app/' все ссылки станут /app/assets/... и /app/favicon.svg.
  base: '/app/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    host: true, // 0.0.0.0 — нужно для доступа с телефона в локальной сети
    // Проксируем API-запросы на backend (избегаем CORS в dev)
    // main.py по умолчанию слушает PORT=8080
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      '/bot': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // Telegram Mini App загружается в WebView — критичен размер бандла
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      output: {
        manualChunks: {
          // Core React — почти на каждой странице
          'react-vendor': ['react', 'react-dom'],
          // TanStack Query — нужен везде, где есть polling/fetch
          'query-vendor': ['@tanstack/react-query'],
          // Stabilization P1 #3 (2026-08-20): recharts (~150 КБ gzip)
          // — отдельный chunk, грузится только когда пользователь откроет
          // вкладку Analytics/Clusters/NpBdd. До этого — не нужен.
          'chart-vendor': ['recharts'],
          // Stabilization P1 #3: marked (~30 КБ gzip) — markdown для LLM
          // ответов. Грузится только когда пользователь откроет вкладку
          // «ИИ-анализ».
          'markdown-vendor': ['marked'],
        },
      },
    },
  },
})
