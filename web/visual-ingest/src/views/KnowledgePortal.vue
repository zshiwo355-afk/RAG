<template>
  <div class="knowledge-portal">
    <a class="skip-link" href="#knowledge-main">跳到主要内容</a>

    <main v-if="!session" id="knowledge-main" class="portal-entry">
      <section class="entry-story" aria-labelledby="entry-title">
        <div class="wordmark"><span class="archive-mark" aria-hidden="true">知</span><span>公司知识中心<small>KNOWLEDGE ARCHIVE</small></span></div>
        <div class="entry-copy"><span class="eyebrow">每一份经验，都有来处。</span><h1 id="entry-title">让经验留得下，<br>也找得到。</h1><p>把日常工作里的方法、案例与实践，<br>变成团队可以继续使用的知识。</p></div>
        <div class="entry-index"><span>01　保留来源</span><span>02　整理经验</span><span>03　持续复用</span></div>
      </section>
      <section class="entry-form" aria-labelledby="login-title">
        <div v-if="checkingSession" class="entry-check" role="status"><span class="loading-dot" />正在确认登录状态…</div>
        <template v-else>
          <span class="eyebrow">个人工作入口</span><h2 id="login-title">进入知识中心</h2>
          <p class="intro">查看沉淀进度、阅读处理结果。使用你的个人 MCP 连接凭据登录。</p>
          <div v-if="loginError" class="message error" role="alert">{{ loginError }}</div>
          <form @submit.prevent="login">
            <label for="knowledge-credential">个人 MCP 凭据</label>
            <input id="knowledge-credential" v-model="credential" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" :disabled="signingIn || signingOut || logoutUnconfirmed" aria-describedby="credential-hint" required placeholder="粘贴你的个人连接凭据">
            <p id="credential-hint" class="field-note">向管理员获取个人凭据；不要使用共享管理员账号。</p>
            <button class="button primary entry-submit" type="submit" :disabled="signingIn || signingOut || !credential.trim() || logoutUnconfirmed">{{ signingIn ? '正在登录…' : '登录知识中心' }}<span aria-hidden="true">→</span></button>
          </form>
          <button v-if="logoutUnconfirmed" class="button subtle" :disabled="signingOut" @click="logout">重试退出登录</button>
          <button v-else-if="initialCheckFailed" class="button subtle" @click="checkSession">重新检查连接</button>
          <div class="entry-note"><span aria-hidden="true">↗</span><p><strong>自动采集与手动上传都可沉淀</strong><br>Agent 按设定时间运行；也可以登录后提交资料、查看知识与处理结果。</p></div>
        </template>
      </section>
    </main>

    <div v-else class="portal-shell" :class="{ 'graph-mode': activeView === 'graph' }">
      <aside class="portal-sidebar" aria-label="知识中心导航">
        <a class="wordmark" href="/rag/knowledge"><span class="archive-mark" aria-hidden="true">知</span><span>公司知识中心<small>KNOWLEDGE ARCHIVE</small></span></a>
        <div class="sidebar-section">工作空间</div>
        <nav class="knowledge-navigation"><a v-for="entry in views" :key="entry.key" :href="`#${entry.key}`" class="sidebar-nav" :class="{ 'sidebar-active': activeView === entry.key }" :aria-current="activeView === entry.key ? 'page' : undefined" :title="entry.label"><span aria-hidden="true">{{ entry.icon }}</span><span class="navigation-label">{{ entry.label }}</span><span v-if="activeView === entry.key" class="nav-dot" /></a></nav>
        <div class="sidebar-note"><span class="eyebrow">经验留在团队里</span><p>按岗位找到沉淀，<br>按类型复用方法，<br>沿着关系发现知识。</p><p>新的资料通过清洗与发布检查后，会自动出现在这里。</p></div>
        <div class="sidebar-footer"><span class="tiny-dot" />原件保留，过程可追溯</div>
      </aside>

      <div class="portal-workspace">
        <header class="portal-topbar"><span>公司数字资产 <span class="breadcrumb-divider">/</span> {{ viewTitle }}</span><div class="identity"><span class="avatar" aria-hidden="true">{{ session.principal.display_name.slice(0, 1) || '我' }}</span><span>{{ session.principal.display_name || '当前用户' }}</span><button class="text-button" :disabled="signingOut" @click="logout">{{ signingOut ? '退出中…' : '退出' }}</button></div></header>
        <KnowledgeDashboard v-if="activeView === 'overview' || activeView === 'graph'" :view="activeView" :session="session" :overview="overview" :overview-error="overviewError" :refresh-key="refreshKey" :refreshing="overviewLoading" @refresh="refresh" @error="handleError" @processing="showProcessing" />
        <KnowledgeUpload v-else-if="activeView === 'upload'" :session="session" @submitted="refresh" @error="handleError" @processing="showProcessing()" />
        <main v-else id="knowledge-main" class="portal-main" tabindex="-1">
          <section class="page-heading"><div><span class="eyebrow">KNOWLEDGE INTAKE</span><h1>上传与异常</h1><p>跟进收件、清洗和发布。每 30 秒更新，异常保留原因和处理入口。</p></div><div class="heading-controls"><label v-if="canCompanyOverview" class="scope-control">统计范围<select v-model="scope"><option value="self">我的收件</option><option value="company">公司汇总</option></select></label><button class="button refresh-button" :disabled="overviewLoading || jobsLoading" @click="refresh">{{ overviewLoading || jobsLoading ? '更新中…' : '刷新数据' }}<span aria-hidden="true">↻</span></button></div></section>

          <section class="overview-section" aria-label="沉淀概览" :aria-busy="overviewLoading">
            <div class="section-caption"><span>{{ scope === 'company' ? '公司处理概览' : '我的处理概览' }}</span><span>{{ overview ? `更新于 ${formatDate(overview.generated_at)}` : '实时服务数据' }}</span></div>
            <div v-if="overviewError" class="message error" role="alert">{{ overviewError }} <button class="text-button" @click="loadOverview">重新读取统计</button></div>
            <div class="metric-grid">
              <article class="metric featured"><div class="metric-title">正式知识<span>全公司</span></div><div class="metric-number">{{ overview && overview.counts.published_assets === null ? '—' : count(overview?.counts.published_assets) }}<small v-if="overview?.counts.published_assets != null">项</small></div><p>{{ overview?.counts.published_assets === null ? '当前账号无正式知识查看权限' : '按已发布知识 ID 计数，更新不重复计数' }}</p></article>
              <article class="metric"><div class="metric-title">收件记录</div><div class="metric-number">{{ count(overview?.counts.receipts) }}<small v-if="overview">份</small></div><p>含未完成、未通过核验与已处理的收件申请</p></article>
              <article class="metric"><div class="metric-title">需要关注<span class="attention-dot" aria-hidden="true" /></div><div class="metric-number">{{ overview ? count(overview.counts.needs_review + overview.counts.failed) : '—' }}<small v-if="overview">个任务</small></div><p>{{ overview ? `${overview.counts.needs_review} 个待处理 · ${overview.counts.failed} 个失败` : '待处理与失败任务分别记录' }}</p></article>
              <article class="metric"><div class="metric-title">待发布内容</div><div class="metric-number">{{ overview ? count(overview.counts.draft_items + (overview.counts.indexing_items || 0)) : '—' }}<small v-if="overview">项</small></div><p>{{ overview ? `${overview.counts.draft_items} 项草稿 · ${overview.counts.indexing_items || 0} 项索引处理中` : '处理完成并通过发布检查后计入正式知识' }}</p></article>
            </div>
            <p v-if="overview" class="processing-breakdown">当前范围：{{ overview.counts.published_items || 0 }} 项发布回执 · {{ overview.counts.duplicate_items || 0 }} 项重复 · {{ overview.counts.outdated_items || 0 }} 项历史版本 · {{ overview.counts.archived_items }} 项归档</p>
          </section>

          <ReceiptList :session="session" :scope="jobsScope" :refresh-key="refreshKey" @error="handleError" @open-job="openJob" />
          <section class="processing-section" aria-labelledby="processing-title">
            <div class="list-heading"><div><h2 id="processing-title">{{ jobsScope === 'company' ? '公司处理记录' : '我的处理记录' }}<span v-if="jobsTotal !== null" class="total-count">{{ jobsTotal }}</span></h2><p>{{ scope === 'company' && !canReview ? '你可以查看公司汇总；以下仅展示本人收件明细。' : '分别记录发布回执、重复、历史版本、草稿与待处理内容。' }}</p></div><label class="status-control">处理状态<select v-model="status" @change="filterJobs"><option value="">全部状态</option><option v-for="(label, value) in statusLabels" :key="value" :value="value">{{ label }}</option></select></label></div>
            <div v-if="jobsError" class="message error inset-message" role="alert">{{ jobsError }} <button class="text-button" @click="loadJobs">重新读取记录</button></div>
            <div v-if="jobsLoading && !jobs.length" class="list-state" role="status"><span class="loading-dot" /><h3>正在读取处理记录</h3><p>从服务端获取当前可见的任务。</p></div>
            <div v-else-if="!jobsLoading && !jobsError && !jobs.length" class="list-state"><span class="empty-symbol" aria-hidden="true">▤</span><h3>{{ status ? '这个状态下还没有记录' : '还没有可见的处理任务' }}</h3><p>{{ status ? '试试其他状态，或稍后刷新。' : '资料上传并通过核验后，处理进度会显示在这里。' }}</p><button v-if="status" class="button subtle" @click="clearFilter">查看全部记录</button></div>
            <div v-if="jobs.length" class="table-wrap" :aria-busy="jobsLoading" tabindex="0" aria-label="处理任务列表"><table class="jobs-table"><thead><tr><th scope="col">收件资料</th><th scope="col">处理状态</th><th scope="col">逐项去向</th><th scope="col">更新时间</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead><tbody><tr v-for="job in jobs" :key="job.job_id"><td><button class="job-name" @click="openJob(job.job_id)"><span class="file-symbol" aria-hidden="true">≡</span><span><strong>{{ job.filename || '未命名资料' }}</strong><small>收件 {{ shortId(job.receipt_id) }}</small></span></button></td><td><span class="status-badge" :class="job.status"><span />{{ statusName(job.status) }}</span><small v-if="job.status === 'running'" class="stage-note">{{ stageName(job.stage) }}</small></td><td><div class="item-counts"><span>{{ job.counts.published || 0 }} 发布回执</span><span>{{ job.counts.draft }} 草稿</span><span v-if="job.counts.indexing">{{ job.counts.indexing }} 索引中</span><span :class="{ 'needs-attention': job.counts.needs_review > 0 }">{{ job.counts.needs_review }} 待处理</span><span v-if="job.counts.duplicate">{{ job.counts.duplicate }} 重复</span><span v-if="job.counts.outdated">{{ job.counts.outdated }} 历史版本</span><span>{{ job.counts.archived }} 归档</span></div></td><td class="time-cell">{{ formatDate(job.updated_at) }}</td><td><button class="text-button details-link" :aria-label="`查看 ${job.filename || '资料'} 的处理详情`" @click="openJob(job.job_id)">查看<span aria-hidden="true"> ↗</span></button></td></tr></tbody></table></div>
            <div v-if="jobsTotal !== null && jobsTotal > 0" class="list-footer"><span>共 {{ jobsTotal }} 个处理任务 · 每页 {{ pageSize }} 个</span><el-pagination :current-page="page" :page-size="pageSize" :total="jobsTotal" layout="prev, pager, next" prev-text="上一页" next-text="下一页" :disabled="jobsLoading" @current-change="changePage" /></div>
          </section>
          <footer class="portal-footnote"><span>原件 ≠ 草稿 ≠ 正式知识</span><p>收件申请与正式知识分别统计。待处理、失败、草稿与索引中的内容不会计入正式知识；重复提交不会增加知识数量。发布回执保留历史结果，正式知识总数按当前发布状态统计。</p></footer>
        </main>
      </div>
    </div>

    <el-drawer v-model="detailOpen" class="knowledge-detail" :title="selectedJob?.filename || '处理详情'" size="min(760px, 100vw)" :destroy-on-close="true" @closed="!detailOpen && clearDetail()">
      <div v-if="detailLoading" class="list-state" role="status"><span class="loading-dot" />正在读取处理详情…</div>
      <div v-else-if="detailError" class="message error" role="alert">{{ detailError }}<button class="text-button" @click="loadDetail(itemPage, eventPage)">重试</button></div>
      <template v-else-if="selectedJob">
        <div class="detail-intro"><span class="status-badge" :class="selectedJob.status"><span />{{ statusName(selectedJob.status) }}</span><span>{{ stageName(selectedJob.stage) }}</span><button v-if="canReviewWrite && selectedJob.retryable" class="button primary" :disabled="retrying || resolving || detailPaging" @click="retrySelected">{{ retrying ? '正在提交…' : '重新处理' }}</button></div>
        <div v-if="actionError" class="message error" role="alert">{{ actionError }}</div>
        <div v-if="actionNotice" class="message success" role="status">{{ actionNotice }}</div>
        <p v-if="canReview && !canReviewWrite" class="message notice">处理操作暂未启用。当前可以查看记录，写操作由 MCP 管理员配置。</p>
        <p v-if="selectedJob.error_code" class="message error">原处理原因：{{ reasonName(selectedJob.error_code) }}</p>
        <p v-if="selectedJob.status === 'needs_review'" class="message notice">这份资料需要进一步核对。补齐材料后重新提交，或记录原因并归档；原件和处理过程会保留。</p>
        <dl class="job-metadata"><div><dt>收件编号</dt><dd>{{ selectedJob.receipt_id }}</dd></div><div><dt>任务编号</dt><dd>{{ selectedJob.job_id }}</dd></div><div><dt>来源标识</dt><dd>{{ selectedJob.source_id || '未提供' }}</dd></div><div><dt>处理规则</dt><dd>{{ selectedJob.rule_version }}</dd></div><div><dt>创建时间</dt><dd>{{ formatDate(selectedJob.created_at) }}</dd></div><div><dt>已尝试</dt><dd>{{ selectedJob.attempts }} 次</dd></div></dl>
        <section class="detail-section" :aria-busy="detailPaging"><div class="detail-section-title"><h3>逐项处理结果</h3><span>共 {{ selectedJob.item_total ?? selectedJob.items?.length ?? 0 }} 项</span></div><p v-if="!selectedJob.items?.length" class="detail-empty">处理完成后，每项内容的去向会显示在这里。</p><article v-for="item in selectedJob.items" :key="item.item_id" class="processing-item">
          <div class="processing-item-heading"><h4>{{ item.title || item.source_path || '未命名内容' }}</h4><span class="status-badge" :class="item.status"><span />{{ itemStatusName(item.status) }}</span></div>
          <p class="source-path">{{ item.source_path }}<span v-if="item.version"> · 处理版本 {{ item.version }}</span></p>
          <ul v-if="item.reason_codes.length" class="reason-list"><li v-for="reason in item.reason_codes" :key="reason">{{ reasonName(reason) }}</li></ul>
          <p v-if="item.knowledge_id" class="item-reference">知识编号 {{ item.knowledge_id }}<span v-if="item.revision != null"> · 修订 {{ item.revision }}</span></p>
          <p v-if="item.replacement_receipt_id" class="item-reference">补充收件 {{ item.replacement_receipt_id }}</p>
          <div class="item-actions">
            <button v-if="item.has_preview" class="text-button preview-button" :disabled="previewLoading && previewItemId === item.item_id" @click="showPreview(item)">{{ previewLoading && previewItemId === item.item_id ? '正在读取…' : '阅读清洗后内容' }} <span aria-hidden="true">→</span></button>
            <button v-if="item.replacement_job_id" class="text-button" :disabled="resolving" @click="openJob(item.replacement_job_id)">查看补充资料的处理进度 →</button>
            <button v-if="canResolveItem(item) && resolutionItemId !== item.item_id" class="button subtle" :disabled="resolving || retrying || detailPaging" @click="beginResolution(item)">处理此异常</button>
          </div>
          <form v-if="resolutionItemId === item.item_id && canResolveItem(item)" class="resolution-form" @submit.prevent="submitResolution(item)">
            <p class="resolution-intro">保留原件及此次处理记录。补充资料会重新核验和查重，通过规则后才发布。</p>
            <fieldset :disabled="resolving"><legend>处理方式</legend><label class="resolution-choice"><input v-model="resolutionAction" type="radio" value="replace">补充材料重新处理</label><label class="resolution-choice"><input v-model="resolutionAction" type="radio" value="archive">仅归档</label></fieldset>
            <template v-if="resolutionAction === 'replace'"><label :for="`replacement-${item.item_id}`">已核验的补充收件编号</label><input :id="`replacement-${item.item_id}`" v-model="replacementReceipt" type="text" pattern="[0-9a-f]{32}" minlength="32" maxlength="32" spellcheck="false" autocapitalize="off" autocomplete="off" :disabled="resolving" :aria-describedby="`replacement-help-${item.item_id}`" required placeholder="32 位收件编号"><p :id="`replacement-help-${item.item_id}`" class="field-note">让原提交人通过 Agent 上传补充材料，核验完成后将返回的收件编号填在这里。</p></template>
            <label :for="`resolution-reason-${item.item_id}`">处理说明</label><textarea :id="`resolution-reason-${item.item_id}`" v-model="resolutionReason" rows="3" maxlength="500" :disabled="resolving" required placeholder="说明补充了哪些材料，或为何只归档。请勿填写密码等敏感信息。" />
            <div class="resolution-buttons"><button class="button primary" type="submit" :disabled="resolving || retrying || detailPaging || !validResolution">{{ resolving ? '正在提交…' : resolutionAction === 'replace' ? '提交补充处理' : '记录原因并归档' }}</button><button class="button subtle" type="button" :disabled="resolving" @click="clearResolution">取消</button></div>
          </form>
        </article><div v-if="(selectedJob.item_total || 0) > detailPageSize" class="detail-pagination"><span>第 {{ itemPage }} 页 · 每页最多 {{ detailPageSize }} 项</span><el-pagination :current-page="itemPage" :page-size="detailPageSize" :total="selectedJob.item_total" layout="prev, pager, next" prev-text="上一页" next-text="下一页" :disabled="detailPaging || retrying || resolving" @current-change="(value: number) => loadDetail(value, eventPage)" /></div></section>
        <section class="detail-section" :aria-busy="detailPaging"><div class="detail-section-title"><h3>处理事件</h3><span>共 {{ selectedJob.event_total ?? selectedJob.events?.length ?? 0 }} 条事件</span></div><p v-if="!selectedJob.events?.length" class="detail-empty">暂无可显示的处理事件。</p><ol v-else class="event-list"><li v-for="event in selectedJob.events" :key="event.event_id"><span class="event-dot" aria-hidden="true" /><div><strong>{{ eventName(event.event_type) }}</strong><p v-if="event.reason_code">{{ reasonName(event.reason_code) }}</p><time :datetime="event.created_at">{{ formatDate(event.created_at) }}</time></div></li></ol><div v-if="(selectedJob.event_total || 0) > detailPageSize" class="detail-pagination"><span>第 {{ eventPage }} 页 · 每页最多 {{ detailPageSize }} 条</span><el-pagination :current-page="eventPage" :page-size="detailPageSize" :total="selectedJob.event_total" layout="prev, pager, next" prev-text="上一页" next-text="下一页" :disabled="detailPaging || retrying || resolving" @current-change="(value: number) => loadDetail(itemPage, value)" /></div></section>
      </template>
    </el-drawer>

    <el-dialog v-model="previewOpen" class="knowledge-preview" :title="preview?.title || '清洗后内容'" width="min(860px, 94vw)" :destroy-on-close="true" @closed="!previewOpen && clearPreview()">
      <div class="message notice">{{ preview?.currently_published ? '此版本是当前正式知识，全员可查看。' : preview?.published ? '此版本保留发布回执，但已不是当前正式版本。以下为历史处理副本。' : '此处理副本尚未作为当前正式版本发布，仅向提交人和获授权的处理人员开放。' }}</div>
      <div v-if="previewLoading" class="list-state" role="status">正在读取正文…</div>
      <div v-else-if="previewError" class="message error" role="alert">{{ previewError }}</div>
      <template v-else-if="preview"><p v-if="preview.knowledge_id" class="preview-meta">{{ preview.knowledge_id }}<span v-if="preview.revision != null"> · 修订 {{ preview.revision }}</span></p><pre class="draft-content">{{ preview.content }}</pre></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  createSession, deleteSession, getDraft, getJob, getJobs, getOverview, getSession, PortalError, processingReasonLabels, processingEventLabels, retryJob, resolveItem,
  type DraftPreview, type Overview, type PortalSession, type ProcessingItem, type ProcessingJob, type Scope
} from '../api/knowledgePortal'
import '../knowledge-portal.css'
import KnowledgeDashboard from '../components/KnowledgeDashboard.vue'
import KnowledgeUpload from '../components/KnowledgeUpload.vue'
import ReceiptList from '../components/ReceiptList.vue'

type View = 'overview' | 'graph' | 'upload' | 'processing'
const views: { key: View; label: string; icon: string }[] = [{ key: 'overview', label: '知识总览', icon: '▦' }, { key: 'graph', label: '知识图谱', icon: '⌘' }, { key: 'upload', label: '上传资料', icon: '↑' }, { key: 'processing', label: '上传与异常', icon: '▤' }]
const readView = (): View => { const value = window.location.hash.slice(1); return value === 'graph' || value === 'processing' || value === 'upload' ? value : 'overview' }
const activeView = ref<View>(readView())
const viewTitle = computed(() => views.find(entry => entry.key === activeView.value)?.label || '知识总览')
const refreshKey = ref(0)
let refreshTimer: ReturnType<typeof setInterval> | undefined
function hashChanged() { activeView.value = readView() }
function showProcessing(value = '') { window.location.hash = 'processing'; status.value = value; filterJobs() }
function visibleRefresh() { if (document.visibilityState === 'visible' && navigator.onLine && session.value && !overviewLoading.value && !jobsLoading.value) void refresh() }
const session = ref<PortalSession | null>(null)
const credential = ref('')
const checkingSession = ref(true)
const signingIn = ref(false)
const signingOut = ref(false)
const loginError = ref('')
const initialCheckFailed = ref(false)
const logoutUnconfirmed = ref(false)
const scope = ref<Scope>('self')
const canReview = computed(() => session.value?.permissions.includes('company_knowledge.review') ?? false)
const canReviewWrite = computed(() => canReview.value && session.value?.capabilities?.review_write === true)
const canCompanyOverview = computed(() => canReview.value || (session.value?.permissions.includes('company_knowledge.dashboard.read') ?? false))
const jobsScope = computed<Scope>(() => canReview.value ? scope.value : 'self')
const overview = ref<Overview | null>(null)
const overviewLoading = ref(false)
const overviewError = ref('')
const jobs = ref<ProcessingJob[]>([])
const jobsTotal = ref<number | null>(null)
const jobsLoading = ref(false)
const jobsError = ref('')
const status = ref('')
const page = ref(1)
const pageSize = 20
const detailOpen = ref(false)
const detailLoading = ref(false)
const detailPaging = ref(false)
const detailPageSize = 100
const detailError = ref('')
const selectedJob = ref<ProcessingJob | null>(null)
const selectedId = ref('')
const itemPage = computed(() => Math.floor((selectedJob.value?.item_offset || 0) / detailPageSize) + 1)
const eventPage = computed(() => Math.floor((selectedJob.value?.event_offset || 0) / detailPageSize) + 1)
const retrying = ref(false)
const resolving = ref(false)
const resolutionItemId = ref('')
const resolutionVersion = ref(0)
const resolutionAction = ref<'replace' | 'archive'>('replace')
const resolutionReason = ref('')
const replacementReceipt = ref('')
const actionError = ref('')
const actionNotice = ref('')
const validResolution = computed(() => !!resolutionReason.value.trim() && resolutionReason.value.trim().length <= 500 && (resolutionAction.value === 'archive' || /^[0-9a-f]{32}$/.test(replacementReceipt.value.trim())))
const previewOpen = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const preview = ref<DraftPreview | null>(null)
const previewItemId = ref('')
let overviewRequest = 0
let jobsRequest = 0
let detailRequest = 0
let previewRequest = 0
let sessionGeneration = 0

const statusLabels = { queued: '排队中', running: '处理中', completed: '处理完成', needs_review: '待处理', failed: '处理失败' }
const statusName = (value: string) => statusLabels[value as keyof typeof statusLabels] || value
const itemStatusName = (value: string) => ({ draft: '已形成草稿', needs_review: '待处理', archived: '仅归档', duplicate: '重复内容', outdated: '历史版本', indexing: '索引处理中', published: '发布回执' }[value] || value)
const stageName = (value: string) => ({ queued: '等待处理', downloading: '读取原件', parsing: '解析内容', cleaning: '清洗内容', drafting: '保存草稿', persisting: '保存处理结果', importing: '写入草稿', indexing: '建立检索索引', publishing: '验证并发布知识', completed: '处理结束', review: '等待核对', needs_review: '等待核对', failed: '处理未完成', done: '处理结束' }[value] || value || '等待更新')
const eventName = (value: string) => processingEventLabels[value] || value
const reasonName = (value: string) => processingReasonLabels[value] || value
const count = (value: number | null | undefined) => value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
const shortId = (value: string) => value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
const formatDate = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '时间未提供' : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(date)
}
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求未完成，请稍后重试。'

function clearPreview() { previewRequest++; preview.value = null; previewError.value = ''; previewItemId.value = ''; previewLoading.value = false }
function clearResolution() { resolutionItemId.value = ''; resolutionVersion.value = 0; resolutionReason.value = ''; replacementReceipt.value = ''; resolutionAction.value = 'replace' }
function clearDetail() { clearResolution(); detailRequest++; selectedJob.value = null; selectedId.value = ''; detailError.value = ''; detailLoading.value = false; detailPaging.value = false; actionError.value = ''; actionNotice.value = ''; previewOpen.value = false; clearPreview() }
function clearPrivateData() {
  sessionGeneration++; overviewRequest++; jobsRequest++; session.value = null
  overview.value = null; jobs.value = []; jobsTotal.value = null; detailOpen.value = false; clearDetail()
  overviewLoading.value = false; jobsLoading.value = false; overviewError.value = ''; jobsError.value = ''; credential.value = ''
}
function handleError(error: unknown) {
  if (error instanceof PortalError && error.status === 401) { clearPrivateData(); loginError.value = '登录已过期，请重新登录。' }
  return errorMessage(error)
}
function initialScope(value: PortalSession): Scope {
  return value.permissions.includes('company_knowledge.review') || value.permissions.includes('company_knowledge.dashboard.read') ? 'company' : 'self'
}
async function checkSession() {
  checkingSession.value = true; loginError.value = ''; initialCheckFailed.value = false
  const generation = sessionGeneration
  try { const result = await getSession(); if (generation !== sessionGeneration) return; session.value = result; scope.value = initialScope(result); await refresh() }
  catch (error) { if (!(error instanceof PortalError && error.status === 401)) { loginError.value = errorMessage(error); initialCheckFailed.value = true } }
  finally { checkingSession.value = false }
}
async function login() {
  if (!credential.value.trim() || signingIn.value || signingOut.value || logoutUnconfirmed.value) return
  signingIn.value = true; loginError.value = ''; initialCheckFailed.value = false
  const generation = sessionGeneration
  const pending = createSession(credential.value.trim())
  credential.value = ''
  try { const result = await pending; if (generation !== sessionGeneration) return; session.value = result; scope.value = initialScope(result); page.value = 1; status.value = ''; await refresh() }
  catch (error) { loginError.value = error instanceof PortalError && error.status === 401 ? '凭据无效或已停用，请检查你的个人 MCP 连接凭据。' : errorMessage(error) }
  finally { signingIn.value = false }
}
async function logout() {
  if (signingOut.value) return
  signingOut.value = true
  clearPrivateData()
  try { await deleteSession(); logoutUnconfirmed.value = false; loginError.value = '' }
  catch (error) { if (error instanceof PortalError && error.status === 401) logoutUnconfirmed.value = false
    else { logoutUnconfirmed.value = true; loginError.value = '页面内容已清空，但服务器尚未确认退出。请重试退出登录。' } }
  finally { signingOut.value = false }
}
async function loadOverview() {
  if (!session.value) return
  const requestId = ++overviewRequest
  overviewLoading.value = true; overviewError.value = ''
  try { const result = await getOverview(scope.value); if (requestId === overviewRequest) overview.value = result }
  catch (error) { if (requestId === overviewRequest) { overview.value = null; overviewError.value = handleError(error) } }
  finally { if (requestId === overviewRequest) overviewLoading.value = false }
}
async function loadJobs() {
  if (!session.value) return
  const requestId = ++jobsRequest
  jobsLoading.value = true; jobsError.value = ''
  try { const result = await getJobs(jobsScope.value, status.value, page.value, pageSize); if (requestId === jobsRequest) { jobs.value = result.items; jobsTotal.value = result.total } }
  catch (error) { if (requestId === jobsRequest) { jobs.value = []; jobsTotal.value = null; jobsError.value = handleError(error) } }
  finally { if (requestId === jobsRequest) jobsLoading.value = false }
}
async function refresh() {
  refreshKey.value++
  const requests = [loadOverview(), loadJobs()]
  if (detailOpen.value && selectedId.value && !detailPaging.value && !retrying.value && !resolving.value && !resolutionItemId.value) requests.push(loadDetail(itemPage.value, eventPage.value))
  await Promise.allSettled(requests)
}
function filterJobs() { page.value = 1; jobs.value = []; jobsTotal.value = null; void loadJobs() }
function clearFilter() { status.value = ''; filterJobs() }
function changePage(value: number) { page.value = value; void loadJobs() }
async function openJob(id: string) {
  if (!id) return
  clearDetail(); selectedId.value = id; detailOpen.value = true
  await loadDetail(1, 1)
}
async function loadDetail(nextItemPage: number, nextEventPage: number) {
  if (!selectedId.value) return
  const requestId = ++detailRequest
  detailError.value = ''; detailPaging.value = true; detailLoading.value = !selectedJob.value
  try {
    const result = await getJob(selectedId.value, jobsScope.value, (nextItemPage - 1) * detailPageSize, (nextEventPage - 1) * detailPageSize, detailPageSize)
    if (requestId === detailRequest) selectedJob.value = result
  } catch (error) { if (requestId === detailRequest) detailError.value = handleError(error) }
  finally { if (requestId === detailRequest) { detailLoading.value = false; detailPaging.value = false } }
}
async function retrySelected() {
  if (!selectedJob.value || !canReviewWrite.value || !selectedJob.value.retryable || retrying.value || resolving.value) return
  const id = selectedJob.value.job_id
  const generation = sessionGeneration
  retrying.value = true; actionError.value = ''; actionNotice.value = ''
  try { const result = await retryJob(id); if (generation === sessionGeneration) { if (selectedId.value === id) { selectedJob.value = result; actionNotice.value = '已提交重试。任务将按队列顺序重新处理。' } await refresh() } }
  catch (error) { if (generation === sessionGeneration) { const message = handleError(error); if (selectedId.value === id) actionError.value = message } }
  finally { retrying.value = false }
}
function canResolveItem(item: ProcessingItem) {
  return canReviewWrite.value && item.status === 'needs_review' && Number.isInteger(item.version) && item.version > 0
    && selectedJob.value?.status !== 'running' && selectedJob.value?.status !== 'queued'
}
function beginResolution(item: ProcessingItem) {
  if (!canResolveItem(item) || resolving.value) return
  clearResolution(); resolutionItemId.value = item.item_id; resolutionVersion.value = item.version; actionError.value = ''; actionNotice.value = ''
}
async function submitResolution(item: ProcessingItem) {
  if (!selectedJob.value || resolutionItemId.value !== item.item_id || !canResolveItem(item) || !validResolution.value || resolving.value || retrying.value) return
  const jobId = selectedJob.value.job_id
  const generation = sessionGeneration
  const action = resolutionAction.value
  const common = { expected_version: resolutionVersion.value, reason: resolutionReason.value.trim() }
  resolving.value = true; actionError.value = ''; actionNotice.value = ''
  try {
    await resolveItem(jobId, item.item_id, action === 'replace'
      ? { ...common, action, replacement_receipt_id: replacementReceipt.value.trim() }
      : { ...common, action })
    if (generation !== sessionGeneration) return
    if (selectedId.value === jobId) {
      clearResolution()
      actionNotice.value = action === 'replace' ? '已关联补充收件。请查看补充资料的处理进度，当前操作不代表发布完成。' : '已记录原因并归档，原件继续保留。'
      await loadDetail(itemPage.value, eventPage.value)
    }
    await refresh()
  } catch (error) {
    if (generation === sessionGeneration) {
      const message = handleError(error)
      if (selectedId.value === jobId) {
        actionError.value = message
        if (error instanceof PortalError && error.status === 409) { clearResolution(); await loadDetail(itemPage.value, eventPage.value) }
      }
    }
  } finally { resolving.value = false }
}
async function showPreview(item: ProcessingItem) {
  if (!selectedJob.value) return
  clearPreview(); previewOpen.value = true; previewLoading.value = true; previewItemId.value = item.item_id
  const requestId = ++previewRequest
  try { const result = await getDraft(selectedJob.value.job_id, item.item_id, jobsScope.value); if (requestId === previewRequest) preview.value = result }
  catch (error) { if (requestId === previewRequest) previewError.value = handleError(error) }
  finally { if (requestId === previewRequest) previewLoading.value = false }
}
watch(scope, () => { overview.value = null; jobs.value = []; jobsTotal.value = null; page.value = 1; detailOpen.value = false; clearDetail(); void refresh() })
watch(viewTitle, value => { document.title = `公司知识中心 · ${value}` }, { immediate: true })
onMounted(() => {
  void checkSession()
  window.addEventListener('hashchange', hashChanged)
  window.addEventListener('online', visibleRefresh)
  document.addEventListener('visibilitychange', visibleRefresh)
  refreshTimer = setInterval(visibleRefresh, 30_000)
})
onBeforeUnmount(() => {
  clearInterval(refreshTimer)
  window.removeEventListener('hashchange', hashChanged)
  window.removeEventListener('online', visibleRefresh)
  document.removeEventListener('visibilitychange', visibleRefresh)
  clearPrivateData()
})
</script>
