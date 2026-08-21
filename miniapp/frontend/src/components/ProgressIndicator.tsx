/**
 * Индикатор прогресса выполнения задачи.
 *
 * Показывает текущий этап, процент выполнения и анимированный прогресс-бар.
 */
import type { TaskStatusResponse } from '@/lib/api'
import { statusLabel } from '@/lib/utils'

interface ProgressIndicatorProps {
  task: TaskStatusResponse
}

export function ProgressIndicator({ task }: ProgressIndicatorProps) {
  const isFailed = task.status === 'failed'
  const isDone = task.status === 'done'

  // N6: backpressure — показываем позицию в очереди
  const queueAhead = task.queue_ahead
  const showQueue = queueAhead != null && queueAhead > 0

  return (
    <div className="tg-card">
      <div className="flex items-center justify-between mb-2">
        <div className="tg-section-header m-0">
          {task.region_name || `Регион ${task.region_code}`}
        </div>
        <div className="text-xs opacity-60">{task.period}</div>
      </div>

      {/* N6: индикатор очереди */}
      {showQueue && (
        <div className="text-xs opacity-70 mb-1" style={{ color: 'var(--tg-color-hint, #999)' }}>
          В очереди: перед вами {queueAhead} {queueAhead === 1 ? 'задача' : queueAhead < 5 ? 'задачи' : 'задач'}
        </div>
      )}

      {/* Прогресс-бар */}
      <div
        className="h-2 rounded-full overflow-hidden mb-2"
        style={{ backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)' }}
      >
        <div
          className={isDone ? 'h-full transition-all duration-500' : 'h-full progress-stripes transition-all duration-500'}
          style={{
            width: `${task.progress}%`,
            backgroundColor: isFailed
              ? 'var(--tg-color-destructive, #ff3b30)'
              : showQueue
                ? 'var(--tg-color-hint, #999)'
                : 'var(--tg-color-button, #2481cc)',
          }}
        />
      </div>

      <div className="flex items-center justify-between text-xs">
        <span className={isFailed ? 'text-red-500' : ''}>
          {isFailed
            ? `Ошибка: ${task.error ?? 'неизвестная'}`
            : showQueue
              ? `Ожидание (очередь ${task.queue_position})`
              : statusLabel(task.status)}
        </span>
        <span className="opacity-60">{task.progress}%</span>
      </div>
    </div>
  )
}
