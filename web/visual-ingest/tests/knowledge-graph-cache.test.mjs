import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'
import ts from 'typescript'

// Mount the actual portal/dashboard templates: either ancestor destroying the
// graph must fail the same navigation check as destroying the graph itself.
const url = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const actualAPI = url(transpile(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8')))
const layoutURL = url(transpile(await readFile(new URL('../src/components/knowledgeGraphLayout.ts', import.meta.url), 'utf8')))
const apiURL = url(`
  export * from '${actualAPI}'
  export async function getSession() { return globalThis.sessionFixture }
  export async function getOverview() { return { scope: 'company', counts: {} } }
  export async function getJobs() { return { items: [], total: 0 } }
  export async function getRoles() { globalThis.roleReads++; return { items: [] } }
  export async function getCatalog() { return { items: [], total: 0, offset: 0 } }
  export async function getGraph(signal) { globalThis.graphReads++; return globalThis.graphPlan(signal) }
  export async function getPublishedKnowledge() { return { knowledge_id: 'k1', title: '知识 1', revision: 1, content: '原文' } }
`)
const stubURL = url('export default { render() { return null } }')
async function componentURL(file, capture, dependencies = {}, template = true) {
  const descriptor = parse(await readFile(new URL(file, import.meta.url), 'utf8')).descriptor
  const compiled = compileScript(descriptor, { id: capture })
  let code = transpile(compiled.content).replace('export default', 'const component =')
  if (template) {
    const result = compileTemplate({ source: descriptor.template.content, filename: file, id: capture, compilerOptions: { bindingMetadata: compiled.bindings } })
    assert.deepEqual(result.errors, [])
    code += `\n${transpile(result.code)}\ncomponent.render = render`
  } else code += '\ncomponent.render = () => null'
  code += `\nconst setup = component.setup; component.setup = (props, context) => { const state = setup(props, context); globalThis.${capture}.push(state); return state }; export default component`
  return url(code
    .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
    .replace(/from (['"])\.\/knowledgeGraphLayout\1/g, `from '${layoutURL}'`)
    .replace(/from (['"])(?:\.\.\/components\/|\.\/)([^'"]+\.vue)\1/g, (_, quote, name) => `from '${dependencies[name] || stubURL}'`)
    .replace(/import ['"]\.\.\/knowledge-[^'"]+\.css['"];?/g, ''))
}
globalThis.graphStates = []; globalThis.dashboardStates = []; globalThis.portalStates = []
globalThis.graphReads = 0; globalThis.roleReads = 0
globalThis.sessionFixture = { authenticated: true, principal: { id: 'alice', display_name: 'Alice' }, permissions: ['company_knowledge.review'], capabilities: { upload: false, review_write: false } }
const graph = {
  nodes: [1, 2].map(id => ({ knowledge_id: `k${id}`, title: `知识 ${id}`, kind: 'case', revision: 1, uploader_position: '设计师' })),
  edges: [], status: 'ready', counts: { published_assets: 2, indexed_assets: 2, failed_assets: 0, explicit_edges: 0, related_edges: 0 },
  truncated: false, generated_at: 'first',
}
globalThis.graphPlan = () => structuredClone(graph)
globalThis.window = Object.assign(new EventTarget(), { location: { hash: '#graph' }, innerWidth: 1200, confirm: () => true })
globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible', title: '', getElementById: () => ({ focus() {} }) })
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { onLine: true } })
const graphURL = await componentURL('../src/components/KnowledgeGraph.vue', 'graphStates', {}, false)
const dashboardURL = await componentURL('../src/components/KnowledgeDashboard.vue', 'dashboardStates', { 'KnowledgeGraph.vue': graphURL })
const portalURL = await componentURL('../src/views/KnowledgePortal.vue', 'portalStates', { 'KnowledgeDashboard.vue': dashboardURL })
const node = (tag = 'div') => Object.assign(new EventTarget(), { tagName: tag.toUpperCase(), children: [], parent: null, style: {}, options: [] })
function remove(child) { if (child.parent) { const list = child.parent.children; const at = list.indexOf(child); if (at >= 0) list.splice(at, 1) }; child.parent = null }
const renderer = createRenderer({
  createElement: node, createComment: node, createText: node, setText() {}, setElementText() {}, patchProp() {},
  insert(child, parent, anchor) { remove(child); const at = parent.children.indexOf(anchor); parent.children.splice(at < 0 ? parent.children.length : at, 0, child); child.parent = parent },
  remove, parentNode: child => child.parent, nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1],
})
const app = renderer.createApp((await import(portalURL)).default)
for (const name of ['el-drawer', 'el-pagination', 'el-dialog']) app.component(name, { render() { return null } })
const flush = async () => { await nextTick(); await new Promise(setImmediate); await nextTick() }
app.mount(node())
try {
  await flush()
  const portal = globalThis.portalStates[0], state = globalThis.graphStates[0]
  assert.equal(globalThis.graphStates.length, 1)
  assert.equal(globalThis.graphReads, 1, 'mount plus activation performs one graph GET')
  state.search.value = '知识'; state.kind.value = 'case'; state.position.value = '设计师'; state.selectNode('k1')
  await flush()
  state.zoom(.4); state.pan.value = { x: 32, y: -17 }; state.showRelated.value = false
  await flush()
  const points = state.points.value
  for (const destination of ['overview', 'receipts', 'attention', 'pending_publication']) {
    await portal.navigate(destination); await flush()
    const reads = globalThis.graphReads
    portal.refreshKey.value++; await flush()
    assert.equal(globalThis.graphReads, reads, 'hidden cached graphs do not react to another page refresh')
    await portal.navigate('graph'); await flush()
    assert.equal(globalThis.graphStates.length, 1, `${destination} return preserves both ancestor and graph instances`)
    assert.equal(globalThis.graphReads, reads + 1)
    assert.equal(state.points.value, points, 'unchanged topology preserves the exact point map')
    assert.equal(state.scale.value, 1.4)
    assert.deepEqual(state.pan.value, { x: 32, y: -17 })
    assert.equal(state.search.value, '知识'); assert.equal(state.kind.value, 'case'); assert.equal(state.position.value, '设计师')
    assert.equal(state.selectedId.value, 'k1'); assert.equal(state.showRelated.value, false)
  }
  // Deactivated dashboard requests and teleported body drawers also stop.
  await portal.navigate('overview'); await flush()
  const dashboard = globalThis.dashboardStates[0]
  await dashboard.openKnowledge('k1')
  await portal.navigate('receipts'); await flush()
  assert.equal(dashboard.readingOpen.value, false)
  assert.equal(dashboard.reading.value, null)
  const roleReads = globalThis.roleReads
  portal.refreshKey.value++; await flush()
  assert.equal(globalThis.roleReads, roleReads, 'a cached overview does not reload roles while tracking is visible')
  await portal.navigate('graph'); await flush()
  let finishOld
  let oldSignal
  globalThis.graphPlan = signal => { oldSignal = signal; return new Promise(resolve => { finishOld = resolve }) }
  portal.refreshKey.value++; await flush()
  assert.equal(state.loading.value, true)
  globalThis.graphPlan = () => ({ ...structuredClone(graph), nodes: [graph.nodes[1]] })
  portal.session.value = { ...globalThis.sessionFixture, principal: { id: 'bob', display_name: 'Bob' } }
  await flush()
  const replacement = globalThis.graphStates.at(-1)
  assert.equal(globalThis.graphStates.length, 2, 'identity changes destroy all cached ancestors')
  assert.equal(oldSignal.aborted, true)
  assert.equal(state.data.value, null)
  assert.equal(replacement.search.value, '')
  assert.equal(replacement.scale.value, 1)
  finishOld(graph); await flush()
  assert.equal(state.data.value, null)
  assert.deepEqual(replacement.data.value.nodes.map(value => value.knowledge_id), ['k2'], 'late old identity response never reaches the new cache')
  portal.session.value = { ...portal.session.value, permissions: ['company_knowledge.submissions.read'] }
  await flush()
  assert.equal(replacement.data.value, null, 'permission loss purges the previously readable cached graph')
  console.log('Graph portal cache: actual nested templates retain state across overview/receipts/attention/drafts; hidden requests/drawers pause; identity/permission changes purge caches and reject late responses.')
} finally { app.unmount() }
