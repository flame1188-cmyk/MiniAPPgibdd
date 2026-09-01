/**
 * Главный компонент Mini App.
 *
 * Layout:
 *  - Шапка с переключателем вкладок: «ДТП» / «Выгрузка файлов» / «НП БДД» / «Редактор карты»
 *  - Вкладка «ДТП»:
 *      - Форма запроса
 *      - Прогресс активной задачи
 *      - Результаты (карта, аналитика, очаги, ИИ-анализ, файлы)
 *      - История последних запросов
 *  - Вкладка «Выгрузка файлов»:
 *      - Выбор региона и периода
 *      - Кнопка «Выгрузить ZIP-архив»
 *  - Вкладка «НП БДД»:
 *      - NpBddView (KPI + 2 графика + заморозка)
 *  - Вкладка «Редактор карты» (только для редакторов):
 *      - PolygonEditorView (Leaflet карта с полигонами)
 */
import { useState, useEffect, lazy, Suspense } from 'react'
import { StructuredForm } from '@/components/StructuredForm'
import { CamerasWidget } from '@/components/CamerasWidget'
import { ProgressIndicator } from '@/components/ProgressIndicator'
import { ResultsPanel } from '@/components/ResultsPanel'
import { HistoryList } from '@/components/HistoryList'
import { VersionBanner } from '@/components/VersionBanner'
import { api } from '@/lib/api'

const ExportView = lazy(() => import('@/components/ExportView').then(m => ({ default: m.ExportView })))
const NpBddView = lazy(() => import('@/components/NpBddView').then(m => ({ default: m.NpBddView })))
const PolygonEditorView = lazy(() => import('@/components/PolygonEditorView').then(m => ({ default: m.PolygonEditorView })))

function TabSpinner() {
  return (
    <div className="tg-card flex items-center justify-center py-8">
      <div className="text-xs opacity-50">Загрузка...</div>
    </div>
  )
}
import { useTaskPolling } from '@/hooks/useTaskPolling'
import { useVersionCheck } from '@/hooks/useVersionCheck'
import { haptic } from '@/lib/telegram'
import {
  getCurrentUser,
  getContainerMaxWidth,
  isInsideTelegram,
  isTelegramDesktop,
  isFullscreenSupported,
  isFullscreenActive,
  requestAppFullscreen,
  exitAppFullscreen,
  onFullscreenChange,
} from '@/lib/telegram'

type Tab = 'dtp' | 'export' | 'np-bdd' | 'polygons'

export default function App() {
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('dtp')
  const [fullscreen, setFullscreen] = useState<boolean>(isFullscreenActive())
  const [isPolygonEditor, setIsPolygonEditor] = useState(false)

  const { data: task, isError } = useTaskPolling(activeTaskId)

  // Проверка версии: раз в 60 сек опрашивает /api/version.
  const hasUpdate = useVersionCheck()

  // Проверка доступа к редактору полигонов (один раз при монтировании)
  useEffect(() => {
    api.polygonCheckAccess()
      .then(r => setIsPolygonEditor(r.is_editor))
      .catch(() => { /* Не редактор — вкладка не покажется */ })
  }, [])

  const user = getCurrentUser()
  const showDevWarning = !isInsideTelegram()
  const isDesktop = isTelegramDesktop()
  const containerMaxWidth = getContainerMaxWidth()
  // Кнопка fullscreen имеет смысл только на десктопе: на мобильных
  // MiniApp и так открывается на весь экран.
  const showFullscreenButton = isDesktop && isFullscreenSupported()

  const handleSelectTask = (taskId: string) => {
    setActiveTaskId(taskId)
    // Прокрутка вверх при выборе задачи из истории
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const handleTabChange = (newTab: Tab) => {
    if (newTab === tab) return
    haptic('light')
    setTab(newTab)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  // Подписка на смену fullscreen-режима (пользователь мог сам выйти через ESC)
  useEffect(() => {
    const unsubscribeFs = onFullscreenChange((isFs) => setFullscreen(isFs))
    return unsubscribeFs
  }, [])

  const toggleFullscreen = async () => {
    haptic('light')
    try {
      if (fullscreen) {
        await exitAppFullscreen()
      } else {
        await requestAppFullscreen()
      }
    } catch (err) {
      console.warn('[App] fullscreen toggle failed:', err)
    }
  }

  return (
    <div className="min-h-screen pb-8">
      {/* Баннер обновления — поверх всего, фикс сверху. */}
      <VersionBanner visible={hasUpdate} />

      <div className={`${containerMaxWidth} mx-auto px-4 py-4 space-y-4`}>
        {/* Шапка */}
        <header className="flex items-center justify-between gap-2 pb-2">
          <div className="text-left">
            <h1 className="text-xl font-bold mb-0.5">ДТП Статистика</h1>
            {user ? (
              <p className="text-xs opacity-60">
                Привет, {user.first_name}!
              </p>
            ) : (
              <p className="text-xs opacity-60">
                Данные ГИБДД · stat.gibdd.ru
              </p>
            )}
          </div>
          {/* Кнопка полноэкранного режима — только на десктопе */}
          {showFullscreenButton && (
            <button
              onClick={toggleFullscreen}
              className="shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
              style={{
                backgroundColor: fullscreen
                  ? 'var(--tg-color-link, #2481cc)'
                  : 'var(--tg-color-section-bg, #ffffff)',
                color: fullscreen
                  ? '#ffffff'
                  : 'var(--tg-color-link, #2481cc)',
                border: '1px solid var(--tg-color-link, #2481cc)',
              }}
              title={fullscreen ? 'Выйти из полноэкранного режима' : 'Открыть в полноэкранном режиме (без панели задач)'}
            >
              {fullscreen ? 'Выйти' : 'Полный экран'}
            </button>
          )}
        </header>

        {/* Переключатель вкладок */}
        <div className="flex gap-1 p-1 rounded-xl bg-tg-secondary-bg">
          <button
            className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium transition-colors ${
              tab === 'dtp'
                ? 'bg-tg-section-bg text-tg-text shadow-sm'
                : 'text-tg-hint'
            }`}
            onClick={() => handleTabChange('dtp')}
          >
            ДТП
          </button>
          <button
            className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium transition-colors ${
              tab === 'export'
                ? 'bg-tg-section-bg text-tg-text shadow-sm'
                : 'text-tg-hint'
            }`}
            onClick={() => handleTabChange('export')}
          >
            Выгрузка файлов
          </button>
          <button
            className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium transition-colors ${
              tab === 'np-bdd'
                ? 'bg-tg-section-bg text-tg-text shadow-sm'
                : 'text-tg-hint'
            }`}
            onClick={() => handleTabChange('np-bdd')}
          >
            НП БДД
          </button>
          {isPolygonEditor && (
            <button
              className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium transition-colors ${
                tab === 'polygons'
                  ? 'bg-tg-section-bg text-tg-text shadow-sm'
                  : 'text-tg-hint'
              }`}
              onClick={() => handleTabChange('polygons')}
            >
              Карта
            </button>
          )}
        </div>

        {/* Подсказка для desktop-пользователей */}
        {isDesktop && !fullscreen && showFullscreenButton && (
          <div
            className="rounded-xl p-2.5 text-xs leading-relaxed"
            style={{
              backgroundColor: 'rgba(36, 129, 204, 0.08)',
              color: 'var(--tg-color-link, #2481cc)',
            }}
          >
            Desktop-режим: нажмите «Полный экран» в шапке,
            чтобы развернуть приложение на весь экран без панели задач.
          </div>
        )}

        {/* Предупреждение о dev-режиме */}
        {showDevWarning && (
          <div
            className="rounded-xl p-3 text-xs"
            style={{
              backgroundColor: 'rgba(255, 149, 0, 0.1)',
              color: '#ff9500',
            }}
          >
            Запущено вне Telegram. Запросы к API не будут аутентифицированы.
            Откройте приложение через Telegram-бота для полноценной работы.
          </div>
        )}

        {/* --- Вкладка «ДТП» --- */}
        {tab === 'dtp' && (
          <>
            {/* Форма запроса */}
            <StructuredForm onTaskCreated={setActiveTaskId} collapseTrigger={activeTaskId} />

            {/* Загрузка камер фотовидеофиксации */}
            <CamerasWidget />

            {/* Активная задача */}
            {activeTaskId && task && (
              <>
                {task.status !== 'done' && task.status !== 'failed' && (
                  <ProgressIndicator task={task} />
                )}

                {task.status === 'done' && <ResultsPanel task={task} />}

                {task.status === 'failed' && (
                  <div
                    className="tg-card"
                    style={{
                      color: 'var(--tg-color-destructive, #ff3b30)',
                    }}
                  >
                    <div className="font-medium mb-1">Задача завершилась с ошибкой</div>
                    <div className="text-xs opacity-80">
                      {task.error ?? 'Неизвестная ошибка'}
                    </div>
                  </div>
                )}

                {isError && (
                  <div className="tg-card text-center text-sm opacity-60">
                    Не удалось получить статус задачи. Попробуйте обновить.
                  </div>
                )}
              </>
            )}

            {/* История */}
            <HistoryList onSelectTask={handleSelectTask} />
          </>
        )}

        {/* --- Вкладка «Выгрузка файлов» --- */}
        {tab === 'export' && (
          <Suspense fallback={<TabSpinner />}>
            <ExportView />
          </Suspense>
        )}

        {/* --- Вкладка «НП БДД» --- */}
        {tab === 'np-bdd' && (
          <Suspense fallback={<TabSpinner />}>
            <NpBddView />
          </Suspense>
        )}

        {/* --- Вкладка «Редактор карты» (только для редакторов) --- */}
        {tab === 'polygons' && isPolygonEditor && (
          <Suspense fallback={<TabSpinner />}>
            <PolygonEditorView />
          </Suspense>
        )}

        {/* Подвал */}
        <footer className="text-center text-xs opacity-40 pt-4">
          GIBDD Stat Mini App · v0.2.0
        </footer>
      </div>
    </div>
  )
}
