"""Opt-in PostgreSQL checks; only this run's random schema can be removed."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from rag_app.knowledge_processing import LEASE_SECONDS, ProcessingError, ProcessingStore, _FencedDraftStore
from rag_app.knowledge_receipts import LeaseLost, ReceiptStore
from rag_app.knowledge_store import KnowledgeStore


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_TEST_DATABASE_URL"), reason="explicit PostgreSQL test database not configured",
)]


class MemoryObjects:
    """No SDK or network: exercise real catalogue writes with synthetic bodies."""

    def __init__(self):
        self.data = {}

    def put(self, content):
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        self.data[digest] = content
        return {"sha256": digest, "byte_length": len(encoded)}

    def get(self, reference):
        return self.data[reference["sha256"]]


@pytest.fixture
def postgres(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    database_url = os.environ["KNOWLEDGE_TEST_DATABASE_URL"]
    # No inherited app credential/config may select production tables or OSS.
    for name in tuple(os.environ):
        if name.startswith(("KNOWLEDGE_", "OSS_", "OPENSEARCH_", "DASHSCOPE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    schema = "knowledge_processing_test_" + uuid4().hex
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    # Replace supplied options: no public fallback and bounded lock waits.
    query["options"] = "-csearch_path=" + schema + " -clock_timeout=10000 -cstatement_timeout=15000"
    # libpq URIs decode %20, not form-encoded '+' separators in options.
    test_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    try:
        admin = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
    except Exception:
        raise RuntimeError("test database connection failed") from None
    created = False
    now = [1000.0]
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        receipts = ReceiptStore(database_url=test_url, clock=lambda: now[0])
        receipts.initialize()
        store = ProcessingStore(database_url=test_url, clock=receipts.clock)
        store.initialize()
        objects = MemoryObjects()
        catalogue = KnowledgeStore(database_url=test_url, objects=objects)
        catalogue.initialize()
        with store._connection() as connection:
            assert connection.execute("SELECT current_schema() AS name").fetchone()["name"] == schema
        yield SimpleNamespace(admin=admin, schema=schema, now=now, receipts=receipts,
                              store=store, objects=objects, catalogue=catalogue, url=test_url)
    finally:
        if created:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def enqueue(system, key="synthetic-1"):
    receipt = system.receipts.prepare("user:synthetic", {
        "idempotency_key": key, "source_id": key, "filename": "synthetic.md",
        "byte_length": 1, "sha256": "0" * 64,
    })
    # This fixture deliberately bypasses upload verification in its isolated schema.
    with system.store._connection(write=True) as connection:
        system.store._execute(connection, "UPDATE knowledge_upload_receipts SET status='verified' WHERE receipt_id=?",
                              (receipt["receipt_id"],))
    return receipt


@pytest.mark.parametrize("old_action", ["finish", "heartbeat"])
def test_claim_rechecks_row_and_global_slot_after_postgres_lock_wait(postgres, old_action):
    system = postgres
    enqueue(system)
    assert system.store.enqueue_verified() == 1
    first = system.store.claim()
    if old_action == "heartbeat":
        system.now[0] += 1
        enqueue(system, "synthetic-2")
        assert system.store.enqueue_verified() == 1

    waiting = threading.Event()
    observed_pid = []

    class ObservedStore(ProcessingStore):
        def _execute(self, connection, statement, parameters=()):
            if "FOR UPDATE" in statement:
                observed_pid.append(connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                waiting.set()
            return super()._execute(connection, statement, parameters)

    claimant = ObservedStore(database_url=system.url, clock=system.store.clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with system.store._connection(write=True) as holding:
            holding_pid = holding.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            system.store.fence(holding, first)
            if old_action == "finish":
                system.store._execute(holding, "UPDATE knowledge_processing_jobs SET status='completed',lease_until=0 WHERE job_id=?", (first["job_id"],))
            else:
                system.store._execute(holding, "UPDATE knowledge_processing_jobs SET lease_until=? WHERE job_id=?", (system.now[0] + 2 * LEASE_SECONDS, first["job_id"]))
            # Other connections still see the old expired lease while this txn holds its row.
            system.now[0] += LEASE_SECONDS + 1
            future = pool.submit(claimant.claim)
            assert waiting.wait(5), "claim did not reach its row-lock SELECT"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                blockers = system.admin.execute("SELECT pg_blocking_pids(%s)", (observed_pid[0],)).fetchone()[0]
                if holding_pid in blockers:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("claim did not wait on the previous worker's transaction")
            assert not future.done()
        # Commit either completion or renewed lease; neither permits reclaiming the task.
        assert future.result(timeout=10) is None
    detail = system.store.get_job(first["job_id"], "user:synthetic")
    assert detail["status"] == ("completed" if old_action == "finish" else "running")
    assert detail["attempts"] == 1
    if old_action == "heartbeat":
        assert system.store.stats("user:synthetic")["queued"] == 1


def test_postgres_idempotency_slot_and_draft_transaction_fence(postgres):
    system = postgres
    enqueue(system)
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(lambda _: system.store.enqueue_verified(), range(12))) == 1
        claimed = [row for row in pool.map(lambda _: system.store.claim(), range(6)) if row]
    assert len(claimed) == 1
    first = claimed[0]
    system.now[0] += LEASE_SECONDS + 1
    replacement = system.store.claim()
    assert replacement["job_id"] == first["job_id"]
    assert replacement["lease_token"] != first["lease_token"]
    entry = {"knowledge_id": "synthetic_draft", "title": "Synthetic", "content": "Synthetic PostgreSQL fence check.",
             "sources": [{"name": "synthetic"}]}
    with pytest.raises(LeaseLost):
        _FencedDraftStore(system.store, first, system.objects).import_draft(entry, allow_new_revision=False)
    with pytest.raises(LeaseLost):
        system.store.finish(first, "completed")
    assert system.catalogue.list_assets() == []
    draft = _FencedDraftStore(system.store, replacement, system.objects).import_draft(entry, allow_new_revision=False)
    assert draft["revision"] == 1 and draft["status"] == "draft"
    system.store.finish(replacement, "completed")
    assert system.store.claim() is None
    assert system.catalogue.published_count() == 0
    tables = {row[0] for row in system.admin.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (system.schema,)).fetchall()}
    assert tables == {"knowledge_upload_receipts", "knowledge_processing_jobs", "knowledge_processing_items",
                      "knowledge_processing_events", "knowledge_assets", "knowledge_revisions",
                      "knowledge_revision_fingerprints", "knowledge_submissions"}


def test_resolution_rechecks_job_after_concurrent_retry_row_lock(postgres):
    system = postgres
    enqueue(system)
    system.store.enqueue_verified()
    job = system.store.claim()
    item = {'item_id': 'a' * 32, 'source_path': 'synthetic.md', 'title': 'Synthetic',
            'status': 'needs_review', 'reason_codes': ['synthetic_missing_dependency']}
    system.store.save_item(job, item)
    system.store.finish(job, 'needs_review')
    with system.store._connection(write=True) as connection:
        system.store._execute(connection, "UPDATE knowledge_processing_jobs SET status='failed' WHERE job_id=?", (job['job_id'],))
    reached, pids = threading.Event(), []

    class ObservedStore(ProcessingStore):
        def _execute(self, connection, statement, parameters=()):
            if 'FOR UPDATE' in statement:
                pids.append(connection.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
                reached.set()
            return super()._execute(connection, statement, parameters)

    resolver = ObservedStore(database_url=system.url, clock=system.store.clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with system.store._connection(write=True) as connection:
            held_pid = connection.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
            system.store._job(connection, job['job_id'], None, lock=True)
            system.store._execute(connection, "UPDATE knowledge_processing_jobs SET status='queued' WHERE job_id=?", (job['job_id'],))
            future = pool.submit(resolver.resolve, job['job_id'], item['item_id'], None,
                                 expected_version=1, action='archive', reason='synthetic archive', actor='reviewer')
            assert reached.wait(5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if held_pid in system.admin.execute('SELECT pg_blocking_pids(%s)', (pids[0],)).fetchone()[0]:
                    break
                time.sleep(0.02)
            else:
                pytest.fail('resolve did not serialize with retry')
            assert not future.done()
        with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
            future.result(timeout=10)
    stored = system.store.item_record(job['job_id'], item['item_id'], None)
    assert stored['status'] == 'needs_review' and stored['version'] == 1
