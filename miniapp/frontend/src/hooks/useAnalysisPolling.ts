/**
 * Хук для polling статуса длительных аналитических операций
 * (очаги, LLM-резюме).
 *
 * Использует LONG POLLING:
 * - При статусе running — отправляет запрос с ?wait=25
 *   backend держит соединение до 25 сек, ожидая завершения
 * - После done/failed — refetchInterval=false (запросы прекращаются)
 * - При таймауте — refetchInterval=100ms (мгновенный повтор)
 *
 * Это устраняет ~30 коротких поллинг-запросов за время генерации,
 * оставляя 1-2 long-polling запроса на всю операцию.
 */
import { useQuery } from '@tanstack/react-query'
import { api, type ClustersResponse, type LLMSummaryResponse } from '@/lib/api'

const LONG_POLL_WAIT_SEC = 25  // сколько ждать на backend (до 60)
const REFETCH_AFTER_TIMEOUT_MS = 100  // почти мгновенно — long polling сам контролирует ритм
const REFETCH_INITIAL_MS = 1000  // первая попытка после запуска операции

// ============================================================
// Clusters polling (long polling)
// ============================================================
export function useClustersPolling(taskId: string | null, enabled: boolean) {
  return useQuery<ClustersResponse>({
    queryKey: ['clusters', taskId],
    queryFn: () => api.getClusters(taskId!, LONG_POLL_WAIT_SEC),
    enabled: !!taskId && enabled,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return REFETCH_INITIAL_MS
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

// ============================================================
// LLM summary polling (long polling)
// ============================================================
export function useLLMSummaryPolling(taskId: string | null, enabled: boolean) {
  return useQuery<LLMSummaryResponse>({
    queryKey: ['llm-summary', taskId],
    queryFn: () => api.getLLMSummary(taskId!, LONG_POLL_WAIT_SEC),
    enabled: !!taskId && enabled,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return REFETCH_INITIAL_MS
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
