import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const receiptAPI = moduleURL(ts.transpileModule(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8'), {compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText)
const portalURL = moduleURL(`export { receiptErrorMessage, receiptStatusLabels, processingReasonLabels } from '${receiptAPI}'; export class PortalError extends Error { constructor(status) { super('权限错误'); this.status = status } }
 export async function getJobs(scope,status,page) { globalThis.plan.calls.push('jobs'); globalThis.plan.pages.push(page); return globalThis.plan.jobPage && page < globalThis.plan.jobPage ? {items:[],total:globalThis.plan.jobPage*100} : {items:[globalThis.plan.job],total:globalThis.plan.jobPage ? globalThis.plan.jobPage*100 : 1}; }
 export async function getJob() { globalThis.plan.calls.push('job'); return globalThis.plan.job; }`)
const { PortalError } = await import(portalURL)
const uploadURL = moduleURL(`export const FILE_LIMIT=8*1024*1024, PACKAGE_LIMIT=64*1024*1024;
 export async function packageFile(file,metadata) { globalThis.plan.calls.push('package'); if(globalThis.plan.pausePackage) await globalThis.plan.pausePackage; return {blob:file,filename:'frozen.zip',sha256:'same-sha',source_id:'same-source',idempotency_key:'same-key'}; }
 export async function prepareUpload(value,signal) { globalThis.plan.calls.push('prepare'); globalThis.plan.packages.push(value); if(globalThis.plan.prepareError) { const error=globalThis.plan.prepareError; globalThis.plan.prepareError=null; throw error; } return globalThis.plan.prepared; }
 export async function putOriginal() { globalThis.plan.calls.push('put'); }
 export async function completeUpload() { globalThis.plan.calls.push('complete'); return globalThis.plan.verified; }
 export async function uploadStatus() { globalThis.plan.calls.push('status'); return globalThis.plan.verified; }`)
const source = await readFile(new URL('../src/components/KnowledgeUpload.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
function compile(inlineTemplate) {
 const code = ts.transpileModule(compileScript(descriptor,{id:'upload-check',inlineTemplate}).content,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText
   .replace(/from (['"])vue\1/g,`from '${import.meta.resolve('vue')}'`)
   .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g,`from '${portalURL}'`)
   .replace(/from (['"])\.\.\/api\/knowledgeUpload\1/g,`from '${uploadURL}'`)
 return import(moduleURL(code))
}
const component = (await compile(false)).default
const renderedComponent = (await compile(true)).default
const session = {authenticated:true,principal:{id:'test-person',display_name:'合成测试'},permissions:[],capabilities:{upload:true,review_write:false},expires_at:'2099-01-01'}
const receipt = {receipt_id:'a'.repeat(32),status:'verified',content_verified:true,published:false,sha256:'same-sha',byte_length:1,filename:'frozen.zip',source_id:'same-source',error_code:null}
function plan() { globalThis.plan={calls:[],packages:[],pages:[],prepared:{receipt:{...receipt,status:'awaiting_upload',content_verified:false},upload:{method:'PUT'}},verified:{receipt:{...receipt},upload:null},job:{job_id:'b'.repeat(32),receipt_id:receipt.receipt_id,status:'completed',counts:{published:0,draft:1,needs_review:0,archived:0,duplicate:0},items:[]}} }
async function harness() {
 const events=[]; let state
 await renderToString(createSSRApp({...component,setup(props,context){state=component.setup(props,context);return state},render(){return null}},{session,onError:error=>events.push(error),onSubmitted:()=>events.push('submitted')}))
 return {state,events}
}
function select(state,name='method.md') { state.chooseFiles([new File(['synthetic work'],name)]); state.metadata.kind='方法';state.metadata.purpose='合成测试用途';state.sharingConfirmed.value=true }
const initial = await renderToString(createSSRApp(renderedComponent,{session}))
assert.match(initial,/选择文件/)
assert.match(initial,/收件核验/)
assert.doesNotMatch(initial, /type="checkbox"[^>]*checked/)
assert.match(await renderToString(createSSRApp(renderedComponent,{session:{...session,capabilities:{upload:false}}})),/当前账号未开放上传/)

plan();let {state}=await harness()
state.chooseFiles([new File(['x'],'a.md'),new File(['x'],'b.md')]);assert.match(state.error.value,/一次请选择一份/);assert.equal(state.file.value,null)
state.chooseFiles([new File(['x'],'script.exe')]);assert.match(state.error.value,/请选择/);assert.equal(state.file.value,null)
state.chooseFiles([new File([new Uint8Array(8*1024*1024+1)],'large.pdf')]);assert.match(state.error.value,/超过/)
select(state);state.sharingConfirmed.value=false;await state.submit();assert.deepEqual(globalThis.plan.calls,[])
state.sharingConfirmed.value=true;assert.equal(state.missingFields.value.length,5);await state.submit()
assert.deepEqual(globalThis.plan.calls,['package','prepare','put','complete','jobs','job'])
assert.equal(state.stage.value,'done');assert.equal(state.receipt.value.content_verified,true);assert.equal(state.job.value.counts.published,0);assert.equal(state.job.value.counts.draft,1)

plan();({state}=await harness());select(state);globalThis.plan.prepareError=new Error('模拟网络断开');await state.submit();assert.equal(state.stage.value,'error');await state.submit()
assert.equal(globalThis.plan.calls.filter(x=>x==='package').length,1);assert.equal(globalThis.plan.packages.length,2);assert.equal(globalThis.plan.packages[0],globalThis.plan.packages[1]);assert.equal(state.stage.value,'done')

plan();({state}=await harness());select(state);globalThis.plan.prepared={receipt:{...receipt,status:'failed',content_verified:false},upload:null};await state.submit();assert(globalThis.plan.calls.includes('complete'));assert(!globalThis.plan.calls.includes('put'));assert.equal(state.stage.value,'done')
plan();({state}=await harness());select(state);globalThis.plan.jobPage=2;await state.submit();assert.deepEqual(globalThis.plan.pages,[1,2]);assert.equal(state.stage.value,'done')
plan();({state}=await harness());select(state);globalThis.plan.jobPage=12;await state.submit();assert.equal(globalThis.plan.pages.length,10);assert.equal(state.stage.value,'pending');assert.match(state.note.value,/无法据此判断是否排队/)
for(const status of [401,403]) { plan();const check=await harness();select(check.state);globalThis.plan.prepareError=new PortalError(status);await check.state.submit();assert.equal(check.state.file.value,null);assert.equal(check.state.frozenPackage.value,null);assert.equal(check.state.metadata.purpose,'');assert.equal(check.state.sharingConfirmed.value,false);assert(check.events.some(event=>event instanceof PortalError)); }

plan();({state}=await harness());select(state);let resume;globalThis.plan.pausePackage=new Promise(resolve=>{resume=resolve});const pending=state.submit();await Promise.resolve();state.cancel();resume();await pending;assert.equal(state.stage.value,'cancelled');assert.deepEqual(globalThis.plan.calls,['package']);assert.equal(state.running.value,false)
const parent=new AbortController();let aborted=false
await assert.rejects(state.timed(parent.signal,signal=>new Promise((_,reject)=>signal.addEventListener('abort',()=>{aborted=true;reject(new DOMException('aborted','AbortError'))})),5),/请求超时/)
assert.equal(aborted,true)
plan();({state}=await harness());select(state);globalThis.plan.prepared={receipt:{...receipt,status:'rejected',content_verified:false,retryable:false,error_code:'object_hash_mismatch'},upload:null};await state.submit();assert.equal(state.job.value,null);assert.equal(state.canSubmit.value,false);assert.match(state.error.value,/文件指纹/);assert.match(state.note.value,/尚未进入内容处理/);const rejectedCalls=[...globalThis.plan.calls];await state.submit();assert.deepEqual(globalThis.plan.calls,rejectedCalls);assert(!globalThis.plan.calls.includes('complete'));assert(!globalThis.plan.calls.includes('jobs'));

plan();({state}=await harness());select(state);globalThis.plan.job.items=[{reason_codes:['source_manifest','automatic_rules_passed']}];globalThis.plan.job.counts.published=1;globalThis.plan.job.counts.draft=0;await state.submit()
assert.deepEqual(state.reasons.value,['来源清单已归档','自动规则检查已通过']);assert.equal(state.stage.value,'done')
state.job.value.items.push({reason_codes:['unknown_synthetic_reason']});assert.equal(state.reasons.value.at(-1),'需要进一步核对（unknown_synthetic_reason）')
const {processingEventLabels,processingReasonLabels}=await import(receiptAPI)
assert.equal(processingEventLabels.indexing,'正在建立索引');assert.equal(processingReasonLabels.source_manifest,'来源清单已归档')
console.log('KnowledgeUpload: sharing gate, invalid/oversize files, receipt vs publication, immutable retry, failed-receipt retry, permission clearing, cancellation and timeout checks passed.')
