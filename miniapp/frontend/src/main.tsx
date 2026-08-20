import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './index.css'
import { initTelegram } from './lib/telegram'

// Инициализируем Telegram WebApp ДО рендера React
initTelegram()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000, // 30 секунд — данные часто меняются
    },
  },
})

// Stabilization A10 (P1 #5): ErrorBoundary оборачивает всё приложение.
// Ловит ошибки в render-дереве и показывает fallback UI вместо белого экрана.
// resetKey = текущий URL, чтобы при смене URL (back/forward) state
// error boundary сбрасывался — приложение пробует отрендерить заново.
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary resetKey={typeof window !== 'undefined' ? window.location.href : ''}>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
)
