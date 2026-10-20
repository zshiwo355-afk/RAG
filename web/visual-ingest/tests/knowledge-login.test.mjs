import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'
import ts from 'typescript'

const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const apiURL = moduleURL(transpile(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8')))
const api = await import(apiURL)
globalThis.window = { location: { hash: '' }, confirm: () => true, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} }
const destinations = []
globalThis.document = { title: '', visibilityState: 'visible', addEventListener() {}, removeEventListener() {}, getElementById(id) { return { scrollIntoView() {}, focus() { destinations.push(id) } } } }
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true }, configurable: true })
const readOnlySession = { authenticated: true, principal: { id: 'synthetic-admin' }, permissions: ['company_knowledge.review'], capabilities: { upload: false, review_write: false } }
let sent
globalThis.fetch = async (path, options) => { sent = { path, ...options }; return { ok: true, status: 200, json: async () => readOnlySession } }
assert.deepEqual(await api.createSession('admin', ' synthetic-password '), readOnlySession)
assert.equal(sent.path, '/portal/knowledge/session')
assert.equal(sent.method, 'POST')
assert.equal(sent.credentials, 'same-origin')
assert.equal(sent.cache, 'no-store')
assert.equal(sent.headers['X-Knowledge-Portal-Request'], '1')
assert.deepEqual(JSON.parse(sent.body), { username: 'admin', password: ' synthetic-password ' })
globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({ error: { code: 'invalid_credentials' } }) })
await assert.rejects(api.createSession('admin', 'wrong-password'), error => error instanceof api.PortalError && error.status === 401)

const stubURL = moduleURL('export default {}')
const portalURL = moduleURL(`
  export { processingBlockers, processingReasonLabels, processingEventLabels, PortalError, materialTitle, uploaderPosition, processingStatusLabels, processingStatusName, processingItemStatusName, processingStageName, processingEventName, processingReasonName } from '${apiURL}';
  import { PortalError } from '${apiURL}';
  export async function getSession() { globalThis.portalReads.session++; if (globalThis.loginPlan.session) return globalThis.loginPlan.session(); throw new PortalError(401, 'unauthenticated') }
  export async function createSession(username, password) { globalThis.loginPlan.calls.push({ username, password }); return globalThis.loginPlan.create() }
  export async function deleteSession() { if(globalThis.loginPlan.logoutError) throw globalThis.loginPlan.logoutError }
  export async function getOverview() { globalThis.portalReads.overview++; return globalThis.loginPlan.overview ? globalThis.loginPlan.overview() : { counts: {} } }
  export async function getJobs(...args) { globalThis.portalReads.jobs++; globalThis.portalJobArgs = args; return globalThis.loginPlan.jobs ? globalThis.loginPlan.jobs(...args) : { items: [], total: 100 } }
  export async function getRoles() { return { items: [] } }
  export async function getDraft() {}
  export async function getJob(id) { return globalThis.loginPlan.job ? globalThis.loginPlan.job(id) : { job_id: id, status: 'needs_review', items: [], counts: {}, item_total: 0, event_total: 0 } }
  export async function retryJob() {}
  export async function resolveItem(...args) { return globalThis.loginPlan.resolve?.(...args) }
`)
const source = await readFile(new URL('../src/views/KnowledgePortal.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
assert.match(descriptor.template.content, /id="knowledge-username"[^>]*autocomplete="username"/)
assert.match(descriptor.template.content, /id="knowledge-password"[^>]*type="password"[^>]*autocomplete="current-password"/)
assert.doesNotMatch(source, /123456|knowledge-credential|localStorage|sessionStorage/)
const code = transpile(compileScript(descriptor, { id: 'login-check' }).content)
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${portalURL}'`)
  .replace(/from (['"])\.\.\/components\/[^'"]+\.vue\1/g, `from '${stubURL}'`)
  .replace(/import ['"]\.\.\/knowledge-portal.css['"];?/, '')
const component = (await import(moduleURL(code))).default
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
let state
const app = renderer.createApp({ ...component, setup(props, context) { state = component.setup(props, context); return state }, render() { return null } })
globalThis.portalReads = { overview: 0, jobs: 0, session: 0 }
globalThis.loginPlan = { calls: [], create: async () => readOnlySession }
app.mount({})
try {
  await nextTick()
  await state.login()
  assert.equal(globalThis.loginPlan.calls.length, 0, 'empty fields do not submit')
  state.username.value = '  admin  '; state.password.value = ' synthetic-password '
  let finish
  globalThis.loginPlan.create = () => new Promise(resolve => { finish = resolve })
  const pending = state.login()
  assert.equal(state.password.value, '', 'password clears as soon as submission starts')
  assert.equal(state.signingIn.value, true)
  state.password.value = 'ignored'; await state.login()
  assert.equal(globalThis.loginPlan.calls.length, 1, 'repeated submission stays blocked')
  assert.deepEqual(globalThis.loginPlan.calls[0], { username: 'admin', password: ' synthetic-password ' })
  finish(readOnlySession); await pending; await nextTick(); await new Promise(setImmediate)
  assert.deepEqual(state.visibleViews.value.map(view => view.label), ['岗位知识', '知识图谱', '上传与异常'], 'tracking pages share the original sidebar entry')
  assert.equal(state.activeNavigationView.value, 'overview')
  assert.equal(globalThis.portalReads.overview, 1, 'login must request overview only once')
  assert.equal(globalThis.portalReads.jobs, 0, 'role page must not request hidden processing jobs')
  globalThis.loginPlan.session = async () => readOnlySession
  const initialReads = { ...globalThis.portalReads }
  await state.periodicRefresh()
  assert.equal(globalThis.portalReads.session, initialReads.session + 1, 'stable pages still verify session and permission changes')
  assert.equal(globalThis.portalReads.overview, initialReads.overview, 'stable pages do not reload statistics every 30 seconds')
  document.visibilityState = 'hidden'
  await state.periodicRefresh()
  assert.equal(globalThis.portalReads.session, initialReads.session + 1, 'hidden tabs do not poll')
  document.visibilityState = 'visible'
  state.activeView.value = 'attention'; await nextTick(); await new Promise(setImmediate)
  assert.equal(state.activeNavigationView.value, 'receipts', 'attention stays inside the upload and issues section')
  assert.equal(globalThis.portalReads.jobs, 1, 'processing view loads jobs only on entry')
  const processingReads = { ...globalThis.portalReads }
  state.receiptPending.value = true
  await state.periodicRefresh()
  assert.equal(globalThis.portalReads.jobs, processingReads.jobs + 1, 'verification in progress keeps the 30 second data check')
  state.receiptPending.value = false
  const idleReads = globalThis.portalReads.jobs
  await state.periodicRefresh()
  assert.equal(globalThis.portalReads.jobs, idleReads, 'idle processing stops fast polling')
  const originalNow = Date.now
  try {
    Date.now = () => originalNow() + 300_001
    await state.periodicRefresh()
    assert.equal(globalThis.portalReads.jobs, idleReads + 1, 'stable data updates at five minutes')
  } finally { Date.now = originalNow }
  let finishOverview
  globalThis.loginPlan.overview = () => new Promise(resolve => { finishOverview = resolve })
  const beforeConcurrent = { ...globalThis.portalReads }
  const oneRefresh = state.refresh(), twoRefresh = state.refresh()
  assert.equal(globalThis.portalReads.overview, beforeConcurrent.overview + 1, 'overlapping refreshes share one request')
  assert.equal(globalThis.portalReads.jobs, beforeConcurrent.jobs + 1)
  finishOverview({ counts: {} }); await Promise.all([oneRefresh, twoRefresh])
  delete globalThis.loginPlan.overview
  state.jobFilters.q = '需要附件'; state.status.value = 'needs_review'; state.filterJobs()
  await nextTick(); await new Promise(setImmediate)
  state.changePage(3); await nextTick(); await new Promise(setImmediate)
  const attentionURL = window.location.hash
  assert.deepEqual(globalThis.portalJobArgs.slice(0, 4), ['company', 'attention', 3, 20])
  assert.equal(globalThis.portalJobArgs[5].q, '需要附件')
  assert.equal(globalThis.portalJobArgs[5].processing_status, 'needs_review')
  await state.openMetric('pending_publication'); await new Promise(setImmediate)
  assert.equal(state.activeNavigationView.value, 'receipts', 'pending publication shares the same sidebar highlight')
  assert.deepEqual(globalThis.portalJobArgs.slice(0, 4), ['company', 'pending_publication', 1, 20])
  assert.equal(state.status.value, '', 'each independent page starts with its own filters')
  state.jobFilters.q = '草稿'; state.filterJobs(); await nextTick()
  await state.openMetric('attention'); await new Promise(setImmediate)
  assert.equal(state.status.value, 'needs_review'); assert.equal(state.page.value, 3)
  assert.equal(state.jobFilters.q, '需要附件', 'switching pages restores their own filters')
  const jobsBeforeReceipts = globalThis.portalReads.jobs
  await state.openMetric('receipts'); await new Promise(setImmediate)
  assert.equal(state.activeView.value, 'receipts')
  assert.equal(state.activeNavigationView.value, 'receipts')
  assert.equal(globalThis.portalReads.jobs, jobsBeforeReceipts, 'receipts must not also fetch the hidden job table')
  state.updateReceiptState({ ...state.pageStates.receipts, status: 'verified', page: 2, q: '流程' })
  const receiptURL = window.location.hash
  assert.equal(state.readLocation().states.receipts.page, 2, 'reload reconstructs active page pagination from the URL')
  assert.equal(state.readLocation().states.attention.q, '需要附件', 'reload also retains other page filters')
  window.location.hash = attentionURL; state.hashChanged(); await nextTick(); await new Promise(setImmediate)
  assert.equal(state.activeView.value, 'attention'); assert.equal(state.page.value, 3)
  window.location.hash = receiptURL; state.hashChanged(); await nextTick()
  assert.equal(state.activeView.value, 'receipts'); assert.equal(state.pageStates.receipts.status, 'verified')
  window.location.hash = '#processing'; state.hashChanged(); await nextTick()
  assert.equal(state.activeView.value, 'receipts', 'old processing links open the full receipt history')
  state.pageStates.receipts.scope = 'company'; await nextTick()
  await state.openJob('a'.repeat(32)); await nextTick()
  assert.equal(state.readLocation().job, 'a'.repeat(32), 'details are directly addressable')
  state.roleDirty('item-1', true); window.confirm = () => false
  await state.navigate('attention')
  assert.equal(state.activeView.value, 'receipts', 'unsaved role choices can keep their current page')
  window.confirm = () => true
  state.pageStates.attention.scope = 'company'
  await state.navigate('attention', true); await nextTick()
  state.pageStates.receipts.scope = 'self'
  await state.navigate('receipts', true); await nextTick()
  assert.equal(state.detailOpen.value, true, 'tracking the same job preserves the detail')
  assert.equal(state.jobsScope.value, 'company', 'tracking follows the current task visibility scope')
  assert.equal(state.roleDirtyItems.value.has('item-1'), true, 'keeping the same form keeps its unsaved-choice guard')
  state.roleDirty('item-1', false)
  state.detailVisibilityChanged(false)
  assert.equal(state.readLocation().job, '', 'closing details removes the task URL after the drawer model changes')
  let browsedRole
  state.dashboard.value = { browseRole(id) { browsedRole = id } }
  await state.openMetric('published')
  assert.equal(browsedRole, '')
  assert.equal(state.activeView.value, 'overview')
  assert.equal(window.location.hash.split('?')[0], '#overview')
  assert.equal(destinations.at(-1), 'knowledge-main')
  state.session.value = { ...readOnlySession, permissions: ['company_knowledge.dashboard.read', 'company_knowledge.submissions.read'] }
  await nextTick(); await new Promise(setImmediate)
  assert.equal(state.canReadSummaryDetails.value, false, 'company summary access must not turn into personal details')
  const beforeDeniedJump = destinations.length, beforeDeniedRequests = globalThis.portalReads.jobs
  for (const target of ['published', 'receipts', 'attention', 'pending_publication']) await state.openMetric(target)
  assert.equal(destinations.length, beforeDeniedJump)
  assert.equal(globalThis.portalReads.jobs, beforeDeniedRequests)
  state.scope.value = 'self'; await nextTick()
  assert.equal(state.canReadSummaryDetails.value, true)
  state.session.value = readOnlySession; state.scope.value = 'company'; state.status.value = ''
  await nextTick(); await new Promise(setImmediate)
  state.activeView.value = 'graph'; await nextTick(); await new Promise(setImmediate)
  const graphReads = { ...globalThis.portalReads }
  const beforeGraphVisible = state.refreshKey.value
  await state.visibleRefresh()
  assert.equal(state.refreshKey.value, beforeGraphVisible, 'graph visibility restoration leaves the graph component as the single read owner')
  assert.equal(globalThis.portalReads.session, graphReads.session + 1, 'graph visibility still revalidates session access')
  await state.refresh()
  assert.equal(state.refreshKey.value, beforeGraphVisible + 1, 'manual refresh still forces the graph child to read')
  assert.equal(globalThis.portalReads.overview, graphReads.overview, 'graph view does not fetch hidden statistics')
  assert.equal(globalThis.portalReads.jobs, graphReads.jobs, 'graph view does not fetch hidden jobs')
  state.graphPending.value = true
  const beforeGraphPeriodic = state.refreshKey.value
  await state.periodicRefresh()
  assert.equal(state.refreshKey.value, beforeGraphPeriodic, 'the parent 30-second check does not duplicate the graph component building poll')
  const graphNow = Date.now
  try {
    Date.now = () => graphNow() + 300_001
    state.graphPending.value = false
    await state.periodicRefresh()
    assert.equal(state.refreshKey.value, beforeGraphPeriodic + 1, 'ready graphs retain the parent five-minute revalidation')
  } finally { Date.now = graphNow }
  state.activeView.value = 'overview'; await nextTick(); await new Promise(setImmediate)
  const beforeOverviewVisible = state.refreshKey.value
  await state.visibleRefresh()
  assert.equal(state.refreshKey.value, beforeOverviewVisible + 1, 'other views still refresh children when becoming visible')
  assert.equal(state.scope.value, 'company')
  globalThis.loginPlan.session = async () => ({ ...readOnlySession, permissions: ['company_knowledge.submissions.read'] })
  const previousGeneration = state.sessionGeneration
  await state.checkAccess(); await nextTick(); await new Promise(setImmediate)
  assert(state.sessionGeneration > previousGeneration, 'revoked company access invalidates old privileged requests')
  assert.equal(state.scope.value, 'self', 'revoked company access falls back to personal scope')
  assert.notEqual(state.overview.value, null, 'scope normalization must not invalidate its own new response')
  window.location.hash = '#receipts?job=' + 'b'.repeat(32); await state.hashChanged(); await nextTick()
  assert.equal(state.selectedId.value, 'b'.repeat(32), 'employees with submissions.read retain task deep links')
  assert.equal(state.detailOpen.value, true)
  globalThis.loginPlan.session = async () => readOnlySession
  assert.equal(state.canReviewWrite.value, false)
  assert.equal(state.visibleViews.value.some(view => view.key === 'upload'), false)
  state.activeView.value = 'upload'; await nextTick()
  assert.equal(state.activeView.value, 'overview', 'read-only accounts cannot stay at the old upload link')
  assert.equal(window.location.hash.split('?')[0], '#overview')
  state.session.value = { ...readOnlySession, capabilities: { upload: true, review_write: false } }
  state.activeView.value = 'upload'; await nextTick()
  assert.equal(state.activeView.value, 'upload', 'employee upload access remains available')
  assert.equal(state.visibleViews.value.some(view => view.key === 'upload'), true)
  // Navigation must keep the already authorized summary and same-query rows while refreshing.
  state.session.value = readOnlySession
  for (const value of Object.values(state.pageStates)) value.scope = 'company'
  globalThis.loginPlan.overview = async () => ({ counts: { receipts: 6, needs_review: 3 } })
  globalThis.loginPlan.jobs = async () => ({ items: [{ job_id: 'retained-attention' }], total: 1 })
  await state.navigate('attention'); await new Promise(setImmediate)
  const savedOverview = state.overview.value
  const savedJobs = state.jobs.value
  let finishNavigationOverview, finishNavigationJobs
  globalThis.loginPlan.overview = () => new Promise(resolve => { finishNavigationOverview = resolve })
  globalThis.loginPlan.jobs = () => new Promise(resolve => { finishNavigationJobs = resolve })
  await state.navigate('receipts')
  assert.equal(state.overview.value, savedOverview, 'same-scope navigation must not blank summary metrics')
  assert.equal(state.jobs.value, savedJobs, 'leaving attention retains its last result for return')
  finishNavigationOverview(savedOverview); await new Promise(setImmediate)
  await state.navigate('attention')
  assert.equal(state.overview.value, savedOverview)
  assert.equal(state.jobs.value, savedJobs, 'returning to the same task query keeps rows visible during refresh')
  finishNavigationOverview(savedOverview)
  finishNavigationJobs({ items: [{ job_id: 'refreshed-attention' }], total: 1 }); await new Promise(setImmediate)
  assert.equal(state.jobs.value[0].job_id, 'refreshed-attention')
  await state.navigate('pending_publication')
  assert.equal(state.jobs.value.length, 0, 'different task group cannot display cached attention rows')
  finishNavigationOverview(savedOverview)
  finishNavigationJobs({ items: [{ job_id: 'pending-only' }], total: 1 }); await new Promise(setImmediate)
  state.scope.value = 'self'; await nextTick()
  assert.equal(state.overview.value, null, 'changing visibility scope immediately clears old summary')
  assert.equal(state.jobs.value.length, 0, 'changing visibility scope immediately clears old tasks')
  const finishOldScopeOverview = finishNavigationOverview, finishOldScopeJobs = finishNavigationJobs
  state.scope.value = 'company'; await nextTick()
  finishOldScopeOverview({ counts: { receipts: 999 } })
  finishOldScopeJobs({ items: [{ job_id: 'late-self' }], total: 1 }); await new Promise(setImmediate)
  assert.equal(state.overview.value, null, 'late response for prior scope cannot restore summary')
  assert.equal(state.jobs.value.length, 0, 'late response for prior scope cannot restore rows')
  finishNavigationOverview(savedOverview)
  finishNavigationJobs({ items: [{ job_id: 'current-company' }], total: 1 }); await new Promise(setImmediate)
  state.session.value = { ...readOnlySession, capabilities: { ...readOnlySession.capabilities, review_write: true } }; await nextTick()
  assert.equal(state.overview.value, null, 'permission change invalidates even a same-scope retained summary')
  assert.equal(state.jobs.value.length, 0, 'permission change clears retained task rows')
  finishNavigationOverview(savedOverview)
  finishNavigationJobs({ items: [], total: 0 }); await new Promise(setImmediate)
  delete globalThis.loginPlan.overview; delete globalThis.loginPlan.jobs

  const reviewItem = { item_id: 'c'.repeat(32), status: 'needs_review', version: 4, title: '交接资料',
    reason_codes: ['missing_attachment'], current_reason_codes: ['missing_attachment'], source_path: '交接.md', has_preview: false }
  const reviewJob = { job_id: 'd'.repeat(32), receipt_id: 'e'.repeat(32), source_id: 'original-source',
    status: 'needs_review', items: [reviewItem], counts: { needs_review: 1 }, item_total: 1, event_total: 0 }
  state.selectedId.value = reviewJob.job_id; state.selectedJob.value = reviewJob
  const writableSession = state.session.value
  state.session.value = { ...writableSession, capabilities: { ...writableSession.capabilities, review_write: false } }
  assert.equal(state.canResolveItem(reviewItem), false, 'read-only review access cannot archive or link receipts')
  state.session.value = writableSession
  for (const status of ['queued', 'running']) {
    state.selectedJob.value = { ...reviewJob, status }
    assert.equal(state.canResolveItem(reviewItem), false, 'active jobs cannot be resolved concurrently')
  }
  state.selectedJob.value = reviewJob
  state.beginResolution(reviewItem)
  assert.equal(state.resolutionAction.value, 'archive', 'an unresolved item defaults to archiving, without demanding a replacement upload')
  assert.equal(state.validResolution.value, false, 'archiving still requires a reason')
  state.resolutionReason.value = '此资料暂不具备复用价值，保留原件及记录'
  assert.equal(state.validResolution.value, true)
  let resolutionSent
  globalThis.loginPlan.resolve = async (...args) => { resolutionSent = args }
  globalThis.loginPlan.job = async () => ({ ...reviewJob, status: 'completed', items: [{ ...reviewItem, status: 'archived' }] })
  await state.submitResolution(reviewItem)
  assert.deepEqual(resolutionSent, [reviewJob.job_id, reviewItem.item_id, { expected_version: 4, action: 'archive', reason: '此资料暂不具备复用价值，保留原件及记录' }])
  assert.match(state.actionNotice.value, /归档.*移出待处理.*原件.*记录.*正式知识/)
  assert.doesNotMatch(state.actionNotice.value, /已发布|入库成功/)
  for (const status of ['published', 'archived', 'draft', 'indexing', 'duplicate']) {
    assert.equal(state.canResolveItem({ ...reviewItem, status }), false)
  }
  state.selectedJob.value = reviewJob; state.selectedId.value = reviewJob.job_id
  state.beginResolution(reviewItem)
  state.resolutionAction.value = 'replace'; state.resolutionReason.value = '补齐引用文档'
  assert.equal(state.validResolution.value, false, 'linking a replacement still requires its verified receipt ID')
  state.replacementReceipt.value = 'f'.repeat(32)
  await state.submitResolution(reviewItem)
  assert.deepEqual(resolutionSent, [reviewJob.job_id, reviewItem.item_id, { expected_version: 4, action: 'replace', reason: '补齐引用文档', replacement_receipt_id: 'f'.repeat(32) }])
  assert.match(state.actionNotice.value, /关联补充收件.*不代表发布完成/)
  assert.match(descriptor.template.content, /归档并移出待处理/)
  assert.match(descriptor.template.content, /关联补充收件/)
  assert.match(descriptor.template.content, /不会删除原件.*正式知识/)
  const confirmedRole = { role_id: 'role-hrbp', department: '人力资源部', name: '招聘主管 / HRBP' }
  const missingItem = { ...reviewItem, role_assignment: { role: confirmedRole }, missing_references: ['附录/参数表.md', 'scripts/prepare.py'],
    reason_codes: ['uploader_role_not_found', 'missing_attachment'], current_reason_codes: ['missing_attachment'] }
  state.selectedJob.value = { ...reviewJob, items: [missingItem] }; state.selectedId.value = reviewJob.job_id
  const requirements = state.repairRequirements(missingItem)
  assert.deepEqual(JSON.parse(requirements.match(/\n(\{[\s\S]*?\n\})\n/)[1]).confirmed_role, confirmedRole,
    'copied repair instructions preserve the confirmed department and role exactly')
  assert.equal(JSON.parse(state.repairRequirements({ ...reviewItem, title: '财务主管交接资料' }).match(/\n(\{[\s\S]*?\n\})\n/)[1]).confirmed_role, null,
    'a title never becomes an inferred confirmed role')
  assert.match(requirements, /get_company_knowledge_upload_status/)
  assert(requirements.includes(reviewJob.receipt_id) && requirements.includes(reviewItem.item_id))
  assert(requirements.includes('附录/参数表.md') && requirements.includes('scripts/prepare.py'))
  assert.match(requirements, /名称.*数据.*不.*指令/)
  assert.match(requirements, /原提交人.*MCP/)
  assert.match(requirements, /自行查找.*自检/)
  assert.match(requirements, /新收件编号/)
  assert.match(requirements, /基线版本.*原提交记录.*当前正式资料.*核实/)
  assert.match(requirements, /无法确认.*暂停重传.*不猜测.*处理版本/)
  assert.match(requirements, /已有.*归属.*照实.*原提交清单.*不.*标题.*猜.*岗位/)
  assert.doesNotMatch(requirements, /未找到对应岗位|主动唤醒|已自动通知/)
  assert.match(state.repairRequirements(reviewItem), /未记录具体.*名称/)
  assert.deepEqual(state.itemBlockers(missingItem).map(value => value.code), ['missing_attachment'])
  assert.match(state.itemBlockers(missingItem)[0].action, /文档和附件/)
  assert.deepEqual(state.itemBlockers({ ...missingItem, rechecking: true }), [], 'rechecking does not repeat obsolete repair guidance')
  let clipboardText
  navigator.clipboard = { writeText: async text => { clipboardText = text } }
  const beforeCopy = { ...globalThis.portalReads }
  await state.copyRepairRequirements(missingItem)
  assert.equal(clipboardText, requirements)
  assert.deepEqual(globalThis.portalReads, beforeCopy, 'copying instructions does not call MCP or resend business data')
  assert.match(state.actionNotice.value, /已复制.*原.*AI/)
  navigator.clipboard.writeText = async () => { throw new Error('clipboard blocked') }
  await state.copyRepairRequirements(missingItem)
  assert.match(state.actionError.value, /手动复制/)
  assert.equal(state.repairRequirements(missingItem), requirements, 'clipboard failure leaves the complete text available for manual copying')
  let finishCopy
  navigator.clipboard.writeText = () => new Promise(resolve => { finishCopy = resolve })
  const lateCopy = state.copyRepairRequirements(missingItem)
  state.clearDetail(); finishCopy(); await lateCopy
  assert.equal(state.actionNotice.value, '', 'late clipboard completion does not write notices into another task')
  assert.match(descriptor.template.content, /复制补充要求/)
  assert.match(descriptor.template.content, /missing_references/)
  assert.doesNotMatch(descriptor.template.content, /卡在：|卡点/)
  delete globalThis.loginPlan.job; delete globalThis.loginPlan.resolve

  let finishLateOverview
  globalThis.loginPlan.overview = () => new Promise(resolve => { finishLateOverview = resolve })
  const lateOverview = state.loadOverview()
  await state.logout()
  finishLateOverview({ counts: { receipts: 999 } }); await lateOverview
  delete globalThis.loginPlan.overview
  assert.equal(state.session.value, null)
  assert.equal(state.overview.value, null)
  assert.equal(state.username.value, '')
  assert.equal(state.password.value, '')

  state.username.value = 'admin'; state.password.value = 'wrong-password'
  globalThis.loginPlan.create = async () => { throw new api.PortalError(401, 'invalid_credentials') }
  await state.login()
  assert.match(state.loginError.value, /账号或密码不正确/)
  assert.equal(state.password.value, '')
  assert.equal(state.signingIn.value, false)
  assert.equal(state.session.value, null)
  globalThis.loginPlan.logoutError = new Error('network unavailable')
  await state.logout()
  assert.equal(state.logoutUnconfirmed.value, true)
  const calls = globalThis.loginPlan.calls.length
  state.username.value = 'admin'; state.password.value = 'synthetic-password'; await state.login()
  assert.equal(globalThis.loginPlan.calls.length, calls, 'unconfirmed logout blocks a new login')
} finally { app.unmount() }
console.log('Knowledge refresh: view-only reads, deduplication, fast/idle polling, hidden tabs, session checks and late logout response passed.');
console.log('Knowledge login: account/password request, cookie/CSRF, input validation, password clearing, read-only navigation, employee upload access, errors and logout passed.')
console.log('Independent tracking pages: URL filter and pagination restoration, legacy links, task location, unsaved choice guard and scope permissions passed.')
console.log('Item resolution: archive by default with retained records, gated writes, replacement receipt contract, current repair guidance and filenames, clipboard fallback and late-result protection passed.')
