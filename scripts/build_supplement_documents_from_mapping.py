#!/usr/bin/env python3
"""Build local supplement documents from confirmed product identity mappings."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for path in [ROOT / "scripts", ROOT / "src"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from check_existing_product_overlap import compact, document_identity, load_documents, resolve_path, safe_text  # type: ignore  # noqa: E402


DEFAULT_DOCUMENTS = "output/ingest_build/product_excel/documents.json"
DEFAULT_MAPPING = "config/product_identity_map.csv"
DEFAULT_OUTPUT_DIR = "output/ingest_build/product_excel_supplement"
DEFAULT_ACCEPTANCE_MD = "output/acceptance/dmz_supplement_build_report.md"
DEFAULT_ACCEPTANCE_JSON = "output/acceptance/dmz_supplement_build_report.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_mapping(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        return [dict(row) for row in csv.DictReader(file_obj)]


def metadata_of(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def section_for(metadata: dict[str, Any]) -> str:
    field_name = compact(metadata.get("field_name"))
    if field_name:
        return field_name
    doc_type = compact(metadata.get("doc_type"))
    return doc_type or "section"


def text_of(document: dict[str, Any]) -> str:
    return compact(document.get("page_content") or document.get("text") or document.get("source_text"))


def safe_section(value: str) -> str:
    text = re.sub(r"[^\w.-]+", "_", value).strip("_")
    return text or "section"


def build_supplement_documents(
    documents: list[dict[str, Any]],
    mappings: list[dict[str, str]],
    doc_id_tag: str = "dmz_excel",
    supplement_reason: str = "大民族品牌产品信息表补充资料",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    confirmed = {row["local_product_id"]: row for row in mappings if compact(row.get("status")) == "confirmed" and compact(row.get("canonical_product_id"))}
    skipped_needs_review = [row for row in mappings if compact(row.get("status")) == "needs_review"]
    docs_by_local: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        identity = document_identity(document)
        local_product_id = identity.get("product_id")
        if local_product_id in confirmed:
            docs_by_local.setdefault(local_product_id, []).append(document)

    supplement_docs: list[dict[str, Any]] = []
    seen_doc_ids: set[str] = set()
    old_doc_ids = {compact(metadata_of(document).get("doc_id")) for document in documents}
    conflicts: list[str] = []
    for local_product_id, local_docs in docs_by_local.items():
        mapping = confirmed[local_product_id]
        canonical_product_id = compact(mapping.get("canonical_product_id"))
        for index, document in enumerate(local_docs, start=1):
            metadata = metadata_of(document)
            identity = document_identity(document)
            source_sha1 = identity.get("source_sha1")
            source_sha1_short = source_sha1[:12]
            section = safe_section(section_for(metadata))
            local_key = safe_section(local_product_id)
            doc_id = f"supp__{canonical_product_id}__{safe_section(doc_id_tag)}__{source_sha1_short}__{local_key}__{section}__{index:03d}"
            if doc_id in old_doc_ids or doc_id in seen_doc_ids:
                conflicts.append(doc_id)
                continue
            seen_doc_ids.add(doc_id)
            new_metadata = dict(metadata)
            new_metadata.update(
                {
                    "doc_id": doc_id,
                    "product_id": canonical_product_id,
                    "canonical_product_id": canonical_product_id,
                    "local_product_id": local_product_id,
                    "doc_type": "supplement",
                    "source_type": "product_excel",
                    "source_doc_id": identity.get("source_doc_id"),
                    "source_sha1": source_sha1,
                    "source_path": identity.get("source_path"),
                    "source_file": identity.get("source_path"),
                    "is_supplement": True,
                    "supplement_of_doc_ids": [],
                    "supplement_reason": supplement_reason,
                    "original_doc_id": compact(metadata.get("doc_id")),
                    "original_doc_type": compact(metadata.get("doc_type")),
                    "original_field_name": compact(metadata.get("field_name")),
                }
            )
            supplement_docs.append(
                {
                    "doc_id": doc_id,
                    "product_id": canonical_product_id,
                    "canonical_product_id": canonical_product_id,
                    "local_product_id": local_product_id,
                    "doc_type": "supplement",
                    "source_type": "product_excel",
                    "source_doc_id": identity.get("source_doc_id"),
                    "source_sha1": source_sha1,
                    "source_path": identity.get("source_path"),
                    "is_supplement": True,
                    "supplement_of_doc_ids": [],
                    "supplement_reason": supplement_reason,
                    "text": text_of(document),
                    "page_content": text_of(document),
                    "metadata": new_metadata,
                }
            )
    stats = {
        "confirmed_mapping_count": len(confirmed),
        "needs_review_mapping_count": len(skipped_needs_review),
        "local_products_with_supplements": len(docs_by_local),
        "supplement_document_count": len(supplement_docs),
        "doc_id_conflicts": conflicts,
        "doc_type_counts": dict(Counter(doc["metadata"].get("original_doc_type") for doc in supplement_docs)),
    }
    return supplement_docs, stats


def write_reports(
    output_dir: Path,
    acceptance_md: Path,
    acceptance_json: Path,
    docs: list[dict[str, Any]],
    stats: dict[str, Any],
    source_name: str = "大民族",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    docs_path = output_dir / "documents.json"
    report_path = output_dir / "build_report.md"
    docs_path.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {**stats, "documents_path": str(docs_path), "created_at": now_iso()}
    acceptance_json.parent.mkdir(parents=True, exist_ok=True)
    acceptance_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# {source_name} Supplement Documents Build Report",
        "",
        f"- confirmed mappings: {stats['confirmed_mapping_count']}",
        f"- needs_review mappings skipped: {stats['needs_review_mapping_count']}",
        f"- local products with supplements: {stats['local_products_with_supplements']}",
        f"- supplement documents: {stats['supplement_document_count']}",
        f"- doc_id conflicts: {len(stats['doc_id_conflicts'])}",
        f"- documents: `{docs_path}`",
    ]
    text = "\n".join(lines) + "\n"
    report_path.write_text(text, encoding="utf-8")
    acceptance_md.write_text(text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local supplement documents from confirmed mapping CSV.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--documents", default=DEFAULT_DOCUMENTS)
    parser.add_argument("--mapping", default=DEFAULT_MAPPING)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--acceptance-report", default=DEFAULT_ACCEPTANCE_MD)
    parser.add_argument("--acceptance-json", default=DEFAULT_ACCEPTANCE_JSON)
    parser.add_argument("--report-output", dest="acceptance_report")
    parser.add_argument("--json-output", dest="acceptance_json")
    parser.add_argument("--source-name", default="大民族")
    parser.add_argument("--doc-id-tag", default="dmz_excel")
    parser.add_argument("--supplement-reason", default="大民族品牌产品信息表补充资料")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    docs = load_documents(resolve_path(root, args.documents))
    mappings = load_mapping(resolve_path(root, args.mapping))
    supplement_docs, stats = build_supplement_documents(docs, mappings, args.doc_id_tag, args.supplement_reason)
    write_reports(
        resolve_path(root, args.output_dir),
        resolve_path(root, args.acceptance_report),
        resolve_path(root, args.acceptance_json),
        supplement_docs,
        stats,
        args.source_name,
    )
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
