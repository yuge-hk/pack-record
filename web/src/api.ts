export type Role = 'integrated' | 'station' | 'center' | 'viewer'
export interface Settings {
  [key: string]: unknown
  station_id: string; station_name: string; role: Role; setup_complete: boolean; web_bind: string
  camera: string; microphone: string; audio_enabled: boolean; audio_backend: string; input_format: string
  width: number; height: number; fps: number; encoder: string; quality: number; preset: string
  max_record_minutes: number; record_path: string; record_paths: string[]; cleanup_policy: string
  retention_days: number; disk_warn_gb: number; disk_critical_gb: number
  upload_kind: string; upload_path: string; upload_url: string; upload_token: string
  speech_enabled: boolean; speech_quantity: boolean; speech_remarks: boolean; speech_refund: boolean; order_valid_hours: number
}
export interface Status { access: 'admin' | 'viewer'; settings: Settings; snapshot: { state: string; current: string | null; can_record: boolean }; sequence: number }
export interface Recording { id: string; barcode: string; station_id: string; station_name: string; start_time: string; duration: number; size: number; upload_state: string; capture: { interrupted?: boolean } }
export interface SearchResult { items: Recording[]; total: number; page: number; page_size: number }
export interface ExportJob { id: string; record_id: string; start: number; end: number; state: string; error: string }
export interface Device { id: string; name: string; enabled: number }
export interface Management { volumes?: { path: string; free_gb: number | null; error: string }[]; exports: ExportJob[]; devices?: Device[]; transfers?: { id: string; barcode: string; record_id: string; state: string; attempts: number; error: string }[]; error?: string }
export interface Trial { id: string; state: 'running' | 'ready' | 'error'; error?: string; duration?: number }
export interface Media { url: string; expires_in: number }
export const stateName: Record<string, string> = { local:'仅本地', queued:'待处理', uploading:'上传中', verified:'已校验接收', error:'失败', ready:'可下载', running:'处理中' }
export class ApiClient {
  token = ''
  async request<T>(path: string, body?: unknown): Promise<T> {
    const abort = new AbortController()
    const timer = setTimeout(() => abort.abort(), 20000)
    try {
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', signal: abort.signal,
        headers: { Authorization: 'Bearer ' + this.token, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body) })
      const value = await response.json()
      if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`)
      return value as T
    } finally { clearTimeout(timer) }
  }
  media(body: { record_id?: string; export_id?: string; test_id?: string }) { return this.request<Media>('/v1/playback', body) }
  release(media: Media) { return this.request<boolean>('/v1/playback/release', { ticket: media.url.split('/').pop() }) }
}
