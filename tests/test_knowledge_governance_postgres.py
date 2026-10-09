"""Opt-in governance checks in a fresh PostgreSQL schema, with inline bodies."""

from concurrent.futures import ThreadPoolExecutor
import os
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from rag_app.knowledge_governance import GovernanceStore
from rag_app.knowledge_receipts import LeaseLost
from rag_app.knowledge_store import KnowledgeStore


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_TEST_DATABASE_URL"), reason="explicit PostgreSQL test database not configured",
)]


@pytest.fixture
def governance_postgres(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    database_url = os.environ["KNOWLEDGE_TEST_DATABASE_URL"]
    for name in tuple(os.environ):
        if name.startswith(("KNOWLEDGE_", "OSS_", "OPENSEARCH_", "DASHSCOPE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_BODY_STORAGE", "inline")
    schema = "knowledge_governance_test_" + uuid4().hex
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query["options"] = "-csearch_path=" + schema + " -clock_timeout=10000 -cstatement_timeout=15000"
    test_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    try:
        admin = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
    except Exception:
        raise RuntimeError("test database connection failed") from None
    created = False
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        catalog = KnowledgeStore(database_url=test_url)
        governance = GovernanceStore(catalog)
        governance.initialize()
        yield SimpleNamespace(catalog=catalog, governance=governance, url=test_url)
    finally:
        if created:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def submit(system, number, content="Synthetic governance source.", base_revision=None, guard=None):
    # Each worker owns a connection; only the PostgreSQL locks serialize writes.
    governance = GovernanceStore(KnowledgeStore(database_url=system.url))
    return governance.import_asset(
        {"knowledge_id": "synthetic_source", "title": "Synthetic governance", "content": content,
         "kind": "method", "sources": [{"name": "synthetic.md"}], "evidence": {"receipt_id": str(number)}},
        {"attachment_sha256": []}, item_id="item-" + str(number), receipt_id="receipt-" + str(number),
        principal="user:synthetic", source_id="synthetic-agent", source_identity="synthetic-asset",
        base_revision=base_revision, guard=guard,
    )


def counts(system):
    with system.catalog._connection() as connection:
        return tuple(connection.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"] for table in (
            "knowledge_assets", "knowledge_revisions", "knowledge_submissions", "knowledge_revision_fingerprints",
        ))


def test_postgres_concurrent_duplicate_source_retains_each_receipt_once(governance_postgres):
    system = governance_postgres
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda number: submit(system, number), range(8)))
    assert counts(system) == (1, 1, 8, 1)
    assert sum(result["disposition"] == "new" for result in results) == 1
    assert {result["record"]["revision"] for result in results} == {1}
    assert {result["expected_generation"] for result in results} == {1}
    assert submit(system, 0)["record"]["revision"] == 1
    assert counts(system) == (1, 1, 8, 1)


def test_postgres_concurrent_changes_cannot_both_approve_one_source_base(governance_postgres):
    system = governance_postgres
    submit(system, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda number: submit(system, number, "Changed source " + str(number), base_revision=1), [2, 3]))
    assert counts(system) == (1, 3, 3, 3)
    assert {result["record"]["revision"] for result in results} == {2, 3}
    assert sum(result["publication_allowed"] for result in results) == 1
    assert sum("source_base_revision_conflict" in result["reasons"] for result in results) == 1
    assert system.catalog.published_count() == 0


def test_postgres_governance_lease_guard_and_late_failure_rollback(governance_postgres, monkeypatch):
    system = governance_postgres

    def expired(connection):
        raise LeaseLost()

    with pytest.raises(LeaseLost):
        submit(system, 1, guard=expired)
    assert counts(system) == (0, 0, 0, 0)
    execute = GovernanceStore._execute

    def fail_submission(self, connection, statement, params=()):
        if "INSERT INTO knowledge_submissions" in statement:
            raise RuntimeError("synthetic submission commit failure")
        return execute(self, connection, statement, params)

    with monkeypatch.context() as patch:
        patch.setattr(GovernanceStore, "_execute", fail_submission)
        with pytest.raises(RuntimeError, match="synthetic submission"):
            submit(system, 1)
    assert counts(system) == (0, 0, 0, 0)
    first = submit(system, 1)
    with pytest.raises(LeaseLost):
        submit(system, 1, guard=expired)
    assert counts(system) == (1, 1, 1, 1)
    assert submit(system, 1)["record"] == first["record"]
