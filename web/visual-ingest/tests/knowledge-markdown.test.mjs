import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'

const moduleURL = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const rendererURL = moduleURL(transpile(await readFile(new URL('../src/knowledge-markdown.ts', import.meta.url), 'utf8'))
  .replace(/from (['"])markdown-it\1/g, `from '${import.meta.resolve('markdown-it')}'`))
const { renderKnowledgeMarkdown } = await import(rendererURL)
const source = await readFile(new URL('../src/components/KnowledgeMarkdown.vue', import.meta.url), 'utf8')
const componentURL = moduleURL(transpile(compileScript(parse(source).descriptor, { id: 'markdown-check', inlineTemplate: true }).content)
  .replace(/from (['"])vue\1/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from (['"])\.\.\/knowledge-markdown\1/g, `from '${rendererURL}'`))
const component = (await import(componentURL)).default
const rendered = await renderToString(createSSRApp(component, { content: `# 库存专员岗位标准作业程序（SOP）

## 1. 文档基本信息

| 字段 | 内容 |
| :--- | ---: |
| 所属部门 | 物流仓储部 |
| 岗位名称 | **库存专员** |

### 验收

1. 核对数量
2. 记录差异

- 及时反馈
- 保留凭证

> 保留原文的待确认事项。

第一行
第二行

\`\`\`text
## 保留代码中的符号 **样例**
\`\`\`
` }))
assert.match(rendered, /<h1>库存专员岗位标准作业程序（SOP）<\/h1>/)
assert.match(rendered, /<h2>1\. 文档基本信息<\/h2>/)
assert.match(rendered, /<strong>库存专员<\/strong>/)
assert.match(rendered, /<table>[\s\S]*<th class="markdown-align-right">内容<\/th>/)
assert.match(rendered, /<td class="markdown-align-left">所属部门<\/td>/)
assert.match(rendered, /<ol>[\s\S]*<li>核对数量<\/li>/)
assert.match(rendered, /<ul>[\s\S]*<li>及时反馈<\/li>/)
assert.match(rendered, /<blockquote>/)
assert.match(rendered, /第一行<br>\n第二行/)
assert.match(rendered, /<pre><code class="language-text">## 保留代码中的符号 \*\*样例\*\*/)
assert.doesNotMatch(rendered, /style=|\*\*库存专员\*\*|## 1\./)

const compactLabels = renderKnowledgeMarkdown('**岗位名称：**招聘主管 / HRBP\n\n**业务目的：**让招聘更有效，**覆盖率：**100%')
assert.match(compactLabels, /<strong>岗位名称：<\/strong>招聘主管 \/ HRBP/)
assert.match(compactLabels, /<strong>业务目的：<\/strong>让招聘更有效，<strong>覆盖率：<\/strong>100%/)
assert.doesNotMatch(compactLabels, /\*\*/)
assert.match(renderKnowledgeMarkdown('**岗位名称：** 招聘'), /<strong>岗位名称：<\/strong> 招聘/)
assert.match(renderKnowledgeMarkdown('`**岗位名称：**招聘`'), /<code>\*\*岗位名称：\*\*招聘<\/code>/)
assert.match(renderKnowledgeMarkdown('```text\n**岗位名称：**招聘\n```'), /<pre><code class="language-text">\*\*岗位名称：\*\*招聘/)
assert.match(renderKnowledgeMarkdown('\\*\\*岗位名称：\\*\\*招聘'), /<p>\*\*岗位名称：\*\*招聘<\/p>/)
assert.match(renderKnowledgeMarkdown('**未闭合：招聘'), /<p>\*\*未闭合：招聘<\/p>/)
assert.match(renderKnowledgeMarkdown('***岗位名称：***招聘'), /<p>\*\*\*岗位名称：\*\*\*招聘<\/p>/)
const hostileLabel = renderKnowledgeMarkdown('**<img src=x onerror=alert(1)>：**招聘\n\n**[链接](javascript:alert%281%29)：**招聘')
assert.match(hostileLabel, /<strong>&lt;img src=x onerror=alert\(1\)&gt;：<\/strong>/)
assert.doesNotMatch(hostileLabel, /<(?:img|script)\b|href="javascript:/i)

const hostile = renderKnowledgeMarkdown(`<script>alert(1)</script>

<img src="https://tracker.invalid/pixel" onerror="alert(1)">

[恶意](javascript:alert%281%29)
[编码](jav&#x61;script:alert%281%29)
[大写](JAVASCRIPT:alert%281%29)
[数据](data:text/html;base64,PHNjcmlwdD4=)
[文件](file:///etc/passwd)
[相对地址](/portal/knowledge/api/session)

![图片说明](https://tracker.invalid/pixel)
![<img src=x onerror=alert(1)>](https://tracker.invalid/alt)
![内嵌](data:image/png;base64,abc)

[链接](https://example.com/docs?q=%22)
`)
assert.doesNotMatch(hostile, /<(?:script|img|iframe|object|svg|style)\b/i)
assert.doesNotMatch(hostile, /(?:src|href)="(?:javascript|data|file|\/portal)/i)
assert.match(hostile, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/)
assert.match(hostile, /\[图片：图片说明\]/)
assert.match(hostile, /href="https:\/\/example.com\/docs\?q=%22" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"/)
assert.equal((hostile.match(/<a /g) || []).length, 1)
assert.equal(renderKnowledgeMarkdown(''), '')
const changed = await renderToString(createSSRApp(component, { content: '## 新版本\n\n**新正文**' }))
assert.match(changed, /<h2>新版本<\/h2>/)
assert.doesNotMatch(changed, /库存专员/)
const portal = await readFile(new URL('../src/views/KnowledgePortal.vue', import.meta.url), 'utf8')
assert.match(portal, /<KnowledgeMarkdown :content="preview.content"/)
const dashboard = await readFile(new URL('../src/components/KnowledgeDashboard.vue', import.meta.url), 'utf8')
assert.match(dashboard, /<KnowledgeMarkdown class="semantic-snippet"[^>]*:content="asset.snippet"/)
const snippet = await renderToString(createSSRApp(component, { class: 'semantic-snippet', tabindex: '0', 'aria-label': '检索摘要', content: '#### 月度绩效\n\n| 指标 | 要求 |\n| --- | --- |\n| **覆盖率：** | 100% |' }))
assert.match(snippet, /class="knowledge-markdown semantic-snippet"/)
assert.match(snippet, /aria-label="检索摘要"[^>]*tabindex="0"/)
assert.match(snippet, /<h4>月度绩效<\/h4>/)
assert.match(snippet, /<td>100%<\/td>/)
assert.doesNotMatch(snippet, /####|\*\*覆盖率/)
console.log('Knowledge Markdown: actual component headings, tables, lists, emphasis, line breaks, code preservation, escaped HTML, blocked unsafe links and no image loads passed.')
