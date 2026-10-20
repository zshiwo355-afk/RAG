import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const apiURL = moduleURL(`
  export class PortalError extends Error { constructor(status) { super('不可读取'); this.status = status } }
  export async function searchKnowledge(query, signal, filters) { return globalThis.searchPlan(query, signal, filters) }
  export async function getRoles() { return globalThis.rolesPlan ? globalThis.rolesPlan() : { items: [] } }
  export async function getCatalog(filters) { return globalThis.catalogPlan ? globalThis.catalogPlan(filters) : { items: [], total: 0, offset: 0 } }
  export async function getKnowledgeFile(id, fileId, disposition) { return globalThis.filePlan(id, fileId, disposition) }
  export function safeFileUrl(value) { return value }
  export async function getGraph() { globalThis.graphCalls++; return globalThis.graphPlan ? globalThis.graphPlan() : { nodes: [], edges: [], status: 'ready' } }
  export function knowledgeKindName(value) { return value === 'skill' ? '智能体技能' : '岗位流程' }
  export async function getPublishedKnowledge(id) { return globalThis.readingPlan(id) }
`)
const stubURL = moduleURL('export default {}')
const source = await readFile(new URL('../src/components/KnowledgeDashboard.vue', import.meta.url), 'utf8')
const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'reading-check' }).content, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
  .replace(/from (['"])\.\/(KnowledgeGraph|KnowledgeMarkdown).vue\1/g, `from '${stubURL}'`)
  .replace(/import ['"]\.\.\/knowledge-dashboard.css['"];?/, '')
const component = (await import(moduleURL(code))).default
const { PortalError } = await import(apiURL)
const props = {
  view: 'overview', refreshKey: 0, refreshing: false, overview: null, overviewError: '',
  session: { permissions: ['company_knowledge.read'], principal: { id: 'local' } },
}
const document = (id, revision = 1) => ({ knowledge_id: id, title: id, revision, content: `正文 ${revision}` })
let state
await renderToString(createSSRApp({ ...component, setup(props, context) {
  state = component.setup(props, context); return state
}, render() { return null } }, props))

globalThis.readingPlan = id => document(id)
globalThis.graphCalls = 0
await state.openKnowledge('first')
assert.equal(globalThis.graphCalls, 0, 'opening a document must not fetch the entire graph')
await state.loadReadingLinks()
assert.equal(globalThis.graphCalls, 1, 'relations load only when explicitly opened')
let graphDone
const graphBefore = globalThis.graphCalls
globalThis.graphPlan = () => new Promise(resolve => { graphDone = resolve })
const graphOne = state.readGraph(), graphTwo = state.readGraph()
assert.equal(globalThis.graphCalls, graphBefore + 1, 'simultaneous graph readers share one request')
graphDone({ nodes: [], edges: [], status: 'ready' }); await Promise.all([graphOne, graphTwo])
delete globalThis.graphPlan
state.directoryOpen.value = false
let revalidationCalls = 0
globalThis.readingPlan = id => { revalidationCalls++; return document(id) }
const unchangedBody = state.reading.value
state.refreshLibrary(); await new Promise(setImmediate)
assert.equal(state.reading.value, unchangedBody, 'same-version refresh preserves the current body object')
assert.equal(revalidationCalls, 1, 'refresh still checks withdrawal and permission on the open document')
assert.equal(globalThis.graphCalls, graphBefore + 1, 'body revalidation does not fetch the graph again')
// An overlapping body refresh must not strand an in-flight relation request.
state.clearReadingLinks()
const realNow = Date.now
let finishLinks
try {
  Date.now = () => realNow() + 31_000
  globalThis.graphPlan = () => new Promise(resolve => { finishLinks = resolve })
  const linksPending = state.loadReadingLinks()
  assert.equal(state.readingLinksLoading.value, true)
  await state.openKnowledge('first')
  finishLinks({ nodes: [], edges: [], status: 'ready' }); await linksPending
  assert.equal(state.readingLinksLoading.value, false, 'same-version body refresh cannot strand relations loading')
  assert.equal(state.readingLinksOpen.value, true)
} finally { Date.now = realNow; delete globalThis.graphPlan }
const original = state.reading.value
let resolve
globalThis.readingPlan = () => new Promise(done => { resolve = done })
const refreshing = state.openKnowledge('first')
assert.equal(state.reading.value, original, 'same document stays rendered during refresh')
assert.equal(state.readingLoading.value, true)
resolve(document('first', 2)); await refreshing
assert.equal(state.reading.value.revision, 2)
assert.equal(state.readingLinksOpen.value, false, 'a new revision offers to reload its relations instead of showing stale empty links')
assert.equal(state.readingLoading.value, false)
// A late graph from the old revision cannot repopulate the new revision cache.
let finishOldGraph, finishNewGraph
const callsBeforeRevision = globalThis.graphCalls
globalThis.graphPlan = () => new Promise(resolve => { finishOldGraph = resolve })
const oldGraph = state.loadReadingLinks()
globalThis.readingPlan = id => document(id, 3)
await state.openKnowledge('first')
finishOldGraph({ nodes: [], edges: [], status: 'ready' }); await oldGraph
assert.equal(state.readingLinksOpen.value, false)
globalThis.graphPlan = () => new Promise(resolve => { finishNewGraph = resolve })
const newGraph = state.loadReadingLinks()
assert.equal(globalThis.graphCalls, callsBeforeRevision + 2, 'old revision graph must not be cached after a version change')
finishNewGraph({ nodes: [], edges: [], status: 'ready' }); await newGraph
delete globalThis.graphPlan
globalThis.readingPlan = () => new Promise(done => { resolve = done })

const switching = state.openKnowledge('second')
assert.equal(state.reading.value, null, 'old body must clear when selecting another document')
resolve(document('second')); await switching
for (const status of [401, 403, 404]) {
  globalThis.readingPlan = () => { throw new PortalError(status) }
  await state.openKnowledge('second')
  assert.equal(state.reading.value, null, 'removed or unauthorized body must not remain visible')
  assert(state.readingError.value)
}

const pending = new Map()
globalThis.readingPlan = id => new Promise(done => pending.set(id, done))
const oldRequest = state.openKnowledge('old')
const newRequest = state.openKnowledge('new')
pending.get('new')(document('new')); await newRequest
pending.get('old')(document('old')); await oldRequest
assert.equal(state.reading.value.knowledge_id, 'new', 'late result cannot replace the selected document')

const closedRequest = state.openKnowledge('closed')
state.readingOpen.value = false; state.clearReading()
pending.get('closed')(document('closed')); await closedRequest
assert.equal(state.reading.value, null, 'closing the drawer invalidates pending responses')
console.log('Knowledge reading: refresh continuity, version update, switching, access loss and stale responses passed.')

// Search uses the semantic endpoint and cannot expose stale results after a new query or reset.
globalThis.searchPlan = async query => ({ ok: true, query, result_count: 1, results: [{ knowledge_id: 'semantic', title: '入库核对方法', snippet: '发布后核对全文。' }] })
state.searchInput.value = '  如何确认同事能搜到资料  '
await state.runSearch()
assert.equal(state.searchResults.value.query, '如何确认同事能搜到资料')
assert.equal(state.searchResults.value.results[0].knowledge_id, 'semantic')
assert.equal(state.filters.q, '', 'semantic query must not become a title filter')
const searches = new Map()
globalThis.searchPlan = (query, signal) => new Promise(done => searches.set(query, { done, signal }))
state.searchInput.value = '旧问题'; const older = state.runSearch()
state.searchInput.value = '新问题'; const newer = state.runSearch()
assert.equal(searches.get('旧问题').signal.aborted, true)
searches.get('新问题').done({ query: '新问题', result_count: 0, results: [] }); await newer
searches.get('旧问题').done({ query: '旧问题', result_count: 1, results: [{ knowledge_id: 'stale' }] }); await older
assert.equal(state.searchResults.value.query, '新问题')
assert.equal(state.searchResults.value.result_count, 0)
assert.equal(state.searchError.value, '')
assert(source.includes('未找到足够相关资料'))
assert(source.includes('@click="resetFilters">浏览当前分类'))
state.searchInput.value = '关闭问题'; const closing = state.runSearch()
state.clearSearch()
searches.get('关闭问题').done({ query: '关闭问题', result_count: 1, results: [{ knowledge_id: 'stale' }] }); await closing
assert.equal(state.searchResults.value, null)
globalThis.searchPlan = async () => { throw new PortalError(403) }
state.searchInput.value = '权限问题'; await state.runSearch()
assert.equal(state.searchResults.value, null)
assert(state.searchError.value)
globalThis.searchPlan = async () => { throw new PortalError(503) }
state.searchInput.value = '服务故障'; await state.runSearch()
assert.equal(state.searchResults.value, null, 'service failure must not be shown as no relevant knowledge')
assert(state.searchError.value)
console.log('Knowledge search: semantic routing, stale responses, cancellation, empty results and permission failure passed.')

// Roles exist independently of asset counts; search retains the selected role and category.
const roleId = 'role_' + 'a'.repeat(24)
globalThis.rolesPlan = async () => ({ items: [{ role_id: roleId, department: '总经办', name: 'AI架构师', counts: { sop: 0, skill: 0, other: 0 }, total: 0 }] })
await state.loadRoles()
assert.equal(state.departments.value[0].roles.length, 1, 'an empty role remains in the directory')
assert.equal(state.expandedDepartments.value.size, 0, 'departments start collapsed')
state.rememberDepartment('总经办', { target: { open: true } })
assert(state.expandedDepartments.value.has('总经办'))
await state.loadRoles()
assert(state.expandedDepartments.value.has('总经办'), 'refresh preserves the open department')
state.browseRole(roleId)
assert.equal(state.filters.category, 'sop')
assert.equal(state.selectedRole.value.total, 0)
assert.deepEqual(state.categories.map(item => item.key), ['sop', 'skill', 'other'])
state.chooseCategory('skill')
let searchFilters
globalThis.searchPlan = async (query, signal, filters) => { searchFilters = filters; return { result_count: 0, results: [] } }
state.searchInput.value = '会议准备'; await state.runSearch()
assert.deepEqual(searchFilters, { role_id: roleId, category: 'skill' })
state.resetFilters()
assert.equal(state.filters.role_id, roleId)
assert.equal(state.filters.category, 'skill', 'clearing search does not leave the role')
let catalogFilters
globalThis.catalogPlan = async filters => { catalogFilters = filters; return { items: [], total: 0, offset: 0 } }
state.browseRole('unknown')
globalThis.searchPlan = () => assert.fail('unassigned title search must not become global semantic search')
state.searchInput.value = '历史流程'; await state.runSearch()
assert.equal(catalogFilters.role_id, 'unknown')
assert.equal(catalogFilters.q, '历史流程')
state.browseRole('')
assert.equal(state.filters.role_id, '')
state.showDirectory()
assert.equal(state.directoryOpen.value, true)
assert(state.expandedDepartments.value.has('总经办'), 'returning from a role preserves its open department')
state.rememberDepartment('总经办', { target: { open: false } })
assert.equal(state.expandedDepartments.value.size, 0, 'department can be collapsed again')

// File grants are requested on demand and invalidated on version changes and drawer closure.
const preview = { file_id: 'preview', filename: '岗位SOP.html', purpose: 'preview', media_type: 'text/html', byte_length: 100, sha256: 'a'.repeat(64) }
const originalFile = { ...preview, file_id: 'original', filename: '岗位SOP.docx', purpose: 'original' }
globalThis.readingPlan = id => ({ ...document(id), files: [preview, originalFile], kind: 'sop' })
await state.openKnowledge('sop')
assert.equal(state.downloadFiles.value.length, 1)
assert.equal(state.previewFile.value.file_id, 'preview', 'DOCX keeps its original layout preview')
globalThis.filePlan = async (id, fileId, disposition) => { assert.equal(disposition, 'inline'); return { html: "<!doctype html><meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\"><style>body{color:black}</style><body><table><tr><td>岗位流程</td></tr></table></body>", expires_in: 60, filename: preview.filename, media_type: 'text/html' } }
await state.accessFile(preview, 'inline')
assert(state.previewHtml.value.includes('岗位流程'))
assert(state.previewHtml.value.includes("style-src 'self'"))
assert(state.previewHtml.value.includes('/portal/knowledge/assets/knowledge-preview.css'))
assert(!state.previewHtml.value.includes('<style>'))
assert.match(source, /:srcdoc="previewHtml"/)
assert.doesNotMatch(source, /:src="previewHtml"/)
globalThis.readingPlan = id => ({ ...document(id, 2), files: [preview, originalFile] })
await state.openKnowledge('sop')
assert.equal(state.previewHtml.value, '', 'new revision invalidates old signed preview')
let fileDone
globalThis.filePlan = () => new Promise(done => { fileDone = done })
const signing = state.accessFile(preview, 'inline')
state.readingOpen.value = false; state.clearReading()
fileDone({ html: '<p>过期正文</p>', expires_in: 60, media_type: 'text/html' }); await signing
assert.equal(state.previewHtml.value, '', 'late grant cannot reopen a closed preview')
globalThis.readingPlan = id => ({ ...document(id), files: [preview, originalFile], kind: 'skill' })
await state.openKnowledge('skill')
assert.equal(state.hasSkillPackage.value, false, 'a downloadable original is not a verified complete Skill package')
const skillPackage = { ...originalFile, purpose: 'skill', filename: 'skill.zip' }
globalThis.readingPlan = id => ({ ...document(id), files: [skillPackage], kind: 'skill' })
await state.openKnowledge('skill')
assert.equal(state.hasSkillPackage.value, true)
let clicks = 0, removed = 0, grants = 0
globalThis.document = { createElement: () => ({ click() { clicks++ }, remove() { removed++ } }), body: { appendChild() {} } }
globalThis.filePlan = async () => { grants++; return { url: 'https://example.invalid/download', expires_in: 60, filename: 'skill.zip', media_type: 'application/zip' } }
await state.accessFile(skillPackage, 'attachment'); await state.accessFile(skillPackage, 'attachment')
assert.equal(grants, 2, 'each download requests a fresh grant')
assert.equal(clicks, 2)
assert.equal(removed, 2, 'temporary download links are removed')
assert.equal(state.previewHtml.value, '', 'download URLs are never kept as preview state')
globalThis.filePlan = async () => ({ html: '<p>正文</p>', expires_in: 0, media_type: 'text/html' })
await state.accessFile(preview, 'inline')
assert.match(state.fileError.value, /过期/)
assert.equal(state.previewHtml.value, '')
for (const response of [{url:'https://example.invalid/forced-download',media_type:'text/html'}, {html:'<p>wrong type</p>',media_type:'text/plain'}, {html:'',media_type:'text/html'}]) {
  globalThis.filePlan = async () => ({...response, expires_in:60})
  await state.accessFile(preview, 'inline')
  assert.equal(state.previewHtml.value, '')
  assert.match(state.fileError.value, /预览/)
}
state.readingOpen.value = false; state.clearReading()
assert.match(source, /sandbox=""/)
assert.doesNotMatch(source, /allow-scripts|allow-same-origin|v-html|localStorage|sessionStorage/)
for (const extension of ['md', 'pdf', 'xlsx']) {
  globalThis.readingPlan = id => ({ ...document(id), files: [preview, { ...originalFile, filename: `岗位SOP.${extension}` }], kind: 'sop' })
  await state.openKnowledge(extension)
  assert.equal(state.previewFile.value, undefined, 'plain-text OSS previews cannot bypass the formatted body')
  assert.equal(state.downloadFiles.value.length, 1, 'all original formats remain downloadable')
}
assert.match(source, /<KnowledgeMarkdown v-else :content="reading.content"/)
state.readingOpen.value = false; state.clearReading()
console.log('Knowledge roles and files: zero-asset roles, three categories, scoped search, legacy access, version changes and stale grants passed.')

{
const cancelled = new AbortController()
let finishCancelledGraph
globalThis.graphPlan = () => new Promise(resolve => { finishCancelledGraph = resolve })
const oldGraph = state.readGraph(cancelled.signal)
cancelled.abort()
const currentGraph = { nodes: [{ knowledge_id: 'fresh' }], edges: [], status: 'ready' }
globalThis.graphPlan = () => currentGraph
const callsBeforeResume = globalThis.graphCalls
const resumedGraph = state.readGraph()
assert.equal(globalThis.graphCalls, callsBeforeResume + 1, 'an aborted request cannot be shared with a new activation')
assert.equal(await resumedGraph, currentGraph, 'returning to the graph must not reuse an aborted pending request')
finishCancelledGraph({ nodes: [{ knowledge_id: 'outdated' }], edges: [], status: 'ready' }); await oldGraph
globalThis.readingPlan = id => document(id)
await state.openKnowledge('fresh'); await state.loadReadingLinks()
assert.equal(state.readingGraph.value.nodes[0].knowledge_id, 'fresh', 'late aborted responses cannot overwrite the shared graph snapshot')
globalThis.graphPlan = () => { throw new PortalError(403) }
await assert.rejects(state.readGraph())
assert.equal(state.readingGraph.value, null, 'forbidden graph responses also purge shared reading relations')
delete globalThis.graphPlan
console.log('Knowledge graph sharing: aborted pending reads are not reused and late responses cannot replace the current snapshot.')
}

// Exercise the actual API contract and URL validation separately from component request stubs.
const realApiURL = moduleURL(ts.transpileModule(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText)
const realApi = await import(realApiURL)
globalThis.window = { location: { origin: 'https://portal.example' }, setTimeout, clearTimeout }
let request
globalThis.fetch = async (path, options) => { request = { path, options }; return { ok: true, status: 200, json: async () => ({}) } }
await realApi.searchKnowledge('测试', undefined, { role_id: roleId, category: 'sop' })
assert.equal(JSON.parse(request.options.body).role_id, roleId)
assert.equal(JSON.parse(request.options.body).category, 'sop')
await realApi.getKnowledgeFile('sop', 'original', 'attachment')
assert.equal(request.path, '/portal/knowledge/api/catalog/sop/files/original?disposition=attachment')
assert.equal(request.options.cache, 'no-store')
assert.equal(realApi.safeFileUrl('/file', 'https://portal.example'), 'https://portal.example/file')
assert.equal(realApi.safeFileUrl('https://private-oss.example/file'), 'https://private-oss.example/file')
assert.equal(realApi.safeFileUrl('http://localhost:9001/file', 'http://127.0.0.1:5174'), 'http://localhost:9001/file')
for (const value of ['javascript:alert(1)', 'data:text/html,unsafe', 'file:///tmp/file', 'http://other.example/file', 'https://user:pass@other.example/file', 'http://localhost/file']) {
  assert.throws(() => realApi.safeFileUrl(value, 'https://portal.example'))
}
console.log('Knowledge file API: role/category payload, download contract and HTTPS/same-origin URL validation passed.')
