/**
 * Встраивает HTML-карту (из report_generator.py) в <iframe>.
 *
 * HTML-карта — самодостаточный файл с inline Leaflet/ECharts,
 * он рендерится в WebView независимо от React-приложения.
 */
import { useState, useCallback } from 'react'
import { api } from '@/lib/api'
import { isTelegramDesktop } from '@/lib/telegram'
import { haptic } from '@/lib/telegram'

interface MapFrameProps {
  taskId: string
}

export function MapFrame({ taskId }: MapFrameProps) {
  const [mapSrc, setMapSrc] = useState(() => api.getMapUrl(taskId))
  const [refreshing, setRefreshing] = useState(false)
  const isDesktop = isTelegramDesktop()

  const handleRefresh = useCallback(async () => {
    if (refreshing) return
    setRefreshing(true)
    haptic('light')
    try {
      // Добавляем случайный параметр, чтобы избежать HTTP-кэша браузера
      const url = api.getRefreshMapUrl(taskId)
      setMapSrc(url + '&_t=' + Date.now())
    } finally {
      setTimeout(() => setRefreshing(false), 3000)
    }
  }, [taskId, refreshing])

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium" style={{ color: 'var(--tg-color-hint, #999)' }}>
          Карта ДТП
        </span>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors"
          style={{
            color: refreshing ? 'var(--tg-color-hint, #999)' : '#2481cc',
            border: '1px solid var(--tg-color-hint, #999)',
            backgroundColor: 'var(--tg-color-section-bg, #fff)',
            opacity: refreshing ? 0.6 : 1,
          }}
        >
          {refreshing ? '⏳ Обновление...' : '↻ Обновить данные'}
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
          src={mapSrc}
          title="Карта ДТП"
          className="w-full h-full border-0"
          sandbox="allow-scripts allow-same-origin allow-popups"
          style={{ display: 'block' }}
        />
      </div>
      <p className="text-xs opacity-50 text-center">
        Нажмите «Обновить данные» чтобы перезагрузить сведения из ГИБДД
        (если база ГИБДД была обновлена после последней выгрузки)
      </p>
    </div>
  )
}
