<template>
  <section class="processing-section receipt-history" aria-labelledby="receipts-title">
    <div class="list-heading">
      <div><h2 id="receipts-title" tabindex="-1">{{ scope === 'company' ? '公司收件记录' : '我的收件记录' }}<span v-if="total !== null" class="total-count">{{ total }}</span></h2><p>从原件收取到内容处理持续追踪；原件核验通过、处理完成与正式发布分别记录。</p></div>
    </div>
    <form class="receipt-filters" aria-label="筛选收件记录" @submit.prevent="filter">
      <label>资料名称<input v-model="q" type="search" maxlength="200" placeholder="资料名称或原文件名"></label>
      <label>确认部门<select v-model="department" :disabled="!canReadRoles" @change="role_id = ''"><option value="">全部部门</option><option v-if="department && !departments.includes(department)" :value="department">{{ department }}</option><option v-for="name in departments" :key="name" :value="name">{{ name }}</option></select></label>
      <label>确认岗位<select v-model="role_id" :disabled="!canReadRoles"><option value="">全部岗位</option><option v-if="role_id && !availableRoles.some(role => role.role_id === role_id)" :value="role_id">已保存的岗位筛选</option><option v-for="role in availableRoles" :key="role.role_id" :value="role.role_id">{{ department ? role.name : `${role.department} / ${role.name}` }}</option></select></label>
      <label>原件核验<select v-model="status"><option value="">全部核验状态</option><option v-for="(label, value) in receiptStatusLabels" :key="value" :value="value">{{ label }}</option></select></label>
      <label>内容处理<select v-model="processing_status"><option value="">全部处理状态</option><option value="none">尚未进入内容处理</option><option v-for="(label, value) in processingStatusLabels" :key="value" :value="value">{{ label }}</option></select></label>
      <label>收件时间从<input v-model="created_from" type="date" :max="created_to || undefined"></label>
      <label>收件时间至<input v-model="created_to" type="date" :min="created_from || undefined"></label>
      <div class="receipt-filter-actions"><button class="button primary" type="submit" :disabled="loading">筛选</button><button class="text-button" type="button" @click="showAll">清除筛选</button></div>
    </form>
    <p v-if="!canReadRoles" class="receipt-filter-warning">当前身份无权读取岗位目录，仍可按其他条件筛选。</p>
    <p v-else-if="roleError" class="receipt-filter-warning" role="status">{{ roleError }} <button class="text-button" @click="loadRoles">重读岗位目录</button></p>
    <p v-if="counts" class="receipt-summary">当前身份范围共 {{ counts.total }} 份收件（包含未命中筛选的收件）· {{ counts.rejected }} 份拒收 · {{ counts.failed }} 份核验失败 · {{ counts.pending_verification }} 份核验中 · {{ counts.awaiting_upload }} 份待上传</p>
    <div v-if="error" class="message error inset-message" role="alert">{{ error }} <button class="text-button" @click="load">重新读取收件</button></div>
    <div v-if="loading && !items.length" class="list-state" role="status">正在读取收件历史…</div>
    <div v-else-if="!loading && !error && !items.length" class="list-state"><h3>{{ hasFilters ? '没有符合筛选条件的收件' : '暂无可见的收件记录' }}</h3><p>申请上传后即保留收件记录，不以是否生成知识判断。</p></div>
    <div v-if="items.length" class="table-wrap" :aria-busy="loading" tabindex="0" aria-label="收件记录列表">
      <table class="jobs-table"><thead><tr><th scope="col">资料名称与上传原岗位</th><th scope="col">确认归属</th><th scope="col">原件核验</th><th scope="col">内容处理与详情</th><th scope="col">更新时间</th></tr></thead>
        <tbody><tr v-for="item in items" :key="item.receipt_id">
          <td><strong class="receipt-title">{{ materialTitle(item) }}</strong><p class="upload-attribution" title="上传时填写的岗位，保留原声明">上传岗位：{{ uploaderPosition(item) }}</p><small class="receipt-size">{{ formatBytes(item.byte_length) }}</small>
            <details class="receipt-technical"><summary>技术信息</summary><dl><div><dt>原文件名</dt><dd>{{ item.filename }}</dd></div><div><dt>收件编号</dt><dd>{{ item.receipt_id }}</dd></div><div><dt>核验状态码</dt><dd>{{ item.status }}</dd></div><div v-if="item.error_code"><dt>原始错误码</dt><dd>{{ item.error_code }}</dd></div></dl><button class="text-button" @click="copyRecord(item)">复制核验信息</button><p v-if="copyReceiptId === item.receipt_id" class="receipt-copy-notice" role="status">{{ copyNotice }}</p></details>
          </td>
          <td class="receipt-assignment"><ul v-if="item.role_assignments?.length"><li v-for="assignment in item.role_assignments" :key="assignment.item_id"><strong>{{ assignment.role.department }} / {{ assignment.role.name }}</strong><small v-if="assignment.title">{{ assignment.title }}</small><span>归属已记录</span></li></ul><p v-else>尚未取得确认归属</p><p v-if="(item.role_assignments_total || 0) > (item.role_assignments?.length || 0)">另 {{ (item.role_assignments_total || 0) - (item.role_assignments?.length || 0) }} 条归属见详情。</p></td>
          <td class="receipt-outcome"><span class="status-badge" :class="{ failed: item.status === 'failed', needs_review: item.status === 'rejected', completed: item.status === 'verified' }">{{ receiptStatusName(item.status) }}</span>
            <p v-if="item.error_code">{{ receiptErrorMessage(item.error_code) }}</p>
            <p v-if="item.status === 'rejected'">此回执已结束；原样提交会返回同一记录。请核对并修正资料后提交，或凭编号联系维护人员。</p>
            <p v-else-if="item.status === 'failed'">可用原资料在上传页继续提交，由系统再次核验。</p>
            <p v-else-if="item.status === 'awaiting_upload'">已建立回执，尚未确认原件上传；可在上传页用原资料继续提交。</p>
            <p v-else-if="item.status === 'pending_verification'">原件仍在核验，暂未进入内容处理。</p>
          </td>
          <td class="receipt-outcome"><template v-if="item.job_id"><span class="status-badge" :class="item.job_status || ''">{{ processingStatusName(item.job_status || '') }}</span><p v-if="item.job_stage">{{ processingStageName(item.job_stage) }}</p>
              <p v-if="item.job_status === 'completed'">本次处理已结束，正式发布以发布回执为准。</p>
              <ul v-if="item.counts" class="receipt-item-counts"><li v-for="(count, itemStatus) in item.counts" v-show="count > 0" :key="itemStatus">{{ processingItemStatusName(String(itemStatus)) }} {{ count }}</li></ul>
              <ul v-if="item.blocking_reasons?.length" class="receipt-reasons"><li v-for="reason in item.blocking_reasons" :key="reason">{{ processingReasonName(reason) }}</li></ul>
              <p v-if="item.blocking_reasons_truncated">更多待核对原因请查看详情。</p>
              <button class="text-button" @click="emit('openJob', item.job_id)">查看处理详情 →</button>
            </template><template v-else><p class="receipt-no-job">尚未进入内容处理</p><p v-if="item.status === 'verified'">原件核验通过，尚未关联处理任务；不代表正式发布。</p><p v-else>原件核验通过并建立关联任务后，可在此继续跟进。</p></template>
          </td><td class="time-cell">{{ formatDate(latestUpdatedAt(item)) }}<small class="receipt-created">收件：{{ formatDate(item.created_at) }}</small></td>
        </tr></tbody>
      </table>
    </div>
    <div v-if="total !== null && total > 0" class="list-footer"><span>共 {{ total }} 份收件 · 第 {{ page }} / {{ Math.max(1, Math.ceil(total / pageSize)) }} 页</span><div class="receipt-pages"><button class="text-button" :disabled="loading || page <= 1" @click="changePage(page - 1)">上一页</button><button class="text-button" :disabled="loading || page * pageSize >= total" @click="changePage(page + 1)">下一页</button></div></div>
  </section>
</template>

<script lang="ts">
export interface ReceiptListState {
  status: '' | 'awaiting_upload' | 'pending_verification' | 'verified' | 'rejected' | 'failed'
  q: string
  department: string
  role_id: string
  processing_status: '' | 'none' | 'queued' | 'running' | 'completed' | 'needs_review' | 'failed'
  created_from: string
  created_to: string
  page: number
}
</script>

<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { getReceipts, getRoles, materialTitle, uploaderPosition, PortalError, receiptErrorMessage, receiptStatusLabels, receiptStatusName, processingStatusLabels, processingStatusName, processingStageName, processingItemStatusName, processingReasonName, type PortalSession, type ReceiptCounts, type ReceiptRecord, type KnowledgeRole, type Scope } from '../api/knowledgePortal'
const props = defineProps<{ session: PortalSession; scope: Scope; refreshKey: number; state?: Partial<ReceiptListState> }>()
const emit = defineEmits<{ error: [error: unknown]; openJob: [id: string]; pending: [value: boolean]; 'update:state': [value: ReceiptListState] }>()
const defaults: ReceiptListState = { status: '', q: '', department: '', role_id: '', processing_status: '', created_from: '', created_to: '', page: 1 }
function normalized(value?: Partial<ReceiptListState>): ReceiptListState {
  return { status: value?.status || '', q: value?.q || '', department: value?.department || '', role_id: value?.role_id || '', processing_status: value?.processing_status || '', created_from: value?.created_from || '', created_to: value?.created_to || '', page: Number.isSafeInteger(value?.page) && value!.page! > 0 ? value!.page! : 1 }
}
const applied = ref(normalized(props.state))
const status = ref(applied.value.status), q = ref(applied.value.q), department = ref(applied.value.department), role_id = ref(applied.value.role_id)
const processing_status = ref(applied.value.processing_status), created_from = ref(applied.value.created_from), created_to = ref(applied.value.created_to), page = ref(applied.value.page)
const items = ref<ReceiptRecord[]>([]), counts = ref<ReceiptCounts | null>(null), total = ref<number | null>(null)
const loading = ref(false), error = ref(''), roles = ref<KnowledgeRole[]>([]), roleError = ref('')
const copyReceiptId = ref(''), copyNotice = ref('')
const pageSize = 20
let requestId = 0, controller: AbortController | null = null, roleRequest = 0, copyRequest = 0
let active = true
const canRead = computed(() => props.scope === 'company' ? props.session.permissions.includes('company_knowledge.review') : props.session.permissions.some(value => ['company_knowledge.review', 'company_knowledge.submissions.read'].includes(value)))
const canReadRoles = computed(() => canRead.value && props.session.permissions.some(value => ['company_knowledge.read', 'company_knowledge.review'].includes(value)))
const departments = computed(() => [...new Set(roles.value.map(role => role.department))])
const availableRoles = computed(() => roles.value.filter(role => !department.value || role.department === department.value))
const hasFilters = computed(() => Object.entries(applied.value).some(([key, value]) => key !== 'page' && !!value))
const formatDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '时间未提供' : new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(date) }
const formatBytes = (value: number) => Number.isFinite(value) ? value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.ceil(value / 1024))} KB` : '大小未提供'
const afterDate = (value: string) => new Date(Date.parse(`${value}T00:00:00Z`) + 86400000).toISOString().slice(0, 10)
function latestUpdatedAt(item: ReceiptRecord) {
  return [item.updated_at, item.job_updated_at, item.role_assignments_updated_at, ...(item.role_assignments || []).map(value => value.updated_at)].filter((value): value is string => !!value).reduce((latest, value) => Date.parse(value) > Date.parse(latest) ? value : latest, item.updated_at)
}
function clear() { requestId++; copyRequest++; copyReceiptId.value = ''; copyNotice.value = ''; controller?.abort(); controller = null; items.value = []; counts.value = null; total.value = null; loading.value = false; emit('pending', false) }
function applyState(value: ReceiptListState) {
  applied.value = value; status.value = value.status; q.value = value.q; department.value = value.department; role_id.value = value.role_id
  processing_status.value = value.processing_status; created_from.value = value.created_from; created_to.value = value.created_to; page.value = value.page
}
function publishState(value: ReceiptListState) { applied.value = value; page.value = value.page; emit('update:state', { ...value }) }
async function loadRoles() {
  const current = ++roleRequest
  if (!canReadRoles.value) { roles.value = []; roleError.value = ''; return }
  if (!active) return
  try { const result = await getRoles(); if (current === roleRequest) { roles.value = result.items; roleError.value = '' } }
  catch (reason) { if (current === roleRequest) { roles.value = []; roleError.value = '岗位目录暂未读取成功，已保存的筛选仍会保留。'; emit('error', reason) } }
}
async function copyRecord(item: ReceiptRecord) {
  const current = ++copyRequest
  copyReceiptId.value = item.receipt_id; copyNotice.value = ''
  const content = [`原文件名：${item.filename}`, `收件编号：${item.receipt_id}`, `核验状态码：${item.status}`, ...(item.error_code ? [`原始错误码：${item.error_code}`] : [])].join('\n')
  try { await navigator.clipboard.writeText(content); if (current === copyRequest) copyNotice.value = '已复制核验信息' }
  catch { if (current === copyRequest) copyNotice.value = '未能自动复制，请选中上方信息复制。' }
}
async function load() {
  if (!canRead.value) { clear(); error.value = '当前身份无权查看收件明细。'; return }
  if (!active) return
  controller?.abort(); controller = new AbortController(); const signal = controller.signal, current = ++requestId
  loading.value = true; error.value = ''
  try {
    const selected = applied.value
    const result = await getReceipts(props.scope, selected.status, page.value, pageSize, signal, {
      q: selected.q, department: selected.department, role_id: selected.role_id, processing_status: selected.processing_status,
      created_from: selected.created_from ? `${selected.created_from}T00:00:00+08:00` : '', created_to: selected.created_to ? `${afterDate(selected.created_to)}T00:00:00+08:00` : '',
    })
    if (current !== requestId) return
    items.value = result.items; counts.value = result.receipt_counts; total.value = result.total
    emit('pending', result.receipt_counts.pending_verification > 0 || result.items.some(item => item.job_status === 'queued' || item.job_status === 'running'))
    const last = Math.max(1, Math.ceil(result.total / pageSize))
    if (page.value > last) { publishState({ ...applied.value, page: last }); await load() }
  } catch (reason) {
    if (current !== requestId || signal.aborted) return
    items.value = []; counts.value = null; total.value = null
    emit('pending', false)
    error.value = reason instanceof PortalError && reason.status === 404 ? '当前服务尚不支持收件历史查询，不能据此判断没有异常。' : reason instanceof Error ? reason.message : '收件记录暂时无法读取，请稍后重试。'
    emit('error', reason)
  } finally { if (current === requestId) loading.value = false }
}
function filter() {
  if (created_from.value && created_to.value && created_from.value > created_to.value) { error.value = '收件开始日期不能晚于结束日期。'; return }
  const next = normalized({ status: status.value, q: q.value.trim(), department: department.value, role_id: role_id.value, processing_status: processing_status.value, created_from: created_from.value, created_to: created_to.value, page: 1 })
  applyState(next); publishState(next); clear(); void load()
}
function showAll() { applyState({ ...defaults }); publishState({ ...defaults }); clear(); void load() }
defineExpose({ showAll })
function changePage(next: number) { if (loading.value || next < 1) return; publishState({ ...applied.value, page: next }); items.value = []; void load() }
watch([() => props.session.principal.id, () => props.scope, canRead, canReadRoles, () => JSON.stringify(props.state)], (value, previous) => {
  const identityChanged = !previous || value.slice(0, 4).some((field, index) => field !== previous[index])
  const next = normalized(props.state)
  if (!identityChanged && JSON.stringify(next) === JSON.stringify(applied.value)) return
  applyState(next); clear(); void load()
  if (identityChanged) { roles.value = []; roleError.value = ''; void loadRoles() }
}, { immediate: true })
watch(() => props.refreshKey, () => { if (!loading.value) void load() })
onActivated(() => { if (active) return; active = true; void load(); void loadRoles() })
onDeactivated(() => { active = false; requestId++; roleRequest++; controller?.abort(); controller = null; loading.value = false; emit('pending', false) })
onBeforeUnmount(() => { roleRequest++; clear() })
</script>

<style scoped>
.receipt-history { margin-bottom: 24px; }
.receipt-filters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; padding: 0 24px 20px; }
.receipt-filters label { display: flex; flex-direction: column; gap: 6px; color: #454d59; font-size: 12px; }
.receipt-filters input, .receipt-filters select { width: 100%; box-sizing: border-box; min-height: 38px; padding: 7px 9px; border: 1px solid #c9d1dd; border-radius: 4px; color: #252a34; background: white; font: inherit; }
.receipt-filters input:focus-visible, .receipt-filters select:focus-visible { outline: 2px solid #36c; outline-offset: 2px; }
.receipt-filter-actions { display: flex; align-items: end; gap: 15px; }
.receipt-filter-warning { margin: 0; padding: 0 24px 18px; font-size: 12px; color: #7a592b; }
.receipt-summary { margin: 0; padding: 0 24px 18px; color: #677363; font-size: 12px; }
.receipt-title { display: block; max-width: 38rem; min-width: 200px; overflow-wrap: anywhere; font-size: 14px; }
.receipt-size, .receipt-created { display: block; margin-top: 7px; color: #72777d; }
.receipt-assignment { min-width: 175px; max-width: 250px; font-size: 12px; line-height: 1.7; }
.receipt-assignment ul { margin: 0; padding: 0; list-style: none; }
.receipt-assignment li + li { margin-top: 10px; }
.receipt-assignment strong, .receipt-assignment small, .receipt-assignment span { display: block; overflow-wrap: anywhere; }
.receipt-assignment span { color: #417141; }
.receipt-technical { margin-top: 9px; max-width: 38rem; font-size: 12px; }
.receipt-technical summary { width: fit-content; cursor: pointer; color: #54595d; }
.receipt-technical summary:focus-visible { outline: 2px solid #36c; outline-offset: 3px; }
.receipt-technical dl { margin: 10px 0; padding: 10px; border: 1px solid #eaecf0; background: #f8f9fa; }
.receipt-technical dl > div + div { margin-top: 8px; }
.receipt-technical dt { color: #72777d; }
.receipt-technical dd { margin: 3px 0 0; overflow-wrap: anywhere; user-select: text; }
.receipt-copy-notice { margin-top: 7px; color: #54595d; }
.receipt-outcome { min-width: 180px; max-width: 340px; }
.receipt-outcome p { margin: 7px 0; font-size: 12px; line-height: 1.7; }
.receipt-item-counts { display: flex; flex-wrap: wrap; gap: 4px 12px; margin: 8px 0; padding: 0; list-style: none; font-size: 12px; }
.receipt-reasons { margin: 8px 0; padding-left: 16px; font-size: 12px; line-height: 1.7; }
.receipt-no-job { color: #54595d; }
.receipt-pages { display: flex; gap: 18px; }
.receipt-pages button:disabled { opacity: .45; cursor: default; }
@media (max-width: 1000px) { .receipt-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 540px) { .receipt-filters { grid-template-columns: 1fr; padding-inline: 18px; } }
</style>
