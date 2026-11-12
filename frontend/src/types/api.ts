export interface ApiError {
  code: string
  message: string
  detail?: string
  traceId?: string | null
  status?: number
  extra?: Record<string, unknown>
}

export interface PageMeta {
  total: number
  returned: number
  filters: Record<string, unknown>
}

export interface ListResponse<T> {
  items: T[]
  meta: PageMeta
}
