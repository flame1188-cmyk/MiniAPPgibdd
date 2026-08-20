/**
 * useUrlState — синхронизация state с URL query-параметрами.
 *
 * Stabilization A10 (P1 #5): замена обычного useState для активной задачи
 * и текущей вкладки. Без этого F5 сбрасывает activeTaskId — пользователь
 * теряет контекст, видит пустую форму вместо результата.
 *
 * С URL state:
 *   /app/?task=abc123 → открывает результаты задачи abc123
 *   /app/?task=abc123&tab=np-bdd → открывает вкладку НП БДД
 *   F5 → state восстанавливается из URL
 *   Поделиться ссылкой → другой пользователь сразу увидит нужную задачу
 *
 * Использование:
 *   const [taskId, setTaskId] = useUrlState('task', null)
 *   const [tab, setTab] = useUrlState('tab', 'dtp')
 *
 * Поведение:
 *   - При init читает URL ?<key>=<value>
 *   - При setTaskId('abc') — обновляет state + URL через history.replaceState
 *   - При setTaskId(null) — удаляет параметр из URL
 *   - Подписывается на popstate (back/forward) — синхронизирует state
 *
 * Технические детали:
 *   - Использует history.replaceState (не pushState) — не засоряет историю.
 *     Если пользователь нажмёт back — уйдёт на предыдущую страницу, а не
 *     на предыдущую задачу.
 *   - Сервер видит URL только при reload — состояние живёт в браузере.
 */
import { useState, useEffect, useCallback } from 'react'

export function useUrlState<T extends string | null>(
  key: string,
  defaultValue: T,
): [T, (value: T) => void] {
  const readFromUrl = useCallback((): T => {
    if (typeof window === 'undefined') return defaultValue
    const params = new URLSearchParams(window.location.search)
    const value = params.get(key)
    return (value as T) ?? defaultValue
  }, [key, defaultValue])

  const [value, setValue] = useState<T>(readFromUrl)

  // Записываем в URL при смене state
  const setValueAndUrl = useCallback(
    (newValue: T) => {
      setValue(newValue)
      try {
        const url = new URL(window.location.href)
        if (newValue === null || newValue === '' || newValue === defaultValue) {
          url.searchParams.delete(key)
        } else {
          url.searchParams.set(key, String(newValue))
        }
        // replaceState — не засоряет историю браузера
        window.history.replaceState({}, '', url.toString())
      } catch (err) {
        // window.location может быть недоступен в SSR (теоретически)
        console.warn(`[useUrlState] failed to update URL for key=${key}:`, err)
      }
    },
    [key, defaultValue],
  )

  // Подписка на popstate — синхронизация при back/forward
  useEffect(() => {
    const onPopState = () => {
      setValue(readFromUrl())
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [readFromUrl])

  return [value, setValueAndUrl]
}
