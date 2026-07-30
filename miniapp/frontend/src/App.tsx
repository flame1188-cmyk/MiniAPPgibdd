/**
 * Главный компонент Mini App.
 *
 * Layout:
 *  - Шапка с приветствием пользователя (имя из Telegram)
 *  - Форма запроса
 *  - Прогресс активной задачи (если есть)
 *  - Результаты (карта, аналитика, файлы) — когда задача выполнена
 *  - История последних запросов
 */
import { useState } from 'react'
import { RequestForm } from '@/components/RequestForm'
import { ProgressIndicator } from '@/components/ProgressIndicator'
import { ResultsPanel } from '@/components/ResultsPanel'
import { HistoryList } from '@/components/HistoryList'
import { useTaskPolling } from '@/hooks/useTaskPolling'
import { getCurrentUser, isInsideTelegram } from '@/lib/telegram'

export default function App() {
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)

  const { data: task, isError } = useTaskPolling(activeTaskId)

  const user = getCurrentUser()
  const showDevWarning = !isInsideTelegram()

  const handleSelectTask = (taskId: string) => {
    setActiveTaskId(taskId)
    // Прокрутка вверх при выборе задачи из истории
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  return (
    <div className="min-h-screen pb-8">
      <div className="max-w-xl mx-auto px-4 py-4 space-y-4">
        {/* Шапка */}
        <header className="text-center pb-2">
          <h1 className="text-xl font-bold mb-0.5">ДТП Статистика</h1>
          {user ? (
            <p className="text-xs opacity-60">
              Привет, {user.first_name}! 👋
            </p>
          ) : (
            <p className="text-xs opacity-60">
              Данные ГИБДД · stat.gibdd.ru
            </p>
          )}
        </header>

        {/* Предупреждение о dev-режиме */}
        {showDevWarning && (
          <div
            className="rounded-xl p-3 text-xs"
            style={{
              backgroundColor: 'rgba(255, 149, 0, 0.1)',
              color: '#ff9500',
            }}
          >
            ⚠️ Запущено вне Telegram. Запросы к API не будут аутентифицированы.
            Откройте приложение через Telegram-бота для полноценной работы.
          </div>
        )}

        {/* Форма запроса */}
        <RequestForm onTaskCreated={setActiveTaskId} />

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

        {/* Подвал */}
        <footer className="text-center text-xs opacity-40 pt-4">
          GIBDD Stat Mini App · v0.1.0
        </footer>
      </div>
    </div>
  )
}
