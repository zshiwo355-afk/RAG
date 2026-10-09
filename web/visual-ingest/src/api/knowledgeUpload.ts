import { PortalError } from './knowledgePortal'

export const OSS_ORIGIN = 'https://hr-knowledge-intake-20261008.oss-cn-hangzhou.aliyuncs.com'
export const FILE_LIMIT = 8 * 1024 * 1024
export const PACKAGE_LIMIT = 64 * 1024 * 1024
export interface UploadMetadata { title: string; kind: string; purpose: string; audience: string; inputs: string; outputs: string; dependencies: string; boundaries: string }
export interface Receipt { receipt_id: string; status: string; error_code: string | null; content_verified: boolean; retryable: boolean; published: false; sha256: string; byte_length: number; source_id: string; filename: string }
export interface IntakeResult { ok: true; receipt: Receipt; upload: null | { method: 'PUT'; url: string; headers: Record<string, string>; expires_at: string } }
export interface UploadPackage { blob: Blob; filename: string; sha256: string; source_id: string; idempotency_key: string }
const encoder = new TextEncoder()
const hex = (data: ArrayBuffer) => [...new Uint8Array(data)].map(value => value.toString(16).padStart(2, '0')).join('')
export const sha256 = async (blob: Blob) => {
  if (!globalThis.crypto?.subtle) throw new Error('当前浏览器无法进行安全文件校验，请通过知识中心的 HTTPS 地址打开，并使用最新版浏览器重试。')
  return hex(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()))
}

// Two stored ZIP entries keep original bytes intact; no compression library or executable archive features.
export function storedZip(entries: { name: string; bytes: Uint8Array }[]): Blob {
  const chunks: BlobPart[] = [], directory: Uint8Array[] = []
  let offset = 0
  for (const entry of entries) {
    const name = encoder.encode(entry.name)
    let crc = 0xffffffff
    for (const byte of entry.bytes) { crc ^= byte; for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0) }
    crc = (crc ^ 0xffffffff) >>> 0
    const local = new Uint8Array(30 + name.length), view = new DataView(local.buffer)
    view.setUint32(0, 0x04034b50, true); view.setUint16(4, 20, true); view.setUint16(6, 0x800, true)
    view.setUint16(12, 33, true); view.setUint32(14, crc, true); view.setUint32(18, entry.bytes.length, true); view.setUint32(22, entry.bytes.length, true); view.setUint16(26, name.length, true); local.set(name, 30)
    const central = new Uint8Array(46 + name.length), cv = new DataView(central.buffer)
    cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true); cv.setUint16(8, 0x800, true)
    cv.setUint16(14, 33, true); cv.setUint32(16, crc, true); cv.setUint32(20, entry.bytes.length, true); cv.setUint32(24, entry.bytes.length, true); cv.setUint16(28, name.length, true); cv.setUint32(42, offset, true); central.set(name, 46)
    chunks.push(local.buffer, entry.bytes.slice().buffer); directory.push(central); offset += local.length + entry.bytes.length
  }
  const end = new Uint8Array(22), ev = new DataView(end.buffer)
  ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, entries.length, true); ev.setUint16(10, entries.length, true); ev.setUint32(12, directory.reduce((sum, item) => sum + item.length, 0), true); ev.setUint32(16, offset, true)
  return new Blob([...chunks, ...directory.map(item => item.buffer as ArrayBuffer), end.buffer], { type: 'application/octet-stream' })
}

export async function packageFile(file: File, metadata: UploadMetadata): Promise<UploadPackage> {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!['.md', '.txt', '.docx', '.pdf', '.zip'].includes(extension) || !file.size || file.name.length > 200 || /[\x00-\x1f\x7f/\\]/.test(file.name)) throw new Error('请选择有效的 Markdown、TXT、Word（.docx）、PDF 或资产 ZIP，文件名不超过 200 字。')
  if (file.size > (extension === '.zip' ? PACKAGE_LIMIT : FILE_LIMIT)) throw new Error(extension === '.zip' ? '资产 ZIP 不能超过 64 MiB。' : '单份资料不能超过 8 MiB。')
  const originalHash = await sha256(file)
  let blob: Blob = file, filename = file.name
  if (extension !== '.zip') {
    if (!metadata.title.trim() || !metadata.purpose.trim() || metadata.title.length > 240 || Object.values(metadata).some(value => value.length > 2000)
      || !['方法', '流程', '模板', '提示词', 'Skill方法', '案例', '规则', '参考'].includes(metadata.kind)) throw new Error('请填写知识名称、类型和用途，并检查说明长度。')
    const headers = ['资产编号','名称','类型','状态','用途','正文路径','来源定位','证据状态','限制与待办','合并目标','基线修订','共享范围','使用对象','输入','输出','依赖与权限','适用边界']
    const bodyPath = '知识正文/原始文件' + extension
    const values = ['web_' + originalHash.slice(0, 24), metadata.title.trim(), metadata.kind, '交付', metadata.purpose.trim(), bodyPath,
      '门户提交原件：' + file.name, '上传人提供原始资料；业务效果未独立核实', '', '', '', '全员', metadata.audience.trim(), metadata.inputs.trim(), metadata.outputs.trim(), metadata.dependencies.trim(), metadata.boundaries.trim()]
    const csv = '\ufeff' + [headers, values].map(row => row.map(value => '"' + value.replace(/"/g, '""') + '"').join(',')).join('\r\n') + '\r\n'
    blob = storedZip([{ name: '交付清单.csv', bytes: encoder.encode(csv) }, { name: bodyPath, bytes: new Uint8Array(await file.arrayBuffer()) }])
    filename = 'portal-' + originalHash.slice(0, 16) + '.zip'
  }
  if (blob.size > PACKAGE_LIMIT) throw new Error('资料包超过处理上限。')
  const digest = await sha256(blob)
  return { blob, filename, sha256: digest, source_id: 'portal-' + originalHash, idempotency_key: 'portal-' + digest }
}

async function intake(path: string, method: string, signal: AbortSignal, body?: unknown): Promise<IntakeResult> {
  const response = await fetch('/portal/knowledge/uploads' + path, { method, credentials: 'same-origin', cache: 'no-store', signal,
    headers: method === 'GET' ? {} : { 'Content-Type': 'application/json', 'X-Knowledge-Portal-Request': '1' }, body: body === undefined ? undefined : JSON.stringify(body) })
  if (!response.ok) { const value = await response.json().catch(() => null); throw new PortalError(response.status, value?.error?.code || 'upload_failed') }
  return response.json()
}
export const prepareUpload = (value: UploadPackage, signal: AbortSignal) => intake('', 'POST', signal, { filename: value.filename, sha256: value.sha256, byte_length: value.blob.size, source_id: value.source_id, idempotency_key: value.idempotency_key })
export const completeUpload = (id: string, signal: AbortSignal) => intake('/' + encodeURIComponent(id) + '/complete', 'POST', signal, {})
export const uploadStatus = (id: string, signal: AbortSignal) => intake('/' + encodeURIComponent(id), 'GET', signal)

export async function putOriginal(value: UploadPackage, result: IntakeResult, signal: AbortSignal) {
  if (!result.upload || result.upload.method !== 'PUT' || result.receipt.sha256 !== value.sha256 || result.receipt.byte_length !== value.blob.size) throw new Error('上传授权与当前资料不匹配，请重新申请。')
  const target = new URL(result.upload.url)
  const headers = Object.fromEntries(Object.entries(result.upload.headers).map(([key, val]) => [key.toLowerCase(), val]))
  if (target.origin !== OSS_ORIGIN || target.username || target.password || target.hash || !target.pathname.startsWith('/knowledge-receipts/raw/')
    || headers['content-length'] !== String(value.blob.size) || headers['content-type'] !== 'application/octet-stream'
    || headers['x-oss-object-acl'] !== 'private' || headers['x-oss-forbid-overwrite'] !== 'true'
    || headers['x-oss-meta-sha256'] !== value.sha256 || headers['x-oss-meta-receipt-id'] !== result.receipt.receipt_id
    || Object.keys(headers).some(key => !['content-length','content-type','x-oss-object-acl','x-oss-forbid-overwrite','x-oss-security-token','x-oss-meta-sha256','x-oss-meta-receipt-id'].includes(key))) throw new Error('上传授权不符合私有存储要求。')
  delete headers['content-length'] // File/Blob supplies the signed length through the browser's network stack.
  const response = await fetch(target.href, { method: 'PUT', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', headers, body: value.blob, signal })
  // A lost success response is retried through the same receipt; never overwrite an existing object.
  if (!response.ok && response.status !== 409) throw new Error('原件上传未完成，请检查网络后重试；同一资料重试不会重复提交。')
}
