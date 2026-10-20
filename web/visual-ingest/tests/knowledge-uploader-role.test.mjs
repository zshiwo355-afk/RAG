import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createRenderer, createSSRApp, nextTick, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const url = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const actualAPI = url(transpile(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8')))
const api = await import(actualAPI)
globalThis.window = { setTimeout, clearTimeout }
let sent
globalThis.fetch = async (path, options) => { sent = { path, ...options }; return { ok: true, status: 200, json: async () => ({}) } }
await api.setUploaderRole('job/a', 'role_' + 'a'.repeat(24), 4)
assert.equal(sent.path, '/portal/knowledge/api/jobs/job%2Fa/uploader-role')
assert.equal(sent.headers['X-Knowledge-Portal-Request'], '1')
assert.deepEqual(JSON.parse(sent.body), { role_id: 'role_' + 'a'.repeat(24), expected_version: 4 }, 'the caller cannot provide the uploader identity')
await api.setItemUploaderRole('job/a', 'item/b', { role_id: 'role_' + 'a'.repeat(24), expected_version: 1, remember_rule: true, source_action: 'auto' })
assert.equal(sent.path, '/portal/knowledge/api/jobs/job%2Fa/items/item%2Fb/uploader-role')
assert.deepEqual(JSON.parse(sent.body), { role_id: 'role_' + 'a'.repeat(24), expected_version: 1, remember_rule: true, source_action: 'auto' })
await api.createRole('总经办', '知识交接专员')
assert.equal(sent.path, '/portal/knowledge/api/roles')
assert.deepEqual(JSON.parse(sent.body), { department: '总经办', name: '知识交接专员' })
const stub = url(`export { PortalError, processingReasonName, processingItemStatusName } from '${actualAPI}';
  export async function createRole(...args) { globalThis.roleCreates.push(args); return globalThis.rolePlans.create(...args) }
  export async function getRoles() { return globalThis.rolePlans.roles() }
  export async function getItemUploaderRole(id, item) { return globalThis.rolePlans.get(id, item) }
  export async function setItemUploaderRole(...args) { globalThis.roleWrites.push(args); return globalThis.rolePlans.save(...args) }`)
const source = await readFile(new URL('../src/components/UploaderRoleAssignment.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
const script = compileScript(descriptor, { id: 'uploader-role' })
const code = transpile(script.content)
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${stub}'`)
const component = (await import(url(code))).default
const template = compileTemplate({ source: descriptor.template.content, filename: 'UploaderRoleAssignment.vue', id: 'uploader-role',
  compilerOptions: { bindingMetadata: script.bindings } }).code.replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
const { render } = await import(url(template))
const directory = [
  { role_id: 'role_' + 'a'.repeat(24), department: '总经办', name: 'AI架构师' },
  { role_id: 'role_' + 'b'.repeat(24), department: '组织人才发展中心', name: 'SSC（共享服务岗·薪酬与员工关系）' },
]
const fields = { original: { department: null, position: 'SSC' }, confirmation_source: null, can_remember: false, remembered: false, rule_status: 'incomplete', remaining_reasons: ['unresolved_reference'], eligible: true }
const unassigned = { ...fields, role: null, version: 0, updated_at: null, requeued: false }
const binding = (index, version, requeued = false) => ({ ...fields, role: directory[index], version, updated_at: '2026-10-17T04:00:00Z', requeued })
const flush = async () => { await nextTick(); await new Promise(setImmediate) }
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
let state
const itemId = ref('first-item'), itemVersion = ref(1), itemStatus = ref('needs_review')
const jobId = ref('first-job'), status = ref('needs_review'), disabled = ref(false), events = [], busy = [], errors = [], dirtyEvents = []
const roleHTML = () => renderToString(createSSRApp({ ...component, render, setup: () => state }, {
  jobId: jobId.value, itemId: itemId.value, itemVersion: itemVersion.value, itemStatus: itemStatus.value, jobStatus: status.value, disabled: disabled.value,
}))
globalThis.roleWrites = []; globalThis.roleCreates = []
globalThis.rolePlans = { roles: () => ({ items: directory }), get: () => unassigned, save: () => binding(0, 1) }
const child = { ...component, setup(props, context) { state = component.setup(props, context); return state }, render() { return null } }
const app = renderer.createApp({ render() { return h(child, { jobId: jobId.value, itemId: itemId.value, itemVersion: itemVersion.value, itemStatus: itemStatus.value, jobStatus: status.value, disabled: disabled.value, onSaved: () => events.push('saved'), onBusy: value => busy.push(value), onError: value => errors.push(value), onDirty: value => dirtyEvents.push(value) }) } })
app.mount({})
try {
  await flush()
  assert.equal(state.assignment.value.version, 0)
  assert.equal(state.roleId.value, '', 'a textual upload declaration is not guessed into a role')
  assert.equal(state.dirty.value, false)
  await state.save(); assert.equal(roleWrites.length, 0)
  state.department.value = directory[1].department; state.roleId.value = directory[0].role_id
  await state.save(); assert.equal(roleWrites.length, 0, 'a role from another selected department cannot be saved')
  state.department.value = directory[0].department
  await flush(); assert.equal(dirtyEvents.at(-1), true, 'a changed selection warns its parent before navigation')
  let finish
  rolePlans.save = () => new Promise(resolve => { finish = resolve })
  const saving = state.save(); await state.save()
  assert.equal(roleWrites.length, 1, 'double click does not issue another mutation')
  assert.deepEqual(roleWrites[0], ['first-job', 'first-item', { role_id: directory[0].role_id, expected_version: 0, remember_rule: false, source_action: 'auto' }])
  finish(binding(0, 1)); await saving
  assert.match(state.notice.value, /岗位已确认.*刷新处理状态/, 'saving a role does not imply content is published')
  assert.match(state.statusMessage.value, /仍需处理.*无须重复选岗/)
  assert.equal(state.editing.value, false, 'a confirmed role no longer asks for repeated selection')
  assert.equal(state.dirty.value, false)
  assert.deepEqual(busy.slice(-2), [true, false])
  assert.equal(events.length, 1)
  // A legacy confirmation on this item is preserved, but another item never inherits a shared default.
  rolePlans.get = () => binding(0, 1); rolePlans.save = () => binding(0, 2, true)
  jobId.value = 'second-job'; await flush(); await state.save()
  assert.deepEqual(roleWrites.at(-1), ['second-job', 'first-item', { role_id: directory[0].role_id, expected_version: 1, remember_rule: false, source_action: 'auto' }])
  assert(!state.notice.value.includes('进入处理队列'), 'the requeued response flag is not current job status')
  assert.deepEqual(state.remainingReasons.value, ['unresolved_reference'], 'a historical requeued flag cannot hide unresolved issues')
  rolePlans.get = () => ({ ...binding(0, 2), remaining_reasons: [] })
  status.value = 'queued'; await flush()
  assert.match(state.statusMessage.value, /等待处理/)
  assert.deepEqual(state.remainingReasons.value, [])
  rolePlans.get = () => binding(0, 2)
  status.value = 'needs_review'; await flush()
  assert.match(state.statusMessage.value, /仍需处理/)
  assert.deepEqual(state.remainingReasons.value, ['unresolved_reference'], 'polling updates blockers even when itemVersion is unchanged')
  status.value = 'running'; await flush()
  assert.deepEqual(state.remainingReasons.value, ['unresolved_reference'], 'processing a sibling cannot hide this item\'s existing blocker')
  status.value = 'needs_review'; await flush()
  rolePlans.get = () => ({ ...binding(0, 2), confirmation_source: 'manual', remaining_reasons: ['uploader_role_conflict'] })
  itemVersion.value++; await flush()
  assert.equal(state.roleNeedsReview.value, true)
  assert.match(state.statusMessage.value, /选择已保存.*仍有岗位问题/, 'a saved choice cannot dismiss a conflict with an established source')
  assert(!(await roleHTML()).includes('使用已保存岗位重新检查'), 'an established-source role conflict has no legacy recovery shortcut')

  const legacyReview = { ...binding(1, 7), confirmation_source: 'legacy', remaining_reasons: ['uploader_role_conflict'] }
  rolePlans.get = () => legacyReview
  itemId.value = 'legacy-review-item'; await flush()
  assert.match(state.statusMessage.value, /此前的岗位选择已恢复.*尚未按该选择重新检查.*本次提交仍待处理.*尚未正式入库/)
  const legacyHTML = await roleHTML()
  const recoveryButton = legacyHTML.match(/<button\b[^>]*>使用已保存岗位重新检查<\/button>/)
  assert(recoveryButton && !recoveryButton[0].includes('disabled'), 'a valid restored role has an enabled direct submit button')
  assert(!legacyHTML.includes('请选择已有部门'), 'rechecking a restored choice does not require choosing the same role again')
  rolePlans.save = () => ({ ...binding(1, 8, true), confirmation_source: 'manual', remaining_reasons: [] })
  await state.save()
  assert.deepEqual(roleWrites.at(-1), ['second-job', 'legacy-review-item', {
    role_id: directory[1].role_id, expected_version: 7, remember_rule: false, source_action: 'auto',
  }], 'the shortcut submits the restored role/version through the existing save path without splitting a source')
  rolePlans.get = () => ({ ...binding(1, 8), confirmation_source: 'manual', remaining_reasons: [] })
  status.value = 'queued'; itemStatus.value = 'queued'; await flush()
  assert.match(state.statusMessage.value, /等待处理/)
  rolePlans.get = () => ({ ...binding(1, 8), confirmation_source: 'manual', remaining_reasons: ['source_relation_review'] })
  status.value = 'needs_review'; itemStatus.value = 'needs_review'; await flush()
  assert.deepEqual(state.remainingReasons.value, ['source_relation_review'])
  assert.match(state.statusMessage.value, /仍需处理.*无须重复选岗/)
  assert(!/已发布|已入库|发布成功/.test(state.statusMessage.value + state.notice.value), 'queued then source review never claims publication')
  assert(!(await roleHTML()).includes('使用已保存岗位重新检查'), 'completed legacy recovery does not become a generic retry button')
  rolePlans.get = () => ({ ...legacyReview, eligible: false })
  itemId.value = 'locked-legacy-item'; await flush()
  assert(!(await roleHTML()).includes('使用已保存岗位重新检查'), 'a locked legacy item cannot use the recovery shortcut')

  rolePlans.get = () => ({ ...binding(1, 3), confirmation_source: 'legacy' })
  itemId.value = 'second-item'; await flush()
  assert.equal(state.roleId.value, directory[1].role_id, 'existing item confirmation is restored')
  assert.match(state.statusMessage.value, /岗位已确认/)
  assert(!(await roleHTML()).includes('使用已保存岗位重新检查'), 'a legacy choice with only attachment problems does not request another role check')
  state.rememberRule.value = true
  rolePlans.save = () => binding(1, 4)
  await state.save()
  assert.equal(roleWrites.at(-1)[1], 'second-item', 'save targets one item, not the package')
  assert.equal(roleWrites.at(-1)[2].remember_rule, false, 'missing department cannot create a company-wide name alias')
  rolePlans.get = () => ({ ...binding(1, 4), original: { department: '组织人才发展中心', position: 'SSC' }, can_remember: true, remaining_reasons: ['source_relation_review'] })
  itemId.value = 'third-item'; await flush()
  assert.equal(state.rememberRule.value, true, 'a complete original declaration remembers the mapping by default')
  state.rememberRule.value = true; state.separateSource.value = true
  await state.save()
  assert.equal(roleWrites.at(-1)[2].remember_rule, true)
  assert.equal(roleWrites.at(-1)[2].source_action, 'new', 'a source split requires an explicit choice')
  rolePlans.get = () => ({ ...binding(1, 4), can_remember: true, rule_status: 'conflict' })
  itemId.value = 'conflicting-rule-item'; await flush()
  assert.equal(state.rememberRule.value, false, 'an existing different mapping is never replaced by default')
  state.rememberRule.value = true; state.separateSource.value = true
  await state.save()
  assert.equal(roleWrites.at(-1)[2].remember_rule, false)
  assert.equal(roleWrites.at(-1)[2].source_action, 'auto', 'no source split is requested without a source-relation blocker')
  const count = roleWrites.length
  status.value = 'running'; await flush(); await state.save(); assert.equal(roleWrites.length, count)
  status.value = 'needs_review'; disabled.value = true; await flush(); await state.save(); assert.equal(roleWrites.length, count)
  disabled.value = false; await flush()
  // Another reviewer wins. Refresh the saved assignment without discarding this reviewer's draft.
  state.department.value = directory[0].department; state.roleId.value = directory[0].role_id
  state.editing.value = true
  rolePlans.save = () => { throw new api.PortalError(409, 'uploader_role_version_conflict') }
  rolePlans.get = () => binding(1, 3)
  await state.save()
  assert.equal(state.assignment.value.version, 3)
  assert.equal(state.assignment.value.role.role_id, directory[1].role_id)
  assert.equal(state.roleId.value, directory[0].role_id, 'a conflict must preserve the unsaved selection')
  assert.equal(state.dirty.value, true)
  itemVersion.value++; await flush()
  assert.equal(state.roleId.value, directory[0].role_id, 'a refreshed item also preserves the unsaved selection')
  assert.match(state.error.value, /其他操作更新/)
  assert.equal(errors.at(-1).status, 409)
  // Add a role within a saved department, then bind it; a partial failure is explicit and retryable.
  const newRole = { role_id: 'role_' + 'c'.repeat(24), department: directory[0].department, name: '知识交接专员' }
  rolePlans.create = () => newRole
  state.roleId.value = '__new__'; state.department.value = '未保存部门'; state.newRoleName.value = newRole.name
  await state.save(); assert.equal(roleCreates.length, 0)
  state.department.value = directory[0].department
  for (const invalid of ['   ', 'a'.repeat(201), '岗位\n名称']) {
    state.newRoleName.value = invalid; await state.save(); assert.equal(roleCreates.length, 0)
  }
  state.newRoleName.value = '  知识交接专员  '
  rolePlans.create = () => { throw new api.PortalError(404, 'role_department_not_found') }
  const beforeCreate = roleWrites.length
  await state.save()
  assert.equal(roleWrites.length, beforeCreate, 'failed role creation cannot change the default')
  assert.match(state.error.value, /部门已不在目录/)
  let finishCreate
  rolePlans.create = () => new Promise(resolve => { finishCreate = resolve })
  rolePlans.save = () => { throw new api.PortalError(503, 'processing_unavailable') }
  const creating = state.save(); await state.save()
  assert.equal(roleCreates.length, 2, 'only one in-flight creation request')
  assert.deepEqual(roleCreates.at(-1), [directory[0].department, newRole.name])
  finishCreate(newRole); await creating
  assert.match(state.error.value, /岗位已在目录中，但本条归属尚未保存/)
  assert.equal(state.assignment.value.version, 3)
  assert.equal(state.roleId.value, newRole.role_id)
  rolePlans.save = () => ({ ...fields, role: newRole, version: 4, updated_at: null, requeued: false })
  await state.save()
  assert.equal(roleCreates.length, 2, 'retry only binds the already-created role')
  assert.equal(state.assignment.value.role.role_id, newRole.role_id)
  assert.equal(state.roles.value.filter(role => role.role_id === newRole.role_id).length, 1)
  assert.equal(state.dirty.value, false, 'only a confirmed save clears the navigation warning')
  // Changing jobs while adding a role must never bind the returned role to another uploader.
  state.roleId.value = '__new__'; state.newRoleName.value = '另一岗位'
  rolePlans.create = () => new Promise(resolve => { finishCreate = resolve })
  const staleCreation = state.save(); const beforeSwitch = roleWrites.length
  jobId.value = 'new-job'; await flush()
  finishCreate(newRole); await staleCreation
  assert.equal(roleWrites.length, beforeSwitch)
  // A response from an earlier job cannot overwrite the currently visible upload's mapping.
  let oldRead
  rolePlans.get = id => id === 'third-job' ? new Promise(resolve => { oldRead = resolve }) : binding(0, 8)
  jobId.value = 'third-job'; await flush()
  jobId.value = 'fourth-job'; await flush()
  oldRead(binding(1, 7)); await flush()
  assert.equal(state.assignment.value.version, 8)
  rolePlans.save = () => new Promise(resolve => { finish = resolve })
  const staleSave = state.save(); const emitted = events.length
  app.unmount(); finish(binding(1, 9)); await staleSave
  assert.equal(events.length, emitted, 'a late save response cannot restore a closed or revoked view')
  assert.equal(busy.at(-1), false)
} finally { app.unmount() }
console.log('Item-scoped uploader role: identity-free requests, department selection, content gates, duplicate clicks, historical job recovery, optimistic version conflict and stale-response isolation passed.')
