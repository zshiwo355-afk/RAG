from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

import pytest

import build_supplement_documents_from_mapping as supp
from conftest import write_json


def source_doc(local_id: str = "local-a", doc_id: str = "local-a__product_full") -> dict:
    return {
        "page_content": "产品名称：测试酒\n包装：礼盒",
        "metadata": {
            "doc_id": doc_id,
            "product_id": local_id,
            "product_name": "测试酒",
            "doc_type": "product_full",
            "source_doc_id": "product_excel__sha",
            "source_sha1": "sha",
            "source_file": "大民族品牌产品信息表.xlsx",
            "source_path": "大民族品牌产品信息表.xlsx",
        },
    }


def write_mapping(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source_name",
        "source_doc_id",
        "source_sha1",
        "local_product_id",
        "local_product_name",
        "local_spec",
        "local_manufacturer",
        "local_barcode",
        "canonical_product_id",
        "canonical_product_name",
        "canonical_spec",
        "match_method",
        "confidence",
        "status",
        "notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"local_product_id": "local-a", "canonical_product_id": "online-a", "status": "confirmed"})
        writer.writerow({"local_product_id": "local-b", "canonical_product_id": "online-b", "status": "needs_review"})


@pytest.mark.offline
def test_confirmed_only_generates_supplement_documents(tmp_path: Path) -> None:
    docs = [source_doc("local-a"), source_doc("local-b", "local-b__product_full")]
    mapping_path = tmp_path / "config/product_identity_map.csv"
    write_mapping(mapping_path)

    supplement_docs, stats = supp.build_supplement_documents(docs, supp.load_mapping(mapping_path))

    assert stats["confirmed_mapping_count"] == 1
    assert stats["needs_review_mapping_count"] == 1
    assert len(supplement_docs) == 1
    doc = supplement_docs[0]
    assert doc["product_id"] == "online-a"
    assert doc["canonical_product_id"] == "online-a"
    assert doc["local_product_id"] == "local-a"
    assert doc["doc_type"] == "supplement"
    assert doc["source_doc_id"] == "product_excel__sha"
    assert doc["metadata"]["is_supplement"] is True
    assert doc["doc_id"] != "local-a__product_full"


@pytest.mark.offline
def test_main_writes_reports_without_embedding_or_opensearch(monkeypatch, tmp_path: Path) -> None:
    docs_path = tmp_path / "documents.json"
    write_json(docs_path, [source_doc("local-a")])
    mapping_path = tmp_path / "config/product_identity_map.csv"
    write_mapping(mapping_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_supplement_documents_from_mapping.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs_path),
            "--mapping",
            str(mapping_path),
        ],
    )

    assert supp.main() == 0
    output = json.loads((tmp_path / "output/ingest_build/product_excel_supplement/documents.json").read_text(encoding="utf-8"))
    assert output[0]["doc_type"] == "supplement"


@pytest.mark.offline
def test_custom_supplement_paths_and_reason(monkeypatch, tmp_path: Path) -> None:
    docs_path = tmp_path / "documents.json"
    write_json(docs_path, [source_doc("local-a")])
    mapping_path = tmp_path / "config/product_identity_map_srx.csv"
    write_mapping(mapping_path)
    output_dir = tmp_path / "output/ingest_build/product_excel_supplement_srx"
    report = tmp_path / "output/acceptance/srx_supplement_build_report.md"
    payload = tmp_path / "output/acceptance/srx_supplement_build_report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_supplement_documents_from_mapping.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs_path),
            "--mapping",
            str(mapping_path),
            "--output-dir",
            str(output_dir),
            "--report-output",
            str(report),
            "--json-output",
            str(payload),
            "--source-name",
            "石荣霄",
            "--doc-id-tag",
            "srx_excel",
            "--supplement-reason",
            "石荣霄品牌产品信息表补充资料",
        ],
    )

    assert supp.main() == 0
    output = json.loads((output_dir / "documents.json").read_text(encoding="utf-8"))
    assert "__srx_excel__" in output[0]["doc_id"]
    assert output[0]["supplement_reason"] == "石荣霄品牌产品信息表补充资料"
    assert report.exists()
    assert payload.exists()


@pytest.mark.offline
def test_doc_id_includes_local_product_to_avoid_same_canonical_conflicts(tmp_path: Path) -> None:
    docs = [
        source_doc("local-a", "local-a__product_full"),
        source_doc("local-c", "local-c__product_full"),
    ]
    mapping_path = tmp_path / "config/product_identity_map.csv"
    write_mapping(mapping_path)
    with mapping_path.open("a", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(
            file_obj,
            fieldnames=[
                "source_name",
                "source_doc_id",
                "source_sha1",
                "local_product_id",
                "local_product_name",
                "local_spec",
                "local_manufacturer",
                "local_barcode",
                "canonical_product_id",
                "canonical_product_name",
                "canonical_spec",
                "match_method",
                "confidence",
                "status",
                "notes",
            ],
        )
        writer.writerow({"local_product_id": "local-c", "canonical_product_id": "online-a", "status": "confirmed"})

    supplement_docs, stats = supp.build_supplement_documents(docs, supp.load_mapping(mapping_path), "srx_excel", "补充资料")

    assert stats["doc_id_conflicts"] == []
    assert len(supplement_docs) == 2
    assert supplement_docs[0]["doc_id"] != supplement_docs[1]["doc_id"]
    assert "__local-a__" in supplement_docs[0]["doc_id"]
    assert "__local-c__" in supplement_docs[1]["doc_id"]
