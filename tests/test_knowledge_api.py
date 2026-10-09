from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_app import config, rerank_service
from rag_app.knowledge_api import get_knowledge_service, router
from rag_app.knowledge_index import chunk_document
from rag_app.knowledge_service import KnowledgeService
from rag_app.knowledge_store import AutomaticPublicationConflict, KnowledgeStore


pytestmark = pytest.mark.offline


@pytest.mark.parametrize("configured_key", [None, "legacy-key-still-in-env"])
def test_knowledge_reads_need_no_api_key(service, monkeypatch, configured_key):
    if configured_key is None:
        monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("KNOWLEDGE_API_KEY", configured_key)
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    with TestClient(app) as client:
        search = client.post("/api/knowledge/search", json={"query": "复盘"})
        detail = client.get("/api/knowledge/decision-review?revision=1")
        assert search.status_code == detail.status_code == 200
        assert search.json()["results"][0]["content"] == detail.json()["knowledge"]["content"]
        assert "answer" not in search.json()


def entry(content="先确认问题，再收集证据，最后比较方案。"):
    return {
        "knowledge_id": "decision-review", "title": "决策复盘方法", "content": content,
        "contributor": "测试贡献人", "kind": "method",
        "sources": [{"name": "模拟访谈.md", "locator": "第二节", "kind": "file"}],
        "evidence": {"adoption": "测试样本，不是公司真实经验"},
    }


class FakeIndex:
    def __init__(self):
        self.rows = []
        self.search_calls = []
        self.fail = False
        self.on_publish = None
        self.on_search = None

    def index_and_verify(self, record, *, require_keyword=False):
        if self.fail:
            raise RuntimeError("模拟新版本写后缺块")
        self.rows.extend({**chunk, "score": 0.8} for chunk in chunk_document(record))
        if self.on_publish:
            self.on_publish()
        return {"verified_chunks": len(chunk_document(record)), "keyword_retrieval_verified": require_keyword}

    def search(self, query, allowed_versions, top_k):
        self.search_calls.append((query, deepcopy(allowed_versions), top_k))
        if self.on_search:
            self.on_search()
        # Deliberately return old versions as well; the service must recheck.
        return deepcopy(sorted(self.rows, key=lambda row: -float(row["score"])))


@pytest.fixture
def service(tmp_path):
    return KnowledgeService(KnowledgeStore(tmp_path / "knowledge.sqlite3"), FakeIndex())


def test_automatic_publication_atomically_completes_and_recovers_without_republishing(service):
    draft = service.import_document(entry(), execute=True)
    with service.store._connection(write=True) as connection:
        connection.execute("CREATE TABLE automatic_outcomes (id TEXT PRIMARY KEY, revision INTEGER)")
    calls = []

    def guard(connection):
        calls.append("guard")
        assert connection.execute("SELECT COUNT(*) FROM knowledge_assets").fetchone()[0] == 1

    def complete(connection, published, verification):
        assert verification["keyword_retrieval_verified"] is True
        assert connection.execute("SELECT published_revision FROM knowledge_assets").fetchone()[0] == 1
        connection.execute("INSERT INTO automatic_outcomes VALUES ('job-a', ?) ON CONFLICT(id) DO NOTHING", (published["revision"],))
        calls.append("complete")

    first = service.publish_automatic("decision-review", 1, expected_generation=draft["generation"],
                                      confirmed_by="rule:v1", transaction_guard=guard, transaction_complete=complete)
    retried = service.publish_automatic("decision-review", 1, expected_generation=draft["generation"],
                                        confirmed_by="rule:v2", transaction_guard=guard, transaction_complete=complete)
    assert first == retried
    assert retried["knowledge"]["confirmed_by"] == "rule:v1"
    assert service.store.snapshot("decision-review")["generation"] == draft["generation"] + 1
    assert calls == ["guard", "complete", "guard", "complete"]
    with service.store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM automatic_outcomes").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["lease", "completion"])
def test_automatic_publication_callback_failure_rolls_back_pointer_and_processing_writes(service, failure):
    first = service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="old reviewer", execute=True)
    draft = service.import_document(entry("第二版正文"), execute=True)
    previous = service.store.get_published("decision-review")
    with service.store._connection(write=True) as connection:
        connection.execute("CREATE TABLE automatic_outcomes (id TEXT PRIMARY KEY)")

    def guard(connection):
        if failure == "lease":
            raise RuntimeError("processing lease expired")

    def complete(connection, published, verification):
        connection.execute("INSERT INTO automatic_outcomes VALUES ('job-a')")
        raise RuntimeError("processing completion failed")

    with pytest.raises(RuntimeError, match="processing"):
        service.publish_automatic("decision-review", 2, expected_generation=draft["generation"], confirmed_by="rule:v1",
                                  transaction_guard=guard, transaction_complete=complete)
    assert service.store.get_published("decision-review") == previous
    assert service.store.snapshot("decision-review") == draft
    with service.store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM automatic_outcomes").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["withdraw", "new_revision"])
def test_automatic_publication_rechecks_version_after_indexing(service, operation):
    draft = service.import_document(entry(), execute=True)
    service.index.on_publish = (lambda: service.withdraw("decision-review", execute=True)) if operation == "withdraw" else (
        lambda: service.import_document(entry("更新草稿"), execute=True))
    with pytest.raises(AutomaticPublicationConflict, match="changed|latest"):
        service.publish_automatic("decision-review", 1, expected_generation=draft["generation"], confirmed_by="rule:v1")
    assert service.store.get_published("decision-review") is None


def test_automatic_publication_never_rolls_back_but_manual_publication_still_can(service):
    first = service.import_document(entry(), execute=True)
    service.publish_automatic("decision-review", 1, expected_generation=first["generation"], confirmed_by="rule:v1")
    second = service.import_document(entry("第二版正文"), execute=True)
    service.publish_automatic("decision-review", 2, expected_generation=second["generation"], confirmed_by="rule:v1")
    current = service.store.snapshot("decision-review")
    with pytest.raises(AutomaticPublicationConflict, match="changed"):
        service.publish_automatic("decision-review", 1, expected_generation=current["generation"], confirmed_by="rule:v1")
    assert service.store.get_published("decision-review")["revision"] == 2
    service.publish("decision-review", 1, confirmed_by="explicit rollback reviewer", execute=True)
    assert service.store.get_published("decision-review")["revision"] == 1


def test_automatic_published_retry_cannot_complete_after_a_new_candidate_exists(service):
    first = service.import_document(entry(), execute=True)
    service.publish_automatic("decision-review", 1, expected_generation=first["generation"], confirmed_by="rule:v1")
    service.import_document(entry("第二版草稿"), execute=True)
    completed = []
    with pytest.raises(AutomaticPublicationConflict, match="changed|latest"):
        service.publish_automatic("decision-review", 1, expected_generation=first["generation"], confirmed_by="rule:v1",
                                  transaction_complete=lambda *args: completed.append(True))
    assert completed == []
    assert service.store.get_published("decision-review")["revision"] == 1


def test_automatic_retry_after_withdrawal_and_manual_republication_is_a_conflict(service):
    draft = service.import_document(entry(), execute=True)
    service.publish_automatic("decision-review", 1, expected_generation=draft["generation"], confirmed_by="rule:v1")
    service.withdraw("decision-review", execute=True)
    service.publish("decision-review", 1, confirmed_by="explicit reviewer", execute=True)
    current = service.store.get_published("decision-review")
    with pytest.raises(AutomaticPublicationConflict):
        service.publish_automatic("decision-review", 1, expected_generation=draft["generation"], confirmed_by="rule:v1")
    assert service.store.get_published("decision-review") == current


def test_default_dry_run_has_no_persistence_or_index_side_effects(service):
    preview = service.import_document(entry())
    assert preview["status"] == "dry_run"
    assert preview["will_write_index"] is False
    assert service.list_assets() == []
    assert service.search("复盘") == []
    assert service.index.search_calls == []

    draft = service.import_document(entry(), execute=True)
    assert draft["revision"] == 1
    assert service.get("decision-review") is None
    assert service.publish("decision-review", confirmed_by="测试确认人")["status"] == "dry_run"
    assert service.index.rows == []
    assert service.get("decision-review") is None


def test_fractional_tied_route_rank_keeps_equal_rrf_contribution(service):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    service.index.rows[0]["route_ranks"] = {"text_keyword": 50.5}
    result = service.search("复盘")[0]
    assert result["retrieval_routes"] == ["text_keyword"]
    assert result["rrf_score"] == pytest.approx(1 / 110.5)
    assert result["rerank_mode"] == "fallback_rrf"


def test_parent_rank_ties_preserve_the_best_chunk_for_reranking(service, monkeypatch):
    service.import_document(entry("背景" * 450 + "关键步骤" * 150), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    assert len(service.index.rows) == 2
    for number, row in enumerate(service.index.rows):
        row.update(score=number, route_ranks={"text_dense": 1.5})
    best = service.index.rows[1]["source_text"]

    def rerank(query, documents):
        assert documents[0].startswith("标题：决策复盘方法\n\n" + best)
        return [0.9]

    monkeypatch.setattr(rerank_service, "call_rerank_model", rerank)
    assert service.search("关键步骤")[0]["snippet"] == best


def test_new_draft_and_failed_publish_preserve_old_version(service):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    service.import_document(entry("新方法：核对依据，再做判断。"), execute=True)
    assert service.get("decision-review")["revision"] == 1
    assert service.export("decision-review")["revision"] == 2

    service.index.fail = True
    with pytest.raises(RuntimeError):
        service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    assert service.get("decision-review")["revision"] == 1
    service.index.fail = False
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    found = service.search("如何判断")
    assert len(found) == 1
    assert found[0]["revision"] == 2
    assert found[0]["snippet"] == "新方法：核对依据，再做判断。"
    assert service.index.search_calls[-1][1] == [{"knowledge_id": "decision-review", "revision": 2}]


def test_cloud_residue_and_forged_rows_are_not_public(service):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    valid_row = deepcopy(service.index.rows[0])
    service.index.rows = [
        {**valid_row, "knowledge_id": "wine-product", "source_text": "不应出现的白酒资料"},
        {**valid_row, "source_text": "被修改的远端正文"},
        {**valid_row, "content_hash": "incorrect"},
        {**valid_row, "revision": 99},
        {**valid_row, "score": float("nan")},
    ]
    assert service.search("复盘") == []
    service.index.rows.append(valid_row)
    assert len(service.search("复盘")) == 1
    service.withdraw("decision-review", execute=True)
    assert service.index.rows
    calls = len(service.index.search_calls)
    assert service.get("decision-review") is None
    assert service.search("复盘") == []
    assert len(service.index.search_calls) == calls


def test_withdraw_during_publish_cannot_resurrect_content(service):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    service.import_document(entry("第二版内容"), execute=True)
    service.index.on_publish = lambda: service.withdraw("decision-review", execute=True)
    with pytest.raises((RuntimeError, ValueError)):
        service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    assert service.get("decision-review") is None
    assert service.search("复盘") == []


def test_withdraw_during_search_hides_inflight_rows(service):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    service.index.on_search = lambda: service.withdraw("decision-review", execute=True)
    assert service.search("复盘") == []


@pytest.mark.parametrize("hit_count", [1, 2])
def test_any_chunk_returns_one_complete_asset_and_lightweight_can_omit_body(service, hit_count):
    document = entry("第一步：保留失败原因。\n" * 200 + "最后一步：核对例外。")
    service.import_document(document, execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    assert len(service.index.rows) > 2
    service.index.rows = service.index.rows[1:1 + hit_count]
    service.index.rows[-1]["score"] = 0.9

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/api/knowledge/search", json={"query": "失败原因"}).json()
        assert response["result_count"] == 1
        result = response["results"][0]
        assert result["content"] == document["content"]
        assert result["content_hash"] == service.export("decision-review")["content_hash"]
        assert result["snippet"] == service.index.rows[-1]["source_text"]
        assert result["snippet"] != result["content"]
        assert result["chunk_id"] == service.index.rows[-1]["chunk_id"]
        assert result["read_url"] == "/api/knowledge/decision-review?revision=1"
        assert "generation" not in result
        light = client.post("/api/knowledge/search", json={"query": "失败原因", "include_content": False}).json()["results"][0]
        assert "content" not in light
        assert light == {key: value for key, value in result.items() if key != "content"}


def test_version_bound_read_never_substitutes_a_new_revision(service):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    old_link = service.search("复盘")[0]["read_url"]
    service.import_document(entry("新版已修正步骤。"), execute=True)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    with TestClient(app) as client:
        assert client.get(old_link).json()["knowledge"]["revision"] == 1
        assert client.get("/api/knowledge/decision-review?revision=2").status_code == 404
        service.publish("decision-review", confirmed_by="测试确认人", execute=True)
        assert client.get(old_link).status_code == 404
        current = client.get("/api/knowledge/decision-review").json()["knowledge"]
        assert current["revision"] == 2
        assert current["content"] == "新版已修正步骤。"
        result = service.search("复盘")[0]
        assert result["revision"] == 2 and result["content"] == current["content"]
        service.withdraw("decision-review", execute=True)
        assert client.get(result["read_url"]).status_code == 404
        assert client.get("/api/knowledge/decision-review?revision=0").status_code == 422


@pytest.mark.parametrize("operation", ["withdraw", "new_revision"])
@pytest.mark.parametrize("endpoint", ["get", "search"])
def test_publication_change_during_body_read_cannot_expose_cached_content(service, monkeypatch, operation, endpoint):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    original_read = service.store.get_published

    def read_then_change(knowledge_id):
        record = original_read(knowledge_id)
        if operation == "withdraw":
            service.withdraw(knowledge_id, execute=True)
        else:
            service.import_document(entry("读取期间发布的新正文。"), execute=True)
            service.publish(knowledge_id, confirmed_by="测试确认人", execute=True)
        return record

    monkeypatch.setattr(service.store, "get_published", read_then_change)
    if endpoint == "get":
        assert service.get("decision-review", revision=1) is None
    else:
        assert service.search("复盘") == []


def test_department_and_scenario_filters_scope_index_and_final_recheck(service, monkeypatch):
    for knowledge_id, department, scenarios in [
        ("decision-review", "运营", ["复盘", "日常汇报"]),
        ("brand-review", "品牌", ["复盘"]),
        ("ops-budget", "运营", ["预算"]),
    ]:
        service.import_document({**entry(), "knowledge_id": knowledge_id,
                                 "department": department, "scenarios": scenarios}, execute=True)
        service.publish(knowledge_id, confirmed_by="测试确认人", execute=True)
    catalogue_calls = []
    original_catalogue = service.store.published_revisions

    def catalogue(**filters):
        catalogue_calls.append(filters)
        return original_catalogue(**filters)

    monkeypatch.setattr(service.store, "published_revisions", catalogue)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/api/knowledge/search", json={
            "query": "复盘", "department": " 运营 ", "scenario": "复盘", "top_k": 1,
        })
        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["knowledge_id"] == "decision-review"
        assert result["department"] == "运营" and result["scenarios"] == ["复盘", "日常汇报"]
        assert service.index.search_calls[-1][1] == [{"knowledge_id": "decision-review", "revision": 1}]
        assert catalogue_calls == [{"department": "运营", "scenario": "复盘"}] * 3
        assert len(service.search("复盘", department="运营")) == 2
        assert len(service.search("复盘", scenario="复盘")) == 2
        count = len(service.index.search_calls)
        assert service.search("复盘", department="未登记部门") == []
        assert len(service.index.search_calls) == count


def test_real_http_routes_no_auth_and_no_knowledge_mutations(service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    with TestClient(app) as client:
        assert client.post("/api/knowledge/search", json={"query": "复盘"}).json()["results"] == []
        service.import_document(entry(), execute=True)
        assert client.get("/api/knowledge/decision-review").status_code == 404
        service.publish("decision-review", confirmed_by="测试确认人", execute=True)
        result = client.post("/api/knowledge/search", json={"query": "复盘"})
        assert result.status_code == 200
        assert result.json()["results"][0]["knowledge_id"] == "decision-review"
        detail = client.get("/api/knowledge/decision-review")
        assert detail.json()["knowledge"]["content"] == entry()["content"]
        assert "generation" not in detail.json()["knowledge"]
        for payload in [
            {"query": "复盘", "table_name": "text_docs"},
            {"query": "复盘", "top_k": 0},
            {"query": "复盘", "top_k": True},
            {"query": "x" * 4001},
            {"query": "复盘", "include_content": "false"},
            {"query": "复盘", "department": "x" * 201},
            {"query": "复盘", "scenario": ["复盘"]},
        ]:
            assert client.post("/api/knowledge/search", json=payload).status_code == 422
        assert client.post("/api/knowledge/search", json={"query": "   "}).status_code == 400
        assert client.post("/api/knowledge/search", json={"query": "复盘", "department": "   "}).status_code == 400
        assert client.post("/api/knowledge/publish", json={}).status_code in (404, 405)
        assert client.delete("/api/knowledge/decision-review").status_code == 405
        service.withdraw("decision-review", execute=True)
        assert client.get("/api/knowledge/decision-review").status_code == 404


def test_cloud_error_is_explicit_and_sanitized(service, monkeypatch):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)

    def fail(*args):
        raise RuntimeError("credential=DO_NOT_EXPOSE internal-source-content")

    monkeypatch.setattr(service.index, "search", fail)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/api/knowledge/search", json={"query": "复盘"})
    assert response.status_code == 503
    assert "DO_NOT_EXPOSE" not in response.text
    assert "internal-source-content" not in response.text


def test_main_app_registers_knowledge_without_changing_wine_search(service, monkeypatch):
    monkeypatch.setattr(config, "load_env", lambda *args: None)
    path = Path(__file__).resolve().parents[1] / "rag_api.py"
    spec = importlib.util.spec_from_file_location("rag_api_real_http_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.dependency_overrides[get_knowledge_service] = lambda: service

    class ProductService:
        def __init__(self, **kwargs):
            pass

        def retrieve(self, query, top_k):
            return {"results": [{"product_id": "wine-1", "best_text": "原白酒查询"}]}

    monkeypatch.setattr(module, "RetrievalService", ProductService)
    monkeypatch.setattr(module, "rerank_candidates", lambda rows_query, rows, top_n: rows)
    with TestClient(module.app) as client:
        wine = client.post("/api/rag/search", json={"query": "白酒"})
        knowledge = client.post("/api/knowledge/search", json={"query": "方法"})
        assert wine.status_code == 200
        assert wine.json()["products"][0]["product_id"] == "wine-1"
        assert knowledge.status_code == 200
        assert knowledge.json()["results"] == []
        assert "products" not in knowledge.json()


def test_dependency_import_order_uses_installed_runtime():
    root = Path(__file__).resolve().parents[1]
    code = """
import sys
sys.path[:0] = ['src', 'scripts']
from rag_app import config
import build_documents, embed_documents, embed_image_text_docs
import push_text_docs_to_opensearch, push_image_vectors_to_opensearch
from fastapi import FastAPI
from rag_app.knowledge_api import router
app = FastAPI()
app.include_router(router)
assert any(route.path == '/api/knowledge/search' for route in app.routes)
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_asset_rrf_merges_routes_once_and_reranks_keyword_only_hits(service, monkeypatch):
    for knowledge_id, content in [
        ("dense", "仅向量命中的方法。\n" * 400),
        ("keyword", "精确关键词命中的完整案例。"),
        ("both", "两路共同命中的规则。\n" * 300),
    ]:
        service.import_document({**entry(content), "knowledge_id": knowledge_id, "title": knowledge_id}, execute=True)
        service.publish(knowledge_id, confirmed_by="测试确认人", execute=True)
    dense_rows = [row for row in service.index.rows if row["knowledge_id"] == "dense"]
    assert len(dense_rows) > 3
    for rank, row in enumerate(dense_rows, start=1):
        row["route_ranks"] = {"text_dense": rank}
    for row in service.index.rows:
        if row["knowledge_id"] == "keyword":
            row.update(route_ranks={"text_keyword": 1}, score=1000)
    both_rows = [row for row in service.index.rows if row["knowledge_id"] == "both"]
    assert len(both_rows) > 1
    for number, row in enumerate(both_rows):
        row.update(route_ranks={"text_dense": 20} if number == 0 else {"text_keyword": number + 1}, score=0.01)
    service.index.rows.extend(deepcopy(dense_rows))
    model_inputs = []

    def rerank(query, documents):
        assert query == "精确关键词"
        assert [text.splitlines()[0] for text in documents] == ["标题：both", "标题：dense", "标题：keyword"]
        assert all(len(text) <= 1800 for text in documents)
        model_inputs.extend(documents)
        return [0.1, 0.2, 0.9]

    monkeypatch.setattr(rerank_service, "call_rerank_model", rerank)
    results = service.search(" 精确关键词 ", top_k=3)
    assert [item["knowledge_id"] for item in results] == ["keyword", "dense", "both"]
    assert results[0]["retrieval_routes"] == ["text_keyword"]
    assert results[0]["content"] == "精确关键词命中的完整案例。"
    assert results[1]["rrf_score"] == pytest.approx(1 / 61)
    assert results[2]["rrf_score"] == pytest.approx(1 / 80 + 1 / 62)
    assert results[2]["retrieval_routes"] == ["text_dense", "text_keyword"]
    assert all(item["rerank_mode"] == "model" and item["score"] == item["rerank_score"] for item in results)
    assert len(model_inputs[1].split("\n\n")) <= 4
    assert all(not any(key.startswith("_") for key in item) for item in results)


@pytest.mark.parametrize("scores", [[], [float("nan"), 0.2], [float("inf"), 0.2], [None, 0.2], [True, 0.2], "error"])
def test_invalid_or_failed_rerank_falls_back_to_asset_rrf_without_error_leak(service, monkeypatch, caplog, scores):
    for knowledge_id in ["first", "second"]:
        service.import_document({**entry(), "knowledge_id": knowledge_id}, execute=True)
        service.publish(knowledge_id, confirmed_by="测试确认人", execute=True)
    service.index.rows[0].update(score=0.01, route_ranks={"text_keyword": 1, "text_dense": 2})
    service.index.rows[1].update(score=100, route_ranks={"text_dense": 1})

    def rerank(*args):
        if scores == "error":
            raise RuntimeError("credential=DO_NOT_EXPOSE internal-source-content")
        return scores

    monkeypatch.setattr(rerank_service, "call_rerank_model", rerank)
    results = service.search("方法")
    assert [item["knowledge_id"] for item in results] == ["first", "second"]
    assert all(item["rerank_mode"] == "fallback_rrf" and item["rerank_score"] is None
               and item["score"] == item["rrf_score"] for item in results)
    assert "using RRF" in caplog.text
    assert "DO_NOT_EXPOSE" not in caplog.text and "internal-source-content" not in caplog.text


def test_invalid_or_withdrawn_hits_never_reach_reranker(service, monkeypatch):
    for knowledge_id in ["valid", "withdrawn", "draft"]:
        service.import_document({**entry(), "knowledge_id": knowledge_id, "title": knowledge_id}, execute=True)
        if knowledge_id != "draft":
            service.publish(knowledge_id, confirmed_by="测试确认人", execute=True)
    valid = deepcopy(service.index.rows[0])
    service.index.rows.extend([
        {**valid, "knowledge_id": "draft", "source_text": "unpublished private text"},
        {**valid, "source_text": "forged source text"},
        {**valid, "content_hash": "forged hash"},
        {**valid, "chunk_id": "forged chunk"},
        {**valid, "revision": 1.5},
    ])
    service.index.on_search = lambda: service.withdraw("withdrawn", execute=True)
    documents_seen = []

    def rerank(query, documents):
        documents_seen.extend(documents)
        return [0.9] * len(documents)

    monkeypatch.setattr(rerank_service, "call_rerank_model", rerank)
    assert [row["knowledge_id"] for row in service.search("方法")] == ["valid"]
    assert len(documents_seen) == 1
    assert documents_seen[0] == f"标题：valid\n\n{entry()['content']}"


@pytest.mark.parametrize("operation", ["withdraw", "new_revision"])
def test_publication_change_during_rerank_hides_stale_asset(service, monkeypatch, operation):
    service.import_document(entry(), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)

    def rerank(query, documents):
        if operation == "withdraw":
            service.withdraw("decision-review", execute=True)
        else:
            service.import_document(entry("排序期间新发布的正文。"), execute=True)
            service.publish("decision-review", confirmed_by="测试确认人", execute=True)
        return [0.95] * len(documents)

    monkeypatch.setattr(rerank_service, "call_rerank_model", rerank)
    assert service.search("方法") == []


def test_rerank_is_bounded_by_assets_not_chunk_count(service, monkeypatch):
    for number in range(55):
        knowledge_id = f"asset-{number:02d}"
        service.import_document({**entry(), "knowledge_id": knowledge_id}, execute=True)
        service.publish(knowledge_id, confirmed_by="测试确认人", execute=True)
    calls = []

    def rerank(query, documents):
        calls.append(len(documents))
        return [0.5] * len(documents)

    monkeypatch.setattr(rerank_service, "call_rerank_model", rerank)
    assert len(service.search("方法", top_k=30)) == 30
    assert calls == [50]


def test_rerank_keeps_the_best_hit_tail_with_exact_keyword_evidence(service, monkeypatch):
    content = "说明" * 350 + "唯一编号 ZX-913：完成标准是双人核对。"
    service.import_document(entry(content), execute=True)
    service.publish("decision-review", confirmed_by="测试确认人", execute=True)
    for row in service.index.rows:
        row['route_ranks'] = {'text_keyword': 1}

    def rerank(query, documents):
        assert len(documents) == 1 and 'ZX-913：完成标准是双人核对。' in documents[0]
        assert len(documents[0]) <= 1800
        return [0.95]

    monkeypatch.setattr(rerank_service, 'call_rerank_model', rerank)
    assert service.search('ZX-913')[0]['rerank_mode'] == 'model'
