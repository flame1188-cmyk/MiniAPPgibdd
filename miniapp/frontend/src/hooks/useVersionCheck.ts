/**
 * Хук проверки версии приложения.
 *
 * Раз в POLL_INTERVAL_MS опрашивает /api/version и сравнивает с
 * версией VITE_APP_VERSION, встроенной в JS-bundle при сборке.
 * Если версии не совпадают — значит вышел новый деплой, и в bundle
 * на сервере уже другой код. Ставим hasUpdate=true, чтобы показать
 * пользователю баннер «Доступна новая версия, обновите страницу».
 *
 * Особый случай: long-lived Telegram WebView сессии.
 * Если пользователь держит Mini App открытым сутками, browser cache
 * может отдавать старый index.html (несмотря на Cache-Control: no-cache),
 * а наш no-cache middleware срабатывает только при свежем запросе.
 * Этот хук — единственный надёжный способ дать пользователю понять,
 * что нужно перезагрузить страницу.
 *
 * Безопасность:
 *  - Тихо игнорируем все ошибки сети (не блокируем UX).
 *  - Не показываем баннер, если VITE_APP_VERSION не задан (dev-режим).
 *  - После первого срабатывания прекращаем опрос — баннер показан,
 *    дальше пользователь сам решит, когда нажать «Обновить».
 */
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

/** Интервал опроса /api/version. */
const POLL_INTERVAL_MS = 60_000

/** Задержка перед первой проверкой — не блокируем initial render. */
const INITIAL_DELAY_MS = 5_000

/**
 * Версия, встроенная в bundle при сборке.
 * Берётся из env VITE_APP_VERSION (см. build_frontend.sh).
 * Если не задана (dev-режим без env) — хук не активен.
 */
const CURRENT_VERSION = import.meta.env.VITE_APP_VERSION as
  | string
  | undefined

export function useVersionCheck(): boolean {
  const [hasUpdate, setHasUpdate] = useState(false)

  useEffect(() => {
    // Без встроенной версии проверять бессмысленно — всегда будет mismatch.
    if (!CURRENT_VERSION) {
      if (import.meta.env.DEV) {
        // В dev-режиме это нормально — VITE_APP_VERSION задаётся только
        // через build_frontend.sh. Подсказываем разработчику.
        console.info(
          '[useVersionCheck] VITE_APP_VERSION не задан — проверка обновлений отключена'
        )
      }
      return
    }

    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const check = async () => {
      if (cancelled) return

      try {
        const data = await api.getVersion()
        if (cancelled) return

        if (data.version && data.version !== CURRENT_VERSION) {
          // Версии не совпадают — значит на сервере новый деплой.
          // Перестаём опрашивать: баннер показан, дальше пользователь
          // сам нажмёт «Обновить» (или закроет — на его ответственность).
          setHasUpdate(true)
          return
        }
      } catch {
        // Сеть / 5xx / etc — тихо игнорируем. Это не критичный функционал,
        // не должен мешать основной работе приложения.
      }

      // Планируем следующую проверку, только если обновление ещё не найдено.
      if (!cancelled) {
        timer = setTimeout(check, POLL_INTERVAL_MS)
      }
    }

    // Стартуем через короткую задержку, чтобы не конкурировать с
    // initial API-запросами (регионы, история задач и т.д.).
    timer = setTimeout(check, INITIAL_DELAY_MS)

    return () => {
      cancelled = true
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
    }
  }, [])

  return hasUpdate
}
