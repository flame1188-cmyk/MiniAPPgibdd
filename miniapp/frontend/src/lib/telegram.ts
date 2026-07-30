/**
 * Обёртка над Telegram WebApp SDK.
 *
 * Документация: https://core.telegram.org/bots/webapps
 *
 * В dev-режиме (вне Telegram) window.Telegram.WebApp недоступен —
 * возвращаем mock, чтобы UI не падал.
 */

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp
    }
  }
}

export interface TelegramUser {
  id: number
  first_name: string
  last_name?: string
  username?: string
  language_code?: string
  is_premium?: boolean
}

export interface TelegramWebApp {
  initData: string
  initDataUnsafe: {
    user?: TelegramUser
    auth_date?: number
    hash?: string
    start_param?: string
  }
  version: string
  platform: 'ios' | 'android' | 'web' | 'tdesktop' | 'unknown'
  colorScheme: 'light' | 'dark'
  themeParams: Record<string, string>
  isExpanded: boolean
  viewportHeight: number
  viewportStableHeight: number
  headerColor: string
  backgroundColor: string

  ready: () => void
  expand: () => void
  close: () => void
  setHeaderColor: (color: 'bg_color' | 'secondary_bg_color') => void
  setBackgroundColor: (color: string) => void
  enableClosingConfirmation: () => void
  disableVerticalSwipes?: () => void
  MainButton: {
    text: string
    color: string
    textColor: string
    isVisible: boolean
    isActive: boolean
    setText: (text: string) => void
    show: () => void
    hide: () => void
    enable: () => void
    disable: () => void
    onClick: (cb: () => void) => void
    offClick: (cb: () => void) => void
  }
  BackButton: {
    isVisible: boolean
    show: () => void
    hide: () => void
    onClick: (cb: () => void) => void
    offClick: (cb: () => void) => void
  }
  HapticFeedback: {
    impactOccurred: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => void
    notificationOccurred: (type: 'error' | 'success' | 'warning') => void
    selectionChanged: () => void
  }
  showAlert: (message: string, cb?: () => void) => void
  showConfirm: (message: string, cb: (ok: boolean) => void) => void
  openLink: (url: string) => void
}

function getWebApp(): TelegramWebApp | null {
  if (typeof window === 'undefined') return null
  return window.Telegram?.WebApp ?? null
}

let initialized = false

export function initTelegram(): void {
  if (initialized) return
  const wa = getWebApp()
  if (!wa) {
    console.warn(
      '[telegram] WebApp SDK не обнаружен. Запуск в dev-режиме вне Telegram.'
    )
    return
  }

  // Сигналим Telegram, что приложение готово к отображению
  wa.ready()
  // Разворачиваем на весь экран
  wa.expand()
  // Запрашиваем подтверждение перед закрытием (если есть активная задача)
  // wa.enableClosingConfirmation()

  // Применяем цветовую схему Telegram
  applyTheme(wa)

  // Блокируем вертикальные свайпы (iOS) — чтобы не закрывали Mini App
  try {
    wa.disableVerticalSwipes?.()
  } catch {
    // Метод доступен не на всех версиях SDK
  }

  initialized = true
  console.info(
    `[telegram] WebApp initialized. Platform: ${wa.platform}, ` +
    `version: ${wa.version}, scheme: ${wa.colorScheme}`
  )
}

function applyTheme(wa: TelegramWebApp): void {
  const root = document.documentElement

  // Маппим themeParams в CSS-переменные
  const tp = wa.themeParams || {}
  const map: Record<string, string> = {
    bg_color: '--tg-color-bg',
    text_color: '--tg-color-text',
    hint_color: '--tg-color-hint',
    link_color: '--tg-color-link',
    button_color: '--tg-color-button',
    button_text_color: '--tg-color-button-text',
    secondary_bg_color: '--tg-color-secondary-bg',
    section_bg_color: '--tg-color-section-bg',
    section_header_text_color: '--tg-color-section-header-text',
    destructive_text_color: '--tg-color-destructive',
  }

  for (const [tgKey, cssVar] of Object.entries(map)) {
    if (tp[tgKey]) {
      root.style.setProperty(cssVar, tp[tgKey])
    }
  }

  // Dark mode
  if (wa.colorScheme === 'dark') {
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
  }

  // Фон страницы = фону Telegram
  if (tp.bg_color) {
    document.body.style.backgroundColor = tp.bg_color
  }
  if (tp.text_color) {
    document.body.style.color = tp.text_color
  }
}

export function getWebAppSafe(): TelegramWebApp | null {
  return getWebApp()
}

export function getInitData(): string {
  return getWebApp()?.initData ?? ''
}

export function getCurrentUser(): TelegramUser | null {
  return getWebApp()?.initDataUnsafe?.user ?? null
}

export function isInsideTelegram(): boolean {
  return !!getWebApp()
}

export function haptic(
  type: 'light' | 'medium' | 'heavy' | 'success' | 'warning' | 'error' = 'light'
): void {
  const wa = getWebApp()
  if (!wa?.HapticFeedback) return
  if (type === 'success' || type === 'warning' || type === 'error') {
    wa.HapticFeedback.notificationOccurred(type)
  } else {
    wa.HapticFeedback.impactOccurred(type)
  }
}

export function showAlert(message: string): Promise<void> {
  return new Promise((resolve) => {
    const wa = getWebApp()
    if (wa?.showAlert) {
      wa.showAlert(message, () => resolve())
    } else {
      window.alert(message)
      resolve()
    }
  })
}

export function showConfirm(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    const wa = getWebApp()
    if (wa?.showConfirm) {
      wa.showConfirm(message, (ok) => resolve(ok))
    } else {
      resolve(window.confirm(message))
    }
  })
}

export function setMainButton(
  text: string,
  onClick: () => void,
  options: { color?: string; textColor?: string } = {}
): () => void {
  const wa = getWebApp()
  if (!wa?.MainButton) return () => {}

  wa.MainButton.setText(text)
  if (options.color) wa.MainButton.color = options.color
  if (options.textColor) wa.MainButton.textColor = options.textColor
  wa.MainButton.onClick(onClick)
  wa.MainButton.show()
  wa.MainButton.enable()

  return () => {
    wa.MainButton.offClick(onClick)
    wa.MainButton.hide()
  }
}

export function hideMainButton(): void {
  getWebApp()?.MainButton?.hide()
}
