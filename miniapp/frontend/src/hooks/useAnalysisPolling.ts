/**
 * Хук для polling статуса длительных аналитических операций
 * (очаги, LLM-резюме).
 *
 * Опрашивает endpoint каждые 2 сек, пока операция выполняется (running).
 * После завершения (done/failed) останавливается.
 */
import { useQuery } from '@tanstack/react-query'
import { api, type ClustersResponse, type LLMSummaryResponse } from '@/lib/api'

const POLL_INTERVAL = 2000 // ms

// ============================================================
// Clusters polling
// ============================================================
export function useClustersPolling(taskId: string | null, enabled: boolean) {
  return useQuery<ClustersResponse>({
    queryKey: ['clusters', taskId],
    queryFn: () => api.getClusters(taskId!),
    enabled: !!taskId && enabled,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return POLL_INTERVAL
      if (
        data.state.status === 'done' ||
        data.state.status === 'failed'
      ) {
        return false
      }
      return POLL_INTERVAL
    },
  })
}

// ============================================================
// LLM summary polling
// ============================================================
export function useLLMSummaryPolling(taskId: string | null, enabled: boolean) {
  return useQuery<LLMSummaryResponse>({
    queryKey: ['llm-summary', taskId],
    queryFn: () => api.getLLMSummary(taskId!),
    enabled: !!taskId && enabled,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return POLL_INTERVAL
      if (
        data.state.status === 'done' ||
        data.state.status === 'failed'
      ) {
        return false
      }
      return POLL_INTERVAL
    },
  })
}
