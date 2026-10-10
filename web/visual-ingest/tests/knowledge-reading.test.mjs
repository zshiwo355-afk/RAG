import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const apiURL = moduleURL(`
  export class PortalError extends Error { constructor(status) { super('不可读取'); this.status = status } }
  export async function searchKnowledge(query, signal) { return globalThis.searchPlan(query, signal) }
  export async function getCatalog() { return { items: [], total: 0, offset: 0 } }
  export async function getGraph() { return { nodes: [], edges: [], status: 'ready' } }
  export async function getPublishedKnowledge(id) { return globalThis.readingPlan(id) }
`)
const stubURL = moduleURL('export default {}')
const source = await readFile(new URL('../src/components/KnowledgeDashboard.vue', import.meta.url), 'utf8')
const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'reading-check' }).content, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
  .replace(/from (['"])\.\/KnowledgeGraph.vue\1/g, `from '${stubURL}'`)
  .replace(/import ['"]\.\.\/knowledge-dashboard.css['"];?/, '')
const component = (await import(moduleURL(code))).default
const { PortalError } = await import(apiURL)
const props = {
  view: 'graph', refreshKey: 0, refreshing: false, overview: null, overviewError: '',
  session: { permissions: ['company_knowledge.read'], principal: { id: 'local' } },
}
const document = (id, revision = 1) => ({ knowledge_id: id, title: id, revision, content: `正文 ${revision}` })
let state
await renderToString(createSSRApp({ ...component, setup(props, context) {
  state = component.setup(props, context); return state
}, render() { return null } }, props))

globalThis.readingPlan = id => document(id)
await state.openKnowledge('first')
const original = state.reading.value
let resolve
globalThis.readingPlan = () => new Promise(done => { resolve = done })
const refreshing = state.openKnowledge('first')
assert.equal(state.reading.value, original, 'same document stays rendered during refresh')
assert.equal(state.readingLoading.value, true)
resolve(document('first', 2)); await refreshing
assert.equal(state.reading.value.revision, 2)
assert.equal(state.readingLoading.value, false)

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
state.searchInput.value = '关闭问题'; const closing = state.runSearch()
state.clearSearch()
searches.get('关闭问题').done({ query: '关闭问题', result_count: 1, results: [{ knowledge_id: 'stale' }] }); await closing
assert.equal(state.searchResults.value, null)
globalThis.searchPlan = async () => { throw new PortalError(403) }
state.searchInput.value = '权限问题'; await state.runSearch()
assert.equal(state.searchResults.value, null)
assert(state.searchError.value)
console.log('Knowledge search: semantic routing, stale responses, cancellation, empty results and permission failure passed.')
