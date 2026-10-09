import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const settings = { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }
const actualAPI = moduleURL(ts.transpileModule(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8'), settings).outputText)
const { PortalError, receiptErrorMessage } = await import(actualAPI)
const apiURL = moduleURL(`export { PortalError, receiptErrorMessage, receiptStatusLabels } from '${actualAPI}';
 export async function getReceipts(...args) { globalThis.calls.push(args); return globalThis.receiptsPlan(...args) }`)
const source = await readFile(new URL('../src/components/ReceiptList.vue', import.meta.url), 'utf8')
const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'receipt-history' }).content, settings).outputText
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)
const component = (await import(moduleURL(code))).default
const record = { receipt_id: 'a'.repeat(32), filename: 'safe.zip', status: 'rejected', error_code: 'object_hash_mismatch', job_id: null, retryable: false }
const result = { items: [record], total: 41, receipt_counts: { total: 41, rejected: 1, failed: 0 } }
async function harness(permissions = ['company_knowledge.submissions.read'], scope = 'self') {
  let state; const events = []; globalThis.calls = []
  await renderToString(createSSRApp({ ...component, setup(props, context) { state = component.setup(props, context); return state }, render() { return null } }, {
    session: { principal: { id: 'alice' }, permissions }, scope, refreshKey: 0, onError: value => events.push(value),
  }))
  return { state, events }
}
globalThis.receiptsPlan = () => result
let { state, events } = await harness(); await state.load()
assert.equal(state.items.value[0].status, 'rejected'); assert.equal(state.items.value[0].job_id, null)
assert.equal(state.counts.value.rejected, 1)
state.changePage(2); await state.load(); assert.equal(globalThis.calls.at(-1)[2], 2)
state.status.value = 'rejected'; state.filter(); await state.load()
assert.deepEqual(globalThis.calls.at(-1).slice(0, 4), ['self', 'rejected', 1, 20])
assert.match(receiptErrorMessage(record.error_code), /文件指纹/)
assert(!receiptErrorMessage('https://private.invalid/?secret=token').includes('secret'))
for (const status of [401, 403]) {
  globalThis.receiptsPlan = () => { throw new PortalError(status, 'denied') }
  await state.load(); assert.deepEqual(state.items.value, []); assert.equal(state.counts.value, null); assert.equal(state.total.value, null)
  assert(events.some(value => value instanceof PortalError && value.status === status))
}
globalThis.receiptsPlan = () => { throw new PortalError(404, 'missing') }; await state.load()
assert.match(state.error.value, /不能据此判断没有异常/)
globalThis.receiptsPlan = () => result
;({ state } = await harness(['company_knowledge.dashboard.read'], 'company')); await state.load()
assert.equal(globalThis.calls.length, 0); assert.equal(state.items.value.length, 0)
;({ state } = await harness(['company_knowledge.review'], 'company')); await state.load()
assert.equal(globalThis.calls.at(-1)[0], 'company')
let finish
globalThis.receiptsPlan = () => new Promise(resolve => { finish = resolve })
const pending = state.load(); state.clear(); finish(result); await pending
assert.deepEqual(state.items.value, [], 'a late response must not restore cleared private records')
console.log('Receipt history: rejected originals without jobs, pagination, filters, safe reasons, denied access and stale response clearing passed.')
