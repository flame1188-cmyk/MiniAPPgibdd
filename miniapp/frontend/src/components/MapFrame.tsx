/**
 * Встраивает HTML-карту (из report_generator.py) в <iframe>.
 *
 * HTML-карта — самодостаточный файл с inline Leaflet/ECharts,
 * он рендерится в WebView независимо от React-приложения.
 *
 * Кнопка «Обновить данные» — отправляет запрос с ?refresh=true,
 * что на бэкенде инвалидирует кэш, перескачивает данные из БД ГИБДД
 * и перегенерирует HTML-карту.
 */
import { useState, useCallback, useRef } from 'react'
import { api } from '@/lib/api'
import { isTelegramDesktop } from '@/lib/telegram'
import { haptic, showAlert } from '@/lib/telegram'

interface MapFrameProps {
  taskId: string
}

export function MapFrame({ taskId }: MapFrameProps) {
  const [refreshing, setRefreshing] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const src = refreshing
    ? '' // Скрываем iframe во время обновления
    : refreshKey > 0
      ? api.getRefreshMapUrl(taskId)
      : api.getMapUrl(taskId)

  const isDesktop = isTelegramDesktop()

  const handleRefresh = useCallback(async () => {
    haptic('medium')
    setRefreshing(true)
    try {
      // Прогреваем кэш — запрашиваем карту с ?refresh=true
      // Ответ — это HTML, нам не нужно его парсить, достаточно
      // чтобы бэкенд пересчитал данные и закэшировал новую карту.
      const url = api.getRefreshMapUrl(taskId)
      const resp = await fetch(url)
      if (!resp.ok) {
        const text = await resp.text().catch(() => '')
        let detail = 'Неизвестная ошибка'
        try { detail = JSON.parse(text).detail || text } catch { detail = text.slice(0, 200) }
        throw new Error(detail)
      }
      // Успех — перезагружаем iframe с новой картой
      setRefreshKey((k) => k + 1)
      haptic('success')
    } catch (err: any) {
      haptic('error')
      await showAlert(`Не удалось обновить данные:\n${err.message}`)
      setRefreshKey((k) => k + 1) // Всё равно восстанавливаем iframe
    } finally {
      setRefreshing(false)
    }
  }, [taskId])

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-end">
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-opacity"
          style={{
            backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
            color: 'var(--tg-color-text, #000000)',
            opacity: refreshing ? 0.5 : 1,
          }}
        >
          {refreshing ? (
            <>
              <span className="inline-block animate-spin">⏳</span>
              Обновление...
            </>
          ) : (
            <>
              🔄 Обновить данные
            </>
          )}
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
        {refreshing ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-sm opacity-60">Обновление данных...</div>
          </div>
        ) : (
          <iframe
            ref={iframeRef}
            key={refreshKey}
            src={src}
            title="Карта ДТП"
            className="w-full h-full border-0"
            sandbox="allow-scripts allow-same-origin allow-popups"
            style={{ display: 'block' }}
          />
        )}
      </div>
    </div>
  )
}
