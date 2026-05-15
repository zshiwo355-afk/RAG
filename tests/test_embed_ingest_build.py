from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

import embed_ingest_build
from conftest import write_json, write_jsonl


def sample_doc(doc_id: str = "doc-a", text: str = "hello") -> dict:
    return {
        "page_content": text,
        "metadata": {
            "doc_id": doc_id,
            "product_id": "prod-a",
            "source_doc_id": "source-a",
            "source_sha1": "source-sha",
            "doc_type": "product_full",
            "field_name": "summary",
        },
    }


def content_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@pytest.mark.offline
def test_dry_run_does_not_call_embedding_api(monkeypatch, tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel/documents.json", [sample_doc()])

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("embedding API should not be called in dry-run")

    monkeypatch.setattr(embed_ingest_build, "embed_batch_with_retry", fail_if_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "embed_ingest_build.py",
            "--source-type",
            "product_excel",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
            "--dry-run",
        ],
    )

    assert embed_ingest_build.main() == 0
    assert not (output_dir / "product_excel/embeddings.jsonl").exists()


@pytest.mark.offline
def test_limit_three_only_plans_three(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel/documents.json", [sample_doc(f"doc-{idx}", f"text {idx}") for idx in range(5)])

    candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        ["product_excel"],
        embed_ingest_build.MODEL_NAME,
        limit=3,
        force=False,
    )

    assert len(candidates) == 3
    assert warnings == []
    assert stats["product_excel"]["to_generate"] == 3


@pytest.mark.offline
def test_product_excel_supplement_source_type_reads_supplement_documents(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel_supplement/documents.json", [sample_doc("supp-a", "supplement text")])

    source_types = embed_ingest_build.selected_source_types("product_excel_supplement")
    candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        source_types,
        embed_ingest_build.MODEL_NAME,
        limit=None,
        force=False,
    )

    assert source_types == ["product_excel_supplement"]
    assert warnings == []
    assert candidates[0]["source_type"] == "product_excel_supplement"
    assert stats["product_excel_supplement"]["documents_path"].endswith("product_excel_supplement/documents.json")
    assert stats["product_excel_supplement"]["output_path"].endswith("product_excel_supplement/embeddings.jsonl")


@pytest.mark.offline
def test_product_excel_supplement_dry_run_does_not_call_embedding_api(monkeypatch, tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel_supplement/documents.json", [sample_doc("supp-a", "supplement text")])

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("embedding API should not be called in dry-run")

    monkeypatch.setattr(embed_ingest_build, "embed_batch_with_retry", fail_if_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "embed_ingest_build.py",
            "--source-type",
            "product_excel_supplement",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
            "--dry-run",
        ],
    )

    assert embed_ingest_build.main() == 0
    assert not (output_dir / "product_excel_supplement/embeddings.jsonl").exists()


@pytest.mark.offline
def test_product_excel_supplement_srx_source_type_reads_srx_documents(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel_supplement_srx/documents.json", [sample_doc("supp-srx", "srx supplement text")])

    source_types = embed_ingest_build.selected_source_types("product_excel_supplement_srx")
    candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        source_types,
        embed_ingest_build.MODEL_NAME,
        limit=None,
        force=False,
    )

    assert source_types == ["product_excel_supplement_srx"]
    assert warnings == []
    assert candidates[0]["source_type"] == "product_excel_supplement_srx"
    assert stats["product_excel_supplement_srx"]["documents_path"].endswith("product_excel_supplement_srx/documents.json")
    assert stats["product_excel_supplement_srx"]["output_path"].endswith("product_excel_supplement_srx/embeddings.jsonl")


@pytest.mark.offline
def test_product_excel_supplement_srx_dry_run_does_not_call_embedding_api(monkeypatch, tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel_supplement_srx/documents.json", [sample_doc("supp-srx", "srx supplement text")])

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("embedding API should not be called in dry-run")

    monkeypatch.setattr(embed_ingest_build, "embed_batch_with_retry", fail_if_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "embed_ingest_build.py",
            "--source-type",
            "product_excel_supplement_srx",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
            "--dry-run",
        ],
    )

    assert embed_ingest_build.main() == 0
    assert not (output_dir / "product_excel_supplement_srx/embeddings.jsonl").exists()


@pytest.mark.offline
def test_product_excel_new_products_source_type_reads_new_product_documents(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel_new_products/documents.json", [sample_doc("new-prod-a", "new product text")])

    source_types = embed_ingest_build.selected_source_types("product_excel_new_products")
    candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        source_types,
        embed_ingest_build.MODEL_NAME,
        limit=None,
        force=False,
    )

    assert source_types == ["product_excel_new_products"]
    assert warnings == []
    assert candidates[0]["source_type"] == "product_excel_new_products"
    assert stats["product_excel_new_products"]["documents_path"].endswith("product_excel_new_products/documents.json")
    assert stats["product_excel_new_products"]["output_path"].endswith("product_excel_new_products/embeddings.jsonl")


@pytest.mark.offline
def test_final_49_source_types_are_supported_by_embedding_wrapper(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel_supplement_reviewed/documents.json", [sample_doc("supp-reviewed", "reviewed supplement")])
    write_json(input_dir / "product_excel_new_products_final/documents.json", [sample_doc("new-final", "new final")])

    for source_type in ["product_excel_supplement_reviewed", "product_excel_new_products_final"]:
        candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
            input_dir,
            output_dir,
            embed_ingest_build.selected_source_types(source_type),
            embed_ingest_build.MODEL_NAME,
            limit=None,
            force=False,
        )
        assert warnings == []
        assert candidates[0]["source_type"] == source_type
        assert stats[source_type]["documents_path"].endswith(f"{source_type}/documents.json")
        assert stats[source_type]["output_path"].endswith(f"{source_type}/embeddings.jsonl")


@pytest.mark.offline
def test_existing_embedding_is_skipped(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    text = "same text"
    write_json(input_dir / "product_excel/documents.json", [sample_doc("doc-a", text)])
    write_jsonl(
        output_dir / "product_excel/embeddings.jsonl",
        [
            {
                "doc_id": "doc-a",
                "content_sha1": content_sha1(text),
                "embedding_model": embed_ingest_build.MODEL_NAME,
                "embedding": [0.1],
            }
        ],
    )

    candidates, skipped, _warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        ["product_excel"],
        embed_ingest_build.MODEL_NAME,
        limit=None,
        force=False,
    )

    assert candidates == []
    assert len(skipped) == 1
    assert stats["product_excel"]["skipped_existing"] == 1


@pytest.mark.offline
def test_changed_content_is_detected(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel/documents.json", [sample_doc("doc-a", "new text")])
    write_jsonl(
        output_dir / "product_excel/embeddings.jsonl",
        [
            {
                "doc_id": "doc-a",
                "content_sha1": content_sha1("old text"),
                "embedding_model": embed_ingest_build.MODEL_NAME,
                "embedding": [0.1],
            }
        ],
    )

    candidates, _skipped, _warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        ["product_excel"],
        embed_ingest_build.MODEL_NAME,
        limit=None,
        force=False,
    )

    assert candidates[0]["embedding_plan_status"] == "changed"
    assert stats["product_excel"]["changed"] == 1


@pytest.mark.offline
def test_empty_text_is_skipped_with_warning(tmp_path: Path) -> None:
    input_dir = tmp_path / "ingest_build"
    output_dir = tmp_path / "embeddings"
    write_json(input_dir / "product_excel/documents.json", [sample_doc("doc-empty", "")])

    candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
        input_dir,
        output_dir,
        ["product_excel"],
        embed_ingest_build.MODEL_NAME,
        limit=None,
        force=False,
    )

    assert candidates == []
    assert warnings[0]["warning"] == "doc-empty text is empty"
    assert stats["product_excel"]["warnings"] == 1


@pytest.mark.offline
def test_status_prints_embedding_summary(tmp_path: Path, capsys) -> None:
    output_dir = tmp_path / "embeddings"
    write_jsonl(
        output_dir / "product_excel/embeddings.jsonl",
        [
            {
                "doc_id": "doc-a",
                "source_type": "product_excel",
                "embedding_model": embed_ingest_build.MODEL_NAME,
                "text": "hello",
                "embedding": [0.1, 0.2, 0.3],
            },
            {
                "doc_id": "doc-a",
                "source_type": "product_excel",
                "embedding_model": embed_ingest_build.MODEL_NAME,
                "text": "",
                "embedding": [],
            },
        ],
    )

    embed_ingest_build.print_embedding_status(output_dir, ["product_excel"])
    output = capsys.readouterr().out

    assert "total embeddings: 2" in output
    assert "embedding_dim distribution" in output
    assert "empty embedding: 1" in output
    assert "duplicate doc_id: 1" in output
