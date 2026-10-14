import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'
import ts from 'typescript'

const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const apiURL = moduleURL(transpile(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8')))
const api = await import(apiURL)
globalThis.window = { location: { hash: '' }, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} }
globalThis.document = { title: '', addEventListener() {}, removeEventListener() {} }
const readOnlySession = { authenticated: true, principal: { id: 'synthetic-admin' }, permissions: ['company_knowledge.review'], capabilities: { upload: false, review_write: false } }
let sent
globalThis.fetch = async (path, options) => { sent = { path, ...options }; return { ok: true, status: 200, json: async () => readOnlySession } }
assert.deepEqual(await api.createSession('admin', ' synthetic-password '), readOnlySession)
assert.equal(sent.path, '/portal/knowledge/session')
assert.equal(sent.method, 'POST')
assert.equal(sent.credentials, 'same-origin')
assert.equal(sent.cache, 'no-store')
assert.equal(sent.headers['X-Knowledge-Portal-Request'], '1')
assert.deepEqual(JSON.parse(sent.body), { username: 'admin', password: ' synthetic-password ' })
globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({ error: { code: 'invalid_credentials' } }) })
await assert.rejects(api.createSession('admin', 'wrong-password'), error => error instanceof api.PortalError && error.status === 401)

const stubURL = moduleURL('export default {}')
const portalURL = moduleURL(`
  export { PortalError, processingReasonLabels, processingEventLabels } from '${apiURL}';
  import { PortalError } from '${apiURL}';
  export async function getSession() { throw new PortalError(401, 'unauthenticated') }
  export async function createSession(username, password) { globalThis.loginPlan.calls.push({ username, password }); return globalThis.loginPlan.create() }
  export async function deleteSession() { if(globalThis.loginPlan.logoutError) throw globalThis.loginPlan.logoutError }
  export async function getOverview() { return { counts: {} } }
  export async function getJobs() { return { items: [], total: 0 } }
  export async function getDraft() {}
  export async function getJob() {}
  export async function retryJob() {}
  export async function resolveItem() {}
`)
const source = await readFile(new URL('../src/views/KnowledgePortal.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
assert.match(descriptor.template.content, /id="knowledge-username"[^>]*autocomplete="username"/)
assert.match(descriptor.template.content, /id="knowledge-password"[^>]*type="password"[^>]*autocomplete="current-password"/)
assert.doesNotMatch(source, /123456|knowledge-credential|localStorage|sessionStorage/)
const code = transpile(compileScript(descriptor, { id: 'login-check' }).content)
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${portalURL}'`)
  .replace(/from (['"])\.\.\/components\/[^'"]+\.vue\1/g, `from '${stubURL}'`)
  .replace(/import ['"]\.\.\/knowledge-portal.css['"];?/, '')
const component = (await import(moduleURL(code))).default
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
let state
const app = renderer.createApp({ ...component, setup(props, context) { state = component.setup(props, context); return state }, render() { return null } })
globalThis.loginPlan = { calls: [], create: async () => readOnlySession }
app.mount({})
try {
  await nextTick()
  await state.login()
  assert.equal(globalThis.loginPlan.calls.length, 0, 'empty fields do not submit')
  state.username.value = '  admin  '; state.password.value = ' synthetic-password '
  let finish
  globalThis.loginPlan.create = () => new Promise(resolve => { finish = resolve })
  const pending = state.login()
  assert.equal(state.password.value, '', 'password clears as soon as submission starts')
  assert.equal(state.signingIn.value, true)
  state.password.value = 'ignored'; await state.login()
  assert.equal(globalThis.loginPlan.calls.length, 1, 'repeated submission stays blocked')
  assert.deepEqual(globalThis.loginPlan.calls[0], { username: 'admin', password: ' synthetic-password ' })
  finish(readOnlySession); await pending
  assert.equal(state.scope.value, 'company')
  assert.equal(state.canReviewWrite.value, false)
  assert.equal(state.visibleViews.value.some(view => view.key === 'upload'), false)
  state.activeView.value = 'upload'; await nextTick()
  assert.equal(state.activeView.value, 'overview', 'read-only accounts cannot stay at the old upload link')
  assert.equal(window.location.hash, 'overview')
  state.session.value = { ...readOnlySession, capabilities: { upload: true, review_write: false } }
  state.activeView.value = 'upload'; await nextTick()
  assert.equal(state.activeView.value, 'upload', 'employee upload access remains available')
  assert.equal(state.visibleViews.value.some(view => view.key === 'upload'), true)
  await state.logout()
  assert.equal(state.session.value, null)
  assert.equal(state.overview.value, null)
  assert.equal(state.username.value, '')
  assert.equal(state.password.value, '')

  state.username.value = 'admin'; state.password.value = 'wrong-password'
  globalThis.loginPlan.create = async () => { throw new api.PortalError(401, 'invalid_credentials') }
  await state.login()
  assert.match(state.loginError.value, /账号或密码不正确/)
  assert.equal(state.password.value, '')
  assert.equal(state.signingIn.value, false)
  assert.equal(state.session.value, null)
  globalThis.loginPlan.logoutError = new Error('network unavailable')
  await state.logout()
  assert.equal(state.logoutUnconfirmed.value, true)
  const calls = globalThis.loginPlan.calls.length
  state.username.value = 'admin'; state.password.value = 'synthetic-password'; await state.login()
  assert.equal(globalThis.loginPlan.calls.length, calls, 'unconfirmed logout blocks a new login')
} finally { app.unmount() }
console.log('Knowledge login: account/password request, cookie/CSRF, input validation, password clearing, read-only navigation, employee upload access, errors and logout passed.')
