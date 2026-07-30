/**
 * API-клиент для общения с FastAPI backend.
 *
 * Все запросы автоматически добавляют Telegram initData в заголовок
 * X-Tg-Init-Data — это нужно для проверки подписи на сервере.
 */
import { getInitData } from './telegram'

export const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public body?: unknown
  ) {
    super(detail)
    this.name = 'ApiError'
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const initData = getInitData()
  const headers = new Headers(options.headers)
  headers.set('X-Tg-Init-Data', initData)

  // Устанавливаем Content-Type: application/json ТОЛЬКО если body — строка
  // (JSON). Для FormData браузер сам поставит multipart/form-data с
  // правильным boundary — нельзя это перетирать.
  if (options.body && typeof options.body === 'string') {
    headers.set('Content-Type', 'application/json')
  }

  const url = `${API_BASE}${path}`
  const response = await fetch(url, {
    ...options,
    headers,
  })

  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    let body: unknown
    try {
      body = await response.json()
      detail = (body as { detail?: string })?.detail ?? detail
    } catch {
      // Не JSON — игнорируем
    }
    throw new ApiError(response.status, detail, body)
  }

  if (response.status === 204) {
    return undefined as T
  }

  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    return (await response.json()) as T
  }
  return (await response.text()) as unknown as T
}

// ============================================================
// Types
// ============================================================
export interface Region {
  code: string
  name: string
  title?: string
}

export interface ParseResult {
  ok: boolean
  region_code?: string
  region_name?: string
  period?: string
  raw_query: string
  error?: string
}

export type TaskStatus =
  | 'pending'
  | 'fetching'
  | 'parsing'
  | 'analytics'
  | 'generating'
  | 'done'
  | 'failed'

export interface TaskFile {
  type: string
  filename: string
  size_bytes: number
  mime: string
}

export interface TaskStatusResponse {
  task_id: string
  status: TaskStatus
  progress: number
  region_code: string
  region_name: string
  period: string
  error?: string | null
  files: TaskFile[]
  analytics?: Record<string, unknown> | null
}

// Структурированный запрос на создание задачи (без текстового парсинга)
export interface StructuredTaskRequest {
  region_code: string
  region_name: string
  dat_list: string[]          // ['1.2025', '2.2025', ...]
  period_label: string         // '2025 год' / 'I квартал 2025'
}

// ============================================================
// Cameras
// ============================================================
export interface CameraRegionInfo {
  reg_code: string
  reg_name: string | null
  has_file: boolean
  file_size_bytes: number
  file_modified: string | null
  cameras_count: number
  cameras_with_piket: number
}

export interface CameraListResponse {
  regions: CameraRegionInfo[]
  total_regions: number
  total_cameras: number
}

export interface CameraUploadResponse {
  ok: boolean
  reg_code: string
  file_size_bytes: number
  cameras_count: number
  cameras_with_piket: number
  message: string
}

// ============================================================
// API methods
// ============================================================
export const api = {
  health: () => request<{ status: string }>('/health'),

  listRegions: () => request<Region[]>('/api/regions'),

  searchRegions: (q: string) =>
    request<Region[]>(`/api/regions/search?q=${encodeURIComponent(q)}`),

  parseQuery: (query: string) =>
    request<ParseResult>('/api/parse', {
      method: 'POST',
      body: JSON.stringify({ query }),
    }),

  // Structured-режим: регион и период выбраны из списка, парсинг не нужен.
  createStructuredTask: (params: StructuredTaskRequest) =>
    request<{ task_id: string; status: TaskStatus; region_code: string; region_name: string; period: string }>(
      '/api/dtp/tasks',
      {
        method: 'POST',
        body: JSON.stringify(params),
      }
    ),

  // Legacy-режим: текстовый запрос (для обратной совместимости).
  createTask: (params: { query?: string; region_code?: string; period?: string }) =>
    request<{ task_id: string; status: TaskStatus; region_code: string; region_name: string; period: string }>(
      '/api/dtp/tasks',
      {
        method: 'POST',
        body: JSON.stringify(params),
      }
    ),

  getTask: (taskId: string) =>
    request<TaskStatusResponse>(`/api/dtp/tasks/${taskId}`),

  listTasks: (limit = 20) =>
    request<TaskStatusResponse[]>(`/api/dtp/tasks?limit=${limit}`),

  getTaskFiles: (taskId: string) =>
    request<TaskFile[]>(`/api/dtp/tasks/${taskId}/files`),

  getMapUrl: (taskId: string) =>
    `${API_BASE}/api/dtp/tasks/${taskId}/map?tg_init_data=${encodeURIComponent(getInitData())}`,

  getDownloadUrl: (taskId: string, fileType: string) =>
    `${API_BASE}/api/dtp/tasks/${taskId}/download/${fileType}?tg_init_data=${encodeURIComponent(getInitData())}`,

  // ============================================================
  // Cameras
  // ============================================================
  listCameras: () => request<CameraListResponse>('/api/cameras'),

  getCamerasStatus: (regCode: string) =>
    request<CameraRegionInfo>(`/api/cameras/${regCode}`),

  uploadCameras: (regCode: string, file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return request<CameraUploadResponse>(`/api/cameras/${regCode}`, {
      method: 'POST',
      body: formData,
      // НЕ устанавливаем Content-Type — браузер сам поставит multipart/form-data
      // с правильным boundary. Заголовок X-Tg-Init-Data добавится в request().
    })
  },

  deleteCameras: (regCode: string) =>
    request<{ ok: boolean; reg_code: string; deleted: boolean }>(
      `/api/cameras/${regCode}`,
      { method: 'DELETE' }
    ),
}
