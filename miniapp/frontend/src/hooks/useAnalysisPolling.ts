/**
 * Хук для polling статуса длительных аналитических операций.
 *
 * Sprint 5: useLLMSummaryPolling УДАЛЁН — LLM-резюме теперь работает
 * только через SSE-стрим, без long-polling fallback'а на ?wait=25.
 * При монтировании вкладки LLMAnalysisView делает one-shot GET /llm/summary
 * (без wait), чтобы показать готовое резюме из кэша. Для генерации
 * используется POST /llm/summary/stream (SSE).
 *
 * Остался только useClustersPolling — кластеры не имеют SSE-эндпоинта.
 *
 * Hotfix (Sprint 7): прекратить polling при 404 (Task not found) и
 * 403 (Access denied). Раньше при 404 (задача удалена из LRU + не
 * сохранилась в БД, либо контейнер перезапущен) polling бесконечно
 * стучался на /clusters каждую секунду, засоряя логи и нагружая
 * сервер. Теперь при 404/403 polling останавливается, а UI показывает
 * пользователю понятное сообщение (см. ClustersView.tsx → state NotFound).
 */
import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type ClustersResponse } from '@/lib/api'

const LONG_POLL_WAIT_SEC = 25  // сколько ждать на backend (до 60)
const REFETCH_AFTER_TIMEOUT_MS = 100  // почти мгновенно — long polling сам контролирует ритм
const REFETCH_INITIAL_MS = 1000  // первая попытка после запуска операции
const REFETCH_AFTER_TRANSIENT_ERROR_MS = 5000  // retry-интервал для 5xx/network
const MAX_TRANSIENT_RETRIES = 3  // максимум попыток для 5xx/network (потом stop)

// ============================================================
// Clusters polling (long polling)
// ============================================================
export function useClustersPolling(taskId: string | null, enabled: boolean) {
  return useQuery<ClustersResponse>({
    queryKey: ['clusters', taskId],
    queryFn: () => api.getClusters(taskId!, LONG_POLL_WAIT_SEC),
    enabled: !!taskId && enabled,
    // 404 (Task not found) и 403 (Access denied) — не ретраить
    // (задача недоступна навсегда). Остальные ошибки (5xx, network) —
    // ограниченный retry.
    retry: (failureCount, error) => {
      if (error instanceof ApiError && (error.status === 404 || error.status === 403)) {
        return false
      }
      return failureCount < MAX_TRANSIENT_RETRIES
    },
    refetchInterval: (query) => {
      const data = query.state.data
      const error = query.state.error as ApiError | undefined

      // 404 (Task not found) / 403 (Access denied) — прекратить polling
      // бесконечно. Задача удалена / недоступна — нет смысла стучаться.
      if (error && (error.status === 404 || error.status === 403)) {
        return false
      }

      if (!data) {
        // Если ошибка 5xx/network — реже ретраим (каждые 5 сек),
        // иначе начальная задержка 1 сек (нормальный старт polling'а).
        if (error) return REFETCH_AFTER_TRANSIENT_ERROR_MS
        return REFETCH_INITIAL_MS
      }
      if (
        data.state.status === 'done' ||
        data.state.status === 'failed'
      ) {
        return false
      }
      // running — long polling сам подождёт, повторяем сразу после его таймаута
      return REFETCH_AFTER_TIMEOUT_MS
    },
  })
}
