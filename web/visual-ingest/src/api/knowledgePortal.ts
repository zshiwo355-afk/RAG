export type Scope = 'self' | 'company'
export type JobStatus = 'queued' | 'running' | 'completed' | 'needs_review' | 'failed'
export type ReceiptStatus = 'awaiting_upload' | 'pending_verification' | 'verified' | 'rejected' | 'failed'
export type ReceiptCounts = Record<ReceiptStatus | 'total', number>
export interface ProcessingFilters {
  q?: string
  department?: string
  role_id?: string
  processing_status?: JobStatus | 'none' | ''
  created_from?: string
  created_to?: string
}
export interface ProcessingRoleAssignment {
  item_id: string
  title: string
  role: { role_id: string; department: string; name: string }
  confirmation_source: string | null
  updated_at: string | null
}
export interface ReceiptRecord {
  receipt_id: string
  filename: string
  display_title?: string | null
  material_count?: number
  uploader_positions?: string[]
  uploader_departments?: string[]
  byte_length: number
  status: ReceiptStatus
  error_code: string | null
  created_at: string
  updated_at: string
  content_verified: boolean
  retryable: boolean
  job_id: string | null
  job_status: JobStatus | null
  job_stage?: string | null
  job_updated_at?: string | null
  role_assignments?: ProcessingRoleAssignment[]
  role_assignments_total?: number
  role_assignments_updated_at?: string | null
  attention_required?: boolean
  counts?: Record<string, number>
  blocking_reasons?: string[]
  blocking_reasons_truncated?: boolean
}
export interface ReceiptList { scope: Scope; items: ReceiptRecord[]; total: number; limit: number; offset: number; receipt_counts: ReceiptCounts }
export const receiptStatusLabels: Record<ReceiptStatus, string> = { awaiting_upload: '等待上传', pending_verification: '正在核验', verified: '核验通过', rejected: '核验拒收', failed: '核验失败' }
const displayLabel = (labels: Record<string, string>, value: string, fallback: string) => Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : fallback
export const receiptStatusName = (value: string) => displayLabel(receiptStatusLabels, value, '核验状态待确认')
export function materialTitle(record: { display_title?: string | null; material_count?: number; filename?: string | null }): string {
  const title = record.display_title?.trim()
  if (title) return Number.isInteger(record.material_count) && record.material_count! > 1 ? `${title}等 ${record.material_count} 份资料` : title
  const filename = record.filename?.trim() || ''
  return /\p{Script=Han}/u.test(filename) ? filename : '资料标题待识别'
}
export function uploaderPosition(record: { uploader_positions?: string[]; uploader_departments?: string[] }): string {
  const positions = [...new Set((record.uploader_positions || []).map(value => value.trim()).filter(Boolean))]
  const departments = [...new Set((record.uploader_departments || []).map(value => value.trim()).filter(Boolean))]
  return `${positions.join('、') || '未取得岗位信息'}${departments.length ? `；部门：${departments.join('、')}` : ''}`
}
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
  return code ? displayLabel(labels, code, '核验遇到异常，请保留收件编号联系维护人员核对。') : ''
}

export const processingReasonLabels: Record<string, string> = { item_uploader_role_selected: '按本条已确认岗位重新检查资料', source_relation_review: '来源标识与其他资料重合，需确认这是独立新资料还是原资料的更新', referenced_material_unparsed: '引用的材料尚未成功解析', skill_package_missing: '未提供完整技能包', skill_package_invalid: '技能包结构不完整或不符合要求', unknown_role_id: '资料指定的岗位不存在', role_id_required: '资料尚未指定有效岗位', role_metadata_mismatch: '资料声明的部门和岗位与目录不一致', invalid_role_id: '资料中的岗位标识无效', role_metadata_incomplete: '资料缺少完整的部门或岗位信息', processing_parse_rejected: '资料解析未通过', uploader_role_selected: '按已确认岗位重新检查资料', source_manifest: '来源清单已归档', structured_delivery_required: '需要提供结构完整的交付清单', sharing_scope_unconfirmed: '共享范围尚未确认为全员', reuse_information_incomplete: '使用对象、输入输出或适用范围等信息不完整', restricted_material: '资料含有限制共享标识，需要核对', manifest_redaction_review: '交付清单包含敏感内容，已脱敏并等待核对', source_base_revision_required: '内容有更新，需要提供对应的基线修订', source_base_revision_conflict: '基线修订与当前资料版本不一致', older_source_revision: '属于已有资料的历史版本', outdated_submission: '此提交已有更新版本', published_duplicate: '与已发布知识一致，保留提交记录', automatic_rules_passed: '自动规则检查已通过', automatic_publication_disabled: '自动发布暂未启用，保留草稿', publication_version_conflict: '发布期间版本发生变化，需要重新核对', attachment_manifest_unverified: '附件清单尚未核验完整', previously_withdrawn_revision: '此版本曾撤回，需要重新核对后再发布', legacy_attachment_manifest_unverified: '历史知识附件清单不完整，需要核对', possible_content_conflict: '存在同名但内容不同的正式知识，需要核对', shared_asset_update_requires_review: '更新涉及其他来源的正式知识，需要核对', replaced_by_submission: '已关联补充收件，继续跟进新的处理记录', reviewer_archived: '已记录处理说明并归档', unsupported_file_type: '当前文件类型暂不支持自动解析', processing_input_limit: '资料大小超过本阶段处理额度', processing_member_limit: '单个文件超过处理额度', processing_archive_limit: '压缩包展开后超过处理额度', invalid_archive: '压缩包损坏或格式不受支持', processing_retries_exhausted: '已达到自动重试上限', processing_storage_unavailable: '存储服务暂不可用，将按队列重试', processing_unavailable: '处理服务暂不可用，将按队列重试', source_authorship_unverified: '内容提供者与作者身份尚未核实', source_incomplete: '来源信息不完整', duplicate_content: '内容与已有知识一致，保留提交记录', outdated_revision: '属于已有知识的历史版本', source_version_conflict: '资料版本存在冲突，需要核对', replacement_submitted: '已关联补充资料，继续跟进新的处理记录', manually_archived: '经核对仅归档保留', incomplete_content: '正文缺少必要信息', package_contains_credentials: '资料包包含凭据类文件，需要核对', preview_unavailable: '暂未生成完整的可阅读副本', existing_asset_requires_review: '存在历史知识，需要核对版本关系', existing_asset_or_invalid_draft: '历史知识关联或草稿结构需要核对', no_parseable_items: '未取得可解析的内容', redaction_review: '内容已脱敏，需要核对完整性', unresolved_reference: '存在未取得的引用或附件', script_not_ingested: '脚本仅保留原件，不执行或自动入库', script_dependency_not_provided: '资料依赖的脚本或工具尚未提供，需要补充后核对', linked_to_asset: '原文件已关联知识条目', unsupported_format: '当前格式暂不支持自动解析', missing_attachment: '缺少必要附件', sensitive_content: '含有需要核对的敏感内容', missing_provenance: '来源信息不完整', parse_failed: '内容解析失败', empty_content: '未取得可用正文', no_reusable_content: '未发现可复用内容', processing_failed: '处理未完成，请核对服务记录', manual_review_required: '需要人工核对', review_required: '需要人工核对', uploader_role_not_found: '未找到对应岗位，请确认本人岗位名称；资料已保留，无需重新上传', uploader_role_ambiguous: '存在同名岗位，请补充所属部门；资料已保留，无需重新上传', uploader_position_is_department: '填写的是部门，请补充本人的具体岗位；资料已保留', uploader_department_mismatch: '所填部门与岗位目录不一致，请核对；资料已保留', uploader_role_conflict: '本次岗位与资料原有岗位不一致，请确认后再处理，原归属保持不变' }
export const processingEventLabels: Record<string, string> = { item_uploader_role_selected: '已确认本条资料岗位', uploader_role_selected: '已确认上传人的默认岗位', indexing: '正在建立索引', created: '任务已创建', queued: '进入处理队列', claimed: '开始处理', processing_started: '开始处理', downloading: '读取核验后的原件', parsing: '解析内容', persisting: '保存处理结果', importing: '写入草稿', item_draft: '条目已形成草稿', item_needs_review: '条目进入待处理', item_archived: '条目已归档', item_duplicate: '条目与已有内容重复', item_outdated: '条目属于历史版本', item_indexing: '条目正在建立索引', item_published: '条目已正式发布', item_replaced: '已关联补充收件', item_resolved: '异常已处理', parsed: '内容已解析', cleaned: '内容已清洗', draft_created: '草稿已保存', completed: '本次处理完成', needs_review: '进入待处理', failed: '处理失败', retry_requested: '已申请重试', retried: '重新进入队列', lease_recovered: '恢复中断任务' }
export const processingStatusLabels: Record<string, string> = { queued: '排队中', running: '处理中', completed: '处理完成', needs_review: '待处理', failed: '处理失败' }
export const processingStatusName = (value: string) => displayLabel(processingStatusLabels, value, '处理状态待确认')
export const processingItemStatusName = (value: string) => displayLabel({ draft: '已形成草稿', needs_review: '待处理', archived: '仅归档', duplicate: '重复内容', outdated: '历史版本', indexing: '索引处理中', published: '发布回执' }, value, '条目状态待确认')
export const processingStageName = (value: string) => displayLabel({ queued: '等待处理', downloading: '读取原件', parsing: '解析内容', cleaning: '清洗内容', drafting: '保存草稿', persisting: '保存处理结果', importing: '写入草稿', indexing: '建立检索索引', publishing: '验证并发布知识', completed: '处理结束', review: '等待核对', needs_review: '等待核对', failed: '处理未完成', done: '处理结束' }, value, '处理阶段待确认')
export const processingEventName = (value: string) => displayLabel(processingEventLabels, value, '处理事件待确认')
export const processingReasonName = (value: string) => value === 'source_ingestion_source_conflict' ? '资料来源与已有记录不一致，需要核对' : displayLabel(processingReasonLabels, value, '处理原因待核对')
export const knowledgeKindName = (value: string) => displayLabel({ sop: '岗位流程', process: '岗位流程', skill: '智能体技能', case: '案例', method: '方法', template: '模板', prompt: '提示词', policy: '规则', reference: '参考', other: '其他' }, value, '其他资料')

export interface PortalSession {
  authenticated: true
  principal: { id: string; display_name: string }
  permissions: string[]
  capabilities: { review_write: boolean; upload?: boolean; uploader_role_write?: boolean }
  upload_limits?: { max_bytes: number; max_file_bytes: number; allowed_extensions: string[]; oss_origin: string }
  expires_at: string
}

export interface Overview {
  scope: Scope
  receipt_counts?: ReceiptCounts
  counts: {
    receipts: number
    attention_jobs?: number
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
  missing_references?: string[]
  current_reason_codes?: string[]
  rechecking?: boolean
  role_assignment?: ProcessingRoleAssignment | null
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
  role_assignments?: ProcessingRoleAssignment[]
  role_assignments_total?: number
  role_assignments_updated_at?: string | null
  attention_required?: boolean
  job_id: string
  receipt_id: string
  source_id: string
  filename: string
  display_title?: string | null
  material_count?: number
  uploader_positions?: string[]
  uploader_departments?: string[]
  rule_version: string
  status: JobStatus
  stage: string
  attempts: number
  created_at: string
  updated_at: string
  error_code: string | null
  blocking_reasons?: string[]
  blocking_reasons_truncated?: boolean
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

const blockerGuidance = [
  { codes: ['uploader_role_not_found', 'uploader_role_ambiguous', 'uploader_position_is_department', 'uploader_department_mismatch'], stage: '岗位匹配', action: '在详情中选择部门和岗位；目录没有对应岗位时可添加后保存。' },
  { codes: ['unknown_role_id', 'role_id_required', 'role_metadata_mismatch', 'invalid_role_id', 'role_metadata_incomplete'], stage: '岗位信息校验', action: '核对资料声明的部门和岗位是否与目录一致，修正后重新提交。' },
  { codes: ['uploader_role_conflict'], stage: '岗位冲突核对', action: '在详情中逐份确认岗位；上传原声明与原资料归属保留，已有正式资料不会自动改岗。' },
  { codes: ['source_relation_review'], stage: '来源关系核对', action: '独立新资料：在详情中勾选“独立的新资料”，点击“确认来源关系并重新检查”。原资料更新请勿勾选；联系原提交人通过原渠道携正确版本重新提交。' },
  { codes: ['unresolved_reference', 'missing_attachment', 'attachment_manifest_unverified', 'legacy_attachment_manifest_unverified', 'referenced_material_unparsed', 'skill_package_missing', 'skill_package_invalid'], stage: '资料完整性检查', action: '补齐正文引用的文档和附件，随资料包重新提交。' },
  { codes: ['script_dependency_not_provided'], stage: '资料完整性检查', action: '补齐正文依赖的脚本或工具文件，随资料包重新提交。' },
  { codes: ['reuse_information_incomplete', 'incomplete_content', 'structured_delivery_required'], stage: '交付信息检查', action: '补齐使用对象、输入、输出、依赖和适用范围，再提交完整资料。' },
  { codes: ['possible_content_conflict', 'shared_asset_update_requires_review', 'existing_asset_requires_review', 'existing_asset_or_invalid_draft'], stage: '内容冲突核对', action: '对照现有正式知识，确认是新资料还是修订；修订需提供对应基线版本。' },
  { codes: ['source_base_revision_required', 'source_base_revision_conflict', 'source_version_conflict', 'publication_version_conflict'], stage: '版本核对', action: '核对当前正式版本，补充正确的基线修订后再提交。' },
  { codes: ['sharing_scope_unconfirmed', 'restricted_material', 'manifest_redaction_review', 'redaction_review', 'sensitive_content', 'package_contains_credentials'], stage: '共享与敏感信息检查', action: '确认共享范围，移除凭据或完成脱敏后再提交。' },
  { codes: ['source_authorship_unverified', 'source_incomplete', 'missing_provenance', 'source_ingestion_source_conflict'], stage: '来源核验', action: '补充或核对作者、来源定位和来源标识，再提交可核验的资料。' },
  { codes: ['unsupported_file_type', 'unsupported_format', 'invalid_archive', 'parse_failed', 'no_parseable_items', 'empty_content', 'no_reusable_content', 'processing_parse_rejected', 'preview_unavailable'], stage: '正文解析', action: '检查原文件能否打开，提供受支持格式和完整可阅读的正文。' },
  { codes: ['processing_input_limit', 'processing_member_limit', 'processing_archive_limit'], stage: '文件额度检查', action: '按提示拆分资料、缩小文件后重新提交。' },
  { codes: ['previously_withdrawn_revision'], stage: '发布检查', action: '核对历史撤回原因，确认处理完成后再决定是否重新发布。' },
  { codes: ['processing_storage_unavailable', 'processing_unavailable', 'processing_retries_exhausted', 'processing_failed'], stage: '系统处理', action: '保留收件编号，联系维护人员核对服务和失败记录后再重试。' },
]

export function processingBlockers(job: Pick<ProcessingJob, 'status' | 'error_code' | 'blocking_reasons' | 'items' | 'attention_required'>) {
  if (!(job.attention_required ?? (job.status === 'needs_review' || job.status === 'failed'))) return []
  const codes = [...new Set(job.blocking_reasons ?? [
    ...(job.error_code ? [job.error_code] : []),
    ...(job.items || []).filter(item => item.status === 'needs_review').flatMap(item => item.current_reason_codes ?? item.reason_codes),
  ])]
  return (codes.length ? codes : ['manual_review_required']).map(code => {
    const guidance = blockerGuidance.find(entry => entry.codes.includes(code))
    return { code, stage: guidance?.stage || '待核对（具体阶段未取得）', reason: processingReasonName(code),
      action: guidance?.action || '打开处理详情核对逐项结果；原因仍不明确时，保留收件编号联系维护人员。' }
  })
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
    super(code === 'role_department_not_found' ? '所选部门已不在目录中，请刷新岗位列表后重新选择。'
      : code === 'uploader_role_version_conflict' ? '这份资料已被其他操作更新，请核对最新归属后重新选择。'
      : code === 'source_action_not_allowed' ? '这份资料的来源状态已变化，请刷新后核对，不能直接另建资料。'
      : code === 'uploader_role_item_required' ? '这次收件包含多份资料，请刷新页面后逐份确认岗位。'
      : code === 'uploader_role_job_busy' ? '资料正在处理，请等待处理完成后再确认岗位。'
      : code === 'uploader_role_not_found' ? '所选岗位已不在目录中，请刷新岗位列表后重选。'
      : code === 'portal_role_assignment_disabled' ? '岗位确认尚未启用，请联系管理员。'
      : code === 'portal_write_disabled' ? '处理操作暂未启用，当前可查看记录。请联系管理员开启门户写操作。'
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
export const createSession = (username: string, password: string) => request<PortalSession>(SESSION, 'POST', { username, password })
export const deleteSession = () => request<void>(SESSION, 'DELETE')
export const getOverview = (scope: Scope) => request<Overview>(`${API}/overview?${scopeQuery(scope)}`)
export const getReceipts = (scope: Scope, status: ReceiptStatus | '', page: number, pageSize: number, signal?: AbortSignal, filters: ProcessingFilters = {}) => {
  const params = new URLSearchParams({ scope, limit: String(pageSize), offset: String((page - 1) * pageSize) })
  if (status) params.set('status', status)
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value)
  return request<ReceiptList>(`${API}/receipts?${params}`, 'GET', undefined, signal)
}
export const getJobs = (scope: Scope, status: string, page: number, pageSize: number, signal?: AbortSignal, filters: ProcessingFilters = {}) => {
  const params = new URLSearchParams({ scope, limit: String(pageSize), offset: String((page - 1) * pageSize) })
  if (status) params.set('status', status)
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value)
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
  role_id?: string | null
  department?: string | null
  role_name?: string | null
  category?: KnowledgeCategory
  scenarios: string[]
  related_knowledge_ids: string[]
  summary?: string | null
}
export type KnowledgeCategory = 'sop' | 'skill' | 'other'
export interface KnowledgeRole {
  role_id: string
  department: string
  name: string
  counts: Record<KnowledgeCategory, number>
  total: number
}
export interface UploaderRoleAssignment {
  role: Pick<KnowledgeRole, 'role_id' | 'department' | 'name'> | null
  version: number
  updated_at: string | null
  requeued: boolean
}
export const getUploaderRole = (jobId: string) => request<UploaderRoleAssignment>(`${API}/jobs/${encodeURIComponent(jobId)}/uploader-role`)
export const setUploaderRole = (jobId: string, roleId: string, expectedVersion: number) => request<UploaderRoleAssignment>(`${API}/jobs/${encodeURIComponent(jobId)}/uploader-role`, 'POST', { role_id: roleId, expected_version: expectedVersion })

export interface ItemUploaderRoleAssignment extends UploaderRoleAssignment {
  original: { department: string | null; position: string | null }
  confirmation_source: 'manual' | 'legacy' | 'alias' | null
  can_remember: boolean
  remembered: boolean
  rule_status: 'none' | 'saved' | 'matched' | 'conflict' | 'incomplete'
  remaining_reasons: string[]
  eligible: boolean
}
export interface ItemUploaderRoleRequest {
  role_id: string
  expected_version: number
  remember_rule: boolean
  source_action: 'auto' | 'new'
}
const itemRolePath = (jobId: string, itemId: string) => `${API}/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/uploader-role`
export const getItemUploaderRole = (jobId: string, itemId: string) => request<ItemUploaderRoleAssignment>(itemRolePath(jobId, itemId))
export const setItemUploaderRole = (jobId: string, itemId: string, body: ItemUploaderRoleRequest) => request<ItemUploaderRoleAssignment>(itemRolePath(jobId, itemId), 'POST', body)

export interface KnowledgeFile {
  file_id: string
  filename: string
  media_type: string
  byte_length: number
  sha256: string
  purpose: 'original' | 'preview' | 'skill'
}
export interface FileAccess { url?: string; html?: string; expires_in: number; filename: string; media_type: string }
export const getRoles = () => request<{ items: KnowledgeRole[] }>(`${API}/roles`)
export const createRole = (department: string, name: string) => request<KnowledgeRole>(`${API}/roles`, 'POST', { department, name })
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
export type PublishedKnowledge = Omit<KnowledgeAsset, 'related_knowledge_ids'> & { content: string; files?: KnowledgeFile[] }
export const getCatalog = (filters: { q?: string; kind?: string; uploader_position?: string; role_id?: string; category?: KnowledgeCategory | ''; limit: number; offset: number }) => {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => { if (value !== '') params.set(key, String(value)) })
  return request<KnowledgeCatalog>(`${API}/catalog?${params}`)
}
export const getPublishedKnowledge = (id: string) => request<PublishedKnowledge>(`${API}/catalog/${encodeURIComponent(id)}`)
export const getKnowledgeFile = (id: string, fileId: string, disposition: 'inline' | 'attachment') => request<FileAccess>(`${API}/catalog/${encodeURIComponent(id)}/files/${encodeURIComponent(fileId)}?disposition=${disposition}`)
export function safeFileUrl(value: string, origin = window.location.origin): string {
  const url = new URL(value, origin)
  const local = new URL(origin)
  const loopback = (hostname: string) => ['localhost', '127.0.0.1', '[::1]'].includes(hostname)
  if (url.username || url.password || !['https:', 'http:'].includes(url.protocol)
    || !(url.protocol === 'https:' || url.origin === local.origin || (loopback(local.hostname) && loopback(url.hostname)))) {
    throw new Error('文件链接不符合安全要求，请重新获取。')
  }
  return url.href
}

export interface KnowledgeSearchResult {
  ok: true
  query: string
  result_count: number
  results: { knowledge_id: string; revision: number; title: string; kind: string; snippet: string; rerank_mode: 'model' | 'fallback_rrf' }[]
}
export const searchKnowledge = (query: string, signal?: AbortSignal, filters: { role_id?: string; category?: KnowledgeCategory | '' } = {}) => request<KnowledgeSearchResult>(`${API}/search`, 'POST', { query, top_k: 10, ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)) }, signal)

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
export const getGraph = (signal?: AbortSignal) => request<GraphData>(`${API}/graph`, 'GET', undefined, signal)
