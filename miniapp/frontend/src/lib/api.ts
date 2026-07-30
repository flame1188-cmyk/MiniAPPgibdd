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

/**
 * Скачивает бинарный файл (Excel, PDF, ...) через fetch с X-Tg-Init-Data,
 * преобразует в Blob и запускает браузерный download с заданным именем.
 *
 * Обычный <a download> не подходит, т.к. не передаёт custom headers.
 */
async function downloadBlobUrl(url: string, fallbackFilename: string): Promise<void> {
  const initData = getInitData()
  const headers = new Headers()
  headers.set('X-Tg-Init-Data', initData)

  const response = await fetch(`${API_BASE}${url}`, { headers })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      detail = (body as { detail?: string })?.detail ?? detail
    } catch {
      // не JSON
    }
    throw new ApiError(response.status, detail)
  }

  const blob = await response.blob()

  // Имя файла из Content-Disposition (если сервер его прислал)
  let filename = fallbackFilename
  const cd = response.headers.get('content-disposition')
  if (cd) {
    const match = cd.match(/filename="?([^";]+)"?/)
    if (match && match[1]) filename = match[1]
  }

  // Создаём временный <a> и кликаем по нему
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // Освобождаем память через небольшую задержку (для надёжности download)
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
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
// Analysis: Clusters / Point / LLM
// ============================================================
export type AnalysisStatus = 'idle' | 'running' | 'done' | 'failed'

export interface AnalysisStateResponse {
  status: AnalysisStatus
  progress: number
  stage: string
  error?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface ClusterItem {
  road: string
  zone_type: string
  total_accidents: number
  deaths: number
  injured: number
  dominant_type: string
  type_counter: Record<string, number>
  center?: { lat: number; lon: number } | null
  start_pos?: number | null
  end_pos?: number | null
  dates: string[]
  dynamics: Record<string, any>
  camera_match?: Record<string, any> | null
}

export interface ClustersSummary {
  total_clusters: number
  total_lost: number
  total_preclusters: number
  current_total_dtp: number
  current_deaths: number
  current_injured: number
  dynamics: Record<string, number>
  has_prev_data: boolean
  prev_label?: string | null
  current_label: string
  region_name: string
}

export interface ClustersResult {
  summary: ClustersSummary
  clusters: ClusterItem[]
  preclusters: ClusterItem[]
}

export interface ClustersResponse {
  state: AnalysisStateResponse
  result?: ClustersResult | null
}

export interface PointPeriodStats {
  total: number
  deaths: number
  injured: number
  alcohol: number
  pedestrians: number
  by_type: Record<string, number>
  by_road: Record<string, number>
  by_weather: Record<string, number>
  cards_count: number
  cards_preview: Array<{
    date: string
    time: string
    type: string
    road: string
    deaths: number
    injured: number
    dist_m: number
    lat: number
    lon: number
  }>
}

export interface PointStatsResponse {
  ok: boolean
  center: { lat: number; lon: number }
  radius_m: number
  current_label: string
  prev_label?: string | null
  current?: PointPeriodStats | null
  prev?: PointPeriodStats | null
  error?: string | null
}

export interface LLMProvidersResponse {
  free: boolean
  paid: boolean
  free_model: string
  paid_model: string
}

export interface LLMSummaryResult {
  text: string
  provider: string
  generated_at: string
}

export interface LLMSummaryResponse {
  state: AnalysisStateResponse
  result?: LLMSummaryResult | null
}

export interface LLMAskResponse {
  ok: boolean
  answer?: string | null
  provider?: string | null
  error?: string | null
}

export interface QAHistoryItem {
  question: string
  answer: string
  provider: string
  timestamp: string
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

  // ============================================================
  // Analysis: Clusters (очаги)
  // ============================================================
  startClusters: (taskId: string) =>
    request<ClustersResponse>(`/api/dtp/tasks/${taskId}/clusters`, {
      method: 'POST',
      body: JSON.stringify({}),
    }),

  getClusters: (taskId: string) =>
    request<ClustersResponse>(`/api/dtp/tasks/${taskId}/clusters`),

  getClustersMapUrl: (taskId: string) =>
    `${API_BASE}/api/dtp/tasks/${taskId}/clusters/map?tg_init_data=${encodeURIComponent(getInitData())}`,

  /**
   * Скачивает Excel с очагами (4 листа: очаги/динамика/детализация/предочаги).
   * Запускает браузерный download через создание <a> и клика по нему.
   */
  downloadClustersExcel: async (taskId: string): Promise<void> => {
    const url = `${API_BASE}/api/dtp/tasks/${taskId}/clusters/excel`
    await downloadBlobUrl(url, `dtp_ochagi_${taskId}.xlsx`)
  },

  // ============================================================
  // Analysis: Point statistics
  // ============================================================
  computePointStats: (taskId: string, lat: number, lon: number, radius_m: number) =>
    request<PointStatsResponse>(`/api/dtp/tasks/${taskId}/point`, {
      method: 'POST',
      body: JSON.stringify({ lat, lon, radius_m }),
    }),

  /**
   * Скачивает Excel со статистикой по точке (2 листа: текущий/прошлый период).
   * Требует предварительно выполненный computePointStats.
   */
  downloadPointStatsExcel: async (taskId: string): Promise<void> => {
    const url = `${API_BASE}/api/dtp/tasks/${taskId}/point/excel`
    await downloadBlobUrl(url, `point_stats_${taskId}.xlsx`)
  },

  /**
   * Возвращает URL HTML-карты точки (для <iframe>).
   * Карта: точка + радиус + ДТП (текущий/прошлый) + камеры в радиусе.
   */
  getPointStatsMapUrl: (
    taskId: string,
    lat: number,
    lon: number,
    radius_m: number
  ) =>
    `${API_BASE}/api/dtp/tasks/${taskId}/point/map` +
    `?lat=${lat}&lon=${lon}&radius_m=${radius_m}` +
    `&tg_init_data=${encodeURIComponent(getInitData())}`,

  // ============================================================
  // Analysis: LLM
  // ============================================================
  getLLMProvidersForTask: (taskId: string) =>
    request<LLMProvidersResponse>(`/api/dtp/tasks/${taskId}/llm/providers`),

  startLLMSummary: (taskId: string, provider: 'free' | 'paid') =>
    request<LLMSummaryResponse>(`/api/dtp/tasks/${taskId}/llm/summary`, {
      method: 'POST',
      body: JSON.stringify({ provider }),
    }),

  getLLMSummary: (taskId: string) =>
    request<LLMSummaryResponse>(`/api/dtp/tasks/${taskId}/llm/summary`),

  askLLM: (taskId: string, question: string, provider: 'free' | 'paid') =>
    request<LLMAskResponse>(`/api/dtp/tasks/${taskId}/llm/ask`, {
      method: 'POST',
      body: JSON.stringify({ question, provider }),
    }),

  getQAHistory: (taskId: string) =>
    request<QAHistoryItem[]>(`/api/dtp/tasks/${taskId}/llm/qa-history`),
}
