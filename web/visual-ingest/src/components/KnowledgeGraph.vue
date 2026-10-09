<template>
  <section class="knowledge-graph" aria-label="知识互联网络">
    <div class="graph-tools">
      <button type="button" class="directory-toggle" :aria-expanded="directoryOpen" :aria-controls="directoryId" @click="directoryOpen = !directoryOpen"><span aria-hidden="true">☷</span>知识目录</button>
      <label class="network-search"><span aria-hidden="true">⌕</span><input v-model="search" type="search" maxlength="200" aria-label="搜索图谱知识" placeholder="搜索知识，发现连接…"></label>
      <label><span class="sr-only">图谱知识类型</span><select v-model="kind"><option value="">全部类型</option><option v-for="value in kinds" :key="value" :value="value">{{ kindLabel(value) }}</option></select></label>
      <label><span class="sr-only">图谱上传岗位</span><select v-model="position"><option value="">全部上传岗位</option><option v-for="value in positions" :key="value" :value="value">{{ value }}</option></select></label>
      <button v-if="search || kind || position" type="button" class="graph-text-button" @click="clearFilters">清除筛选</button>
    </div>
    <div class="graph-options">
      <div class="edge-toggles"><label><input v-model="showReferences" type="checkbox"><i class="legend-line" />明确关系 <span>{{ data?.counts.explicit_edges ?? '—' }}</span></label><label><input v-model="showRelated" type="checkbox"><i class="legend-line related" />内容相关 <span>{{ data?.counts.related_edges ?? '—' }}</span></label></div>
      <div class="scope-buttons" aria-label="图谱探索范围"><button type="button" :aria-pressed="depth === 0" @click="depth = 0">全局</button><button type="button" :aria-pressed="depth === 1" :disabled="!selectedNode" @click="depth = 1">1 跳</button><button type="button" :aria-pressed="depth === 2" :disabled="!selectedNode" @click="depth = 2">2 跳</button></div>
    </div>
    <div v-if="!canRead" class="graph-state"><h3>当前账号未开放知识图谱</h3><p>管理员可通过现有角色配置知识查看权限。</p></div>
    <div v-else-if="error" class="graph-state graph-failure" role="alert"><h3>图谱暂时无法读取</h3><p>{{ error }}</p><button type="button" @click="loadGraph">重新读取</button></div>
    <div v-else-if="!data" class="graph-state" role="status"><span class="network-loading" /><h3>正在整理知识之间的连接</h3><p>读取当前正式知识和有依据的关系。</p></div>
    <template v-else>
      <div v-if="data.status !== 'ready'" class="graph-status" role="status"><span class="status-dot" />{{ data.status === 'building' ? '关系索引正在更新' : '部分关系尚未完成处理' }} · 已处理 {{ data.counts.indexed_assets }} / {{ data.counts.published_assets }} 项知识<span v-if="data.counts.failed_assets"> · {{ data.counts.failed_assets }} 项处理失败</span>。现有结果可继续探索。</div>
      <div v-if="!data.nodes.length" class="graph-state"><span class="empty-orbit" aria-hidden="true">◎</span><h3>{{ data.counts.published_assets ? '关系正在准备中' : '从第一份正式知识开始' }}</h3><p>{{ data.counts.published_assets ? '后台更新完成后，知识节点会自动显示。' : '正式知识发布后，会成为图中的一个节点。' }}</p></div>
      <template v-else>
        <div v-if="!filteredNodes.length" class="graph-no-match" role="status">没有符合筛选条件的知识。<button type="button" class="graph-text-button" @click="clearFilters">清除筛选</button></div>
        <div class="network-workspace" :class="{ 'directory-open': directoryOpen }">
          <aside v-if="directoryOpen" :id="directoryId" class="network-directory" aria-label="知识目录">
            <div class="directory-heading"><span>知识目录 <small>{{ filteredNodes.length }}</small></span><button type="button" aria-label="收起知识目录" @click="directoryOpen = false">‹</button></div>
            <nav class="directory-list" aria-label="按类型浏览知识"><details v-for="group in directoryGroups" :key="group.kind" open><summary><span class="directory-chevron" aria-hidden="true">›</span><span>{{ kindLabel(group.kind) }}</span><small>{{ group.nodes.length }}</small></summary><button v-for="node in group.nodes" :key="node.knowledge_id" type="button" class="directory-node" :class="{ selected: node.knowledge_id === selectedId }" :aria-current="node.knowledge_id === selectedId ? 'true' : undefined" :title="node.title" @click="selectNode(node.knowledge_id)"><span class="directory-file" aria-hidden="true">▱</span><span>{{ node.title }}</span></button></details><p v-if="!filteredNodes.length" class="directory-empty">没有符合筛选的知识</p></nav>
          </aside>
          <div class="network-stage" :aria-busy="loading">
            <div class="network-caption"><span>{{ depth ? `以「${shorten(selectedNode?.title || '', 18)}」为中心 · ${depth} 跳` : '全局知识网络' }}</span><span v-if="loading">正在更新…</span><button v-if="(selectedNode || selectedEdge) && !inspectorOpen" type="button" class="inspector-reopen" @click="inspectorOpen = true">展开详情 ↗</button></div>
            <svg ref="svg" class="graph-canvas" :class="{ 'is-dragging': dragging }" :viewBox="`0 0 ${canvasSize.width} ${canvasSize.height}`" role="group" tabindex="0"
              aria-label="知识网络；滚轮缩放，拖动平移；点击知识查看出链入链，点击连线查看依据；方向键平移，加减键缩放，Home 键复位"
              @pointerdown="startDrag" @pointermove="moveDrag" @pointerup="stopDrag" @pointercancel="stopDrag" @lostpointercapture="stopDrag" @keydown="panWithKeyboard" @wheel="wheelZoom">
              <defs><pattern :id="gridId" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="#dfe5d7" /></pattern><marker :id="arrowId" viewBox="0 0 10 10" refX="20" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#718c73" /></marker></defs>
              <rect :width="canvasSize.width" :height="canvasSize.height" :fill="`url(#${gridId})`" />
              <g :transform="`translate(${pan.x} ${pan.y}) scale(${scale}) translate(${fit.x} ${fit.y}) scale(${fit.scale})`">
                <g v-for="edge in visibleEdges" :key="edge.id" class="network-edge" :class="{ related: edge.relation === 'related', selected: edge.id === selectedEdgeId, dimmed: selectedId && edge.source !== selectedId && edge.target !== selectedId }"
                  role="button" tabindex="0" data-graph-node :aria-label="`${nodeTitle(edge.source)}${edge.label}${nodeTitle(edge.target)}，查看关系依据`"
                  @click="selectEdge(edge.id)" @keydown.enter.prevent="selectEdge(edge.id)" @keydown.space.prevent="selectEdge(edge.id)">
                  <line class="edge-hit" :x1="pointFor(edge.source).x" :y1="pointFor(edge.source).y" :x2="pointFor(edge.target).x" :y2="pointFor(edge.target).y" />
                  <line class="edge-visible" :x1="pointFor(edge.source).x" :y1="pointFor(edge.source).y" :x2="pointFor(edge.target).x" :y2="pointFor(edge.target).y" :marker-end="edge.directed ? `url(#${arrowId})` : undefined" />
                  <title>{{ edge.label }}：{{ edge.reason }}</title>
                </g>
                <g v-for="node in visibleNodes" :key="node.knowledge_id" class="network-node" :class="{ selected: node.knowledge_id === selectedId, dimmed: selectedId && !neighbors.has(node.knowledge_id) }"
                  :transform="`translate(${pointFor(node.knowledge_id).x} ${pointFor(node.knowledge_id).y})`" role="button" tabindex="0" data-graph-node
                  :aria-label="`${node.title}，${kindLabel(node.kind)}，查看相连知识`" :aria-pressed="node.knowledge_id === selectedId"
                  @mouseenter="hoveredId = node.knowledge_id" @mouseleave="hoveredId = ''" @focus="focusedId = node.knowledge_id" @blur="focusedId = ''"
                  @click="selectNode(node.knowledge_id)" @keydown.enter.prevent="selectNode(node.knowledge_id)" @keydown.space.prevent="selectNode(node.knowledge_id)">
                  <circle class="node-halo" r="19" /><circle class="node-dot" :r="node.knowledge_id === selectedId ? 9 : 5.5 + Math.min(4, (degrees.get(node.knowledge_id) || 0) / 3)" :fill="kindColor(node.kind)" />
                  <text v-if="nodeLabels.has(node.knowledge_id)" :y="nodeLabels.get(node.knowledge_id)" :style="labelStyle">{{ shorten(node.title, 15) }}</text>
                  <title>{{ node.title }} · {{ kindLabel(node.kind) }}</title>
                </g>
              </g>
            </svg>
            <div v-if="visibleNodes.length && !visibleEdges.length" class="no-edge-note">{{ !showReferences && !showRelated ? '关系线已隐藏，可以通过上方开关显示。' : '当前范围尚无可展示的关系。知识节点仍可阅读。' }}</div>
            <div class="network-bottom"><div class="graph-controls"><button type="button" :disabled="scale <= .25" aria-label="缩小图谱" @click="zoom(-.2)">−</button><output aria-label="缩放比例">{{ Math.round(scale * 100) }}%</output><button type="button" :disabled="scale >= 5" aria-label="放大图谱" @click="zoom(.2)">＋</button><button type="button" class="graph-reset" @click="resetView">复位</button></div><span>滚轮缩放 · 拖动平移</span></div>
          <aside v-if="inspectorOpen && (selectedNode || selectedEdge)" class="network-inspector" aria-label="知识关系详情" aria-live="polite">
            <template v-if="selectedEdge">
              <div class="inspector-kicker">关系依据 <button type="button" class="graph-text-button" aria-label="收起关系详情" @click="inspectorOpen = false">×</button></div><span class="relation-tag" :class="selectedEdge.relation">{{ selectedEdge.relation === 'reference' ? `${selectedEdge.label === '正文引用' ? '明确引用' : '已记录关联'} · 实线` : '内容相关 · 虚线' }}</span>
              <div class="edge-endpoints"><button type="button" @click="selectNode(selectedEdge.source)">{{ nodeTitle(selectedEdge.source) }}</button><span aria-hidden="true">{{ selectedEdge.directed ? '↓' : '↕' }}</span><button type="button" @click="selectNode(selectedEdge.target)">{{ nodeTitle(selectedEdge.target) }}</button></div>
              <h3>{{ selectedEdge.label }}</h3><p class="relationship-reason">{{ selectedEdge.reason || '关系记录未提供说明。' }}</p><p v-if="selectedEdge.relation === 'related'" class="related-boundary">内容相关是一条发现线索，不代表两份知识存在引用或前后依赖。</p>
              <div v-if="selectedEdge.terms.length" class="relation-terms"><span v-for="term in selectedEdge.terms" :key="term">{{ term }}</span></div>
              <section class="edge-evidence"><h4>依据摘录</h4><article v-for="(evidence, index) in currentEvidence" :key="`${evidence.knowledge_id}-${index}`"><blockquote>{{ evidence.excerpt }}</blockquote><button type="button" class="graph-text-button" @click="emit('select', evidence.knowledge_id)">{{ nodeTitle(evidence.knowledge_id) }} · v{{ evidence.revision }} ↗</button></article><p v-if="!currentEvidence.length" class="inspector-muted">暂未提供当前正式版本的摘录，可阅读两端知识核对。</p></section>
            </template>
            <template v-else-if="selectedNode">
              <div class="inspector-kicker">当前知识 <button type="button" class="graph-text-button" aria-label="收起知识详情" @click="inspectorOpen = false">×</button></div><span class="node-type"><i :style="{ background: kindColor(selectedNode.kind) }" />{{ kindLabel(selectedNode.kind) }} · v{{ selectedNode.revision }}</span><h3>{{ selectedNode.title }}</h3><p class="inspector-muted">上传员工岗位：{{ selectedNode.uploader_position || '待补充岗位' }}</p><button type="button" class="read-knowledge" @click="emit('select', selectedNode.knowledge_id)">阅读全文 <span aria-hidden="true">↗</span></button>
              <div class="focus-actions"><button type="button" @click="focusNode(1)">查看 1 跳连接</button><button type="button" @click="focusNode(2)">查看 2 跳连接</button></div>
              <section v-for="section in connectionSections" :key="section.label" class="connections-list"><h4>{{ section.label }}<span>{{ section.items.length }}</span></h4><p v-if="!section.items.length" class="inspector-muted">暂无{{ section.label }}</p><div v-for="edge in section.items" :key="edge.id" class="connection-item"><button type="button" @click="selectNode(otherEnd(edge))">{{ nodeTitle(otherEnd(edge)) }}</button><button type="button" class="connection-evidence" :aria-label="`查看与${nodeTitle(otherEnd(edge))}的关系依据`" @click="selectEdge(edge.id)">依据 ↗</button></div></section>
            </template>
          </aside>
          </div>
        </div>
      </template>
      <div class="graph-footnote"><span>当前显示 {{ visibleNodes.length }} 个节点 · {{ visibleEdges.length }} 条关系 / {{ data.counts.published_assets }} 项正式知识</span><span v-if="data.truncated">关系索引范围有限，全部正式知识可到知识总览查阅。</span><span v-if="filteredNodes.length > GRAPH_LIMIT">画布最多显示 {{ GRAPH_LIMIT }} 个节点，请搜索或筛选缩小范围。</span><span>每条关系可查看理由，发布后的变化自动更新。</span></div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { getGraph, type GraphData, type GraphEdge } from '../api/knowledgePortal'
import { GRAPH_LIMIT, chooseKnowledgeLabels, fitKnowledge, layoutKnowledge, neighborhood, zoomKnowledge, type GraphPoint } from './knowledgeGraphLayout'

const props = defineProps<{ refreshKey: number; canRead: boolean }>()
const emit = defineEmits<{ select: [knowledgeId: string]; error: [error: unknown] }>()
const data = ref<GraphData | null>(null)
const loading = ref(false)
const error = ref('')
const search = ref('')
const kind = ref('')
const position = ref('')
const showReferences = ref(true)
const showRelated = ref(true)
const selectedId = ref('')
const selectedEdgeId = ref('')
const hoveredId = ref('')
const focusedId = ref('')
const depth = ref(0)
const directoryOpen = ref(typeof window === 'undefined' || window.innerWidth > 740)
const inspectorOpen = ref(false)
const directoryId = `knowledge-directory-${useId()}`
const svg = ref<SVGSVGElement | null>(null)
const canvasSize = ref({ width: 920, height: 620 })
const gridId = `knowledge-grid-${useId()}`
const arrowId = `knowledge-arrow-${useId()}`
const points = ref(new Map<string, GraphPoint>())
const fit = ref({ scale: 1, x: 0, y: 0 })
const scale = ref(1)
const pan = ref({ x: 0, y: 0 })
const dragging = ref(false)
let dragOrigin = { x: 0, y: 0, panX: 0, panY: 0, pointerId: -1 }
let requestId = 0
let layoutTopology = ''
let manualView = false
let resizeObserver: ResizeObserver | undefined
const nodesById = computed(() => new Map((data.value?.nodes || []).map(node => [node.knowledge_id, node])))
const validEdges = computed(() => (data.value?.edges || []).filter(edge => nodesById.value.has(edge.source) && nodesById.value.has(edge.target) && edge.source !== edge.target))
const enabledEdges = computed(() => validEdges.value.filter(edge => edge.relation === 'reference' ? showReferences.value : showRelated.value))
const selectedNode = computed(() => nodesById.value.get(selectedId.value))
const selectedEdge = computed(() => validEdges.value.find(edge => edge.id === selectedEdgeId.value))
const currentEvidence = computed(() => (selectedEdge.value?.evidence || []).filter(evidence => nodesById.value.get(evidence.knowledge_id)?.revision === evidence.revision))
const kinds = computed(() => [...new Set((data.value?.nodes || []).map(node => node.kind))].sort())
const positions = computed(() => [...new Set((data.value?.nodes || []).map(node => node.uploader_position || '待补充岗位'))].sort())
const focusedIds = computed(() => depth.value && selectedId.value ? neighborhood(selectedId.value, enabledEdges.value, depth.value) : null)
const filteredNodes = computed(() => (data.value?.nodes || []).filter(node =>
  (!search.value.trim() || node.title.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())) &&
  (!kind.value || node.kind === kind.value) && (!position.value || (node.uploader_position || '待补充岗位') === position.value) &&
  (!focusedIds.value || focusedIds.value.has(node.knowledge_id))))
const directoryGroups = computed(() => kinds.value.map(value => ({ kind: value, nodes: filteredNodes.value.filter(node => node.kind === value) })).filter(group => group.nodes.length))
const visibleNodes = computed(() => {
  const result = filteredNodes.value.slice(0, GRAPH_LIMIT)
  const selected = filteredNodes.value.find(node => node.knowledge_id === selectedId.value)
  if (selected && !result.some(node => node.knowledge_id === selectedId.value)) result[result.length - 1] = selected
  return result
})
const visibleIds = computed(() => new Set(visibleNodes.value.map(node => node.knowledge_id)))
const visibleEdges = computed(() => enabledEdges.value.filter(edge => visibleIds.value.has(edge.source) && visibleIds.value.has(edge.target)))
const displayScale = computed(() => scale.value * fit.value.scale)
const labelStyle = computed(() => ({ fontSize: `${12 / displayScale.value}px`, strokeWidth: `${4 / displayScale.value}px` }))
const nodeLabels = computed(() => chooseKnowledgeLabels(visibleNodes.value, visibleEdges.value, points.value, selectedId.value, hoveredId.value, focusedId.value, displayScale.value))
const neighbors = computed(() => selectedId.value ? neighborhood(selectedId.value, enabledEdges.value, 1) : new Set<string>())
const degrees = computed(() => { const result = new Map<string, number>(); for (const edge of enabledEdges.value) { result.set(edge.source, (result.get(edge.source) || 0) + 1); result.set(edge.target, (result.get(edge.target) || 0) + 1) } return result })
const connectionSections = computed(() => [
  { label: '出链', items: validEdges.value.filter(edge => edge.relation === 'reference' && edge.source === selectedId.value) },
  { label: '入链', items: validEdges.value.filter(edge => edge.relation === 'reference' && edge.target === selectedId.value) },
  { label: '内容相关', items: validEdges.value.filter(edge => edge.relation === 'related' && (edge.source === selectedId.value || edge.target === selectedId.value)) },
])
const kindNames: Record<string, string> = { sop: 'SOP / 流程', skill: 'Skill', case: '案例', template: '模板', prompt: '提示词', method: '方法', policy: '规则', reference: '参考', other: '其他' }
const kindColors: Record<string, string> = { sop: '#2b6553', skill: '#ac8447', case: '#789260', template: '#688994', prompt: '#9e725c', method: '#65735b', policy: '#856c81', reference: '#7d9294', other: '#8a9382' }
const kindLabel = (value: string) => kindNames[value] || value || '其他'
const kindColor = (value: string) => kindColors[value] || kindColors.other
const nodeTitle = (id: string) => nodesById.value.get(id)?.title || '当前不可见的知识'
const shorten = (value: string, limit: number) => Array.from(value).length > limit ? `${Array.from(value).slice(0, limit).join('')}…` : value
const pointFor = (id: string) => points.value.get(id) || { x: 460, y: 310 }
const otherEnd = (edge: GraphEdge) => edge.source === selectedId.value ? edge.target : edge.source

async function loadGraph() {
  if (!props.canRead) return
  const current = ++requestId
  loading.value = true; error.value = ''
  try {
    const result = await getGraph()
    if (current !== requestId) return
    data.value = result
    const ids = new Set(result.nodes.map(node => node.knowledge_id))
    points.value = new Map([...points.value].filter(([id]) => ids.has(id)))
    if (!ids.has(selectedId.value)) { selectedId.value = ''; depth.value = 0 }
    if (!result.edges.some(edge => edge.id === selectedEdgeId.value)) selectedEdgeId.value = ''
  } catch (reason) {
    if (current !== requestId) return
    data.value = null; points.value = new Map(); clearSelection()
    error.value = reason instanceof Error ? reason.message : '连接暂不可用，请稍后重试。'
    emit('error', reason)
  } finally { if (current === requestId) loading.value = false }
}
function clearSelection() { selectedId.value = ''; selectedEdgeId.value = ''; depth.value = 0; inspectorOpen.value = false }
function clearFilters() { search.value = ''; kind.value = ''; position.value = ''; depth.value = 0 }
function selectNode(id: string) { if (!filteredNodes.value.some(node => node.knowledge_id === id)) { search.value = ''; kind.value = ''; position.value = '' } selectedId.value = id; selectedEdgeId.value = ''; inspectorOpen.value = true; if (typeof window !== 'undefined' && window.innerWidth <= 740) directoryOpen.value = false }
function selectEdge(id: string) { selectedEdgeId.value = id; inspectorOpen.value = true }
function focusNode(value: number) { depth.value = value; search.value = ''; kind.value = ''; position.value = ''; resetView() }
function fitVisible() { fit.value = fitKnowledge(visibleNodes.value.map(node => pointFor(node.knowledge_id)), canvasSize.value.width, canvasSize.value.height) }
function resetView() { manualView = false; fitVisible(); scale.value = 1; pan.value = { x: 0, y: 0 } }
function setZoom(next: number, anchor: GraphPoint) { manualView = true; const result = zoomKnowledge(scale.value, pan.value, next, anchor); scale.value = result.scale; pan.value = result.pan }
function zoom(delta: number) { setZoom(Math.round((scale.value + delta) * 100) / 100, { x: canvasSize.value.width / 2, y: canvasSize.value.height / 2 }) }
function pointerPoint(event: { clientX: number; clientY: number }) { const matrix = svg.value?.getScreenCTM(); return matrix ? new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse()) : null }
function startDrag(event: PointerEvent) {
  if (event.button !== 0 || (event.target as Element).closest('[data-graph-node]')) return
  const start = pointerPoint(event); if (!start || !svg.value) return
  dragging.value = true; dragOrigin = { x: start.x, y: start.y, panX: pan.value.x, panY: pan.value.y, pointerId: event.pointerId }; svg.value.setPointerCapture(event.pointerId)
}
function moveDrag(event: PointerEvent) { if (!dragging.value || event.pointerId !== dragOrigin.pointerId) return; const current = pointerPoint(event); if (current) { manualView = true; pan.value = { x: dragOrigin.panX + current.x - dragOrigin.x, y: dragOrigin.panY + current.y - dragOrigin.y } } }
function stopDrag(event: PointerEvent) { if (event.pointerId !== dragOrigin.pointerId) return; dragging.value = false; if (svg.value?.hasPointerCapture(event.pointerId)) svg.value.releasePointerCapture(event.pointerId) }
function panWithKeyboard(event: KeyboardEvent) {
  if (event.target !== svg.value) return
  const directions: Record<string, [number, number]> = { ArrowLeft: [30, 0], ArrowRight: [-30, 0], ArrowUp: [0, 30], ArrowDown: [0, -30] }
  const move = directions[event.key]
  if (move) { event.preventDefault(); manualView = true; pan.value = { x: pan.value.x + move[0], y: pan.value.y + move[1] } }
  if (['Home', '0'].includes(event.key)) { event.preventDefault(); resetView() }
  if (['+', '='].includes(event.key)) { event.preventDefault(); zoom(.2) }
  if (event.key === '-') { event.preventDefault(); zoom(-.2) }
  if (event.key === 'Escape') { event.preventDefault(); clearSelection() }
}
function wheelZoom(event: WheelEvent) {
  event.preventDefault()
  if (!event.deltaY) return
  const anchor = pointerPoint(event)
  if (!anchor) return
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvasSize.value.height : 1
  const delta = Math.max(-240, Math.min(240, event.deltaY * unit))
  setZoom(scale.value * Math.exp(-delta * (event.ctrlKey ? .009 : .0025)), anchor)
}
watch([visibleNodes, visibleEdges], () => {
  const topology = JSON.stringify([visibleNodes.value.map(node => node.knowledge_id).sort(), visibleEdges.value.map(edge => `${edge.source}:${edge.target}:${edge.relation}`).sort()])
  const next = layoutKnowledge(visibleNodes.value, visibleEdges.value, points.value, !!layoutTopology && topology !== layoutTopology)
  layoutTopology = topology
  points.value = new Map([...points.value, ...next])
  if (!manualView) fitVisible()
})
watch([search, kind, position, depth, () => depth.value ? selectedId.value : ''], resetView, { flush: 'post' })
watch(() => props.refreshKey, () => { if (!loading.value) void loadGraph() })
watch(() => props.canRead, value => { if (value) void loadGraph(); else { requestId++; data.value = null; points.value = new Map(); clearSelection(); loading.value = false; error.value = '' } })
watch(svg, element => {
  resizeObserver?.disconnect()
  if (!element) return
  const resize = () => {
    const bounds = element.getBoundingClientRect()
    if (bounds.width < 1 || bounds.height < 1) return
    canvasSize.value = { width: bounds.width, height: bounds.height }
    if (!manualView) fitVisible()
  }
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(element)
  resize()
}, { flush: 'post' })
onMounted(loadGraph)
onBeforeUnmount(() => { requestId++; resizeObserver?.disconnect(); data.value = null; points.value = new Map() })
</script>

<style scoped>
.knowledge-graph { --graph-ink: #36443d; --graph-muted: #7d8580; display: flex; flex-direction: column; flex: 1; height: 100%; min-height: 0; min-width: 0; overflow: hidden; border: 1px solid #dfe3e0; border-radius: 5px; background: #fafbfa; color: var(--graph-ink); }
.graph-tools { display: flex; flex-shrink: 0; align-items: center; flex-wrap: wrap; gap: 8px; padding: 10px 14px; border-bottom: 1px solid #e3e6e3; background: #fcfdfc; }
.network-search { flex: 1; min-width: 190px; display: flex; align-items: center; gap: 9px; border: 1px solid #d4ddc9; background: white; border-radius: 5px; padding: 0 12px; }
.network-search > span { font-size: 22px; color: #8a967c; }
.network-search input { width: 100%; min-width: 0; border: 0; outline: 0; background: transparent; padding: 10px 0; color: var(--graph-ink); font: inherit; font-size: 12px; }
.graph-tools select { max-width: 210px; min-height: 38px; padding: 8px 27px 8px 10px; border: 1px solid #d4ddc9; border-radius: 5px; background: #fff; color: var(--graph-ink); font: inherit; font-size: 11px; }
.directory-toggle { display: flex; align-items: center; gap: 6px; flex-shrink: 0; min-height: 36px; border: 1px solid #dce2dc; border-radius: 4px; padding: 0 10px; background: #fff; color: #6b746c; font: inherit; font-size: 11px; cursor: pointer; }
.directory-toggle[aria-expanded="true"] { color: #35563f; background: #edf2ed; }
.directory-toggle > span { font-size: 19px; line-height: 1; }
.network-directory { display: flex; flex-direction: column; flex: 0 0 228px; min-width: 0; min-height: 0; border-right: 1px solid #e0e4e0; background: #f4f6f4; }
.directory-heading { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; height: 43px; padding: 0 14px; font-size: 11px; color: #6f7871; border-bottom: 1px solid #e6e9e6; }
.directory-heading small { margin-left: 6px; color: #a0a7a1; font-size: 10px; }
.directory-heading button { border: 0; background: transparent; color: #8c958c; font: inherit; font-size: 24px; line-height: 1; cursor: pointer; }
.directory-list { flex: 1; min-height: 0; overflow-y: auto; padding: 7px 5px 15px; scrollbar-width: thin; }
.directory-list details { margin-bottom: 4px; }
.directory-list summary { display: flex; align-items: center; gap: 7px; min-height: 32px; padding: 5px 9px; list-style: none; color: #69746b; font-size: 11px; cursor: pointer; }
.directory-list summary::-webkit-details-marker { display: none; }
.directory-list summary small { margin-left: auto; color: #a1a9a2; font-size: 9px; }
.directory-chevron { font-size: 17px; color: #9ca79d; transform: rotate(0); }
.directory-list details[open] .directory-chevron { transform: rotate(90deg); }
.directory-node { display: flex; align-items: start; gap: 7px; width: 100%; min-height: 31px; padding: 7px 10px 7px 23px; border: 0; border-radius: 4px; background: transparent; color: #7a847c; text-align: left; font: inherit; font-size: 10px; line-height: 1.65; cursor: pointer; }
.directory-node > span:last-child { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.directory-file { color: #adb6ad; flex-shrink: 0; font-size: 14px; line-height: 1.2; }
.directory-node:hover { background: #e9eeea; color: #4d5e51; }
.directory-node.selected { background: #dfeadf; color: #315f3e; }
.directory-empty { padding: 14px; color: #929b92; font-size: 11px; }
.inspector-reopen { margin-left: auto; pointer-events: auto; border: 1px solid #dce2dc; border-radius: 4px; padding: 6px 9px; background: #fff; color: #687d69; cursor: pointer; font: inherit; font-size: 10px; box-shadow: 0 2px 8px #26402b08; }
.graph-text-button { padding: 3px 0; border: 0; background: none; color: #466449; cursor: pointer; font: inherit; font-size: 11px; text-align: left; }
.graph-options { display: flex; flex-shrink: 0; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; padding: 8px 14px; border-bottom: 1px solid #e3e6e3; background: #fcfdfc; }
.edge-toggles { display: flex; flex-wrap: wrap; gap: 20px; }
.edge-toggles label { display: flex; align-items: center; gap: 7px; font-size: 11px; cursor: pointer; }
.edge-toggles input { accent-color: #406448; width: 12px; height: 12px; margin: 0; }
.edge-toggles label > span { color: #8d967f; font-variant-numeric: tabular-nums; }
.legend-line { width: 21px; height: 0; border-top: 1.5px solid #61826a; display: inline-block; }
.legend-line.related { border-top-style: dashed; border-top-color: #b89459; }
.scope-buttons { display: flex; padding: 3px; gap: 2px; border-radius: 5px; background: #eef2e7; }
.scope-buttons button { border: 0; background: transparent; border-radius: 3px; padding: 5px 10px; color: #7d886f; cursor: pointer; font: inherit; font-size: 11px; }
.scope-buttons button[aria-pressed="true"] { background: #fff; color: #2e5136; box-shadow: 0 1px 3px #31492614; }
.scope-buttons button:disabled { opacity: .4; cursor: default; }
.graph-status { padding: 11px 22px; background: #f4efde; color: #826934; line-height: 1.7; font-size: 11px; }
.status-dot { display: inline-block; width: 6px; height: 6px; margin-right: 8px; background: #b48e44; border-radius: 50%; }
.network-workspace { display: flex; flex: 1; min-height: 0; min-width: 0; position: relative; overflow: hidden; }
.network-stage { flex: 1; min-width: 0; min-height: 0; position: relative; background: #fafbfa; }
.network-caption { position: absolute; inset: 14px 16px auto; z-index: 1; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 5px; font-size: 11px; letter-spacing: .02em; color: #8c928e; pointer-events: none; }
.graph-canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; min-height: 0; cursor: grab; touch-action: none; overflow: hidden; }
.graph-canvas.is-dragging { cursor: grabbing; }
.network-edge { cursor: pointer; outline: none; }
.edge-visible { stroke: #698369; stroke-width: 1.2; opacity: .75; vector-effect: non-scaling-stroke; }
.edge-hit { stroke: transparent; stroke-width: 13; }
.network-edge.related .edge-visible { stroke: #9e8151; stroke-dasharray: 4 5; opacity: .7; }
.network-edge:hover .edge-visible, .network-edge.selected .edge-visible, .network-edge:focus-visible .edge-visible { stroke: #997039; stroke-width: 2.6; opacity: 1; }
.network-edge.dimmed .edge-visible { opacity: .10; }
.network-node { cursor: pointer; outline: none; }
.node-halo { fill: transparent; }
.node-dot { stroke: #fff; stroke-width: 1.5; }
.network-node text { fill: #526749; font-size: 12px; text-anchor: middle; paint-order: stroke; stroke: #fbfcf8; stroke-width: 4px; stroke-linejoin: round; }
.network-node.selected .node-halo { fill: #dce7cc; stroke: #9ab086; stroke-width: 1; }
.network-node:hover .node-halo { fill: #e9efdd; }
.network-node:focus-visible .node-halo { stroke: #a67535; stroke-width: 2; }
.network-node.dimmed { opacity: .25; }
.network-bottom { position: absolute; bottom: 12px; left: 12px; right: 12px; z-index: 2; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; pointer-events: none; }
.network-bottom > span { font-size: 9px; color: #98a18c; }
.graph-controls { display: flex; align-items: center; gap: 3px; padding: 5px; border: 1px solid #e0e4e0; border-radius: 5px; background: #ffffffed; box-shadow: 0 2px 10px #23332b08; pointer-events: auto; }
.graph-controls button { min-width: 29px; min-height: 29px; border: 1px solid #d7dfcf; border-radius: 4px; background: white; color: var(--graph-ink); cursor: pointer; font: inherit; font-size: 14px; }
.graph-controls output { min-width: 42px; text-align: center; font-size: 10px; color: var(--graph-muted); font-variant-numeric: tabular-nums; }
.graph-controls button:disabled { opacity: .4; cursor: default; }
.graph-controls .graph-reset { padding: 0 10px; margin-left: 5px; font-size: 10px; }
.network-inspector { position: absolute; top: 12px; right: 12px; bottom: 62px; width: 292px; max-width: calc(100% - 24px); min-width: 0; z-index: 3; padding: 18px; border: 1px solid #dfe5df; border-radius: 6px; background: #fffffff7; box-shadow: 0 8px 32px #213a2b12; overflow-y: auto; overflow-wrap: anywhere; scrollbar-width: thin; }
.inspector-kicker { display: flex; justify-content: space-between; align-items: center; font-size: 10px; letter-spacing: .07em; color: #8a947d; margin-bottom: 20px; }
.inspector-kicker button { font-size: 22px; padding: 0 3px; line-height: 1; }
.inspector-orbit { font: 70px Georgia, serif; color: #aab895; margin-top: 37px; }
.network-inspector h3 { margin: 12px 0 10px; font-family: 'Songti SC', 'Noto Serif CJK SC', serif; font-size: 19px; line-height: 1.6; font-weight: 500; color: #324c34; }
.inspector-muted { font-size: 11px; line-height: 1.9; color: #89927c; }
.inspector-legend { display: grid; gap: 12px; margin: 30px 0; font-size: 10px; color: #7c876e; }
.inspector-legend > span { display: flex; align-items: center; gap: 10px; }
.node-type { display: flex; align-items: center; gap: 7px; color: #78866b; font-size: 10px; }
.node-type i { width: 7px; height: 7px; border-radius: 50%; }
.read-knowledge { width: 100%; margin-top: 10px; border: 1px solid #335d40; border-radius: 4px; background: #31543d; color: #fff; display: flex; justify-content: space-between; padding: 10px 13px; cursor: pointer; font: inherit; font-size: 12px; }
.focus-actions { display: flex; gap: 6px; margin: 10px 0 23px; }
.focus-actions button { flex: 1; padding: 7px 4px; border: 1px solid #d3ddc6; border-radius: 4px; background: transparent; color: #667957; cursor: pointer; font: inherit; font-size: 10px; }
.connections-list { border-top: 1px solid #e0e6d6; padding: 14px 0 6px; }
.connections-list h4, .edge-evidence h4 { font-size: 11px; font-weight: 500; margin: 0 0 12px; }
.connections-list h4 span { margin-left: 8px; color: #93a182; font-weight: 400; }
.connection-item { display: flex; align-items: start; gap: 9px; padding-bottom: 11px; }
.connection-item button { border: 0; background: none; padding: 0; color: #4c6544; cursor: pointer; font: inherit; font-size: 11px; line-height: 1.7; text-align: left; }
.connection-item > button:first-child { flex: 1; }
.connection-item .connection-evidence { font-size: 9px; white-space: nowrap; color: #87966f; }
.relation-tag { display: inline-block; padding: 4px 7px; border: 1px solid #d1ddc4; border-radius: 3px; color: #638056; font-size: 10px; }
.relation-tag.related { border-color: #decdaa; color: #957443; }
.edge-endpoints { display: grid; gap: 4px; margin: 17px 0; }
.edge-endpoints button { padding: 8px 10px; border: 1px solid #dae2cf; border-radius: 4px; background: #fff; color: #536e45; font: inherit; font-size: 11px; text-align: left; cursor: pointer; }
.edge-endpoints > span { color: #9aac88; text-align: center; }
.relationship-reason { font-size: 11px; line-height: 1.8; color: #637456; white-space: pre-wrap; }
.related-boundary { padding: 8px 10px; background: #efe9d9; color: #917d54; font-size: 10px; line-height: 1.8; }
.relation-terms { display: flex; flex-wrap: wrap; gap: 5px; margin: 13px 0; }
.relation-terms span { padding: 3px 6px; font-size: 9px; background: #e8eddf; color: #81916d; border-radius: 3px; }
.edge-evidence { margin-top: 22px; }
.edge-evidence article { border-top: 1px solid #e0e5d8; padding: 10px 0; }
.edge-evidence blockquote { margin: 0 0 8px; padding: 0 0 0 10px; border-left: 2px solid #b3c397; font-size: 11px; line-height: 1.9; color: #6d7c60; white-space: pre-wrap; }
.accessible-nodes { font-size: 10px; color: #849171; }
.accessible-nodes summary { cursor: pointer; margin-bottom: 9px; }
.accessible-nodes button { display: block; width: 100%; border: 0; border-top: 1px solid #e2e8d9; background: none; text-align: left; padding: 8px 0; color: #526e46; font: inherit; font-size: 11px; cursor: pointer; }
.graph-footnote { display: flex; flex-shrink: 0; justify-content: space-between; flex-wrap: wrap; gap: 3px 12px; max-height: 64px; overflow-y: auto; border-top: 1px solid #e1e5e1; padding: 7px 14px; background: #f7f8f7; color: #8a928c; font-size: 10px; line-height: 1.6; }
.graph-no-match { padding: 14px 22px; color: #7f886e; background: #f3f5ec; font-size: 12px; }
.graph-no-match button { margin-left: 12px; }
.no-edge-note { position: absolute; left: 16px; right: 16px; bottom: 65px; font-size: 11px; color: #8c967d; line-height: 1.8; pointer-events: none; }
.graph-state { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; flex-direction: column; padding: 30px; text-align: center; }
.graph-state h3 { font-family: 'Songti SC', serif; font-size: 20px; font-weight: 500; margin: 18px 0 9px; }
.graph-state p { max-width: 400px; color: #7f8c72; font-size: 12px; line-height: 1.8; }
.graph-state button { border: 1px solid #cbd8bc; border-radius: 4px; background: #fff; padding: 8px 15px; color: #4d6a40; font: inherit; font-size: 12px; cursor: pointer; }
.empty-orbit { font-size: 65px; color: #a8b792; }
.network-loading { display: block; width: 29px; height: 29px; border: 1px solid #d8e3c9; border-top-color: #7c955f; border-radius: 50%; animation: orbit 1s linear infinite; }
.graph-failure h3 { color: #896e42; }
button:focus-visible, select:focus-visible, .graph-canvas:focus-visible, summary:focus-visible { outline: 2px solid #a57738; outline-offset: 3px; }
.network-search:focus-within { outline: 2px solid #a57738; outline-offset: 2px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@keyframes orbit { to { transform: rotate(360deg); } }
@media (max-width: 1000px) {
  .network-directory { flex-basis: 200px; }
  .network-inspector { width: 260px; }
  .graph-tools select { max-width: 160px; }
}
@media (max-width: 740px) {
  .graph-tools, .graph-options { padding: 8px 10px; gap: 7px; }
  .network-search { min-width: 140px; }
  .graph-tools select { max-width: 145px; min-height: 31px; padding-top: 5px; padding-bottom: 5px; }
  .edge-toggles { gap: 12px; }
  .edge-toggles label { font-size: 10px; }
  .network-directory { position: absolute; z-index: 5; left: 0; top: 0; bottom: 0; width: min(280px, calc(100% - 36px)); box-shadow: 8px 0 25px #26372c18; }
  .network-inspector { left: 10px; right: 10px; top: 10px; bottom: 58px; width: auto; max-width: none; padding: 15px; }
  .network-caption { inset: 10px 12px auto; font-size: 10px; }
  .network-bottom { left: 8px; right: 8px; bottom: 8px; }
  .network-bottom > span { font-size: 8px; }
  .network-inspector h3 { font-size: 17px; }
  .graph-footnote { padding: 6px 10px; font-size: 9px; }
  .graph-footnote > span:last-child { display: none; }
  .network-node text { font-size: 14px; }
}
@media (prefers-reduced-motion: reduce) { .network-loading { animation: none; } }
</style>
