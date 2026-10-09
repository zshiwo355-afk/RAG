from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from rag_app.knowledge_store import KnowledgeStore, normalize_entry


pytestmark = pytest.mark.offline


def draft(**changes):
    return {
        "knowledge_id": "meeting-01", "title": "会议准备方法", "content": "先说明决策目标，再准备证据。\n",
        "contributor": "示例贡献者", "kind": "method", "sources": [{"name": "示例访谈", "locator": "片段 1"}],
        "evidence": {"verified": False}, **changes,
    }


def test_import_is_idempotent_and_metadata_creates_immutable_revision(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    original = draft()
    first = store.import_draft(original)
    assert first["revision"] == first["generation"] == 1
    assert first["content_hash"] == hashlib.sha256(original["content"].encode()).hexdigest()
    assert store.import_draft(dict(reversed(list(original.items())))) == first
    original["sources"][0]["name"] = "caller mutation"
    assert store.snapshot("meeting-01")["sources"][0]["name"] == "示例访谈"
    second = store.import_draft(draft(evidence={"verified": True}))
    assert second["revision"] == second["generation"] == 2
    assert second["content_hash"] == first["content_hash"]
    assert second["payload_hash"] != first["payload_hash"]
    assert store.snapshot("meeting-01", 1)["evidence"] == {"verified": False}
    assert json.loads(json.dumps(second, ensure_ascii=False)) == second


def test_shared_draft_import_commits_with_caller_state_or_rolls_back_together(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    store.initialize()
    payload = normalize_entry(draft())
    with pytest.raises(RuntimeError, match="caller failed"):
        with store._connection(write=True) as connection:
            store._lock(connection, payload["knowledge_id"])
            store._import_draft(connection, payload, payload["payload_hash"])
            raise RuntimeError("caller failed")
    assert store.list_assets() == []
    with store._connection(write=True) as connection:
        store._lock(connection, payload["knowledge_id"])
        imported = store._import_draft(connection, payload, payload["payload_hash"])
        connection.execute("CREATE TABLE submission_binding (knowledge_id TEXT)")
        connection.execute("INSERT INTO submission_binding VALUES (?)", (imported["knowledge_id"],))
    assert store.snapshot("meeting-01") == imported
    assert store.import_draft(draft()) == imported


@pytest.mark.parametrize("changes", [
    {"title": "更新标题"}, {"contributor": "另一贡献者"}, {"kind": "case"},
    {"sources": [{"name": "新来源"}]}, {"content": "更新正文"},
    {"chunking_version": "structure-v1"},
])
def test_all_business_metadata_affects_version(tmp_path, changes):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    original = store.import_draft(draft())
    updated = store.import_draft(draft(**changes))
    assert updated["revision"] == 2
    assert updated["payload_hash"] != original["payload_hash"]


def test_draft_preserves_published_revision_and_withdraw_invalidates_inflight_publish(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    first = store.import_draft(draft())
    assert store.get_published("meeting-01") is None
    published = store.publish("meeting-01", 1, expected_generation=first["generation"], confirmed_by="示例审核人")
    second = store.import_draft(draft(content="新版本待审"))
    assert store.get_published("meeting-01")["revision"] == 1
    assert store.published_revisions() == [{"knowledge_id": "meeting-01", "revision": 1}]
    asset = store.list_assets()[0]
    assert asset["published_revision"] == 1 and asset["latest_revision"] == 2 and asset["has_draft"]
    with pytest.raises(RuntimeError, match="changed"):
        store.publish("meeting-01", 1, expected_generation=published["generation"], confirmed_by="示例审核人")
    withdrawn = store.withdraw("meeting-01")
    assert withdrawn["generation"] > second["generation"]
    assert store.get_published("meeting-01") is None
    assert store.published_revisions() == []
    with pytest.raises(RuntimeError, match="changed"):
        store.publish("meeting-01", 2, expected_generation=second["generation"], confirmed_by="示例审核人")
    assert store.snapshot("meeting-01", 1)["content"] == first["content"]
    fresh = store.snapshot("meeting-01")
    store.publish("meeting-01", 2, expected_generation=fresh["generation"], confirmed_by="示例审核人")
    assert store.get_published("meeting-01")["revision"] == 2


def test_asset_and_revision_states_survive_withdraw_new_draft_and_republish(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    first = store.import_draft(draft())
    assert first["status"] == store.snapshot("meeting-01")["status"] == "draft"
    assert store.list_assets()[0]["status"] == "draft"
    published = store.publish("meeting-01", 1, expected_generation=first["generation"], confirmed_by="示例审核人")
    assert published["status"] == store.get_published("meeting-01")["status"] == "published"
    second = store.import_draft(draft(content="新版本待审"))
    assert second["status"] == store.snapshot("meeting-01", 2)["status"] == "draft"
    assert store.snapshot("meeting-01", 1)["status"] == store.list_assets()[0]["status"] == "published"
    store.publish("meeting-01", 2, expected_generation=second["generation"], confirmed_by="示例审核人")
    assert store.snapshot("meeting-01", 1)["status"] == "withdrawn"
    store.withdraw("meeting-01")
    assert store.list_assets()[0]["status"] == "withdrawn"
    assert store.snapshot("meeting-01", 1)["status"] == store.snapshot("meeting-01", 2)["status"] == "withdrawn"
    assert store.import_draft(draft(content="新版本待审"))["status"] == "withdrawn"
    third = store.import_draft(draft(content="撤回后的新草稿"))
    assert third["status"] == store.list_assets()[0]["status"] == "draft"
    assert store.snapshot("meeting-01", 2)["status"] == "withdrawn"
    store.publish("meeting-01", 1, expected_generation=third["generation"], confirmed_by="示例审核人")
    assert store.snapshot("meeting-01", 1)["status"] == store.list_assets()[0]["status"] == "published"
    assert store.snapshot("meeting-01", 2)["status"] == "withdrawn"
    assert store.snapshot("meeting-01", 3)["status"] == "draft"
    assert store.snapshot("meeting-01", 1)["content"] == first["content"]


def test_withdraw_marks_asset_with_unpublished_newer_revision(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    first = store.import_draft(draft())
    store.publish("meeting-01", 1, expected_generation=first["generation"], confirmed_by="示例审核人")
    store.import_draft(draft(content="尚未发布的新版本"))
    store.withdraw("meeting-01")
    assert store.list_assets()[0]["status"] == "withdrawn"
    assert store.snapshot("meeting-01", 1)["status"] == "withdrawn"
    assert store.snapshot("meeting-01", 2)["status"] == "draft"


def test_revision_confirmation_survives_new_publication_withdraw_and_json_export(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    first = store.import_draft(draft())
    assert first["confirmed_by"] is None and first["published_at"] is None
    published_v1 = store.publish("meeting-01", 1, expected_generation=first["generation"], confirmed_by="示例确认人甲")
    assert store.snapshot("meeting-01", 1) == published_v1 == store.get_published("meeting-01")
    second = store.import_draft(draft(content="第二版正文"))
    published_v2 = store.publish("meeting-01", 2, expected_generation=second["generation"], confirmed_by="示例确认人乙")
    assert store.get_published("meeting-01") == store.snapshot("meeting-01", 2) == published_v2
    assert store.snapshot("meeting-01", 1)["confirmed_by"] == "示例确认人甲"
    assert store.snapshot("meeting-01", 1)["status"] == "withdrawn"
    store.withdraw("meeting-01")
    for revision, published in [(1, published_v1), (2, published_v2)]:
        exported = json.loads(json.dumps(store.snapshot("meeting-01", revision), ensure_ascii=False))
        assert exported["confirmed_by"] == published["confirmed_by"]
        assert exported["published_at"] == published["published_at"]
        assert exported["payload_hash"] == published["payload_hash"]
        assert exported["status"] == "withdrawn"
    assert store.get_published("meeting-01") is None


def test_only_one_concurrent_publisher_can_use_snapshot(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    first = KnowledgeStore(path).import_draft(draft())

    def publish():
        try:
            KnowledgeStore(path).publish("meeting-01", 1, expected_generation=first["generation"], confirmed_by="示例审核人")
            return "published"
        except RuntimeError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: publish(), range(2))) == ["published", "stale"]
    assert KnowledgeStore(path).get_published("meeting-01")["generation"] == 2


def test_concurrent_identical_import_is_one_revision(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: KnowledgeStore(path).import_draft(draft()), range(2)))
    assert results[0] == results[1]
    assert KnowledgeStore(path).list_assets()[0]["latest_revision"] == 1


def test_missing_database_reads_create_nothing_and_environment_selects_directory(tmp_path, monkeypatch):
    data_dir = tmp_path / "missing" / "knowledge"
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    store = KnowledgeStore()
    assert store.path == data_dir / "knowledge.sqlite3"
    assert store.get_published("meeting-01") is None
    assert store.published_revisions() == []
    assert store.list_assets() == []
    with pytest.raises(KeyError):
        store.snapshot("meeting-01")
    normalized = normalize_entry(draft())
    assert "content_hash" in normalized and "payload_hash" in normalized
    assert "revision" not in normalized and "generation" not in normalized
    assert not data_dir.exists()


@pytest.mark.parametrize("knowledge_id", [None, "", "../outside", "bad/id", "_bad", "a" * 81, "中文", 123])
def test_rejects_invalid_knowledge_ids_without_creating_database(tmp_path, knowledge_id):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    with pytest.raises(ValueError, match="knowledge_id"):
        store.import_draft(draft(knowledge_id=knowledge_id))
    with pytest.raises(ValueError, match="knowledge_id"):
        store.get_published(knowledge_id)
    assert not store.path.exists()


@pytest.mark.parametrize("changes", [
    {"title": " "}, {"content": ""}, {"content": "x" * 500_001}, {"sources": []},
    {"sources": [{"name": "示例", "path": "/internal/source.md"}]},
    {"sources": [{"locator": "/internal/source.md"}]},
    {"sources": [{"url": "file:///internal/source.md"}]},
    {"sources": [{"url": "https://user:secret@example.test/source"}]},
    {"sources": [{}]}, {"evidence": {"not_json": object()}}, {"evidence": {"bad": float("nan")}},
])
def test_validation_rejects_empty_content_paths_and_non_json_metadata(tmp_path, changes):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    with pytest.raises(ValueError):
        store.import_draft(draft(**changes))
    assert not store.path.exists()


class MemoryObjects:
    def __init__(self):
        self.bodies = {}
        self.reads = 0
        self.before_read = None

    def put(self, content):
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = f"private-bodies/{digest}.txt"
        self.bodies[key] = content
        return {"object_key": key, "sha256": digest, "byte_length": len(content.encode("utf-8"))}

    def get(self, ref):
        self.reads += 1
        if self.before_read:
            callback, self.before_read = self.before_read, None
            callback()
        return self.bodies[ref["object_key"]]


def test_oss_catalogue_keeps_only_reference_and_returns_full_body_without_private_ref(tmp_path):
    objects = MemoryObjects()
    path = tmp_path / "catalog.sqlite3"
    store = KnowledgeStore(path, objects=objects)
    first = store.import_draft(draft())
    assert store.import_draft(draft()) == first
    with sqlite3.connect(path) as connection:
        stored = json.loads(connection.execute("SELECT record_json FROM knowledge_revisions").fetchone()[0])
    assert "content" not in stored
    assert stored["content_ref"]["sha256"] == first["content_hash"]
    published = store.publish("meeting-01", 1, expected_generation=1, confirmed_by="审核人")
    for record in (first, published, store.snapshot("meeting-01"), store.get_published("meeting-01")):
        assert record["content"] == draft()["content"]
        assert "content_ref" not in record and "object_key" not in record


def test_object_failure_does_not_create_catalogue_and_corruption_is_not_served(tmp_path, monkeypatch):
    objects = MemoryObjects()
    path = tmp_path / "catalog.sqlite3"
    store = KnowledgeStore(path, objects=objects)
    with monkeypatch.context() as patch:
        def failed_put(_):
            raise RuntimeError("storage unavailable")
        patch.setattr(objects, "put", failed_put)
        with pytest.raises(RuntimeError, match="storage unavailable"):
            store.import_draft(draft())
    assert not path.exists()
    store.import_draft(draft())
    store.publish("meeting-01", 1, expected_generation=1, confirmed_by="审核人")
    objects.bodies = {key: "corrupted body" for key in objects.bodies}
    with pytest.raises(RuntimeError, match="hash"):
        store.get_published("meeting-01")
    def invalid_reference(_):
        raise ValueError("invalid object reference")
    monkeypatch.setattr(objects, "get", invalid_reference)
    with pytest.raises(RuntimeError, match="reference"):
        store.get_published("meeting-01")


@pytest.mark.parametrize("corruption", ["hash", "missing_body", "empty_body"])
def test_corrupted_inline_catalogue_never_returns_successful_body(tmp_path, corruption):
    path = tmp_path / "catalog.sqlite3"
    store = KnowledgeStore(path)
    store.import_draft(draft())
    store.publish("meeting-01", 1, expected_generation=1, confirmed_by="审核人")
    with sqlite3.connect(path) as connection:
        record = json.loads(connection.execute("SELECT record_json FROM knowledge_revisions").fetchone()[0])
        if corruption == "hash":
            record["content_hash"] = "0" * 64
        elif corruption == "missing_body":
            record.pop("content")
        else:
            record["content"] = " "
        connection.execute("UPDATE knowledge_revisions SET record_json = ?", (json.dumps(record),))
    with pytest.raises(RuntimeError, match="body"):
        store.get_published("meeting-01")
    with pytest.raises(RuntimeError, match="body"):
        store.snapshot("meeting-01")


def test_withdrawal_during_object_read_blocks_body_and_inflight_publication(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3", objects=MemoryObjects())
    store.import_draft(draft())
    published = store.publish("meeting-01", 1, expected_generation=1, confirmed_by="审核人")
    store.objects.before_read = lambda: store.withdraw("meeting-01")
    assert store.get_published("meeting-01") is None
    fresh = store.snapshot("meeting-01")
    store.objects.before_read = lambda: store.withdraw("meeting-01")
    with pytest.raises(RuntimeError, match="changed"):
        store.publish("meeting-01", 1, expected_generation=fresh["generation"], confirmed_by="审核人")
    assert store.get_published("meeting-01") is None
    assert published["content"] == draft()["content"]


@pytest.mark.parametrize("action", ["draft", "replace", "withdraw", "withdraw_republish"])
def test_public_read_survives_new_draft_but_not_publication_changes(tmp_path, action):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3", objects=MemoryObjects())
    store.import_draft(draft())
    original = store.publish("meeting-01", 1, expected_generation=1, confirmed_by="reviewer")

    def change_during_read():
        if action in {"withdraw", "withdraw_republish"}:
            store.withdraw("meeting-01")
            if action == "withdraw_republish":
                snapshot = store.snapshot("meeting-01", 1)
                store.publish("meeting-01", 1, expected_generation=snapshot["generation"], confirmed_by="reviewer")
        else:
            newer = store.import_draft(draft(content="更新后的内容"))
            if action == "replace":
                store.publish("meeting-01", newer["revision"], expected_generation=newer["generation"], confirmed_by="reviewer")

    store.objects.before_read = change_during_read
    current = store.get_published("meeting-01")
    if action == "draft":
        assert current is not None
        assert current["revision"] == original["revision"] and current["content"] == original["content"]
        assert current["published_at"] == original["published_at"]
    else:
        assert current is None
    assert store.published_count() == len(store.published_catalog()) == (0 if action == "withdraw" else 1)


def test_catalogue_filters_published_metadata_without_loading_any_body(tmp_path):
    objects = MemoryObjects()
    store = KnowledgeStore(tmp_path / "catalog.sqlite3", objects=objects)
    first = store.import_draft(draft(department="运营", scenarios=["复盘", "会议"]))
    store.publish("meeting-01", 1, expected_generation=first["generation"], confirmed_by="审核人")
    second = store.import_draft(draft(department="品牌", scenarios=["推广"]))
    assert second["revision"] == 2 and second["payload_hash"] != first["payload_hash"]
    reads = objects.reads
    expected = [{"knowledge_id": "meeting-01", "revision": 1}]
    assert store.published_revisions(department=" 运营 ", scenario="会议") == expected
    assert store.published_revisions(scenario="复盘") == expected
    assert store.published_revisions(department="品牌") == []
    assert store.published_revisions(department="运营", scenario="推广") == []
    assert store.list_assets()[0]["latest_revision"] == 2
    assert objects.reads == reads
    store.withdraw("meeting-01")
    assert store.published_revisions(department="运营") == []
    assert objects.reads == reads


def test_public_catalog_uses_current_revision_and_only_public_relationship_targets(tmp_path):
    objects = MemoryObjects()
    store = KnowledgeStore(tmp_path / "catalog.sqlite3", objects=objects)
    for key, published in (("meeting-01", True), ("case-01", True), ("private-01", False)):
        record = store.import_draft(draft(knowledge_id=key, department="运营", scenarios=["复盘"],
            evidence={"source_type": "Skill方法", "uploader_position": "不能信任的上传自述",
                      "private_field": "private-value", "related_assets": [
                {"knowledge_id": "case-01", "source_locator": "private-location"},
                {"knowledge_id": "private-01"}, {"knowledge_id": "meeting-01"}]}))
        if published:
            store.publish(key, record["revision"], expected_generation=record["generation"], confirmed_by="reviewer")
    store.import_draft(draft(title="未公开的新标题", kind="case"))
    reads = objects.reads
    catalog = {item["knowledge_id"]: item for item in store.published_catalog()}
    assert set(catalog) == {"meeting-01", "case-01"}
    assert catalog["meeting-01"]["title"] == "会议准备方法"
    assert catalog["meeting-01"]["revision"] == 1
    assert catalog["meeting-01"]["related_knowledge_ids"] == ["case-01"]
    assert catalog["meeting-01"]["scenarios"] == ["复盘"]
    assert catalog["meeting-01"]["uploader_position"] is None
    assert "private" not in json.dumps(catalog) and "content" not in catalog["meeting-01"]
    assert objects.reads == reads
    store.withdraw("case-01")
    assert store.published_catalog()[0]["related_knowledge_ids"] == []
    assert objects.reads == reads


def test_old_inline_payload_keeps_hash_and_revision_after_enabling_oss(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    original = KnowledgeStore(path).import_draft(draft())
    normalized = normalize_entry(draft())
    payload = {key: value for key, value in normalized.items() if key not in {"content_hash", "payload_hash"}}
    assert "department" not in payload and "scenarios" not in payload
    expected_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert normalized["payload_hash"] == expected_hash
    store = KnowledgeStore(path, objects=MemoryObjects())
    assert store.import_draft(draft()) == original
    assert store.list_assets()[0]["latest_revision"] == 1
    assert store.snapshot("meeting-01") == original
    assert store.objects.reads == 0


def test_chunking_upgrade_creates_new_draft_without_changing_published_revision(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    first = store.import_draft(draft(chunking_version="structure-v1"))
    store.publish(first["knowledge_id"], first["revision"], expected_generation=first["generation"], confirmed_by="reviewer")
    upgraded = store.import_draft(draft(chunking_version="structure-v2"))
    assert upgraded["revision"] == 2 and upgraded["status"] == "draft"
    assert upgraded["content_hash"] == first["content_hash"]
    assert upgraded["payload_hash"] != first["payload_hash"]
    assert store.get_published(first["knowledge_id"])["chunking_version"] == "structure-v1"
    assert store.snapshot(first["knowledge_id"], 1)["content"] == upgraded["content"]


@pytest.mark.parametrize("metadata", [
    {"department": None}, {"department": " "}, {"department": "x" * 201},
    {"scenarios": "会议"}, {"scenarios": [""]}, {"scenarios": [None]},
    {"scenarios": ["会议", " 会议 "]}, {"scenarios": ["x" * 201]},
    {"scenarios": [str(index) for index in range(21)]},
])
def test_metadata_validation_never_creates_storage(tmp_path, metadata):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    with pytest.raises(ValueError):
        store.import_draft(draft(**metadata))
    assert not store.path.exists()


def test_database_selection_is_explicit_and_rejects_unsupported_url(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_DATABASE_URL", "postgresql://test.invalid/test")
    monkeypatch.delenv("KNOWLEDGE_BODY_STORAGE", raising=False)
    local = KnowledgeStore(tmp_path / "catalog.sqlite3")
    assert local.database_url is None and local.body_storage == "inline"
    postgres = KnowledgeStore()
    assert postgres.path is None and postgres.body_storage == "oss"
    for url in ("sqlite:///tmp/catalog.sqlite3", "mysql://user:password@host/db", "postgresql://host"):
        with pytest.raises(ValueError, match="PostgreSQL URL") as error:
            KnowledgeStore(database_url=url)
        assert url not in str(error.value)
    with pytest.raises(ValueError, match="choose"):
        KnowledgeStore(tmp_path / "local.sqlite3", database_url="postgresql://host/db")
    monkeypatch.setenv("KNOWLEDGE_BODY_STORAGE", "invalid")
    with pytest.raises(ValueError, match="inline or oss"):
        KnowledgeStore()


def test_local_read_connection_cannot_mutate_catalogue(tmp_path):
    store = KnowledgeStore(tmp_path / "catalog.sqlite3")
    store.initialize()
    with store._connection() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM knowledge_assets")
    assert store.list_assets() == []


def test_postgres_connection_errors_do_not_expose_dsn(monkeypatch):
    def failed_connect(*args, **kwargs):
        raise RuntimeError("postgresql://secret-user:secret-password@host/db")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=failed_connect))
    monkeypatch.setitem(sys.modules, "psycopg.rows", SimpleNamespace(dict_row=object()))
    store = KnowledgeStore(database_url="postgresql://secret-user:secret-password@host/db")
    with pytest.raises(RuntimeError, match="database connection failed") as error:
        store.list_assets()
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
