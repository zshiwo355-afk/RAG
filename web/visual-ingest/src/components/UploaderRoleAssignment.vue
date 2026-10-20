<template>
  <section class="role-assignment" :aria-labelledby="`role-title-${itemId}`" :aria-busy="loading || saving">
    <h3 :id="`role-title-${itemId}`">{{ legacyRolePending ? '岗位选择已保存，待重新检查' : assignment?.role ? roleNeedsReview ? '岗位归属仍需核对' : '岗位已确认' : '确认这份资料的岗位归属' }}</h3>
    <p v-if="loading && !assignment" role="status">正在读取岗位…</p>
    <p v-else-if="!assignment" class="message error" role="alert">{{ error }} <button class="text-button" type="button" @click="load()">重新读取</button></p>
    <form v-else @submit.prevent="save">
      <p class="current-role">上传原声明：<strong>{{ assignment.original.department || '部门未提供' }} / {{ assignment.original.position || '岗位未提供' }}</strong></p>
      <p class="current-role">{{ roleNeedsReview ? '已保存归属选择' : '已确认归属' }}：<strong>{{ assignment.role ? `${assignment.role.department} / ${assignment.role.name}` : '尚未确认' }}</strong><span v-if="assignment.confirmation_source === 'legacy'">（已恢复此前保存的选择）</span><span v-else-if="assignment.confirmation_source === 'alias'">（按已记住的匹配规则）</span></p>
      <p v-if="statusMessage" class="role-status" role="status">{{ statusMessage }}</p>
      <p v-if="loading" class="role-help" role="status">正在更新岗位状态…</p>
      <ul v-if="remainingReasons.length" class="remaining-reasons" aria-label="仍需处理的问题"><li v-for="reason in remainingReasons" :key="reason">{{ processingReasonName(reason) }}</li></ul>
      <p class="role-help">本次只确认这份资料。原上传声明保留，已发布资料保持不变。</p>
      <button v-if="assignment.role && !editing && assignment.eligible" class="text-button" type="button" :disabled="locked" @click="editing = true">更改岗位归属</button>
      <template v-if="editing || !assignment.role">
      <div class="role-fields">
        <label :for="`role-department-${itemId}`">部门<select :id="`role-department-${itemId}`" v-model="department" :disabled="locked" required @change="roleId = ''; newRoleName = ''"><option disabled value="">请选择已有部门</option><option v-for="name in departments" :key="name" :value="name">{{ name }}</option></select></label>
        <label :for="`role-choice-${itemId}`">岗位<select :id="`role-choice-${itemId}`" v-model="roleId" :disabled="locked || !department" required><option disabled value="">请选择岗位</option><option v-for="role in availableRoles" :key="role.role_id" :value="role.role_id">{{ role.name }}</option><option v-if="department" value="__new__">＋ 添加岗位…</option></select></label>
      </div>
      <div v-if="roleId === '__new__'" class="new-role">
        <label :for="`role-new-${itemId}`">新岗位名称<input :id="`role-new-${itemId}`" v-model="newRoleName" :disabled="locked" maxlength="200" required autocomplete="off" :aria-describedby="`role-new-help-${itemId}`" placeholder="填写完整岗位名称"></label>
        <p :id="`role-new-help-${itemId}`" class="role-help">将添加到“{{ department }}”，并用于这份资料。同部门已有同名岗位时会直接使用。</p>
      </div>
      <label v-if="assignment.can_remember" class="remember-choice"><input v-model="rememberRule" type="checkbox" :disabled="locked || assignment.rule_status === 'conflict'">记住原声明“{{ assignment.original.department }} / {{ assignment.original.position }}”的匹配，供后续相同声明使用</label>
      <p v-else-if="!assignment.original.department || !assignment.original.position" class="role-help">原声明缺少完整部门或岗位，本次可指定归属；不会建立全公司同名岗位的匹配规则。</p>
      <p v-else class="role-help">这份资料暂不能建立匹配规则，本次只确认当前资料的归属。</p>
      </template>
      <p v-if="assignment.rule_status === 'conflict'" class="message notice">相同原声明已有其他岗位匹配。本次可单独确认；记忆规则冲突需要另外核对，不会自动覆盖。</p>
      <p v-else-if="assignment.remembered" class="role-help">相同部门和岗位声明的匹配规则已记住。</p>
      <label v-if="remainingReasons.includes('source_relation_review') && assignment.eligible" class="remember-choice"><input v-model="separateSource" type="checkbox" :disabled="locked">我确认这份资料是独立的新资料，保留原有正式资料，另行处理本条</label>
      <p v-if="!roles.length" class="role-help">岗位目录暂无可选岗位，请先补充岗位目录。</p>
      <p v-if="jobBusy" class="role-help">这次收件正在处理，处理完成后可{{ assignment.role ? '更改' : '确认' }}岗位。</p>
      <p v-else-if="!assignment.eligible" class="role-help">当前条目不符合修改岗位的条件，请核对处理结果。</p>
      <p class="role-help">确认岗位后仍会检查附件、脚本和内容完整性；通过全部检查后才会入库。</p>
      <p v-if="dirty" class="message notice" role="status">当前选择尚未保存，请确认后保存；已确认归属仍以上方记录为准。</p>
      <p v-if="error" class="message error" role="alert">{{ error }}</p>
      <p v-if="notice" class="message success" role="status">{{ notice }}</p>
      <button v-if="editing || !assignment.role || separateSource || legacyRolePending" class="button primary" type="submit" :disabled="locked || !validSelection">{{ saving ? '正在保存…' : roleId === '__new__' ? '添加岗位并确认本条' : separateSource && !editing ? '确认来源关系并重新检查' : legacyRolePending && !editing ? '使用已保存岗位重新检查' : '保存本条岗位并重新检查' }}</button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { createRole, getRoles, getItemUploaderRole, setItemUploaderRole, processingReasonName, processingItemStatusName, PortalError, type JobStatus, type KnowledgeRole, type ItemUploaderRoleAssignment } from '../api/knowledgePortal'

const props = defineProps<{ jobId: string; itemId: string; itemVersion: number; itemStatus?: string; jobStatus: JobStatus; disabled?: boolean }>()
const emit = defineEmits<{ saved: [notice: string]; busy: [value: boolean]; dirty: [value: boolean]; error: [value: unknown] }>()
const roles = ref<KnowledgeRole[]>([])
const assignment = ref<ItemUploaderRoleAssignment | null>(null)
const department = ref('')
const roleId = ref('')
const newRoleName = ref('')
const rememberRule = ref(false)
const separateSource = ref(false)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const editing = ref(false)
let generation = 0
let refreshPending = false
const departments = computed(() => [...new Set(roles.value.map(role => role.department))])
const availableRoles = computed(() => roles.value.filter(role => role.department === department.value))
const validSelection = computed(() => roleId.value === '__new__'
  ? departments.value.includes(department.value) && !!newRoleName.value.trim() && newRoleName.value.length <= 200 && !/[\u0000-\u001f\u007f]/u.test(newRoleName.value)
  : availableRoles.value.some(role => role.role_id === roleId.value))
const jobBusy = computed(() => props.jobStatus === 'queued' || props.jobStatus === 'running')
const remainingReasons = computed(() => props.itemStatus && props.itemStatus !== 'needs_review' ? [] : assignment.value?.remaining_reasons || [])
const roleNeedsReview = computed(() => remainingReasons.value.some(reason => ['uploader_role_not_found', 'uploader_role_ambiguous', 'uploader_position_is_department', 'uploader_department_mismatch', 'uploader_role_conflict'].includes(reason)))
const legacyRolePending = computed(() => !!assignment.value?.role && assignment.value.confirmation_source === 'legacy' && roleNeedsReview.value && assignment.value.eligible)
const statusMessage = computed(() => {
  if (!assignment.value?.role) return ''
  if (legacyRolePending.value) return '此前的岗位选择已恢复，尚未按该选择重新检查；本次提交仍待处理，尚未正式入库。'
  if (roleNeedsReview.value) return '岗位选择已保存，但仍有岗位问题需要核对。请按以下原因处理。'
  if (remainingReasons.value.length) return '岗位已确认，本条仍需处理以下问题；无须重复选岗。'
  if (props.jobStatus === 'queued') return '岗位已确认，资料等待处理。后续进度可在收件记录查看；通过全部检查后才会入库。'
  if (props.jobStatus === 'running') return '岗位已确认，资料正在处理。后续进度可在收件记录查看。'
  if (props.itemStatus && props.itemStatus !== 'needs_review') return `岗位已确认，当前条目：${processingItemStatusName(props.itemStatus)}。`
  if (props.jobStatus === 'failed') return '岗位已确认，任务处理未完成，请核对当前待处理问题。'
  return '岗位已确认，处理结果以收件记录的最新状态为准。'
})
const locked = computed(() => loading.value || saving.value || props.disabled || jobBusy.value || assignment.value?.eligible === false)
const dirty = computed(() => !!assignment.value && (department.value !== (assignment.value.role?.department || '')
  || roleId.value !== (assignment.value.role?.role_id || '') || !!newRoleName.value || separateSource.value
  || rememberRule.value !== (assignment.value.can_remember && assignment.value.rule_status !== 'conflict')))
const message = (value: unknown) => value instanceof Error ? value.message : '请求未完成，请稍后重试。'
function selectCurrent(value: ItemUploaderRoleAssignment, preserveDraft = false) {
  assignment.value = value
  if (preserveDraft) return
  department.value = value.role?.department || ''
  roleId.value = value.role?.role_id || ''
  newRoleName.value = ''
  rememberRule.value = value.can_remember && value.rule_status !== 'conflict'
  separateSource.value = false
  editing.value = false
}
async function load(reset = false) {
  const current = ++generation
  const preserveDraft = !reset && (dirty.value || editing.value)
  loading.value = true; notice.value = ''
  if (!preserveDraft) error.value = ''
  if (reset) {
    assignment.value = null; roles.value = []; department.value = ''; roleId.value = ''; newRoleName.value = ''
    rememberRule.value = false; separateSource.value = false; editing.value = false
  }
  try {
    const [directory, binding] = await Promise.all([getRoles(), getItemUploaderRole(props.jobId, props.itemId)])
    if (current !== generation) return
    roles.value = directory.items; selectCurrent(binding, preserveDraft)
  } catch (value) {
    if (current !== generation) return
    error.value = message(value); emit('error', value)
  } finally { if (current === generation) loading.value = false }
}
async function save() {
  if (locked.value || !assignment.value || !validSelection.value) return
  const current = generation
  const id = props.jobId, item = props.itemId
  let addedRole = false
  saving.value = true; emit('busy', true); error.value = ''; notice.value = ''
  try {
    if (roleId.value === '__new__') {
      const role = await createRole(department.value, newRoleName.value.trim())
      if (current !== generation) return
      roles.value = [...roles.value.filter(existing => existing.role_id !== role.role_id), role]
      department.value = role.department; roleId.value = role.role_id; newRoleName.value = ''; addedRole = true
    }
    const result = await setItemUploaderRole(id, item, { role_id: roleId.value, expected_version: assignment.value.version,
      remember_rule: assignment.value.can_remember && assignment.value.rule_status !== 'conflict' && rememberRule.value,
      source_action: separateSource.value && assignment.value.remaining_reasons.includes('source_relation_review') ? 'new' : 'auto' })
    if (current !== generation) return
    selectCurrent(result)
    notice.value = roleNeedsReview.value ? '本条岗位选择已保存，仍有岗位问题需要核对。' : '本条岗位已确认，正在刷新处理状态。'
    if (result.rule_status === 'saved' || result.rule_status === 'matched') notice.value += '相同原声明的匹配规则已记住。'
    else if (result.rule_status === 'conflict') notice.value += '已有匹配规则存在冲突，本次没有覆盖。'
    emit('saved', notice.value)
  } catch (value) {
    if (current !== generation) return
    error.value = `${addedRole ? '岗位已在目录中，但本条归属尚未保存。' : ''}${message(value)}`; emit('error', value)
    if (value instanceof PortalError && value.status === 409) {
      try {
        const binding = await getItemUploaderRole(id, item)
        if (current !== generation) return
        selectCurrent(binding, true)
      } catch (refreshError) {
        if (current !== generation) return
        error.value += ` 最新归属读取失败：${message(refreshError)}当前选择已保留。`; emit('error', refreshError)
      }
      if (current === generation) emit('saved', '')
    }
  } finally { if (current === generation) {
    saving.value = false; emit('busy', false)
    if (refreshPending) { refreshPending = false; void load() }
  } }
}
watch(() => [props.jobId, props.itemId, props.itemVersion, props.jobStatus, props.itemStatus], (value, previous) => {
  const reset = !previous || value[0] !== previous[0] || value[1] !== previous[1]
  if (reset) { saving.value = false; refreshPending = false; emit('busy', false) }
  if (saving.value) refreshPending = true
  else void load(reset)
}, { immediate: true })
watch(dirty, value => emit('dirty', value), { immediate: true })
onBeforeUnmount(() => { generation++; emit('busy', false); emit('dirty', false) })
</script>

<style scoped>
.role-assignment { margin: 20px 0; padding: 18px; border: 1px solid #dce3ed; border-radius: 5px; background: #f8faff; }
h3 { margin: 0 0 8px; font-size: 15px; color: #252a34; }
.role-help { margin: 8px 0 12px; font-size: 12px; line-height: 1.7; color: #646d7b; }
.current-role { margin: 14px 0; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.role-status { margin: 12px 0; color: #334d37; font-size: 13px; line-height: 1.7; }
.message { margin: 12px 0; }
.role-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 12px; }
.new-role { margin-top: 14px; }
label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: #454d59; }
select, input { box-sizing: border-box; width: 100%; min-height: 38px; padding: 7px 9px; border: 1px solid #c9d1dd; border-radius: 4px; color: #252a34; background: white; font: inherit; }
select:focus-visible, input:focus-visible, button:focus-visible { outline: 2px solid #2e63d9; outline-offset: 3px; }
select:disabled, input:disabled { color: #737c8c; background: #f1f3f6; }
.remember-choice { margin: 14px 0; display: flex; flex-direction: row; align-items: flex-start; gap: 8px; line-height: 1.7; }
.remember-choice input { width: 16px; min-height: 16px; margin-top: 3px; flex-shrink: 0; }
.remaining-reasons { padding-left: 20px; color: #644b27; font-size: 12px; line-height: 1.7; }
button { max-width: 100%; white-space: normal; }
@media (max-width: 540px) { .role-fields { grid-template-columns: 1fr; } .role-assignment { padding: 14px; } }
</style>
