/**
 * Панель результатов: показывает готовые файлы, HTML-карту и аналитику.
 *
 * Stabilization P1 #3 (2026-08-20): Code splitting через React.lazy.
 * Раньше все 5 View (Map/Analytics/Clusters/PointStats/LLM) грузились
 * в начальном бандле — ~250 КБ gzip лишнего на первом заходе.
 *
 * Теперь: 4 тяжёлых View загружаются lazy (отдельные чанки .js),
 * когда пользователь первый раз открывает соответствующую вкладку.
 * MapFrame остаётся eager (открывается по умолчанию).
 *
 * Suspense fallback — простой spinner на bg-secondary, чтобы пользователь
 * видел, что что-то грузится (не пустой экран).
 */
import { useState, lazy, Suspense } from 'react'
import { api, type TaskStatusResponse } from '@/lib/api'
import { haptic } from '@/lib/telegram'
import { formatSize, statusLabel } from '@/lib/utils'
import { MapFrame } from './MapFrame'

// Lazy-loaded views — каждый в свой chunk:
//   - AnalyticsView.tsx  → /assets/analytics-*.js
//   - ClustersView.tsx   → /assets/clusters-*.js
//   - PointStatsView.tsx → /assets/point-*.js
//   - LLMAnalysisView.tsx → /assets/llm-*.js
// recharts и marked грузятся только когда нужен первый View,
// который их использует (recharts — analytics/clusters, marked — LLM).
const AnalyticsView = lazy(() =>
  import('./AnalyticsView').then((m) => ({ default: m.AnalyticsView })),
)
const ClustersView = lazy(() =>
  import('./ClustersView').then((m) => ({ default: m.ClustersView })),
)
const PointStatsView = lazy(() =>
  import('./PointStatsView').then((m) => ({ default: m.PointStatsView })),
)
const LLMAnalysisView = lazy(() =>
  import('./LLMAnalysisView').then((m) => ({ default: m.LLMAnalysisView })),
)

interface ResultsPanelProps {
  task: TaskStatusResponse
}

type Tab = 'map' | 'analytics' | 'clusters' | 'point' | 'llm' | 'files'

export function ResultsPanel({ task }: ResultsPanelProps) {
  const [tab, setTab] = useState<Tab>('map')

  const cardsFile = task.files.find((f) => f.type === 'dtp_cards')
  const uchFile = task.files.find((f) => f.type === 'dtp_participants')
  const mapFile = task.files.find((f) => f.type === 'map_html')

  const tabs: { id: Tab; label: string; visible: boolean }[] = [
    { id: 'map', label: 'Карта', visible: !!mapFile },
    { id: 'analytics', label: 'Аналитика', visible: !!task.analytics },
    { id: 'clusters', label: 'Очаги', visible: true },
    { id: 'point', label: 'По точке', visible: true },
    { id: 'llm', label: 'ИИ-анализ', visible: true },
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
        <div className="flex gap-1 p-1 rounded-xl overflow-x-auto" style={{
          backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
        }}>
          {visibleTabs.map((t) => (
            <button
              key={t.id}
              onClick={() => {
                setTab(t.id)
                haptic('light')
              }}
              className="flex-1 min-w-[70px] py-2 px-2 text-xs font-medium rounded-lg transition-colors whitespace-nowrap"
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
        <Suspense fallback={<ViewFallback label="Аналитика" />}>
          <AnalyticsView analytics={task.analytics} />
        </Suspense>
      )}

      {tab === 'clusters' && (
        <Suspense fallback={<ViewFallback label="Очаги" />}>
          <ClustersView task={task} />
        </Suspense>
      )}

      {tab === 'point' && (
        <Suspense fallback={<ViewFallback label="Статистика по точке" />}>
          <PointStatsView task={task} />
        </Suspense>
      )}

      {tab === 'llm' && (
        <Suspense fallback={<ViewFallback label="ИИ-анализ" />}>
          <LLMAnalysisView task={task} />
        </Suspense>
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
// Suspense fallback — простой spinner с подписью.
// Показывается < 500ms обычно (chunk уже в кэше браузера), но
// для первого захода на вкладку — пользователь видит, что грузится.
// ============================================================
function ViewFallback({ label }: { label: string }) {
  return (
    <div
      className="flex flex-col items-center justify-center py-12 space-y-2"
      style={{
        backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
      }}
    >
      <div
        className="w-8 h-8 rounded-full border-2 border-t-transparent animate-spin"
        style={{
          borderColor: 'var(--tg-color-button, #2481cc)',
          borderTopColor: 'transparent',
        }}
      />
      <div className="text-xs opacity-60">Загрузка: {label}…</div>
    </div>
  )
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
