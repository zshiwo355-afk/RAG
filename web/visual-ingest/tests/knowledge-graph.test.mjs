import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const transpile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const layoutURL = moduleURL(transpile(await readFile(new URL('../src/components/knowledgeGraphLayout.ts', import.meta.url), 'utf8')))
const { layoutKnowledge, chooseKnowledgeLabels, fitKnowledge, neighborhood, zoomKnowledge, GRAPH_LIMIT } = await import(layoutURL)
const apiURL = moduleURL('export async function getGraph() { if (globalThis.__graphFailure) throw new Error(globalThis.__graphFailure); return globalThis.__graphFixture; }')
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
