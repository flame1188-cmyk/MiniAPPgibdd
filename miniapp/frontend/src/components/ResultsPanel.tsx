/**
 * Панель результатов: показывает карту, аналитику и кнопки выгрузки файлов.
 */
import { useState } from 'react'
import { api, type TaskStatusResponse } from '@/lib/api'
import { haptic, showAlert } from '@/lib/telegram'
import { MapFrame } from './MapFrame'
import { AnalyticsView } from './AnalyticsView'
import { ClustersView } from './ClustersView'
import { PointStatsView } from './PointStatsView'
import { LLMAnalysisView } from './LLMAnalysisView'

interface ResultsPanelProps {
  task: TaskStatusResponse
}

type Tab = 'map' | 'analytics' | 'clusters' | 'point' | 'llm' | 'files'

export function ResultsPanel({ task }: ResultsPanelProps) {
  const [tab, setTab] = useState<Tab>('map')

  const tabs: { id: Tab; label: string; visible: boolean }[] = [
    { id: 'map', label: 'Карта', visible: true },
    { id: 'analytics', label: 'Аналитика', visible: !!task.analytics },
    { id: 'clusters', label: 'Очаги', visible: true },
    { id: 'point', label: 'По точке', visible: true },
    { id: 'llm', label: 'ИИ-анализ', visible: true },
    { id: 'files', label: 'Файлы', visible: true },
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
            {task.period}
          </div>
        </div>
        <div className="text-xs opacity-60">
          {task.total_dtp} ДТП · {task.total_dead} погибших · {task.total_injured} раненых
        </div>
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
      {tab === 'map' && <MapFrame taskId={task.task_id} />}

      {tab === 'analytics' && task.analytics && (
        <AnalyticsView analytics={task.analytics} taskId={task.task_id} />
      )}

      {tab === 'clusters' && <ClustersView task={task} />}

      {tab === 'point' && <PointStatsView task={task} />}

      {tab === 'llm' && <LLMAnalysisView task={task} />}

      {tab === 'files' && <FilesList taskId={task.task_id} />}
    </div>
  )
}

// ============================================================
// Список файлов — ленивая генерация по нажатию
// ============================================================
function FilesList({ taskId }: { taskId: string }) {
  const [generating, setGenerating] = useState<string | null>(null)

  const handleGenerate = async (fileType: 'dtp_cards' | 'dtp_participants') => {
    haptic('medium')
    setGenerating(fileType)
    try {
      await api.generateExcel(taskId, fileType)
    } catch (err: any) {
      haptic('error')
      await showAlert(`Не удалось сгенерировать файл:\n${err.message}`)
    } finally {
      setGenerating(null)
    }
  }

  const isGeneratingCards = generating === 'dtp_cards'
  const isGeneratingUch = generating === 'dtp_participants'
  const isAnyGenerating = generating !== null

  return (
    <div className="tg-card space-y-3">
      <div className="tg-section-header m-0">Файлы для скачивания</div>
      <div className="text-xs opacity-60">
        Файлы генерируются по запросу. Первый запрос занимает 5-8 секунд.
      </div>

      <button
        onClick={() => handleGenerate('dtp_cards')}
        disabled={isAnyGenerating}
        className="w-full flex items-center justify-between active:opacity-70"
        style={{
          opacity: isAnyGenerating ? 0.5 : 1,
          textDecoration: 'none',
          color: 'var(--tg-color-text, #000000)',
        }}
      >
        <div className="flex-1 min-w-0 text-left">
          <div className="font-medium text-sm">Список ДТП (Excel)</div>
          <div className="text-xs opacity-60">Карточки ДТП за выбранный период</div>
        </div>
        <div
          className="ml-3 px-3 py-1.5 rounded-lg text-xs font-medium"
          style={{
            backgroundColor: 'var(--tg-color-button, #2481cc)',
            color: 'var(--tg-color-button-text, #ffffff)',
          }}
        >
          {isGeneratingCards ? 'Генерация...' : 'Выгрузить'}
        </div>
      </button>

      <button
        onClick={() => handleGenerate('dtp_participants')}
        disabled={isAnyGenerating}
        className="w-full flex items-center justify-between active:opacity-70"
        style={{
          opacity: isAnyGenerating ? 0.5 : 1,
          textDecoration: 'none',
          color: 'var(--tg-color-text, #000000)',
        }}
      >
        <div className="flex-1 min-w-0 text-left">
          <div className="font-medium text-sm">Список участников (Excel)</div>
          <div className="text-xs opacity-60">Участники ДТП за выбранный период</div>
        </div>
        <div
          className="ml-3 px-3 py-1.5 rounded-lg text-xs font-medium"
          style={{
            backgroundColor: 'var(--tg-color-button, #2481cc)',
            color: 'var(--tg-color-button-text, #ffffff)',
          }}
        >
          {isGeneratingUch ? 'Генерация...' : 'Выгрузить'}
        </div>
      </button>
    </div>
  )
}
