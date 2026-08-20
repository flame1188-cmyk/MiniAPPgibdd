/**
 * ErrorBoundary — top-level React error boundary.
 *
 * Stabilization A10 (P1 #5): ловит любой uncaught render error в дочерних
 * компонентах. Без этого белый экран — пользователь не понимает, что делать.
 *
 * Использование:
 *   <ErrorBoundary>
 *     <App />
 *   </ErrorBoundary>
 *
 * Поведение при ошибке:
 *   - Показывает карточку с сообщением «Что-то пошло не так»
 *   - Кнопка «Перезагрузить» — window.location.reload()
 *   - Кнопка «Сбросить состояние» — очищает URL params (?task=) + reload
 *     (часто помогает, если ошибка была из-за протухшего activeTaskId)
 *   - В dev-режиме (!isProduction) показывает stack trace
 *
 * Логика:
 *   - Это class component (React требует для componentDidCatch)
 *   - Не ловит ошибки в event handlers, async code, SSR — только в render
 *   - Сбрасывает состояние при смене location (через key=location)
 *
 * Источник паттерна: https://react.dev/reference/react/Component#catching-rendering-errors-with-an-error-boundary
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
  /** Уникальный ключ; при смене ErrorBoundary сбрасывает state (полезно для route changes) */
  resetKey?: string
}

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
  errorInfo: ErrorInfo | null
}

const isProduction = import.meta.env.PROD

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = { hasError: false, error: null, errorInfo: null }
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    // Обновляем state, чтобы следующий render показал fallback UI
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Логируем в console — это увидит разработчик в DevTools
    console.error('[ErrorBoundary] caught error:', error, errorInfo)
    // Сохраняем errorInfo для display в dev-режиме
    this.setState({ errorInfo })
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps): void {
    // Если resetKey сменился (например, пользователь перешёл на другой URL) —
    // сбрасываем error state, чтобы попробовать отрендерить заново.
    if (
      this.state.hasError &&
      prevProps.resetKey !== this.props.resetKey
    ) {
      this.setState({ hasError: false, error: null, errorInfo: null })
    }
  }

  handleReload = (): void => {
    window.location.reload()
  }

  handleResetAndReload = (): void => {
    // Убираем ?task= из URL (частая причина ошибок — протухший taskId)
    const url = new URL(window.location.href)
    url.searchParams.delete('task')
    url.searchParams.delete('tab')
    window.history.replaceState({}, '', url.toString())
    window.location.reload()
  }

  render(): ReactNode {
    if (!this.state.hasError || !this.state.error) {
      return this.props.children
    }

    const { error, errorInfo } = this.state

    return (
      <div
        className="min-h-screen flex items-center justify-center p-4"
        style={{
          backgroundColor: 'var(--tg-color-bg, #ffffff)',
          color: 'var(--tg-color-text, #000000)',
        }}
      >
        <div
          className="max-w-md w-full rounded-2xl p-6 space-y-4"
          style={{
            backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
          }}
        >
          <div className="text-center space-y-2">
            {/* Иконка — эмодзи (без SVG dependency) */}
            <div className="text-4xl">⚠️</div>
            <h2 className="text-lg font-bold">Что-то пошло не так</h2>
            <p className="text-sm opacity-70">
              Произошла непредвиденная ошибка в интерфейсе.
              Попробуйте перезагрузить страницу.
            </p>
          </div>

          {/* Краткое сообщение об ошибке (даже в проде — помогает саппорту) */}
          <div
            className="rounded-lg p-3 text-xs font-mono break-all"
            style={{
              backgroundColor: 'var(--tg-color-bg, #ffffff)',
            }}
          >
            {error.name}: {error.message}
          </div>

          {/* В dev-режиме — полный stack trace */}
          {!isProduction && error.stack && (
            <details className="text-xs opacity-70">
              <summary className="cursor-pointer">Stack trace (dev only)</summary>
              <pre
                className="mt-2 p-2 rounded overflow-auto max-h-40"
                style={{
                  backgroundColor: 'var(--tg-color-bg, #ffffff)',
                  fontSize: '10px',
                  lineHeight: 1.4,
                }}
              >
                {error.stack}
                {errorInfo?.componentStack && (
                  <>
                    {'\n\nComponent stack:\n'}
                    {errorInfo.componentStack}
                  </>
                )}
              </pre>
            </details>
          )}

          {/* Кнопки */}
          <div className="space-y-2">
            <button
              onClick={this.handleReload}
              className="w-full py-2.5 rounded-lg font-medium text-sm"
              style={{
                backgroundColor: 'var(--tg-color-button, #2481cc)',
                color: 'var(--tg-color-button-text, #ffffff)',
              }}
            >
              Перезагрузить страницу
            </button>
            <button
              onClick={this.handleResetAndReload}
              className="w-full py-2 rounded-lg font-medium text-sm"
              style={{
                backgroundColor: 'transparent',
                color: 'var(--tg-color-hint, #999999)',
              }}
            >
              Сбросить state (?task=) и перезагрузить
            </button>
          </div>
        </div>
      </div>
    )
  }
}
