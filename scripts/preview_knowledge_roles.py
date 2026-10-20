"""Run the real local portal and intake pipeline with RAM-only cloud substitutes.

No .env is loaded. Non-loopback socket connections are blocked before imports.
The supplied SOP is read-only; databases and collector files live in a fresh
temporary directory. OSS and retrieval doubles disappear when the process exits.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import zipfile

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
for _name in tuple(os.environ):
    if _name.startswith(('KNOWLEDGE_', 'OSS_', 'OPENSEARCH_', 'DASHSCOPE_')):
        del os.environ[_name]
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
def _loopback_only(original):
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise RuntimeError('local preview forbids external network access')
        return original(sock, address)
    return connect
socket.socket.connect = _loopback_only(_connect)
socket.socket.connect_ex = _loopback_only(_connect_ex)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--sop', type=Path, required=True)
parser.add_argument('--mcp-root', type=Path, required=True)
parser.add_argument('--collector-root', type=Path, required=True)
parser.add_argument('--evidence', type=Path, required=True)
parser.add_argument('--port', type=int, default=19014)
args = parser.parse_args()
if args.evidence.resolve() == args.sop.resolve() or args.evidence.resolve().suffix.lower() != '.json':
    parser.error('--evidence must be a separate JSON output, never the source SOP')
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(args.mcp_root)]

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
import uvicorn
from mcp import Client
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from rag_app.knowledge_objects import KnowledgeObjects
from rag_app.knowledge_files import KnowledgeFiles
from rag_app.knowledge_store import KnowledgeStore
from rag_app.knowledge_service import KnowledgeService
from rag_app.knowledge_index import chunk_document
from rag_app.knowledge_receipts import ReceiptObjects, ReceiptService, ReceiptStore
from rag_app.knowledge_processing import ARTIFACT_PREFIX, ProcessingStore, ProcessingService
from rag_app import knowledge_processing_api as api, knowledge_api, knowledge_receipts_api as intake_api
from rag_app import rerank_service, knowledge_relevance
from reading_checkins_mcp.authorization import ToolAuthorizationMiddleware
from reading_checkins_mcp.catalog import Registry
from reading_checkins_mcp.connectors.company_knowledge import CompanyKnowledgeDependencies, register_tools
from reading_checkins_mcp.connectors.company_knowledge.manifest import MANIFEST
from reading_checkins_mcp.connectors.company_knowledge.service import CompanyKnowledgeService, KnowledgeSettings
from reading_checkins_mcp.bootstrap import bootstrap_permission_catalog
from reading_checkins_mcp.database import Database
from reading_checkins_mcp.identity import IdentityService
from reading_checkins_mcp.portal import mount_knowledge_portal, READ_KNOWLEDGE, READ_SELF, REVIEW, DASHBOARD

ORIGIN = f'http://127.0.0.1:{args.port}'
FAKE_OSS = 'https://synthetic-private.oss-cn-hangzhou.aliyuncs.com'
TOKEN = 'local-only-isolated-receipts-' + secrets.token_hex(24)
os.environ.update(KNOWLEDGE_RECEIPTS_ENABLED='1', KNOWLEDGE_PROCESSING_ENABLED='1', KNOWLEDGE_RECEIPTS_TOKEN=TOKEN)
temporary = tempfile.TemporaryDirectory(prefix='knowledge-role-preview-')
WORK = Path(temporary.name).resolve()

class MemoryBucket:
    def __init__(self):
        self.data, self.headers, self.grants = {}, {}, {}
    def get_bucket_acl(self): return SimpleNamespace(acl='private')
    def get_bucket_versioning(self): return SimpleNamespace(status=None)
    def object_exists(self, key): return key in self.data
    def get_object_acl(self, key):
        assert key in self.data
        return SimpleNamespace(acl='private')
    def put_object(self, key, data, headers):
        assert headers['x-oss-object-acl'] == 'private' and headers['x-oss-forbid-overwrite'] == 'true'
        assert key not in self.data
        self.data[key], self.headers[key] = bytes(data), dict(headers)
    def head_object(self, key):
        return SimpleNamespace(content_length=len(self.data[key]), etag=hashlib.md5(self.data[key]).hexdigest().upper(), headers=self.headers[key])
    def get_object(self, key, headers=None):
        if headers is not None:
            assert headers == {'If-Match': '"' + self.head_object(key).etag + '"'}
        return io.BytesIO(self.data[key])
    def sign_url(self, method, key, expires, headers=None, additional_headers=None, params=None):
        if method == 'PUT':
            return FAKE_OSS + '/' + key + '?x-oss-signature-version=OSS4-HMAC-SHA256&x-oss-additional-headers=content-length&x-oss-signature=synthetic'
        grant = secrets.token_urlsafe(32)
        self.grants[grant] = (key, time.time() + expires, params or {})
        return FAKE_OSS + '/local-objects/' + grant

class LocalFiles(KnowledgeFiles):
    def signed_url(self, *positional, **kwargs):
        result = super().signed_url(*positional, **kwargs)
        assert result['url'].startswith(FAKE_OSS + '/local-objects/')
        return {**result, 'url': result['url'].replace(FAKE_OSS, ORIGIN, 1)}

def grams(text):
    clean = re.sub(r'\s+', '', text.casefold())
    return {clean[i:i + 2] for i in range(len(clean) - 1)}

class LocalIndex:
    """Deterministic local contract double, not a cloud embedding quality test."""
    def __init__(self): self.rows = []
    def index_and_verify(self, record, *, require_keyword=False):
        chunks = chunk_document(record)
        self.rows.extend(chunks)
        return {'verified_chunks': len(chunks), 'retrieval_verified': True, 'keyword_retrieval_verified': require_keyword}
    def search(self, query, allowed_versions, top_k):
        allowed = {(row['knowledge_id'], row['revision']) for row in allowed_versions}
        scored = [{**row, 'score': len(grams(query) & grams(row['title'] + row['source_text'])),
                   'route_ranks': {'text_dense': 1, 'text_keyword': 1}}
                  for row in self.rows if (row['knowledge_id'], row['revision']) in allowed]
        return sorted((row for row in scored if row['score']), key=lambda row: -row['score'])[:top_k]

# Inject only the offline model calls used by this isolated preview process.
rerank_service.call_rerank_model = lambda query, texts: [min(1., len(grams(query) & grams(text)) / max(1, len(grams(query)))) for text in texts]
knowledge_relevance.supported_indices = lambda query, texts: {i for i, text in enumerate(texts) if len(grams(query) & grams(text)) >= 2}
bucket, index = MemoryBucket(), LocalIndex()
receipts = ReceiptStore(WORK / 'knowledge.sqlite3'); receipts.initialize()
processing_store = ProcessingStore(receipts.path); processing_store.initialize()
bodies = KnowledgeObjects(bucket)
catalogue = KnowledgeStore(receipts.path, objects=bodies); catalogue.initialize()
files = LocalFiles(bucket)
receipt_service = ReceiptService(receipts, ReceiptObjects(bucket))
processing = ProcessingService(processing_store, ReceiptObjects(bucket),
    artifacts=KnowledgeObjects(bucket, prefix=ARTIFACT_PREFIX), draft_objects=bodies,
    files=files, auto_publish=True, knowledge_index=index)
knowledge = KnowledgeService(catalogue, index)
rag = FastAPI()
for router in (api.router, knowledge_api.router, intake_api.router): rag.include_router(router)
rag.dependency_overrides.update({api.get_catalog: lambda: catalogue, api.get_processing_store: lambda: processing_store,
    api.get_processing_service: lambda: processing, api.get_knowledge_files: lambda: files,
    knowledge_api.get_knowledge_service: lambda: knowledge, intake_api.get_receipt_service: lambda: receipt_service})
# The production route normally constructs a cloud service; pin its index here.
api.KnowledgeService = lambda store: KnowledgeService(store, index)
evidence = {'isolation': {'database': 'temporary SQLite', 'object_storage': 'in-memory OSS substitute',
    'retrieval': 'local deterministic index', 'external_network': 'blocked', 'production_writes': False}, 'checks': {}}

class Audit:
    async def record(self, event): pass
    async def complete(self, event): pass

async def seed(upstream):
    service = CompanyKnowledgeService(KnowledgeSettings(base_url='http://127.0.0.1:3000',
        intake_enabled=True, intake_base_url='http://127.0.0.1:3000', intake_bearer_token=TOKEN), client=upstream)
    server = MCPServer('isolated-role-acceptance', middleware=[ToolAuthorizationMiddleware(Registry((MANIFEST,)), Audit(), write_tools_enabled=True)])
    register_tools(server, CompanyKnowledgeDependencies(service))
    async def call(name, parameters, owner='local-collector'):
        scopes = {'company_knowledge.submit': 'self', READ_SELF: 'self', READ_KNOWLEDGE: 'company'}
        user = AuthenticatedUser(AccessToken(token='local-only-synthetic-pat', client_id='local-acceptance', subject=owner,
            scopes=list(scopes), claims={'data_scopes': {key: [{'scope_type': value}] for key, value in scopes.items()}}))
        marker = auth_context_var.set(user)
        try:
            async with Client(server) as client: result = await client.call_tool(name, parameters)
            assert not result.is_error, str(result)
            return result.structured_content
        finally: auth_context_var.reset(marker)
    async def submit(name, data, source):
        payload = {'filename': name, 'byte_length': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                   'idempotency_key': hashlib.sha256(data).hexdigest(), 'source_id': source}
        prepared = await call('prepare_company_knowledge_upload', payload)
        receipt_id = prepared['receipt']['receipt_id']
        row = receipts.get('local-collector', receipt_id)
        if row['status'] == 'awaiting_upload':
            bucket.put_object(row['object_key'], data, headers=prepared['upload']['headers'])
            await call('complete_company_knowledge_upload', {'receipt_id': receipt_id})
            assert receipt_service.verify_once()
            assert processing.run_once()
        job = next(row for row in processing_store.list_jobs('local-collector')['items'] if row['receipt_id'] == receipt_id)
        result = processing_store.get_job(job['job_id'], 'local-collector')
        assert result['status'] == 'completed', json.dumps(result, ensure_ascii=False)
        return result
    # A pre-existing, unassigned asset is retained byte-for-byte through ingestion.
    sentinel = {'knowledge_id': 'existing-unassigned', 'title': '既有资料保留验证', 'content': '# 既有资料\n只用于本地保护验证。',
        'kind': 'reference', 'sources': [{'name': 'synthetic-existing.md'}], 'evidence': {}}
    saved = knowledge.import_document(sentinel, execute=True)
    knowledge.publish(sentinel['knowledge_id'], confirmed_by='local-fixture', execute=True)
    before = catalogue.get_published(sentinel['knowledge_id'])
    source = args.sop.read_bytes()
    first = await submit(args.sop.name, source, 'local-sop-ai-architect')
    roles = catalogue.roles()
    role = next(row for row in roles if row['department'] == '总经办' and row['name'] == 'AI架构师')
    sop = next(row for row in catalogue.published_catalog() if row.get('role_id') == role['role_id'])
    record = catalogue.get_published(sop['knowledge_id'])
    original = next(row for row in record['files'] if row['purpose'] == 'original')
    assert files.get(original) == source
    preview = next(row for row in record['files'] if row['purpose'] == 'preview')
    assert 'AI架构师'.encode() in files.get(preview)
    spec = importlib.util.spec_from_file_location('collector_package', args.collector_root / 'scripts/knowledge_package.py')
    package_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(package_module)
    work, state_dir = WORK / 'collector-work', WORK / 'collector-state'
    work.mkdir(); state_dir.mkdir()
    profile = {'uploader_position': 'AI架构师', 'authorized_roots': [str(work)], 'source_id': 'local-agent-collector',
               'state_dir': str(state_dir), 'role_id': role['role_id'], 'department': role['department'], 'role_name': role['name']}
    state = {'baseline': {}, 'packages': {}, 'trial_package': None}
    for kind, filename, title, content in [
        ('案例', 'case.md', '本地联测案例：岗位交接核对', '# 本地联测案例\n\n本地测试资料，不是已发生的业务案例。\n\n输入：岗位资料。\n操作：先检查原件，再比对版本和验收记录。\n输出：交接检查结果。'),
        ('Skill方法', 'skill.md', '本地联测 Skill：交接检查', '# 交接检查 Skill 使用说明\n\n本地演示包，未验证业务效果。\n\n输入：岗位资料。\n按包内说明检查资料是否齐全，输出核对结果。')]:
        body = work / filename; body.write_text(content)
        package_module.scan(profile, state)
        metadata = dict.fromkeys(package_module.FIELDS, '')
        metadata.update({'名称': title, '类型': kind, '状态': '交付', '用途': '本地岗位联测', '来源定位': '本地合成测试资料',
            '证据状态': '合成数据，仅验证链路', '限制与待办': '尚未业务验收', '共享范围': '全员', '使用对象': '本地测试人员',
            '输入': '岗位资料', '输出': '核对结果', '依赖与权限': '岗位资料读取权限', '适用边界': '本地测试，非公司制度'})
        review = {'logical_id': filename, 'body': {'root': 0, 'path': filename, 'sha256': hashlib.sha256(body.read_bytes()).hexdigest()}, 'metadata': metadata}
        if kind == 'Skill方法':
            install = work / 'skill.zip'
            with zipfile.ZipFile(install, 'w') as archive:
                archive.writestr('SKILL.md', '---\nname: local-handoff-check\ndescription: 本地联测岗位交接检查\n---\n# 交接检查\n读取 [清单](references/check.md)，核对输入并记录结果。\n')
                archive.writestr('references/check.md', '# 检查清单\n核对岗位、版本、原件与验收记录。\n')
            review['skill_package'] = {'root': 0, 'path': install.name, 'sha256': hashlib.sha256(install.read_bytes()).hexdigest()}
        packed = package_module.package(profile, state, review, False)
        await submit(Path(packed['package_path']).name, Path(packed['package_path']).read_bytes(), profile['source_id'])
    await submit(args.sop.name, source, 'local-sop-ai-architect')
    assert catalogue.get_published(sentinel['knowledge_id']) == before
    assert args.sop.read_bytes() == source
    role = next(row for row in catalogue.roles() if row['role_id'] == role['role_id'])
    assert role['counts'] == {'sop': 1, 'skill': 1, 'other': 1}, role
    readback = await call('query_company_knowledge', {'knowledge_id': sop['knowledge_id']})
    assert readback['results'][0]['role_id'] == role['role_id']
    found = await call('query_company_knowledge', {'query': record['title'], 'role_id': role['role_id'], 'category': 'sop'})
    assert found['result_count'] == 1
    assert not any('object_ref' in json.dumps(row) for row in found['results'])
    evidence.update(role=role, sop={'knowledge_id': sop['knowledge_id'], 'sha256': hashlib.sha256(source).hexdigest(),
        'original_bytes': len(source), 'chunks': len(chunk_document(record))}, assets=catalogue.published_catalog())
    evidence['checks'].update(native_mcp_intake=True, collector_role_mapping=True, complete_skill_package=True,
        original_hash_match=True, preview_contains_chinese=True, idempotent_reupload=True,
        existing_asset_unchanged=True, source_file_unchanged=True, mcp_role_readback=True, role_filtered_search=True)

@asynccontextmanager
async def lifespan(app):
    database = Database(f'sqlite+aiosqlite:///{WORK / "portal.sqlite3"}')
    await database.create_schema(); await bootstrap_permission_catalog(database.session_factory)
    identity = IdentityService(database.session_factory, 'local-only-digest-key-' + secrets.token_hex(32))
    principal = await identity.create_principal('本地联测管理员')
    for permission in (READ_KNOWLEDGE, READ_SELF, REVIEW, DASHBOARD):
        await identity.set_permission_override(principal.id, permission, 'allow')
        await identity.create_data_scope(permission, 'company' if permission != READ_SELF else 'self', principal_id=principal.id)
    token = await identity.issue_token(principal.id, 'local-only-preview')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rag), trust_env=False) as upstream:
        class Resolver:
            async def resolve(self, name):
                assert name == 'company_knowledge'
                return SimpleNamespace(revision=1, config={'intake_enabled': True, 'intake_base_url': 'http://127.0.0.1:3000'},
                                       secrets={'intake_bearer_token': TOKEN})
        # Routes are mounted before uvicorn begins serving requests.
        portal_app = FastAPI()
        mount_knowledge_portal(portal_app, identity=identity, session_factory=database.session_factory, resolver=Resolver(),
            origin=ORIGIN, username='local', password='local-preview-only', credential_id=token.credential_id,
            static_dir=str(ROOT / 'web/visual-ingest/dist'), client=upstream)
        app.mount('/', portal_app)
        await seed(upstream)
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({'ready': ORIGIN + '/rag/knowledge', 'account': 'local', 'password': 'local-preview-only',
                          'evidence': str(args.evidence)}, ensure_ascii=False), flush=True)
        yield
    await database.dispose()
    temporary.cleanup()

app = FastAPI(lifespan=lifespan)
app.mount('/assets', StaticFiles(directory=ROOT / 'web/visual-ingest/dist/assets'), name='local-built-assets')

@app.get('/local-objects/{grant}')
def local_object(grant: str):
    item = bucket.grants.get(grant)
    if item is None or item[1] < time.time(): raise HTTPException(403)
    key, _, params = item
    return Response(bucket.data[key], headers={'Content-Type': params.get('response-content-type', 'application/octet-stream'),
        'Content-Disposition': params.get('response-content-disposition', 'attachment'), 'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning')
