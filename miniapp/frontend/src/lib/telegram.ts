/**
 * Обёртка над Telegram WebApp SDK (с фиксом устаревшего initData).
 *
 * Что нового:
 * - isInitDataStale() — проверяет возраст initDataUnsafe.auth_date
 *   и возвращает true, если он старше 23 часов (с запасом 1ч от
 *   24-часового лимита Telegram).
 * - ensureFreshInitData() — если initData устарел, принудительно
 *   перезагружает страницу, чтобы Telegram выдал свежий initData.
 *   Если вне Telegram — возвращает пустую строку без reload.
 * - getInitDataFresh() — безопасный геттер: если initData устарел,
 *   возвращает '' (не отправляем протухший токен на сервер).
 *
 * Документация: https://core.telegram.org/bots/webapps
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
  isFullscreen?: boolean

  ready: () => void
  expand: () => void
  close: () => void
  setHeaderColor: (color: 'bg_color' | 'secondary_bg_color') => void
  setBackgroundColor: (color: string) => void
  enableClosingConfirmation: () => void
  disableVerticalSwipes?: () => void
  requestFullscreen?: () => Promise<void>
  exitFullscreen?: () => Promise<void>
  isVersionAtLeast?: (version: string) => boolean
  onEvent: (event: string, cb: () => void) => void
  offEvent: (event: string, cb: () => void) => void
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

  wa.ready()
  wa.expand()
  applyTheme(wa)

  try {
    wa.disableVerticalSwipes?.()
  } catch {
    // Метод доступен не на всех версиях SDK
  }

  // === Sprint 7 fix: проверяем свежесть initData при старте ===
  // Если initData старше 23 часов (близко к 24-часовому лимиту Telegram),
  // принудительно перезагружаем страницу — Telegram выдаст свежий.
  // Это решает проблему 401 Unauthorized после длительного простоя Mini App.
  if (isInitDataStale()) {
    console.warn(
      `[telegram] initData устарел (auth_date=${wa.initDataUnsafe.auth_date}, ` +
      `age=${ Math.floor((Date.now() / 1000 - (wa.initDataUnsafe.auth_date ?? 0)) / 3600) }h). ` +
      `Перезагружаем страницу для получения свежего токена.`
    )
    // Небольшая задержка, чтобы успели отработать ready/expand
    setTimeout(() => {
      window.location.reload()
    }, 100)
    return  // Не продолжаем инициализацию — страница сейчас перезагрузится
  }

  initialized = true
  console.info(
    `[telegram] WebApp initialized. Platform: ${wa.platform}, ` +
    `version: ${wa.version}, scheme: ${wa.colorScheme}, ` +
    `initData_age_min=${ wa.initDataUnsafe.auth_date ? Math.floor((Date.now() / 1000 - wa.initDataUnsafe.auth_date) / 60) : 'N/A' }`
  )
}

function applyTheme(wa: TelegramWebApp): void {
  const root = document.documentElement

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

  if (wa.colorScheme === 'dark') {
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
  }

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

/**
 * Проверяет, устарел ли initData (близок к 24-часовому лимиту Telegram).
 * Порог — 23 часа (1 час запаса, чтобы успеть сделать запрос до истечения).
 *
 * Возвращает false, если:
 * - WebApp SDK недоступен (вне Telegram)
 * - auth_date отсутствует или некорректен
 * - возраст initData меньше 23 часов
 *
 * Возвращает true, если возраст initData >= 23 часов.
 */
export function isInitDataStale(): boolean {
  const wa = getWebApp()
  if (!wa) return false
  const authDate = wa.initDataUnsafe?.auth_date
  if (!authDate || typeof authDate !== 'number' || authDate <= 0) {
    return false
  }
  const ageSeconds = Date.now() / 1000 - authDate
  // 23 часа = 82800 сек (запас 1ч от 24-часового лимита Telegram)
  return ageSeconds > 82800
}

/**
 * Возвращает initData, только если он свежий.
 * Если устарел — возвращает пустую строку (сервер всё равно вернёт 401).
 *
 * Используется в api.ts вместо getInitData(), чтобы не отправлять
 * заведомо протухший токен.
 */
export function getInitDataFresh(): string {
  if (isInitDataStale()) {
    return ''
  }
  return getWebApp()?.initData ?? ''
}

/**
 * Если initData устарел — принудительно перезагружает страницу.
 * Используется в api.ts при получении 401, чтобы восстановить сессию
 * без ручного закрытия/открытия Mini App.
 */
export function ensureFreshInitData(): void {
  if (isInitDataStale()) {
    console.warn('[telegram] initData устарел — перезагружаем страницу для восстановления сессии')
    window.location.reload()
  }
}

/**
 * Оригинальный геттер initData — возвращает как есть, без проверки свежести.
 * Используется только в крайних случаях (например, для логирования).
 * Для API-запросов используйте getInitDataFresh().
 */
export function getInitData(): string {
  return getWebApp()?.initData ?? ''
}

export function getCurrentUser(): TelegramUser | null {
  return getWebApp()?.initDataUnsafe?.user ?? null
}

export function isInsideTelegram(): boolean {
  return !!getWebApp()
}

/**
 * Детектит Telegram Desktop (Windows/Mac/Linux).
 */
export function isTelegramDesktop(): boolean {
  const wa = getWebApp()
  if (!wa) {
    return typeof window !== 'undefined' && window.innerWidth >= 900
  }
  return wa.platform === 'tdesktop'
}

export function getContainerMaxWidth(): string {
  return isTelegramDesktop() ? 'max-w-5xl' : 'max-w-xl'
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

export function isFullscreenSupported(): boolean {
  const wa = getWebApp()
  if (!wa) return false
  if (typeof wa.requestFullscreen !== 'function') return false
  if (typeof wa.isVersionAtLeast === 'function') {
    return wa.isVersionAtLeast('8.0')
  }
  return true
}

export function isFullscreenActive(): boolean {
  return !!getWebApp()?.isFullscreen
}

export function requestAppFullscreen(): Promise<void> {
  const wa = getWebApp()
  if (!wa || typeof wa.requestFullscreen !== 'function') {
    return Promise.reject(new Error('requestFullscreen is not supported'))
  }
  return wa.requestFullscreen()
}

export function exitAppFullscreen(): Promise<void> {
  const wa = getWebApp()
  if (!wa || typeof wa.exitFullscreen !== 'function') {
    return Promise.reject(new Error('exitFullscreen is not supported'))
  }
  return wa.exitFullscreen()
}

export function onFullscreenChange(cb: (isFullscreen: boolean) => void): () => void {
  const wa = getWebApp()
  if (!wa) return () => {}

  const handler = () => cb(!!wa.isFullscreen)
  wa.onEvent('fullscreenChanged', handler)
  return () => {
    wa.offEvent('fullscreenChanged', handler)
  }
}

export function expandApp(): void {
  getWebApp()?.expand()
}

export function isExpandedActive(): boolean {
  return !!getWebApp()?.isExpanded
}

export function onExpandedChange(cb: (isExpanded: boolean) => void): () => void {
  const wa = getWebApp()
  if (!wa) return () => {}

  const handler = () => cb(!!wa.isExpanded)
  wa.onEvent('viewportChanged', handler)
  return () => {
    wa.offEvent('viewportChanged', handler)
  }
}
