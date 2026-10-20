"""Opt-in graph cache checks in disposable schemas of an explicit isolated test DB."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from rag_app.knowledge_graph import ALGORITHM_VERSION, KnowledgeGraph, _features
from rag_app.knowledge_store import KnowledgeStore

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_TEST_DATABASE_URL"), reason="explicit isolated PostgreSQL test database not configured")]


class Queued:
    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args):
        self.tasks.append((fn, args))

    def finish(self):
        tasks, self.tasks = self.tasks, []
        for fn, args in tasks:
            fn(*args)


def publish(store, key, content):
    draft = store.import_draft({"knowledge_id": key, "title": key, "content": content,
                                "sources": [{"name": "synthetic.md"}]})
    return store.publish(key, draft["revision"], expected_generation=draft["generation"], confirmed_by="synthetic")


def ready(store, queue, graph):
    graph.read(store, lambda row: "other")
    queue.finish()
    result = graph.read(store, lambda row: "other")
    assert result["status"] == "ready"
    return result


@pytest.fixture
def isolated_graph_pg(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    database_url = os.environ["KNOWLEDGE_TEST_DATABASE_URL"]
    for name in tuple(os.environ):
        if name.startswith(("KNOWLEDGE_", "OSS_", "OPENSEARCH_", "DASHSCOPE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_BODY_STORAGE", "inline")
    schemas = ["knowledge_graph_test_" + uuid4().hex for _ in range(2)]
    parts = urlsplit(database_url)

    def url(*names):
        query = dict(parse_qsl(parts.query))
        query["options"] = "-csearch_path=" + ",".join(names) + " -clock_timeout=10000 -cstatement_timeout=15000"
        return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))

    try:
        admin = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
    except Exception:
        raise RuntimeError("isolated graph test database connection failed") from None
    created = []
    try:
        for schema in schemas:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created.append(schema)
        store = KnowledgeStore(database_url=url(schemas[0]))
        store.initialize()
        yield SimpleNamespace(store=store, url=url, schemas=schemas)
    finally:
        for schema in reversed(created):
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def test_postgres_graph_restart_and_incremental_publication_safety(isolated_graph_pg, monkeypatch):
    store = isolated_graph_pg.store
    queue, graph = Queued(), KnowledgeGraph(executor=Queued())
    graph.executor = queue
    old = publish(store, "left", "旧版引用 [[right]]")
    publish(store, "right", "岗位画像与候选人面试")
    before = ready(store, queue, graph)
    assert before["counts"]["explicit_edges"] == 1
    reads = Counter()
    original = KnowledgeStore.get_published

    def counted(catalog, kid):
        reads[kid] += 1
        return original(catalog, kid)

    monkeypatch.setattr(KnowledgeStore, "get_published", counted)
    reloaded = KnowledgeStore(database_url=store.database_url)
    result = KnowledgeGraph(executor=queue).read(reloaded, lambda row: "other")
    assert result["status"] == "ready" and result["edges"] == before["edges"]
    assert not reads and not queue.tasks
    current = publish(store, "left", "新版没有引用")
    result = ready(reloaded, queue, KnowledgeGraph(executor=queue))
    assert not result["edges"] and reads == Counter(left=1)
    assert store.save_graph_features(old, ALGORITHM_VERSION, _features(old, old["content"])) is False
    cached = {row["knowledge_id"]: row for row in store.graph_features(ALGORITHM_VERSION)}
    assert cached["left"]["revision"] == current["revision"]
    store.withdraw("right")
    reads.clear()
    final = KnowledgeGraph(executor=queue).read(reloaded, lambda row: "other")
    assert final["status"] == "ready" and final["counts"]["indexed_assets"] == 1
    assert [node["knowledge_id"] for node in final["nodes"]] == ["left"]
    assert not final["edges"] and not reads and not queue.tasks


def test_postgres_old_cache_writer_waits_then_rechecks_publication(isolated_graph_pg):
    store = isolated_graph_pg.store
    old = publish(store, "left", "旧正文 [[old-only]]")
    draft = store.import_draft({"knowledge_id": "left", "title": "left", "content": "新正文",
                                "sources": [{"name": "synthetic.md"}]})
    waiting = threading.Event()

    class WaitingStore(KnowledgeStore):
        def _lock(self, connection, knowledge_id):
            if knowledge_id == "left":
                waiting.set()
            super()._lock(connection, knowledge_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._connection(write=True) as connection:
            store._lock(connection, "left")
            future = pool.submit(WaitingStore(database_url=store.database_url).save_graph_features,
                                 old, ALGORITHM_VERSION, _features(old, old["content"]))
            assert waiting.wait(timeout=10)
            current = store._publish(connection, draft, draft["generation"], "synthetic")
        assert future.result(timeout=10) is False
    assert store.save_graph_features(current, ALGORITHM_VERSION, _features(current, current["content"])) is True
    assert store.save_graph_features(old, ALGORITHM_VERSION, _features(old, old["content"])) is False
    cached = store.graph_features(ALGORITHM_VERSION)
    assert len(cached) == 1 and cached[0]["revision"] == current["revision"]
    assert "old-only" not in cached[0]["features_json"]


def test_postgres_missing_local_cache_cannot_fall_back_to_other_schema(isolated_graph_pg):
    fixture = isolated_graph_pg
    local = fixture.store
    local_record = publish(local, "same", "本库正文")
    other = KnowledgeStore(database_url=fixture.url(fixture.schemas[1]))
    other.initialize()
    other_record = publish(other, "same", "其他库正文 [[foreign-only]]")
    assert other.save_graph_features(other_record, ALGORITHM_VERSION, _features(other_record, other_record["content"]))
    protected = other.graph_features(ALGORITHM_VERSION)
    with local._connection(write=True) as connection:
        connection.execute("DROP TABLE knowledge_graph_features")
    search_path = KnowledgeStore(database_url=fixture.url(*fixture.schemas))
    assert search_path.graph_features(ALGORITHM_VERSION) == []
    assert search_path.save_graph_features(local_record, ALGORITHM_VERSION, _features(local_record, local_record["content"])) is False
    queue = Queued()
    result = ready(search_path, queue, KnowledgeGraph(executor=queue))
    assert result["counts"]["indexed_assets"] == 1
    assert "foreign-only" not in str(result)
    assert other.graph_features(ALGORITHM_VERSION) == protected
    with local._connection() as connection:
        assert connection.execute("SELECT to_regclass('knowledge_graph_features') AS table_name").fetchone()["table_name"] is None
