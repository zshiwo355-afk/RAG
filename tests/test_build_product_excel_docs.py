from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import build_documents
import build_product_excel_docs
from conftest import read_jsonl, write_json


@pytest.mark.offline
def test_prefers_documents_preview_v2(monkeypatch, temp_project: Path) -> None:
    rel_source = "素材/sample_product.xlsx"
    write_json(
        temp_project / "output/documents_preview_v2.json",
        [
            {
                "page_content": "preview document",
                "metadata": {
                    "doc_id": "doc-preview",
                    "source_file": rel_source,
                    "source_doc_id": "source-preview",
                    "source_sha1": "sha-preview",
                    "product_id": "prod-preview",
                },
            }
        ],
    )

    output_dir = temp_project / "output/ingest_build/product_excel"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_product_excel_docs.py",
            "--root",
            str(temp_project),
            "--source",
            rel_source,
            "--output-dir",
            str(output_dir.relative_to(temp_project)),
        ],
    )

    assert build_product_excel_docs.main() == 0

    docs = json.loads((output_dir / "documents.json").read_text(encoding="utf-8"))
    source_sha1 = build_product_excel_docs.sha1_file(temp_project / rel_source)
    assert docs[0]["metadata"]["doc_id"] == "doc-preview"
    assert docs[0]["metadata"]["source_doc_id"] == f"product_excel__{source_sha1}"
    assert docs[0]["metadata"]["source_sha1"] == source_sha1
    assert docs[0]["metadata"]["product_id"] == "prod-preview"
    assert docs[0]["metadata"]["legacy_product_source_doc_id"] == "source-preview"
    assert docs[0]["metadata"]["source_path"] == rel_source
    assert docs[0]["metadata"]["source_type"] == "product_excel"


@pytest.mark.offline
def test_falls_back_to_products_enriched(temp_project: Path) -> None:
    rel_source = "素材/sample_product.xlsx"
    write_json(
        temp_project / "output/products_enriched.json",
        [
            {
                "product_id": "prod-a",
                "product_name": "测试酒A",
                "brand": "测试品牌",
                "source_file": rel_source,
                "source_sheet": "产品",
                "source_row": 2,
                "source_sha1": "sha-source",
            }
        ],
    )
    output_dir = temp_project / "output/ingest_build/product_excel"

    docs_by_source = build_product_excel_docs.load_documents_by_source(temp_project / "missing.json")
    products_by_source = build_product_excel_docs.load_products_by_source(temp_project / "output/products_enriched.json")
    assert docs_by_source == {}
    assert rel_source in products_by_source

    document = build_product_excel_docs.product_to_document(products_by_source[rel_source][0])
    assert document["metadata"]["doc_id"]
    assert document["metadata"]["source_doc_id"] == "product_excel__sha-source"
    assert document["metadata"]["source_sha1"] == "sha-source"
    assert document["metadata"]["source_file"] == rel_source
    assert document["metadata"]["source_path"] == rel_source
    assert document["metadata"]["source_type"] == "product_excel"
    assert document["metadata"]["product_id"] == "prod-a"
    assert document["metadata"]["product_source_doc_id"] == "product__prod-a"

    write_json(output_dir / "documents.json", [document])
    generated_doc_ids = [item["metadata"]["doc_id"] for item in json.loads((output_dir / "documents.json").read_text(encoding="utf-8"))]
    assert generated_doc_ids == ["prod-a__ingest_build_source_row"]


@pytest.mark.offline
def test_build_documents_uses_source_file_doc_id_for_product_excel_metadata() -> None:
    record = {
        "product_name": "测试酒A",
        "source_file": "大民族品牌产品信息表.xlsx",
        "source_sheet": "产品",
        "source_row": 45,
        "source_sha1": "row-level-sha",
        "source_file_sha1": "d1803d262c93fe33fe9b854ae6331148318943be",
    }

    metadata = build_documents.make_metadata(
        record,
        "prod_0052_大民族品牌产品信息表_r45",
        "prod_0052_大民族品牌产品信息表_r45__basic_info",
        "product_field",
        field_name="basic_info",
    )

    assert metadata["source_doc_id"] == "product_excel__d1803d262c93fe33fe9b854ae6331148318943be"
    assert metadata["source_sha1"] == "d1803d262c93fe33fe9b854ae6331148318943be"
    assert metadata["source_file_sha1"] == "d1803d262c93fe33fe9b854ae6331148318943be"
    assert metadata["record_source_sha1"] == "row-level-sha"
    assert metadata["source_path"] == "大民族品牌产品信息表.xlsx"
    assert metadata["product_source_doc_id"] == "product__prod_0052_大民族品牌产品信息表_r45__basic_info"
    assert metadata["product_doc_group_id"] == "product__prod_0052_大民族品牌产品信息表_r45"
    assert metadata["source_type"] == "product_excel"


@pytest.mark.offline
def test_unrecognized_excel_generates_needs_mapping(temp_project: Path) -> None:
    rows = build_product_excel_docs.build_needs_mapping_rows(
        temp_project,
        temp_project / "素材/sample_product.xlsx",
        max_preview_rows=2,
    )
    path = temp_project / "output/ingest_build/product_excel/needs_mapping.jsonl"
    build_product_excel_docs.write_jsonl(path, rows)

    loaded = read_jsonl(path)

    assert loaded
    assert loaded[0]["status"] == "needs_mapping"
    assert loaded[0]["source_doc_id"]
    assert loaded[0]["product_id"] == "unknown"
