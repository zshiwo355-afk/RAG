from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from rag_app.knowledge_objects import KnowledgeObjects
from rag_app.knowledge_processing import (
    ARTIFACT_PREFIX, LEASE_SECONDS, MAX_ATTEMPTS, ProcessingError, ProcessingService, ProcessingStore,
    _FencedDraftStore,
)
from rag_app.knowledge_receipts import LeaseLost, ReceiptObjects, ReceiptService, ReceiptStore
from rag_app.knowledge_store import KnowledgeStore
from test_knowledge_receipts import FakeBucket as ReceiptBucket


pytestmark = pytest.mark.offline
BODY = '# 工作方法\n\n先核对输入，保存验证依据，再记录处理结果。\n'


class Bucket(ReceiptBucket):
    def object_exists(self, key):
        return key in self.data

    def put_object(self, key, data, headers):
        assert headers['x-oss-object-acl'] == 'private'
        assert headers['x-oss-forbid-overwrite'] == 'true'
        assert key not in self.data
        self.data[key], self.meta[key], self.acls[key] = data, {}, 'private'

    def get_object(self, key, headers=None):
        if headers is not None:
            return super().get_object(key, headers)
        return io.BytesIO(self.data[key])


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    receipts = ReceiptStore(tmp_path / 'database.db', clock=lambda: now[0])
    receipts.initialize()
    store = ProcessingStore(receipts.path, clock=receipts.clock)
    store.initialize()
    bucket = Bucket()
    objects = ReceiptObjects(bucket)
    drafts = KnowledgeObjects(bucket, prefix='company-knowledge/bodies')
    catalogue = KnowledgeStore(receipts.path, objects=drafts)
    catalogue.initialize()
    service = ProcessingService(store, objects, artifacts=KnowledgeObjects(bucket, prefix=ARTIFACT_PREFIX), draft_objects=drafts)
    return SimpleNamespace(now=now, receipts=receipts, store=store, bucket=bucket, objects=objects,
                           service=service, catalogue=catalogue, drafts=drafts)


def submit(system, data=BODY, filename='method.md', principal='user:a', source_id='hermes:session:1'):
    data = data.encode() if isinstance(data, str) else data
    payload = dict(idempotency_key=hashlib.sha256(data).hexdigest(), source_id=source_id,
                   filename=filename, byte_length=len(data), sha256=hashlib.sha256(data).hexdigest())
    row = system.receipts.prepare(principal, payload)
    system.bucket.upload(row, data)
    receipts = ReceiptService(system.receipts, system.objects)
    receipts.complete(principal, row['receipt_id'])
    assert receipts.verify_once()
    return system.receipts.get(principal, row['receipt_id'])


def job(system, principal='user:a'):
    summaries = system.store.list_jobs(principal)
    assert summaries['total'] == 1
    return system.store.get_job(summaries['items'][0]['job_id'], principal)


def zip_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_verified_markdown_becomes_persistent_unapproved_draft(system):
    receipt = submit(system)
    assert system.service.run_once()
    result = job(system)
    assert result['status'] == 'completed' and result['counts']['draft'] == 1
    draft = next(item for item in result['items'] if item['status'] == 'draft')
    record = system.catalogue.snapshot(draft['knowledge_id'])
    assert record['status'] == 'draft' and record['evidence']['source_verified'] is False
    assert record['contributor'] == '未确认' and system.catalogue.get_published(draft['knowledge_id']) is None
    assert system.service.get_item(result['job_id'], draft['item_id'], 'user:a')['content'] == record['content']
    assert system.receipts.get('user:a', receipt['receipt_id'])['status'] == 'verified'
    assert not system.service.run_once()
    assert max(system.bucket.read_sizes) <= 65536
    assert system.store.stats('user:a') == dict(receipts=1, receipt_counts=dict(total=1, awaiting_upload=0, pending_verification=0, verified=1, rejected=0, failed=0), queued=0, running=0, completed=1,
                                               needs_review=0, failed=0, draft_items=1, review_items=0, archived_items=0,
                                               duplicate_items=0, outdated_items=0, indexing_items=0, published_items=0)
    with system.store._connection() as connection:
        stored = connection.execute('SELECT record_json FROM knowledge_revisions').fetchone()['record_json']
        assert '"content"' not in stored and 'content_ref' in stored
    assert not any(key in json.dumps(result) for key in ('object_key', 'lease_token', 'content_ref'))


def test_sensitive_text_is_cleaned_preview_only_not_imported(system):
    submit(system, BODY + '\npassword=do-not-leak-123456\n')
    assert system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review' and not result['counts']['draft']
    item = next(item for item in result['items'] if item['has_preview'])
    assert 'redaction_review' in item['reason_codes']
    preview = system.service.get_item(result['job_id'], item['item_id'], 'user:a')['content']
    assert 'do-not-leak' not in preview and '已隐藏' in preview
    assert system.catalogue.list_assets() == []


def test_zip_records_every_member_and_blocks_missing_dependencies(system):
    files = {'good.md': BODY, 'missing.md': '# 使用方法\n参考 [附件](lost.txt) 后完成工作。',
             'tools/run.sh': 'echo never-execute', 'photo.png': b'fake image'}
    submit(system, zip_bytes(files), filename='bundle.zip')
    assert system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review' and result['counts']['draft'] == 1
    assert set(files) <= {item['source_path'] for item in result['items']}
    assert any('unresolved_reference' in item['reason_codes'] for item in result['items'])
    assert any(item['source_path'] == 'tools/run.sh' and item['status'] == 'archived' for item in result['items'])
    assert len(system.catalogue.list_assets()) == 1


@pytest.mark.parametrize('filename,data,reason', [
    ('input.exe', b'binary', 'unsupported_file_type'), ('broken.zip', b'bad zip', 'invalid_archive'),
])
def test_unsupported_or_bad_file_has_durable_reason(system, filename, data, reason):
    submit(system, data, filename=filename)
    system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review' and result['error_code'] == reason
    assert result['items'][0]['reason_codes'] == [reason]
    assert system.catalogue.list_assets() == []


def test_zip_credentials_block_all_automatic_drafts(system):
    submit(system, zip_bytes({'good.md': BODY, '.env': 'PRIVATE=value'}), filename='bundle.zip')
    system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review' and result['counts']['draft'] == 0
    assert any('package_contains_credentials' in item['reason_codes'] for item in result['items'])
    assert system.catalogue.list_assets() == []


def test_unsafe_zip_never_creates_draft_and_preserves_member_inventory(system):
    submit(system, zip_bytes({'../escape.md': BODY, 'good.md': BODY}), filename='bundle.zip')
    system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review'
    assert {item['source_path'] for item in result['items']} == {'../escape.md', 'good.md'}
    assert system.catalogue.list_assets() == []


def test_enqueuing_and_claims_are_atomic_and_owner_scoped(system):
    submit(system)
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(lambda _: system.store.enqueue_verified(), range(12))) == 1
        claims = list(pool.map(lambda _: system.store.claim(), range(6)))
    assert len([row for row in claims if row]) == 1
    result = job(system)
    assert system.store.list_jobs('user:b')['total'] == 0
    assert system.store.stats('user:b')['receipts'] == 0
    with pytest.raises(ProcessingError, match='processing_job_not_found'):
        system.store.get_job(result['job_id'], 'user:b')


def test_expired_worker_cannot_insert_draft_or_finish(system):
    submit(system)
    system.store.enqueue_verified()
    first = system.store.claim()
    system.now[0] += LEASE_SECONDS + 1
    second = system.store.claim()
    assert first['job_id'] == second['job_id'] and first['lease_token'] != second['lease_token']
    entry = {'knowledge_id': 'ka_test', 'title': 'Test', 'content': BODY, 'sources': [{'name': 'synthetic'}]}
    with pytest.raises(LeaseLost):
        _FencedDraftStore(system.store, first, system.drafts).import_draft(entry, allow_new_revision=False)
    with pytest.raises(LeaseLost):
        system.store.finish(first, 'completed')
    assert system.catalogue.list_assets() == []
    _FencedDraftStore(system.store, second, system.drafts).import_draft(entry, allow_new_revision=False)
    assert len(system.catalogue.list_assets()) == 1


def test_bounded_retry_then_explicit_retry_records_actor(system):
    submit(system)
    system.bucket.read_error = RuntimeError('NEVER expose signed-secret-url')
    for _ in range(MAX_ATTEMPTS):
        assert system.service.run_once()
        system.now[0] += 301
    result = job(system)
    assert result['status'] == 'failed' and result['retryable']
    assert not system.service.run_once()
    retried = system.store.retry(result['job_id'], None, actor='reviewer:a')
    assert retried['status'] == 'queued' and retried['attempts'] == 0
    assert any(event['event_type'] == 'retried' and event['actor'] == 'reviewer:a' for event in retried['events'])
    assert 'NEVER' not in json.dumps(retried)
    system.bucket.read_error = None
    assert system.service.run_once()
    assert job(system)['status'] == 'completed'
    with pytest.raises(ProcessingError, match='processing_retry_not_allowed'):
        system.store.retry(result['job_id'], None, actor='reviewer:a')


def test_crash_after_draft_commit_recovers_without_second_revision(system, monkeypatch):
    submit(system)
    save = system.store.save_item
    attempts = [0]

    def fail_once(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError('simulated crash after draft commit')
        return save(*args, **kwargs)

    monkeypatch.setattr(system.store, 'save_item', fail_once)
    system.service.run_once()
    assert job(system)['status'] == 'queued' and len(system.catalogue.list_assets()) == 1
    system.now[0] += 31
    resumed = ProcessingService(system.store, system.objects, artifacts=system.service.artifacts, draft_objects=system.drafts)
    assert resumed.run_once()
    result = job(system)
    assert result['status'] == 'completed' and result['counts']['draft'] == 1
    assert system.catalogue.list_assets()[0]['latest_revision'] == 1


def test_principal_namespace_and_same_source_modified_do_not_overwrite(system):
    submit(system)
    system.service.run_once()
    first = system.catalogue.list_assets()[0]
    submit(system, principal='user:b')
    system.service.run_once()
    assert len(system.catalogue.list_assets()) == 2
    submit(system, BODY + '\n修改内容。')
    system.service.run_once()
    assert len(system.catalogue.list_assets()) == 2
    assert system.catalogue.snapshot(first['knowledge_id'])['revision'] == 2
    assert system.catalogue.snapshot(first['knowledge_id'], 1)['content'] == BODY
    results = system.store.list_jobs('user:a')['items']
    assert {item['status'] for item in results} == {'completed', 'needs_review'}


def test_preview_is_scoped_and_integrity_checked(system):
    submit(system)
    system.service.run_once()
    result = job(system)
    item = next(item for item in result['items'] if item['has_preview'])
    with pytest.raises(ProcessingError, match='processing_job_not_found'):
        system.service.get_item(result['job_id'], item['item_id'], 'user:b')
    stored = system.store.item_record(result['job_id'], item['item_id'], 'user:a')
    ref = json.loads(stored['content_ref'])
    system.bucket.data[ref['object_key']] = b'corrupted'
    with pytest.raises(ProcessingError, match='processing_preview_unavailable'):
        system.service.get_item(result['job_id'], item['item_id'], 'user:a')


def test_original_is_reverified_before_parsing(system):
    receipt = submit(system)
    original = system.bucket.data[receipt['object_key']]
    system.bucket.data[receipt['object_key']] = b'x' * len(original)
    # Preserve the old ETag response to exercise the actual SHA check, not only If-Match.
    head = system.bucket.head_object
    system.bucket.head_object = lambda key: SimpleNamespace(**{**vars(head(key)), 'etag': receipt['etag']}) if key == receipt['object_key'] else head(key)
    system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review' and result['error_code'] == 'object_hash_mismatch'
    assert system.catalogue.list_assets() == []


def test_zip_expansion_limit_fails_closed_without_draft(system, monkeypatch):
    import rag_app.knowledge_processing as processing
    submit(system, zip_bytes({'large.md': BODY * 20}), filename='large.zip')
    monkeypatch.setattr(processing, 'MAX_EXPANDED_BYTES', 20)
    system.service.run_once()
    result = job(system)
    assert result['error_code'] == 'processing_archive_limit' and result['status'] == 'needs_review'
    assert system.catalogue.list_assets() == []


def test_nonverified_receipts_not_enqueued_and_pagination_is_summarized(system):
    payload = dict(idempotency_key='incomplete', source_id='session:pending', filename='file.md', byte_length=1, sha256='0' * 64)
    system.receipts.prepare('user:a', payload)
    assert system.store.enqueue_verified() == 0
    submit(system)
    system.service.run_once()
    listed = system.store.list_jobs('user:a', status='completed', limit=1, offset=0)
    assert listed['total'] == 1 and len(listed['items']) == 1
    assert 'items' not in listed['items'][0] and 'events' not in listed['items'][0]
    assert system.store.list_jobs('user:a', limit=1, offset=1)['items'] == []


def test_invalid_package_manifest_is_reviewable_not_retried(system, monkeypatch):
    import rag_app.knowledge_processing as processing
    submit(system)

    def invalid(*_args, **_kwargs):
        raise ValueError('private untrusted malformed manifest')

    monkeypatch.setattr(processing, 'process_batch', invalid)
    assert system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review' and result['error_code'] == 'processing_parse_rejected'
    assert 'private untrusted' not in json.dumps(result)
    assert not system.service.run_once()


def test_large_legal_inventory_and_all_events_are_bounded_and_pageable(system):
    files = {'文' * 980 + str(number) + '.bin': b'x' for number in range(600)}
    submit(system, zip_bytes(files), filename='many-files.zip')
    assert system.service.run_once()
    result = job(system)
    assert result['status'] == 'needs_review'
    assert result['item_total'] == result['counts']['needs_review'] == 600
    assert len(result['items']) == len(result['events']) == result['limit'] == 100
    all_items, all_events = [], []
    for offset in range(0, max(result['item_total'], result['event_total']), 100):
        page = system.store.get_job(result['job_id'], 'user:a', item_offset=offset, event_offset=offset)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 2 * 1024 * 1024
        assert page['counts']['needs_review'] == 600
        all_items.extend(item['item_id'] for item in page['items'])
        all_events.extend(event['event_id'] for event in page['events'])
    assert len(all_items) == len(set(all_items)) == 600
    assert len(all_events) == len(set(all_events)) == result['event_total']
    with pytest.raises(ProcessingError, match='invalid_processing_query'):
        system.store.get_job(result['job_id'], 'user:a', limit=101)


def test_schema_creation_is_explicit_and_cli_defaults_closed(tmp_path):
    receipts = ReceiptStore(tmp_path / 'receipt.db')
    receipts.initialize()
    store = ProcessingStore(receipts.path)
    with pytest.raises(Exception):
        store.list_jobs(None)
    script = Path(__file__).resolve().parents[1] / 'scripts/knowledge_processing.py'
    dry = subprocess.run([sys.executable, str(script), 'init-db'], capture_output=True, text=True)
    assert dry.returncode == 0 and json.loads(dry.stdout)['status'] == 'dry_run'
    closed = subprocess.run([sys.executable, str(script), '--sqlite', str(receipts.path), 'work', '--once'],
                            capture_output=True, text=True)
    assert closed.returncode == 1 and json.loads(closed.stdout)['error_code'] == 'processing_disabled'
