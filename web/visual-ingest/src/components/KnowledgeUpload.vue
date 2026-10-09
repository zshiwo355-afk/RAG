<template>
  <main id="knowledge-main" class="portal-main knowledge-upload" tabindex="-1">
    <header class="upload-heading"><div><span class="eyebrow">ADD TO THE SHARED LIBRARY</span><h1>把经验，交给下一位同事。</h1><p>上传原件，补充用途；系统负责核验、清洗、查重和处理。</p></div><button type="button" class="upload-link" @click="emit('processing')">查看上传与异常 ↗</button></header>
    <section v-if="!canUpload" class="upload-unavailable"><h2>当前账号未开放上传</h2><p>管理员可在 MCP 角色中配置知识提交权限。无需另建上传账号。</p></section>
    <div v-else class="upload-layout">
      <form class="upload-form" @submit.prevent="submit">
        <section class="upload-panel"><div class="upload-section-title"><span>01</span><h2>选择一份资料</h2></div>
          <div class="upload-drop" :class="{ dragging: dragOver, selected: file, locked }" @dragover.prevent="!locked && (dragOver = true)" @dragleave.prevent="dragOver = false" @drop.prevent="dropFile">
            <span class="upload-file-mark" aria-hidden="true">{{ file ? '▤' : '↥' }}</span><strong>{{ file?.name || '拖入资料，或选择文件' }}</strong><p>{{ file ? `${formatBytes(file.size)} · ${isZip ? '现有资产资料包' : '保留原始文件'}` : 'Markdown、TXT、Word（.docx）、PDF 或现有资产 ZIP' }}</p>
            <label class="upload-pick" :class="{ disabled: locked }">{{ file ? '更换文件' : '选择文件' }}<input ref="fileInput" type="file" accept=".md,.txt,.docx,.pdf,.zip" :disabled="locked" aria-label="选择知识资料文件" @change="pickFile"></label><small>一次上传一份。原件最多 {{ formatBytes(originalLimit) }}，资产 ZIP 最多 {{ formatBytes(packageLimit) }}。</small>
          </div>
          <p v-if="isZip" class="upload-hint">ZIP 应为已有资产资料包，包含交付清单和正文。系统按包内清单处理，普通文件压缩包可能需要补充信息。</p>
        </section>
        <fieldset v-if="file && !isZip" class="upload-panel upload-metadata" :disabled="locked"><legend class="sr-only">知识说明</legend><div class="upload-section-title"><span>02</span><h2>让同事知道怎么用</h2></div>
          <div class="upload-fields"><label>知识名称 <span>必填</span><input v-model="metadata.title" maxlength="240" required placeholder="例如：新员工客户交接流程"></label><label>知识类型 <span>必填</span><select v-model="metadata.kind" required><option value="" disabled>请选择类型</option><option v-for="entry in kinds" :key="entry.value" :value="entry.value">{{ entry.label }}</option></select></label></div>
          <label class="upload-field">用途 <span>必填</span><textarea v-model="metadata.purpose" maxlength="2000" rows="2" required placeholder="这份资料帮助同事完成什么，适合在什么情况下使用？" /></label>
          <details class="upload-reuse"><summary>补充使用信息 <span>{{ missingFields.length ? `还有 ${missingFields.length} 项待补充` : '已填写完整' }}</span></summary><p>以下信息用于判断能否直接复用。未填写的内容会进入待补充，不会被当成已经通过发布检查。</p>
            <label class="upload-field">使用对象<textarea v-model="metadata.audience" maxlength="2000" rows="2" placeholder="哪些岗位或同事适用？" /></label>
            <label class="upload-field">需要什么输入<textarea v-model="metadata.inputs" maxlength="2000" rows="2" placeholder="使用前需要准备哪些资料或数据？" /></label><button type="button" class="upload-fill" @click="metadata.inputs = '不需要额外输入'">不需要额外输入</button>
            <label class="upload-field">预期输出<textarea v-model="metadata.outputs" maxlength="2000" rows="2" placeholder="执行后应得到什么成果，怎样判断完成？" /></label>
            <label class="upload-field">依赖与权限<textarea v-model="metadata.dependencies" maxlength="2000" rows="2" placeholder="需要哪些系统、工具或权限？不要填写密码或密钥。" /></label><button type="button" class="upload-fill" @click="metadata.dependencies = '无额外依赖或特殊权限'">无额外依赖或特殊权限</button>
            <label class="upload-field">适用边界<textarea v-model="metadata.boundaries" maxlength="2000" rows="2" placeholder="哪些情况可以使用，哪些情况需要调整或不能使用？" /></label>
          </details>
          <p v-if="missingFields.length" class="upload-review-note">待补充：{{ missingFields.join('、') }}。你仍可提交，后台会记录缺项并进入待处理。</p>
        </fieldset>
        <section class="upload-panel upload-confirm"><label class="sharing-confirm"><input v-model="sharingConfirmed" type="checkbox" :disabled="locked"><span>我确认这份资料允许公司全员查看，且没有不应共享的个人信息、密码或密钥。</span></label><p>原文保存在私有 OSS。只有通过处理并正式发布的知识，才会进入全员可查的目录与图谱。</p>
          <div v-if="error" class="upload-error" role="alert">{{ error }}</div>
          <div class="upload-actions"><button v-if="!frozenPackage" type="submit" class="upload-primary" :disabled="running || !canSubmit">{{ running ? '正在提交…' : '提交资料' }}<span aria-hidden="true">↗</span></button><button v-else-if="!running && !jobFinished && receipt?.status !== 'rejected'" type="button" class="upload-primary" :disabled="!sharingConfirmed" @click="submit">{{ receipt?.content_verified ? '继续查询处理结果' : receipt?.status === 'failed' ? '再次核验原件' : '继续 / 重试本次提交' }}</button><button v-if="running" type="button" class="upload-secondary" @click="cancel">停止本页操作</button><button v-if="file && !running && frozenPackage" type="button" class="upload-secondary" @click="resetUpload">上传另一份 / 重新填写</button></div>
          <small v-if="frozenPackage">本次资料与说明已冻结；可重试的收件沿用同一回执。资料或说明改变后才形成新的提交，原样提交仍返回原记录。</small>
        </section>
      </form>
      <aside class="upload-progress" aria-label="本次提交进度" aria-live="polite">
        <span class="eyebrow">SUBMISSION STATUS</span><h2>{{ stageTitle }}</h2><p class="upload-status-note">{{ note || '选择文件并确认共享范围后开始。' }}</p>
        <ol class="upload-steps"><li v-for="(step, index) in steps" :key="step.title" :class="{ complete: stepIndex > index, current: stepIndex === index && running }"><span>{{ stepIndex > index ? '✓' : String(index + 1).padStart(2, '0') }}</span><div><strong>{{ step.title }}</strong><p>{{ step.description }}</p></div></li></ol>
        <dl v-if="receipt" class="upload-receipt"><div><dt>收件编号</dt><dd>{{ receipt.receipt_id }}</dd></div><div><dt>原件核验</dt><dd>{{ receipt.content_verified ? '已通过；不代表正式发布' : receiptLabel }}</dd></div><div v-if="receipt.error_code"><dt>核验原因</dt><dd>{{ receiptErrorMessage(receipt.error_code) }}</dd></div><div v-if="receipt.status === 'rejected'"><dt>下一步</dt><dd>此回执已结束，尚未进入内容处理。请核对并修正资料后提交；原样提交不会重试此回执。如原件无误，请凭收件编号联系维护人员。</dd></div></dl>
        <div v-if="job" class="upload-result"><h3>{{ job.status === 'failed' ? '后台处理失败' : job.status === 'needs_review' ? '需要补充或核对资料' : jobFinished ? '本次处理已结束' : '后台正在处理' }}</h3><div class="upload-counts"><span><strong>{{ job.counts.published || 0 }}</strong>发布回执</span><span><strong>{{ job.counts.duplicate || 0 }}</strong>重复内容</span><span><strong>{{ job.counts.draft || 0 }}</strong>草稿</span><span><strong>{{ job.counts.needs_review || 0 }}</strong>待处理</span></div><p v-if="job.counts.indexing">另有 {{ job.counts.indexing }} 项正在建立索引。</p><p v-if="job.counts.archived || job.counts.outdated">{{ job.counts.archived || 0 }} 项归档 · {{ job.counts.outdated || 0 }} 项历史版本</p><ul v-if="reasons.length" class="upload-reasons"><li v-for="reason in reasons" :key="reason">{{ reason }}</li></ul><p class="upload-result-boundary">发布回执是本次处理记录，正式知识是否可用以知识库当前状态为准。</p></div>
        <button v-if="receipt" type="button" class="upload-view-processing" @click="emit('processing')">{{ job ? '查看内容处理记录 →' : '查看收件核验历史 →' }}</button>
        <p class="upload-persistence">关闭页面或停止查询不会撤销已收到的资料，也不会停止服务端已经开始的处理。</p>
      </aside>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { getJob, getJobs, PortalError, processingReasonLabels as reasonLabels, receiptErrorMessage, receiptStatusLabels, type ReceiptStatus, type PortalSession, type ProcessingJob } from '../api/knowledgePortal'
import { FILE_LIMIT, PACKAGE_LIMIT, packageFile, prepareUpload, putOriginal, completeUpload, uploadStatus, type UploadMetadata, type UploadPackage, type Receipt } from '../api/knowledgeUpload'

const props = defineProps<{ session: PortalSession }>()
const emit = defineEmits<{ error: [error: unknown]; submitted: []; processing: [] }>()
const blankMetadata = (): UploadMetadata => ({ title: '', kind: '', purpose: '', audience: '', inputs: '', outputs: '', dependencies: '', boundaries: '' })
const metadata = reactive(blankMetadata())
const file = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const frozenPackage = ref<UploadPackage | null>(null)
const sharingConfirmed = ref(false)
const dragOver = ref(false)
const running = ref(false)
const error = ref('')
const note = ref('')
const stage = ref('idle')
const receipt = ref<Receipt | null>(null)
const job = ref<ProcessingJob | null>(null)
let operation = 0
let controller: AbortController | null = null
const canUpload = computed(() => props.session.capabilities.upload === true)
const originalLimit = computed(() => Math.min(FILE_LIMIT, props.session.upload_limits?.max_file_bytes || FILE_LIMIT))
const packageLimit = computed(() => Math.min(PACKAGE_LIMIT, props.session.upload_limits?.max_bytes || PACKAGE_LIMIT))
const isZip = computed(() => file.value?.name.toLowerCase().endsWith('.zip') === true)
const locked = computed(() => running.value || frozenPackage.value !== null)
const canSubmit = computed(() => canUpload.value && receipt.value?.status !== 'rejected' && file.value && sharingConfirmed.value && (isZip.value || metadata.title.trim() && metadata.kind && metadata.purpose.trim()))
const missingFields = computed(() => ([['audience', '使用对象'], ['inputs', '输入'], ['outputs', '输出'], ['dependencies', '依赖与权限'], ['boundaries', '适用边界']] as const).filter(([key]) => !metadata[key].trim()).map(([, label]) => label))
const receiptLabel = computed(() => receiptStatusLabels[receipt.value?.status as ReceiptStatus] || '尚未通过')
const jobFinished = computed(() => !!job.value && ['completed', 'needs_review', 'failed'].includes(job.value.status))
const kinds = [{ value: '流程', label: 'SOP / 流程' }, { value: 'Skill方法', label: 'Skill' }, { value: '案例', label: '案例' }, { value: '方法', label: '方法' }, { value: '模板', label: '模板' }, { value: '提示词', label: '提示词' }, { value: '规则', label: '规则' }, { value: '参考', label: '参考资料' }]
const steps = [{ title: '准备提交', description: '保留原文，整理你填写的说明' }, { title: '上传原件', description: '直接上传到私有 OSS' }, { title: '收件核验', description: '核对完整性与文件指纹' }, { title: '后台处理', description: '清洗、查重、发布或记录异常' }]
const stepIndex = computed(() => ({ idle: -1, packaging: 0, preparing: 0, uploading: 1, verifying: 2, processing: 3, done: 4 }[stage.value] ?? (job.value ? 3 : receipt.value?.content_verified ? 3 : receipt.value ? 2 : -1)))
const stageTitle = computed(() => ({ idle: '资料准备好了吗？', packaging: '正在准备资料', preparing: '正在申请上传', uploading: '原件正在上传', verifying: '正在核验收件', processing: '等待后台处理结果', done: job.value?.status === 'failed' ? '资料已收到，处理失败' : job.value?.status === 'needs_review' ? '资料已收到，需要补充' : '本次处理已完成', pending: '后台仍在继续处理', cancelled: '本页操作已停止', error: '本次操作未完成' }[stage.value] || '提交进度'))
const reasons = computed(() => [...new Set([...(job.value?.error_code ? [job.value.error_code] : []), ...(job.value?.items || []).flatMap(item => item.reason_codes)])].slice(0, 5).map(code => reasonLabels[code] || `需要进一步核对（${code}）`))
const formatBytes = (value: number) => value >= 1024 * 1024 ? `${Math.round(value / 1024 / 1024 * 10) / 10} MiB` : value >= 1024 ? `${Math.round(value / 1024 * 10) / 10} KiB` : `${value} B`

function clearSensitive() {
  operation++; controller?.abort(); controller = null; running.value = false; file.value = null; frozenPackage.value = null; sharingConfirmed.value = false; receipt.value = null; job.value = null
  Object.assign(metadata, blankMetadata()); if (fileInput.value) fileInput.value.value = ''
}
function resetUpload() { clearSensitive(); stage.value = 'idle'; error.value = ''; note.value = '' }
function chooseFiles(files: File[]) {
  if (locked.value || !canUpload.value) return
  error.value = ''; dragOver.value = false
  if (files.length !== 1) { error.value = '一次请选择一份资料。多个文件请分别提交，或使用已有结构完整的资产 ZIP。'; return }
  const candidate = files[0]
  const extension = candidate.name.slice(candidate.name.lastIndexOf('.')).toLowerCase()
  if (!['.md', '.txt', '.docx', '.pdf', '.zip'].includes(extension) || /[\x00-\x1f\x7f/\\]/.test(candidate.name)) { error.value = '请选择 .md、.txt、.docx、.pdf 或现有资产 .zip 文件。'; return }
  if (!candidate.size) { error.value = '文件为空，请选择有内容的资料。'; return }
  const limit = extension === '.zip' ? packageLimit.value : originalLimit.value
  if (candidate.size > limit) { error.value = `${extension === '.zip' ? '资产 ZIP' : '这份原件'}超过 ${formatBytes(limit)} 上限，请拆分后提交。`; return }
  resetUpload(); file.value = candidate; metadata.title = candidate.name.replace(/\.[^.]+$/, '').slice(0, 240)
}
function pickFile(event: Event) { const input = event.target as HTMLInputElement; chooseFiles(Array.from(input.files || [])); input.value = '' }
function dropFile(event: DragEvent) { dragOver.value = false; chooseFiles(Array.from(event.dataTransfer?.files || [])) }
function cancel() { operation++; controller?.abort(); controller = null; running.value = false; stage.value = 'cancelled'; note.value = receipt.value ? '已停止本页上传或查询。已取得的回执保留，可继续查询；后台任务不会被撤销。' : '已停止本页操作。你可以继续本次提交或选择其他资料。' }
function pause(signal: AbortSignal, milliseconds = 2000) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return }
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new DOMException('Aborted', 'AbortError')) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, milliseconds)
    signal.addEventListener('abort', abort, { once: true })
  })
}
async function timed<T>(parent: AbortSignal, request: (signal: AbortSignal) => Promise<T>, milliseconds = 30_000): Promise<T> {
  const child = new AbortController()
  let timeout = false
  const abort = () => child.abort()
  if (parent.aborted) child.abort(); else parent.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(() => { timeout = true; child.abort() }, milliseconds)
  try { return await request(child.signal) }
  catch (reason) { if (timeout && !parent.aborted) throw new Error('请求超时。可继续查询或重试，同一资料不会重复提交。'); throw reason }
  finally { clearTimeout(timer); parent.removeEventListener('abort', abort) }
}
async function followProcessing(signal: AbortSignal, current: number) {
  stage.value = 'processing'; note.value = '原件核验已通过，正在等待清洗、查重和发布结果；此时还不能视为正式知识。'
  const deadline = Date.now() + 120_000
  let searchPage = 1
  let historyScanned = false
  while (Date.now() < deadline && current === operation) {
    if (!job.value) {
      const result = await timed(signal, value => getJobs('self', '', historyScanned ? 1 : searchPage, 100, value))
      if (current !== operation) return
      job.value = result.items.find(item => item.receipt_id === receipt.value?.receipt_id) || null
      if (!job.value && !historyScanned && result.total > searchPage * 100) {
        if (searchPage >= 10) { stage.value = 'pending'; note.value = '收件已核验，但在最新 1,000 条处理记录中尚未定位到对应任务。本页无法据此判断是否排队，请保留回执并到处理中心核对历史记录。'; return }
        searchPage++; continue
      }
      historyScanned = true
    }
    if (job.value) {
      const result = await timed(signal, value => getJob(job.value!.job_id, 'self', 0, 0, 100, value))
      if (current !== operation) return
      job.value = result
      if (jobFinished.value) { stage.value = 'done'; note.value = result.status === 'needs_review' ? '资料已经收到。请按下方原因补充或核对，异常不会被自动当成正式知识。' : result.status === 'failed' ? '原件已经保留。后台处理失败，请到处理中心查看原因或申请重试。' : '请查看下方每项处理去向；重复、草稿和归档不会增加正式知识数量。'; emit('submitted'); return }
    }
    await pause(signal)
  }
  if (current === operation) { stage.value = 'pending'; note.value = job.value ? '后台仍在排队或处理中，本页已暂停轮询。可继续查询，也可到处理中心查看。' : '收件已核验，但尚未定位到对应的处理任务，当前不能确认是否排队。可继续查询或到处理中心核对回执。' }
}
async function submit() {
  if (running.value || !canSubmit.value) return
  const current = ++operation
  controller?.abort(); controller = new AbortController(); const signal = controller.signal
  running.value = true; error.value = ''; note.value = ''
  try {
    if (!frozenPackage.value) { stage.value = 'packaging'; const value = await packageFile(file.value!, { ...metadata }); if (current !== operation) return; if (value.blob.size > packageLimit.value) throw new Error('整理后的资料包超过上传上限，请拆分后提交。'); frozenPackage.value = value }
    stage.value = 'preparing'
    let result = await timed(signal, value => prepareUpload(frozenPackage.value!, value))
    if (current !== operation) return
    receipt.value = result.receipt; emit('submitted')
    if (result.receipt.status === 'rejected') throw new Error((receiptErrorMessage(result.receipt.error_code) || '这份原件未通过核验。') + ' 此回执已结束，原样提交不会重新核验。')
    if (!result.receipt.content_verified && result.upload) { stage.value = 'uploading'; note.value = '原件正在直接上传到私有 OSS，请保持页面打开。'; await timed(signal, value => putOriginal(frozenPackage.value!, result, value), 120_000); if (current !== operation) return; result = await timed(signal, value => completeUpload(receipt.value!.receipt_id, value)); if (current !== operation) return; receipt.value = result.receipt }
    else if (result.receipt.status === 'failed') { result = await timed(signal, value => completeUpload(receipt.value!.receipt_id, value)); if (current !== operation) return; receipt.value = result.receipt }
    stage.value = 'verifying'; note.value = '上传字节已提交，等待后台核对文件完整性。收件核验通过不等于正式发布。'
    const deadline = Date.now() + 90_000
    while (!receipt.value.content_verified && Date.now() < deadline) {
      if (['rejected', 'failed'].includes(receipt.value.status)) throw new Error((receiptErrorMessage(receipt.value.error_code) || '原件核验未完成或未通过。') + (receipt.value.status === 'rejected' ? ' 此回执已结束，原样提交不会重新核验。' : ' 可以继续使用本回执再次核验。'))
      await pause(signal)
      result = await timed(signal, value => uploadStatus(receipt.value!.receipt_id, value))
      if (current !== operation) return
      receipt.value = result.receipt
    }
    if (!receipt.value.content_verified) { stage.value = 'pending'; note.value = '收件仍在核验，本页已暂停轮询。点击继续查询可以跟进；后台核验不受影响。'; return }
    emit('submitted'); await followProcessing(signal, current)
  } catch (reason) {
    if (current !== operation || signal.aborted) return
    if (reason instanceof PortalError && [401, 403].includes(reason.status)) { clearSensitive(); stage.value = 'error'; error.value = reason.status === 401 ? '登录已过期，本页文件和填写内容已清除，请重新登录。' : '上传权限已发生变化，本页文件和填写内容已清除，请联系管理员核对权限。'; emit('error', reason); return }
    stage.value = 'error'; error.value = reason instanceof Error ? reason.message : '本次操作未完成，请稍后重试。'; note.value = receipt.value?.status === 'rejected' ? '核验拒收已记录，可在收件历史中查看；尚未进入内容处理。' : receipt.value ? '已取得的收件编号保留。重试继续使用同一资料和回执。' : '原件仍保留在本机，可以重试。'
    emit('error', reason)
  } finally { if (current === operation) { running.value = false; controller?.abort(); controller = null } }
}
watch(() => props.session.principal.id, () => resetUpload())
watch(canUpload, value => { if (!value) resetUpload() })
onBeforeUnmount(clearSensitive)
</script>

<style scoped>
.knowledge-upload { color: #29483a; }
.upload-heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 28px; }
.upload-heading h1 { margin: 12px 0; font: 500 30px/1.45 'Songti SC', 'Noto Serif CJK SC', serif; }
.upload-heading p { margin: 0; color: #7a856f; font-size: 12px; line-height: 1.8; }
.upload-link, .upload-fill { border: 0; background: none; color: #56734e; font: inherit; font-size: 11px; cursor: pointer; text-align: left; }
.upload-layout { display: grid; grid-template-columns: minmax(0, 1fr) 295px; gap: 23px; align-items: start; }
.upload-form { min-width: 0; }
.upload-panel { min-width: 0; margin: 0 0 16px; border: 1px solid #dde4d3; border-radius: 8px; padding: 23px; background: #fcfdf9; }
.upload-section-title { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.upload-section-title > span { color: #9dab8d; font-size: 11px; }
.upload-section-title h2 { margin: 0; font-size: 15px; font-weight: 500; }
.upload-drop { display: flex; flex-direction: column; align-items: center; border: 1px dashed #c2cfb3; border-radius: 6px; background: #f6f8ef; padding: 22px 16px; text-align: center; }
.upload-drop.dragging { background: #e9f0df; border-color: #5d8154; }
.upload-file-mark { font: 32px/1.2 Georgia, serif; color: #839b6c; margin-bottom: 10px; }
.upload-drop strong { max-width: 100%; overflow-wrap: anywhere; font-size: 13px; font-weight: 500; }
.upload-drop p { margin: 9px 0 13px; font-size: 11px; color: #849276; line-height: 1.7; }
.upload-drop small { font-size: 10px; color: #9aa68b; line-height: 1.8; }
.upload-pick { position: relative; display: inline-flex; align-items: center; min-height: 34px; padding: 0 17px; margin-bottom: 13px; border: 1px solid #c5d2b7; border-radius: 4px; background: #fff; color: #526e45; font-size: 11px; cursor: pointer; }
.upload-pick input { position: absolute; inset: 0; opacity: 0; width: 100%; cursor: pointer; }
.upload-pick:focus-within { outline: 2px solid #a77e42; outline-offset: 3px; }
.upload-pick.disabled { opacity: .5; cursor: default; }
.upload-hint, .upload-review-note { font-size: 11px; line-height: 1.8; margin: 13px 0 0; color: #8d7b51; }
.upload-fields { display: grid; grid-template-columns: minmax(0, 1fr) 160px; gap: 15px; }
.upload-fields label, .upload-field { display: block; font-size: 11px; color: #647759; }
.upload-fields label > span, .upload-field > span { margin-left: 5px; color: #a0ad92; font-size: 9px; }
.upload-metadata input, .upload-metadata select, .upload-metadata textarea { display: block; box-sizing: border-box; width: 100%; max-width: 100%; margin-top: 8px; padding: 9px 10px; border: 1px solid #d5dfca; border-radius: 4px; background: #fff; color: #405839; font: inherit; font-size: 12px; line-height: 1.65; }
.upload-metadata textarea { resize: vertical; min-height: 62px; }
.upload-field { margin-top: 17px; }
.upload-reuse { margin-top: 20px; border-top: 1px solid #e1e7d9; padding-top: 15px; }
.upload-reuse summary { color: #657b58; font-size: 12px; cursor: pointer; }
.upload-reuse summary > span { margin-left: 7px; color: #a1ac96; font-size: 10px; }
.upload-reuse > p { color: #8c987f; font-size: 11px; line-height: 1.8; }
.upload-fill { padding: 6px 0; font-size: 10px; }
.sharing-confirm { display: flex; align-items: start; gap: 10px; font-size: 12px; line-height: 1.8; color: #58714c; }
.sharing-confirm input { margin-top: 4px; accent-color: #466d43; flex-shrink: 0; }
.upload-confirm > p, .upload-confirm > small { color: #929e84; font-size: 10px; line-height: 1.9; }
.upload-actions { display: flex; flex-wrap: wrap; gap: 9px; margin: 18px 0 8px; }
.upload-primary, .upload-secondary { display: inline-flex; align-items: center; justify-content: space-between; gap: 18px; min-height: 37px; padding: 0 16px; border: 1px solid #365c3d; border-radius: 4px; background: #365c3d; color: #fff; font: inherit; font-size: 11px; cursor: pointer; }
.upload-secondary { background: #fff; border-color: #c8d5ba; color: #6e835e; }
button:disabled, fieldset:disabled input, fieldset:disabled select, fieldset:disabled textarea { opacity: .55; cursor: default; }
.upload-error { margin: 12px 0; padding: 11px 13px; border: 1px solid #e4cbbb; background: #faf0e8; color: #946848; border-radius: 4px; font-size: 11px; line-height: 1.8; }
.upload-progress { position: sticky; top: 22px; min-width: 0; padding: 23px; border: 1px solid #dce5cd; border-radius: 8px; background: #eff4e5; }
.upload-progress h2 { margin: 15px 0 10px; font: 500 21px/1.5 'Songti SC', serif; }
.upload-status-note { color: #80936e; font-size: 11px; line-height: 1.9; }
.upload-steps { list-style: none; margin: 25px 0 20px; padding: 0; }
.upload-steps li { position: relative; display: flex; gap: 12px; padding-bottom: 23px; color: #93a482; }
.upload-steps li:not(:last-child)::after { content: ''; position: absolute; left: 11px; top: 26px; bottom: 3px; width: 1px; background: #d5e0c8; }
.upload-steps li > span { flex-shrink: 0; display: grid; place-items: center; width: 23px; height: 23px; border: 1px solid #d0ddc1; border-radius: 50%; font-size: 9px; }
.upload-steps strong { font-size: 11px; font-weight: 500; }
.upload-steps p { font-size: 10px; line-height: 1.7; margin: 6px 0 0; color: #a0af90; }
.upload-steps .current > span { background: #53764b; border-color: #53764b; color: white; }
.upload-steps .current strong, .upload-steps .complete { color: #486b3e; }
.upload-receipt { margin: 0; padding: 14px 0; border-top: 1px solid #d8e2cc; }
.upload-receipt > div { margin-bottom: 11px; }
.upload-receipt dt { color: #92a17f; font-size: 10px; }
.upload-receipt dd { margin: 5px 0 0; font-size: 11px; line-height: 1.8; color: #647e53; overflow-wrap: anywhere; }
.upload-result { border-top: 1px solid #d8e2cc; padding-top: 17px; }
.upload-result h3 { font-size: 13px; line-height: 1.7; font-weight: 500; margin: 0 0 13px; }
.upload-counts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.upload-counts > span { display: flex; flex-direction: column; gap: 5px; color: #879c75; font-size: 10px; }
.upload-counts strong { font-size: 24px; font-weight: 400; color: #506f42; }
.upload-result > p, .upload-reasons { color: #859775; font-size: 10px; line-height: 1.9; }
.upload-reasons { padding-left: 16px; }
.upload-result .upload-result-boundary { color: #a0ad92; }
.upload-view-processing { width: 100%; border: 1px solid #c7d5b9; border-radius: 4px; background: #fbfdf7; padding: 9px; color: #637f51; font: inherit; font-size: 11px; cursor: pointer; }
.upload-persistence { font-size: 10px; color: #a0ae91; line-height: 1.9; margin-bottom: 0; }
.upload-unavailable { padding: 35px; border: 1px solid #dce3d3; border-radius: 7px; background: #f9fbf5; }
.upload-unavailable h2 { font-size: 18px; font-weight: 500; }
.upload-unavailable p { font-size: 12px; line-height: 1.8; color: #89977c; }
button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid #a77e42; outline-offset: 3px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 1080px) { .upload-layout { grid-template-columns: minmax(0, 1fr) 265px; gap: 15px; } .upload-panel, .upload-progress { padding: 18px; } }
@media (max-width: 800px) { .upload-layout { grid-template-columns: minmax(0, 1fr); } .upload-progress { position: static; } .upload-heading { flex-wrap: wrap; } .upload-heading h1 { font-size: 25px; } .upload-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
