/// <reference types="vite/client" />
/// <reference types="leaflet" />

interface ImportMetaEnv {
  /** Базовый URL API. Пусто = тот же origin (для production). */
  readonly VITE_API_BASE?: string
  /**
   * Версия сборки (git commit / docker-tag), встраивается в bundle
   * при `npm run build` через env. Используется в useVersionCheck.ts
   * для сравнения с /api/version и показа баннера обновления.
   * Не задана в dev-режиме — хук тогда тихо отключается.
   */
  readonly VITE_APP_VERSION?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
