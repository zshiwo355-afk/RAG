from __future__ import annotations

import json
from pathlib import Path

import pytest

import knowledge


pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def isolated_cli_storage(monkeypatch, tmp_path):
    import socket
    from rag_app import knowledge_service

    monkeypatch.setattr(knowledge_service, "load_env", lambda: None)
    monkeypatch.setenv("KNOWLEDGE_DATABASE_URL", "")
    monkeypatch.setenv("KNOWLEDGE_BODY_STORAGE", "inline")
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(tmp_path / "isolated-store"))
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("Offline CLI tests must not connect to a provider"))


class FakeService:
    def __init__(self):
        self.calls = []

    def import_document(self, entry, execute=False):
        self.calls.append(("import", execute))
        return entry

    def publish(self, knowledge_id, revision=None, confirmed_by=None, execute=False):
        self.calls.append(("publish", execute, knowledge_id, revision, confirmed_by))
        return {"knowledge_id": knowledge_id}

    def index_draft(self, knowledge_id, revision=None, execute=False):
        self.calls.append(("index-draft", execute, knowledge_id, revision))
        return {"knowledge_id": knowledge_id, "published": False}

    def withdraw(self, knowledge_id, execute=False):
        self.calls.append(("withdraw", execute, knowledge_id))
        return {"knowledge_id": knowledge_id}

    def list_assets(self):
        return [{"knowledge_id": "example", "status": "draft"}]

    def export(self, knowledge_id, revision=None):
        return {"knowledge_id": knowledge_id, "revision": revision, "content": "方法正文", "title": "方法"}


def import_args(path: Path):
    return ["import", "--file", str(path), "--id", "example", "--title", "测试方法", "--contributor", "同事"]


def test_import_defaults_to_dry_run_without_file_changes(tmp_path, capsys):
    source = tmp_path / "method.md"
    source.write_text("# 方法\n按步骤处理。", encoding="utf-8")
    service = FakeService()
    assert knowledge.main(import_args(source), service=service) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "dry_run"
    assert service.calls == [("import", False)]
    assert output["result"]["content"] == "# 方法\n按步骤处理。"
    assert output["result"]["sources"] == [{"name": "method.md"}]
    assert list(tmp_path.iterdir()) == [source]


def test_catalogue_init_is_explicit_and_department_scenarios_round_trip(tmp_path, capsys):
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    path = tmp_path / "catalog.sqlite3"
    service = KnowledgeService(store=KnowledgeStore(path))
    assert knowledge.main(["init-db"], service=service) == 0
    assert not path.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert knowledge.main(["init-db", "--execute"], service=service) == 0
    assert path.exists()
    source = tmp_path / "method.md"
    source.write_text("# 方法\n先检查证据。", encoding="utf-8")
    assert knowledge.main(import_args(source) + [
        "--department", "品牌部", "--scenario", "选题", "--scenario", "复盘", "--execute",
    ], service=service) == 0
    saved = service.store.snapshot("example")
    assert saved["department"] == "品牌部" and saved["scenarios"] == ["选题", "复盘"]
    assert knowledge.main(["init-db", "--execute"], service=service) == 0
    assert service.store.snapshot("example") == saved
    assert service.store.published_revisions() == []


def test_metadata_precedence_and_explicit_execute(tmp_path, capsys):
    source = tmp_path / "method.txt"
    source.write_text("方法", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({
        "title": "旧标题", "contributor": "旧作者", "kind": "case",
        "sources": [{"name": "旧来源"}], "evidence": {"verified": False},
    }), encoding="utf-8")
    service = FakeService()
    args = import_args(source) + ["--metadata", str(metadata), "--source-name", "会议", "--source-locator", "2026-09-26 第三节", "--execute"]
    assert knowledge.main(args, service=service) == 0
    entry = json.loads(capsys.readouterr().out)["result"]
    assert service.calls == [("import", True)]
    assert (entry["title"], entry["contributor"], entry["kind"]) == ("测试方法", "同事", "case")
    assert entry["sources"] == [{"name": "会议", "locator": "2026-09-26 第三节"}]
    assert entry["evidence"] == {"verified": False}


@pytest.mark.parametrize("metadata_content", ["[]", "not json", '{"knowledge_id":"other"}', '{"unknown":"secret-token"}'])
def test_invalid_metadata_never_reaches_service(tmp_path, capsys, metadata_content):
    source = tmp_path / "method.md"
    source.write_text("方法", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(metadata_content, encoding="utf-8")
    service = FakeService()
    assert knowledge.main(import_args(source) + ["--metadata", str(metadata)], service=service) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret-token" not in captured.err
    assert service.calls == []


@pytest.mark.parametrize("name,body", [("method.pdf", b"text"), ("method.md", b"\xff"), ("method.txt", b"x" * 500_001)])
def test_invalid_documents_never_reach_service(tmp_path, capsys, name, body):
    source = tmp_path / name
    source.write_bytes(body)
    service = FakeService()
    assert knowledge.main(import_args(source), service=service) == 1
    assert service.calls == []
    assert json.loads(capsys.readouterr().err)["status"] == "error"


@pytest.mark.parametrize("execute", [False, True])
def test_publish_and_withdraw_forward_execution_flag(capsys, execute):
    service = FakeService()
    extra = ["--execute"] if execute else []
    assert knowledge.main(["publish", "--id", "example", "--revision", "2", "--confirmed-by", "审核人"] + extra, service=service) == 0
    assert knowledge.main(["withdraw", "--id", "example"] + extra, service=service) == 0
    assert service.calls == [("publish", execute, "example", 2, "审核人"), ("withdraw", execute, "example")]
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(item["status"] == ("ok" if execute else "dry_run") for item in outputs)


@pytest.mark.parametrize("execute", [False, True])
def test_index_draft_needs_no_publication_confirmation(capsys, execute):
    service = FakeService()
    extra = ["--execute"] if execute else []
    assert knowledge.main(["index-draft", "--id", "example", "--revision", "2"] + extra, service=service) == 0
    assert service.calls == [("index-draft", execute, "example", 2)]
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == ("ok" if execute else "dry_run")
    assert output["result"]["published"] is False


def test_list_and_export_are_machine_readable_and_do_not_overwrite(tmp_path, capsys):
    service = FakeService()
    assert knowledge.main(["list"], service=service) == 0
    assert json.loads(capsys.readouterr().out)["result"][0]["status"] == "draft"
    output = tmp_path / "backup.json"
    args = ["export", "--id", "example", "--revision", "3", "--output", str(output)]
    assert knowledge.main(args, service=service) == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record == service.export("example", revision=3)
    assert json.loads(capsys.readouterr().out)["result"] == record
    output.write_text("keep this", encoding="utf-8")
    assert knowledge.main(args, service=service) == 1
    assert output.read_text(encoding="utf-8") == "keep this"
    assert capsys.readouterr().out == ""


def test_provider_exception_never_prints_secret(capsys):
    class BrokenService:
        def list_assets(self):
            raise RuntimeError("token=secret-token")

    assert knowledge.main(["list"], service=BrokenService()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret-token" not in captured.err
    assert "Traceback" not in captured.err


def test_real_service_dry_runs_do_not_create_or_change_storage(monkeypatch, tmp_path, capsys):
    from rag_app import knowledge_index

    def no_cloud(*args, **kwargs):
        pytest.fail("CLI dry-run must not access the cloud")

    monkeypatch.setattr(knowledge_index.KnowledgeIndex, "_connect", no_cloud)
    monkeypatch.setattr(knowledge_index, "embed_text_query", no_cloud)
    data_dir = tmp_path / "store"
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    source = tmp_path / "method.md"
    source.write_text("# 方法\n核对来源后发布。", encoding="utf-8")
    args = import_args(source) + ["--source-name", "会议记录"]
    assert knowledge.main(args) == 0
    assert json.loads(capsys.readouterr().out)["result"]["will_call_embedding"] is False
    assert knowledge.main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == []
    assert not data_dir.exists()

    assert knowledge.main(args + ["--execute"]) == 0
    capsys.readouterr()
    before = {path.name: path.read_bytes() for path in data_dir.iterdir()}
    assert knowledge.main(["index-draft", "--id", "example"], service=None) == 0
    assert knowledge.main(["publish", "--id", "example", "--confirmed-by", "审核人"]) == 0
    assert knowledge.main(["withdraw", "--id", "example"]) == 0
    assert {path.name: path.read_bytes() for path in data_dir.iterdir()} == before
    assert knowledge.main(["export", "--id", "example"]) == 0
    exported = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert exported["content"] == source.read_text(encoding="utf-8")
    assert exported["sources"] == [{"name": "会议记录"}]
    assert "generation" not in exported


def test_draft_vector_preparation_never_approves_or_publishes(tmp_path):
    from unittest.mock import Mock

    from rag_app.knowledge_index import chunk_document
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    index = Mock()
    index.index_and_verify.return_value = {"retrieval_verified": True}
    service = KnowledgeService(KnowledgeStore(tmp_path / "knowledge.sqlite3"), index)
    entry = {"knowledge_id": "example", "title": "测试方法", "content": "先核对来源。", "sources": [{"name": "样本"}]}
    draft = service.import_document(entry, execute=True)

    assert service.index_draft("example")["will_call_embedding"] is False
    index.index_and_verify.assert_not_called()
    result = service.index_draft("example", execute=True)
    assert result["status"] == "indexed_draft" and result["published"] is False
    index.index_and_verify.assert_called_once_with(draft)
    assert service.store.snapshot("example") == draft
    assert service.get("example") is None and service.search("核对") == []
    index.search.assert_not_called()

    # Publication still requires explicit confirmation and fresh verification.
    with pytest.raises(ValueError):
        service.publish("example", execute=True)
    service.publish("example", confirmed_by="测试审核人", execute=True)
    assert index.index_and_verify.call_count == 2
    published = service.get("example")
    with pytest.raises(ValueError, match="unpublished draft"):
        service.index_draft("example", execute=True)

    service.import_document({**entry, "content": "新方法待审。"}, execute=True)
    before = service.store.snapshot("example")
    service.index_draft("example", revision=2, execute=True)
    assert service.store.snapshot("example") == before
    assert service.get("example") == published
    # Even if the cloud returns staged rows, only the published version is read.
    index.search.return_value = [{**chunk, "score": 0.8} for record in (published, before) for chunk in chunk_document(record)]
    assert [row["revision"] for row in service.search("方法")] == [1]
    service.withdraw("example", execute=True)
    with pytest.raises(ValueError, match="unpublished draft"):
        service.index_draft("example", revision=1, execute=True)
    assert service.search("方法") == []


@pytest.mark.parametrize("change", ["new_revision", "withdraw", "index_failure"])
def test_draft_indexing_failure_or_concurrent_change_never_publishes(tmp_path, change):
    from unittest.mock import Mock

    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    index = Mock()
    service = KnowledgeService(KnowledgeStore(tmp_path / "knowledge.sqlite3"), index)
    entry = {"knowledge_id": "example", "title": "方法", "content": "待审正文。", "sources": [{"name": "样本"}]}
    original = service.import_document(entry, execute=True)
    if change == "new_revision":
        index.index_and_verify.side_effect = lambda _: service.import_document({**entry, "content": "修订后的正文。"}, execute=True)
    elif change == "withdraw":
        index.index_and_verify.side_effect = lambda _: service.withdraw("example", execute=True)
    else:
        index.index_and_verify.side_effect = RuntimeError("index verification failed")
    with pytest.raises(RuntimeError):
        service.index_draft("example", execute=True)
    assert service.store.published_revisions() == []
    assert service.get("example") is None and service.search("方法") == []
    assert service.store.snapshot("example", 1)["content"] == original["content"]


def test_metadata_can_supply_title_and_contributor(tmp_path, capsys):
    source = tmp_path / "method.txt"
    source.write_text("方法", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"title": "文档", "contributor": "同事"}), encoding="utf-8")
    assert knowledge.main(["import", "--file", str(source), "--id", "example", "--metadata", str(metadata)], service=FakeService()) == 0
    entry = json.loads(capsys.readouterr().out)["result"]
    assert entry["title"] == "文档"
    assert entry["contributor"] == "同事"


@pytest.fixture
def batch_publication(tmp_path):
    import zipfile
    from unittest.mock import Mock

    from rag_app import knowledge_intake as intake
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import normalize_entry

    class Store:
        database_url = "postgresql://unused.invalid/isolated_test"
        body_storage = "oss"

        def __init__(self):
            self.records = {}
            self.imports = 0

        def import_draft(self, entry, *, allow_new_revision=True):
            assert allow_new_revision is False
            self.imports += 1
            record = normalize_entry(entry)
            record.update(revision=1, generation=1, status="draft")
            self.records[record["knowledge_id"]] = record
            return dict(record)

        def snapshot(self, knowledge_id, revision=None):
            record = self.records[knowledge_id]
            assert revision is None or revision == record["revision"]
            return dict(record)

        def publish(self, knowledge_id, revision, *, expected_generation, confirmed_by):
            record = self.records[knowledge_id]
            if record["generation"] != expected_generation:
                raise RuntimeError("concurrent change")
            record.update(status="published", generation=expected_generation + 1, confirmed_by=confirmed_by)
            return dict(record)

    package = tmp_path / "source.zip"
    reviews = []
    with zipfile.ZipFile(package, "w") as archive:
        for number in range(3):
            name = f"知识正文/KA-{number}.md"
            text = f"# 方法{number}\n先核对来源，再记录结果。"
            archive.writestr(name, text)
            if number < 2:
                reviews.append({"source_package": package.name, "source_path": name,
                                "source_sha256": intake.sha(text), "decision": "candidate", "reasons": ["有明确检查点"],
                                "evidence_anchors": [{"quote": "先核对来源", "location": "正文"}]})
    output = tmp_path / "batch"
    intake.process_batch([{"path": package, "source_id": "test-source", "package": package.name,
                           "sha256": intake.file_hash(package), "contributor": "测试"}], output, reviews=reviews)
    index = Mock()
    index.index_and_verify.return_value = {"retrieval_verified": True, "table": "test_table", "data_source": "test_source",
                                          "chunk_count": 1, "verified_chunk_ids": ["test_chunk"], "content": "DO_NOT_SAVE"}
    service = KnowledgeService(store=Store(), index=index)
    return output, service


def test_publish_batch_requires_receipt_and_resumes_without_republishing(batch_publication, capsys):
    output, service = batch_publication
    args = ["publish-batch", "--batch", str(output), "--confirmed-by", "审核人"]
    assert knowledge.main(args, service=service) == 1
    assert knowledge.main(["import-batch", "--batch", str(output)], service=service) == 0
    assert not (output / "import.json").exists() and not service.store.records
    assert knowledge.main(["import-batch", "--batch", str(output), "--execute"], service=service) == 0
    imported = json.loads((output / "import.json").read_text())
    assert len(imported["results"]) == 2  # The third, unreviewed asset stays out.
    assert all(set(row) == {"candidate_id", *knowledge.PINS, "generation", "status"} for row in imported["results"])
    assert knowledge.main(args, service=service) == 0
    assert not (output / "publication.json").exists()
    service.index.index_and_verify.assert_not_called()
    service.index.index_and_verify.side_effect = [{"retrieval_verified": True}, RuntimeError("token=DO_NOT_PRINT")]
    assert knowledge.main(args + ["--execute"], service=service) == 1
    assert len(json.loads((output / "publication.json").read_text())["results"]) == 1
    assert "DO_NOT_PRINT" not in capsys.readouterr().err
    service.index.index_and_verify.side_effect = None
    assert knowledge.main(args + ["--execute"], service=service) == 0
    assert service.index.index_and_verify.call_count == 3  # Only the unfinished item retried.
    assert knowledge.main(args + ["--execute"], service=service) == 0
    assert knowledge.main(["import-batch", "--batch", str(output), "--execute"], service=service) == 0
    assert service.index.index_and_verify.call_count == 3 and service.store.imports == 2
    receipt_text = (output / "publication.json").read_text()
    receipt = json.loads(receipt_text)
    assert len(receipt["results"]) == 2
    assert all(row["verification_status"] == "verified" for row in receipt["results"])
    assert "DO_NOT_SAVE" not in receipt_text
    # A crash after catalogue publication but before saving the local receipt
    # must skip the model and explicitly disclose the missing verification.
    (output / "publication.json").unlink()
    assert knowledge.main(args + ["--execute"], service=service) == 0
    assert service.index.index_and_verify.call_count == 3
    assert all(row["verification_status"] == "recovered_without_verification_receipt"
               for row in json.loads((output / "publication.json").read_text())["results"])


@pytest.mark.parametrize("change", ["new_revision", "withdraw", "payload", "body", "review", "foreign", "sqlite", "inline"])
def test_publish_batch_rejects_changed_or_unapproved_targets_and_local_storage(batch_publication, change):
    output, service = batch_publication
    assert knowledge.main(["import-batch", "--batch", str(output), "--execute"], service=service) == 0
    record = next(iter(service.store.records.values()))
    if change == "new_revision":
        record.update(revision=2, generation=2)
    elif change == "withdraw":
        record.update(status="withdrawn", generation=2)
    elif change == "payload":
        record["payload_hash"] = "changed"
    elif change in {"body", "review"}:
        path = output / "batch.json"
        batch = json.loads(path.read_text())
        if change == "body":
            (output / batch["assets"][0]["body_file"]).write_text("changed", encoding="utf-8")
        else:
            batch["assets"][0]["decision"] = "repair"
            path.write_text(json.dumps(batch), encoding="utf-8")
    elif change == "foreign":
        path = output / "import.json"
        receipt = json.loads(path.read_text())
        receipt["results"][0]["knowledge_id"] = "someone_else"
        path.write_text(json.dumps(receipt), encoding="utf-8")
    elif change == "sqlite":
        service.store.database_url = None
    else:
        service.store.body_storage = "inline"
    assert knowledge.main(["publish-batch", "--batch", str(output), "--confirmed-by", "审核人", "--execute"], service=service) == 1
    service.index.index_and_verify.assert_not_called()
    assert not (output / "publication.json").exists()


def test_publish_batch_pins_the_snapshot_inside_service_call(batch_publication, monkeypatch):
    output, service = batch_publication
    assert knowledge.main(["import-batch", "--batch", str(output), "--execute"], service=service) == 0
    original = service.publish
    store = service.store

    def concurrent_change(knowledge_id, **kwargs):
        store.records[knowledge_id]["generation"] += 1
        return original(knowledge_id, **kwargs)

    monkeypatch.setattr(service, "publish", concurrent_change)
    assert knowledge.main(["publish-batch", "--batch", str(output), "--confirmed-by", "审核人", "--execute"], service=service) == 1
    service.index.index_and_verify.assert_not_called()
    assert service.store is store
    assert json.loads((output / "publication.json").read_text())["results"] == []


def test_batch_import_collision_preserves_published_sqlite_asset(batch_publication, tmp_path):
    import sqlite3
    from unittest.mock import Mock

    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    output, _ = batch_publication
    _, targets, _ = knowledge._batch_plan(output)
    candidate = next(iter(targets.values()))
    path = tmp_path / "isolated-collision.sqlite3"
    store = KnowledgeStore(path)
    assert store.database_url is None and store.body_storage == "inline"
    index = Mock()
    service = KnowledgeService(store=store, index=index)
    original = service.import_document({**candidate, "content": "原有已发布正文"}, execute=True)
    store.publish(original["knowledge_id"], 1, expected_generation=1, confirmed_by="原审核人")
    before = store.list_assets()
    published = store.get_published(original["knowledge_id"])

    assert knowledge.main(["import-batch", "--batch", str(output), "--execute"], service=service) == 1
    assert store.list_assets() == before  # Publication pointer and generation unchanged.
    assert store.get_published(original["knowledge_id"]) == published
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_revisions").fetchone()[0] == 1
    reused = service.import_document(original, execute=True, allow_new_revision=False)
    assert reused["revision"] == 1 and store.list_assets() == before
    # Explicit/manual imports retain the existing version-update behavior.
    assert service.import_document(candidate, execute=True)["revision"] == 2
    assert store.get_published(original["knowledge_id"])["revision"] == 1
    index.index_and_verify.assert_not_called()
