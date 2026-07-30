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
}
