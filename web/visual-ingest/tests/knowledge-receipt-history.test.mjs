import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createSSRApp, createRenderer, KeepAlive, nextTick, ref, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const settings = { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }
const actualAPI = moduleURL(ts.transpileModule(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8'), settings).outputText)
const { PortalError, materialTitle, uploaderPosition, receiptErrorMessage, receiptStatusName, processingStatusName, processingItemStatusName, processingStageName, processingEventName, processingReasonName, knowledgeKindName } = await import(actualAPI)
const apiURL = moduleURL(`export { PortalError, materialTitle, uploaderPosition, receiptErrorMessage, receiptStatusLabels, receiptStatusName, processingStatusLabels, processingStatusName, processingStageName, processingItemStatusName, processingReasonName } from '${actualAPI}';
 export async function getRoles() { globalThis.roleCalls = (globalThis.roleCalls || 0) + 1; return globalThis.rolesPlan ? globalThis.rolesPlan() : { items: [{ role_id: 'role-ai', department: '总经办', name: 'AI架构师' }] } }
 export async function getReceipts(...args) { globalThis.calls.push(args); return globalThis.receiptsPlan(...args) }`)
const source = await readFile(new URL('../src/components/ReceiptList.vue', import.meta.url), 'utf8')
const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'receipt-history' }).content, settings).outputText
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
const component = (await import(moduleURL(code))).default
const record = { receipt_id: 'a'.repeat(32), filename: 'safe.zip', status: 'rejected', error_code: 'object_hash_mismatch', job_id: null, retryable: false, created_at: '2026-10-15T07:00:00Z', updated_at: '2026-10-15T08:00:00Z', byte_length: 3072 }
const result = { items: [record], total: 41, receipt_counts: { total: 41, rejected: 1, failed: 0 } }
async function harness(permissions = ['company_knowledge.submissions.read'], scope = 'self', initialState) {
  let state; const events = [], pendingSignals = [], stateUpdates = []; globalThis.calls = []
  await renderToString(createSSRApp({ ...component, setup(props, context) { state = component.setup(props, context); return state }, render() { return null } }, {
    session: { principal: { id: 'alice' }, permissions }, scope, refreshKey: 0, state: initialState, onError: value => events.push(value), onPending: value => pendingSignals.push(value), 'onUpdate:state': value => stateUpdates.push(value),
  }))
  return { state, events, pendingSignals, stateUpdates }
}
globalThis.receiptsPlan = () => result
let { state, events, pendingSignals } = await harness(); await state.load()
assert.equal(state.items.value[0].status, 'rejected'); assert.equal(state.items.value[0].job_id, null)
assert.equal(state.counts.value.rejected, 1)
assert.equal(pendingSignals.at(-1), false)
globalThis.receiptsPlan = () => ({ ...result, receipt_counts: { ...result.receipt_counts, pending_verification: 2 } })
await state.load(); assert.equal(pendingSignals.at(-1), true, 'active verification needs background refresh')
globalThis.receiptsPlan = () => ({ ...result, receipt_counts: { ...result.receipt_counts, pending_verification: 0, awaiting_upload: 4 } })
await state.load(); assert.equal(pendingSignals.at(-1), false, 'idle upload reservations do not need 30-second refresh')
globalThis.receiptsPlan = () => ({ ...result, items: [{ ...record, job_id: 'job-active', job_status: 'running' }] })
await state.load(); assert.equal(pendingSignals.at(-1), true, 'active content processing keeps receipt tracking refreshed')
globalThis.receiptsPlan = () => result
state.changePage(2); await state.load(); assert.equal(globalThis.calls.at(-1)[2], 2)
state.status.value = 'rejected'; state.filter(); await state.load()
assert.deepEqual(globalThis.calls.at(-1).slice(0, 4), ['self', 'rejected', 1, 20])
const persisted = { status: 'verified', q: '交接', department: '总经办', role_id: 'role-ai', processing_status: 'needs_review', created_from: '2026-10-01', created_to: '2026-10-20', page: 2 }
const restored = await harness(['company_knowledge.review'], 'company', persisted); await restored.state.load()
assert.deepEqual(globalThis.calls.at(-1).slice(0, 4), ['company', 'verified', 2, 20], 'mount keeps saved verification filter and page')
assert.deepEqual(globalThis.calls.at(-1)[5], { q: '交接', department: '总经办', role_id: 'role-ai', processing_status: 'needs_review', created_from: '2026-10-01T00:00:00+08:00', created_to: '2026-10-21T00:00:00+08:00' }, 'every filter reaches the server before pagination; inclusive UI end date becomes next-day exclusive boundary')
assert.deepEqual(restored.stateUpdates, [], 'mount does not overwrite the parent saved state')
const beforeDraft = globalThis.calls.length
restored.state.q.value = '未提交的输入'
await nextTick()
assert.equal(globalThis.calls.length, beforeDraft, 'typing does not fetch or change the active filter')
restored.state.changePage(3); await restored.state.load()
assert.equal(globalThis.calls.at(-1)[5].q, '交接', 'paging retains the applied filter rather than submitting unfinished input')
restored.state.filter(); await restored.state.load()
assert.equal(globalThis.calls.at(-1)[5].q, '未提交的输入'); assert.equal(globalThis.calls.at(-1)[2], 1)
assert.equal(restored.stateUpdates.at(-1).q, '未提交的输入')
restored.state.created_from.value = '2026-10-21'; restored.state.created_to.value = '2026-10-20'
const beforeInvalidDates = globalThis.calls.length
restored.state.filter()
assert.equal(globalThis.calls.length, beforeInvalidDates, 'reversed dates do not query or replace saved state')
assert.match(restored.state.error.value, /开始日期不能晚于结束日期/)
assert.match(receiptErrorMessage(record.error_code), /文件指纹/)
assert(!receiptErrorMessage('https://private.invalid/?secret=token').includes('secret'))
assert.equal(materialTitle({ filename: 'native-text.zip', display_title: '  自动采集故障分层核验方法  ', material_count: 1 }), '自动采集故障分层核验方法')
assert.equal(materialTitle({ filename: 'a'.repeat(64) + '.zip', display_title: '岗位交接流程', material_count: 3 }), '岗位交接流程等 3 份资料')
for (const filename of ['native-text.zip', 'probe.txt', 'a'.repeat(64) + '.zip', '']) assert.equal(materialTitle({ filename }), '资料标题待识别', 'technical filenames are preserved only in details, not guessed as knowledge titles')
assert.equal(materialTitle({ filename: '技术主管岗位流程.docx' }), '技术主管岗位流程.docx', 'older Chinese filenames remain useful before the server supplies a title')
assert.equal(materialTitle({ filename: 'native-text.zip', display_title: 'Deployment Checklist', material_count: 1 }), 'Deployment Checklist', 'a genuine source title is not silently translated or discarded')
assert.equal(materialTitle({ display_title: '岗位交接流程', material_count: -1 }), '岗位交接流程')
assert.equal(materialTitle({ display_title: '岗位交接流程', material_count: 1.5 }), '岗位交接流程')
assert.equal(uploaderPosition({ uploader_positions: [' AI架构师 ', 'AI架构师', ''], uploader_departments: [' 总经办 ', '总经办'] }), 'AI架构师；部门：总经办')
assert.equal(uploaderPosition({ uploader_positions: ['SSC', '人事专员'], uploader_departments: ['组织人才发展中心', '人事部'] }), 'SSC、人事专员；部门：组织人才发展中心、人事部', 'multiple independent declarations must not be guessed into department-role pairs')
assert.equal(uploaderPosition({ uploader_departments: ['技术部'] }), '未取得岗位信息；部门：技术部', 'a department alone must not be presented as a role')
assert.equal(uploaderPosition({}), '未取得岗位信息')
assert.equal(uploaderPosition({ uploader_positions: [], uploader_departments: [] }), '未取得岗位信息')
assert.equal(uploaderPosition({ role_name: '人事专员', department: '人事部' }), '未取得岗位信息', 'a formal catalog role must not be substituted for the upload-time declaration')
assert.equal(processingReasonName('script_dependency_not_provided'), '资料依赖的脚本或工具尚未提供，需要补充后核对')
const descriptor = parse(source).descriptor
const templateCode = compileTemplate({ source: descriptor.template.content, filename: 'ReceiptList.vue', id: 'receipt-history', compilerOptions: { bindingMetadata: compileScript(descriptor, { id: 'receipt-history' }).bindings } }).code
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
const { render } = await import(moduleURL(templateCode))
const positionHTML = await renderToString(createSSRApp({ ...component, render, setup() { return { ...state, items: ref([
  { ...record, updated_at: '2026-10-16T08:49:00Z', uploader_positions: ['AI架构师'], uploader_departments: ['总经办'] },
  { ...record, receipt_id: 'b'.repeat(32), updated_at: '2026-10-16T08:47:00Z' },
  { ...record, receipt_id: 'c'.repeat(32), updated_at: '2026-10-16T08:46:00Z', uploader_positions: ['<b>人事专员</b>'] },
]), counts: ref(null), loading: ref(false), error: ref('') } } }, { session: { principal: { id: 'admin' }, permissions: ['company_knowledge.review'] }, scope: 'company', refreshKey: 0 }))
assert.match(positionHTML, /上传岗位：AI架构师；部门：总经办/)
assert.match(positionHTML, /上传岗位：未取得岗位信息/)
assert.match(positionHTML, /上传岗位：&lt;b&gt;人事专员&lt;\/b&gt;/)
assert(!positionHTML.includes('<b>人事专员</b>'), 'upload-time position declarations are escaped')
const tracked = { ...record, status: 'verified', error_code: null, job_id: 'job-a', job_status: 'completed', job_stage: 'completed', job_updated_at: '2026-10-16T12:00:00Z', counts: { draft: 1, published: 0 }, uploader_positions: ['原声明岗位'], role_assignments: [{ item_id: 'item-a', title: '交接规范', role: { role_id: 'role-ai', department: '总经办', name: '<b>AI架构师</b>' }, confirmation_source: 'item', updated_at: '2026-10-17T12:00:00Z' }] }
const truncatedTracked = { ...tracked, role_assignments_total: 8, role_assignments_updated_at: '2026-10-20T12:00:00Z',
  role_assignments: Array.from({ length: 5 }, (_, index) => ({ ...tracked.role_assignments[0], item_id: `item-${index}`, title: `交接规范 ${index + 1}` })),
}
const trackingHTML = await renderToString(createSSRApp({ ...component, render, setup() { return { ...state, items: ref([
  truncatedTracked,
  { ...tracked, receipt_id: 'd'.repeat(32), job_status: 'needs_review', counts: { needs_review: 1 }, blocking_reasons: ['script_dependency_not_provided'] },
  { ...record, status: 'verified', error_code: null },
]), counts: ref({ total: 41, rejected: 1, failed: 0, pending_verification: 0, awaiting_upload: 0 }), loading: ref(false), error: ref('') } } }, { session: { principal: { id: 'admin' }, permissions: ['company_knowledge.review'] }, scope: 'company', refreshKey: 0 }))
assert.match(trackingHTML, /上传岗位：原声明岗位/)
assert.match(trackingHTML, /总经办 \/ &lt;b&gt;AI架构师&lt;\/b&gt;/)
assert(!trackingHTML.includes('<b>AI架构师</b>'), 'confirmed assignments are escaped separately from original claims')
assert.match(trackingHTML, /归属已记录/)
assert.match(trackingHTML, /另 3 条归属见详情/)
assert.match(trackingHTML, /资料依赖的脚本或工具尚未提供/)
assert.match(trackingHTML, /尚未取得确认归属/)
assert.match(trackingHTML, /尚未进入内容处理/)
assert.match(trackingHTML, /已形成草稿 1/)
assert.match(trackingHTML, /本次处理已结束，正式发布以发布回执为准/)
assert.match(trackingHTML, /查看处理详情/)
assert.equal(state.latestUpdatedAt(tracked), '2026-10-17T12:00:00Z', 'updated time includes later role confirmation')
assert.equal(state.latestUpdatedAt(truncatedTracked), '2026-10-20T12:00:00Z', 'an updated assignment outside the first five still advances the eight-material receipt timestamp')
assert(trackingHTML.includes(state.formatDate(truncatedTracked.role_assignments_updated_at)), 'the latest full-summary assignment time is rendered in the receipt row')
assert.match(trackingHTML, /当前身份范围共 41 份收件（包含未命中筛选的收件）/)
assert.equal(receiptStatusName('verified'), '核验通过', 'receipt verification must not be labelled published')
assert.equal(processingStatusName('completed'), '处理完成', 'job completion is separate from publication')
for (const translate of [receiptStatusName, processingStatusName, processingItemStatusName, processingStageName, processingEventName, processingReasonName]) {
  assert.match(translate('new_internal_code'), /[\u4e00-\u9fff]/)
  assert(!translate('new_internal_code').includes('new_internal_code'), 'unknown raw codes must not become user-facing status messages')
  for (const unknown of ['__proto__', 'constructor', 'toString']) assert.equal(typeof translate(unknown), 'string')
}
assert.match(processingReasonName('source_ingestion_source_conflict'), /来源.*不一致/)
assert.equal(knowledgeKindName('sop'), '岗位流程')
assert.equal(knowledgeKindName('skill'), '智能体技能')
assert.equal(state.formatBytes(3072), '3 KB')
const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
let copied
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { async writeText(value) { copied = value } } } })
await state.copyRecord(record)
assert.equal(copied, `原文件名：safe.zip\n收件编号：${'a'.repeat(32)}\n核验状态码：rejected\n原始错误码：object_hash_mismatch`)
assert.equal(state.copyNotice.value, '已复制核验信息')
globalThis.navigator.clipboard.writeText = async () => { throw new Error('denied') }
await state.copyRecord(record)
assert.match(state.copyNotice.value, /选中.*复制/)
let finishCopy
globalThis.navigator.clipboard.writeText = () => new Promise(resolve => { finishCopy = resolve })
const copying = state.copyRecord(record); state.clear(); finishCopy(); await copying
assert.equal(state.copyReceiptId.value, '', 'late clipboard completion cannot restore a previous identity or page notice')
assert.equal(state.copyNotice.value, '')
if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator); else delete globalThis.navigator
assert.match(source, /<details class="receipt-technical"><summary>技术信息<\/summary>/)
assert.match(source, /<strong class="receipt-title">\{\{ materialTitle\(item\) \}\}<\/strong>/)
for (const status of [401, 403]) {
  globalThis.receiptsPlan = () => { throw new PortalError(status, 'denied') }
  await state.load(); assert.deepEqual(state.items.value, []); assert.equal(state.counts.value, null); assert.equal(state.total.value, null)
  assert.equal(pendingSignals.at(-1), false, 'denied or failed reads cannot retain a stale pending signal')
  assert(events.some(value => value instanceof PortalError && value.status === status))
}
globalThis.receiptsPlan = () => { throw new PortalError(404, 'missing') }; await state.load()
assert.match(state.error.value, /不能据此判断没有异常/)
globalThis.receiptsPlan = () => result
;({ state } = await harness(['company_knowledge.dashboard.read'], 'company')); await state.load()
assert.equal(globalThis.calls.length, 0); assert.equal(state.items.value.length, 0)
;({ state } = await harness(['company_knowledge.review'], 'company')); await state.load()
assert.equal(globalThis.calls.at(-1)[0], 'company')
let finish
globalThis.receiptsPlan = () => new Promise(resolve => { finish = resolve })
const pending = state.load(); state.clear(); finish(result); await pending
assert.deepEqual(state.items.value, [], 'a late response must not restore cleared private records')
console.log('Receipt history: rejected originals without jobs, pagination, filters, safe reasons, denied access and stale response clearing passed.')

// A successful session heartbeat must not reset the receipt filter or page.
globalThis.receiptsPlan = () => result; globalThis.calls = []
const sessionRef = ref({ principal: { id: 'alice' }, permissions: ['company_knowledge.review'] })
let liveState
const liveComponent = { ...component, setup(props, context) { liveState = component.setup(props, context); return liveState }, render() { return null } }
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
const app = renderer.createApp({ render() { return h(liveComponent, { session: sessionRef.value, scope: 'company', refreshKey: 0 }) } })
app.mount({})
try {
  await nextTick(); await new Promise(setImmediate)
  liveState.status.value = 'rejected'; liveState.page.value = 2
  const before = globalThis.calls.length
  sessionRef.value = { principal: { id: 'alice' }, permissions: ['company_knowledge.review'] }
  await nextTick(); await new Promise(setImmediate)
  assert.equal(globalThis.calls.length, before, 'unchanged session fields do not refetch receipts')
  assert.equal(liveState.status.value, 'rejected'); assert.equal(liveState.page.value, 2)
  liveState.showAll(); await nextTick(); await new Promise(setImmediate)
  assert.equal(liveState.status.value, ''); assert.equal(liveState.page.value, 1)
  assert.deepEqual(globalThis.calls.at(-1).slice(0, 4), ['company', '', 1, 20], 'summary entry resets receipt filters and pagination')
  sessionRef.value = { principal: { id: 'alice' }, permissions: [] }
  await nextTick(); await new Promise(setImmediate)
  assert.deepEqual(liveState.items.value, [], 'revoked read permission clears receipts immediately')
} finally { app.unmount() }
console.log('Receipt history session heartbeat: filter/page retained, permission loss clears records.')

// Parent URL/history state controls the list without an echo request or losing unsent input.
globalThis.receiptsPlan = () => result; globalThis.calls = []
const routeState = ref({ ...persisted, scope: 'company' })
const controlledApp = renderer.createApp({ render() { return h(liveComponent, {
  session: { principal: { id: 'alice' }, permissions: ['company_knowledge.review'] }, scope: 'company', refreshKey: 0,
  state: routeState.value, 'onUpdate:state': value => { routeState.value = { ...value, scope: 'company' } },
}) } })
controlledApp.mount({})
try {
  await nextTick(); await new Promise(setImmediate)
  assert.equal(liveState.page.value, 2); assert.equal(liveState.q.value, '交接')
  assert.equal(globalThis.calls.length, 1, 'saved state loads once without an initial reset')
  liveState.q.value = '已提交的条件'
  const beforeSubmit = globalThis.calls.length
  liveState.filter(); await nextTick(); await new Promise(setImmediate)
  assert.equal(globalThis.calls.length, beforeSubmit + 1, 'parent echo does not fetch the same filter twice')
  assert.equal(routeState.value.q, '已提交的条件'); assert.equal(routeState.value.page, 1)
  routeState.value = { ...persisted, q: '历史条件', page: 3, scope: 'company' }
  await nextTick(); await new Promise(setImmediate)
  assert.equal(liveState.q.value, '历史条件'); assert.equal(liveState.page.value, 3)
  assert.equal(globalThis.calls.at(-1)[5].q, '历史条件'); assert.equal(globalThis.calls.at(-1)[2], 3)
  assert.equal(liveState.hasFilters.value, true)
  liveState.showAll(); await nextTick(); await new Promise(setImmediate)
  assert.equal(liveState.hasFilters.value, false, 'parent-only scope metadata is not treated as a receipt filter')
  globalThis.receiptsPlan = () => ({ ...result, total: 21 })
  routeState.value = { ...persisted, page: 3 }
  await nextTick(); await new Promise(setImmediate)
  assert.equal(liveState.page.value, 2); assert.equal(routeState.value.page, 2, 'out-of-range pages update the parent URL state after a result shrinks')
} finally { controlledApp.unmount() }
console.log('Receipt tracking: server filters, inclusive date range, confirmed/original roles, separate processing state and controlled history restoration passed.')

// Exercise the real KeepAlive lifecycle used by the tracking pages.
globalThis.receiptsPlan = () => result; globalThis.calls = []
const cachedVisible = ref(true), cachedRefresh = ref(0)
const cachedSession = ref({ principal: { id: 'alice' }, permissions: ['company_knowledge.review'] })
const cachedScope = ref('company'), cachedFilters = ref({ ...persisted })
const cachedPending = []
const cacheRenderer = createRenderer({
  createElement: () => ({ children: [] }), createComment: () => ({}),
  insert(node, parent, anchor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    parent.children ||= []
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node); node.parent = parent
  },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1],
})
const cacheApp = cacheRenderer.createApp({ render() { return h(KeepAlive, null, { default: () => cachedVisible.value ? h(liveComponent, {
  key: 'receipts', session: cachedSession.value, scope: cachedScope.value, state: cachedFilters.value, refreshKey: cachedRefresh.value,
  onPending: value => cachedPending.push(value),
}) : null }) } })
const flushCache = async () => { await nextTick(); await new Promise(setImmediate) }
cacheApp.mount({ children: [] })
try {
  await flushCache()
  assert.equal(globalThis.calls.length, 1, 'initial mount and initial activation share one receipt request')
  cachedVisible.value = false; await flushCache()
  const beforeReturn = globalThis.calls.length
  const beforeReturnRoles = globalThis.roleCalls
  globalThis.rolesPlan = () => ({ items: [
    { role_id: 'role-ai', department: '总经办', name: 'AI架构师' },
    { role_id: 'role-new', department: '总经办', name: '新确认岗位' },
  ] })
  let finishReturn
  globalThis.receiptsPlan = () => new Promise(resolve => { finishReturn = resolve })
  cachedVisible.value = true; await flushCache()
  assert.equal(globalThis.calls.length, beforeReturn + 1, 'returning to a cached receipt page refreshes once in the background')
  assert.equal(liveState.items.value[0].receipt_id, record.receipt_id, 'returning retains the previously loaded rows while refreshing')
  assert.equal(liveState.total.value, 41); assert.equal(liveState.loading.value, true)
  assert.equal(globalThis.roleCalls, beforeReturnRoles + 1, 'returning refreshes an already populated role directory')
  assert(liveState.roles.value.some(role => role.role_id === 'role-new'), 'a role created on the attention page becomes available after returning')
  delete globalThis.rolesPlan
  finishReturn({ ...result, items: [{ ...record, receipt_id: 'b'.repeat(32) }] }); await flushCache()
  assert.equal(liveState.items.value[0].receipt_id, 'b'.repeat(32))

  let finishHidden
  globalThis.receiptsPlan = () => new Promise(resolve => { finishHidden = resolve })
  cachedRefresh.value++; await flushCache()
  const hiddenSignal = globalThis.calls.at(-1)[4]
  cachedVisible.value = false; await flushCache()
  assert.equal(hiddenSignal.aborted, true, 'leaving a cached page cancels its in-flight receipt request')
  assert.equal(liveState.loading.value, false)
  assert.equal(liveState.items.value[0].receipt_id, 'b'.repeat(32), 'deactivation preserves the last completed result')
  assert.equal(liveState.total.value, 41)
  assert.equal(cachedPending.at(-1), false, 'hidden receipts stop requesting background polling')
  finishHidden({ ...result, items: [{ ...record, receipt_id: 'c'.repeat(32) }], receipt_counts: { ...result.receipt_counts, pending_verification: 1 } })
  await flushCache()
  assert.equal(liveState.items.value[0].receipt_id, 'b'.repeat(32), 'a response that ignores abort cannot replace cached rows')
  assert.equal(cachedPending.at(-1), false, 'a late hidden response cannot restore polling')

  const beforeHiddenFilter = globalThis.calls.length
  cachedFilters.value.q = '历史导航恢复'; await flushCache()
  assert.equal(globalThis.calls.length, beforeHiddenFilter, 'a shared filter change cannot fetch while the receipt page is hidden')
  assert.deepEqual(liveState.items.value, [], 'a changed filter invalidates its previous rows even while hidden')
  cachedSession.value.permissions = []; await flushCache()
  assert.deepEqual(liveState.roles.value, [])
  const beforeHiddenRoles = globalThis.roleCalls
  cachedSession.value.permissions = ['company_knowledge.review']; await flushCache()
  assert.equal(globalThis.calls.length, beforeHiddenFilter, 'restoring access while hidden waits for activation')
  assert.equal(globalThis.roleCalls, beforeHiddenRoles, 'role reads also wait for activation')
  globalThis.receiptsPlan = () => result
  cachedVisible.value = true; await flushCache()
  assert.equal(globalThis.calls.length, beforeHiddenFilter + 1, 'activation reads the latest saved filter exactly once')
  assert.equal(globalThis.calls.at(-1)[5].q, '历史导航恢复')
  assert.equal(liveState.roles.value[0].role_id, 'role-ai', 'activation restores an invalidated role directory')

  cachedVisible.value = false; await flushCache()
  cachedSession.value.permissions = []; await flushCache()
  assert.deepEqual(liveState.items.value, [], 'permission loss clears populated cached rows before returning')
  assert.equal(liveState.counts.value, null); assert.equal(liveState.total.value, null)
  const beforeDeniedReturn = globalThis.calls.length
  cachedVisible.value = true; await flushCache()
  assert.equal(globalThis.calls.length, beforeDeniedReturn, 'activation cannot read without permission')
  assert.match(liveState.error.value, /无权/)

  cachedSession.value.permissions = ['company_knowledge.review']; await flushCache()
  assert.equal(liveState.items.value[0].receipt_id, record.receipt_id)
  cachedVisible.value = false; await flushCache()
  cachedSession.value = { principal: { id: 'bob' }, permissions: ['company_knowledge.submissions.read'] }
  cachedScope.value = 'self'
  let finishNewIdentity
  globalThis.receiptsPlan = () => new Promise(resolve => { finishNewIdentity = resolve })
  const beforeNewIdentity = globalThis.calls.length
  cachedVisible.value = true; await flushCache()
  assert.deepEqual(liveState.items.value, [], 'returning with a new identity and scope never exposes previous rows')
  assert.equal(liveState.total.value, null)
  assert.equal(globalThis.calls.length, beforeNewIdentity + 1, 'updated props and activation share one read')
  assert.equal(globalThis.calls.at(-1)[0], 'self')
  finishNewIdentity(result); await flushCache()
} finally { cacheApp.unmount() }
console.log('Receipt KeepAlive: return refresh deduplicated, cached rows retained, hidden reads paused, late responses ignored and identity/scope/permission isolation passed.')
