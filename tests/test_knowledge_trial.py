import hashlib
import json
from types import SimpleNamespace

import pytest

import knowledge_trial as trial
from rag_app.knowledge_index import KnowledgeIndexError


pytestmark = pytest.mark.offline


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_batch(root):
    """Real intake import fixture with one selected body and one review hold."""
    body = "# 部门复盘方法\n\n" + "先明确统计周期、单位和指标口径，再核对异常。\n" * 90 + "\n最后保留完整证据。\n"
    batch = root / "batch"
    metadata = {"title": "部门复盘方法", "kind": "method", "contributor": "测试",
                "sources": [{"name": "method.md", "locator": "正文"}],
                "evidence": {"review_status": "pending"}, "chunking_version": "structure-v2"}
    write_json(batch / "assets" / "selected.json", metadata)
    (batch / "assets" / "selected.md").write_text(body, encoding="utf-8")
    selected = {
        "candidate_id": "source_candidate", "knowledge_id": "source-method",
        "decision": "candidate", "state": "draft_ready", "issues": [],
        "body_file": "assets/selected.md", "metadata_file": "assets/selected.json",
        "content_hash": trial.digest(body),
        "metadata_hash": hashlib.sha256((batch / "assets" / "selected.json").read_bytes()).hexdigest(),
    }
    write_json(batch / "batch.json", {"assets": [selected, {
        **selected, "candidate_id": "needs_review", "decision": "review_required",
    }]})
    return body


def test_prepare_reuses_intake_selection_remaps_identity_and_preserves_complete_body(tmp_path, monkeypatch):
    body = make_batch(tmp_path)
    real_import = trial.import_batch
    calls = []

    def observed_import(*args, **kwargs):
        calls.append(kwargs.get("execute", False))
        return real_import(*args, **kwargs)

    monkeypatch.setattr(trial, "import_batch", observed_import)
    monkeypatch.setattr(trial, "load_env", lambda: pytest.fail("must not read credentials"))
    monkeypatch.setattr(trial, "ensure_database", lambda *_: pytest.fail("prepare must stay local"))
    result = trial.prepare(tmp_path)
    assert result["status"] == "prepared" and result["assets"] == 1 and result["skipped"] == 1
    assert calls == [False]
    manifest = trial.read(tmp_path / "trial.json")
    asset, = manifest["assets"]
    entry = trial.read(tmp_path / asset["entry_file"])
    assert asset["knowledge_id"] != "source-method"
    assert asset["knowledge_id"].startswith("trial_" + manifest["trial_id"][:8] + "_")
    assert manifest["prefix"] == "company-knowledge/trials/" + manifest["trial_id"] + "/"
    assert manifest["formal_published"] is False
    assert entry["content"] == body == (tmp_path / asset["content_file"]).read_text()
    assert entry["evidence"]["review_status"] == "pending"
    assert entry["evidence"]["trial"]["source_knowledge_id"] == "source-method"
    assert entry["evidence"]["trial"]["formal_published"] is False
    assert asset["chunk_ids"] == [row["id"] for row in trial.chunk_document(dict(entry, revision=1))]
    assert len(asset["chunk_ids"]) > 1
    before = (tmp_path / "trial.json").read_bytes()
    assert trial.prepare(tmp_path)["status"] == "already_prepared"
    assert (tmp_path / "trial.json").read_bytes() == before
    assert calls == [False]


def test_extend_preserves_existing_ready_resources_and_adds_only_new_candidate(tmp_path):
    make_batch(tmp_path)
    trial.prepare(tmp_path)
    manifest = trial.read(tmp_path / "trial.json")
    original = manifest["assets"][0]
    original.update(state="ready", preflight_absent=True)
    manifest.update(status="ready", created_object_versions={original["object_key"]: "owned-version-1"})
    write_json(tmp_path / "trial.json", manifest)
    existing_entry = (tmp_path / original["entry_file"]).read_bytes()
    batch = trial.read(tmp_path / "batch" / "batch.json")
    batch["assets"][1]["decision"] = "candidate"
    write_json(tmp_path / "batch" / "batch.json", batch)

    assert trial.prepare(tmp_path, extend=True)["assets"] == 2
    extended = trial.read(tmp_path / "trial.json")
    assert extended["trial_id"] == manifest["trial_id"]
    assert extended["prefix"] == manifest["prefix"]
    assert extended["created_object_versions"] == manifest["created_object_versions"]
    assert extended["assets"][0] == original
    assert (tmp_path / original["entry_file"]).read_bytes() == existing_entry
    added = extended["assets"][1]
    assert added["candidate_id"] == "needs_review" and added["state"] == "prepared"
    assert added["knowledge_id"] != original["knowledge_id"]
    assert added["preflight_absent"] is False
    before_repeat = (tmp_path / "trial.json").read_bytes()
    trial.prepare(tmp_path, extend=True)
    assert (tmp_path / "trial.json").read_bytes() == before_repeat


@pytest.mark.parametrize("change", ["cancelled", "payload_changed"])
def test_extend_rejects_old_candidate_changes_without_writing_even_when_new_candidate_comes_first(tmp_path, change):
    make_batch(tmp_path)
    trial.prepare(tmp_path)
    batch_path = tmp_path / "batch" / "batch.json"
    batch = trial.read(batch_path)
    old, added = batch["assets"]
    added["decision"] = "candidate"
    if change == "cancelled":
        old["decision"] = "review_required"
    else:
        metadata_path = tmp_path / "batch" / old["metadata_file"]
        metadata = trial.read(metadata_path)
        metadata["title"] = "调整后的资产标题"
        write_json(metadata_path, metadata)
        for asset in (old, added):
            asset["metadata_hash"] = hashlib.sha256(metadata_path.read_bytes()).hexdigest()
    batch["assets"] = [added, old]
    write_json(batch_path, batch)
    before_manifest = (tmp_path / "trial.json").read_bytes()
    before_entries = {path.name: path.read_bytes() for path in (tmp_path / "entries").iterdir()}
    with pytest.raises(ValueError):
        trial.prepare(tmp_path, extend=True)
    assert (tmp_path / "trial.json").read_bytes() == before_manifest
    assert {path.name: path.read_bytes() for path in (tmp_path / "entries").iterdir()} == before_entries


@pytest.mark.parametrize("mutation", ["entry", "prefix", "chunk_ids", "object_key", "candidate_id", "cleanup_in_progress"])
def test_changed_plan_is_rejected_before_database_or_cloud(tmp_path, monkeypatch, mutation):
    make_batch(tmp_path)
    trial.prepare(tmp_path)
    manifest = trial.read(tmp_path / "trial.json")
    asset = manifest["assets"][0]
    if mutation == "entry":
        file = tmp_path / asset["entry_file"]
        entry = trial.read(file)
        entry["content"] += "篡改正文"
        write_json(file, entry)
    elif mutation == "prefix":
        manifest["prefix"] = "company-knowledge/bodies/"
    elif mutation == "chunk_ids":
        asset["chunk_ids"] = asset["chunk_ids"][:-1]
    elif mutation == "object_key":
        asset["object_key"] = "company-knowledge/bodies/" + asset["content_hash"] + ".txt"
    elif mutation == "candidate_id":
        asset["candidate_id"] = "different_candidate"
    else:
        manifest["status"] = mutation
    write_json(tmp_path / "trial.json", manifest)
    monkeypatch.setattr(trial, "ensure_database", lambda *_: pytest.fail("no database operation"))
    monkeypatch.setattr(trial, "CachedIndex", lambda *_: pytest.fail("no cloud client"))
    monkeypatch.setattr(trial, "KnowledgeObjects", lambda **_: pytest.fail("no OSS client"))
    with pytest.raises(ValueError):
        trial.load(tmp_path, 1)


@pytest.mark.parametrize("failure", ["fetch_exception", "fetch_error", "object_exception", "object_exists"])
def test_cloud_preflight_failure_never_embeds_imports_publishes_or_writes_objects(tmp_path, monkeypatch, failure):
    make_batch(tmp_path)
    trial.prepare(tmp_path)
    calls = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("cloud write/embedding must not follow failed preflight")

    def fetch(_request):
        calls.append("fetch")
        if failure == "fetch_exception":
            raise PermissionError("private-error-detail")
        return {"code": 403, "message": "private-error-detail"} if failure == "fetch_error" else {"result": []}

    def exists(_key):
        calls.append("object_exists")
        if failure == "object_exception":
            raise PermissionError("private-error-detail")
        return True

    index = SimpleNamespace(_connect=lambda: None, table="company_knowledge", warm=forbidden,
                            client=SimpleNamespace(fetch=fetch), models=SimpleNamespace(FetchRequest=lambda **kw: kw))
    objects = SimpleNamespace(bucket=SimpleNamespace(object_exists=exists, put_object=forbidden))
    monkeypatch.setattr(trial, "ensure_database", lambda *_: {"database_url": "unused-test-runtime"})
    monkeypatch.setattr(trial, "service_for", lambda *_: SimpleNamespace(store=SimpleNamespace(initialize=lambda: None)))
    monkeypatch.setattr(trial, "CachedIndex", lambda *_: index)
    monkeypatch.setattr(trial, "KnowledgeObjects", lambda **_: objects)
    monkeypatch.setattr(trial, "KnowledgeStore", forbidden)
    monkeypatch.setattr(trial, "KnowledgeService", forbidden)
    result = trial.load(tmp_path, 1)
    assert result == {"status": "partial", "ready": 0, "total": 1}
    manifest = trial.read(tmp_path / "trial.json")
    assert manifest["assets"][0]["state"] == "error"
    assert manifest["assets"][0]["preflight_absent"] is False
    assert manifest["created_object_versions"] == {}
    assert "private-error-detail" not in (tmp_path / "trial.json").read_text()
    assert calls == (["fetch"] if failure.startswith("fetch") else ["fetch", "object_exists"])


@pytest.mark.parametrize("url", [
    "postgresql://trial:test@remote.example:55432/knowledge_trial",
    "postgresql://trial:test@127.0.0.1:5432/knowledge_trial",
    "postgresql://trial:test@127.0.0.1:55432/formal",
])
def test_runtime_cannot_redirect_owned_container_to_another_database(tmp_path, monkeypatch, url):
    trial_id = "a" * 32
    name = "rag-trial-" + trial_id[:12]
    write_json(tmp_path / "runtime.json", {"container_name": name, "volume_name": name, "database_url": url})
    calls = []

    def inspect(*args, **kwargs):
        calls.append(args)
        assert args == ("inspect", name)
        return json.dumps([{"Config": {"Labels": {"com.rag.knowledge-trial": trial_id}},
                            "State": {"Running": True},
                            "HostConfig": {"PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}}}])

    monkeypatch.setattr(trial, "docker", inspect)
    with pytest.raises(ValueError):
        trial.ensure_database(tmp_path, {"trial_id": trial_id})
    assert calls == [("inspect", name)]


def test_embedding_warmup_batches_then_reuses_validated_cache(tmp_path, monkeypatch):
    entry = {
        "knowledge_id": "cache-test", "title": "复盘方法", "chunking_version": "structure-v2",
        "content": "# 输入\n\n" + "\n\n".join(
            f"## 步骤 {number}\n\n" + f"核对指标{number}。" * 110 for number in range(12)),
    }
    entry["content_hash"] = trial.digest(entry["content"])
    calls = []

    def embed_batch(batch, timeout):
        assert 1 <= len(batch) <= 10 and timeout == 120
        calls.append(batch)
        return [[1.0] + [0.0] * 1023 for _ in batch]

    monkeypatch.setattr(trial, "embed_batch", embed_batch)
    monkeypatch.setattr(trial.KnowledgeIndex, "_embed", lambda *_: pytest.fail("cache must avoid another model call"))
    index = trial.CachedIndex(tmp_path / "cache")
    index.warm(entry)
    assert len(calls) >= 2
    count = len(calls)
    index.warm(entry)
    assert len(calls) == count
    text = calls[0][0]["page_content"]
    assert index._embed(text) == [1.0] + [0.0] * 1023
    index.cache_path(text).write_text("[1]")
    with pytest.raises(KnowledgeIndexError):
        index._embed(text)
