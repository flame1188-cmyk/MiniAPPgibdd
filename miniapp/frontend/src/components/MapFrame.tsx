/**
 * Встраивает HTML-карту (из report_generator.py) в <iframe>.
 *
 * HTML-карта — самодостаточный файл с inline Leaflet/ECharts,
 * он рендерится в WebView независимо от React-приложения.
 */
import { api } from '@/lib/api'

interface MapFrameProps {
  taskId: string
}

export function MapFrame({ taskId }: MapFrameProps) {
  // srcdoc не подойдёт — нужен полноценный iframe с src,
  // чтобы корректно работали inline-скрипты Leaflet
  const src = api.getMapUrl(taskId)

  return (
    <div
      className="rounded-2xl overflow-hidden"
      style={{
        backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
        height: '60vh',
        minHeight: '400px',
      }}
    >
      <iframe
        src={src}
        title="Карта ДТП"
        className="w-full h-full border-0"
        sandbox="allow-scripts allow-same-origin allow-popups"
        style={{ display: 'block' }}
      />
    </div>
  )
}
