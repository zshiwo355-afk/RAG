import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp, createRenderer, h, KeepAlive, nextTick, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const transpile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const layoutURL = moduleURL(transpile(await readFile(new URL('../src/components/knowledgeGraphLayout.ts', import.meta.url), 'utf8')))
const { layoutKnowledge, chooseKnowledgeLabels, fitKnowledge, neighborhood, zoomKnowledge, GRAPH_LIMIT } = await import(layoutURL)
const apiURL = moduleURL('export class PortalError extends Error { constructor(status) { super("无权读取"); this.status = status } }; export async function getGraph() { if (globalThis.__graphFailure) throw new Error(globalThis.__graphFailure); return globalThis.__graphFixture; }')
const { PortalError } = await import(apiURL)
const source = await readFile(new URL('../src/components/KnowledgeGraph.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'graph-check', inlineTemplate: true }).content
// SSR awaits the same fetch function normally invoked at mount; no graph logic is replaced.
const javascript = transpile(script)
  .replace('onMounted,', 'onServerPrefetch as onMounted,')
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
  .replace(/from (['"])\.\/knowledgeGraphLayout\1/g, `from '${layoutURL}'`)
const component = (await import(moduleURL(javascript))).default
const node = (id, extra = {}) => ({ knowledge_id: `k${id}`, title: `知识 ${id}`, kind: 'case', revision: 1, uploader_position: null, ...extra })
const edge = (a, b, extra = {}) => ({ id: `${a}-${b}`, source: `k${a}`, target: `k${b}`, relation: 'reference', label: '明确引用', directed: true, reason: '正文中存在明确链接', evidence: [], terms: [], ...extra })
const fixture = (nodes = [], edges = [], extra = {}) => ({ nodes, edges, status: 'ready', counts: { published_assets: nodes.length, indexed_assets: nodes.length, failed_assets: 0, explicit_edges: edges.filter(e => e.relation === 'reference').length, related_edges: edges.filter(e => e.relation === 'related').length }, truncated: false, generated_at: '2026-10-09T12:00:00Z', ...extra })
const render = async (data, canRead = true) => { globalThis.__graphFixture = data; return renderToString(createSSRApp(component, { refreshKey: 0, canRead })) }

const nodes = Array.from({ length: 149 }, (_, index) => node(index))
const edges = Array.from({ length: 148 }, (_, index) => edge(index, index + 1, index % 2 ? { relation: 'related', label: '内容相关', directed: false, reason: '共享内容主题', terms: ['方法'] } : {}))
const html = await render(fixture(nodes, edges))
assert.equal((html.match(/class="network-node/g) || []).length, 149)
assert.equal((html.match(/class="network-edge/g) || []).length, 148)
assert.match(html, /149 个节点/)
assert.match(html, /明确关系/)
assert.match(html, /内容相关/)
assert.match(html, /aria-label="知识目录"/)
assert.match(html, /aria-label="按类型浏览知识"/)
assert.equal((html.match(/class="directory-node/g) || []).length, 149)
assert.doesNotMatch(html, /class="network-inspector/)
assert.doesNotMatch(html, /岗位知识图谱|class="kind-node|class="position-node/)

const large = await render(fixture(Array.from({ length: 280 }, (_, index) => node(index))))
assert.equal((large.match(/class="network-node/g) || []).length, GRAPH_LIMIT)
assert.match(large, /画布最多显示 250 个节点/)
assert.match(await render(fixture([node(1)], [], { truncated: true })), /关系索引范围有限，全部正式知识可到知识总览查阅/)
const empty = await render(fixture())
assert.match(empty, /暂无已发布知识/)
const isolated = await render(fixture([node(1)]))
assert.match(isolated, /当前范围尚无可展示的关系/)
const privateTarget = await render(fixture([node(1)], [edge(1, 999)]))
assert.doesNotMatch(privateTarget, /class="network-edge/)
const pending = await render(fixture([node(1)], [], { status: 'building' }))
assert.match(pending, /关系索引正在更新/)
const partial = await render(fixture([node(1)], [], { status: 'partial' }))
assert.match(partial, /部分关系尚未完成处理/)
const denied = await render(fixture(nodes, edges), false)
assert.match(denied, /当前账号未开放知识图谱/)
assert.doesNotMatch(denied, /class="network-node/)
const escaped = await render(fixture([node(1, { title: '</text><script>alert(1)</script>' })]))
assert.doesNotMatch(escaped, /<script>alert/)
assert.match(escaped, /&lt;script&gt;/)
globalThis.__graphFailure = '合成网络故障'
assert.match(await render(fixture(nodes, edges)), /图谱暂时无法读取/)
delete globalThis.__graphFailure

const chain = [edge(1, 2), edge(2, 3), edge(3, 4), edge(2, 5, { relation: 'related' })]
assert.deepEqual([...neighborhood('k2', chain, 1)].sort(), ['k1', 'k2', 'k3', 'k5'])
assert.deepEqual([...neighborhood('k2', chain, 2)].sort(), ['k1', 'k2', 'k3', 'k4', 'k5'])
const positions = layoutKnowledge(nodes, edges)
assert.equal(positions.size, 149)
assert.equal(new Set([...positions.values()].map(p => `${p.x},${p.y}`)).size, 149)
for (const point of positions.values()) assert(Number.isFinite(point.x) && Number.isFinite(point.y) && point.x >= 42 && point.x <= 878 && point.y >= 40 && point.y <= 580)
assert.deepEqual(layoutKnowledge(nodes, edges), positions)
assert.deepEqual(layoutKnowledge(nodes, edges, positions), positions)
const noEdges = layoutKnowledge(nodes, [])
const ready = layoutKnowledge(nodes, edges, noEdges, true)
assert([...ready].some(([id, point]) => point.x !== noEdges.get(id).x || point.y !== noEdges.get(id).y))
assert.deepEqual(layoutKnowledge(nodes, edges, ready), ready)
const extended = layoutKnowledge([...nodes, node(200)], [...edges, edge(1, 200)], positions)
for (const [id, point] of positions) assert.deepEqual(extended.get(id), point)
const cluster = [{ x: 400, y: 220 }, { x: 620, y: 430 }, { x: 430, y: 270 }]
const fitted = fitKnowledge(cluster)
assert(fitted.scale > 1.5)
for (const point of cluster) {
  assert(point.x * fitted.scale + fitted.x >= 0 && point.x * fitted.scale + fitted.x <= 920)
  assert(point.y * fitted.scale + fitted.y >= 0 && point.y * fitted.scale + fitted.y <= 620)
}
assert.deepEqual(fitKnowledge(cluster), fitted)
assert.deepEqual(fitKnowledge([]), { scale: 1, x: 0, y: 0 })
assert(Number.isFinite(fitKnowledge([{ x: 420, y: 260 }]).scale))
const labels = chooseKnowledgeLabels(nodes, edges, positions)
assert(labels.size > 0)
assert.deepEqual(chooseKnowledgeLabels(nodes, edges, positions), labels)
assert(chooseKnowledgeLabels(nodes, edges, positions, '', 'k148').has('k148'))
assert(chooseKnowledgeLabels(nodes, edges, positions, '', '', 'k148').has('k148'))
const zoomLabels = chooseKnowledgeLabels(nodes, edges, positions, '', '', '', 3)
assert(zoomLabels.size > labels.size && zoomLabels.size > 10)
assert(chooseKnowledgeLabels(nodes, edges, positions, 'k148', '', '', 3).has('k148'))
assert(chooseKnowledgeLabels(nodes, edges, positions, '', 'k148', '', .3).has('k148'))
const spaced = new Map(nodes.slice(0, 12).map((value, i) => [value.knowledge_id, { x: i * 200, y: 100 }]))
assert.equal(chooseKnowledgeLabels(nodes.slice(0, 12), edges, spaced).size, 12)
// Validate collision boxes in screen coordinates at both zoom levels. The
// reserved text width/height stays constant instead of ballooning with zoom.
for (const displayScale of [.3, 1, 3, 5]) {
  const visible = chooseKnowledgeLabels(nodes, edges, positions, '', '', '', displayScale)
  const boxes = []
  for (const [id, offset] of visible) {
    const title = nodes.find(value => value.knowledge_id === id).title
    const chars = Array.from(title)
    const width = chars.slice(0, 15).reduce((sum, char) => sum + (char.codePointAt(0) > 127 ? 14 : 8), chars.length > 15 ? 14 : 0) + 12
    const point = positions.get(id)
    const box = { left: point.x * displayScale - width / 2, right: point.x * displayScale + width / 2, top: (point.y + offset) * displayScale - 17, bottom: (point.y + offset) * displayScale + 10 }
    assert(!boxes.some(existing => box.left < existing.right && box.right > existing.left && box.top < existing.bottom && box.bottom > existing.top))
    boxes.push(box)
  }
}
const overlapping = new Map(nodes.map(node => [node.knowledge_id, { x: 460, y: 310 }]))
assert.equal(chooseKnowledgeLabels(nodes, edges, overlapping).size, 2)
const anchor = { x: 193, y: 427 }
const initialPan = { x: 37, y: -23 }
for (const requestedScale of [.1, .6, 1.5, 3.8, 8]) {
  const zoomed = zoomKnowledge(1.2, initialPan, requestedScale, anchor)
  assert(zoomed.scale >= .25 && zoomed.scale <= 5)
  assert(Math.abs((anchor.x - initialPan.x) / 1.2 - (anchor.x - zoomed.pan.x) / zoomed.scale) < 1e-9)
  assert(Math.abs((anchor.y - initialPan.y) / 1.2 - (anchor.y - zoomed.pan.y) / zoomed.scale) < 1e-9)
}
const narrowFit = fitKnowledge(cluster, 410, 730)
for (const point of cluster) {
  assert(point.x * narrowFit.scale + narrowFit.x >= 0 && point.x * narrowFit.scale + narrowFit.x <= 410)
  assert(point.y * narrowFit.scale + narrowFit.y >= 0 && point.y * narrowFit.scale + narrowFit.y <= 730)
}
console.log('KnowledgeGraph: network, directory, states, escaping, BFS, stable layout, scale-aware non-overlapping labels, adaptive fit and cursor-anchored zoom passed.')

// Exercise real mounted hooks/watchers against a deterministic browser clock.
const liveCode = transpile(compileScript(parse(source).descriptor, { id: 'graph-live-check' }).content)
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
  .replace(/from (['"])\.\/knowledgeGraphLayout\1/g, `from '${layoutURL}'`)
const liveComponent = (await import(moduleURL(liveCode))).default
const portalSource = await readFile(new URL('../src/views/KnowledgePortal.vue', import.meta.url), 'utf8')
const visibleRefreshSource = portalSource.match(/^async function visibleRefresh\(\).*$/m)?.[0]
assert(visibleRefreshSource, 'exercise the actual parent visibility handler with the mounted graph')
const parentVisibilityHandler = new Function('canRefresh', 'checkAccess', 'refresh', 'refreshCurrent', 'activeView', `${visibleRefreshSource}; return visibleRefresh`)
const renderer = createRenderer({ createComment: () => ({}), createElement: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
const flush = async () => { await nextTick(); await new Promise(setImmediate) }
const originals = new Map(['document', 'window', 'navigator', 'setTimeout', 'clearTimeout', 'ResizeObserver'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
const browserDocument = Object.assign(new EventTarget(), { visibilityState: 'visible' })
const browserWindow = Object.assign(new EventTarget(), { innerWidth: 1200 })
const browserNavigator = { onLine: true }
let now = 0, timerId = 0
const timers = new Map()
const observers = []
for (const [key, value] of Object.entries({
  document: browserDocument, window: browserWindow, navigator: browserNavigator,
  setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, at: now + delay }); return id },
  clearTimeout: id => timers.delete(id),
  ResizeObserver: class {
    constructor(callback) { this.callback = callback; this.connected = false; observers.push(this) }
    observe() { this.connected = true }
    disconnect() { this.connected = false }
  },
})) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
async function advance(milliseconds) {
  const until = now + milliseconds
  while (true) {
    const next = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0]
    if (!next) break
    now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush()
  }
  now = until; await flush()
}
function mountGraph(fetchGraph, cached = false) {
  let state
  const refreshKey = ref(0), canRead = ref(true), page = ref('graph'), accessKey = ref('first-account')
  const wrapper = {
    ...liveComponent, setup(props, context) { state = liveComponent.setup(props, context); return state }, render() { return null },
  }
  const graph = () => h(wrapper, { refreshKey: refreshKey.value, canRead: canRead.value, fetchGraph })
  const app = renderer.createApp({ render() { return cached ? h(KeepAlive, { key: accessKey.value }, () => page.value === 'graph' ? graph() : null) : graph() } })
  app.mount({})
  return { app, get state() { return state }, refreshKey, canRead, page, accessKey }
}
let live
try {
  let cachedReads = 0
  live = mountGraph(async () => { cachedReads++; return fixture([node(1), node(2)], [edge(1, 2)], { status: 'building', generated_at: String(cachedReads) }) }, true)
  await flush()
  live.state.svg.value = { getBoundingClientRect: () => ({ width: 920, height: 620 }), hasPointerCapture: () => false }
  await flush()
  assert(observers.some(observer => observer.connected), 'active graph observes canvas dimensions')
  live.state.search.value = '知识'; live.state.kind.value = 'case'; live.state.selectNode('k1')
  await flush()
  live.state.zoom(.2); live.state.pan.value = { x: 38, y: -24 }
  const cachedState = live.state, cachedPoints = live.state.points.value
  live.page.value = 'overview'; await flush()
  assert.equal(timers.size, 0, 'a cached graph stops polling when another portal page is visible')
  assert(observers.every(observer => !observer.connected), 'hidden cached graphs disconnect resize observers')
  const hiddenCanvasSize = live.state.canvasSize.value
  for (const observer of observers) observer.callback()
  assert.equal(live.state.canvasSize.value, hiddenCanvasSize, 'late resize callbacks cannot update a hidden graph')
  live.refreshKey.value++; await flush(); await advance(6000)
  browserWindow.dispatchEvent(new Event('online')); await flush()
  assert.equal(cachedReads, 1, 'hidden cached graphs ignore parent refresh and browser events')
  live.page.value = 'graph'; await flush()
  assert.equal(live.state, cachedState, 'navigation restores the same graph instance')
  assert.equal(cachedReads, 2, 'returning validates the graph once')
  assert(observers.some(observer => observer.connected), 'returning resumes canvas observation')
  assert.equal(live.state.points.value, cachedPoints, 'an unchanged graph retains points without another layout')
  assert.equal(live.state.search.value, '知识')
  assert.equal(live.state.kind.value, 'case')
  assert.equal(live.state.selectedId.value, 'k1')
  assert.equal(live.state.inspectorOpen.value, true)
  assert.equal(live.state.scale.value, 1.2)
  assert.deepEqual(live.state.pan.value, { x: 38, y: -24 })
  console.log('KnowledgeGraph cache: same instance/points/filter/selection/zoom/pan restored; hidden timers, requests and observers stop; unchanged results skip layout.')
  live.app.unmount(); now = 0
  let refreshFailure
  live = mountGraph(async () => { if (refreshFailure) throw refreshFailure; return fixture([node(1), node(2)], [edge(1, 2)]) })
  await flush()
  live.state.selectNode('k1'); live.state.zoom(.2); await flush()
  const beforeFailure = live.state.data.value, beforeFailurePoints = live.state.points.value
  refreshFailure = new Error('服务暂时不可用')
  live.refreshKey.value++; await flush()
  assert.equal(live.state.data.value, beforeFailure, 'temporary refresh failures retain the previously validated graph')
  assert.equal(live.state.points.value, beforeFailurePoints)
  assert.equal(live.state.selectedId.value, 'k1')
  assert.equal(live.state.error.value, '服务暂时不可用')
  refreshFailure = undefined; live.refreshKey.value++; await flush()
  assert.equal(live.state.points.value, beforeFailurePoints, 'recovery with unchanged data needs no layout')
  assert.equal(live.state.error.value, '')
  refreshFailure = new PortalError(403)
  live.refreshKey.value++; await flush()
  assert.equal(live.state.data.value, null, 'forbidden responses purge previously cached graph data')
  assert.equal(live.state.points.value.size, 0)
  console.log('KnowledgeGraph refresh failures: transient errors preserve the graph; recovery keeps layout; forbidden responses purge cached data.')
  live.app.unmount()
  let initialReads = 0
  browserDocument.visibilityState = 'hidden'
  live = mountGraph(async () => { initialReads++; return fixture([node(1)], []) })
  await flush()
  assert.equal(initialReads, 0)
  browserDocument.visibilityState = 'visible'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush()
  assert.equal(initialReads, 1, 'a graph mounted in a background tab loads when first shown')
  live.app.unmount()
  const calls = []
  live = mountGraph(async () => {
    calls.push(now)
    return fixture([node(1), node(2)], now >= 4000 ? [edge(1, 2)] : [], {
      status: now >= 4000 ? 'ready' : 'building',
      counts: { published_assets: 2, indexed_assets: now >= 4000 ? 2 : 0, failed_assets: 0, explicit_edges: now >= 4000 ? 1 : 0, related_edges: 0 },
    })
  })
  await flush()
  assert.equal(live.state.data.value.status, 'building')
  await advance(4100)
  assert.equal(live.state.data.value.status, 'ready', 'a backend that completes at 4s must become visible within 2s, without waiting for the parent 30s interval')
  assert.deepEqual(calls, [0, 2000, 4000])
  assert.equal(live.state.data.value.edges.length, 1)
  await advance(30_000)
  assert.equal(calls.length, 3, 'ready graphs stop their fast polling')
  console.log('KnowledgeGraph progress: backend ready at 4000ms, displayed at 4000ms; graph reads at 0/2000/4000ms, no fast reads after ready.')
  live.app.unmount(); now = 0
  const resumedCalls = []
  live = mountGraph(async () => { resumedCalls.push(now); return fixture([node(1)], [], { status: 'building' }) })
  await flush()
  browserDocument.visibilityState = 'hidden'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush()
  assert.equal(timers.size, 0, 'hiding the graph cancels its pending progress timer immediately')
  await advance(6000)
  assert.equal(resumedCalls.length, 1, 'background pages make no progress requests')
  browserDocument.visibilityState = 'visible'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush()
  assert.equal(resumedCalls.length, 2, 'a visible building graph resumes immediately')
  browserNavigator.onLine = false; browserWindow.dispatchEvent(new Event('offline')); await flush()
  assert.equal(timers.size, 0, 'going offline cancels progress polling')
  await advance(6000)
  assert.equal(resumedCalls.length, 2)
  browserNavigator.onLine = true; browserWindow.dispatchEvent(new Event('online')); await flush()
  assert.equal(resumedCalls.length, 3, 'reconnecting resumes building progress')
  live.app.unmount()
  assert.equal(timers.size, 0, 'leaving the graph removes its timer')
  browserDocument.dispatchEvent(new Event('visibilitychange')); browserWindow.dispatchEvent(new Event('online')); await advance(6000)
  assert.equal(resumedCalls.length, 3, 'an unmounted graph cannot restart from browser events')
  console.log('KnowledgeGraph lifecycle: hidden/offline pause, visible/online resume, navigation removes timers and listeners.')
  let requestCount = 0, finishRequest
  live = mountGraph(() => {
    requestCount++
    return requestCount === 1 ? Promise.resolve(fixture([node(1)], [], { status: 'building' }))
      : new Promise(resolve => { finishRequest = resolve })
  })
  await flush()
  const previousData = live.state.data.value
  await advance(2000)
  assert.equal(requestCount, 2)
  assert.equal(live.state.loading.value, true)
  assert.equal(live.state.data.value, previousData, 'background progress refresh keeps existing nodes visible')
  live.refreshKey.value++; await flush()
  browserDocument.dispatchEvent(new Event('visibilitychange')); await flush()
  await advance(6000)
  assert.equal(requestCount, 2, 'parent refresh, browser events and polling cannot overlap an in-flight graph request')
  live.canRead.value = false; await flush()
  assert.equal(live.state.data.value, null, 'lost read access clears cached graph data')
  assert.equal(timers.size, 0)
  finishRequest(fixture([node(1), node(2)])); await flush()
  assert.equal(live.state.data.value, null, 'late responses cannot repopulate the graph after access is lost')
  await advance(6000)
  assert.equal(requestCount, 2)
  live.canRead.value = true; await flush()
  assert.equal(requestCount, 3)
  live.app.unmount()
  finishRequest(fixture([node(1)], [], { status: 'building' })); await flush()
  assert.equal(timers.size, 0, 'late building responses cannot restart polling after navigation or logout')
  await advance(6000)
  assert.equal(requestCount, 3)
  console.log('KnowledgeGraph concurrency: existing data retained, one request at a time, revoked access and unmount discard late responses.')
  now = 0
  browserDocument.visibilityState = 'hidden'
  const requestStarts = [], refreshFlags = [], sessionChecks = []
  let response = fixture([node(1)]), revokeOnCheck = false
  const activeView = ref('graph')
  const refreshCurrent = children => { refreshFlags.push(children); if (children) live.refreshKey.value++; return Promise.resolve() }
  const parentVisibleRefresh = parentVisibilityHandler(
    () => browserDocument.visibilityState === 'visible' && browserNavigator.onLine && live.canRead.value,
    async () => { sessionChecks.push(now); await new Promise(resolve => setTimeout(resolve, 1000)); if (revokeOnCheck) live.canRead.value = false },
    () => refreshCurrent(true), refreshCurrent, activeView,
  )
  browserDocument.addEventListener('visibilitychange', parentVisibleRefresh)
  try {
    live = mountGraph(() => { requestStarts.push(now); return new Promise(resolve => setTimeout(() => resolve(response), 90)) })
    await flush()
    browserDocument.visibilityState = 'visible'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush()
    await advance(1100)
    assert.deepEqual(requestStarts, [0], 'a 90ms graph response and a slower 1000ms session check must not produce a second graph GET')
    assert.equal(live.state.data.value.status, 'ready')
    browserDocument.visibilityState = 'hidden'; browserDocument.dispatchEvent(new Event('visibilitychange'))
    await advance(200)
    browserDocument.visibilityState = 'visible'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush()
    await advance(1100)
    assert.deepEqual(requestStarts, [0, 1300], 'ready graphs also revalidate once on returning to the page')
    response = fixture()
    for (let attempt = 0; attempt < 2; attempt++) {
      const before = requestStarts.length
      browserDocument.visibilityState = 'hidden'; browserDocument.dispatchEvent(new Event('visibilitychange')); await advance(200)
      browserDocument.visibilityState = 'visible'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush(); await advance(1100)
      assert.equal(requestStarts.length, before + 1, 'an empty ready graph revalidates once, so newly published nodes can appear')
      assert.equal(live.state.data.value.nodes.length, 0)
    }
    revokeOnCheck = true; response = fixture([node(2)])
    browserDocument.visibilityState = 'hidden'; browserDocument.dispatchEvent(new Event('visibilitychange')); await advance(200)
    browserDocument.visibilityState = 'visible'; browserDocument.dispatchEvent(new Event('visibilitychange')); await flush(); await advance(1100)
    assert.equal(live.state.data.value, null, 'the parent session check still clears data when read access is revoked')
    assert.equal(requestStarts.length, 5)
    assert.equal(sessionChecks.length, 5, 'visibility restoration still performs its normal access check')
    assert.deepEqual(refreshFlags, [false, false, false, false], 'graph restoration never asks the parent to refresh its child again')
    live.app.unmount(); revokeOnCheck = false; live.canRead.value = true; activeView.value = 'overview'
    browserDocument.dispatchEvent(new Event('visibilitychange')); await advance(1100)
    assert.equal(refreshFlags.at(-1), true, 'other views retain their normal child refresh')
    assert.equal(requestStarts.length, 5)
    console.log('KnowledgeGraph parent coordination: 90ms graph/1000ms session, one GET per visible restore; ready/empty revalidate, revoked access clears, other views retain refresh.')
  } finally { browserDocument.removeEventListener('visibilitychange', parentVisibleRefresh) }
} finally {
  live?.app.unmount()
  for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else delete globalThis[key]
  }
}
