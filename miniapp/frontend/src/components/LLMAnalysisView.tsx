/**
 * LLMAnalysisView — вкладка «ИИ-анализ».
 *
 * Логика:
 *  1. Проверяем доступность провайдеров (free/paid)
 *  2. Пользователь выбирает провайдера (radio)
 *  3. Раздел «Резюме»: кнопка «Сгенерировать» → polling → текст
 *  4. Раздел «Вопрос-ответ»: input + «Спросить» → loading → ответ
 *  5. История вопросов сохраняется на задаче (последние 10)
 *
 * UX:
 *  - Резюме кэшируется на задаче (повторное открытие = мгновенно)
 *  - Если сменили провайдера — кнопка «Перегенерировать с <provider>»
 *  - Длинный текст разбит на абзацы с переносами
 *  - Подсказки: типичные вопросы (что росло, какие рекомендации и т.д.)
 */
import { useEffect, useState } from 'react'
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
]

export function LLMAnalysisView({ task }: LLMAnalysisViewProps) {
  const [providers, setProviders] = useState<LLMProvidersResponse | null>(null)
  const [provider, setProvider] = useState<'free' | 'paid'>('free')
  const [started, setStarted] = useState(false)

  // Вопрос-ответ
  const [question, setQuestion] = useState('')
  const [qaLoading, setQaLoading] = useState(false)
  const [qaError, setQaError] = useState<string | null>(null)
  const [qaHistory, setQaHistory] = useState<QAHistoryItem[]>([])

  // Polling для summary
  const { data: summaryData } = useLLMSummaryPolling(task.task_id, started)

  // Загружаем провайдеров и историю
  useEffect(() => {
    api.getLLMProvidersForTask(task.task_id).then(setProviders).catch(() => {})
    api.getQAHistory(task.task_id).then(setQaHistory).catch(() => {})
  }, [task.task_id])

  // Авто-показ готового резюме
  useEffect(() => {
    if (summaryData?.state.status === 'done' && summaryData.result) {
      setStarted(true)
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

  const handleGenerate = async () => {
    setStarted(true)
    haptic('medium')
    try {
      await api.startLLMSummary(task.task_id, provider)
    } catch (e: any) {
      haptic('error')
      setStarted(false)
    }
  }

  const handleAsk = async () => {
    if (!question.trim()) return
    setQaError(null)
    setQaLoading(true)
    haptic('medium')
    try {
      const resp = await api.askLLM(task.task_id, question, provider)
      if (resp.ok && resp.answer) {
        // Добавляем в локальную историю
        setQaHistory((prev) => [
          {
            question,
            answer: resp.answer!,
            provider: resp.provider || provider,
            timestamp: new Date().toISOString(),
          },
          ...prev,
        ])
        setQuestion('')
        haptic('success')
      } else {
        setQaError(resp.error ?? 'Не удалось получить ответ')
        haptic('error')
      }
    } catch (e: any) {
      setQaError(e?.message ?? 'Ошибка запроса')
      haptic('error')
    } finally {
      setQaLoading(false)
    }
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

        {!started && !summaryData?.result && (
          <>
            <p className="text-sm opacity-80 mb-3">
              Нейросеть проанализирует метрики ДТП, кросс-таблицы
              корреляций и очаги (если рассчитаны), затем сформирует
              развёрнутое резюме с рекомендациями.
            </p>
            <button
              onClick={handleGenerate}
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

        {summaryData?.state.status === 'running' && (
          <div className="text-center py-4">
            <div className="text-3xl mb-2">⏳</div>
            <div className="font-medium mb-1">Нейросеть анализирует...</div>
            <div className="text-xs opacity-70 mb-3">
              {summaryData.state.stage}
            </div>
            <div
              className="w-full h-2 rounded-full overflow-hidden"
              style={{
                backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
              }}
            >
              <div
                className="h-full transition-all duration-500"
                style={{
                  width: `${summaryData.state.progress}%`,
                  backgroundColor: 'var(--tg-color-button, #2481cc)',
                }}
              />
            </div>
            <div className="text-xs opacity-60 mt-1">
              {summaryData.state.progress}%
            </div>
          </div>
        )}

        {summaryData?.state.status === 'failed' && (
          <div className="text-center py-4">
            <div className="text-3xl mb-2">❌</div>
            <div className="font-medium mb-1" style={{ color: '#ff3b30' }}>
              Ошибка генерации
            </div>
            <div className="text-xs opacity-80 mb-3">
              {summaryData.state.error}
            </div>
            <button
              onClick={handleGenerate}
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

        {summaryData?.state.status === 'done' && summaryData.result && (
          <>
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs opacity-60">
                Провайдер: {summaryData.result.provider === 'free' ? '⚡' : '🔬'}{' '}
                {summaryData.result.provider}
              </div>
              <button
                onClick={handleGenerate}
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
          {!question && (
            <div className="flex flex-wrap gap-1.5">
              {SUGGESTED_QUESTIONS.slice(0, 3).map((q) => (
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

          <button
            onClick={handleAsk}
            disabled={qaLoading || !question.trim()}
            className="w-full py-2 rounded-xl text-sm font-medium disabled:opacity-50"
            style={{
              backgroundColor: 'var(--tg-color-button, #2481cc)',
              color: 'var(--tg-color-button-text, #ffffff)',
            }}
          >
            {qaLoading ? '🤔 Думаю...' : '💬 Спросить'}
          </button>

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

        {/* История вопросов */}
        {qaHistory.length > 0 && (
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
