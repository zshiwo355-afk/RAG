<template>
  <main id="knowledge-main" class="portal-main knowledge-dashboard" :class="{ 'graph-dashboard': view === 'graph' }" tabindex="-1">
    <header v-if="view === 'overview'" class="page-heading dashboard-heading">
      <div><button v-if="!directoryOpen" class="text-button role-back" @click="showDirectory">← 岗位目录</button><h1>{{ directoryOpen ? '岗位知识' : scopeTitle }}</h1><p>{{ directoryOpen ? '先选择部门，再查看岗位的 岗位流程、智能体技能与工作案例。' : selectedRole ? selectedRole.department + ' · 岗位资料持续积累' : filters.role_id === 'unknown' ? '尚未对应标准岗位的历史资料，仍可查阅。' : '浏览和查找全公司的当前正式知识。' }}</p></div>
      <button class="button refresh-button" :disabled="loading || rolesLoading || refreshing" @click="$emit('refresh')">{{ loading || rolesLoading || refreshing ? '正在更新' : '刷新' }}</button>
    </header>
    <header v-else class="graph-page-toolbar"><h1>知识图谱</h1><button class="button refresh-button" :disabled="refreshing" @click="$emit('refresh')">{{ refreshing ? '正在更新' : '刷新' }}</button></header>
    <div v-if="overviewError" class="message error" role="alert">上传统计暂不可用：{{ overviewError }}</div>
    <template v-if="view === 'overview'">
      <div class="wiki-summary" aria-label="知识统计" :aria-busy="loading">
        <span><strong>{{ number(catalog?.counts.published_assets ?? overview?.counts.published_assets) }}</strong> 项正式知识</span>
        <button class="text-button" @click="$emit('processing', '')">{{ processingScope }}提交 {{ number(overview?.counts.receipts) }} 批</button>
        <button class="text-button" @click="$emit('processing', 'needs_review')">待核对 {{ number(overview?.counts.needs_review) }}</button>
        <button class="text-button" @click="$emit('processing', 'failed')">处理失败 {{ number(overview?.counts.failed) }}</button>
      </div>
      <nav class="library-navigation" aria-label="浏览范围"><button class="text-button" :aria-current="directoryOpen ? 'page' : undefined" @click="showDirectory">岗位目录 <small v-if="roles">{{ roles.length }}</small></button><button class="text-button" :aria-current="!directoryOpen && !filters.role_id ? 'page' : undefined" @click="browseRole('')">全库浏览</button><button class="text-button" :aria-current="!directoryOpen && filters.role_id === 'unknown' ? 'page' : undefined" @click="browseRole('unknown')">未归属资料</button></nav>
    </template>

    <KeepAlive><KnowledgeGraph v-if="view === 'graph'" :refresh-key="refreshKey" :can-read="canRead" :fetch-graph="readGraph" @select="openKnowledge" @error="emit('error', $event)" /></KeepAlive>
    <template v-if="view === 'overview'">
    <div v-if="!canRead" class="list-state"><h3>当前账号未开放知识目录</h3><p>请联系管理员配置知识查看权限。</p></div>
    <section v-else-if="directoryOpen" class="role-directory" aria-label="按部门和岗位浏览" :aria-busy="rolesLoading">
      <div v-if="rolesError" class="message error" role="alert">{{ rolesError }} <button class="text-button" @click="loadRoles">重新读取</button></div>
      <div v-else-if="rolesLoading && !roles" class="list-state" role="status"><span class="loading-dot" /><h3>正在读取岗位目录…</h3></div>
      <div v-else-if="roles && !roles.length" class="list-state"><h3>岗位目录待建立</h3><p>正式岗位流程接入后，对应部门和岗位会显示在这里。</p><button class="button subtle" @click="browseRole('')">先浏览已有知识</button></div>
      <details v-for="department in departments" :key="department.name" class="department-section" :open="expandedDepartments.has(department.name)" @toggle="rememberDepartment(department.name, $event)">
        <summary><h2>{{ department.name }}</h2><span class="department-count">{{ department.roles.length }} 个岗位</span><span class="department-toggle" aria-hidden="true">{{ expandedDepartments.has(department.name) ? '收起岗位' : '查看岗位' }}<span class="department-chevron" /></span></summary>
        <div class="department-roles"><button v-for="role in department.roles" :key="role.role_id" class="role-entry" @click="browseRole(role.role_id)"><span class="role-entry-title"><strong>{{ role.name }}</strong><span aria-hidden="true">↗</span></span><span class="role-entry-counts"><span>岗位流程 <b>{{ role.counts.sop }}</b></span><span>智能体技能 <b>{{ role.counts.skill }}</b></span><span>其他 <b>{{ role.counts.other }}</b></span></span><small>{{ role.total ? `${role.total} 项岗位资料` : '资料待补充' }}</small></button></div>
      </details>
    </section>
    <section v-else class="dashboard-panel library-panel" aria-labelledby="knowledge-library-title">
      <h2 id="knowledge-library-title" class="sr-only">{{ scopeTitle }}的资料</h2>
      <nav class="wiki-categories role-categories" aria-label="岗位资料分类"><button v-if="!selectedRole" :aria-current="!filters.category ? 'true' : undefined" @click="chooseCategory('')">全部</button><button v-for="group in categories" :key="group.key" :aria-current="filters.category === group.key ? 'true' : undefined" @click="chooseCategory(group.key)">{{ group.label }} <small v-if="selectedRole">{{ selectedRole.counts[group.key] }}</small></button></nav>
      <form class="catalog-filters" role="search" @submit.prevent="runSearch"><label class="catalog-search"><span aria-hidden="true">⌕</span><span class="sr-only">搜索{{ scopeTitle }}的知识内容</span><input v-model="searchInput" type="search" :maxlength="filters.role_id === 'unknown' ? 200 : 4000" :placeholder="filters.role_id === 'unknown' ? '按标题查找未归属资料…' : `在${scopeTitle}中搜索问题或关键词…`"><button type="submit" class="text-button" :disabled="searchLoading">{{ searchLoading ? '检索中…' : '搜索' }}</button></label><button v-if="semanticQuery || filters.q" type="button" class="text-button" @click="resetFilters">清除搜索</button></form>
      <div v-if="error" class="message error" role="alert">{{ error }} <button class="text-button" @click="loadCatalog">重新读取</button></div>
      <section v-if="semanticQuery" class="semantic-results" aria-label="语义检索结果" :aria-busy="searchLoading">
        <p class="filter-result" role="status">{{ scopeTitle }}{{ categoryTitle ? ' / ' + categoryTitle : '' }}：{{ semanticQuery }}<span v-if="searchResults"> · 按相关性展示 {{ searchResults.result_count }} 项</span></p>
        <div v-if="searchLoading" class="list-state" role="status">正在检索知识内容…</div>
        <div v-else-if="searchError" class="message error" role="alert">{{ searchError }} <button class="text-button" @click="runSearch">重试</button></div>
        <template v-else-if="searchResults"><div v-if="!searchResults.result_count" class="list-state" role="status"><h3>未找到足够相关资料</h3><p>补充具体场景或换一种问法，也可以返回当前分类继续浏览。</p><button class="button subtle" @click="resetFilters">浏览当前分类</button></div><ol v-else class="semantic-list"><li v-for="asset in searchResults.results" :key="asset.knowledge_id"><button class="asset-title" @click="openKnowledge(asset.knowledge_id)">{{ asset.title }}</button><KnowledgeMarkdown class="semantic-snippet" tabindex="0" aria-label="检索摘要" :content="asset.snippet" /><button class="text-button" :aria-label="`阅读 ${asset.title}`" @click="openKnowledge(asset.knowledge_id)">阅读全文</button></li></ol></template>
      </section>
      <div v-else-if="loading && !catalog" class="list-state" role="status"><span class="loading-dot" /><h3>正在读取知识资产…</h3></div>
      <div v-else-if="!catalog && !loading && !error" class="list-state"><h3>暂未取得知识目录</h3><p>请重试连接，统计缺失不会作为零展示。</p></div>
      <template v-else-if="catalog">
        <p class="filter-result">{{ categoryTitle || '当前范围' }} · {{ catalog.total }} 项<span v-if="filters.q"> · 标题包含「{{ filters.q }}」</span></p>
        <div v-if="!catalog.total" class="list-state"><span class="empty-symbol" aria-hidden="true">▤</span><h3>暂无{{ categoryTitle || '正式' }}资料</h3><p>{{ selectedRole ? '这个岗位已在目录中。对应资料发布后会自动显示在这里。' : '这个范围暂时没有资料，可以切换分类或浏览全库。' }}</p></div>
        <div v-else class="table-wrap" tabindex="0" aria-label="正式知识资产列表" :aria-busy="loading"><table class="assets-table"><thead><tr><th scope="col">资料名称</th><th scope="col">类型</th><th scope="col">所属岗位</th><th scope="col">发布时间</th><th scope="col"><span class="sr-only">阅读</span></th></tr></thead><tbody><tr v-for="asset in catalog.items" :key="asset.knowledge_id"><td><button class="asset-title" @click="openKnowledge(asset.knowledge_id)"><span><strong>{{ asset.title }}</strong><small v-if="asset.scenarios?.length">{{ asset.scenarios.slice(0, 2).join(' / ') }}</small></span></button></td><td><span class="asset-kind">{{ kindLabel(asset.kind) }}</span></td><td><span :class="{ 'missing-position': !asset.role_id }">{{ asset.role_name ? [asset.department, asset.role_name].filter(Boolean).join(' / ') : '未归属' }}</span></td><td class="time-cell">{{ date(asset.published_at) }}</td><td><button class="text-button" :aria-label="`阅读 ${asset.title}`" @click="openKnowledge(asset.knowledge_id)">阅读</button></td></tr></tbody></table></div>
        <div v-if="catalog.total" class="list-footer"><span>显示 {{ catalog.offset + 1 }}–{{ catalog.offset + catalog.items.length }} / {{ catalog.total }} 项</span><el-pagination :current-page="page" :page-size="20" :total="catalog.total" layout="prev, pager, next" prev-text="上一页" next-text="下一页" :disabled="loading" @current-change="changePage" /></div>
      </template>
    </section>
    </template>
    <footer v-if="view === 'overview'" class="dashboard-footnote"><span>岗位以岗位流程中的部门与岗位为准；资料按当前正式版本计数。</span><span>每 5 分钟检查更新，也可手动刷新</span></footer>

    <el-drawer v-model="readingOpen" class="knowledge-detail" :title="reading?.title || '阅读正式知识'" size="min(980px, 100vw)" :destroy-on-close="true" @closed="clearReading">
      <div v-if="readingLoading && !reading" class="list-state" role="status"><span class="loading-dot" />正在读取当前正式版本…</div><div v-else-if="readingError" class="message error" role="alert">{{ readingError }}</div>
      <template v-else-if="reading">
        <div class="reading-meta"><button class="text-button" :disabled="readingLoading" @click="openKnowledge(reading.knowledge_id)">{{ readingLoading ? '正在更新…' : '刷新正文' }}</button><span class="asset-kind">{{ kindLabel(reading.kind) }}</span><span>版本 {{ reading.revision }}</span><span v-if="reading.kind === 'skill'">{{ hasSkillPackage ? '完整智能体技能包' : '方法说明' }}</span></div>
        <p class="reading-position">所属岗位：{{ reading.role_name ? [reading.department, reading.role_name].filter(Boolean).join(' / ') : '未归属' }}<span v-if="reading.uploader_position"> · 上传员工岗位：{{ reading.uploader_position }}</span></p>
        <section class="reading-files" aria-label="预览与下载">
          <div class="reading-file-actions"><button v-if="previewFile" class="button subtle" :disabled="!!fileLoading" @click="accessFile(previewFile, 'inline')">{{ fileLoading === previewFile.file_id ? '正在读取…' : '文档预览' }}</button><button v-if="previewHtml" class="text-button" @click="clearFileAccess">返回正文</button></div>
          <ul v-if="downloadFiles.length" class="download-files"><li v-for="file in downloadFiles" :key="file.file_id"><span><strong>{{ file.filename }}</strong><small>{{ file.purpose === 'skill' ? '完整智能体技能包' : '原件' }} · {{ fileSize(file.byte_length) }}</small></span><button class="text-button" :disabled="!!fileLoading" :aria-label="`下载 ${file.filename}`" @click="accessFile(file, 'attachment')">{{ fileLoading === file.file_id ? '准备中…' : '下载' }}</button></li></ul>
          <p v-else class="file-note">暂未提供可下载文件，以下正文仍可阅读。</p>
          <p v-if="reading.kind === 'skill' && !hasSkillPackage" class="file-note">当前为方法说明，尚未提供经核验的完整智能体技能包。</p>
          <p v-if="fileError" class="message error" role="alert">{{ fileError }}</p><p v-if="fileNotice" class="file-note" role="status">{{ fileNotice }}</p>
        </section>
        <iframe v-if="previewHtml" :key="previewHtml" :srcdoc="previewHtml" class="document-preview" :title="`${reading.title}文档预览`" sandbox="" referrerpolicy="no-referrer" @error="previewFailed" />
        <KnowledgeMarkdown v-else :content="reading.content" />
        <section class="reading-connections" aria-label="相关条目"><h3>相关条目 <span v-if="readingLinksOpen">{{ readingLinks.length }}</span></h3><button v-if="!readingLinksOpen" type="button" class="text-button" @click="loadReadingLinks">查看相关资料</button><template v-else><p v-if="readingLinksLoading">正在读取相关资料…</p><p v-else-if="readingGraphError">关联暂时无法读取。<button class="text-button" @click="loadReadingLinks">重新读取</button></p><p v-else-if="!readingGraph || readingGraph.status === 'building'">关联正在整理，可以稍后重新读取。<button class="text-button" @click="loadReadingLinks">重新读取</button></p><p v-else-if="!readingLinks.length">暂未找到这篇知识的引用或内容关联。</p><button v-for="link in readingLinks" :key="link.id" type="button" @click="openKnowledge(link.target)"><span>{{ link.label }}</span><strong>{{ link.title }} ↗</strong><small>{{ link.reason }}</small></button><p v-if="readingLinks.length" class="connection-caution">内容相关是自动推荐，不代表原文引用或事实依赖。</p></template></section><p class="preview-meta">知识编号：{{ reading.knowledge_id }}</p>
      </template>
    </el-drawer>
  </main>
</template>

<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { getCatalog, getRoles, getPublishedKnowledge, getKnowledgeFile, safeFileUrl, searchKnowledge, PortalError, type KnowledgeSearchResult, type KnowledgeCategory, type KnowledgeFile, type KnowledgeRole, type KnowledgeCatalog, type Overview, type PortalSession, type PublishedKnowledge, getGraph, knowledgeKindName, type GraphData } from '../api/knowledgePortal'
import KnowledgeGraph from './KnowledgeGraph.vue'
import KnowledgeMarkdown from './KnowledgeMarkdown.vue'
import '../knowledge-dashboard.css'

const props = defineProps<{ view: 'overview' | 'graph'; session: PortalSession; overview: Overview | null; overviewError: string; refreshKey: number; refreshing: boolean }>()
const emit = defineEmits<{ refresh: []; error: [error: unknown]; processing: [status: string]; graphPending: [pending: boolean] }>()
let active = true
const directoryOpen = ref(true)
const expandedDepartments = ref(new Set<string>())
function rememberDepartment(name: string, event: Event) {
  if ((event.target as HTMLDetailsElement).open) expandedDepartments.value.add(name)
  else expandedDepartments.value.delete(name)
}
const roles = ref<KnowledgeRole[] | null>(null)
const rolesLoading = ref(false)
const rolesError = ref('')
let rolesRequest = 0
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
const filters = reactive({ q: '', role_id: '', category: '' as KnowledgeCategory | '' })
const page = ref(1)
const selectedRole = computed(() => roles.value?.find(role => role.role_id === filters.role_id))
const scopeTitle = computed(() => selectedRole.value?.name || (filters.role_id === 'unknown' ? '未归属资料' : filters.role_id ? '岗位资料' : '全库'))
const categories: { key: KnowledgeCategory; label: string }[] = [{ key: 'sop', label: '岗位流程' }, { key: 'skill', label: '智能体技能' }, { key: 'other', label: '其他' }]
const categoryTitle = computed(() => categories.find(group => group.key === filters.category)?.label || '')
const departments = computed(() => {
  const groups = new Map<string, KnowledgeRole[]>()
  for (const role of roles.value || []) groups.set(role.department, [...(groups.get(role.department) || []), role])
  return [...groups].map(([name, entries]) => ({ name, roles: entries }))
})
const readingOpen = ref(false)
const reading = ref<PublishedKnowledge | null>(null)
const readingLoading = ref(false)
const readingError = ref('')
const readingGraph = ref<GraphData | null>(null)
const readingGraphError = ref(false)
const readingLinksOpen = ref(false)
const readingLinksLoading = ref(false)
let graphPending: Promise<GraphData> | null = null
let graphSignal: AbortSignal | undefined
let graphSnapshot: GraphData | null = null
let graphFetchedAt = 0
let graphGeneration = 0
let linksRequest = 0
function clearReadingLinks() { linksRequest++; readingGraph.value = null; readingLinksOpen.value = false; readingLinksLoading.value = false; readingGraphError.value = false }
async function readGraph(signal?: AbortSignal): Promise<GraphData> {
  if (graphPending && !graphSignal?.aborted) return graphPending
  const generation = graphGeneration
  const pending = getGraph(signal).then(result => { if (generation === graphGeneration && !signal?.aborted) { graphSnapshot = result; graphFetchedAt = Date.now(); emit('graphPending', result.status === 'building') } return result })
    .catch(reason => { if (generation === graphGeneration && !signal?.aborted && reason instanceof PortalError && [401, 403].includes(reason.status)) { graphSnapshot = null; readingGraph.value = null } throw reason })
    .finally(() => { if (graphPending === pending) graphPending = null })
  graphPending = pending
  graphSignal = signal
  return pending
}
async function loadReadingLinks() {
  if (!reading.value || readingLinksLoading.value) return
  const current = ++linksRequest
  readingLinksOpen.value = true; readingLinksLoading.value = true; readingGraphError.value = false
  try { const result = graphSnapshot && Date.now() - graphFetchedAt < 30_000 ? graphSnapshot : await readGraph(); if (current === linksRequest) readingGraph.value = result }
  catch (reason) { if (current === linksRequest) { readingGraphError.value = true; emit('error', reason) } }
  finally { if (current === linksRequest) readingLinksLoading.value = false }
}
const previewFile = computed(() => {
  const files = reading.value?.files || []
  // Other formats already use the formatted body; their older OSS preview is plain text.
  return files.some(file => file.purpose === 'original' && /\.docx$/i.test(file.filename))
    ? files.find(file => file.purpose === 'preview') : undefined
})
const downloadFiles = computed(() => reading.value?.files?.filter(file => file.purpose !== 'preview') || [])
const hasSkillPackage = computed(() => downloadFiles.value.some(file => file.purpose === 'skill'))
const previewHtml = ref('')
const fileLoading = ref('')
const fileError = ref('')
const fileNotice = ref('')
let fileRequest = 0
let fileExpiry: ReturnType<typeof setTimeout> | undefined
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
const number = (value: number | null | undefined) => value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
const date = (value: string | null) => { const parsed = value ? new Date(value) : null; return parsed && !Number.isNaN(parsed.valueOf()) ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(parsed) : '时间未记录' }
const kindLabel = knowledgeKindName
const fileSize = (bytes: number) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`
async function loadRoles() {
  if (!canRead.value || props.view !== 'overview') return
  const current = ++rolesRequest
  rolesLoading.value = true; rolesError.value = ''
  try { const result = await getRoles(); if (current === rolesRequest) roles.value = result.items }
  catch (reason) { if (current !== rolesRequest) return; rolesError.value = reason instanceof Error ? reason.message : '岗位目录暂不可用。'; if (reason instanceof PortalError && [401, 403].includes(reason.status)) roles.value = null; emit('error', reason) }
  finally { if (current === rolesRequest) rolesLoading.value = false }
}
async function loadCatalog() {
  if (!canRead.value || props.view !== 'overview' || directoryOpen.value) return
  const current = ++requestId
  loading.value = true; error.value = ''
  try {
    const result = await getCatalog({ ...filters, limit: 20, offset: (page.value - 1) * 20 })
    if (current !== requestId) return
    catalog.value = result
    if (page.value > 1 && result.total <= result.offset) { page.value = Math.max(1, Math.ceil(result.total / 20)); void loadCatalog() }
  } catch (reason) {
    if (current !== requestId) return
    catalog.value = null
    if (reason instanceof PortalError && [401, 403].includes(reason.status)) { clearSearch(); readingOpen.value = false; clearReading() }
    error.value = reason instanceof Error ? reason.message : '知识目录暂不可用，请稍后重试。'; emit('error', reason)
  } finally { if (current === requestId) loading.value = false }
}
function clearSearch() { searchRequest++; searchController?.abort(); searchController = null; semanticQuery.value = ''; searchResults.value = null; searchLoading.value = false; searchError.value = '' }
async function runSearch() {
  const query = searchInput.value.trim()
  if (!query) { resetFilters(); return }
  if (!canRead.value) return
  clearSearch()
  if (filters.role_id === 'unknown') { filters.q = query.slice(0, 200); page.value = 1; catalog.value = null; await loadCatalog(); return }
  const current = ++searchRequest
  searchController = new AbortController()
  semanticQuery.value = query; searchLoading.value = true; filters.q = ''
  try { const result = await searchKnowledge(query, searchController.signal, { role_id: filters.role_id, category: filters.category }); if (current === searchRequest) searchResults.value = result }
  catch (reason) {
    if (current !== searchRequest) return
    searchError.value = reason instanceof Error ? reason.message : '检索暂不可用，请重试。'
    if (reason instanceof PortalError && [401, 403].includes(reason.status)) { readingOpen.value = false; clearReading() }
    emit('error', reason)
  } finally { if (current === searchRequest) searchLoading.value = false }
}
function applyFilters() { clearSearch(); searchInput.value = ''; filters.q = ''; page.value = 1; catalog.value = null; void loadCatalog() }
function chooseCategory(key: KnowledgeCategory | '') { filters.category = key; applyFilters() }
function browseRole(id: string) { directoryOpen.value = false; filters.role_id = id; filters.category = id && id !== 'unknown' ? 'sop' : ''; applyFilters() }
defineExpose({ browseRole })
function showDirectory() { directoryOpen.value = true; requestId++; loading.value = false; error.value = ''; clearSearch(); catalog.value = null; void loadRoles() }
function resetFilters() { applyFilters() }
function changePage(value: number) { page.value = value; void loadCatalog() }
function clearFileAccess() { fileRequest++; clearTimeout(fileExpiry); fileExpiry = undefined; previewHtml.value = ''; fileLoading.value = ''; fileError.value = ''; fileNotice.value = '' }
function previewFailed() { clearFileAccess(); fileError.value = '预览暂不可用，可以阅读正文或下载原件。' }
async function accessFile(file: KnowledgeFile, disposition: 'inline' | 'attachment') {
  if (!reading.value || !canRead.value || fileLoading.value) return
  const id = reading.value.knowledge_id, revision = reading.value.revision
  const current = ++fileRequest
  fileLoading.value = file.file_id; fileError.value = ''; fileNotice.value = ''
  try {
    const access = await getKnowledgeFile(id, file.file_id, disposition)
    if (current !== fileRequest || reading.value?.knowledge_id !== id || reading.value?.revision !== revision) return
    if (!Number.isFinite(access.expires_in) || access.expires_in <= 0) throw new Error('文件链接已过期，请重新获取。')
    if (disposition === 'inline') {
      if (access.media_type !== 'text/html' || typeof access.html !== 'string' || !access.html.trim()) throw new Error('此文件暂不支持在线预览，请下载查看。')
      // srcdoc inherits the portal CSP: use its same-origin stylesheet, not inline styles.
      previewHtml.value = access.html.replace("style-src 'unsafe-inline'", "style-src 'self'")
        .replace(/<style>[\s\S]*?<\/style>/i, '<link rel="stylesheet" href="/portal/knowledge/assets/knowledge-preview.css">')
      clearTimeout(fileExpiry)
      fileExpiry = setTimeout(() => { clearFileAccess(); fileNotice.value = '预览已过期，请点击文档预览重新读取。' }, Math.min(access.expires_in, 3600) * 1000)
    } else {
      if (typeof access.url !== 'string') throw new Error('下载链接暂不可用，请重新获取。')
      const url = safeFileUrl(access.url)
      const link = document.createElement('a')
      link.href = url; link.download = access.filename; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer'
      document.body.appendChild(link); link.click(); link.remove()
      fileNotice.value = '已请求下载；若未开始，请重新点击下载。'
    }
  } catch (reason) {
    if (current !== fileRequest) return
    if (reason instanceof PortalError && [401, 403, 404].includes(reason.status)) { clearFileAccess(); void openKnowledge(id) }
    fileError.value = reason instanceof Error ? reason.message : '文件暂不可用，请重试。'; emit('error', reason)
  } finally { if (current === fileRequest) fileLoading.value = '' }
}
function clearReading() { if (readingOpen.value) return; readingRequest++; clearFileAccess(); reading.value = null; readingError.value = ''; readingLoading.value = false; clearReadingLinks() }
async function openKnowledge(id: string) {
  if (!canRead.value) return
  const current = ++readingRequest
  const sameKnowledge = readingOpen.value && reading.value?.knowledge_id === id
  if (!sameKnowledge) { clearFileAccess(); reading.value = null; clearReadingLinks() }
  readingOpen.value = true; readingGraphError.value = false; readingError.value = ''; readingLoading.value = true
  try {
    const result = await getPublishedKnowledge(id)
    if (current !== readingRequest) return
    if (reading.value && (reading.value.revision !== result.revision || JSON.stringify(reading.value.files) !== JSON.stringify(result.files))) clearFileAccess()
    if (reading.value && reading.value.revision !== result.revision) { clearReadingLinks(); graphGeneration++; graphSnapshot = null; graphPending = null }
    if (JSON.stringify(reading.value) !== JSON.stringify(result)) reading.value = result
  } catch (reason) { if (current === readingRequest) { clearFileAccess(); reading.value = null; clearReadingLinks(); readingError.value = reason instanceof Error ? reason.message : '正文暂不可用'; emit('error', reason) } }
  finally { if (current === readingRequest) readingLoading.value = false }
}
function refreshLibrary() { if (!active) return; if (!rolesLoading.value) void loadRoles(); if (!loading.value) void loadCatalog(); if (readingOpen.value && reading.value && !readingLoading.value) void openKnowledge(reading.value.knowledge_id) }
watch(() => props.refreshKey, refreshLibrary)
watch(readingOpen, value => { if (!value) clearFileAccess() })
watch(() => props.view, () => { clearSearch(); requestId++; loading.value = false; error.value = ''; page.value = 1; catalog.value = null; refreshLibrary() })
watch(canRead, value => { if (!value) { graphGeneration++; graphSnapshot = null; graphPending = null; clearSearch(); rolesRequest++; roles.value = null; rolesLoading.value = false; requestId++; loading.value = false; catalog.value = null; readingOpen.value = false; clearReading() } else refreshLibrary() })
onMounted(refreshLibrary)
onActivated(() => { if (!active) { active = true; refreshLibrary() } })
onDeactivated(() => {
  active = false; clearSearch(); requestId++; rolesRequest++; loading.value = false; rolesLoading.value = false
  readingOpen.value = false; clearReading(); graphGeneration++; graphPending = null
})
onBeforeUnmount(() => { clearSearch(); clearFileAccess(); requestId++; rolesRequest++; readingRequest++; linksRequest++; catalog.value = null; reading.value = null; roles.value = null; readingGraph.value = null; graphGeneration++; graphSnapshot = null; graphPending = null })
</script>
