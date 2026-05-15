from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import push_ingest_embeddings
from conftest import read_jsonl, write_jsonl


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeModels:
    class FetchRequest:
        def __init__(self, table_name, ids, include_vector=False, output_fields=None):
            self.table_name = table_name
            self.ids = ids

    class PushDocumentsRequest:
        def __init__(self, headers=None, body=None):
            self.headers = headers or {}
            self.body = body or []


class FakeClient:
    def __init__(self, existing_ids=None, fail_ids=None, rows=None):
        self.existing_ids = set(existing_ids or [])
        self.fail_ids = set(fail_ids or [])
        self.rows = rows or {}
        self.pushed = []

    def fetch(self, request):
        result = []
        for doc_id in request.ids:
            if doc_id in self.rows:
                result.append({"fields": self.rows[doc_id]})
            elif doc_id in self.existing_ids:
                result.append({"fields": {"id": doc_id, "doc_id": doc_id}})
        return FakeResponse({"result": result})

    def push_documents(self, table_name, pk_field, request):
        document = request.body[0]
        doc_id = document["fields"]["id"]
        if doc_id in self.fail_ids:
            raise RuntimeError("boom")
        self.pushed.append(document)
        return FakeResponse({"status": "OK"})

    def get_table(self, table_name):
        return FakeResponse({"fields": [{"name": "source_text_vector", "dimension": 1024}], "count": 12})

    def count(self, table_name):
        return FakeResponse({"count": 12})


def embedding_record(doc_id: str = "doc-a", *, text: str = "hello", dim: int = 1024) -> dict:
    return {
        "doc_id": doc_id,
        "source_type": "product_excel",
        "source_doc_id": "source-a",
        "source_sha1": "sha-a",
        "product_id": "prod-a",
        "content_sha1": f"content-{doc_id}",
        "embedding_model": "text-embedding-v4",
        "embedding_dim": dim,
        "text": text,
        "page_content": text,
        "doc_type": "product_full",
        "field_name": "summary",
        "metadata": {
            "doc_id": doc_id,
            "product_id": "prod-a",
            "source_doc_id": "source-a",
            "source_sha1": "sha-a",
            "source_file": "素材/sample.xlsx",
        },
        "embedding": [0.1] * dim,
    }


def write_embeddings(root: Path, records: list[dict]) -> Path:
    path = root / "output/ingest_embeddings/product_excel/embeddings.jsonl"
    write_jsonl(path, records)
    return path


def write_srx_supplement_embeddings(root: Path, records: list[dict]) -> Path:
    path = root / "output/ingest_embeddings/product_excel_supplement_srx/embeddings.jsonl"
    write_jsonl(path, records)
    return path


def write_new_product_embeddings(root: Path, records: list[dict]) -> Path:
    path = root / "output/ingest_embeddings/product_excel_new_products/embeddings.jsonl"
    write_jsonl(path, records)
    return path


@pytest.mark.offline
def test_default_dry_run_does_not_write_opensearch(monkeypatch, tmp_path: Path) -> None:
    write_embeddings(tmp_path, [embedding_record()])
    fake = FakeClient()
    monkeypatch.setattr(push_ingest_embeddings, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "push_ingest_embeddings.py",
            "--root",
            str(tmp_path),
            "--source-type",
            "product_excel",
            "--input-dir",
            "output/ingest_embeddings",
        ],
    )

    assert push_ingest_embeddings.main() == 0
    assert fake.pushed == []


@pytest.mark.offline
def test_execute_requires_confirmation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")

    with pytest.raises(SystemExit):
        push_ingest_embeddings.confirm_or_exit(False)


@pytest.mark.offline
def test_limit_three_only_loads_three(tmp_path: Path) -> None:
    write_embeddings(tmp_path, [embedding_record(f"doc-{idx}") for idx in range(5)])

    records, _paths = push_ingest_embeddings.load_embedding_records(
        tmp_path / "output/ingest_embeddings",
        ["product_excel"],
        limit=3,
    )

    assert [record["doc_id"] for record in records] == ["doc-0", "doc-1", "doc-2"]


@pytest.mark.offline
def test_product_excel_supplement_srx_source_type_loads_srx_embeddings(tmp_path: Path) -> None:
    write_srx_supplement_embeddings(
        tmp_path,
        [
            {
                **embedding_record("supp-srx"),
                "source_type": "product_excel_supplement_srx",
                "doc_type": "supplement",
                "metadata": {
                    **embedding_record("supp-srx")["metadata"],
                    "canonical_product_id": "prod-a",
                    "doc_type": "supplement",
                    "is_supplement": True,
                },
            }
        ],
    )

    records, paths = push_ingest_embeddings.load_embedding_records(
        tmp_path / "output/ingest_embeddings",
        push_ingest_embeddings.selected_source_types("product_excel_supplement_srx"),
        limit=3,
    )

    assert len(records) == 1
    assert records[0]["source_type"] == "product_excel_supplement_srx"
    assert paths["product_excel_supplement_srx"].as_posix().endswith("product_excel_supplement_srx/embeddings.jsonl")


@pytest.mark.offline
def test_product_excel_new_products_source_type_loads_new_product_embeddings(tmp_path: Path) -> None:
    record = {
        **embedding_record("new-prod-a"),
        "source_type": "product_excel_new_products",
        "doc_type": "product_full",
        "product_id": "prd_000286",
        "metadata": {
            **embedding_record("new-prod-a")["metadata"],
            "product_id": "prd_000286",
            "canonical_product_id": "prd_000286",
            "doc_type": "product_full",
            "source_type": "product_excel_new_product",
        },
    }
    write_new_product_embeddings(tmp_path, [record])

    records, paths = push_ingest_embeddings.load_embedding_records(
        tmp_path / "output/ingest_embeddings",
        push_ingest_embeddings.selected_source_types("product_excel_new_products"),
        limit=None,
    )
    summary = push_ingest_embeddings.analyze_records(records, existing_doc_ids=set(), force=False, skip_existing=True)

    assert len(records) == 1
    assert records[0]["source_type"] == "product_excel_new_products"
    assert paths["product_excel_new_products"].as_posix().endswith("product_excel_new_products/embeddings.jsonl")
    assert summary["planned_new"] == 1
    assert summary["planned_upsert"] == 0
    assert records[0]["doc_type"] != "supplement"
    assert records[0]["product_id"].startswith("prd_")


@pytest.mark.offline
def test_final_49_source_types_are_supported_by_push_wrapper(tmp_path: Path) -> None:
    for source_type, doc_type, product_id in [
        ("product_excel_supplement_reviewed", "supplement", "prod-existing"),
        ("product_excel_new_products_final", "product_full", "prd_000286"),
    ]:
        path = tmp_path / f"output/ingest_embeddings/{source_type}/embeddings.jsonl"
        write_jsonl(
            path,
            [
                {
                    **embedding_record(f"{source_type}-doc"),
                    "source_type": source_type,
                    "doc_type": doc_type,
                    "product_id": product_id,
                    "metadata": {
                        **embedding_record(f"{source_type}-doc")["metadata"],
                        "product_id": product_id,
                        "canonical_product_id": product_id,
                        "doc_type": doc_type,
                    },
                }
            ],
        )
        records, paths = push_ingest_embeddings.load_embedding_records(
            tmp_path / "output/ingest_embeddings",
            push_ingest_embeddings.selected_source_types(source_type),
            limit=None,
        )
        summary = push_ingest_embeddings.analyze_records(records, existing_doc_ids=set(), force=False, skip_existing=True)
        assert records[0]["source_type"] == source_type
        assert paths[source_type].as_posix().endswith(f"{source_type}/embeddings.jsonl")
        assert summary["planned_new"] == 1
        assert summary["planned_upsert"] == 0


@pytest.mark.offline
def test_validation_detects_empty_doc_id_embedding_duplicate_dim_and_metadata_warnings(tmp_path: Path) -> None:
    records = [
        embedding_record("doc-a"),
        embedding_record("doc-a"),
        {**embedding_record(""), "doc_id": ""},
        {**embedding_record("doc-empty"), "embedding": []},
        {**embedding_record("doc-missing-meta"), "source_doc_id": "", "metadata": {}},
    ]

    summary = push_ingest_embeddings.analyze_records(records, existing_doc_ids=set(), force=False, skip_existing=True)

    assert summary["empty_doc_id"] == 1
    assert summary["empty_embedding"] == 1
    assert summary["duplicate_doc_ids"] == ["doc-a"]
    assert summary["embedding_dim_counts"][1024] >= 1
    assert any("missing source_doc_id" in item["warning"] for item in summary["warnings"])


@pytest.mark.offline
def test_existing_doc_id_skips_by_default() -> None:
    record = embedding_record("doc-a")

    summary = push_ingest_embeddings.analyze_records([record], existing_doc_ids={"doc-a"}, force=False, skip_existing=True)

    assert summary["planned_skip"] == 1
    assert summary["selected_documents"] == []


@pytest.mark.offline
def test_force_allows_upsert() -> None:
    record = embedding_record("doc-a")

    summary = push_ingest_embeddings.analyze_records([record], existing_doc_ids={"doc-a"}, force=True, skip_existing=True)

    assert summary["planned_upsert"] == 1
    assert len(summary["selected_documents"]) == 1


@pytest.mark.offline
def test_single_write_failure_continues() -> None:
    fake = FakeClient(fail_ids={"doc-b"})
    docs = [
        push_ingest_embeddings.build_push_document(embedding_record("doc-a"), 1),
        push_ingest_embeddings.build_push_document(embedding_record("doc-b"), 2),
        push_ingest_embeddings.build_push_document(embedding_record("doc-c"), 3),
    ]

    success, failures = push_ingest_embeddings.execute_push(fake, FakeModels, "inst_text_docs", docs)

    assert success == ["doc-a", "doc-c"]
    assert [failure["doc_id"] for failure in failures] == ["doc-b"]


@pytest.mark.offline
def test_report_and_manifest_are_written(tmp_path: Path) -> None:
    records = [embedding_record("doc-a")]
    summary = push_ingest_embeddings.analyze_records(records, existing_doc_ids=set(), force=False, skip_existing=True)
    report = tmp_path / "push_report.md"
    manifest = tmp_path / "manifest.jsonl"

    push_ingest_embeddings.write_report(
        report,
        mode="execute",
        table_name="inst_text_docs",
        summary=summary,
        success_doc_ids=["doc-a"],
        failures=[],
        elapsed_seconds=0.1,
        mapping_dim=1024,
        check_result={"connectable": True},
        source_type="product_excel",
    )
    push_ingest_embeddings.append_push_manifest(records, manifest, ["doc-a"], [], "inst_text_docs")

    assert "Ingest Embeddings Push Report" in report.read_text(encoding="utf-8")
    payload = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["pushed_doc_ids"] == ["doc-a"]
    assert payload["success_count"] == 1
    rows = read_jsonl(manifest)
    assert rows[0]["event_type"] == "push"
    assert rows[0]["pushed_doc_ids"] == ["doc-a"]


@pytest.mark.offline
def test_check_opensearch_uses_mock_client() -> None:
    result = push_ingest_embeddings.check_opensearch(FakeClient(), FakeModels, "inst_text_docs", {1024})

    assert result["connectable"] is True
    assert result["table_exists"] is True
    assert result["document_count"] == 12
    assert result["dim_match"] is True


@pytest.mark.offline
def test_check_opensearch_warns_when_mapping_dim_unknown() -> None:
    class UnknownDimClient(FakeClient):
        def get_table(self, table_name):
            return FakeResponse({"fields": [{"name": "source_text_vector"}], "count": 12})

    result = push_ingest_embeddings.check_opensearch(UnknownDimClient(), FakeModels, "inst_text_docs", {1024})

    assert result["mapping_vector_dim"] is None
    assert result["dim_match"] is False
    assert result["warnings"]


@pytest.mark.offline
def test_resolve_push_target_prefers_schema_verified_text_docs() -> None:
    table_name, checked = push_ingest_embeddings.resolve_push_target_table(FakeClient(), FakeModels, {"instance_id": "inst"})

    assert table_name == "text_docs"
    assert checked[0]["table_name"] == "text_docs"
    assert checked[0]["mapping_vector_dim"] == 1024


@pytest.mark.offline
def test_execute_safety_rejects_table_mismatch() -> None:
    with pytest.raises(RuntimeError, match="push target table does not match retrieval table"):
        push_ingest_embeddings.ensure_execute_safety(
            {
                "retrieval_table": "text_docs",
                "push_target_table": "inst_text_docs",
                "table_alignment_ok": False,
                "mapping_vector_dim": 1024,
                "dim_match": True,
            }
        )


@pytest.mark.offline
def test_execute_safety_rejects_unknown_mapping_dim() -> None:
    with pytest.raises(RuntimeError, match="mapping_dim unknown"):
        push_ingest_embeddings.ensure_execute_safety(
            {
                "retrieval_table": "text_docs",
                "push_target_table": "text_docs",
                "table_alignment_ok": True,
                "mapping_vector_dim": None,
                "dim_match": False,
            }
        )


def fetched_row(doc_id: str = "doc-a", *, vector_len: int = 1024, metadata=None) -> dict:
    return {
        "id": doc_id,
        "doc_id": doc_id,
        "source_text": "hello",
        "source_type": "product_excel",
        "source_doc_id": "source-a",
        "source_sha1": "sha-a",
        "product_id": "prod-a",
        "content_sha1": "content-a",
        "embedding_model": "text-embedding-v4",
        "embedding_dim": 1024,
        "source_text_vector": [0.1] * vector_len,
        "metadata": metadata if metadata is not None else {"doc_id": doc_id},
    }


@pytest.mark.offline
def test_verify_pushed_reads_push_report_json(monkeypatch, tmp_path: Path) -> None:
    report_json = tmp_path / "output/ingest_push/push_report.json"
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps({"pushed_doc_ids": ["doc-a"]}), encoding="utf-8")
    fake = FakeClient(rows={"doc-a": fetched_row("doc-a")})
    monkeypatch.setattr(push_ingest_embeddings, "ensure_runtime_config", lambda: {"instance_id": "inst", "endpoint": "https://x", "username": "u", "password": "p"})
    monkeypatch.setattr(push_ingest_embeddings, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "push_ingest_embeddings.py",
            "--root",
            str(tmp_path),
            "--verify-pushed",
        ],
    )

    assert push_ingest_embeddings.main() == 0
    verify_json = tmp_path / "output/ingest_push/verify_report.json"
    payload = json.loads(verify_json.read_text(encoding="utf-8"))
    assert payload["verified_doc_ids"] == ["doc-a"]
    assert payload["results"][0]["status"] == "found"


@pytest.mark.offline
def test_verify_doc_id_finds_and_missing_records(tmp_path: Path) -> None:
    fake = FakeClient(rows={"doc-a": fetched_row("doc-a")})

    payload = push_ingest_embeddings.verify_pushed_documents(
        fake,
        FakeModels,
        "inst_text_docs",
        ["doc-a", "doc-missing"],
    )

    assert payload["results"][0]["status"] == "found"
    assert payload["results"][1]["status"] == "missing"
    assert payload["missing_doc_ids"] == ["doc-missing"]


@pytest.mark.offline
def test_verify_warnings_for_missing_fields_and_dim_mismatch() -> None:
    bad = fetched_row("doc-b", vector_len=3, metadata="not-json")
    bad.pop("source_sha1")
    fake = FakeClient(rows={"doc-b": bad})

    payload = push_ingest_embeddings.verify_pushed_documents(fake, FakeModels, "inst_text_docs", ["doc-b"])

    warnings = payload["results"][0]["warnings"]
    assert any("missing source_sha1" in warning for warning in warnings)
    assert any("vector length 3 != embedding_dim 1024" in warning for warning in warnings)
    assert any("metadata is not valid JSON" in warning for warning in warnings)


@pytest.mark.offline
def test_verify_does_not_warn_when_fetch_omits_vector_or_metadata() -> None:
    row = fetched_row("doc-c")
    row.pop("source_text_vector")
    row.pop("metadata")
    fake = FakeClient(rows={"doc-c": row})

    payload = push_ingest_embeddings.verify_pushed_documents(fake, FakeModels, "inst_text_docs", ["doc-c"])

    warnings = payload["results"][0]["warnings"]
    assert not any("source_text_vector" in warning for warning in warnings)
    assert not any("metadata" in warning for warning in warnings)


@pytest.mark.offline
def test_verify_report_md_and_json_are_written(tmp_path: Path) -> None:
    payload = {
        "verify_status": "partial",
        "target_index": "inst_text_docs",
        "vector_field": "source_text_vector",
        "requested_doc_ids": ["doc-a"],
        "verified_doc_ids": [],
        "missing_doc_ids": ["doc-a"],
        "warning_count": 0,
        "results": [{"doc_id": "doc-a", "status": "missing", "warnings": []}],
        "verified_at": "2026-01-01T00:00:00+08:00",
    }
    report = tmp_path / "verify_report.md"

    push_ingest_embeddings.write_verify_report(report, payload)

    assert "Ingest Push Verify Report" in report.read_text(encoding="utf-8")
    assert json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))["missing_doc_ids"] == ["doc-a"]
