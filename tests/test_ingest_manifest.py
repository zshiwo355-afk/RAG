from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.ingest_manifest import (
    append_manifest_record,
    find_by_source_doc_id,
    find_by_source_sha1,
    load_manifest,
    summarize_manifest,
)


def base_record(**overrides):
    record = {
        "source_doc_id": "source-a",
        "source_path": "素材/a.xlsx",
        "source_sha1": "sha1-a",
        "source_type": "product_excel",
        "product_id": "prod-a",
        "generated_doc_ids": ["doc-a"],
        "embedding_model": None,
        "vector_tables": [],
        "status": "success",
    }
    record.update(overrides)
    return record


@pytest.mark.offline
def test_append_and_load_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    append_manifest_record(base_record(), path)

    records = load_manifest(path)

    assert len(records) == 1
    assert records[0]["source_doc_id"] == "source-a"
    assert records[0]["status"] == "success"


@pytest.mark.offline
def test_find_by_source_doc_id_and_sha1(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    append_manifest_record(base_record(), path)

    assert find_by_source_doc_id("source-a", path)[0]["source_sha1"] == "sha1-a"
    assert find_by_source_sha1("sha1-a", path)[0]["source_doc_id"] == "source-a"


@pytest.mark.offline
def test_summarize_success_failed_duplicate_and_changed(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    append_manifest_record(base_record(source_doc_id="source-a", source_sha1="same-sha", status="success"), path)
    append_manifest_record(base_record(source_doc_id="source-b", source_path="素材/b.xlsx", source_sha1="same-sha", status="failed"), path)
    append_manifest_record(base_record(source_doc_id="source-c", source_path="素材/c.xlsx", source_sha1="old-sha", status="success"), path)
    append_manifest_record(base_record(source_doc_id="source-c", source_path="素材/c.xlsx", source_sha1="new-sha", status="success"), path)

    summary = summarize_manifest(path)

    assert summary["status_counts"]["success"] == 3
    assert summary["status_counts"]["failed"] == 1
    assert "same-sha" in summary["duplicate_source_sha1"]
    assert "source-c" in summary["changed_source_doc_ids"]
