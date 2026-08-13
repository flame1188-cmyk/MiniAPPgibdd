/**
 * Баннер «Доступна новая версия приложения».
 *
 * Показывается фиксированно сверху экрана, когда backend отдаёт версию,
 * отличную от встроенной в JS-bundle. Использует Telegram theme colors
 * чтобы гармонично вписаться в любое оформление Mini App.
 *
 * Поведение:
 *  - Клик по кнопке «Обновить» → window.location.reload()
 *  - Баннер нельзя закрыть без перезагрузки: иначе пользователь
 *    останется на устаревшем коде, что и стало причиной бесконечного
 *    polling /clusters в предыдущем инциденте.
 */
import { haptic } from '@/lib/telegram'

interface Props {
  /** Показывать ли баннер. */
  visible: boolean
}

export function VersionBanner({ visible }: Props) {
  if (!visible) return null

  const handleReload = () => {
    haptic('light')
    // Принудительный reload с обходом кэша — на случай, если
    // браузер всё-таки отдаст старый index.html из cache.
    window.location.reload()
  }

  return (
    <div
      className="fixed top-0 left-0 right-0 z-50 shadow-md"
      style={{
        backgroundColor: 'var(--tg-color-link, #2481cc)',
        color: '#ffffff',
      }}
      role="alert"
      aria-live="polite"
    >
      <div className="mx-auto max-w-3xl px-4 py-2.5 flex items-center justify-between gap-3">
        <div className="text-sm font-medium leading-snug">
          🔄 Доступна новая версия приложения
        </div>
        <button
          onClick={handleReload}
          className="shrink-0 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors"
          style={{
            backgroundColor: 'rgba(255, 255, 255, 0.22)',
            color: '#ffffff',
            border: '1px solid rgba(255, 255, 255, 0.35)',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.backgroundColor = 'rgba(255, 255, 255, 0.35)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.backgroundColor = 'rgba(255, 255, 255, 0.22)'
          }}
        >
          Обновить
        </button>
      </div>
    </div>
  )
}
