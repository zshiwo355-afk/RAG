from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import check_existing_product_overlap as overlap
from conftest import write_json


class FakeTextQuery:
    def __init__(self, query_string, query_params=None):
        self.query_string = query_string
        self.query_params = query_params or {}


class FakeSearchRequest:
    def __init__(self, table_name, size, output_fields, text):
        self.table_name = table_name
        self.size = size
        self.output_fields = output_fields
        self.text = text


class FakeModels:
    TextQuery = FakeTextQuery
    SearchRequest = FakeSearchRequest


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.search_calls = []

    def search(self, request):
        self.search_calls.append(request)
        return FakeResponse({"result": [{"fields": row} for row in self.rows]})


def document(
    doc_id: str = "doc-a",
    *,
    source_doc_id: str = "source-a",
    product_id: str = "prod-a",
    product_name: str = "测试酒A",
    spec: str = "500ml",
    barcode: str = "",
) -> dict:
    return {
        "page_content": f"产品名称：{product_name}\n规格：{spec}",
        "metadata": {
            "doc_id": doc_id,
            "source_doc_id": source_doc_id,
            "source_sha1": "sha-a",
            "source_file": "sample.xlsx",
            "product_id": product_id,
            "product_name": product_name,
            "spec": spec,
            "barcode": barcode,
        },
    }


@pytest.mark.offline
def test_dry_run_does_not_connect_opensearch(monkeypatch, tmp_path: Path) -> None:
    docs = tmp_path / "documents.json"
    write_json(docs, [document()])

    def fail_create(_config):
        raise AssertionError("OpenSearch client should not be created in dry-run")

    monkeypatch.setattr(overlap, "create_client", fail_create)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_existing_product_overlap.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs),
            "--source-doc-id",
            "source-a",
            "--dry-run",
            "--limit",
            "20",
        ],
    )

    assert overlap.main() == 0


@pytest.mark.offline
def test_load_filter_and_extract_matching_fields(tmp_path: Path) -> None:
    docs_path = tmp_path / "documents.json"
    write_json(
        docs_path,
        [
            document("doc-a", source_doc_id="source-a", barcode="6901234567890"),
            document("doc-b", source_doc_id="source-b"),
        ],
    )

    docs = overlap.load_documents(docs_path)
    selected = overlap.select_documents(docs, "source-a", limit=20, include_all=False)
    identity = overlap.document_identity(selected[0])

    assert len(selected) == 1
    assert identity["product_id"] == "prod-a"
    assert identity["product_name"] == "测试酒A"
    assert identity["barcode"] == "6901234567890"
    assert identity["spec"] == "500ml"


@pytest.mark.offline
def test_no_match_classifies_new_product() -> None:
    result = overlap.build_result(overlap.document_identity(document()), [])

    assert result["classification"] == "new_product"
    assert result["risk_level"] == "low"
    assert result["needs_review"] is False


@pytest.mark.offline
def test_strong_match_without_conflict_classifies_supplement() -> None:
    item = overlap.document_identity(document())
    result = overlap.build_result(
        item,
        [
            {
                "doc_id": "old-a",
                "product_id": "prod-a",
                "product_name": "测试酒A",
                "spec": "500ml",
            }
        ],
    )

    assert result["classification"] == "supplement_existing_product"
    assert result["matched_existing_doc_ids"] == ["old-a"]
    assert result["needs_review"] is False


@pytest.mark.offline
def test_strong_match_with_conflict_classifies_conflict() -> None:
    item = overlap.document_identity(document(spec="500ml"))
    result = overlap.build_result(
        item,
        [
            {
                "doc_id": "old-a",
                "product_id": "prod-a",
                "product_name": "测试酒A",
                "spec": "100ml",
            }
        ],
    )

    assert result["classification"] == "conflict_existing_product"
    assert result["needs_review"] is True
    assert result["conflict_fields"]


@pytest.mark.offline
def test_weak_match_classifies_ambiguous() -> None:
    item = overlap.document_identity(document(product_id="prod-new", product_name="测试酒A"))
    result = overlap.build_result(
        item,
        [
            {
                "doc_id": "old-a",
                "product_id": "prod-old",
                "product_name": "测试酒B",
                "spec": "500ml",
            }
        ],
    )

    assert result["classification"] == "ambiguous"
    assert result["needs_review"] is True


@pytest.mark.offline
def test_execute_uses_mock_opensearch_client(monkeypatch, tmp_path: Path) -> None:
    docs = tmp_path / "documents.json"
    write_json(docs, [document()])
    fake = FakeClient(
        rows=[
            {
                "doc_id": "old-a",
                "product_id": "prod-a",
                "product_name": "测试酒A",
                "spec": "500ml",
            }
        ]
    )

    monkeypatch.setattr(overlap, "load_env", lambda _root: None)
    monkeypatch.setattr(overlap, "ensure_runtime_config", lambda: {"instance_id": "inst"})
    monkeypatch.setattr(overlap, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_existing_product_overlap.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs),
            "--source-doc-id",
            "source-a",
            "--execute",
            "--limit",
            "20",
        ],
    )

    assert overlap.main() == 0
    payload = json.loads((tmp_path / "output/acceptance/dmz_existing_overlap_report.json").read_text(encoding="utf-8"))
    assert payload["classification_counts"]["supplement_existing_product"] == 1
    assert fake.search_calls


@pytest.mark.offline
def test_markdown_json_and_strategy_reports_are_generated(tmp_path: Path) -> None:
    payload = overlap.build_payload(
        mode="execute",
        documents_path=tmp_path / "documents.json",
        source_doc_id="source-a",
        selected_items=[overlap.document_identity(document())],
        results=[overlap.build_result(overlap.document_identity(document()), [])],
        connection_success=True,
        table_name="inst_text_docs",
        warnings=[],
    )
    json_path = tmp_path / "overlap.json"
    md_path = tmp_path / "overlap.md"
    strategy_json = tmp_path / "strategy.json"
    strategy_md = tmp_path / "strategy.md"

    overlap.write_overlap_report(json_path, md_path, payload)
    overlap.write_strategy(strategy_json, strategy_md, overlap.build_strategy(payload))

    assert json_path.exists()
    assert "OpenSearch" in md_path.read_text(encoding="utf-8")
    assert strategy_json.exists()
    assert "补充入库策略" in strategy_md.read_text(encoding="utf-8")
