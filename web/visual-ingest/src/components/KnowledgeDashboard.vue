<template>
  <main id="knowledge-main" class="portal-main knowledge-dashboard" :class="{ 'graph-dashboard': view === 'graph' }" tabindex="-1">
    <section v-if="view === 'overview'" class="page-heading dashboard-heading">
      <div><span class="eyebrow">TEAM KNOWLEDGE / OVERVIEW</span><h1>团队沉淀，一目了然。</h1><p>有哪些岗位在贡献，留下了什么，哪些知识已经可以使用。</p></div>
      <div class="dashboard-sync"><span class="sync-label"><i aria-hidden="true" />每 30 秒自动更新</span><button class="button refresh-button" :disabled="loading || refreshing" @click="$emit('refresh')">{{ loading || refreshing ? '正在更新' : '刷新数据' }} <span aria-hidden="true">↻</span></button></div>
    </section>
    <header v-else class="graph-page-toolbar"><h1>关系图谱</h1><div class="dashboard-sync"><span class="sync-label"><i aria-hidden="true" />每 30 秒自动更新</span><button class="button refresh-button" :disabled="refreshing" @click="$emit('refresh')">{{ refreshing ? '正在更新' : '刷新' }} <span aria-hidden="true">↻</span></button></div></header>
    <div v-if="error" class="message error" role="alert">{{ error }}<button class="text-button" @click="loadCatalog">重新读取</button></div>
    <div v-if="overviewError" class="message error" role="alert">上传统计暂不可用：{{ overviewError }}</div>

    <template v-if="view === 'overview'">
      <section class="dashboard-metrics" aria-label="知识沉淀统计" :aria-busy="loading">
        <article class="knowledge-total"><div class="metric-title">正式知识资产 <span>全公司</span></div><div class="knowledge-total-value">{{ number(catalog?.counts.published_assets ?? overview?.counts.published_assets) }}<small>项</small></div><p>已发布、可检索、可复用</p><div class="total-caption">同一知识更新版本不重复计数 <span aria-hidden="true">↗</span></div></article>
        <article class="dashboard-stat"><span class="stat-index">01 / 岗位</span><h2>已归属岗位</h2><div>{{ number(catalog?.counts.positions) }}<small>个</small></div><p>按上传员工所属岗位统计</p><span v-if="catalog" class="stat-note">{{ catalog.counts.unknown_position }} 项知识待补充岗位</span></article>
        <article class="dashboard-stat"><span class="stat-index">02 / 上传</span><h2>{{ processingScope }}提交批次</h2><div>{{ number(overview?.counts.receipts) }}<small>批</small></div><p>含未完成和未通过核验的提交</p><button class="text-button" @click="$emit('processing', '')">查看上传记录 ↗</button></article>
        <article class="dashboard-stat attention-stat"><span class="stat-index">03 / 关注</span><h2>{{ processingScope }}待处理异常</h2><div>{{ number(attention) }}<small>个任务</small></div><p>{{ overview ? `${overview.counts.needs_review} 个待核对 · ${overview.counts.failed} 个失败` : '等待处理状态' }}</p><div class="stat-actions"><button class="text-button" @click="$emit('processing', 'needs_review')">待核对 ↗</button><button class="text-button" @click="$emit('processing', 'failed')">处理失败 ↗</button></div><button v-if="overview?.receipt_counts" class="text-button stat-note" @click="$emit('processing', '')">原件核验异常 {{ overview.receipt_counts.failed + overview.receipt_counts.rejected }} 份 ↗</button></article>
      </section>

      <section class="dashboard-distributions">
        <article class="dashboard-panel types-panel"><header><div><span class="eyebrow">WHAT WE KNOW</span><h2>都沉淀了什么</h2></div><span class="panel-description">按正式知识类型统计</span></header>
          <div v-if="!canRead" class="compact-state">当前账号没有正式知识目录权限。</div>
          <template v-else>
            <div class="type-strip" aria-hidden="true"><span v-for="(group, index) in nonzeroKinds" :key="group.key" :style="{ flex: group.count, background: kindColor(group.key) }" /></div>
            <div class="type-summary"><button v-for="(group, index) in kindGroups" :key="group.key" class="type-summary-row" :class="{ selected: filters.kind === group.key }" :disabled="!catalog" @click="chooseKind(group.key)"><span class="type-glyph" :style="{ color: kindColor(group.key) }">{{ typeIcon(group.key) }}</span><span>{{ group.label }}</span><strong>{{ catalog ? number(group.count) : '—' }}</strong><small>项</small><span class="type-row-arrow" aria-hidden="true">↗</span></button></div>
          </template>
        </article>
        <article class="dashboard-panel positions-panel"><header><div><span class="eyebrow">WHERE IT COMES FROM</span><h2>岗位知识沉淀</h2></div><span class="panel-description">上传员工所属岗位</span></header>
          <div v-if="positionGroups.length" class="position-bars"><button v-for="group in positionGroups" :key="group.key" :class="{ selected: filters.uploader_position === group.key }" @click="choosePosition(group.key)"><span>{{ group.label }}</span><strong>{{ group.count }}<small> 项</small></strong><i><b :style="{ width: `${group.count / maxPosition * 100}%` }" /></i></button></div>
          <div v-else class="position-empty"><span aria-hidden="true">▥</span><h3>{{ catalog ? '岗位信息待补充' : '等待岗位数据' }}</h3><p>{{ catalog ? '现有资料尚未关联上传员工的岗位。完成关联后，将在这里展示各岗位的知识数量。' : '读取正式知识目录后展示岗位分布。' }}</p></div>
          <button v-if="catalog?.counts.unknown_position" class="unknown-position" :class="{ selected: filters.uploader_position === 'unknown' }" @click="choosePosition('unknown')"><span><i aria-hidden="true" />待补充岗位</span><strong>{{ catalog.counts.unknown_position }} 项</strong><span aria-hidden="true">→</span></button>
          <p class="panel-footnote">岗位未归属的知识仍计入总量，也可以正常查阅。</p>
        </article>
      </section>
    </template>

    <KnowledgeGraph v-if="view === 'graph'" :refresh-key="refreshKey" :can-read="canRead" @select="openKnowledge" @error="emit('error', $event)" />
    <section v-else class="dashboard-panel library-panel" aria-labelledby="knowledge-library-title">
      <header><div><span class="eyebrow">THE SHARED LIBRARY</span><h2 id="knowledge-library-title">知识资产目录<span v-if="catalog" class="total-count">{{ catalog.total }}</span></h2></div><span class="panel-description">{{ catalog ? `更新于 ${date(catalog.generated_at)}` : '正式知识，全员共享' }}</span></header>
      <form class="catalog-filters" role="search" @submit.prevent="applyFilters"><label class="catalog-search"><span aria-hidden="true">⌕</span><span class="sr-only">搜索知识标题</span><input v-model="searchInput" type="search" maxlength="200" placeholder="搜索知识标题…"><button type="submit" class="text-button">搜索</button></label><label><span class="sr-only">知识类型</span><select v-model="filters.kind" @change="applyFilters"><option value="">全部类型</option><option v-for="group in kindGroups" :key="group.key" :value="group.key">{{ group.label }}</option></select></label><label><span class="sr-only">上传员工岗位</span><select v-model="filters.uploader_position" @change="applyFilters"><option value="">全部上传岗位</option><option v-for="group in positionGroups" :key="group.key" :value="group.key">{{ group.label }}</option><option value="unknown">待补充岗位</option></select></label><button v-if="hasFilters" type="button" class="text-button" @click="resetFilters">重置</button></form>
      <div v-if="hasFilters && catalog" class="filter-result" role="status">当前筛选 {{ catalog.total }} 项 <span>· 公司共 {{ catalog.counts.published_assets }} 项正式知识</span></div>
      <div v-if="!canRead" class="list-state"><h3>当前账号未开放知识目录</h3><p>管理员可通过现有 MCP 角色配置知识查看权限。</p></div>
      <div v-else-if="loading && !catalog" class="list-state" role="status"><span class="loading-dot" /><h3>正在读取知识资产</h3></div>
      <div v-else-if="!catalog && !loading" class="list-state"><h3>暂未取得知识目录</h3><p>请重试连接，统计缺失不会作为零展示。</p></div>
      <template v-else-if="catalog">
        <div v-if="!catalog.total" class="list-state"><span class="empty-symbol" aria-hidden="true">▤</span><h3>{{ hasFilters ? '没有符合筛选条件的知识' : '知识库正等待第一份沉淀' }}</h3><p>{{ hasFilters ? '调整关键词、岗位或类型后再试。' : '上传资料通过清洗和发布检查后，将自动显示在这里。' }}</p><button v-if="hasFilters" class="button subtle" @click="resetFilters">清除筛选</button></div>
        <div v-else class="table-wrap" tabindex="0" aria-label="正式知识资产列表" :aria-busy="loading"><table class="assets-table"><thead><tr><th scope="col">知识名称 / 适用场景</th><th scope="col">类型</th><th scope="col">上传员工岗位</th><th scope="col">发布时间</th><th scope="col"><span class="sr-only">阅读</span></th></tr></thead><tbody><tr v-for="asset in catalog.items" :key="asset.knowledge_id"><td><button class="asset-title" @click="openKnowledge(asset.knowledge_id)"><span class="asset-icon" aria-hidden="true">{{ typeIcon(asset.kind) }}</span><span><strong>{{ asset.title }}</strong><small>{{ asset.scenarios.slice(0, 2).join(' / ') || '未填写适用场景' }}</small></span></button></td><td><span class="asset-kind">{{ kindLabel(asset.kind) }}</span></td><td><span :class="{ 'missing-position': !asset.uploader_position }">{{ asset.uploader_position || '待补充岗位' }}</span></td><td class="time-cell">{{ date(asset.published_at) }}</td><td><button class="text-button" :aria-label="`阅读 ${asset.title}`" @click="openKnowledge(asset.knowledge_id)">阅读 ↗</button></td></tr></tbody></table></div>
        <div v-if="view === 'overview' && catalog.total" class="list-footer"><span>显示 {{ catalog.offset + 1 }}–{{ catalog.offset + catalog.items.length }} / {{ catalog.total }} 项</span><el-pagination :current-page="page" :page-size="20" :total="catalog.total" layout="prev, pager, next" prev-text="上一页" next-text="下一页" :disabled="loading" @current-change="changePage" /></div>
      </template>
    </section>
    <footer v-if="view === 'overview'" class="dashboard-footnote"><span><i class="tiny-dot" />自动上传的新资料，发布后进入目录与图谱。</span><button class="text-button" @click="$emit('processing', '')">查看上传与异常 →</button><p>正式知识按知识 ID 去重；提交批次按收件申请统计。{{ processingScope }}上传与异常仅展示当前账号获授权的范围。</p></footer>

    <el-drawer v-model="readingOpen" class="knowledge-detail" :title="reading?.title || '阅读正式知识'" size="min(860px, 100vw)" :destroy-on-close="true" @closed="clearReading">
      <div v-if="readingLoading && !reading" class="list-state" role="status"><span class="loading-dot" />正在读取当前正式版本…</div><div v-else-if="readingError" class="message error" role="alert">{{ readingError }}</div><template v-else-if="reading"><div class="reading-meta"><span class="asset-kind">{{ kindLabel(reading.kind) }}</span><span>全员可查</span><span>版本 {{ reading.revision }}</span></div><p class="reading-position">上传员工岗位：{{ reading.uploader_position || '待补充岗位' }}</p><section class="reading-connections" aria-label="这篇知识的连接"><h3>这篇知识的连接 <span>{{ readingLinks.length }}</span></h3><p v-if="readingGraphError">关联暂时无法读取，稍后会自动重试。</p><p v-else-if="!readingGraph || readingGraph.status === 'building'">正在分析已发布知识之间的关联…</p><p v-else-if="!readingLinks.length">暂未找到这篇知识的引用或内容关联。</p><button v-for="link in readingLinks" :key="link.id" type="button" @click="openKnowledge(link.target)"><span>{{ link.label }}</span><strong>{{ link.title }} ↗</strong><small>{{ link.reason }}</small></button><p v-if="readingLinks.length" class="connection-caution">内容相关是自动推荐，不代表原文引用或事实依赖。</p></section><pre class="draft-content">{{ reading.content }}</pre><p class="preview-meta">知识编号：{{ reading.knowledge_id }}</p></template>
    </el-drawer>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { getCatalog, getPublishedKnowledge, PortalError, type CatalogGroup, type KnowledgeCatalog, type Overview, type PortalSession, type PublishedKnowledge, getGraph, type GraphData } from '../api/knowledgePortal'
import KnowledgeGraph from './KnowledgeGraph.vue'
import '../knowledge-dashboard.css'

const props = defineProps<{ view: 'overview' | 'graph'; session: PortalSession; overview: Overview | null; overviewError: string; refreshKey: number; refreshing: boolean }>()
const emit = defineEmits<{ refresh: []; error: [error: unknown]; processing: [status: string] }>()
const catalog = ref<KnowledgeCatalog | null>(null)
const loading = ref(false)
const error = ref('')
const searchInput = ref('')
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
const attention = computed(() => props.overview ? props.overview.counts.needs_review + props.overview.counts.failed : null)
const defaults: CatalogGroup[] = [{ key: 'sop', label: 'SOP / 流程', count: 0 }, { key: 'skill', label: 'Skill', count: 0 }, { key: 'case', label: '案例', count: 0 }, { key: 'template', label: '模板', count: 0 }, { key: 'method', label: '方法', count: 0 }, { key: 'prompt', label: '提示词', count: 0 }, { key: 'policy', label: '规则', count: 0 }, { key: 'reference', label: '参考', count: 0 }, { key: 'other', label: '其他知识', count: 0 }]
const kindGroups = computed(() => catalog.value?.by_kind || defaults)
const nonzeroKinds = computed(() => kindGroups.value.filter(group => group.count > 0))
const positionGroups = computed(() => catalog.value?.by_uploader_position.filter(group => group.key !== 'unknown') || [])
const maxPosition = computed(() => Math.max(1, ...positionGroups.value.map(group => group.count)))
const hasFilters = computed(() => !!(filters.q || filters.kind || filters.uploader_position))
const number = (value: number | null | undefined) => value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
const date = (value: string | null) => { const parsed = value ? new Date(value) : null; return parsed && !Number.isNaN(parsed.valueOf()) ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(parsed) : '时间未记录' }
const typeColor = (index: number) => ['#2d6652', '#728d63', '#b58a4a', '#6a8992', '#997965', '#949989'][index % 6]
const kindColor = (key: string) => typeColor(kindGroups.value.findIndex(group => group.key === key))
const typeIcon = (key: string) => ({ sop: '≡', skill: '⌘', case: '◇', template: '▧', method: '↗', prompt: '❝' }[key] || '▤')
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
    if (reason instanceof PortalError && [401, 403].includes(reason.status)) { readingOpen.value = false; clearReading() }
    error.value = reason instanceof Error ? reason.message : '知识目录暂不可用，请稍后重试。'
    emit('error', reason)
  } finally { if (current === requestId) loading.value = false }
}
function applyFilters() { filters.q = searchInput.value.trim(); page.value = 1; catalog.value = null; void loadCatalog() }
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
watch(() => props.view, () => { requestId++; loading.value = false; error.value = ''; page.value = 1; catalog.value = null; void loadCatalog() })
watch(canRead, value => { if (!value) { requestId++; loading.value = false; catalog.value = null; readingOpen.value = false; clearReading() } else void loadCatalog() })
onMounted(loadCatalog)
onBeforeUnmount(() => { requestId++; readingRequest++; catalog.value = null; reading.value = null; readingGraph.value = null })
</script>
