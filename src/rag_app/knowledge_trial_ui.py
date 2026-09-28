"""Small, read-only UI bound to an explicitly supplied isolated knowledge service."""

import base64
import hashlib
import re

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .knowledge_api import get_knowledge_service, router


class _Asset(BaseModel):
    knowledge_id: str
    revision: int = Field(ge=1)
    title: str
    kind: str
    chars: int = Field(ge=0)


class _Catalog(BaseModel):
    trial_id: str
    asset_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    assets: list[_Asset]
    sample_queries: list[str]


_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>公司知识 · 隔离试用</title>
<style>
:root{color-scheme:light;--paper:#f4f2eb;--ink:#233a32;--muted:#68756e;--line:#d6ddd4;--green:#275b47}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.7 "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
main{max-width:1040px;margin:auto;padding:40px 28px 64px}header{border-top:4px solid var(--green);padding-top:18px}
.eyebrow{font:12px/1.5 Menlo,monospace;letter-spacing:.1em;color:var(--green)}h1{font:500 clamp(28px,5vw,40px)/1.3 "Songti SC","Noto Serif CJK SC",serif;margin:18px 0 12px}
.notice{margin:0;color:var(--green)}.muted,.meta{color:var(--muted);font-size:13px}.meta{overflow-wrap:anywhere}
.overview{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:18px 0 26px;padding-bottom:16px;border-bottom:1px solid var(--line)}
label{display:block;font-weight:600;margin-bottom:9px}.search-row{display:flex;gap:10px}input,select,button{font:inherit;border-radius:6px;min-height:46px}
input,select{border:1px solid #b9c5ba;background:#fff;color:var(--ink);padding:10px 12px;min-width:0}input{flex:1}
button{border:1px solid var(--green);background:var(--green);color:#fff;padding:9px 25px;cursor:pointer;white-space:nowrap}button:hover{background:#1c4635}
button:disabled{cursor:wait;opacity:.6}select{width:100%;margin-top:12px;font-size:14px}select:disabled{opacity:.6}
:focus-visible{outline:3px solid #bc9344;outline-offset:3px}.status{min-height:28px;margin:18px 0 8px;color:var(--muted)}
.result{padding:24px 0;border-top:1px solid var(--line)}.result h2{font-size:20px;line-height:1.5;margin:0 0 8px;font-weight:600;overflow-wrap:anywhere}
.snippet{margin:14px 0;padding:14px 18px;border-left:3px solid #759780;background:#eaf0e8;white-space:pre-wrap;overflow-wrap:anywhere}
.source{font-size:13px;overflow-wrap:anywhere}.result details{margin-top:16px}summary{cursor:pointer;color:var(--green);font-weight:600;min-height:32px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font:14px/1.85 "PingFang SC","Hiragino Sans GB",sans-serif;background:#fff;border:1px solid var(--line);padding:20px;border-radius:6px}
.catalog{margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}.catalog ul{padding-left:22px}.catalog li{margin:9px 0;overflow-wrap:anywhere}
footer{margin-top:30px;color:var(--muted);font-size:12px}@media(max-width:560px){main{padding:24px 18px 40px}.search-row{flex-direction:column}button{width:100%}.result{padding:20px 0}pre{padding:14px}}
</style>
</head>
<body>
<main>
<header><div class="eyebrow">KNOWLEDGE / LOCAL TRIAL</div><h1>公司知识检索</h1>
<p class="notice">隔离试用，正式未发布；只查本批新入库资料</p></header>
<div class="overview"><span id="counts">正在读取本批资料…</span><span id="trial" class="meta"></span></div>
<form id="search-form">
<label for="query">你想查找什么？</label>
<div class="search-row"><input id="query" name="query" type="search" maxlength="4000" required placeholder="用一个实际工作问题开始检索" autocomplete="off"><button id="search-button" type="submit">检索资料</button></div>
<label class="muted" for="examples">示例问题（选择后可编辑）</label><select id="examples"><option value="">选择一个示例问题</option></select>
</form>
<p id="status" class="status" role="status" aria-live="polite">输入问题后，将显示命中片段和对应完整正文。</p>
<section id="results" aria-label="检索结果" aria-busy="false"></section>
<details class="catalog"><summary id="catalog-summary">查看本批资料</summary><ul id="asset-list"></ul></details>
<footer>检索结果直接来自资料原文，不生成 AI 回答。脚本文件未入库，Skill 正文不能视为可直接运行的完整技能包。试用可见状态不代表内容已通过业务审核。</footer>
</main>
<script>
'use strict';
const byId = (id) => document.getElementById(id);
function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = String(text);
  if (className) item.className = className;
  return item;
}
async function jsonRequest(url, options) {
  const response = await fetch(url, {cache: 'no-store', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '服务暂不可用，请稍后重试。');
  return data;
}
function showResult(result, position) {
  const article = element('article', undefined, 'result');
  article.append(element('h2', `${position + 1}. ${result.title}`));
  article.append(element('div', `${result.kind} · ID ${result.knowledge_id} · 版本 ${result.revision}`, 'meta'));
  article.append(element('blockquote', result.snippet || '暂无命中片段。', 'snippet'));
  const sources = Array.isArray(result.sources) ? result.sources : [];
  for (const source of sources) {
    article.append(element('p', `来源：${[source.name, source.locator, source.url].filter(Boolean).join(' · ')}`, 'source'));
  }
  if (!sources.length) article.append(element('p', '来源：未登记', 'source'));
  const details = element('details');
  const content = typeof result.content === 'string' ? result.content : '完整正文未返回，请重试检索。';
  details.append(element('summary', `展开完整正文 · 版本 ${result.revision} · ${Array.from(content).length} 字符`));
  details.append(element('pre', content));
  article.append(details);
  byId('results').append(article);
}
byId('examples').addEventListener('change', (event) => {
  if (event.target.value) { byId('query').value = event.target.value; byId('query').focus(); }
});
byId('search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = byId('query').value.trim();
  if (!query) { byId('status').textContent = '请先输入要查找的问题。'; return; }
  const started = performance.now();
  byId('search-button').disabled = true;
  byId('results').setAttribute('aria-busy', 'true');
  byId('results').replaceChildren();
  byId('status').textContent = '正在检索本批资料…';
  try {
    const data = await jsonRequest('/api/knowledge/search', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, top_k: 5, include_content: true})
    });
    data.results.forEach(showResult);
    const elapsed = ((performance.now() - started) / 1000).toFixed(2);
    byId('status').textContent = data.results.length
      ? `找到 ${data.results.length} 份资料 · 耗时 ${elapsed} 秒`
      : `本批暂无匹配资料 · 耗时 ${elapsed} 秒；可以换一个关键词试试。`;
  } catch (error) {
    byId('status').textContent = `检索失败：${error.message}（耗时 ${((performance.now() - started) / 1000).toFixed(2)} 秒）`;
  } finally {
    byId('search-button').disabled = false;
    byId('results').setAttribute('aria-busy', 'false');
  }
});
jsonRequest('/api/trial/catalog').then((catalog) => {
  byId('counts').textContent = `${catalog.asset_count} 份资料 · ${catalog.chunk_count} 个检索片段`;
  byId('trial').textContent = `试用批次 ${catalog.trial_id}`;
  byId('catalog-summary').textContent = `查看本批 ${catalog.asset_count} 份资料`;
  for (const query of catalog.sample_queries) {
    const option = element('option', query); option.value = query; byId('examples').append(option);
  }
  for (const asset of catalog.assets) {
    byId('asset-list').append(element('li', `${asset.title} · ${asset.kind} · 版本 ${asset.revision} · ${asset.chars} 字符`));
  }
}).catch(() => { byId('counts').textContent = '资料清单暂不可用，请刷新页面重试。'; });
</script>
</body></html>'''


def create_app(service, catalog: dict) -> FastAPI:
    """No default service, credentials, write endpoints or product routes."""
    public_catalog = _Catalog.model_validate(catalog).model_dump()
    app = FastAPI(title="公司知识隔离试用", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    hashes = {}
    for tag in ("script", "style"):
        source = re.search(f"<{tag}>(.*?)</{tag}>", _PAGE, re.S).group(1)
        hashes[tag] = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
    policy = (f"default-src 'none'; script-src 'sha256-{hashes['script']}'; "
              f"style-src 'sha256-{hashes['style']}'; connect-src 'self'; "
              "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

    @app.get("/", response_class=HTMLResponse)
    def home():
        return HTMLResponse(_PAGE, headers={"Content-Security-Policy": policy, "Cache-Control": "no-store"})

    @app.get("/api/trial/catalog")
    def trial_catalog():
        return JSONResponse(public_catalog, headers={"Cache-Control": "no-store"})

    return app
