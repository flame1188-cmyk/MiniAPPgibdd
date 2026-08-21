/**
 * Список последних задач пользователя.
 *
 * Возможности:
 *  - сворачивание/разворачивание списка (состояние сохраняется в
 *    localStorage, чтобы между сессиями пользователь не видел длинный
 *    список, если сам его свернул)
 *  - удаление задачи через иконку 🗑 — удаляет из БД, in-memory и
 *    файлы с диска; с подтверждением через Telegram showConfirm
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type TaskStatusResponse } from '@/lib/api'
import { cn, statusLabel } from '@/lib/utils'
import { haptic, showAlert, showConfirm } from '@/lib/telegram'

interface HistoryListProps {
  onSelectTask: (taskId: string) => void
}

const COLLAPSE_KEY = 'history-list-collapsed'

/** Читаем сохранённое состояние сворачивания из localStorage. */
function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
}

/** Сохраняем состояние сворачивания в localStorage. */
function writeCollapsed(value: boolean): void {
  try {
    localStorage.setItem(COLLAPSE_KEY, value ? '1' : '0')
  } catch {
    // ignore — если localStorage недоступен, просто не персистим
  }
}

export function HistoryList({ onSelectTask }: HistoryListProps) {
  const queryClient = useQueryClient()
  const [collapsed, setCollapsed] = useState<boolean>(readCollapsed)

  const { data: tasks, isLoading, error } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.listTasks(20),
  })

  // === Мутация удаления задачи ===
  // Оптимистичное обновление: задача убирается из кэша сразу при клике,
  // до получения ответа сервера. Если запрос упадёт — возвращаем обратно.
  const deleteMutation = useMutation<
    { ok: boolean; task_id: string; deleted: boolean },
    Error,
    string,
    { previousTasks: TaskStatusResponse[] | undefined }
  >({
    mutationFn: (taskId: string) => api.deleteTask(taskId),

    onMutate: async (taskId: string) => {
      // Отменяем исходящие refetch, чтобы они не перезаписали оптимистичное
      // обновление.
      await queryClient.cancelQueries({ queryKey: ['tasks'] })

      // Снимок текущего кэша — для отката в onError.
      const previousTasks = queryClient.getQueryData<TaskStatusResponse[]>(['tasks'])

      // Оптимистично убираем задачу из кэша — UI обновится мгновенно.
      if (previousTasks) {
        queryClient.setQueryData<TaskStatusResponse[]>(
          ['tasks'],
          previousTasks.filter((t) => t.task_id !== taskId)
        )
      }

      // Возвращаем контекст для onError (восстановление кэша).
      return { previousTasks }
    },

    onSuccess: () => {
      haptic('success')
    },

    onError: async (err: Error, _taskId: string, context) => {
      haptic('error')
      // Восстанавливаем кэш — задача вернётся в список.
      if (context?.previousTasks) {
        queryClient.setQueryData(['tasks'], context.previousTasks)
      }
      await showAlert(`Не удалось удалить задачу:\n${err.message}`)
    },

    // Финальный refetch в любом случае — чтобы получить актуальный список
    // с сервера (включая задачи, которые могли добавиться параллельно, и
    // чтобы убедиться, что оптимистичное удаление совпадает с реальностью).
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })

  const toggleCollapsed = () => {
    haptic('light')
    const next = !collapsed
    setCollapsed(next)
    writeCollapsed(next)
  }

  const handleDelete = async (
    taskId: string,
    regionName: string,
    e: React.MouseEvent
  ) => {
    // Останавливаем всплытие, чтобы не сработал onClick карточки (открытие задачи)
    e.stopPropagation()
    haptic('light')

    const ok = await showConfirm(
      `Удалить задачу?\n${regionName}\n\nФайлы на диске и данные в БД будут удалены безвозвратно.`
    )
    if (!ok) return

    deleteMutation.mutate(taskId)
  }

  // === Рендер: загрузка ===
  if (isLoading) {
    return (
      <div className="tg-card text-center text-sm opacity-60">
        Загрузка истории…
      </div>
    )
  }

  // === Рендер: ошибка ===
  if (error) {
    return (
      <div className="tg-card text-center text-sm" style={{
        color: 'var(--tg-color-destructive, #ff3b30)',
      }}>
        Не удалось загрузить историю
      </div>
    )
  }

  // === Рендер: пусто ===
  if (!tasks || tasks.length === 0) {
    return (
      <div className="tg-card text-center text-sm opacity-60">
        История пуста. Создайте первый запрос выше.
      </div>
    )
  }

  // === Рендер: список ===
  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={toggleCollapsed}
        className="tg-section-header px-1 w-full flex items-center justify-between text-left active:opacity-70 transition-opacity"
        style={{ background: 'transparent', border: 'none', cursor: 'pointer' }}
      >
        <span>Последние запросы</span>
        <span
          className="text-xs opacity-50"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '4px',
          }}
        >
          {tasks.length > 0 && (
            <span
              className="px-1.5 py-0.5 rounded-full"
              style={{
                backgroundColor: 'var(--tg-color-hint, #999999)',
                color: 'var(--tg-color-button-text, #ffffff)',
                fontSize: '10px',
                lineHeight: 1,
              }}
            >
              {tasks.length}
            </span>
          )}
          <span
            style={{
              display: 'inline-block',
              transition: 'transform 0.15s ease',
              transform: collapsed ? 'rotate(-90deg)' : 'rotate(0deg)',
            }}
          >
            ▾
          </span>
        </span>
      </button>

      {!collapsed && (
        <div className="space-y-2">
          {tasks.map((task) => {
            const regionName = task.region_name || `Регион ${task.region_code}`
            const isDeleting =
              deleteMutation.isPending &&
              deleteMutation.variables === task.task_id

            return (
              <div
                key={task.task_id}
                className={cn(
                  'tg-card relative transition-opacity',
                  isDeleting && 'opacity-50'
                )}
              >
                <button
                  type="button"
                  onClick={() => {
                    if (isDeleting) return
                    haptic('light')
                    onSelectTask(task.task_id)
                  }}
                  disabled={isDeleting}
                  className="w-full text-left active:opacity-70 transition-opacity pr-9"
                  style={{ background: 'transparent', border: 'none', cursor: 'pointer' }}
                >
                  <div className="flex items-center justify-between mb-1">
                    <div className="font-medium text-sm truncate pr-2">
                      {regionName}
                    </div>
                    <StatusBadge status={task.status} />
                  </div>
                  <div className="text-xs opacity-60">
                    {task.period}
                  </div>
                </button>

                {/* Кнопка удаления — абсолютно позиционирована в правом верхнем углу карточки */}
                <button
                  type="button"
                  onClick={(e) => handleDelete(task.task_id, regionName, e)}
                  disabled={isDeleting}
                  aria-label="Удалить задачу"
                  className="absolute top-2 right-2 p-1.5 rounded-lg transition-opacity"
                  style={{
                    background: 'transparent',
                    border: 'none',
                    color: 'var(--tg-color-destructive, #ff3b30)',
                    cursor: isDeleting ? 'wait' : 'pointer',
                    fontSize: '16px',
                    lineHeight: 1,
                    opacity: 0.5,
                  }}
                  onMouseEnter={(e) => {
                    if (!isDeleting) e.currentTarget.style.opacity = '1'
                  }}
                  onMouseLeave={(e) => {
                    if (!isDeleting) e.currentTarget.style.opacity = '0.5'
                  }}
                >
                  {isDeleting ? '…' : '🗑'}
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    done: 'var(--tg-color-button, #2481cc)',
    failed: 'var(--tg-color-destructive, #ff3b30)',
    pending: 'var(--tg-color-hint, #999999)',
    fetching: 'var(--tg-color-link, #2481cc)',
    parsing: 'var(--tg-color-link, #2481cc)',
    analytics: 'var(--tg-color-link, #2481cc)',
    generating: 'var(--tg-color-link, #2481cc)',
  }

  return (
    <span
      className="text-xs px-2 py-0.5 rounded-full whitespace-nowrap"
      style={{
        backgroundColor: colors[status] ?? 'var(--tg-color-hint, #999)',
        color: 'var(--tg-color-button-text, #ffffff)',
      }}
    >
      {statusLabel(status)}
    </span>
  )
}
