"""Opt-in real PostgreSQL concurrency check in a disposable random schema."""

from concurrent.futures import ThreadPoolExecutor
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from rag_app.knowledge_receipts import LEASE_SECONDS, ReceiptError, ReceiptStore


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_TEST_DATABASE_URL"), reason="explicit PostgreSQL test database not configured",
)]


def test_receipt_postgres_concurrent_idempotency_global_slot_and_fencing():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "knowledge_receipt_test_" + uuid4().hex
    database_url = os.environ["KNOWLEDGE_TEST_DATABASE_URL"]
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query["options"] = "-csearch_path=" + schema
    test_url = urlunsplit(parts._replace(query=urlencode(query)))
    try:
        admin = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
    except Exception:
        raise RuntimeError("test database connection failed") from None
    now = [1000.0]

    def store():
        return ReceiptStore(database_url=test_url, clock=lambda: now[0])

    payload = {"idempotency_key": "synthetic-1", "source_id": "synthetic-session", "filename": "synthetic.json",
               "byte_length": 1, "sha256": "0" * 64}
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        with pytest.raises(psycopg.errors.UndefinedTable):
            store().prepare("user:a", payload)
        store().initialize()
        store().initialize()
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: store().prepare("user:a", payload), range(12)))
        assert len({row["receipt_id"] for row in rows}) == 1
        with pytest.raises(ReceiptError, match="idempotency_conflict"):
            store().prepare("user:a", {**payload, "byte_length": 2})
        second = store().prepare("user:b", payload)
        assert second["receipt_id"] != rows[0]["receipt_id"]
        store().enqueue(rows[0], "1" * 32)
        store().enqueue(second, "2" * 32)
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = [row for row in pool.map(lambda _: store().claim(), range(6)) if row]
        assert len(claims) == 1
        first = claims[0]
        now[0] += LEASE_SECONDS + 1
        replacement = store().claim()
        assert replacement["receipt_id"] == first["receipt_id"]
        assert not store().finish(first, "verified")
        assert store().finish(replacement, "verified")
        remaining = store().claim()
        assert remaining["receipt_id"] != replacement["receipt_id"]
        assert store().finish(remaining, "verified")
        assert store().claim() is None
        tables = admin.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (schema,)).fetchall()
        assert tables == [("knowledge_upload_receipts",)]
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
