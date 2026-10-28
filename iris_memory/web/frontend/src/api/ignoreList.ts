import { apiGet, apiPost } from './request'

export type IgnoreRowStatus = 'ok' | 'invalid' | 'not_active'

/** own = 手写名单（可编辑）；external = 外部黑名单同步（只读） */
export type IgnoreRowSource = 'own' | 'external'

export interface IgnoreRow {
  index: number
  /** 用户写的原始内容，回显用 */
  raw: string
  /** 解析出的纯数字 ID（可能为空 = 格式无效） */
  ids: string[]
  active: boolean
  status: IgnoreRowStatus
  source: IgnoreRowSource
}

export interface IgnoreListPayload {
  rows: IgnoreRow[]
  raw: string
  effective: string[]
  external: string[]
  sync_external: boolean
  sync_user_only: boolean
}

export interface CheckRow {
  raw: string
  ids: string[]
  valid: boolean
}

interface ApiBaseResponse {
  success: boolean
  error?: string
}

function checkSuccess(response: ApiBaseResponse, errorMsg: string): void {
  if (!response.success) {
    throw new Error(response.error || errorMsg)
  }
}

export async function getIgnoreList(): Promise<IgnoreListPayload> {
  const response = await apiGet<any>('ignore-list')
  checkSuccess(response, '读取忽略名单失败')
  return {
    rows: response.rows || [],
    raw: response.raw || '',
    effective: response.effective || [],
    external: response.external || [],
    sync_external: !!response.sync_external,
    sync_user_only: !!response.sync_user_only,
  }
}

export async function saveIgnoreList(
  entries: string[],
): Promise<IgnoreListPayload> {
  const response = await apiPost<any>('ignore-list/save', { entries })
  checkSuccess(response, '保存忽略名单失败')
  return {
    rows: response.rows || [],
    raw: response.raw || '',
    effective: response.effective || [],
    external: response.external || [],
    sync_external: false,
    sync_user_only: false,
  }
}

export async function checkIgnoreIds(text: string): Promise<CheckRow[]> {
  const response = await apiPost<any>('ignore-list/check', { text })
  checkSuccess(response, '校验 ID 失败')
  return response.rows || []
}
