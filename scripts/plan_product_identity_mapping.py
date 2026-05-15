#!/usr/bin/env python3
"""Plan read-only product identity mappings between local docs and OpenSearch."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for path in [ROOT / "src", ROOT / "scripts"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from check_existing_product_overlap import (  # type: ignore  # noqa: E402
    compact,
    document_identity,
    extract_result_items,
    flatten_item,
    load_documents,
    load_env,
    resolve_path,
    resolve_read_table_name,
    safe_text,
    table_name_for,
)
from push_text_docs_to_opensearch import create_client, ensure_runtime_config  # type: ignore  # noqa: E402
from rag_app.retrieval_service import build_text_query_string  # noqa: E402


DEFAULT_DOCUMENTS = "output/ingest_build/product_excel/documents.json"
DEFAULT_SOURCE_DOC_ID = "product_excel__d1803d262c93fe33fe9b854ae6331148318943be"
DEFAULT_OUTPUT = "output/acceptance/dmz_product_mapping_candidates.json"
DEFAULT_REPORT = "output/acceptance/dmz_product_mapping_candidates.md"
DEFAULT_MAP_TEMPLATE = "config/product_identity_map.csv"
MAP_FIELDS = [
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


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_name(value: str) -> str:
    text = compact(value)
    text = text.replace("·", "").replace("•", "").replace(" ", "")
    return text.lower()


def similarity(left: str, right: str) -> float:
    left_n = normalize_name(left)
    right_n = normalize_name(right)
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0
    if left_n in right_n or right_n in left_n:
        return 0.9
    return SequenceMatcher(None, left_n, right_n).ratio()


def strict_field_similarity(left: str, right: str) -> float:
    left_n = normalize_name(left)
    right_n = normalize_name(right)
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0
    return SequenceMatcher(None, left_n, right_n).ratio()


def parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            payload = json.loads(value)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def extract_label(text: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}[:：]\s*([^\n；;，,]{{1,80}})", text)
        if match:
            return compact(match.group(1))
    return ""


def online_identity(row: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(row.get("metadata"))
    merged = dict(metadata)
    merged.update({key: value for key, value in row.items() if value not in (None, "")})
    text = safe_text(merged.get("source_text") or merged.get("page_content") or merged.get("text")).strip()
    return {
        "doc_id": compact(merged.get("doc_id") or merged.get("id")),
        "product_id": compact(merged.get("product_id")),
        "product_name": compact(merged.get("product_name")) or extract_label(text, ["产品名称", "样品名称", "名称"]),
        "spec": compact(merged.get("spec")) or extract_label(text, ["规格", "净含量"]),
        "barcode": compact(merged.get("barcode") or merged.get("条码")),
        "manufacturer": compact(merged.get("manufacturer")) or extract_label(text, ["生产厂家", "生产酒厂", "酒厂"]),
        "brand": compact(merged.get("brand")) or extract_label(text, ["品牌"]),
        "source_doc_id": compact(merged.get("source_doc_id")),
        "text_preview": re.sub(r"\s+", " ", text)[:240],
    }


def unique_local_products(documents: list[dict[str, Any]], source_doc_id: str, limit: int | None) -> list[dict[str, str]]:
    by_product: dict[str, dict[str, str]] = {}
    for document in documents:
        item = document_identity(document)
        if item.get("source_doc_id") != source_doc_id:
            continue
        product_id = item.get("product_id")
        if not product_id or product_id in by_product:
            continue
        by_product[product_id] = item
        if limit is not None and len(by_product) >= limit:
            break
    return list(by_product.values())


def query_candidates(client: Any, models_module: Any, table_name: str, item: dict[str, str], top_k: int = 12) -> list[dict[str, Any]]:
    query = compact(" ".join(part for part in [item.get("product_name"), item.get("spec"), item.get("manufacturer")] if part))
    if not query:
        return []
    method = getattr(client, "search_by_keyword", None)
    if callable(method):
        return [online_identity(row) for row in method(table_name, query, top_k)]
    text = models_module.TextQuery(
        query_string=build_text_query_string("source_text", query),
        query_params={"default_op": "OR"},
    )
    request = models_module.SearchRequest(
        table_name=table_name,
        size=top_k,
        output_fields=[
            "id",
            "doc_id",
            "product_id",
            "product_name",
            "brand",
            "doc_type",
            "source_text",
            "source_doc_id",
            "metadata",
        ],
        text=text,
    )
    response = client.search(request)
    return [online_identity(flatten_item(row)) for row in extract_result_items(response)]


def choose_mapping(local: dict[str, str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    by_product: dict[str, dict[str, Any]] = {}
    evidence_by_product: defaultdict[str, list[str]] = defaultdict(list)
    best_score: dict[str, float] = {}
    for raw_row in candidates:
        row = online_identity(raw_row)
        product_id = compact(row.get("product_id"))
        if not product_id:
            continue
        name_score = similarity(local.get("product_name", ""), row.get("product_name", ""))
        spec_score = strict_field_similarity(local.get("spec", ""), row.get("spec", ""))
        manufacturer_score = similarity(local.get("manufacturer", ""), row.get("manufacturer", ""))
        score = name_score * 0.6 + spec_score * 0.25 + manufacturer_score * 0.15
        if local.get("barcode") and row.get("barcode") and local["barcode"] == row["barcode"]:
            score = 1.0
            evidence_by_product[product_id].append("barcode_exact")
        if name_score >= 0.9:
            evidence_by_product[product_id].append(f"name_high:{name_score:.2f}")
        elif name_score >= 0.72:
            evidence_by_product[product_id].append(f"name_medium:{name_score:.2f}")
        if spec_score >= 0.85:
            evidence_by_product[product_id].append(f"spec_match:{spec_score:.2f}")
        if manufacturer_score >= 0.85:
            evidence_by_product[product_id].append(f"manufacturer_match:{manufacturer_score:.2f}")
        if score > best_score.get(product_id, -1):
            best_score[product_id] = score
            by_product[product_id] = row

    ranked = sorted(by_product.values(), key=lambda row: best_score.get(row["product_id"], 0), reverse=True)
    top = ranked[0] if ranked else {}
    top_score = best_score.get(top.get("product_id", ""), 0.0)
    evidence = evidence_by_product.get(top.get("product_id", ""), [])
    if top and ("barcode_exact" in evidence or ("name_high" in " ".join(evidence) and any(e.startswith("spec_match") for e in evidence))):
        confidence = "high"
        action = "map_to_existing"
        needs_review = False
    elif top and top_score >= 0.68:
        confidence = "medium"
        action = "needs_review"
        needs_review = True
    elif top:
        confidence = "low"
        action = "needs_review"
        needs_review = True
    else:
        confidence = "low"
        action = "create_new"
        needs_review = True

    return {
        "local_product_id": local.get("product_id", ""),
        "local_product_name": local.get("product_name", ""),
        "local_spec": local.get("spec", ""),
        "local_manufacturer": local.get("manufacturer", ""),
        "local_barcode": local.get("barcode", ""),
        "local_unique_id": local.get("barcode") or "",
        "candidate_online_doc_ids": [row.get("doc_id", "") for row in ranked[:5]],
        "candidate_online_product_ids": [row.get("product_id", "") for row in ranked[:5]],
        "candidate_online_product_names": [row.get("product_name", "") for row in ranked[:5]],
        "candidate_online_specs": [row.get("spec", "") for row in ranked[:5]],
        "match_evidence": list(evidence) or (["no_candidate"] if not top else ["weak_keyword_match"]),
        "confidence": confidence,
        "recommended_action": action,
        "canonical_product_id": top.get("product_id", "") if action == "map_to_existing" else "",
        "canonical_product_name": top.get("product_name", "") if top else "",
        "canonical_spec": top.get("spec", "") if top else "",
        "needs_review": needs_review,
    }


def write_reports(output: Path, report_output: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    report_output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = payload["summary"]
    lines = [
        f"# {payload.get('source_name') or '产品'}身份映射候选",
        "",
        f"- mode: `{payload['mode']}`",
        f"- checked products: {payload['checked_product_count']}",
        f"- high confidence: {counts['high']}",
        f"- medium confidence: {counts['medium']}",
        f"- low confidence: {counts['low']}",
        f"- map_to_existing: {counts['map_to_existing']}",
        f"- create_new: {counts['create_new']}",
        f"- needs_review: {counts['needs_review']}",
        "",
        "## Candidates",
        "",
    ]
    for item in payload["candidates"]:
        lines.extend(
            [
                f"### {item['local_product_name']}",
                "",
                f"- local_product_id: `{item['local_product_id']}`",
                f"- local_spec: `{item['local_spec']}`",
                f"- candidates: `{', '.join(item['candidate_online_product_ids'])}`",
                f"- confidence: `{item['confidence']}`",
                f"- recommended_action: `{item['recommended_action']}`",
                f"- canonical_product_id: `{item['canonical_product_id']}`",
                f"- evidence: `{', '.join(item['match_evidence'])}`",
                "",
            ]
        )
    report_output.write_text("\n".join(lines), encoding="utf-8")


def ensure_map_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
                reader = csv.DictReader(file_obj)
                if reader.fieldnames == MAP_FIELDS:
                    return
        except Exception:
            pass
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=MAP_FIELDS)
        writer.writeheader()


def write_mapping_csv(path: Path, candidates: list[dict[str, Any]], source_doc_id: str, source_sha1: str, source_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=MAP_FIELDS)
        writer.writeheader()
        for item in candidates:
            if item["recommended_action"] == "map_to_existing" and item["confidence"] == "high":
                status = "confirmed"
            elif item["recommended_action"] == "create_new":
                status = "new_product"
            else:
                status = "needs_review"
            writer.writerow(
                {
                    "source_name": source_name,
                    "source_doc_id": source_doc_id,
                    "source_sha1": source_sha1,
                    "local_product_id": item["local_product_id"],
                    "local_product_name": item["local_product_name"],
                    "local_spec": item["local_spec"],
                    "local_manufacturer": item.get("local_manufacturer", ""),
                    "local_barcode": item["local_barcode"],
                    "canonical_product_id": item["canonical_product_id"],
                    "canonical_product_name": item.get("canonical_product_name", ""),
                    "canonical_spec": item.get("canonical_spec", ""),
                    "match_method": ",".join(item.get("match_evidence") or []),
                    "confidence": item["confidence"],
                    "status": status,
                    "notes": "auto candidate; confirmed only because name+spec evidence is high, still recommend human spot-check",
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan product identity mappings, read-only.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--documents", default=DEFAULT_DOCUMENTS)
    parser.add_argument("--source-doc-id", default=DEFAULT_SOURCE_DOC_ID)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument("--map-template", default=DEFAULT_MAP_TEMPLATE)
    parser.add_argument("--source-name", default="大民族品牌产品信息表.xlsx")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.execute and args.dry_run:
        raise SystemExit("--execute and --dry-run cannot be used together")
    root = Path(args.root).resolve()
    documents_path = resolve_path(root, args.documents)
    output = resolve_path(root, args.output)
    report_output = resolve_path(root, args.report_output)
    map_path = resolve_path(root, args.map_template)
    ensure_map_template(map_path)
    docs = load_documents(documents_path)
    products = unique_local_products(docs, args.source_doc_id, None if args.all else args.limit)
    mode = "execute" if args.execute else "dry-run"
    candidates: list[dict[str, Any]] = []
    table_name = ""
    if args.execute:
        load_env(root)
        config = ensure_runtime_config()
        client, models_module = create_client(config)
        table_name = resolve_read_table_name(client, models_module, table_name_for(config))
        for product in products:
            candidates.append(choose_mapping(product, query_candidates(client, models_module, table_name, product)))
    else:
        for product in products:
            row = choose_mapping(product, [])
            row["recommended_action"] = "needs_review"
            row["needs_review"] = True
            candidates.append(row)
    summary = Counter(item["confidence"] for item in candidates)
    summary.update(item["recommended_action"] for item in candidates)
    payload = {
        "mode": mode,
        "created_at": now_iso(),
        "documents": str(documents_path),
        "source_name": args.source_name,
        "source_doc_id": args.source_doc_id,
        "table_name": table_name,
        "checked_product_count": len(products),
        "summary": {
            "high": summary.get("high", 0),
            "medium": summary.get("medium", 0),
            "low": summary.get("low", 0),
            "map_to_existing": summary.get("map_to_existing", 0),
            "create_new": summary.get("create_new", 0),
            "needs_review": summary.get("needs_review", 0),
        },
        "candidates": candidates,
    }
    write_reports(output, report_output, payload)
    source_sha1 = args.source_doc_id.rsplit("__", 1)[-1] if "__" in args.source_doc_id else ""
    write_mapping_csv(map_path, candidates, args.source_doc_id, source_sha1, args.source_name)
    print(json.dumps(payload["summary"] | {"checked_product_count": len(products), "mode": mode}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
