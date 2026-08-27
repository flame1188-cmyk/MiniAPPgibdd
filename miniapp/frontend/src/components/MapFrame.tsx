/**
 * Встраивает HTML-карту (из report_generator.py) в <iframe>.
 *
 * HTML-карта — самодостаточный файл с inline Leaflet/ECharts,
 * он рендерится в WebView независимо от React-приложения.
 */
import { useState, useRef } from 'react'
import { api } from '@/lib/api'
import { isTelegramDesktop } from '@/lib/telegram'

interface MapFrameProps {
  taskId: string
}

export function MapFrame({ taskId }: MapFrameProps) {
  const [refreshing, setRefreshing] = useState(false)
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const isDesktop = isTelegramDesktop()

  const handleRefresh = () => {
    setRefreshing(true)
    // Обновляем src iframe — это вызовет повторный запрос с ?refresh=true
    if (iframeRef.current) {
      iframeRef.current.src = api.getRefreshMapUrl(taskId)
    }
    // Сбрасываем состояние после завершения загрузки
    const timer = setTimeout(() => setRefreshing(false), 10000)
    iframeRef.current?.addEventListener('load', () => {
      clearTimeout(timer)
      setRefreshing(false)
    }, { once: true })
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-end">
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors"
          style={{
            backgroundColor: 'var(--tg-color-button, #2481cc)',
            color: 'var(--tg-color-button-text, #ffffff)',
            opacity: refreshing ? 0.6 : 1,
          }}
        >
          {refreshing ? '⏳ Обновление...' : '🔄 Обновить данные'}
        </button>
      </div>
      <div
        className="rounded-2xl overflow-hidden"
        style={{
          backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
          height: isDesktop ? '80vh' : '60vh',
          minHeight: isDesktop ? '600px' : '400px',
        }}
      >
        <iframe
          ref={iframeRef}
          src={api.getMapUrl(taskId)}
          title="Карта ДТП"
          className="w-full h-full border-0"
          sandbox="allow-scripts allow-same-origin allow-popups"
          style={{ display: 'block' }}
        />
      </div>
    </div>
  )
}
