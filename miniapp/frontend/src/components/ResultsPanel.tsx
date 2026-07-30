/**
 * Панель результатов: показывает готовые файлы, HTML-карту и аналитику.
 */
import { useState } from 'react'
import { api, type TaskStatusResponse } from '@/lib/api'
import { haptic } from '@/lib/telegram'
import { formatSize, statusLabel } from '@/lib/utils'
import { MapFrame } from './MapFrame'

interface ResultsPanelProps {
  task: TaskStatusResponse
}

type Tab = 'map' | 'analytics' | 'files'

export function ResultsPanel({ task }: ResultsPanelProps) {
  const [tab, setTab] = useState<Tab>('map')

  const cardsFile = task.files.find((f) => f.type === 'dtp_cards')
  const uchFile = task.files.find((f) => f.type === 'dtp_participants')
  const mapFile = task.files.find((f) => f.type === 'map_html')

  const tabs: { id: Tab; label: string; visible: boolean }[] = [
    { id: 'map', label: 'Карта', visible: !!mapFile },
    { id: 'analytics', label: 'Аналитика', visible: !!task.analytics },
    { id: 'files', label: 'Файлы', visible: task.files.length > 0 },
  ]
  const visibleTabs = tabs.filter((t) => t.visible)

  return (
    <div className="space-y-3">
      {/* Заголовок задачи */}
      <div className="tg-card">
        <div className="flex items-center justify-between mb-1">
          <div className="font-semibold">
            {task.region_name || `Регион ${task.region_code}`}
          </div>
          <div
            className="text-xs px-2 py-0.5 rounded-full"
            style={{
              backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
              color: 'var(--tg-color-text, #000000)',
            }}
          >
            {statusLabel(task.status)}
          </div>
        </div>
        <div className="text-xs opacity-60">Период: {task.period}</div>
      </div>

      {/* Табы */}
      {visibleTabs.length > 0 && (
        <div className="flex gap-1 p-1 rounded-xl" style={{
          backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
        }}>
          {visibleTabs.map((t) => (
            <button
              key={t.id}
              onClick={() => {
                setTab(t.id)
                haptic('light')
              }}
              className="flex-1 py-2 px-3 text-sm font-medium rounded-lg transition-colors"
              style={{
                backgroundColor:
                  tab === t.id
                    ? 'var(--tg-color-section-bg, #ffffff)'
                    : 'transparent',
                color:
                  tab === t.id
                    ? 'var(--tg-color-button, #2481cc)'
                    : 'var(--tg-color-hint, #999999)',
              }}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}

      {/* Содержимое таба */}
      {tab === 'map' && mapFile && <MapFrame taskId={task.task_id} />}

      {tab === 'analytics' && task.analytics && (
        <AnalyticsView analytics={task.analytics} />
      )}

      {tab === 'files' && (
        <FilesList
          task={task}
          cardsFile={cardsFile}
          uchFile={uchFile}
          mapFile={mapFile}
        />
      )}
    </div>
  )
}

// ============================================================
// Аналитика
// ============================================================
function AnalyticsView({ analytics }: { analytics: Record<string, unknown> }) {
  const entries = Object.entries(analytics)

  return (
    <div className="tg-card">
      <div className="tg-section-header">Сводка</div>
      <div className="space-y-2">
        {entries.map(([key, value]) => (
          <div
            key={key}
            className="flex items-center justify-between py-2 border-b last:border-b-0"
            style={{ borderColor: 'var(--tg-color-secondary-bg, #f1f1f1)' }}
          >
            <span className="text-sm opacity-80">{formatAnalyticsKey(key)}</span>
            <span className="font-medium text-sm">{formatAnalyticsValue(value)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function formatAnalyticsKey(key: string): string {
  const labels: Record<string, string> = {
    total_dtp: 'Всего ДТП',
    total_dead: 'Погибших',
    total_injured: 'Ранено',
    total_participants: 'Участников',
    severity_rate: 'Тяжесть',
    comparison: 'Сравнение с прошлым годом',
  }
  return labels[key] ?? key
}

function formatAnalyticsValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value.toString() : value.toFixed(2)
  }
  if (typeof value === 'object') {
    return JSON.stringify(value)
  }
  return String(value)
}

// ============================================================
// Список файлов
// ============================================================
interface FilesListProps {
  task: TaskStatusResponse
  cardsFile?: { type: string; filename: string; size_bytes: number; mime: string }
  uchFile?: { type: string; filename: string; size_bytes: number; mime: string }
  mapFile?: { type: string; filename: string; size_bytes: number; mime: string }
}

function FilesList({ task, cardsFile, uchFile, mapFile }: FilesListProps) {
  const files = [cardsFile, uchFile, mapFile].filter(Boolean) as {
    type: string
    filename: string
    size_bytes: number
    mime: string
  }[]

  const typeLabels: Record<string, string> = {
    dtp_cards: 'Карточки ДТП (Excel)',
    dtp_participants: 'Участники ДТП (Excel)',
    map_html: 'Карта (HTML)',
  }

  return (
    <div className="space-y-2">
      {files.map((file) => (
        <a
          key={file.type}
          href={api.getDownloadUrl(task.task_id, file.type)}
          onClick={() => haptic('medium')}
          className="tg-card flex items-center justify-between active:opacity-70"
          style={{ textDecoration: 'none' }}
        >
          <div className="flex-1 min-w-0">
            <div className="font-medium text-sm truncate">
              {typeLabels[file.type] ?? file.type}
            </div>
            <div className="text-xs opacity-60 truncate">
              {file.filename} · {formatSize(file.size_bytes)}
            </div>
          </div>
          <div
            className="ml-3 px-3 py-1.5 rounded-lg text-xs font-medium"
            style={{
              backgroundColor: 'var(--tg-color-button, #2481cc)',
              color: 'var(--tg-color-button-text, #ffffff)',
            }}
          >
            Скачать
          </div>
        </a>
      ))}
    </div>
  )
}
