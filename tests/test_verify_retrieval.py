from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import verify_retrieval
from conftest import read_jsonl


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeModels:
    class FetchRequest:
        def __init__(self, table_name, ids, include_vector=False, output_fields=None):
            self.table_name = table_name
            self.ids = ids


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def fetch(self, request):
        return FakeResponse(
            {
                "result": [
                    {"fields": self.rows[doc_id]}
                    for doc_id in request.ids
                    if doc_id in self.rows
                ]
            }
        )


def fetched_doc(doc_id: str = "doc-a", text: str = "测试产品 酱香 包装") -> dict:
    return {
        "id": doc_id,
        "doc_id": doc_id,
        "source_text": text,
        "source_type": "product_excel",
        "source_doc_id": "source-a",
        "source_sha1": "sha-a",
        "product_id": "prod-a",
        "content_sha1": "content-a",
        "embedding_model": "text-embedding-v4",
        "embedding_dim": 1024,
        "source_text_vector": [0.1] * 1024,
        "metadata": {"doc_id": doc_id, "product_name": "测试产品"},
    }


@pytest.mark.offline
def test_dry_run_does_not_call_retrieval(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "output/ingest_push/push_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"pushed_doc_ids": ["doc-a"]}), encoding="utf-8")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("retrieval should not be called in dry-run")

    monkeypatch.setattr(verify_retrieval.retrieval_verify, "search", fail_if_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_retrieval.py",
            "--root",
            str(tmp_path),
            "--from-push-report",
            str(report),
            "--dry-run",
        ],
    )

    assert verify_retrieval.main() == 0
    payload = json.loads((tmp_path / "output/ingest_push/retrieval_verify_report.json").read_text(encoding="utf-8"))
    assert payload["planned_doc_ids"] == ["doc-a"]


@pytest.mark.offline
def test_doc_id_and_push_report_selection(tmp_path: Path) -> None:
    report = tmp_path / "push_report.json"
    report.write_text(json.dumps({"pushed_doc_ids": ["doc-a", "doc-b"]}), encoding="utf-8")

    assert verify_retrieval.selected_doc_ids([], report, 1) == ["doc-a"]
    assert verify_retrieval.selected_doc_ids(["manual"], report, 10) == ["manual"]


@pytest.mark.offline
def test_auto_query_from_text_and_metadata() -> None:
    assert verify_retrieval.make_auto_query(fetched_doc(text="  第一行  第二行  ")).startswith("第一行")
    assert verify_retrieval.make_auto_query({"doc_id": "doc-meta", "metadata": {"product_name": "元数据产品"}}) == "元数据产品"


@pytest.mark.offline
def test_doc_verification_hit_and_rank(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        verify_retrieval.retrieval_verify,
        "search",
        lambda query, top_k, root: [
            {"doc_id": "other", "score": 0.5, "source_text": "other"},
            {"doc_id": "doc-a", "score": 0.9, "source_text": "target", "product_id": "prod-a"},
        ],
    )

    results = verify_retrieval.verify_doc_queries(
        doc_ids=["doc-a"],
        fetched_docs={"doc-a": fetched_doc("doc-a")},
        top_k=5,
        root=tmp_path,
    )

    assert results[0]["hit"] is True
    assert results[0]["rank"] == 2


@pytest.mark.offline
def test_doc_verification_miss(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        verify_retrieval.retrieval_verify,
        "search",
        lambda query, top_k, root: [{"doc_id": "other", "score": 0.5, "source_text": "other"}],
    )

    results = verify_retrieval.verify_doc_queries(
        doc_ids=["doc-a"],
        fetched_docs={"doc-a": fetched_doc("doc-a")},
        top_k=5,
        root=tmp_path,
    )

    assert results[0]["hit"] is False
    assert results[0]["rank"] is None


@pytest.mark.offline
def test_manual_query_outputs_top_k(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        verify_retrieval.retrieval_verify,
        "search",
        lambda query, top_k, root: [
            {
                "doc_id": "doc-a",
                "score": 0.9,
                "source_type": "product_excel",
                "product_id": "prod-a",
                "source_doc_id": "source-a",
                "source_text": "测试产品",
            }
        ],
    )

    results = verify_retrieval.verify_manual_queries(["测试产品"], top_k=5, root=tmp_path)

    assert results[0]["query"] == "测试产品"
    assert results[0]["top_k_results"][0]["doc_id"] == "doc-a"


@pytest.mark.offline
def test_reports_and_manifest_are_written(tmp_path: Path) -> None:
    payload = verify_retrieval.build_payload(
        mode="execute",
        doc_results=[
            {
                "doc_id": "doc-a",
                "auto_query": "测试产品",
                "hit": True,
                "rank": 1,
                "top_k_doc_ids": ["doc-a"],
                "top_k_scores": [0.9],
            }
        ],
        manual_results=[],
        top_k=5,
        warnings=[],
    )
    md = tmp_path / "retrieval_verify_report.md"
    js = tmp_path / "retrieval_verify_report.json"
    manifest = tmp_path / "manifest.jsonl"

    verify_retrieval.write_report(md, js, payload)
    verify_retrieval.append_manifest_event(payload, manifest)

    assert "Retrieval Verify Report" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["hit_doc_ids"] == ["doc-a"]
    rows = read_jsonl(manifest)
    assert rows[0]["event_type"] == "verify_retrieval"
    assert rows[0]["retrieval_status"] == "success"


@pytest.mark.offline
def test_execute_uses_mock_opensearch_and_retriever(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "output/ingest_push/push_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"pushed_doc_ids": ["doc-a"]}), encoding="utf-8")
    fake = FakeClient(rows={"doc-a": fetched_doc("doc-a")})

    monkeypatch.setattr(verify_retrieval, "ensure_runtime_config", lambda: {"instance_id": "inst", "endpoint": "https://x", "username": "u", "password": "p"})
    monkeypatch.setattr(verify_retrieval, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(verify_retrieval, "confirm_or_exit", lambda yes: None)
    monkeypatch.setattr(
        verify_retrieval.retrieval_verify,
        "search",
        lambda query, top_k, root: [{"doc_id": "doc-a", "score": 0.9, "source_text": "target", "product_id": "prod-a"}],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_retrieval.py",
            "--root",
            str(tmp_path),
            "--from-push-report",
            str(report),
            "--execute",
            "--verify-limit",
            "1",
        ],
    )

    assert verify_retrieval.main() == 0
    payload = json.loads((tmp_path / "output/ingest_push/retrieval_verify_report.json").read_text(encoding="utf-8"))
    assert payload["hit_doc_ids"] == ["doc-a"]
