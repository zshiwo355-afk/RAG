"""Opt-in PostgreSQL check in a disposable schema, never the application tables.

Run against an explicitly supplied test database only:
KNOWLEDGE_TEST_DATABASE_URL=postgresql://... python3 -m pytest -q tests/test_knowledge_postgres.py
"""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from rag_app.knowledge_store import KnowledgeStore


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_TEST_DATABASE_URL"), reason="explicit PostgreSQL test database not configured",
)]


def test_postgres_initialization_concurrency_and_withdrawal(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    # A random schema isolates this check from every existing asset and table.
    schema = "knowledge_test_" + uuid4().hex
    database_url = os.environ["KNOWLEDGE_TEST_DATABASE_URL"]
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-csearch_path={schema}"
    test_url = urlunsplit(parts._replace(query=urlencode(query)))
    monkeypatch.setenv("KNOWLEDGE_BODY_STORAGE", "inline")
    try:
        admin = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
    except Exception:
        raise RuntimeError("test database connection failed") from None
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        store = KnowledgeStore(database_url=test_url)
        entry = {
            "knowledge_id": "pg-example", "title": "数据库并发检查", "content": "完整正文",
            "sources": [{"name": "合成测试"}], "department": "测试", "scenarios": ["并发"],
        }
        assert store.list_assets() == store.published_revisions() == []
        with pytest.raises(RuntimeError, match="not initialized"):
            store.import_draft(entry)
        store.initialize()
        store.initialize()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: KnowledgeStore(database_url=test_url).import_draft(entry), range(2)))
        assert results[0] == results[1]
        assert store.list_assets()[0]["latest_revision"] == 1

        def publish(_):
            try:
                KnowledgeStore(database_url=test_url).publish("pg-example", 1, expected_generation=1, confirmed_by="测试")
                return "published"
            except RuntimeError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(publish, range(2))) == ["published", "stale"]
        assert store.get_published("pg-example")["content"] == entry["content"]
        assert store.published_revisions(department="测试", scenario="并发") == [{"knowledge_id": "pg-example", "revision": 1}]
        latest = store.import_draft({**entry, "content": "新版待审"})
        assert store.get_published("pg-example")["revision"] == 1
        store.withdraw("pg-example")
        with pytest.raises(RuntimeError, match="changed"):
            store.publish("pg-example", 2, expected_generation=latest["generation"], confirmed_by="测试")
        assert store.get_published("pg-example") is None
        assert store.published_revisions() == []

        automatic = store.import_draft({**entry, "knowledge_id": "pg-automatic"})
        with store._connection(write=True) as connection:
            connection.execute("CREATE TABLE publication_outcomes (id TEXT PRIMARY KEY)")
            connection.execute("CREATE TABLE publication_lease (expires DOUBLE PRECISION)")
            connection.execute("INSERT INTO publication_lease VALUES (EXTRACT(EPOCH FROM clock_timestamp()) + 60)")

        def guard(connection):
            lease = connection.execute("SELECT expires > EXTRACT(EPOCH FROM clock_timestamp()) AS valid FROM publication_lease FOR UPDATE").fetchone()
            if not lease["valid"]:
                raise RuntimeError("lease expired")

        def complete(connection, published):
            connection.execute("INSERT INTO publication_outcomes VALUES ('job-a')")
            raise RuntimeError("completion failed")

        with pytest.raises(RuntimeError, match="completion failed"):
            store.publish_automatic("pg-automatic", 1, expected_generation=automatic["generation"],
                                    confirmed_by="rule:v1", transaction_guard=guard, transaction_complete=complete)
        assert store.get_published("pg-automatic") is None
        with store._connection() as connection:
            assert connection.execute("SELECT COUNT(*) AS n FROM publication_outcomes").fetchone()["n"] == 0

        waiting = threading.Event()

        class WaitingStore(KnowledgeStore):
            def _lock(self, connection, knowledge_id):
                if knowledge_id == "pg-automatic":
                    waiting.set()
                super()._lock(connection, knowledge_id)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with store._connection(write=True) as connection:
                store._lock(connection, "pg-automatic")
                future = pool.submit(
                    WaitingStore(database_url=test_url).publish_automatic,
                    "pg-automatic", 1, expected_generation=automatic["generation"],
                    confirmed_by="rule:v1", transaction_guard=guard,
                )
                assert waiting.wait(timeout=10)
                connection.execute("UPDATE publication_lease SET expires=0")
            with pytest.raises(RuntimeError, match="lease expired"):
                future.result(timeout=10)
        assert store.snapshot("pg-automatic") == automatic
        assert store.get_published("pg-automatic") is None
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
