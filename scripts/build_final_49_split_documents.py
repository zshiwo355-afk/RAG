#!/usr/bin/env python3
"""Build final split documents for reviewed 49 products.

This script is local-only: it reads review outputs and ingest_build documents,
then writes two isolated build directories:
- product_excel_supplement_reviewed
- product_excel_new_products_final
"""

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

from check_existing_product_overlap import compact, document_identity, load_documents, safe_text  # type: ignore  # noqa: E402


DEFAULT_RECHECK = "output/review/new_product_recheck_final.json"
DEFAULT_DMZ_DOCUMENTS = "output/ingest_build/product_excel/documents.json"
DEFAULT_SRX_DOCUMENTS = "output/ingest_build/product_excel_srx/documents.json"
DEFAULT_SUPP_DIR = "output/ingest_build/product_excel_supplement_reviewed"
DEFAULT_NEW_DIR = "output/ingest_build/product_excel_new_products_final"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def safe_slug(value: str) -> str:
    text = re.sub(r"[^\w.-]+", "_", safe_text(value)).strip("_")
    return text or "item"


def metadata_of(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def text_of(document: dict[str, Any]) -> str:
    return compact(document.get("page_content") or document.get("text") or document.get("source_text"))


def section_for(document: dict[str, Any]) -> str:
    metadata = metadata_of(document)
    return safe_slug(safe_text(metadata.get("field_name") or metadata.get("doc_type") or document.get("doc_type") or "section"))


def first_reliable_hit(row: dict[str, Any]) -> dict[str, Any]:
    for query_result in row.get("query_results") or []:
        if not query_result.get("reliable"):
            continue
        for hit in query_result.get("hits") or []:
            product_id = safe_text(hit.get("product_id"))
            if product_id:
                return dict(hit)
    return {}


def split_rows(recheck_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    supplement_rows: list[dict[str, Any]] = []
    new_rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row in recheck_payload.get("results") or []:
        row = dict(row)
        if row.get("should_be_supplement"):
            hit = first_reliable_hit(row)
            if not safe_text(hit.get("product_id")):
                missing.append(row)
                continue
            row["matched_canonical_product_id"] = safe_text(hit.get("product_id"))
            row["matched_canonical_product_name"] = safe_text(hit.get("product_name")) or safe_text(row.get("local_product_name"))
            row["matched_doc_ids"] = [
                safe_text(hit_item.get("doc_id"))
                for query_result in row.get("query_results") or []
                if query_result.get("reliable")
                for hit_item in (query_result.get("hits") or [])
                if safe_text(hit_item.get("doc_id"))
            ]
            supplement_rows.append(row)
        else:
            new_rows.append(row)
    return supplement_rows, new_rows, missing


def docs_by_local_product(documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        local_product_id = document_identity(document).get("product_id")
        if local_product_id:
            grouped.setdefault(local_product_id, []).append(document)
    return grouped


def build_supplement_docs(rows: list[dict[str, Any]], grouped_docs: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    conflicts: list[str] = []
    missing_local: list[str] = []
    for row in rows:
        local_product_id = safe_text(row.get("local_product_id"))
        canonical_product_id = safe_text(row.get("matched_canonical_product_id"))
        local_docs = grouped_docs.get(local_product_id) or []
        if not local_docs:
            missing_local.append(local_product_id)
            continue
        for index, document in enumerate(local_docs, start=1):
            identity = document_identity(document)
            metadata = metadata_of(document)
            source_sha1 = identity.get("source_sha1")
            doc_id = (
                f"supp__{canonical_product_id}__reviewed49__{source_sha1[:12]}__"
                f"{safe_slug(local_product_id)}__{section_for(document)}__{index:03d}"
            )
            if doc_id in seen:
                conflicts.append(doc_id)
                continue
            seen.add(doc_id)
            text = text_of(document)
            new_metadata = dict(metadata)
            new_metadata.update(
                {
                    "doc_id": doc_id,
                    "product_id": canonical_product_id,
                    "canonical_product_id": canonical_product_id,
                    "local_product_id": local_product_id,
                    "doc_type": "supplement",
                    "source_type": "product_excel_supplement_reviewed",
                    "source_doc_id": identity.get("source_doc_id"),
                    "source_sha1": source_sha1,
                    "source_path": identity.get("source_path"),
                    "source_file": identity.get("source_path"),
                    "is_supplement": True,
                    "supplement_of_doc_ids": row.get("matched_doc_ids") or [],
                    "supplement_reason": "复查后按完整产品名命中线上产品，作为补充资料入库",
                    "matched_canonical_product_name": row.get("matched_canonical_product_name") or "",
                    "original_doc_id": safe_text(metadata.get("doc_id") or document.get("doc_id")),
                    "original_product_id": local_product_id,
                }
            )
            output.append(
                {
                    "doc_id": doc_id,
                    "product_id": canonical_product_id,
                    "canonical_product_id": canonical_product_id,
                    "local_product_id": local_product_id,
                    "doc_type": "supplement",
                    "source_type": "product_excel_supplement_reviewed",
                    "source_doc_id": identity.get("source_doc_id"),
                    "source_sha1": source_sha1,
                    "source_path": identity.get("source_path"),
                    "is_supplement": True,
                    "supplement_of_doc_ids": row.get("matched_doc_ids") or [],
                    "supplement_reason": "复查后按完整产品名命中线上产品，作为补充资料入库",
                    "text": text,
                    "page_content": text,
                    "metadata": new_metadata,
                }
            )
    return output, {
        "supplement_product_count": len(rows),
        "supplement_document_count": len(output),
        "doc_id_conflicts": conflicts,
        "missing_local_product_ids": missing_local,
        "doc_type_counts": dict(Counter(doc["doc_type"] for doc in output)),
    }


def build_new_docs(rows: list[dict[str, Any]], grouped_docs: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    conflicts: list[str] = []
    missing_local: list[str] = []
    for row in rows:
        local_product_id = safe_text(row.get("local_product_id"))
        canonical_product_id = safe_text(row.get("suggested_new_product_id"))
        local_docs = grouped_docs.get(local_product_id) or []
        if not local_docs:
            missing_local.append(local_product_id)
            continue
        for index, document in enumerate(local_docs, start=1):
            identity = document_identity(document)
            metadata = metadata_of(document)
            original_doc_type = safe_text(metadata.get("doc_type") or document.get("doc_type"))
            doc_type = original_doc_type if original_doc_type in {"product_full", "product_field"} else "product_field"
            source_sha1 = identity.get("source_sha1")
            doc_id = (
                f"newprod__{canonical_product_id}__final49__{source_sha1[:12]}__"
                f"{safe_slug(local_product_id)}__{section_for(document)}__{index:03d}"
            )
            if doc_id in seen:
                conflicts.append(doc_id)
                continue
            seen.add(doc_id)
            text = text_of(document)
            new_metadata = dict(metadata)
            new_metadata.update(
                {
                    "doc_id": doc_id,
                    "product_id": canonical_product_id,
                    "canonical_product_id": canonical_product_id,
                    "registry_product_id": canonical_product_id,
                    "local_product_id": local_product_id,
                    "doc_type": doc_type,
                    "source_type": "product_excel_new_product",
                    "source_doc_id": identity.get("source_doc_id"),
                    "source_sha1": source_sha1,
                    "source_path": identity.get("source_path"),
                    "source_file": identity.get("source_path"),
                    "product_name": safe_text(row.get("local_product_name")) or identity.get("product_name"),
                    "spec": safe_text(row.get("local_spec")) or identity.get("spec"),
                    "manufacturer": safe_text(row.get("local_manufacturer")) or identity.get("manufacturer"),
                    "brand": safe_text(row.get("brand")) or identity.get("brand"),
                    "original_doc_id": safe_text(metadata.get("doc_id") or document.get("doc_id")),
                    "original_product_id": local_product_id,
                }
            )
            output.append(
                {
                    "doc_id": doc_id,
                    "product_id": canonical_product_id,
                    "canonical_product_id": canonical_product_id,
                    "registry_product_id": canonical_product_id,
                    "local_product_id": local_product_id,
                    "doc_type": doc_type,
                    "source_type": "product_excel_new_product",
                    "source_doc_id": identity.get("source_doc_id"),
                    "source_sha1": source_sha1,
                    "source_path": identity.get("source_path"),
                    "product_name": new_metadata["product_name"],
                    "spec": new_metadata["spec"],
                    "manufacturer": new_metadata["manufacturer"],
                    "brand": new_metadata["brand"],
                    "text": text,
                    "page_content": text,
                    "metadata": new_metadata,
                }
            )
    return output, {
        "new_product_count": len(rows),
        "new_product_document_count": len(output),
        "doc_id_conflicts": conflicts,
        "missing_local_product_ids": missing_local,
        "doc_type_counts": dict(Counter(doc["doc_type"] for doc in output)),
        "product_id_range": [rows[0].get("suggested_new_product_id"), rows[-1].get("suggested_new_product_id")] if rows else [],
    }


def write_docs(output_dir: Path, docs: list[dict[str, Any]], stats: dict[str, Any], title: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    docs_path = output_dir / "documents.json"
    docs_path.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# {title}", "", f"- documents: {len(docs)}", f"- doc_id conflicts: {len(stats.get('doc_id_conflicts') or [])}"]
    (output_dir / "build_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build final 49 split documents.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--recheck", default=DEFAULT_RECHECK)
    parser.add_argument("--dmz-documents", default=DEFAULT_DMZ_DOCUMENTS)
    parser.add_argument("--srx-documents", default=DEFAULT_SRX_DOCUMENTS)
    parser.add_argument("--supplement-output-dir", default=DEFAULT_SUPP_DIR)
    parser.add_argument("--new-product-output-dir", default=DEFAULT_NEW_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    recheck_payload = json.loads(resolve_path(root, args.recheck).read_text(encoding="utf-8"))
    supplement_rows, new_rows, missing_matched = split_rows(recheck_payload)
    documents = load_documents(resolve_path(root, args.dmz_documents)) + load_documents(resolve_path(root, args.srx_documents))
    grouped = docs_by_local_product(documents)
    supplement_docs, supplement_stats = build_supplement_docs(supplement_rows, grouped)
    new_docs, new_stats = build_new_docs(new_rows, grouped)

    write_docs(resolve_path(root, args.supplement_output_dir), supplement_docs, supplement_stats, "Reviewed Supplement Documents")
    write_docs(resolve_path(root, args.new_product_output_dir), new_docs, new_stats, "Final New Product Documents")

    split_rows_flat = []
    for row in supplement_rows:
        split_rows_flat.append(
            {
                "split": "supplement",
                "brand": row.get("brand"),
                "local_product_id": row.get("local_product_id"),
                "local_product_name": row.get("local_product_name"),
                "local_spec": row.get("local_spec"),
                "matched_canonical_product_id": row.get("matched_canonical_product_id"),
                "matched_canonical_product_name": row.get("matched_canonical_product_name"),
                "match_reason": row.get("reason"),
            }
        )
    for row in new_rows:
        split_rows_flat.append(
            {
                "split": "new_product",
                "brand": row.get("brand"),
                "local_product_id": row.get("local_product_id"),
                "local_product_name": row.get("local_product_name"),
                "local_spec": row.get("local_spec"),
                "suggested_new_product_id": row.get("suggested_new_product_id"),
                "reason": row.get("reason"),
            }
        )

    split_payload = {
        "created_at": now_iso(),
        "total": len(supplement_rows) + len(new_rows) + len(missing_matched),
        "should_be_supplement": len(supplement_rows),
        "new_product": len(new_rows),
        "missing_matched_product_id": len(missing_matched),
        "supplement_stats": supplement_stats,
        "new_product_stats": new_stats,
        "rows": split_rows_flat,
    }
    review_dir = root / "output/review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "final_49_split_plan.json").write_text(json.dumps(split_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(review_dir / "final_49_split_plan.csv", split_rows_flat)
    lines = [
        "# Final 49 Split Plan",
        "",
        f"- total: {split_payload['total']}",
        f"- should_be_supplement: {split_payload['should_be_supplement']}",
        f"- new_product: {split_payload['new_product']}",
        f"- missing matched product_id: {split_payload['missing_matched_product_id']}",
        f"- supplement documents: {supplement_stats['supplement_document_count']}",
        f"- new product documents: {new_stats['new_product_document_count']}",
        "",
    ]
    for row in split_rows_flat:
        if row["split"] == "supplement":
            lines.append(f"- supplement `{row['matched_canonical_product_id']}` <- {row['local_product_name']}")
        else:
            lines.append(f"- new `{row['suggested_new_product_id']}` <- {row['local_product_name']}")
    (review_dir / "final_49_split_plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    registry_rows = [
        {
            "product_id": row.get("suggested_new_product_id"),
            "source_file": row.get("source_path"),
            "source_sheet": "",
            "source_row": row.get("excel_row"),
            "product_name": row.get("local_product_name"),
            "brand": row.get("brand"),
            "series": "",
            "spec": row.get("local_spec"),
            "manufacturer": row.get("local_manufacturer"),
            "catalog_fingerprint": "",
            "source_locator": f"{row.get('source_path')}||{row.get('excel_row')}",
            "status": "pending",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        for row in new_rows
    ]
    write_csv(review_dir / "product_catalog_registry_new_24.csv", registry_rows)

    for path, payload in [
        (root / "output/acceptance/reviewed_supplement_documents_acceptance.json", supplement_stats),
        (root / "output/acceptance/final_new_product_documents_acceptance.json", new_stats),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "output/acceptance/reviewed_supplement_documents_acceptance.md").write_text(
        f"# Reviewed Supplement Documents Acceptance\n\n- products: {len(supplement_rows)}\n- documents: {supplement_stats['supplement_document_count']}\n- conflicts: {len(supplement_stats['doc_id_conflicts'])}\n",
        encoding="utf-8",
    )
    (root / "output/acceptance/final_new_product_documents_acceptance.md").write_text(
        f"# Final New Product Documents Acceptance\n\n- products: {len(new_rows)}\n- documents: {new_stats['new_product_document_count']}\n- conflicts: {len(new_stats['doc_id_conflicts'])}\n- product_id_range: `{new_stats['product_id_range']}`\n",
        encoding="utf-8",
    )
    print(json.dumps({key: split_payload[key] for key in ("total", "should_be_supplement", "new_product", "missing_matched_product_id")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
