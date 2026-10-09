from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

import pytest
from alibabacloud_ha3engine_vector import models

from rag_app import knowledge_index as index


pytestmark = pytest.mark.offline


def test_console_schema_matches_chunk_fields_and_required_filters():
    import json
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[1] / "docs" / "company_knowledge_opensearch.json").read_text())["schema"]
    fields = {field["field_name"]: field for field in schema["fields"]}
    assert schema["table_name"] == index.HYBRID_TABLE_NAME
    assert set(fields) == set(index.SCHEMA_FIELDS)
    assert fields["revision"]["field_type"] == fields["chunk_index"]["field_type"] == "INT64"
    assert fields[index.VECTOR_FIELD]["multi_value"] is True
    assert fields[index.VECTOR_FIELD]["field_type"] == "FLOAT"
    assert "revision_key" in {field["field_name"] for field in schema["attributes"]}
    assert set(index.OUTPUT_FIELDS) <= set(schema["summarys"]["summary_fields"])
    vector = next(item for item in schema["indexs"] if item["index_name"] == index.VECTOR_FIELD)
    assert int(vector["parameters"]["dimension"]) == index.VECTOR_DIMENSIONS
    assert vector["parameters"]["distance_type"] == "InnerProduct"
    for name in ("title_terms", "text_terms"):
        assert fields[name]["field_type"] == "TEXT" and fields[name]["analyzer"] == "chn_standard"
        assert name not in {item["field_name"] for item in schema["attributes"]}
        assert {"index_name": name, "index_type": "TEXT", "index_fields": name} in schema["indexs"]
    for name in ("title", "source_text"):
        assert fields[name]["field_type"] == 'STRING'
        assert name in {item['field_name'] for item in schema['attributes']}


def record(knowledge_id="method-a", revision=1, content="公司方法与经验。", title="方法"):
    return {
        "knowledge_id": knowledge_id, "revision": revision, "title": title,
        "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }


class TableClient:
    """Each cloud operation is scoped to a table; no network or credential reads."""

    def __init__(self):
        self.tables = {"company_knowledge": {}, "text_docs": {"product-a": {"id": "product-a"}}}
        self.calls = []
        self.skip_id = None
        self.fetch_delay = 0
        self.fetch_mutation = None
        self.push_body = {"status": "OK"}
        self.read_body = None
        self.query_empty = False
        self.query_delay = 0
        self.query_error = False
        self.extra_hits = []
        self.keyword_error = False
        self.keyword_hits = None

    def push_documents(self, source, primary_key, request):
        self.calls.append(("push", source, request.to_map()))
        assert primary_key == "id"
        assert source in ("company_knowledge", "inst_company_knowledge")
        for document in request.body:
            assert document["cmd"] == "add"
            fields = dict(document["fields"])
            if fields["id"] != self.skip_id:
                self.tables["company_knowledge"][fields["id"]] = fields
        return SimpleNamespace(body=self.push_body)

    def fetch(self, request):
        self.calls.append(("fetch", request.table_name, request.to_map()))
        if self.read_body is not None:
            return SimpleNamespace(body=self.read_body)
        if self.fetch_delay:
            self.fetch_delay -= 1
            return SimpleNamespace(body={"result": []})
        rows = []
        for document_id in request.ids:
            if document_id in self.tables[request.table_name]:
                fields = {key: value for key, value in self.tables[request.table_name][document_id].items()
                          if key in request.output_fields}
                if self.fetch_mutation:
                    fields.update(self.fetch_mutation)
                rows.append({"fields": fields})
        return SimpleNamespace(body={"result": rows})

    def query(self, request):
        self.calls.append(("query", request.table_name, request.to_map()))
        if self.query_error:
            raise RuntimeError("credential-sentinel: remote connection failed")
        if self.read_body is not None:
            return SimpleNamespace(body=self.read_body)
        if self.query_delay:
            self.query_delay -= 1
            return SimpleNamespace(body={"result": []})
        assert request.index_name == index.VECTOR_FIELD
        keys = re.findall(r'revision_key="([A-Za-z0-9_-]+)"', request.filter)
        assert keys and request.filter == "(" + " OR ".join(f'revision_key="{key}"' for key in keys) + ")"
        rows = []
        if not self.query_empty:
            for fields in self.tables[request.table_name].values():
                if fields["revision_key"] in keys:
                    rows.append({"fields": {key: value for key, value in fields.items()
                                            if key in request.output_fields},
                                 "score": fields.get("test_score", 1.0)})
        rows.extend(self.extra_hits)
        rows.sort(key=lambda row: float(row.get("score", 0)), reverse=True)
        return SimpleNamespace(body={"result": rows[:request.top_k]})

    def search(self, request):
        self.calls.append(("search", request.table_name, request.to_map()))
        if self.keyword_error:
            raise RuntimeError("credential-sentinel: missing text index")
        if self.read_body is not None:
            return SimpleNamespace(body=self.read_body)
        assert request.text.query_params == {"default_op": "OR"}
        keys = re.findall(r'revision_key="([A-Za-z0-9_-]+)"', request.text.filter)
        assert keys and request.text.filter == index._filter(keys)
        rows = self.keyword_hits
        if rows is None:
            rows = [{"fields": {key: value for key, value in fields.items() if key in request.output_fields},
                     "score": fields.get("test_score", 1.0)}
                    for fields in self.tables[request.table_name].values() if fields["revision_key"] in keys]
        rows = [*rows, *self.extra_hits]
        return SimpleNamespace(body={"result": sorted(rows, key=lambda row: float(row.get("score", 0)),
                                                       reverse=True)[:request.size]})


@pytest.fixture
def cloud(monkeypatch):
    fake = TableClient()
    calls = {"client": 0, "embeddings": []}
    config = {"endpoint": "https://example.invalid", "instance_id": "inst",
              "username": "offline", "password": "credential-sentinel"}

    def create(_config):
        calls["client"] += 1
        return fake, models

    def embed(text):
        calls["embeddings"].append(text)
        return [0.1] * 1024

    monkeypatch.setattr(index, "ensure_runtime_config", lambda: config)
    monkeypatch.setattr(index, "create_client", create)
    monkeypatch.setattr(index, "embed_text_query", embed)
    monkeypatch.setattr(index.time, "sleep", lambda _: None)
    monkeypatch.delenv("KNOWLEDGE_TABLE", raising=False)
    monkeypatch.setenv("KNOWLEDGE_PUSH_DATASOURCE", "inst_company_knowledge")
    return fake, calls, config


def seed(fake, document, score=1):
    for chunk in index.chunk_document(document):
        fake.tables["company_knowledge"][chunk["id"]] = {**chunk, "test_score": score}


def test_chunk_identity_hash_overlap_and_title_limit():
    document = record(content="甲" * 1000, title="长" * 1000)
    chunks = index.chunk_document(document)
    assert [row["chunk_id"] for row in chunks] == ["method-a__r1__c1", "method-a__r1__c2"]
    assert [len(row["source_text"]) for row in chunks] == [900, 220]
    assert chunks[0]["source_text"][-120:] == chunks[1]["source_text"][:120]
    assert all(row["title"] == "长" * 240 for row in chunks)
    assert all(row["content_hash"] == hashlib.sha256(row["source_text"].encode()).hexdigest() for row in chunks)
    assert index.chunk_document(document) == chunks
    assert index.chunk_document(record(revision=2))[0]["chunk_id"] == "method-a__r2__c1"
    assert index.chunk_document(record(knowledge_id="a" * 80))[0]["knowledge_id"] == "a" * 80


def test_chunking_version_preserves_legacy_and_selects_structure_explicitly(cloud):
    from rag_app.knowledge_chunking import split_structure

    content = "# 一\n\n" + "证据。" * 240 + "\n\n# 二\n\n" + "方法。" * 240
    document = record(content=content)
    legacy = index.chunk_document(document)
    assert index.chunk_document({**document, "chunking_version": "legacy-v1"}) == legacy
    structured = {**document, "chunking_version": "structure-v1"}
    chunks = index.chunk_document(structured)
    assert [chunk["source_text"] for chunk in chunks] == [chunk["source_text"] for chunk in split_structure(content)]
    assert chunks != legacy and structured["content"] == content
    assert all(set(chunk) == set(index.OUTPUT_FIELDS) for chunk in chunks)
    assert all(chunk["content_hash"] == hashlib.sha256(chunk["source_text"].encode()).hexdigest() for chunk in chunks)
    assert index.KnowledgeIndex().index_and_verify(structured)["retrieval_verified"] is True


def test_v2_embeddings_include_section_path_without_changing_cloud_schema(cloud):
    from rag_app.knowledge_chunking import split_structure_v2

    fake, calls, _ = cloud
    content = "# 方法\n\n## 异常处理\n\n| 情形 | 操作 |\n| --- | --- |\n| 空值 | 停止核对 |\n\n" + "核对原值。" * 220
    document = {**record(content=content, title="公司知识"), "chunking_version": "structure-v2"}
    previews = split_structure_v2(content)
    chunks = index.chunk_document(document)
    assert [row["source_text"] for row in chunks] == [row["source_text"] for row in previews]
    assert all(row["section_path"] == ["方法", "异常处理"] for row in chunks)
    assert all(set(row) == set(index.OUTPUT_FIELDS) | {"section_path"} for row in chunks)
    assert index.KnowledgeIndex().index_and_verify(document)["retrieval_verified"] is True
    assert calls["embeddings"] == ["公司知识\n方法\n异常处理\n" + chunk["source_text"] for chunk in chunks]
    assert all(set(row) == set(index.SCHEMA_FIELDS) for row in fake.tables["company_knowledge"].values())
    assert all(row["content_hash"] == hashlib.sha256(row["source_text"].encode()).hexdigest() for row in chunks)


@pytest.mark.parametrize("version", ["legacy-v1", "structure-v1"])
def test_old_versions_keep_embedding_input_and_fields_unchanged(cloud, version):
    _, calls, _ = cloud
    document = {**record(content="# 方法\n\n## 输入\n\n确认。"), "chunking_version": version}
    chunks = index.chunk_document(document)
    assert all(set(row) == set(index.OUTPUT_FIELDS) for row in chunks)
    index.KnowledgeIndex().index_and_verify(document)
    assert calls["embeddings"] == ["方法\n" + chunk["source_text"] for chunk in chunks]


def test_v2_decoration_only_document_fails_before_cloud(cloud):
    fake, calls, _ = cloud
    with pytest.raises(ValueError, match="no indexable content"):
        index.KnowledgeIndex().index_and_verify({**record(content="---\n\n***\n"), "chunking_version": "structure-v2"})
    assert calls == {"client": 0, "embeddings": []}
    assert fake.calls == []


@pytest.mark.parametrize("changes", [
    {"knowledge_id": "a" * 81}, {"knowledge_id": 'a\" OR revision=1'},
    {"knowledge_id": "../a"}, {"revision": True}, {"revision": "1"},
    {"revision": 0}, {"revision": 2**63}, {"content_hash": "bad"},
    {"title": " "}, {"content": " "},
    {"chunking_version": "unknown"}, {"chunking_version": None},
])
def test_invalid_documents_fail_before_cloud_or_embedding(cloud, changes):
    fake, calls, _ = cloud
    with pytest.raises(ValueError):
        index.KnowledgeIndex().index_and_verify({**record(), **changes})
    assert calls == {"client": 0, "embeddings": []}
    assert fake.calls == []


def test_empty_allowed_versions_does_no_work(cloud):
    fake, calls, _ = cloud
    client = index.KnowledgeIndex()
    assert calls["client"] == 0
    assert client.search("anything", [], 10) == []
    assert calls == {"client": 0, "embeddings": []}
    assert fake.calls == []


def test_full_batch_write_fetch_query_and_idempotent_retry(cloud):
    fake, calls, _ = cloud
    document = record(content="方法" * 4500)
    chunks = index.chunk_document(document)
    client = index.KnowledgeIndex()
    result = client.index_and_verify(document)
    assert result["verified_chunk_ids"] == [chunk["id"] for chunk in chunks]
    assert result["chunk_count"] == len(chunks) > index.WRITE_BATCH_SIZE
    assert result["retrieval_verified"] is True
    assert fake.tables["text_docs"] == {"product-a": {"id": "product-a"}}
    assert len(fake.tables["company_knowledge"]) == len(chunks)
    assert len(calls["embeddings"]) == len(chunks)
    assert all(target == ("inst_company_knowledge" if operation == "push" else "company_knowledge")
               for operation, target, _ in fake.calls)
    assert fake.calls[-1][2]["filter"] == '(revision_key="method-a__r1")'
    assert fake._runtime_options.read_timeout == 15000
    assert fake._runtime_options.connect_timeout == 5000
    assert fake._runtime_options.autoretry is False
    client.index_and_verify(document)
    assert len(fake.tables["company_knowledge"]) == len(chunks)
    assert calls["client"] == 1


def test_automatic_publication_verifies_dense_keyword_and_every_chunk(cloud, tmp_path):
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    fake, calls, _ = cloud
    service = KnowledgeService(KnowledgeStore(tmp_path / "catalog.sqlite3"), index.KnowledgeIndex())
    draft = service.import_document({**record(content="方法" * 4500), "sources": [{"name": "离线样本"}]}, execute=True)
    result = service.publish_automatic("method-a", 1, expected_generation=draft["generation"], confirmed_by="rule:v1")
    assert result["verification"]["keyword_retrieval_verified"] is True
    assert result["verification"]["retrieval_verified"] is True
    assert result["verification"]["verified_chunk_ids"] == [chunk["id"] for chunk in index.chunk_document(draft)]
    assert [op for op, _, _ in fake.calls][-2:] == ["query", "search"]
    assert service.get("method-a")["content"] == draft["content"]


@pytest.mark.parametrize("failure", ["error", "empty", "wrong_fields"])
def test_automatic_keyword_verification_failure_never_publishes(cloud, tmp_path, failure):
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    fake, calls, _ = cloud
    if failure == "error":
        fake.keyword_error = True
    elif failure == "empty":
        fake.keyword_hits = []
    else:
        fake.keyword_hits = [{"fields": {**index.chunk_document(record())[0], "source_text": "wrong"}, "score": 1.0}]
    service = KnowledgeService(KnowledgeStore(tmp_path / "catalog.sqlite3"), index.KnowledgeIndex())
    draft = service.import_document({**record(), "sources": [{"name": "离线样本"}]}, execute=True)
    with pytest.raises(index.KnowledgeIndexError):
        service.publish_automatic("method-a", 1, expected_generation=draft["generation"], confirmed_by="rule:v1")
    assert service.store.snapshot("method-a") == draft
    assert service.get("method-a") is None
    assert len(calls["embeddings"]) == 1
    assert len([op for op, _, _ in fake.calls if op == "push"]) == 1


@pytest.mark.parametrize("fetch_delay,query_delay", [(5, 7), (30, 0), (0, 30)])
def test_eventual_fetch_and_query_visibility_is_retried_without_rewriting(cloud, monkeypatch, fetch_delay, query_delay):
    fake, calls, _ = cloud
    fake.fetch_delay = fetch_delay
    fake.query_delay = query_delay
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    assert index.KnowledgeIndex().index_and_verify(record())["retrieval_verified"] is True
    operations = [operation for operation, _, _ in fake.calls]
    assert operations.count("fetch") == fetch_delay + 1
    assert operations.count("query") == query_delay + 1
    assert operations.count("push") == 1
    assert len(calls["embeddings"]) == 1
    assert sleeps == [index.VERIFY_RETRY_DELAY] * (fetch_delay + query_delay)
    assert sum(sleeps) <= 30


def test_partial_write_cannot_pass_verification(cloud):
    fake, _, _ = cloud
    fake.skip_id = "method-a__r1__c2"
    with pytest.raises(index.KnowledgeIndexError, match="write verification failed"):
        index.KnowledgeIndex().index_and_verify(record(content="甲" * 1000))
    assert not any(operation == "query" for operation, _, _ in fake.calls)


@pytest.mark.parametrize("mutation", [{"source_text": "篡改"}, {"content_hash": "bad"},
                                    {"knowledge_id": "other"}, {"revision": 2}, {"id": []}])
def test_fetch_must_match_every_chunk_field(cloud, mutation):
    fake, _, _ = cloud
    fake.fetch_mutation = mutation
    with pytest.raises(index.KnowledgeIndexError):
        index.KnowledgeIndex().index_and_verify(record())


@pytest.mark.parametrize("body", [
    {"status": "FAIL", "code": 200}, {"status": "OK", "errors": ["partial failure"]},
    {"code": "InvalidDocument"}, {}, {"error": "credential-sentinel"},
])
def test_http_success_does_not_hide_push_business_failure(cloud, body):
    fake, _, _ = cloud
    fake.push_body = body
    with pytest.raises(index.KnowledgeIndexError) as exc:
        index.KnowledgeIndex().index_and_verify(record())
    assert "credential-sentinel" not in str(exc.value)
    assert all(operation == "push" for operation, _, _ in fake.calls)


@pytest.mark.parametrize("invisible", ["fetch", "query", "shared_budget"])
def test_invisible_index_has_bounded_wait_and_cannot_publish(cloud, monkeypatch, tmp_path, invisible):
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    fake, calls, _ = cloud
    if invisible == "fetch":
        fake.skip_id = "method-a__r1__c1"
    elif invisible == "query":
        fake.query_empty = True
    else:
        fake.fetch_delay, fake.query_delay = 15, 16
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    service = KnowledgeService(KnowledgeStore(tmp_path / "catalog.sqlite3"), index.KnowledgeIndex())
    draft = service.import_document({**record(), "sources": [{"name": "离线测试样本"}]}, execute=True)
    with pytest.raises(index.KnowledgeIndexError, match="verification failed"):
        service.publish("method-a", confirmed_by="离线测试审核人", execute=True)
    assert service.store.snapshot("method-a") == draft
    assert service.get("method-a") is None and service.search("方法") == []
    operations = [operation for operation, _, _ in fake.calls]
    assert operations.count("push") == 1
    if invisible == "fetch":
        assert operations.count("fetch") == index.VERIFY_ATTEMPTS and operations.count("query") == 0
    elif invisible == "query":
        assert operations.count("fetch") == 1 and operations.count("query") == index.VERIFY_ATTEMPTS
    else:
        assert operations.count("fetch") == 16 and operations.count("query") == 16
    assert len(calls["embeddings"]) == 1
    assert sleeps == [index.VERIFY_RETRY_DELAY] * (index.VERIFY_ATTEMPTS - 1)
    assert sum(sleeps) == 30


@pytest.mark.parametrize("vector", [[0.1] * 1023, [float("nan")] * 1024,
                                    [float("inf")] * 1024, [True] * 1024,
                                    ["1"] * 1024, {"embedding": [0.1] * 1024}])
def test_bad_vectors_are_rejected_before_push(cloud, monkeypatch, vector):
    fake, _, _ = cloud
    monkeypatch.setattr(index, "embed_text_query", lambda _: vector)
    with pytest.raises(index.KnowledgeIndexError, match="invalid vector"):
        index.KnowledgeIndex().index_and_verify(record())
    assert fake.calls == []


def test_search_batches_cloud_filters_and_embeds_once(cloud):
    fake, calls, _ = cloud
    documents = [record(knowledge_id=f"method-{number:02d}") for number in range(35)]
    for number, document in enumerate(documents):
        seed(fake, document, score=number)
    seed(fake, record(revision=2), score=9999)
    result = index.KnowledgeIndex().search("公司方法", documents, 200)
    assert len(result) == 35
    assert result[0]["knowledge_id"] == "method-34"
    assert result[-1]["knowledge_id"] == "method-00"
    assert len(calls["embeddings"]) == 1
    assert [operation for operation, _, _ in fake.calls] == ["query", "search", "query", "search"]
    assert all(target == "company_knowledge" for _, target, _ in fake.calls)
    assert [len(re.findall("revision_key=", request.get("filter", request.get("text", {}).get("filter"))))
            for _, _, request in fake.calls] == [32, 32, 3, 3]
    assert set(result[0]) == {"knowledge_id", "revision", "chunk_id", "chunk_index", "source_text", "content_hash", "score", "route_ranks", "route_scores"}
    assert result[0]["route_ranks"] == {"text_dense": 1, "text_keyword": 1}
    assert result[-1]["route_ranks"] == {"text_dense": 35, "text_keyword": 35}


def test_keyword_only_hit_is_kept_and_query_literals_cannot_change_filter(cloud):
    fake, _, _ = cloud
    doc = record()
    seed(fake, doc)
    fake.query_empty = True
    query = "O'Reilly\\样例 OR revision_key:'not-published'"
    result = index.KnowledgeIndex().search(query, [doc], 10)
    assert len(result) == 1 and result[0]["route_ranks"] == {"text_keyword": 1}
    request = fake.calls[-1][2]
    assert request["text"]["filter"] == '(revision_key="method-a__r1")'
    escaped = r"O\'Reilly\\样例 OR revision_key:\'not-published\'"
    assert request["text"]["queryString"] == f"(title_terms:'{escaped}' OR text_terms:'{escaped}')"


def test_routes_keep_their_own_ranks_and_union_instead_of_comparing_raw_scores(cloud):
    fake, _, _ = cloud
    dense, lexical = record(knowledge_id="dense"), record(knowledge_id="lexical")
    seed(fake, dense, score=0.99)
    seed(fake, lexical, score=0.1)
    fake.keyword_hits = [{"fields": index.chunk_document(lexical)[0], "score": 10000}]
    rows = index.KnowledgeIndex().search("同义问题和精确编号", [dense, lexical], 1)
    assert len(rows) == 2
    assert {row["knowledge_id"]: row["route_ranks"] for row in rows} == {
        "dense": {"text_dense": 1}, "lexical": {"text_keyword": 1}}


def test_keyword_failure_is_visible_instead_of_silently_using_dense_only(cloud):
    fake, _, _ = cloud
    fake.keyword_error = True
    seed(fake, record())
    with pytest.raises(index.KnowledgeIndexError) as error:
        index.KnowledgeIndex().search("方法", [record()], 10)
    assert "credential-sentinel" not in str(error.value)
    assert [operation for operation, _, _ in fake.calls] == ["query", "search", "search"]


def test_equal_keyword_scores_do_not_turn_ids_or_extra_chunks_into_relevance(cloud):
    fake, _, _ = cloud
    first = record(knowledge_id="a-long", content="匹配方法。" * 500)
    second = record(knowledge_id="z-short", content="匹配方法。")
    seed(fake, first, score=0)
    seed(fake, second, score=0)
    fake.query_empty = True
    rows = index.KnowledgeIndex().search("匹配", [first, second], 20)
    assert {row["knowledge_id"] for row in rows} == {"a-long", "z-short"}
    assert all(row["route_ranks"] == {"text_keyword": 1.5} for row in rows)
    assert sum(row["knowledge_id"] == "a-long" for row in rows) <= 3


def test_full_text_tokenization_cannot_change_raw_chunk_emoji_or_hash(cloud, monkeypatch):
    import json
    from pathlib import Path
    fake, _, _ = cloud
    schema = json.loads((Path(__file__).resolve().parents[1] / 'docs/company_knowledge_opensearch.json').read_text())['schema']
    tokenized = {field['field_name'] for field in schema['fields'] if field['field_type'] == 'TEXT'}
    push = fake.push_documents

    def tokenizer_loses_non_bmp(source, key, request):
        for document in request.body:
            for field in tokenized:
                if field in document['fields']:
                    document['fields'][field] = ''.join(c for c in document['fields'][field] if ord(c) <= 0xffff)
        return push(source, key, request)

    monkeypatch.setattr(fake, 'push_documents', tokenizer_loses_non_bmp)
    document = record(content='📊检查数据，📦核对交付，🛡保留证据。', title='📊核对方法')
    client = index.KnowledgeIndex()
    assert client.index_and_verify(document)['retrieval_verified'] is True
    assert client.search('核对方法', [document], 10)[0]['source_text'] == document['content']


def test_search_rejects_rogue_old_and_corrupt_chunks(cloud):
    fake, _, _ = cloud
    seed(fake, record())
    valid = index.chunk_document(record())[0]
    fake.extra_hits = [
        {"fields": index.chunk_document(record(revision=2))[0], "score": 90},
        {"fields": index.chunk_document(record(knowledge_id="not-published"))[0], "score": 90},
        {"fields": {**valid, "source_text": "篡改"}, "score": 90},
        {"fields": {**valid, "chunk_id": "another-chunk"}, "score": 90},
        {"fields": valid, "score": float("inf")},
    ]
    result = index.KnowledgeIndex().search("method", [record()], 10)
    assert len(result) == 1
    assert result[0]["chunk_id"] == "method-a__r1__c1"
    assert result[0]["score"] == 1


def test_query_failure_has_bounded_retries_no_fallback_and_sanitized_error(cloud):
    fake, calls, _ = cloud
    fake.query_error = True
    with pytest.raises(index.KnowledgeIndexError) as exc:
        index.KnowledgeIndex().search("method", [record()], 5)
    assert "credential-sentinel" not in str(exc.value)
    assert len(calls["embeddings"]) == 1
    assert [(operation, target) for operation, target, _ in fake.calls] == [
        ("query", "company_knowledge"), ("query", "company_knowledge"),
    ]


@pytest.mark.parametrize("error", [
    {"status": "FAIL"},
    {"errorCode": 123, "errorMsg": "credential-sentinel"},
    {"status": "OK", "code": 0, "errorCode": 123},
])
def test_business_error_cannot_be_used_as_results(cloud, error):
    fake, _, _ = cloud
    fake.read_body = {**error, "result": [{"fields": index.chunk_document(record())[0]}]}
    with pytest.raises(index.KnowledgeIndexError, match="rejected") as exc:
        index.KnowledgeIndex().search("method", [record()], 5)
    assert "credential-sentinel" not in str(exc.value)


@pytest.mark.parametrize("name", ["", "text_docs", "inst_text_docs", "image_docs", "arbitrary", "other_company_knowledge"])
def test_product_or_unknown_query_table_is_rejected(cloud, monkeypatch, name):
    fake, calls, _ = cloud
    monkeypatch.setenv("KNOWLEDGE_TABLE", name)
    with pytest.raises(index.KnowledgeIndexError, match="configuration"):
        index.KnowledgeIndex().search("method", [record()], 5)
    assert calls == {"client": 0, "embeddings": []}
    assert fake.calls == []


@pytest.mark.parametrize("name", ["", "text_docs", "inst_text_docs", "image_docs", "arbitrary", "other_company_knowledge"])
def test_push_requires_explicit_company_data_source(cloud, monkeypatch, name):
    fake, calls, _ = cloud
    monkeypatch.setenv("KNOWLEDGE_PUSH_DATASOURCE", name)
    with pytest.raises(index.KnowledgeIndexError, match="KNOWLEDGE_PUSH_DATASOURCE"):
        index.KnowledgeIndex().index_and_verify(record())
    assert calls["embeddings"] == []
    assert fake.calls == []


@pytest.mark.parametrize("changes", [
    {"password": ""}, {"instance_id": "inst/other"}, {"endpoint": "https://name:secret@example.invalid"},
    {"endpoint": "https://example.invalid/path"}, {"endpoint": "https://example.invalid?secret=1"},
    {"endpoint": "file:///tmp/x"}, {"endpoint": "https://example.invalid\n"},
])
def test_invalid_cloud_config_fails_closed(cloud, changes):
    fake, calls, config = cloud
    config.update(changes)
    with pytest.raises(index.KnowledgeIndexError, match="configuration"):
        index.KnowledgeIndex().search("method", [record()], 5)
    assert calls == {"client": 0, "embeddings": []}
    assert fake.calls == []


def test_instance_prefixed_company_table_is_explicitly_supported(cloud, monkeypatch):
    fake, _, _ = cloud
    fake.tables["inst_company_knowledge"] = fake.tables["company_knowledge"]
    monkeypatch.setenv("KNOWLEDGE_TABLE", "inst_company_knowledge")
    result = index.KnowledgeIndex().index_and_verify(record())
    assert result["table"] == "inst_company_knowledge"
    assert all(target == "inst_company_knowledge" for _, target, _ in fake.calls)


@pytest.mark.parametrize("prefixed", [False, True])
def test_hybrid_table_is_explicit_and_push_cannot_target_the_old_table(cloud, monkeypatch, prefixed):
    fake, _, _ = cloud
    table = ("inst_" if prefixed else "") + index.HYBRID_TABLE_NAME
    monkeypatch.setenv("KNOWLEDGE_TABLE", table)
    client = index.KnowledgeIndex()
    client._connect()
    assert client.table == table
    with pytest.raises(index.KnowledgeIndexError, match="KNOWLEDGE_PUSH_DATASOURCE"):
        client._datasource()
    monkeypatch.setenv("KNOWLEDGE_PUSH_DATASOURCE", "inst_" + index.HYBRID_TABLE_NAME)
    assert client._datasource() == "inst_company_knowledge_hybrid"
    assert fake.calls == []


def test_invalid_revision_filter_cannot_reach_cloud(cloud):
    fake, calls, _ = cloud
    with pytest.raises(ValueError):
        index.KnowledgeIndex().search("method", [{"knowledge_id": 'x\" OR true', "revision": 1}], 5)
    assert calls == {"client": 0, "embeddings": []}
    assert fake.calls == []


def test_real_api_store_and_index_publication_lifecycle(cloud, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from rag_app.knowledge_api import get_knowledge_service, router
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore

    fake, calls, _ = cloud
    service = KnowledgeService(KnowledgeStore(tmp_path / "knowledge.sqlite3"), index.KnowledgeIndex())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    entry = {
        "knowledge_id": "method-a", "title": "复盘方法", "content": "先验证证据，再讨论方法。",
        "contributor": "测试贡献人", "kind": "method",
        "sources": [{"name": "模拟样本.md", "locator": "第二节", "kind": "file"}],
    }
    with TestClient(app) as client:
        service.import_document(entry, execute=True)
        assert client.get("/api/knowledge/method-a").status_code == 404
        service.publish("method-a", confirmed_by="测试确认人", execute=True)
        response = client.post("/api/knowledge/search", json={"query": "复盘", "top_k": 30})
        assert response.status_code == 200
        assert response.json()["results"][0]["revision"] == 1
        assert fake.calls[-2][2]["topK"] == fake.calls[-1][2]["size"] == 150
        assert client.get("/api/knowledge/method-a").json()["knowledge"]["content"] == entry["content"]

        service.import_document({**entry, "content": "新版复盘方法。"}, execute=True)
        fake.skip_id = "method-a__r2__c1"
        with pytest.raises(index.KnowledgeIndexError, match="write verification failed"):
            service.publish("method-a", confirmed_by="测试确认人", execute=True)
        assert client.get("/api/knowledge/method-a").json()["knowledge"]["revision"] == 1
        response = client.post("/api/knowledge/search", json={"query": "复盘", "top_k": 30})
        assert response.status_code == 200
        assert response.json()["results"][0]["revision"] == 1

        service.withdraw("method-a", execute=True)
        cloud_call_count, embedding_count = len(fake.calls), len(calls["embeddings"])
        assert client.get("/api/knowledge/method-a").status_code == 404
        response = client.post("/api/knowledge/search", json={"query": "复盘", "top_k": 30})
        assert response.status_code == 200
        assert response.json()["results"] == []
        assert len(fake.calls) == cloud_call_count
        assert len(calls["embeddings"]) == embedding_count
        assert fake.tables["text_docs"] == {"product-a": {"id": "product-a"}}
