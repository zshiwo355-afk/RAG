"""Receipt failures remain readable without fabricating a processing job."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_app.knowledge_receipts import ReceiptStore
from rag_app.knowledge_processing import ProcessingStore, ProcessingError
from rag_app.knowledge_processing_api import get_processing_store, get_catalog, router
from rag_app.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.offline
BASE = '/api/rag/knowledge-processing'
TOKEN = 'synthetic-receipt-history-' + 'x' * 40
SELF, READ, REVIEW, DASH = ('company_knowledge.' + value for value in
                          ('submissions.read', 'read', 'review', 'dashboard.read'))


def headers(permission=SELF, principal='alice'):
    return {'Authorization': 'Bearer ' + TOKEN, 'X-Knowledge-Principal': principal,
            'X-Knowledge-Permissions': permission}


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_PROCESSING_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_TOKEN', TOKEN)
    receipts = ReceiptStore(tmp_path / 'history.db', clock=lambda: 1000)
    receipts.initialize()
    store = ProcessingStore(receipts.path, clock=receipts.clock)
    store.initialize()
    rows = {}
    for principal, status in [('alice', state) for state in ('awaiting_upload', 'pending_verification', 'verified', 'rejected', 'failed')] + [('bob', 'rejected')]:
        row = receipts.prepare(principal, dict(idempotency_key=status, source_id='source:' + status,
            filename=status + '.zip', byte_length=10, sha256='0' * 64))
        with store._connection(write=True) as db:
            db.execute('UPDATE knowledge_upload_receipts SET status=?,error_code=? WHERE receipt_id=?',
                (status, 'object_hash_mismatch' if status == 'rejected' else 'verification_retries_exhausted' if status == 'failed' else None, row['receipt_id']))
        rows[principal + ':' + status] = row['receipt_id']
    assert store.enqueue_verified() == 1
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_processing_store] = lambda: store
    app.dependency_overrides[get_catalog] = lambda: KnowledgeStore(receipts.path)
    with TestClient(app) as client:
        yield client, store, rows


def test_history_includes_rejected_without_job_and_paginates_per_owner(history):
    client, store, ids = history
    all_items = []
    for offset in (0, 2, 4):
        response = client.get(BASE + f'/receipts?limit=2&offset={offset}', headers=headers())
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        data = response.json()
        assert data['total'] == 5 and data['receipt_counts'] == dict(total=5, awaiting_upload=1, pending_verification=1, verified=1, rejected=1, failed=1)
        all_items.extend(data['items'])
    assert [row['receipt_id'] for row in all_items] == sorted((value for key, value in ids.items() if key.startswith('alice:')), reverse=True)
    assert len({row['receipt_id'] for row in all_items}) == 5
    expected = {'receipt_id', 'filename', 'byte_length', 'status', 'error_code', 'created_at', 'updated_at', 'content_verified', 'retryable', 'job_id', 'job_status'}
    for row in all_items:
        assert set(row) == expected
        assert bool(row['job_id']) == (row['status'] == 'verified')
    rejected = client.get(BASE + '/receipts?status=rejected', headers=headers()).json()
    assert rejected['total'] == 1 and rejected['items'][0]['receipt_id'] == ids['alice:rejected']
    assert rejected['items'][0]['retryable'] is False and rejected['items'][0]['job_id'] is None
    assert rejected['receipt_counts']['total'] == 5  # Counts cover the selected owner, not just this filter.
    assert store.list_jobs('alice')['total'] == 1
    bob = client.get(BASE + '/receipts', headers=headers(principal='bob')).json()
    assert [row['receipt_id'] for row in bob['items']] == [ids['bob:rejected']]


def test_history_scope_and_aggregate_do_not_grant_private_receipts(history):
    client, _, _ = history
    assert client.get(BASE + '/receipts').status_code == 401
    for permission in (SELF, READ, DASH):
        assert client.get(BASE + '/receipts?scope=company', headers=headers(permission)).status_code == 403
    for permission in (READ, DASH):
        assert client.get(BASE + '/receipts', headers=headers(permission)).status_code == 403
    assert client.get(BASE + '/receipts?scope=company', headers=headers(REVIEW)).json()['total'] == 6
    value = client.get(BASE + '/overview', headers=headers()).json()
    assert value['counts']['failed'] == 0 and value['counts']['receipts'] == 5
    assert value['receipt_counts']['failed'] == value['receipt_counts']['rejected'] == 1
    assert 'receipt_counts' not in value['counts']
    company = client.get(BASE + '/overview?scope=company', headers=headers(DASH)).json()
    assert company['receipt_counts']['total'] == 6 and company['receipt_counts']['rejected'] == 2


def test_claimed_receipt_stays_in_verification_counts_filter_and_pages(history):
    client, store, ids = history
    claimed = ReceiptStore.claim(store)
    assert claimed['status'] == 'verifying' and claimed['receipt_id'] == ids['alice:pending_verification']
    waiting = store.prepare('alice', dict(idempotency_key='another', source_id='source:another',
        filename='another.zip', byte_length=10, sha256='1' * 64))
    store.enqueue(waiting, 'synthetic-etag')
    found = []
    for offset in (0, 1):
        response = client.get(BASE + f'/receipts?status=pending_verification&limit=1&offset={offset}', headers=headers())
        assert response.status_code == 200
        data = response.json()
        assert data['total'] == 2 and data['receipt_counts']['pending_verification'] == 2
        assert data['receipt_counts']['total'] == sum(value for key, value in data['receipt_counts'].items() if key != 'total')
        assert data['items'][0]['status'] == 'pending_verification' and data['items'][0]['job_id'] is None
        found.extend(row['receipt_id'] for row in data['items'])
    assert set(found) == {claimed['receipt_id'], waiting['receipt_id']}
    overview = client.get(BASE + '/overview', headers=headers()).json()
    assert overview['receipt_counts']['pending_verification'] == 2
    assert store.get('alice', claimed['receipt_id'])['status'] == 'verifying'  # Read projection never mutates the lease state.


@pytest.mark.parametrize('query', ['limit=101', 'offset=-1', 'offset=1000001', 'status=queued',
    'status=secret-value', 'scope=company&scope=self', 'principal=bob', 'object_key=private', 'limit=1&limit=2'])
def test_history_rejects_invalid_or_spoofed_queries(history, query):
    client, _, _ = history
    response = client.get(BASE + '/receipts?' + query, headers=headers(REVIEW))
    assert response.status_code == 422
    assert response.json()['detail'] == 'invalid_processing_request'


def test_history_job_association_matches_owner_and_selects_one_latest_job(history):
    _, store, ids = history
    original = store.list_jobs('alice')['items'][0]
    with store._connection(write=True) as db:
        # A mismatching owner must never expose another person's processing job.
        for key, owner, created in [('b' * 32, 'bob', 3000), ('c' * 32, 'alice', 2000)]:
            db.execute('''INSERT INTO knowledge_processing_jobs
                (job_id,receipt_id,principal,source_id,filename,rule_version,status,stage,created_at,updated_at)
                VALUES (?,?,?,?,?,?, 'queued','queued',?,?)''',
                (key, ids['alice:verified'], owner, 'source', 'synthetic.zip', key, created, created))
    value = store.list_receipts('alice', status='verified')
    assert value['total'] == 1 and len(value['items']) == 1
    assert value['items'][0]['job_id'] == 'c' * 32 != original['job_id']
    with pytest.raises(ProcessingError):
        store.list_receipts('alice', limit=True)
