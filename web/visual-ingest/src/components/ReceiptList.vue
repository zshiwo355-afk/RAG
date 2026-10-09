<template>
  <section class="processing-section receipt-history" aria-labelledby="receipts-title">
    <div class="list-heading">
      <div><h2 id="receipts-title">{{ scope === 'company' ? '公司收件核验' : '我的收件核验' }}<span v-if="total !== null" class="total-count">{{ total }}</span></h2><p>保留等待上传、核验中与未通过的收件；核验通过后，才会进入下方内容处理。</p></div>
      <label class="status-control">核验状态<select v-model="status" @change="filter"><option value="">全部状态</option><option v-for="(label, value) in receiptStatusLabels" :key="value" :value="value">{{ label }}</option></select></label>
    </div>
    <p v-if="counts" class="receipt-summary">当前范围共 {{ counts.total }} 份收件 · {{ counts.rejected }} 份拒收 · {{ counts.failed }} 份核验失败 · {{ counts.pending_verification }} 份核验中 · {{ counts.awaiting_upload }} 份待上传</p>
    <div v-if="error" class="message error inset-message" role="alert">{{ error }} <button class="text-button" @click="load">重新读取收件</button></div>
    <div v-if="loading && !items.length" class="list-state" role="status">正在读取收件历史…</div>
    <div v-else-if="!loading && !error && !items.length" class="list-state"><h3>{{ status ? '此核验状态下没有收件' : '暂无可见的收件记录' }}</h3><p>申请上传后即保留收件记录，不以是否生成知识判断。</p></div>
    <div v-if="items.length" class="table-wrap" :aria-busy="loading" tabindex="0" aria-label="收件核验列表">
      <table class="jobs-table"><thead><tr><th scope="col">原件与回执</th><th scope="col">核验状态</th><th scope="col">结果与下一步</th><th scope="col">更新时间</th></tr></thead>
        <tbody><tr v-for="item in items" :key="item.receipt_id">
          <td><strong>{{ item.filename }}</strong><small class="receipt-id">{{ item.receipt_id }}</small><small>{{ formatBytes(item.byte_length) }}</small></td>
          <td><span class="status-badge" :class="{ failed: item.status === 'failed', needs_review: item.status === 'rejected', completed: item.status === 'verified' }">{{ receiptStatusLabels[item.status] }}</span></td>
          <td class="receipt-outcome"><p v-if="item.error_code">{{ receiptErrorMessage(item.error_code) }}</p>
            <p v-if="item.status === 'rejected'">此回执已结束；原样提交会返回同一记录。请核对并修正资料后提交，或凭编号联系维护人员。</p>
            <p v-else-if="item.status === 'failed'">尚未进入内容处理。可用原资料在上传页继续提交，由系统再次核验。</p>
            <p v-else-if="item.status === 'awaiting_upload'">已建立回执，尚未确认原件上传；可在上传页用原资料继续提交。</p>
            <p v-else-if="item.status === 'pending_verification'">原件仍在核验，暂未进入内容处理。</p>
            <button v-if="item.job_id" class="text-button" @click="emit('openJob', item.job_id)">查看关联处理任务 →</button>
            <p v-else-if="item.status === 'verified'">原件核验通过，尚未关联处理任务；不代表正式发布。</p>
          </td><td class="time-cell">{{ formatDate(item.updated_at) }}</td>
        </tr></tbody>
      </table>
    </div>
    <div v-if="total !== null && total > 0" class="list-footer"><span>共 {{ total }} 份收件 · 第 {{ page }} / {{ Math.max(1, Math.ceil(total / pageSize)) }} 页</span><div class="receipt-pages"><button class="text-button" :disabled="loading || page <= 1" @click="changePage(page - 1)">上一页</button><button class="text-button" :disabled="loading || page * pageSize >= total" @click="changePage(page + 1)">下一页</button></div></div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { getReceipts, PortalError, receiptErrorMessage, receiptStatusLabels, type PortalSession, type ReceiptCounts, type ReceiptRecord, type ReceiptStatus, type Scope } from '../api/knowledgePortal'
const props = defineProps<{ session: PortalSession; scope: Scope; refreshKey: number }>()
const emit = defineEmits<{ error: [error: unknown]; openJob: [id: string] }>()
const items = ref<ReceiptRecord[]>([]), counts = ref<ReceiptCounts | null>(null), total = ref<number | null>(null)
const status = ref<ReceiptStatus | ''>(''), page = ref(1), loading = ref(false), error = ref('')
const pageSize = 20
let requestId = 0, controller: AbortController | null = null
const canRead = computed(() => props.scope === 'company' ? props.session.permissions.includes('company_knowledge.review') : props.session.permissions.some(value => ['company_knowledge.review', 'company_knowledge.submissions.read'].includes(value)))
const formatDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '时间未提供' : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(date) }
const formatBytes = (value: number) => value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MiB` : `${Math.max(1, Math.ceil(value / 1024))} KiB`
function clear() { requestId++; controller?.abort(); controller = null; items.value = []; counts.value = null; total.value = null; loading.value = false }
async function load() {
  if (!canRead.value) { clear(); error.value = '当前身份无权查看收件明细。'; return }
  controller?.abort(); controller = new AbortController(); const signal = controller.signal, current = ++requestId
  loading.value = true; error.value = ''
  try {
    const result = await getReceipts(props.scope, status.value, page.value, pageSize, signal)
    if (current !== requestId) return
    items.value = result.items; counts.value = result.receipt_counts; total.value = result.total
    const last = Math.max(1, Math.ceil(result.total / pageSize))
    if (page.value > last) { page.value = last; await load() }
  } catch (reason) {
    if (current !== requestId || signal.aborted) return
    items.value = []; counts.value = null; total.value = null
    error.value = reason instanceof PortalError && reason.status === 404 ? '当前服务尚不支持收件历史查询，不能据此判断没有异常。' : reason instanceof Error ? reason.message : '收件记录暂时无法读取，请稍后重试。'
    emit('error', reason)
  } finally { if (current === requestId) loading.value = false }
}
function filter() { page.value = 1; clear(); void load() }
function changePage(next: number) { if (loading.value) return; page.value = next; items.value = []; void load() }
watch(() => [props.session.principal.id, props.scope, canRead.value], () => { page.value = 1; status.value = ''; clear(); void load() }, { immediate: true })
watch(() => props.refreshKey, () => { if (!loading.value) void load() })
onBeforeUnmount(clear)
</script>

<style scoped>
.receipt-history { margin-bottom: 24px; }
.receipt-summary { margin: 0; padding: 0 24px 18px; color: #677363; font-size: 12px; }
.receipt-id { display: block; margin: 7px 0; overflow-wrap: anywhere; font-size: 10px; }
.receipt-outcome { min-width: 230px; max-width: 420px; }
.receipt-outcome p { margin: 0 0 7px; font-size: 12px; line-height: 1.7; }
.receipt-pages { display: flex; gap: 18px; }
.receipt-pages button:disabled { opacity: .45; cursor: default; }
</style>
