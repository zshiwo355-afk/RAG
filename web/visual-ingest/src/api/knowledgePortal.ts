export type Scope = 'self' | 'company'
export type JobStatus = 'queued' | 'running' | 'completed' | 'needs_review' | 'failed'
export type ReceiptStatus = 'awaiting_upload' | 'pending_verification' | 'verified' | 'rejected' | 'failed'
export type ReceiptCounts = Record<ReceiptStatus | 'total', number>
export interface ReceiptRecord {
  receipt_id: string
  filename: string
  byte_length: number
  status: ReceiptStatus
  error_code: string | null
  created_at: string
  updated_at: string
  content_verified: boolean
  retryable: boolean
  job_id: string | null
  job_status: JobStatus | null
}
export interface ReceiptList { scope: Scope; items: ReceiptRecord[]; total: number; limit: number; offset: number; receipt_counts: ReceiptCounts }
export const receiptStatusLabels: Record<ReceiptStatus, string> = { awaiting_upload: '等待上传', pending_verification: '正在核验', verified: '核验通过', rejected: '核验拒收', failed: '核验失败' }
export function receiptErrorMessage(code: string | null): string {
  const labels: Record<string, string> = {
    object_size_mismatch: '上传大小与申报不一致，原件核验未通过。',
    object_hash_mismatch: '上传内容与申报的文件指纹不一致，原件核验未通过。',
    object_not_private: '原件的私有访问设置不符合要求。',
    object_metadata_mismatch: '原件标识与本次收件不一致。',
    object_etag_invalid: '未取得有效的原件校验标识。', object_changed: '核验期间原件发生变化。',
    verification_retries_exhausted: '已达到自动核验重试上限，尚未进入内容处理。',
    verification_timed_out: '原件核验超时，尚未确认内容完整性。',
    verification_unavailable: '原件核验服务暂不可用。', receipt_storage_unavailable: '原件存储服务暂不可用。',
  }
  return code ? labels[code] || '核验遇到异常，请保留收件编号联系维护人员核对。' : ''
}

export const processingReasonLabels: Record<string, string> = { source_manifest: '来源清单已归档', structured_delivery_required: '需要提供结构完整的交付清单', sharing_scope_unconfirmed: '共享范围尚未确认为全员', reuse_information_incomplete: '使用对象、输入输出或适用范围等信息不完整', restricted_material: '资料含有限制共享标识，需要核对', manifest_redaction_review: '交付清单包含敏感内容，已脱敏并等待核对', source_base_revision_required: '内容有更新，需要提供对应的基线修订', source_base_revision_conflict: '基线修订与当前资料版本不一致', older_source_revision: '属于已有资料的历史版本', outdated_submission: '此提交已有更新版本', published_duplicate: '与已发布知识一致，保留提交记录', automatic_rules_passed: '自动规则检查已通过', automatic_publication_disabled: '自动发布暂未启用，保留草稿', publication_version_conflict: '发布期间版本发生变化，需要重新核对', attachment_manifest_unverified: '附件清单尚未核验完整', previously_withdrawn_revision: '此版本曾撤回，需要重新核对后再发布', legacy_attachment_manifest_unverified: '历史知识附件清单不完整，需要核对', possible_content_conflict: '存在同名但内容不同的正式知识，需要核对', shared_asset_update_requires_review: '更新涉及其他来源的正式知识，需要核对', replaced_by_submission: '已关联补充收件，继续跟进新的处理记录', reviewer_archived: '已记录处理说明并归档', unsupported_file_type: '当前文件类型暂不支持自动解析', processing_input_limit: '资料大小超过本阶段处理额度', processing_member_limit: '单个文件超过处理额度', processing_archive_limit: '压缩包展开后超过处理额度', invalid_archive: '压缩包损坏或格式不受支持', processing_retries_exhausted: '已达到自动重试上限', processing_storage_unavailable: '存储服务暂不可用，将按队列重试', processing_unavailable: '处理服务暂不可用，将按队列重试', source_authorship_unverified: '内容提供者与作者身份尚未核实', source_incomplete: '来源信息不完整', duplicate_content: '内容与已有知识一致，保留提交记录', outdated_revision: '属于已有知识的历史版本', source_version_conflict: '资料版本存在冲突，需要核对', replacement_submitted: '已关联补充资料，继续跟进新的处理记录', manually_archived: '经核对仅归档保留', incomplete_content: '正文缺少必要信息', package_contains_credentials: '资料包包含凭据类文件，需要核对', preview_unavailable: '暂未生成完整的可阅读副本', existing_asset_requires_review: '存在历史知识，需要核对版本关系', existing_asset_or_invalid_draft: '历史知识关联或草稿结构需要核对', no_parseable_items: '未取得可解析的内容', redaction_review: '内容已脱敏，需要核对完整性', unresolved_reference: '存在未取得的引用或附件', script_not_ingested: '脚本仅保留原件，不执行或自动入库', linked_to_asset: '原文件已关联知识条目', unsupported_format: '当前格式暂不支持自动解析', missing_attachment: '缺少必要附件', sensitive_content: '含有需要核对的敏感内容', missing_provenance: '来源信息不完整', parse_failed: '内容解析失败', empty_content: '未取得可用正文', no_reusable_content: '未发现可复用内容', processing_failed: '处理未完成，请核对服务记录', manual_review_required: '需要人工核对', review_required: '需要人工核对' }
export const processingEventLabels: Record<string, string> = { indexing: '正在建立索引', created: '任务已创建', queued: '进入处理队列', claimed: '开始处理', processing_started: '开始处理', downloading: '读取核验后的原件', parsing: '解析内容', persisting: '保存处理结果', importing: '写入草稿', item_draft: '条目已形成草稿', item_needs_review: '条目进入待处理', item_archived: '条目已归档', item_duplicate: '条目与已有内容重复', item_outdated: '条目属于历史版本', item_indexing: '条目正在建立索引', item_published: '条目已正式发布', item_replaced: '已关联补充收件', item_resolved: '异常已处理', parsed: '内容已解析', cleaned: '内容已清洗', draft_created: '草稿已保存', completed: '本次处理完成', needs_review: '进入待处理', failed: '处理失败', retry_requested: '已申请重试', retried: '重新进入队列', lease_recovered: '恢复中断任务' }

export interface PortalSession {
  authenticated: true
  principal: { id: string; display_name: string }
  permissions: string[]
  capabilities: { review_write: boolean; upload?: boolean }
  upload_limits?: { max_bytes: number; max_file_bytes: number; allowed_extensions: string[]; oss_origin: string }
  expires_at: string
}

export interface Overview {
  scope: Scope
  receipt_counts?: ReceiptCounts
  counts: {
    receipts: number
    queued: number
    running: number
    completed: number
    needs_review: number
    failed: number
    draft_items: number
    review_items: number
    archived_items: number
    duplicate_items?: number
    outdated_items?: number
    indexing_items?: number
    published_items?: number
    published_assets: number | null
  }
  generated_at: string
}

export interface ProcessingItem {
  item_id: string
  source_path: string
  status: string
  reason_codes: string[]
  knowledge_id: string | null
  revision: string | number | null
  title: string
  has_preview: boolean
  version: number
  published: boolean
  replacement_receipt_id?: string | null
  replacement_job_id?: string | null
}

export interface ProcessingEvent {
  event_id: string
  event_type: string
  created_at: string
  reason_code: string | null
}

export interface ProcessingJob {
  job_id: string
  receipt_id: string
  source_id: string
  filename: string
  rule_version: string
  status: JobStatus
  stage: string
  attempts: number
  created_at: string
  updated_at: string
  error_code: string | null
  retryable: boolean
  published: boolean
  counts: { draft: number; needs_review: number; archived: number; duplicate?: number; outdated?: number; indexing?: number; published?: number }
  items?: ProcessingItem[]
  events?: ProcessingEvent[]
  item_total?: number
  event_total?: number
  item_offset?: number
  event_offset?: number
  limit?: number
}

export interface JobList {
  items: ProcessingJob[]
  total: number
  limit: number
  offset: number
  scope: Scope
}

export interface DraftPreview {
  item_id: string
  title: string
  content: string
  knowledge_id: string | null
  revision: string | number | null
  published: boolean
  currently_published: boolean
}

export type ResolveRequest = { expected_version: number; reason: string } & (
  { action: 'replace'; replacement_receipt_id: string } | { action: 'archive' }
)

export class PortalError extends Error {
  constructor(public status: number, public code: string) {
    super(code === 'portal_write_disabled' ? '处理操作暂未启用，当前可查看记录。请联系管理员开启门户写操作。'
      : status === 401 ? '登录已过期，请重新登录。'
      : status === 403 ? '当前账号没有此项权限，请联系管理员在 MCP 中配置。'
        : status === 404 ? '记录不存在，或已不在你的可见范围内。'
          : status === 409 ? '记录状态已经变化，或补充收件尚不符合处理条件。请刷新后核对。'
            : status === 400 || status === 422 ? '提交内容不符合要求，请检查处理说明、条目版本和收件编号。'
            : status === 429 ? '请求较多，请稍后重试。'
              : status === 502 || status === 503 ? '知识服务暂时不可用，请稍后重试。'
                : '请求未完成，请稍后重试。')
  }
}

const SESSION = '/portal/knowledge/session'
const API = '/portal/knowledge/api'

async function request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  const cancel = () => controller.abort()
  if (signal?.aborted) cancel()
  else signal?.addEventListener('abort', cancel, { once: true })
  const timeout = window.setTimeout(() => controller.abort(), 30_000)
  try {
    const response = await fetch(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers: method === 'GET' ? {} : {
        'Content-Type': 'application/json',
        'X-Knowledge-Portal-Request': '1'
      },
      body: body === undefined ? undefined : JSON.stringify(body)
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new PortalError(response.status, payload?.error?.code || `http_${response.status}`)
    }
    return response.status === 204 ? undefined as T : await response.json() as T
  } catch (error) {
    if (error instanceof PortalError) throw error
    throw new Error(controller.signal.aborted ? '请求超时，请检查连接后重试。' : '无法连接知识中心，请检查网络后重试。')
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener('abort', cancel)
  }
}

const scopeQuery = (scope: Scope) => `scope=${scope}`
export const getSession = () => request<PortalSession>(SESSION)
export const createSession = (credential: string) => request<PortalSession>(SESSION, 'POST', { credential })
export const deleteSession = () => request<void>(SESSION, 'DELETE')
export const getOverview = (scope: Scope) => request<Overview>(`${API}/overview?${scopeQuery(scope)}`)
export const getReceipts = (scope: Scope, status: ReceiptStatus | '', page: number, pageSize: number, signal?: AbortSignal) => {
  const params = new URLSearchParams({ scope, limit: String(pageSize), offset: String((page - 1) * pageSize) })
  if (status) params.set('status', status)
  return request<ReceiptList>(`${API}/receipts?${params}`, 'GET', undefined, signal)
}
export const getJobs = (scope: Scope, status: string, page: number, pageSize: number, signal?: AbortSignal) => {
  const params = new URLSearchParams({ scope, limit: String(pageSize), offset: String((page - 1) * pageSize) })
  if (status) params.set('status', status)
  return request<JobList>(`${API}/jobs?${params}`, 'GET', undefined, signal)
}
export const getJob = (id: string, scope: Scope, itemOffset = 0, eventOffset = 0, limit = 100, signal?: AbortSignal) => {
  const params = new URLSearchParams({ scope, limit: String(limit), item_offset: String(itemOffset), event_offset: String(eventOffset) })
  return request<ProcessingJob>(`${API}/jobs/${encodeURIComponent(id)}?${params}`, 'GET', undefined, signal)
}
export const retryJob = (id: string) => request<ProcessingJob>(`${API}/jobs/${encodeURIComponent(id)}/retry`, 'POST', {})
export const resolveItem = (jobId: string, itemId: string, body: ResolveRequest) => request<ProcessingItem>(`${API}/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/resolve`, 'POST', body)
export const getDraft = (jobId: string, itemId: string, scope: Scope) => request<DraftPreview>(`${API}/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}?${scopeQuery(scope)}`)

export interface KnowledgeAsset {
  knowledge_id: string
  title: string
  kind: string
  source_kind?: string
  revision: number
  published_at: string | null
  uploader_position: string | null
  scenarios: string[]
  related_knowledge_ids: string[]
  summary?: string | null
}
export interface CatalogGroup { key: string; label: string; count: number }
export interface KnowledgeCatalog {
  items: KnowledgeAsset[]
  total: number
  limit: number
  offset: number
  counts: { published_assets: number; positions: number; unknown_position: number }
  by_kind: CatalogGroup[]
  by_uploader_position: CatalogGroup[]
  generated_at: string
}
export type PublishedKnowledge = Pick<KnowledgeAsset, 'knowledge_id' | 'title' | 'kind' | 'revision' | 'published_at' | 'uploader_position' | 'source_kind' | 'scenarios' | 'summary'> & { content: string }
export const getCatalog = (filters: { q: string; kind: string; uploader_position: string; limit: number; offset: number }) => {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => { if (value !== '') params.set(key, String(value)) })
  return request<KnowledgeCatalog>(`${API}/catalog?${params}`)
}
export const getPublishedKnowledge = (id: string) => request<PublishedKnowledge>(`${API}/catalog/${encodeURIComponent(id)}`)

export interface KnowledgeSearchResult {
  ok: true
  query: string
  result_count: number
  results: { knowledge_id: string; revision: number; title: string; kind: string; snippet: string; rerank_mode: 'model' | 'fallback_rrf' }[]
}
export const searchKnowledge = (query: string, signal?: AbortSignal) => request<KnowledgeSearchResult>(`${API}/search`, 'POST', { query, top_k: 10 }, signal)

export interface GraphNode {
  knowledge_id: string
  title: string
  kind: string
  revision: number
  uploader_position: string | null
}
export interface GraphEdge {
  id: string
  source: string
  target: string
  relation: 'reference' | 'related'
  label: string
  directed: boolean
  reason: string
  evidence: { knowledge_id: string; revision: number; excerpt: string }[]
  terms: string[]
}
export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  status: 'building' | 'ready' | 'partial'
  counts: { published_assets: number; indexed_assets: number; failed_assets: number; explicit_edges: number; related_edges: number }
  truncated: boolean
  generated_at: string
}
export const getGraph = () => request<GraphData>(`${API}/graph`)
