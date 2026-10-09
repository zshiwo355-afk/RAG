import assert from 'node:assert/strict'
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import ts from 'typescript'
const input = await readFile(new URL('../src/api/knowledgeUpload.ts', import.meta.url), 'utf8')
const source = ts.transpileModule(input.replace("import { PortalError } from './knowledgePortal'", 'class PortalError extends Error {}'), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText
const {packageFile,putOriginal,OSS_ORIGIN,FILE_LIMIT} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
const metadata = {title:'流程："引用",测试',kind:'流程',purpose:'准备资料\n完成交接',audience:'同事',inputs:'无额外输入',outputs:'交接记录',dependencies:'无额外依赖',boundaries:'只适用此流程'}
const file = new File(['# 原文\n完整保留 UTF-8 内容\n'], '说明.md')
const first = await packageFile(file,metadata)
const again = await packageFile(file,metadata)
assert.equal(first.sha256,again.sha256); assert.equal(first.idempotency_key,again.idempotency_key)
const revised = await packageFile(file,{...metadata,title:'新标题'})
assert.equal(first.source_id,revised.source_id); assert.notEqual(first.sha256,revised.sha256)
const dir=await mkdtemp(join(tmpdir(),'knowledge-upload-check-'))
try {
 const path=join(dir,'asset.zip'); await writeFile(path,Buffer.from(await first.blob.arrayBuffer()))
 const check=spawnSync('/Users/xx/.hermes/hermes-agent/venv/bin/python',['-c',`import csv,io,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
 assert z.testzip() is None
 assert z.namelist()==['交付清单.csv','知识正文/原始文件.md']
 assert z.read('知识正文/原始文件.md')=='# 原文\\n完整保留 UTF-8 内容\\n'.encode()
 row=list(csv.DictReader(io.StringIO(z.read('交付清单.csv').decode('utf-8-sig'))))[0]
 assert row['名称']=='流程："引用",测试'
 assert row['用途']=='准备资料\\n完成交接'
 assert row['共享范围']=='全员'
 assert row['基线修订']==''
 assert row['正文路径']=='知识正文/原始文件.md'
`,path],{encoding:'utf8'})
 assert.equal(check.status,0,check.stderr)
} finally {await rm(dir,{recursive:true,force:true})}
const zip=new File([new Uint8Array([80,75,1,2])],'原包.zip'); const unchanged=await packageFile(zip,metadata)
assert.equal(unchanged.blob,zip)
for (const bad of [new File(['x'],'old.doc'),new File(['x'],'../unsafe.md'),new File([],'empty.txt'),new File(['x'],'a'.repeat(201)+'.zip'),new File([new Uint8Array(FILE_LIMIT+1)],'large.pdf')]) await assert.rejects(packageFile(bad,metadata))
await assert.rejects(packageFile(file,{...metadata,title:''}))
const receipt={receipt_id:'a'.repeat(32),sha256:first.sha256,byte_length:first.blob.size}
const result={receipt,upload:{method:'PUT',url:OSS_ORIGIN+'/knowledge-receipts/raw/test/blob?Signature=synthetic',headers:{'Content-Length':String(first.blob.size),'Content-Type':'application/octet-stream','x-oss-object-acl':'private','x-oss-forbid-overwrite':'true','x-oss-meta-sha256':first.sha256,'x-oss-meta-receipt-id':receipt.receipt_id}}}
let calls=0; const originalFetch=globalThis.fetch
try {
 globalThis.fetch=async (url,opts)=>{calls++; assert.equal(opts.credentials,'omit');assert.equal(opts.redirect,'error');assert.equal(opts.referrerPolicy,'no-referrer');assert.equal(opts.headers['content-length'],undefined);assert.equal(opts.body,first.blob);return {ok:true}}
 await putOriginal(first,result,new AbortController().signal)
 for(const url of ['https://example.com/upload',OSS_ORIGIN+'/other/path']) await assert.rejects(putOriginal(first,{...result,upload:{...result.upload,url}},new AbortController().signal))
 await assert.rejects(putOriginal(first,{...result,upload:{...result.upload,headers:{...result.upload.headers,'x-oss-object-acl':'public-read'}}},new AbortController().signal))
 assert.equal(calls,1)
} finally {globalThis.fetch=originalFetch}
console.log('Upload package checks passed: deterministic ZIP, original bytes, Python CRC/CSV, limits, private same-bucket PUT.')
