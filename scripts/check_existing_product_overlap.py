#!/usr/bin/env python3
"""Read-only overlap check between local product Excel docs and OpenSearch."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from difflib import SequenceMatcher
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from push_ingest_embeddings import (  # type: ignore  # noqa: E402
    extract_result_items,
    flatten_item,
    get_mapping_info,
    load_env,
    safe_text,
    table_name_for,
)
from push_text_docs_to_opensearch import create_client, ensure_runtime_config  # type: ignore  # noqa: E402
from rag_app.retrieval_service import build_text_query_string  # noqa: E402


DEFAULT_DOCUMENTS = "output/ingest_build/product_excel/documents.json"
DEFAULT_SOURCE_DOC_ID = "product_excel__d1803d262c93fe33fe9b854ae6331148318943be"
DEFAULT_OUTPUT = "output/acceptance/dmz_existing_overlap_report.json"
DEFAULT_REPORT = "output/acceptance/dmz_existing_overlap_report.md"
DEFAULT_STRATEGY_JSON = "output/acceptance/dmz_ingest_strategy.json"
DEFAULT_STRATEGY_MD = "output/acceptance/dmz_ingest_strategy.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def load_documents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"documents file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"documents must be a JSON list: {path}")
    return [item for item in payload if isinstance(item, dict)]


def metadata_of(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


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


def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", safe_text(value)).strip()


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = compact(value)
        if text:
            return text
    return ""


def text_of(document: dict[str, Any]) -> str:
    return first_nonempty(document.get("page_content"), document.get("source_text"), document.get("text"))


def extract_from_text(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return compact(match.group(1))
    return ""


def document_identity(document: dict[str, Any]) -> dict[str, str]:
    metadata = metadata_of(document)
    text = text_of(document)
    return {
        "doc_id": first_nonempty(metadata.get("doc_id"), document.get("doc_id"), document.get("id")),
        "product_id": first_nonempty(metadata.get("product_id"), document.get("product_id")),
        "product_name": first_nonempty(metadata.get("product_name"), document.get("product_name")),
        "barcode": first_nonempty(
            metadata.get("barcode"),
            metadata.get("条码"),
            document.get("barcode"),
            extract_from_text([r"(?:条码|barcode)[:：]?\s*([0-9A-Za-z-]{6,})"], text),
        ),
        "spec": first_nonempty(metadata.get("spec"), metadata.get("规格"), document.get("spec")),
        "brand": first_nonempty(metadata.get("brand"), metadata.get("品牌"), document.get("brand")),
        "manufacturer": first_nonempty(
            metadata.get("manufacturer"),
            metadata.get("生产厂家"),
            metadata.get("生产酒厂"),
            document.get("manufacturer"),
            extract_from_text([r"(?:生产厂家|生产酒厂|酒厂)[:：]\s*([^\n，,；;]{2,40})"], text),
        ),
        "channel": first_nonempty(metadata.get("channel"), metadata.get("售卖渠道"), document.get("channel")),
        "price": first_nonempty(metadata.get("price"), metadata.get("价格"), document.get("price")),
        "source_doc_id": first_nonempty(metadata.get("source_doc_id"), document.get("source_doc_id")),
        "source_sha1": first_nonempty(metadata.get("source_sha1"), document.get("source_sha1")),
        "source_path": first_nonempty(metadata.get("source_path"), metadata.get("source_file"), document.get("source_path")),
        "text": text,
    }


def flatten_existing(row: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(row.get("metadata"))
    merged = dict(metadata)
    merged.update({key: value for key, value in row.items() if value not in (None, "")})
    return {
        "doc_id": first_nonempty(merged.get("doc_id"), merged.get("id")),
        "product_id": first_nonempty(merged.get("product_id")),
        "product_name": first_nonempty(merged.get("product_name")),
        "barcode": first_nonempty(merged.get("barcode"), merged.get("条码")),
        "spec": first_nonempty(merged.get("spec"), merged.get("规格")),
        "brand": first_nonempty(merged.get("brand"), merged.get("品牌")),
        "manufacturer": first_nonempty(merged.get("manufacturer"), merged.get("生产厂家"), merged.get("生产酒厂")),
        "channel": first_nonempty(merged.get("channel"), merged.get("售卖渠道")),
        "price": first_nonempty(merged.get("price"), merged.get("价格")),
        "score": merged.get("score"),
        "source_text": first_nonempty(merged.get("source_text"), merged.get("page_content"), merged.get("text")),
    }


def similar(left: str, right: str) -> float:
    left = compact(left).lower()
    right = compact(right).lower()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.88
    return SequenceMatcher(None, left, right).ratio()


def conflict_fields(new_item: dict[str, str], existing: dict[str, Any]) -> list[dict[str, str]]:
    fields = [
        ("product_name", "产品名称"),
        ("brand", "品牌"),
        ("spec", "规格"),
        ("manufacturer", "生产酒厂"),
        ("price", "价格"),
    ]
    conflicts = []
    for key, label in fields:
        new_value = compact(new_item.get(key))
        old_value = compact(existing.get(key))
        if not new_value or not old_value:
            continue
        if key == "product_name" and similar(new_value, old_value) >= 0.72:
            continue
        if key != "product_name" and (new_value == old_value or new_value in old_value or old_value in new_value):
            continue
        conflicts.append({"field": key, "label": label, "new_value": new_value, "existing_value": old_value})
    return conflicts


def classify_overlap(new_item: dict[str, str], existing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [flatten_existing(row) for row in existing_rows]
    strong_matches: list[dict[str, Any]] = []
    medium_matches: list[dict[str, Any]] = []
    weak_matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []

    for row in normalized:
        product_id_match = bool(new_item.get("product_id") and row.get("product_id") == new_item.get("product_id"))
        barcode_match = bool(new_item.get("barcode") and row.get("barcode") == new_item.get("barcode"))
        name_score = similar(new_item.get("product_name", ""), row.get("product_name", ""))
        spec_match = bool(new_item.get("spec") and row.get("spec") and similar(new_item["spec"], row["spec"]) >= 0.82)
        manufacturer_match = bool(new_item.get("manufacturer") and row.get("manufacturer") and similar(new_item["manufacturer"], row["manufacturer"]) >= 0.82)
        row_conflicts = conflict_fields(new_item, row)
        if product_id_match or barcode_match:
            strong_matches.append(row)
            conflicts.extend(row_conflicts)
        elif name_score >= 0.86 or (name_score >= 0.78 and (spec_match or manufacturer_match)):
            medium_matches.append(row)
            conflicts.extend(row_conflicts)
        elif name_score >= 0.55 or row.get("doc_id"):
            weak_matches.append(row)

    matched = strong_matches or medium_matches or weak_matches
    if conflicts and (strong_matches or medium_matches):
        classification = "conflict_existing_product"
        risk_level = "high"
        suggested_action = "禁止直接入库，进入人工复核。"
        match_reason = "strong_or_medium_match_with_conflicting_fields"
        needs_review = True
    elif strong_matches or medium_matches:
        classification = "supplement_existing_product"
        risk_level = "low"
        suggested_action = "可作为补充文档追加入库，不覆盖旧 doc_id。"
        match_reason = "strong_match" if strong_matches else "medium_name_match"
        needs_review = False
    elif weak_matches:
        classification = "ambiguous"
        risk_level = "medium"
        suggested_action = "证据不足，先人工确认。"
        match_reason = "weak_keyword_or_name_match"
        needs_review = True
    else:
        classification = "new_product"
        risk_level = "low"
        suggested_action = "可作为新产品文档进入后续 embedding/push。"
        match_reason = "no_existing_match"
        needs_review = False

    return {
        "matched_rows": matched,
        "match_reason": match_reason,
        "classification": classification,
        "risk_level": risk_level,
        "suggested_action": suggested_action,
        "conflict_fields": conflicts,
        "needs_review": needs_review,
    }


def select_documents(documents: list[dict[str, Any]], source_doc_id: str, limit: int, include_all: bool) -> list[dict[str, Any]]:
    filtered = [doc for doc in documents if document_identity(doc).get("source_doc_id") == source_doc_id]
    if include_all:
        return filtered
    return filtered[:limit]


def matching_field_summary(items: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "selected_count": len(items),
        "product_id_non_empty": sum(1 for item in items if item.get("product_id")),
        "product_name_non_empty": sum(1 for item in items if item.get("product_name")),
        "barcode_non_empty": sum(1 for item in items if item.get("barcode")),
        "spec_non_empty": sum(1 for item in items if item.get("spec")),
        "matching_fields": ["product_id", "product_name", "barcode", "spec", "manufacturer", "channel", "selling_points/text", "packaging/text", "price", "brand"],
    }


def query_existing_rows(client: Any, models_module: Any, table_name: str, item: dict[str, str], top_k: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_doc_ids: set[str] = set()

    def add(found: list[dict[str, Any]]) -> None:
        for row in found:
            flat = flatten_item(row)
            doc_id = first_nonempty(flat.get("doc_id"), flat.get("id"))
            if doc_id and doc_id in seen_doc_ids:
                continue
            if doc_id:
                seen_doc_ids.add(doc_id)
            rows.append(flat)

    for method_name, key in [("search_by_product_id", "product_id"), ("search_by_barcode", "barcode")]:
        value = item.get(key)
        method = getattr(client, method_name, None)
        if value and callable(method):
            add(method(table_name, value))

    query_parts = [item.get("product_name", ""), item.get("spec", ""), item.get("brand", ""), item.get("manufacturer", "")]
    query = compact(" ".join(part for part in query_parts if part))
    if query:
        search_by_keyword = getattr(client, "search_by_keyword", None)
        if callable(search_by_keyword):
            add(search_by_keyword(table_name, query, top_k))
        else:
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
                    "source_text",
                    "source_doc_id",
                    "source_sha1",
                    "metadata",
                ],
                text=text,
            )
            add(extract_result_items(client.search(request)))
    return rows


def resolve_read_table_name(client: Any, models_module: Any, configured_table_name: str) -> str:
    candidates = [configured_table_name]
    if "_" in configured_table_name:
        logical_name = configured_table_name.rsplit("_", 1)[-1]
        if logical_name == "docs":
            logical_name = "text_docs"
        candidates.append(logical_name)
    candidates.append("text_docs")
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        info = get_mapping_info(client, models_module, candidate)
        if info.get("ok"):
            return candidate
    return configured_table_name


def build_result(item: dict[str, str], existing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    classified = classify_overlap(item, existing_rows)
    matched_rows = [flatten_existing(row) for row in classified["matched_rows"]]
    return {
        "new_doc_id": item.get("doc_id", ""),
        "new_product_id": item.get("product_id", ""),
        "new_product_name": item.get("product_name", ""),
        "new_barcode": item.get("barcode", ""),
        "new_spec": item.get("spec", ""),
        "new_source_doc_id": item.get("source_doc_id", ""),
        "matched_existing_doc_ids": [row.get("doc_id", "") for row in matched_rows if row.get("doc_id")],
        "matched_existing_product_ids": sorted({row.get("product_id", "") for row in matched_rows if row.get("product_id")}),
        "matched_existing_product_names": sorted({row.get("product_name", "") for row in matched_rows if row.get("product_name")}),
        "matched_existing_scores": [row.get("score") for row in matched_rows if row.get("score") is not None],
        "match_reason": classified["match_reason"],
        "classification": classified["classification"],
        "risk_level": classified["risk_level"],
        "suggested_action": classified["suggested_action"],
        "conflict_fields": classified["conflict_fields"],
        "needs_review": classified["needs_review"],
    }


def build_payload(
    *,
    mode: str,
    documents_path: Path,
    source_doc_id: str,
    selected_items: list[dict[str, str]],
    results: list[dict[str, Any]],
    connection_success: bool,
    table_name: str,
    warnings: list[str],
    error: str = "",
) -> dict[str, Any]:
    counts = Counter(result.get("classification") for result in results)
    return {
        "mode": mode,
        "documents": str(documents_path),
        "source_doc_id": source_doc_id,
        "checked_count": len(selected_items),
        "field_summary": matching_field_summary(selected_items),
        "opensearch": {
            "connected": connection_success,
            "queried": mode == "execute" and connection_success,
            "table_name": table_name,
            "error": error,
        },
        "classification_counts": {
            "new_product": counts.get("new_product", 0),
            "supplement_existing_product": counts.get("supplement_existing_product", 0),
            "conflict_existing_product": counts.get("conflict_existing_product", 0),
            "ambiguous": counts.get("ambiguous", 0),
        },
        "needs_review_doc_ids": [result["new_doc_id"] for result in results if result.get("needs_review")],
        "allow_embedding_doc_ids": [
            result["new_doc_id"]
            for result in results
            if result.get("classification") in {"new_product", "supplement_existing_product"}
        ],
        "blocked_doc_ids": [
            result["new_doc_id"]
            for result in results
            if result.get("classification") in {"conflict_existing_product", "ambiguous"}
        ],
        "warnings": warnings,
        "results": results,
        "created_at": now_iso(),
    }


def write_overlap_report(json_path: Path, markdown_path: Path, payload: dict[str, Any]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = payload["classification_counts"]
    lines = [
        "# 大民族品牌产品信息表 OpenSearch 只读重叠分析",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Checked documents: {payload['checked_count']}",
        f"- Source doc id: `{payload['source_doc_id']}`",
        f"- OpenSearch connected: {payload['opensearch']['connected']}",
        f"- OpenSearch queried: {payload['opensearch']['queried']}",
        f"- Table: `{payload['opensearch']['table_name']}`",
        f"- Error: `{payload['opensearch']['error']}`",
        "",
        "## 分类汇总",
        "",
        f"- new_product: {counts['new_product']}",
        f"- supplement_existing_product: {counts['supplement_existing_product']}",
        f"- conflict_existing_product: {counts['conflict_existing_product']}",
        f"- ambiguous: {counts['ambiguous']}",
        "",
        "## 字段可用性",
        "",
    ]
    for key, value in payload["field_summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## 分类样例", ""])
    for classification in ["new_product", "supplement_existing_product", "conflict_existing_product", "ambiguous"]:
        lines.append(f"### {classification}")
        samples = [item for item in payload["results"] if item.get("classification") == classification][:8]
        if not samples:
            lines.append("- 无。")
        for item in samples:
            lines.append(f"- `{item['new_doc_id']}` / {item.get('new_product_name')} / reason={item.get('match_reason')}")
        lines.append("")
    needs_review_lines = [f"- `{doc_id}`" for doc_id in payload["needs_review_doc_ids"][:50]] or ["- 无。"]
    allow_lines = [f"- `{doc_id}`" for doc_id in payload["allow_embedding_doc_ids"][:80]] or ["- 无。"]
    blocked_lines = [f"- `{doc_id}`" for doc_id in payload["blocked_doc_ids"][:80]] or ["- 无。"]
    lines.extend(["", "## 需要人工确认", "", *needs_review_lines])
    lines.extend(["", "## 建议允许后续 embedding 的 doc_id", "", *allow_lines])
    lines.extend(["", "## 禁止直接入库的 doc_id", "", *blocked_lines])
    if payload["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in payload["warnings"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_strategy(overlap_payload: dict[str, Any]) -> dict[str, Any]:
    results = overlap_payload.get("results") or []
    allowed = [item for item in results if item.get("classification") in {"new_product", "supplement_existing_product"}]
    needs_review = [item for item in results if item.get("classification") in {"conflict_existing_product", "ambiguous"}]
    supplement = [item for item in results if item.get("classification") == "supplement_existing_product"]
    return {
        "source_doc_id": overlap_payload.get("source_doc_id"),
        "checked_count": overlap_payload.get("checked_count"),
        "allowed_doc_ids": [item["new_doc_id"] for item in allowed],
        "needs_review_doc_ids": [item["new_doc_id"] for item in needs_review],
        "supplement_doc_ids": [item["new_doc_id"] for item in supplement],
        "blocked_doc_ids": [item["new_doc_id"] for item in needs_review],
        "rules": {
            "new_product": "可以进入后续 embedding / push。",
            "supplement_existing_product": "可以作为补充文档追加，但不能覆盖旧 doc_id。",
            "conflict_existing_product": "不允许直接入库，进入 needs_review。",
            "ambiguous": "不允许直接入库，进入 needs_review。",
        },
        "metadata_gap": "当前 documents.json / embedding / push wrapper 尚未自动写入 is_supplement、supplement_of_doc_ids、supplement_reason；若要区分补充文档，下一步应做最小元数据透传改造后再入库。",
        "recommended_supplement_metadata": [
            "is_supplement",
            "supplement_of_doc_ids",
            "supplement_reason",
            "source_doc_id",
            "source_sha1",
            "source_path",
            "product_id",
        ],
        "created_at": now_iso(),
    }


def write_strategy(json_path: Path, markdown_path: Path, payload: dict[str, Any]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 大民族品牌产品信息表补充入库策略",
        "",
        f"- Checked documents: {payload['checked_count']}",
        f"- Allowed doc_ids: {len(payload['allowed_doc_ids'])}",
        f"- Needs review doc_ids: {len(payload['needs_review_doc_ids'])}",
        f"- Supplement doc_ids: {len(payload['supplement_doc_ids'])}",
        "",
        "## 策略规则",
        "",
        *[f"- `{key}`：{value}" for key, value in payload["rules"].items()],
        "",
        "## 当前缺失点",
        "",
        f"- {payload['metadata_gap']}",
        "",
        "## 建议补充 metadata",
        "",
        *[f"- `{field}`" for field in payload["recommended_supplement_metadata"]],
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only product overlap check against OpenSearch text docs.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--documents", default=DEFAULT_DOCUMENTS)
    parser.add_argument("--source-doc-id", default=DEFAULT_SOURCE_DOC_ID)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--all", action="store_true", help="Check all matching documents.")
    parser.add_argument("--dry-run", action="store_true", help="Do not connect OpenSearch.")
    parser.add_argument("--execute", action="store_true", help="Execute read-only OpenSearch queries.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument("--strategy-output", default=DEFAULT_STRATEGY_JSON)
    parser.add_argument("--strategy-report-output", default=DEFAULT_STRATEGY_MD)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    documents_path = resolve_path(root, args.documents)
    output_path = resolve_path(root, args.output)
    report_path = resolve_path(root, args.report_output)
    strategy_output = resolve_path(root, args.strategy_output)
    strategy_report = resolve_path(root, args.strategy_report_output)
    documents = load_documents(documents_path)
    selected_docs = select_documents(documents, args.source_doc_id, args.limit, args.all)
    selected_items = [document_identity(doc) for doc in selected_docs]
    warnings: list[str] = []
    field_summary = matching_field_summary(selected_items)
    if not selected_items:
        warnings.append("No documents matched the requested source_doc_id.")
    if field_summary["product_id_non_empty"] == 0 and field_summary["product_name_non_empty"] == 0:
        warnings.append("No product_id or product_name available; overlap check is unreliable.")

    if args.execute and args.dry_run:
        raise SystemExit("--execute and --dry-run cannot be used together")
    mode = "execute" if args.execute else "dry-run"

    if not args.execute:
        payload = build_payload(
            mode=mode,
            documents_path=documents_path,
            source_doc_id=args.source_doc_id,
            selected_items=selected_items,
            results=[],
            connection_success=False,
            table_name="",
            warnings=warnings,
        )
        write_overlap_report(output_path, report_path, payload)
        print(f"Documents selected: {len(selected_items)}")
        print(f"Source doc id matched: {bool(selected_items)}")
        print(f"Matching fields: {', '.join(field_summary['matching_fields'])}")
        print(f"product_id non-empty: {field_summary['product_id_non_empty']}")
        print(f"product_name non-empty: {field_summary['product_name_non_empty']}")
        print(f"barcode non-empty: {field_summary['barcode_non_empty']}")
        print(f"spec non-empty: {field_summary['spec_non_empty']}")
        print("Will connect OpenSearch: no")
        print(f"Report: {report_path}")
        print(f"JSON: {output_path}")
        return 0 if not warnings else 2

    load_env(root)
    table_name = ""
    try:
        config = ensure_runtime_config()
        client, models_module = create_client(config)
        table_name = resolve_read_table_name(client, models_module, table_name_for(config))
        results = []
        for item in selected_items:
            existing_rows = query_existing_rows(client, models_module, table_name, item)
            results.append(build_result(item, existing_rows))
        payload = build_payload(
            mode=mode,
            documents_path=documents_path,
            source_doc_id=args.source_doc_id,
            selected_items=selected_items,
            results=results,
            connection_success=True,
            table_name=table_name,
            warnings=warnings,
        )
        write_overlap_report(output_path, report_path, payload)
        strategy = build_strategy(payload)
        write_strategy(strategy_output, strategy_report, strategy)
        counts = payload["classification_counts"]
        print(f"OpenSearch read-only overlap check succeeded: {len(results)} documents")
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
        print(f"Report: {report_path}")
        print(f"JSON: {output_path}")
        print(f"Strategy: {strategy_report}")
        return 0
    except Exception as exc:
        error = str(exc)
        payload = build_payload(
            mode=mode,
            documents_path=documents_path,
            source_doc_id=args.source_doc_id,
            selected_items=selected_items,
            results=[],
            connection_success=False,
            table_name=table_name,
            warnings=warnings + ["OpenSearch read-only query failed; no safe ingest conclusion is available."],
            error=error,
        )
        write_overlap_report(output_path, report_path, payload)
        print(f"[ERROR] OpenSearch read-only overlap check failed: {error}", file=sys.stderr)
        print(f"Report: {report_path}")
        print(f"JSON: {output_path}")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
