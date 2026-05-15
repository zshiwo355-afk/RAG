from __future__ import annotations

from pathlib import Path

import pytest

import build_product_excel_docs
import embed_ingest_build
import ingest_content
import plan_ingest
from rag_app.ingest_manifest import append_manifest_record, load_manifest
from conftest import write_json


@pytest.mark.offline
def test_minimal_offline_ingest_loop(temp_project: Path, capsys) -> None:
    source = temp_project / "素材/sample_product.xlsx"

    plan = plan_ingest.build_plan(temp_project, [source])
    assert len(plan) == 1

    manifest_path = temp_project / "output/ingest_manifest.jsonl"
    annotated = ingest_content.annotate_plan_with_manifest(plan, load_manifest(manifest_path))
    assert annotated[0]["suggested_action"] == "new"

    append_manifest_record(
        {
            "source_doc_id": annotated[0]["source_doc_id"],
            "source_path": annotated[0]["source_path"],
            "source_sha1": annotated[0]["source_sha1"],
            "source_type": annotated[0]["source_type"],
            "status": "success",
        },
        manifest_path,
    )
    annotated_again = ingest_content.annotate_plan_with_manifest(plan, load_manifest(manifest_path))
    assert annotated_again[0]["suggested_action"] == "unchanged"

    rel_source = plan[0]["source_path"]
    write_json(
        temp_project / "output/documents_preview_v2.json",
        [
            {
                "page_content": "offline e2e document",
                "metadata": {
                    "doc_id": "doc-e2e",
                    "product_id": "prod-e2e",
                    "source_doc_id": "source-e2e",
                    "source_sha1": "sha-e2e",
                    "source_file": rel_source,
                },
            }
        ],
    )
    docs_by_source = build_product_excel_docs.load_documents_by_source(temp_project / "output/documents_preview_v2.json")
    standard_docs = docs_by_source[rel_source]
    write_json(temp_project / "output/ingest_build/product_excel/documents.json", standard_docs)

    candidates, _skipped, warnings, stats = embed_ingest_build.load_candidates(
        temp_project / "output/ingest_build",
        temp_project / "output/ingest_embeddings",
        ["product_excel"],
        embed_ingest_build.MODEL_NAME,
        limit=3,
        force=False,
    )

    assert warnings == []
    assert len(candidates) == 1
    assert stats["product_excel"]["to_generate"] == 1

    embed_ingest_build.print_embedding_status(temp_project / "output/ingest_embeddings", ["product_excel"])
    assert "total embeddings: 0" in capsys.readouterr().out
