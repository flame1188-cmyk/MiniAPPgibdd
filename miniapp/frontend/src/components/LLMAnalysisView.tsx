/**
 * LLMAnalysisView — вкладка «ИИ-анализ».
 *
 * Логика:
 *  1. Проверяем доступность провайдеров (free/paid)
 *  2. Пользователь выбирает провайдера (radio)
 *  3. Раздел «Резюме»: кнопка «Сгенерировать» → SSE-стрим → текст по мере поступления
 *  4. Раздел «Вопрос-ответ»: input + «Спросить» → SSE-стрим → ответ по мере поступления
 *  5. История вопросов сохраняется на сервере (последние 10)
 *
 * Sprint 4: Q&A и резюме используют SSE-стриминг (token-by-token).
 *  - Старые JSON-эндпоинты (/llm/ask, /llm/summary) сохранены для совместимости,
 *    но UI использует только /stream-версии.
 *  - Кнопка «Стоп» во время стрима — AbortController.
 *
 * UX:
 *  - Резюме кэшируется на сервере (cache hit = мгновенно одним delta)
 *  - Длинный текст разбит на абзацы с переносами
 *  - Подсказки: типичные вопросы
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  api,
  type LLMProvidersResponse,
  type QAHistoryItem,
  type TaskStatusResponse,
} from '@/lib/api'
import { haptic } from '@/lib/telegram'
import { useLLMSummaryPolling } from '@/hooks/useAnalysisPolling'

interface LLMAnalysisViewProps {
  task: TaskStatusResponse
}

const SUGGESTED_QUESTIONS = [
  'В какие дни недели происходит больше всего ДТП?',
  'Какие основные причины роста аварийности?',
  'Где наблюдаются наиболее опасные участки?',
  'Какие рекомендации по снижению ДТП с пешеходами?',
  'Как влияет время суток на тяжесть последствий?',
  'Какова доля нетрезвых водителей в ДТП?',
  // Новые вопросы для Этапов 1-2 (БДД-экспертиза + профиль ТС):
  'Какие недостатки дороги чаще всего способствуют ДТП?',
  'На каких участках УДС (перекрёстки, переходы) больше аварий?',
  'Как состояние покрытия влияет на тяжесть последствий?',
  'В ДТП с каким возрастом ТС больше погибших?',
  'Какие марки автомобилей чаще всего фигурируют в ДТП?',
  'Как распределены ДТП по количеству участвующих ТС?',
]

// Тикер для обновления elapsed-time раз в секунду.
// Используем отдельный state, чтобы не плодить re-render'ы всего компонента.
function useElapsedSeconds(startedAt: string | null | undefined): number {
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!startedAt) return
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [startedAt])
  if (!startedAt) return 0
  const start = new Date(startedAt).getTime()
  if (isNaN(start)) return 0
  return Math.max(0, Math.floor((Date.now() - start) / 1000))
}

function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec} сек`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m} мин ${s} сек`
}

export function LLMAnalysisView({ task }: LLMAnalysisViewProps) {
  const queryClient = useQueryClient()
  const [providers, setProviders] = useState<LLMProvidersResponse | null>(null)
  const [provider, setProvider] = useState<'free' | 'paid'>('free')
  const [started, setStarted] = useState(false)
  // Локальный флаг «нажали кнопку, ждём первый ответ от API».
  // Нужен, чтобы показать прогресс-бар МГНОВЕННО, не дожидаясь
  // первого long-polling ответа (который может идти 25 сек).
  const [starting, setStarting] = useState(false)

  // === Q&A (Sprint 4: streaming) ===
  const [question, setQuestion] = useState('')
  const [qaLoading, setQaLoading] = useState(false)
  const [qaError, setQaError] = useState<string | null>(null)
  const [qaHistory, setQaHistory] = useState<QAHistoryItem[]>([])
  // Streaming-ответ, который ещё не сохранён в history.
  // Показываем его отдельной карточкой с «typing cursor».
  const [streamingQA, setStreamingQA] = useState<{
    question: string
    answer: string
    provider: 'free' | 'paid'
  } | null>(null)
  // AbortController для отмены стрима (кнопка «Стоп»)
  const qaAbortRef = useRef<AbortController | null>(null)

  // === Summary (Sprint 4: streaming) ===
  // Streaming-резюме — накапливается по delta.
  const [streamingSummary, setStreamingSummary] = useState<string>('')
  const [summaryStreaming, setSummaryStreaming] = useState(false)
  const summaryAbortRef = useRef<AbortController | null>(null)

  // 3 случайных подсказки из полного списка — при каждом монтировании
  // компонента пользователь видит разные, что расширяет охват возможностей
  // (теперь включает БДД-факторы и профиль ТС).
  const suggestedQuestions = useMemo(
    () => [...SUGGESTED_QUESTIONS].sort(() => Math.random() - 0.5).slice(0, 3),
    [],
  )

  // Polling для summary — нужен для получения статуса из кэша при первом открытии.
  // Sprint 4: если summary уже готово в кэше, показываем мгновенно.
  // Для новой генерации используем SSE-стрим ниже.
  const { data: summaryData } = useLLMSummaryPolling(task.task_id, started && !summaryStreaming)

  // Elapsed time — пока статус running, показываем сколько секунд идёт анализ
  const isRunning = summaryStreaming || summaryData?.state.status === 'running' || starting
  const elapsedSec = useElapsedSeconds(isRunning ? summaryData?.state.started_at : null)
  // Если прошло больше 90 сек — показываем предупреждение, что это дольше обычного
  const isSlow = isRunning && elapsedSec > 90
  // Если прошло больше 240 сек (4 мин) — показываем рекомендацию отменить
  const isVerySlow = isRunning && elapsedSec > 240

  // Загружаем провайдеров и историю
  useEffect(() => {
    api.getLLMProvidersForTask(task.task_id).then(setProviders).catch(() => {})
    api.getQAHistory(task.task_id).then(setQaHistory).catch(() => {})
  }, [task.task_id])

  // Авто-показ готового резюме (из polling)
  useEffect(() => {
    if (summaryData?.state.status === 'done' && summaryData.result) {
      setStarted(true)
      setStarting(false)
    }
    // Когда пришёл первый ответ со статусом running — локальный loading можно снять
    if (summaryData?.state.status === 'running') {
      setStarting(false)
    }
  }, [summaryData?.state.status, summaryData?.result])

  // Авто-выбор доступного провайдера
  useEffect(() => {
    if (providers) {
      if (provider === 'free' && !providers.free && providers.paid) {
        setProvider('paid')
      }
      if (provider === 'paid' && !providers.paid && providers.free) {
        setProvider('free')
      }
    }
  }, [providers, provider])

  // Cleanup: отменяем стримы при размонтировании
  useEffect(() => {
    return () => {
      qaAbortRef.current?.abort()
      summaryAbortRef.current?.abort()
    }
  }, [])

  // === Summary streaming (Sprint 4) ===
  const handleGenerateStream = async () => {
    // Сброс polling-кэша
    queryClient.removeQueries({ queryKey: ['llm-summary', task.task_id] })
    setStarting(true)
    setStarted(true)
    setSummaryStreaming(true)
    setStreamingSummary('')
    haptic('medium')

    // Отменяем предыдущий стрим, если был
    summaryAbortRef.current?.abort()
    const controller = new AbortController()
    summaryAbortRef.current = controller

    try {
      await api.getLLMSummaryStream(
        task.task_id,
        provider,
        {
          onDelta: (delta) => {
            setStreamingSummary((prev) => prev + delta)
            setStarting(false)  // первый токен пришёл — больше не "запускаем"
          },
          onDone: () => {
            setSummaryStreaming(false)
            setStarting(false)
            // Обновляем polling-кэш, чтобы получить финальный state
            queryClient.invalidateQueries({ queryKey: ['llm-summary', task.task_id] })
            haptic('success')
          },
          onError: (err) => {
            setSummaryStreaming(false)
            setStarting(false)
            setQaError(err)  // используем тот же error-state для простоты
            haptic('error')
          },
        },
        controller.signal,
      )
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        setSummaryStreaming(false)
        setStarting(false)
      }
    }
  }

  const handleStopSummary = () => {
    summaryAbortRef.current?.abort()
    setSummaryStreaming(false)
    setStarting(false)
    haptic('light')
    // Частичный текст остаётся в streamingSummary — пользователь видит, что уже сгенерировалось
  }

  // === Q&A streaming (Sprint 4) ===
  const handleAskStream = async () => {
    const trimmed = question.trim()
    if (!trimmed) return
    setQaError(null)
    setQaLoading(true)
    setStreamingQA({
      question: trimmed,
      answer: '',
      provider,
    })
    haptic('medium')

    // Отменяем предыдущий стрим, если был
    qaAbortRef.current?.abort()
    const controller = new AbortController()
    qaAbortRef.current = controller

    try {
      await api.askLLMStream(
        task.task_id,
        trimmed,
        provider,
        {
          onDelta: (delta) => {
            setStreamingQA((prev) => prev
              ? { ...prev, answer: prev.answer + delta }
              : prev,
            )
          },
          onDone: () => {
            // Сохраняем в локальную историю (сервер уже сохранил в task.llm_qa_history)
            setStreamingQA((final) => {
              if (final && final.answer.trim()) {
                setQaHistory((prev) => [
                  {
                    question: final.question,
                    answer: final.answer,
                    provider: final.provider,
                    timestamp: new Date().toISOString(),
                  },
                  ...prev,
                ])
                return null
              }
              // Fallback: если answer пустой (proxy буферизовал stream и
              // deltas не дошли до frontend), но сервер уже сохранил ответ
              // в qa-history — запросим его через REST API.
              if (final) {
                api.getQAHistory(task.task_id).then((history) => {
                  if (history.length > 0) {
                    const latest = history[0]
                    setQaHistory((prev) => [latest, ...prev])
                  }
                }).catch(() => {
                  // Если и fallback не сработал — показываем ошибку
                  setQaError('Ответ не получен. Попробуйте ещё раз или смените провайдера.')
                })
              }
              return null
            })
            setQaLoading(false)
            setQuestion('')
            haptic('success')
          },
          onError: (err) => {
            setStreamingQA(null)
            setQaError(err)
            setQaLoading(false)
            haptic('error')
          },
        },
        controller.signal,
      )
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        setStreamingQA(null)
        setQaError(e?.message ?? 'Ошибка запроса')
        setQaLoading(false)
        haptic('error')
      } else {
        // Abort — сохраняем partial в историю, если есть
        setStreamingQA((partial) => {
          if (partial && partial.answer.trim().length > 10) {
            setQaHistory((prev) => [
              {
                question: partial.question,
                answer: partial.answer + '\n\n_[ответ прерван пользователем]_',
                provider: partial.provider,
                timestamp: new Date().toISOString(),
              },
              ...prev,
            ])
          }
          return null
        })
        setQaLoading(false)
      }
    }
  }

  const handleStopQA = () => {
    qaAbortRef.current?.abort()
    haptic('light')
    // onDone/onError не вызвался — обрабатываем здесь
    setStreamingQA((partial) => {
      if (partial && partial.answer.trim().length > 10) {
        setQaHistory((prev) => [
          {
            question: partial.question,
            answer: partial.answer + '\n\n_[ответ прерван пользователем]_',
            provider: partial.provider,
            timestamp: new Date().toISOString(),
          },
          ...prev,
        ])
      }
      return null
    })
    setQaLoading(false)
  }

  // === Заглушка если LLM не настроен ===
  if (providers && !providers.free && !providers.paid) {
    return (
      <div className="tg-card text-center py-6">
        <div className="text-3xl mb-2">🤖</div>
        <div className="font-medium mb-1">ИИ-анализ недоступен</div>
        <div className="text-xs opacity-70">
          Не настроен ни один LLM-провайдер.
          <br />
          Задайте <code>LLM_API_KEY</code> в переменных окружения для
          бесплатного анализа через GLM (ZhipuAI).
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* Выбор провайдера */}
      {providers && (providers.free || providers.paid) && (
        <div className="tg-card">
          <div className="tg-section-header mb-2">Провайдер ИИ</div>
          <div className="grid grid-cols-2 gap-1.5">
            <ProviderButton
              active={provider === 'free'}
              disabled={!providers.free}
              onClick={() => {
                setProvider('free')
                haptic('light')
              }}
              title="Бесплатный"
              subtitle={providers.free_model || 'GLM'}
              icon="⚡"
            />
            <ProviderButton
              active={provider === 'paid'}
              disabled={!providers.paid}
              onClick={() => {
                setProvider('paid')
                haptic('light')
              }}
              title="Полный"
              subtitle={providers.paid_model || 'DeepSeek'}
              icon="🔬"
            />
          </div>
          <div className="text-[10px] opacity-60 mt-2">
            ⚡ Быстрый (15-30с) — агрегированные метрики + кросс-таблицы.
            <br />
            🔬 Полный (30-90с) — все данные участников ДТП.
          </div>
        </div>
      )}

      {/* Раздел: Резюме */}
      <div className="tg-card">
        <div className="tg-section-header mb-2">Аналитическое резюме</div>

        {!started && !summaryData?.result && !starting && !summaryStreaming && (
          <>
            <p className="text-sm opacity-80 mb-3">
              Нейросеть проанализирует метрики ДТП, кросс-таблицы
              корреляций и очаги (если рассчитаны), затем сформирует
              развёрнутое резюме с рекомендациями. Текст появится здесь
              по мере генерации (token-by-token).
            </p>
            <button
              onClick={handleGenerateStream}
              disabled={!providers}
              className="w-full py-2.5 rounded-xl font-medium text-sm disabled:opacity-50"
              style={{
                backgroundColor: 'var(--tg-color-button, #2481cc)',
                color: 'var(--tg-color-button-text, #ffffff)',
              }}
            >
              🤖 Сгенерировать резюме
            </button>
            <p className="text-xs opacity-60 mt-2 text-center">
              {provider === 'free' ? '15-30 секунд' : '30-90 секунд'}
            </p>
          </>
        )}

        {(summaryStreaming || starting) && (
          <div className="text-center py-4">
            <div className="text-3xl mb-2 animate-pulse">{isVerySlow ? '⏰' : '⏳'}</div>
            <div className="font-medium mb-1">
              {isVerySlow
                ? 'Анализ идёт дольше обычного...'
                : starting && !streamingSummary
                  ? 'Запуск нейросети...'
                  : 'Нейросеть генерирует...'}
            </div>
            <div className="text-xs opacity-70 mb-3">
              {summaryData?.state.stage || 'Подготовка промпта...'}
            </div>
            {/* Elapsed time */}
            {elapsedSec >= 5 && (
              <div
                className="text-xs mb-3 font-mono"
                style={{
                  color: isVerySlow
                    ? '#ff3b30'
                    : isSlow
                      ? '#ff9500'
                      : 'var(--tg-color-subtitle, #888)',
                }}
              >
                ⏱ {formatElapsed(elapsedSec)}
                {isSlow && !isVerySlow && ' — дольше обычного'}
                {isVerySlow && ' — вероятно, сбой нейросети'}
              </div>
            )}
            {/* Подсказка при долгом ожидании */}
            {isVerySlow && (
              <div
                className="text-xs p-2 rounded-lg mb-3 text-left"
                style={{
                  backgroundColor: 'rgba(255, 149, 0, 0.1)',
                  color: '#ff9500',
                }}
              >
                Сервис нейросети не отвечает достаточно долго. Подождите ещё
                минуту или нажмите «Стоп» и попробуйте другой провайдер.
              </div>
            )}
            {/* Streaming text — показываем по мере поступления */}
            {streamingSummary && (
              <div
                className="text-sm leading-relaxed whitespace-pre-wrap text-left mb-3"
                style={{
                  fontFamily:
                    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
                  borderLeft: '2px solid var(--tg-color-button, #2481cc)',
                  paddingLeft: '8px',
                }}
              >
                {streamingSummary}
                <span className="animate-pulse">▌</span>
              </div>
            )}
            {/* Прогресс-бар (фаза подготовки промпта) */}
            {!streamingSummary && (
              <>
                <div
                  className="w-full h-2 rounded-full overflow-hidden"
                  style={{
                    backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
                  }}
                >
                  <div
                    className="h-full transition-all duration-500"
                    style={{
                      width: `${summaryData?.state.progress ?? 5}%`,
                      backgroundColor: isVerySlow
                        ? '#ff3b30'
                        : isSlow
                          ? '#ff9500'
                          : 'var(--tg-color-button, #2481cc)',
                    }}
                  />
                </div>
                <div className="text-xs opacity-60 mt-1">
                  {summaryData?.state.progress ?? 5}%
                </div>
              </>
            )}
            {/* Кнопка «Стоп» — во время стрима всегда доступна */}
            <button
              onClick={handleStopSummary}
              className="mt-3 text-xs px-3 py-1.5 rounded-lg"
              style={{
                backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
                color: 'var(--tg-color-text, #000)',
              }}
            >
              ✕ Остановить
            </button>
          </div>
        )}

        {summaryData?.state.status === 'failed' && !summaryStreaming && (
          <div className="text-center py-4">
            <div className="text-3xl mb-2">❌</div>
            <div className="font-medium mb-1" style={{ color: '#ff3b30' }}>
              Ошибка генерации
            </div>
            <div className="text-xs opacity-80 mb-3">
              {summaryData.state.error}
            </div>
            <button
              onClick={handleGenerateStream}
              className="px-4 py-2 rounded-xl text-sm font-medium"
              style={{
                backgroundColor: 'var(--tg-color-button, #2481cc)',
                color: 'var(--tg-color-button-text, #ffffff)',
              }}
            >
              Повторить
            </button>
          </div>
        )}

        {summaryData?.state.status === 'done' && summaryData.result && !summaryStreaming && (
          <>
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs opacity-60">
                Провайдер: {summaryData.result.provider === 'free' ? '⚡' : '🔬'}{' '}
                {summaryData.result.provider}
              </div>
              <button
                onClick={handleGenerateStream}
                className="text-xs px-2 py-1 rounded-lg"
                style={{
                  backgroundColor:
                    'var(--tg-color-secondary-bg, #f1f1f1)',
                  color: 'var(--tg-color-text, #000)',
                }}
              >
                ↻ Перегенерировать
              </button>
            </div>
            <div
              className="text-sm leading-relaxed whitespace-pre-wrap"
              style={{
                fontFamily:
                  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
              }}
            >
              {summaryData.result.text}
            </div>
          </>
        )}
      </div>

      {/* Раздел: Вопрос-ответ */}
      <div className="tg-card">
        <div className="tg-section-header mb-2">Спросить нейросеть</div>

        <div className="space-y-2 mb-3">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Например: в какие часы происходит больше всего ДТП?"
            rows={2}
            className="w-full px-3 py-2 rounded-lg text-sm resize-none"
            style={{
              backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
              color: 'var(--tg-color-text, #000)',
              border: 'none',
              outline: 'none',
            }}
          />

          {/* Подсказки */}
          {!question && !qaLoading && (
            <div className="flex flex-wrap gap-1.5">
              {suggestedQuestions.map((q) => (
                <button
                  key={q}
                  onClick={() => {
                    setQuestion(q)
                    haptic('light')
                  }}
                  className="text-xs px-2 py-1 rounded-full"
                  style={{
                    backgroundColor:
                      'var(--tg-color-secondary-bg, #f1f1f1)',
                    color: 'var(--tg-color-text, #000)',
                    opacity: 0.8,
                  }}
                >
                  {q}
                </button>
              ))}
            </div>
          )}

          {!qaLoading ? (
            <button
              onClick={handleAskStream}
              disabled={!question.trim()}
              className="w-full py-2 rounded-xl text-sm font-medium disabled:opacity-50"
              style={{
                backgroundColor: 'var(--tg-color-button, #2481cc)',
                color: 'var(--tg-color-button-text, #ffffff)',
              }}
            >
              💬 Спросить
            </button>
          ) : (
            <button
              onClick={handleStopQA}
              className="w-full py-2 rounded-xl text-sm font-medium"
              style={{
                backgroundColor: 'rgba(255, 59, 48, 0.15)',
                color: '#ff3b30',
              }}
            >
              ⏹ Остановить генерацию
            </button>
          )}

          {qaError && (
            <div
              className="text-xs p-2 rounded-lg"
              style={{
                backgroundColor: 'rgba(255, 59, 48, 0.1)',
                color: '#ff3b30',
              }}
            >
              {qaError}
            </div>
          )}
        </div>

        {/* Streaming-ответ (показываем во время генерации) */}
        {streamingQA && (
          <div className="space-y-2 pt-2 border-t border-current/10">
            <div className="text-xs opacity-60 mb-1">Генерация ответа...</div>
            <StreamingQACard
              question={streamingQA.question}
              answer={streamingQA.answer}
              provider={streamingQA.provider}
            />
          </div>
        )}

        {/* История вопросов */}
        {!streamingQA && qaHistory.length > 0 && (
          <div className="space-y-2 pt-2 border-t border-current/10">
            <div className="text-xs opacity-60 mb-1">История:</div>
            {qaHistory.map((item, idx) => (
              <QACard key={idx} item={item} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================================
// Подкомпоненты
// ============================================================
function ProviderButton({
  active,
  disabled,
  onClick,
  title,
  subtitle,
  icon,
}: {
  active: boolean
  disabled: boolean
  onClick: () => void
  title: string
  subtitle: string
  icon: string
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="p-2.5 rounded-xl text-left transition-all disabled:opacity-40"
      style={{
        backgroundColor: active
          ? 'var(--tg-color-button, #2481cc)'
          : 'var(--tg-color-secondary-bg, #f1f1f1)',
        color: active
          ? 'var(--tg-color-button-text, #ffffff)'
          : 'var(--tg-color-text, #000)',
      }}
    >
      <div className="flex items-center gap-1.5">
        <span>{icon}</span>
        <span className="text-sm font-medium">{title}</span>
      </div>
      <div
        className="text-[10px] mt-0.5"
        style={{ opacity: active ? 0.9 : 0.6 }}
      >
        {subtitle}
      </div>
    </button>
  )
}

// Streaming-карточка: показывает partial-ответ с typing-cursor.
function StreamingQACard({
  question,
  answer,
  provider,
}: {
  question: string
  answer: string
  provider: 'free' | 'paid'
}) {
  return (
    <div
      className="rounded-lg p-2.5"
      style={{
        backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
        borderLeft: '2px solid var(--tg-color-button, #2481cc)',
      }}
    >
      <div className="text-xs font-medium mb-1 opacity-80">
        ❓ {question}
      </div>
      <div className="text-xs whitespace-pre-wrap leading-relaxed">
        {answer}
        <span className="animate-pulse">▌</span>
      </div>
      <div className="text-[10px] opacity-50 mt-1">
        {provider === 'free' ? '⚡ GLM' : '🔬 DeepSeek'} · генерация...
      </div>
    </div>
  )
}

function QACard({ item }: { item: QAHistoryItem }) {
  const [expanded, setExpanded] = useState(false)
  const answerPreview = item.answer.slice(0, 200)
  const hasMore = item.answer.length > 200

  return (
    <div
      className="rounded-lg p-2.5"
      style={{
        backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
      }}
    >
      <div className="text-xs font-medium mb-1 opacity-80">
        ❓ {item.question}
      </div>
      <div className="text-xs whitespace-pre-wrap leading-relaxed">
        {expanded || !hasMore
          ? item.answer
          : `${answerPreview}...`}
      </div>
      {hasMore && (
        <button
          onClick={() => {
            setExpanded(!expanded)
            haptic('light')
          }}
          className="text-xs mt-1 opacity-70"
        >
          {expanded ? 'Свернуть' : 'Читать далее'}
        </button>
      )}
      <div className="text-[10px] opacity-50 mt-1">
        {item.provider === 'free' ? '⚡ GLM' : '🔬 DeepSeek'} ·{' '}
        {new Date(item.timestamp).toLocaleString('ru-RU', {
          day: 'numeric',
          month: 'short',
          hour: '2-digit',
          minute: '2-digit',
        })}
      </div>
    </div>
  )
}
