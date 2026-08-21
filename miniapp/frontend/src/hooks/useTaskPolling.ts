/**
 * Хук для polling статуса задачи.
 *
 * Опрашивает /api/dtp/tasks/{id} каждые 1.5 сек, пока задача не завершится
 * (done / failed). После завершения останавливается.
 *
 * Sprint 7 fix: при 404 (задача не найдена — например, после сбоя БД)
 * останавливаем polling и прокидываем isNotFound=true, чтобы UI мог
 * показать пользователю понятное сообщение вместо бесконечного спиннера.
 */
import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type TaskStatusResponse } from '@/lib/api'

const POLL_INTERVAL = 1500 // ms

export function useTaskPolling(taskId: string | null) {
  const query = useQuery<TaskStatusResponse>({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId!),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const data = q.state.data
      if (!data) return POLL_INTERVAL
      if (data.status === 'done' || data.status === 'failed') {
        return false // Останавливаем polling
      }
      return POLL_INTERVAL
    },
    // Sprint 7 fix: при 404 не ретраим — задача удалена навсегда
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) {
        return false // Не ретраить 404
      }
      return failureCount < 1
    },
  })

  // Sprint 7 fix: detect 404 — задача не найдена
  const error = query.error
  const isNotFound =
    error instanceof ApiError && error.status === 404

  return {
    ...query,
    isNotFound,
  }
}
