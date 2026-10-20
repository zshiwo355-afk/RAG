import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const url = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const apiURL = url(transpile(await readFile(new URL('../src/api/knowledgePortal.ts', import.meta.url), 'utf8')))
const { processingBlockers } = await import(apiURL)
const job = { status: 'needs_review', error_code: null }
for (const [code, stage, action] of [
  ['uploader_role_not_found', '岗位匹配', /选择部门和岗位/],
  ['uploader_role_conflict', '岗位冲突核对', /原资料归属/],
  ['source_relation_review', '来源关系核对', /详情.*勾选.*独立的新资料.*确认来源关系并重新检查/],
  ['possible_content_conflict', '内容冲突核对', /基线版本/],
  ['source_base_revision_conflict', '版本核对', /基线修订/],
  ['unresolved_reference', '资料完整性检查', /文档和附件/],
  ['script_dependency_not_provided', '资料完整性检查', /脚本或工具/],
  ['reuse_information_incomplete', '交付信息检查', /输入、输出/],
  ['sharing_scope_unconfirmed', '共享与敏感信息检查', /共享范围/],
]) {
  const result = processingBlockers({ ...job, blocking_reasons: [code] })
  assert.equal(result[0].stage, stage); assert.match(result[0].action, action)
  assert(!result[0].reason.includes(code), 'show readable reasons, not raw internal codes')
}
assert.match(processingBlockers({ ...job, blocking_reasons: ['source_relation_review'] })[0].action,
  /原资料更新.*勿勾选.*原提交人.*原渠道.*正确版本.*重新提交/, 'an update is not presented as permission to split a new source')
assert.deepEqual(processingBlockers({ ...job, status: 'completed', blocking_reasons: ['unresolved_reference'] }), [], 'resolved jobs cannot retain stale warnings')
assert.deepEqual(processingBlockers({ ...job, status: 'running', error_code: 'uploader_role_not_found' }), [])
assert.deepEqual(processingBlockers({ ...job, status: 'queued', attention_required: true, blocking_reasons: ['unresolved_reference'] }).map(value => value.code), ['unresolved_reference'], 'rechecking one item must keep its sibling blocker visible')
assert.deepEqual(processingBlockers({ ...job, status: 'running', attention_required: false, blocking_reasons: ['uploader_role_not_found'] }), [], 'resolved role warnings stay hidden while rechecking')
const oldServer = processingBlockers({ ...job, items: [
  { status: 'archived', reason_codes: ['source_manifest'] },
  { status: 'published', reason_codes: ['automatic_rules_passed'] },
  { status: 'needs_review', reason_codes: ['unresolved_reference', 'unresolved_reference'] },
] })
assert.equal(oldServer.length, 1); assert.equal(oldServer[0].code, 'unresolved_reference')
const summary = processingBlockers({ ...job, blocking_reasons: ['uploader_role_conflict'], items: [{ status: 'needs_review', reason_codes: ['unresolved_reference'] }] })
assert.deepEqual(summary.map(value => value.code), ['uploader_role_conflict'], 'server aggregate is authoritative, independent of the current detail page')
assert.match(processingBlockers(job)[0].stage, /未取得/)
const source = await readFile(new URL('../src/components/ProcessingBlockers.vue', import.meta.url), 'utf8')
const component = (await import(url(transpile(compileScript(parse(source).descriptor, { id: 'blocker-check', inlineTemplate: true }).content)
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/api\/knowledgePortal\1/g, `from '${apiURL}'`)))).default
const many = { ...job, blocking_reasons: ['uploader_role_not_found', 'unresolved_reference', 'possible_content_conflict'] }
let html = await renderToString(createSSRApp(component, { job: many, compact: true }))
assert.match(html, /岗位匹配/); assert.match(html, /下一步：/); assert.match(html, /查看全部问题/)
assert(!html.includes('卡在：'), 'stage names appear without a conversational prefix')
assert(!html.includes('内容冲突核对'), 'compact list exposes two blockers and a detail route')
html = await renderToString(createSSRApp(component, { job: many }))
assert.match(html, /内容冲突核对/); assert.match(html, /待处理环节与处理建议/)
assert(!html.includes('卡在：') && !html.includes('卡点'), 'detail uses the same direct stage wording')
html = await renderToString(createSSRApp(component, { job: { ...job, blocking_reasons: ['<img src=x onerror=alert(1)>', '__proto__'] } }))
assert(!html.includes('<img') && !html.includes('onerror') && !html.includes('__proto__'))
assert.match(html, /具体阶段未取得/)
console.log('Blocker guidance: role, conflict, reference, script and information gates, legacy fallback, live aggregate priority, compact/detail rendering and safe unknown reasons passed.')
