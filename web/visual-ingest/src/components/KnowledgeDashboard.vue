<template>
  <main id="knowledge-main" class="portal-main knowledge-dashboard" :class="{ 'graph-dashboard': view === 'graph' }" tabindex="-1">
    <header v-if="view === 'overview'" class="page-heading dashboard-heading">
      <div><h1>知识目录</h1><p>浏览和查找已发布的公司知识。</p></div>
      <button class="button refresh-button" :disabled="loading || refreshing" @click="$emit('refresh')">{{ loading || refreshing ? '正在更新' : '刷新' }}</button>
    </header>
    <header v-else class="graph-page-toolbar"><h1>知识图谱</h1><button class="button refresh-button" :disabled="refreshing" @click="$emit('refresh')">{{ refreshing ? '正在更新' : '刷新' }}</button></header>
    <div v-if="error" class="message error" role="alert">{{ error }}<button class="text-button" @click="loadCatalog">重新读取</button></div>
    <div v-if="overviewError" class="message error" role="alert">上传统计暂不可用：{{ overviewError }}</div>
    <template v-if="view === 'overview'">
      <div class="wiki-summary" aria-label="知识统计" :aria-busy="loading">
        <span><strong>{{ number(catalog?.counts.published_assets ?? overview?.counts.published_assets) }}</strong> 项正式知识</span>
        <button class="text-button" @click="$emit('processing', '')">{{ processingScope }}提交 {{ number(overview?.counts.receipts) }} 批</button>
        <button class="text-button" @click="$emit('processing', 'needs_review')">待核对 {{ number(overview?.counts.needs_review) }}</button>
        <button class="text-button" @click="$emit('processing', 'failed')">处理失败 {{ number(overview?.counts.failed) }}</button>
        <button v-if="overview?.receipt_counts" class="text-button" @click="$emit('processing', '')">原件核验异常 {{ overview.receipt_counts.failed + overview.receipt_counts.rejected }}</button>
      </div>
      <nav class="wiki-categories" aria-label="知识分类">
        <button :aria-current="!filters.kind ? 'true' : undefined" @click="chooseKind('')">全部 <small>{{ number(catalog?.counts.published_assets) }}</small></button>
        <button v-for="group in kindGroups" :key="group.key" :aria-current="filters.kind === group.key ? 'true' : undefined" :disabled="!catalog" @click="chooseKind(group.key)">{{ group.label }} <small>{{ catalog ? number(group.count) : '—' }}</small></button>
      </nav>
    </template>

    <KnowledgeGraph v-if="view === 'graph'" :refresh-key="refreshKey" :can-read="canRead" @select="openKnowledge" @error="emit('error', $event)" />
    <section v-else class="dashboard-panel library-panel" aria-labelledby="knowledge-library-title">
      <h2 id="knowledge-library-title" class="sr-only">知识文章</h2>
      <form class="catalog-filters" role="search" @submit.prevent="runSearch"><label class="catalog-search"><span aria-hidden="true">⌕</span><span class="sr-only">搜索知识内容</span><input v-model="searchInput" type="search" maxlength="4000" placeholder="输入问题或关键词，搜索知识内容…"><button type="submit" class="text-button" :disabled="!canRead || searchLoading">{{ searchLoading ? '检索中…' : '搜索' }}</button></label><label><span class="sr-only">上传员工岗位</span><select :disabled="!!semanticQuery" v-model="filters.uploader_position" @change="applyFilters"><option value="">全部上传岗位</option><option v-for="group in positionGroups" :key="group.key" :value="group.key">{{ group.label }}</option><option value="unknown">待补充岗位</option></select></label><button v-if="hasFilters || semanticQuery" type="button" class="text-button" @click="resetFilters">返回目录</button></form>
      <div v-if="!semanticQuery && hasFilters && catalog" class="filter-result" role="status">当前筛选 {{ catalog.total }} 项 <span>· 公司共 {{ catalog.counts.published_assets }} 项正式知识</span></div>
      <div v-if="!canRead" class="list-state"><h3>当前账号未开放知识目录</h3><p>管理员可通过现有 MCP 角色配置知识查看权限。</p></div>
      <section v-else-if="semanticQuery" class="semantic-results" aria-label="语义检索结果" :aria-busy="searchLoading">
        <p class="filter-result" role="status">全库搜索：{{ semanticQuery }}<span v-if="searchResults"> · 按相关性展示 {{ searchResults.result_count }} 项</span></p>
        <div v-if="searchLoading" class="list-state" role="status">正在检索知识内容…</div>
        <div v-else-if="searchError" class="message error" role="alert">{{ searchError }} <button class="text-button" @click="runSearch">重试</button></div>
        <template v-else-if="searchResults">
          <p v-if="searchResults.results.some(item => item.rerank_mode === 'fallback_rrf')" class="filter-result">精排暂不可用，当前显示初步匹配结果，请核对原文。</p>
          <div v-if="!searchResults.result_count" class="list-state"><h3>没有找到匹配资料</h3><p>尝试补充具体场景或换一种问法。</p></div>
          <ol v-else class="semantic-list"><li v-for="asset in searchResults.results" :key="asset.knowledge_id"><button class="asset-title" @click="openKnowledge(asset.knowledge_id)">{{ asset.title }}</button><p class="semantic-snippet">{{ asset.snippet }}</p><button class="text-button" :aria-label="`阅读 ${asset.title}`" @click="openKnowledge(asset.knowledge_id)">阅读全文</button></li></ol>
        </template>
      </section>
      <div v-else-if="loading && !catalog" class="list-state" role="status"><span class="loading-dot" /><h3>正在读取知识资产</h3></div>
      <div v-else-if="!catalog && !loading" class="list-state"><h3>暂未取得知识目录</h3><p>请重试连接，统计缺失不会作为零展示。</p></div>
      <template v-else-if="catalog">
        <div v-if="!catalog.total" class="list-state"><span class="empty-symbol" aria-hidden="true">▤</span><h3>{{ hasFilters ? '没有符合筛选条件的知识' : '暂无已发布知识' }}</h3><p>{{ hasFilters ? '调整关键词、岗位或类型后再试。' : '上传资料通过清洗和发布检查后，将自动显示在这里。' }}</p><button v-if="hasFilters" class="button subtle" @click="resetFilters">清除筛选</button></div>
        <div v-else class="table-wrap" tabindex="0" aria-label="正式知识资产列表" :aria-busy="loading"><table class="assets-table"><thead><tr><th scope="col">文章</th><th scope="col">类型</th><th scope="col">上传员工岗位</th><th scope="col">发布时间</th><th scope="col"><span class="sr-only">阅读</span></th></tr></thead><tbody><tr v-for="asset in catalog.items" :key="asset.knowledge_id"><td><button class="asset-title" @click="openKnowledge(asset.knowledge_id)"><span><strong>{{ asset.title }}</strong><small v-if="asset.scenarios.length">{{ asset.scenarios.slice(0, 2).join(' / ') }}</small></span></button></td><td><span class="asset-kind">{{ kindLabel(asset.kind) }}</span></td><td><span :class="{ 'missing-position': !asset.uploader_position }">{{ asset.uploader_position || '待补充岗位' }}</span></td><td class="time-cell">{{ date(asset.published_at) }}</td><td><button class="text-button" :aria-label="`阅读 ${asset.title}`" @click="openKnowledge(asset.knowledge_id)">阅读</button></td></tr></tbody></table></div>
        <div v-if="view === 'overview' && catalog.total" class="list-footer"><span>显示 {{ catalog.offset + 1 }}–{{ catalog.offset + catalog.items.length }} / {{ catalog.total }} 项</span><el-pagination :current-page="page" :page-size="20" :total="catalog.total" layout="prev, pager, next" prev-text="上一页" next-text="下一页" :disabled="loading" @current-change="changePage" /></div>
      </template>
    </section>
    <details v-if="view === 'overview'" class="wiki-statistics">
      <summary>岗位统计 <span>{{ number(catalog?.counts.positions) }} 个已归属岗位</span></summary>
      <p>按上传员工所属岗位统计。</p>
      <div class="position-links"><button v-for="group in positionGroups" :key="group.key" class="text-button" @click="choosePosition(group.key)">{{ group.label }} · {{ group.count }} 项</button><button v-if="catalog?.counts.unknown_position" class="text-button" @click="choosePosition('unknown')">待补充岗位 · {{ catalog.counts.unknown_position }} 项</button></div>
    </details>
    <footer v-if="view === 'overview'" class="dashboard-footnote"><span>仅展示当前已发布版本，更新版本不重复计数。</span><span>{{ catalog ? `更新于 ${date(catalog.generated_at)} · 每 30 秒自动刷新` : '' }}</span></footer>

    <el-drawer v-model="readingOpen" class="knowledge-detail" :title="reading?.title || '阅读正式知识'" size="min(860px, 100vw)" :destroy-on-close="true" @closed="clearReading">
      <div v-if="readingLoading && !reading" class="list-state" role="status"><span class="loading-dot" />正在读取当前正式版本…</div><div v-else-if="readingError" class="message error" role="alert">{{ readingError }}</div><template v-else-if="reading"><div class="reading-meta"><span class="asset-kind">{{ kindLabel(reading.kind) }}</span><span>全员可查</span><span>版本 {{ reading.revision }}</span></div><p class="reading-position">上传员工岗位：{{ reading.uploader_position || '待补充岗位' }}</p><pre class="draft-content reading-content">{{ reading.content }}</pre><section class="reading-connections" aria-label="相关条目"><h3>相关条目 <span>{{ readingLinks.length }}</span></h3><p v-if="readingGraphError">关联暂时无法读取，稍后会自动重试。</p><p v-else-if="!readingGraph || readingGraph.status === 'building'">正在分析已发布知识之间的关联…</p><p v-else-if="!readingLinks.length">暂未找到这篇知识的引用或内容关联。</p><button v-for="link in readingLinks" :key="link.id" type="button" @click="openKnowledge(link.target)"><span>{{ link.label }}</span><strong>{{ link.title }} ↗</strong><small>{{ link.reason }}</small></button><p v-if="readingLinks.length" class="connection-caution">内容相关是自动推荐，不代表原文引用或事实依赖。</p></section><p class="preview-meta">知识编号：{{ reading.knowledge_id }}</p></template>
    </el-drawer>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { getCatalog, getPublishedKnowledge, searchKnowledge, PortalError, type KnowledgeSearchResult, type CatalogGroup, type KnowledgeCatalog, type Overview, type PortalSession, type PublishedKnowledge, getGraph, type GraphData } from '../api/knowledgePortal'
import KnowledgeGraph from './KnowledgeGraph.vue'
import '../knowledge-dashboard.css'

const props = defineProps<{ view: 'overview' | 'graph'; session: PortalSession; overview: Overview | null; overviewError: string; refreshKey: number; refreshing: boolean }>()
const emit = defineEmits<{ refresh: []; error: [error: unknown]; processing: [status: string] }>()
const catalog = ref<KnowledgeCatalog | null>(null)
const loading = ref(false)
const error = ref('')
const searchInput = ref('')
const semanticQuery = ref('')
const searchResults = ref<KnowledgeSearchResult | null>(null)
const searchLoading = ref(false)
const searchError = ref('')
let searchRequest = 0
let searchController: AbortController | null = null
const filters = reactive({ q: '', kind: '', uploader_position: '' })
const page = ref(1)
const readingOpen = ref(false)
const reading = ref<PublishedKnowledge | null>(null)
const readingLoading = ref(false)
const readingError = ref('')
const readingGraph = ref<GraphData | null>(null)
const readingGraphError = ref(false)
const readingLinks = computed(() => {
  if (!reading.value || !readingGraph.value) return []
  const id = reading.value.knowledge_id
  const nodes = new Map(readingGraph.value.nodes.map(node => [node.knowledge_id, node]))
  if (nodes.get(id)?.revision !== reading.value.revision) return []
  return readingGraph.value.edges.filter(edge => edge.source === id || edge.target === id).map(edge => {
    const target = edge.source === id ? edge.target : edge.source
    return { id: edge.id, target, title: nodes.get(target)?.title || '关联知识', label: `${edge.directed ? edge.source === id ? '出链' : '入链' : '相关'} · ${edge.label}`, reason: edge.reason }
  })
})
let requestId = 0
let readingRequest = 0
const canRead = computed(() => props.session.permissions.some(value => ['company_knowledge.read', 'company_knowledge.review'].includes(value)))
const processingScope = computed(() => props.overview?.scope === 'company' ? '公司' : '我的')
const defaults: CatalogGroup[] = [{ key: 'sop', label: 'SOP / 流程', count: 0 }, { key: 'skill', label: 'Skill', count: 0 }, { key: 'case', label: '案例', count: 0 }, { key: 'template', label: '模板', count: 0 }, { key: 'method', label: '方法', count: 0 }, { key: 'prompt', label: '提示词', count: 0 }, { key: 'policy', label: '规则', count: 0 }, { key: 'reference', label: '参考', count: 0 }, { key: 'other', label: '其他知识', count: 0 }]
const kindGroups = computed(() => catalog.value?.by_kind || defaults)
const positionGroups = computed(() => catalog.value?.by_uploader_position.filter(group => group.key !== 'unknown') || [])
const hasFilters = computed(() => !!(filters.q || filters.kind || filters.uploader_position))
const number = (value: number | null | undefined) => value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
const date = (value: string | null) => { const parsed = value ? new Date(value) : null; return parsed && !Number.isNaN(parsed.valueOf()) ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(parsed) : '时间未记录' }
const kindLabel = (key: string) => kindGroups.value.find(group => group.key === key)?.label || key
async function loadCatalog() {
  if (!canRead.value || props.view !== 'overview') return
  const current = ++requestId
  loading.value = true
  error.value = ''
  try {
    const result = await getCatalog({ ...filters, limit: 20, offset: (page.value - 1) * 20 })
    if (current !== requestId) return
    catalog.value = result
    if (readingOpen.value && reading.value && !readingLoading.value) void openKnowledge(reading.value.knowledge_id)
    if (page.value > 1 && result.total <= result.offset) { page.value = Math.max(1, Math.ceil(result.total / 20)); void loadCatalog() }
  } catch (reason) {
    if (current !== requestId) return
    catalog.value = null
    if (reason instanceof PortalError && [401, 403].includes(reason.status)) { clearSearch(); readingOpen.value = false; clearReading() }
    error.value = reason instanceof Error ? reason.message : '知识目录暂不可用，请稍后重试。'
    emit('error', reason)
  } finally { if (current === requestId) loading.value = false }
}
function clearSearch() { searchRequest++; searchController?.abort(); searchController = null; semanticQuery.value = ''; searchResults.value = null; searchLoading.value = false; searchError.value = '' }
async function runSearch() {
  const query = searchInput.value.trim()
  if (!query) { resetFilters(); return }
  if (!canRead.value) return
  clearSearch()
  const current = ++searchRequest
  searchController = new AbortController()
  semanticQuery.value = query; searchLoading.value = true
  filters.q = ''; filters.kind = ''; filters.uploader_position = ''
  try {
    const result = await searchKnowledge(query, searchController.signal)
    if (current === searchRequest) searchResults.value = result
  } catch (reason) {
    if (current !== searchRequest) return
    searchError.value = reason instanceof Error ? reason.message : '检索暂不可用，请重试。'
    if (reason instanceof PortalError && [401, 403].includes(reason.status)) { readingOpen.value = false; clearReading() }
    emit('error', reason)
  } finally { if (current === searchRequest) searchLoading.value = false }
}
function applyFilters() { clearSearch(); searchInput.value = ''; filters.q = ''; page.value = 1; catalog.value = null; void loadCatalog() }
function chooseKind(key: string) { filters.kind = filters.kind === key ? '' : key; applyFilters() }
function choosePosition(key: string) { filters.uploader_position = filters.uploader_position === key ? '' : key; applyFilters() }
function resetFilters() { searchInput.value = ''; filters.q = ''; filters.kind = ''; filters.uploader_position = ''; applyFilters() }
function changePage(value: number) { page.value = value; void loadCatalog() }
function clearReading() { if (readingOpen.value) return; readingRequest++; reading.value = null; readingError.value = ''; readingLoading.value = false; readingGraph.value = null; readingGraphError.value = false }
async function openKnowledge(id: string) {
  if (!canRead.value) return
  const current = ++readingRequest
  const sameKnowledge = readingOpen.value && reading.value?.knowledge_id === id
  if (!sameKnowledge) { reading.value = null; readingGraph.value = null }
  readingOpen.value = true; readingGraphError.value = false; readingError.value = ''; readingLoading.value = true
  const graphRead = getGraph().then(result => { if (current === readingRequest) readingGraph.value = result }).catch(reason => { if (current === readingRequest) { readingGraphError.value = true; emit('error', reason) } })
  try { const result = await getPublishedKnowledge(id); if (current === readingRequest) reading.value = result }
  catch (reason) { if (current === readingRequest) { reading.value = null; readingGraph.value = null; readingError.value = reason instanceof Error ? reason.message : '正文暂不可用'; emit('error', reason) } }
  finally { if (current === readingRequest) readingLoading.value = false; await graphRead }
}
watch(() => props.refreshKey, () => { if (!loading.value) void loadCatalog(); if (props.view === 'graph' && readingOpen.value && reading.value && !readingLoading.value) void openKnowledge(reading.value.knowledge_id) })
watch(() => props.view, () => { clearSearch(); requestId++; loading.value = false; error.value = ''; page.value = 1; catalog.value = null; void loadCatalog() })
watch(canRead, value => { if (!value) { clearSearch(); requestId++; loading.value = false; catalog.value = null; readingOpen.value = false; clearReading() } else void loadCatalog() })
onMounted(loadCatalog)
onBeforeUnmount(() => { clearSearch(); requestId++; readingRequest++; catalog.value = null; reading.value = null; readingGraph.value = null })
</script>
