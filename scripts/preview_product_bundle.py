#!/usr/bin/env python3
"""Preview a read-only product bundle from OpenSearch."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
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
    extract_result_items,
    flatten_item,
    load_env,
    parse_metadata,
    resolve_path,
    resolve_read_table_name,
    safe_text,
    table_name_for,
)
from push_text_docs_to_opensearch import create_client, ensure_runtime_config  # type: ignore  # noqa: E402
from rag_app.retrieval_service import RetrievalService, build_text_query_string  # noqa: E402


DEFAULT_OUTPUT = "output/acceptance/product_bundle_preview.json"
DEFAULT_REPORT = "output/acceptance/product_bundle_preview.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(row.get("metadata"))
    merged = dict(metadata)
    merged.update({key: value for key, value in row.items() if value not in (None, "")})
    text = safe_text(merged.get("source_text") or merged.get("page_content") or merged.get("text")).strip()
    return {
        "doc_id": compact(merged.get("doc_id") or merged.get("id")),
        "product_id": compact(merged.get("product_id")),
        "doc_type": compact(merged.get("doc_type")),
        "field_name": compact(merged.get("field_name")),
        "source_doc_id": compact(merged.get("source_doc_id")),
        "source_sha1": compact(merged.get("source_sha1")),
        "source_file": compact(merged.get("source_file")),
        "source_text": text,
        "text_preview": re.sub(r"\s+", " ", text)[:320],
        "image_id": compact(merged.get("image_id")),
        "image_path": compact(merged.get("image_path")),
        "image_url": compact(merged.get("image_url")),
    }


def search_rows(client: Any, models_module: Any, table_name: str, query: str, size: int = 50, field_name: str = "source_text") -> list[dict[str, Any]]:
    method = getattr(client, "search_by_product_id", None)
    if callable(method):
        return [normalize_row(row) for row in method(table_name, query)]
    text = models_module.TextQuery(
        query_string=build_text_query_string(field_name, query),
        query_params={"default_op": "OR"},
    )
    request = models_module.SearchRequest(
        table_name=table_name,
        size=size,
        output_fields=[
            "id",
            "doc_id",
            "product_id",
            "doc_type",
            "field_name",
            "source_text",
            "source_doc_id",
            "source_sha1",
            "source_file",
            "image_id",
            "image_path",
            "image_url",
            "metadata",
        ],
        text=text,
    )
    return [normalize_row(flatten_item(row)) for row in extract_result_items(client.search(request))]


def build_bundle(product_id: str, text_rows: list[dict[str, Any]], image_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    image_rows = image_rows or []
    bundle: dict[str, Any] = {
        "product_id": product_id,
        "product_name": "",
        "basic_info": [],
        "packaging": [],
        "selling_points": [],
        "gift_attributes": [],
        "quality_reports": [],
        "image_texts": [],
        "supplements": [],
        "source_doc_ids": [],
        "doc_ids": [],
    }
    source_doc_ids = set()
    doc_ids = set()
    for row in text_rows:
        if product_id and row.get("product_id") != product_id:
            continue
        doc_ids.add(row.get("doc_id"))
        source_doc_ids.add(row.get("source_doc_id"))
        target = "supplements"
        if row.get("doc_type") == "product_full":
            target = "basic_info"
        elif row.get("doc_type") == "quality_report":
            target = "quality_reports"
        elif row.get("field_name") in {"basic_info"}:
            target = "basic_info"
        elif row.get("field_name") in {"packaging_desc"}:
            target = "packaging"
        elif row.get("field_name") in {"selling_points"}:
            target = "selling_points"
        elif row.get("field_name") in {"gift_attributes"}:
            target = "gift_attributes"
        bundle[target].append(row)
    for row in image_rows:
        if product_id and row.get("product_id") != product_id:
            continue
        bundle["image_texts"].append(row)
        doc_ids.add(row.get("doc_id") or row.get("image_id"))
        source_doc_ids.add(row.get("source_doc_id"))
    bundle["doc_ids"] = sorted(x for x in doc_ids if x)
    bundle["source_doc_ids"] = sorted(x for x in source_doc_ids if x)
    return bundle


def preview_by_query(query: str, top_k: int, root: Path) -> list[str]:
    service = RetrievalService(root=root)
    payload = service.retrieve(query, top_k=top_k)
    product_ids = []
    for item in payload.get("results") or []:
        product_id = compact(item.get("product_id"))
        if product_id and product_id not in product_ids:
            product_ids.append(product_id)
    return product_ids[:top_k]


def write_reports(output: Path, report: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Product Bundle Preview", "", f"- mode: `{payload['mode']}`", f"- bundles: {len(payload['bundles'])}", ""]
    for bundle in payload["bundles"]:
        lines.extend(
            [
                f"## `{bundle['product_id']}`",
                "",
                f"- basic_info: {len(bundle['basic_info'])}",
                f"- packaging: {len(bundle['packaging'])}",
                f"- selling_points: {len(bundle['selling_points'])}",
                f"- gift_attributes: {len(bundle['gift_attributes'])}",
                f"- quality_reports: {len(bundle['quality_reports'])}",
                f"- image_texts: {len(bundle['image_texts'])}",
                f"- supplements: {len(bundle['supplements'])}",
                f"- doc_ids: {len(bundle['doc_ids'])}",
                "",
            ]
        )
    report.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview read-only product bundle.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--product-id", action="append")
    parser.add_argument("--query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.execute and args.dry_run:
        raise SystemExit("--execute and --dry-run cannot be used together")
    root = Path(args.root).resolve()
    output = resolve_path(root, args.output)
    report = resolve_path(root, args.report_output)
    mode = "execute" if args.execute else "dry-run"
    requested = [item for item in (args.product_id or []) if item]
    if args.query and not args.execute:
        requested = []
    if not args.execute:
        payload = {
            "mode": mode,
            "created_at": now_iso(),
            "query": args.query or "",
            "requested_product_ids": requested,
            "will_connect_opensearch": False,
            "will_call_query_embedding": bool(args.query and args.execute),
            "bundles": [],
        }
        write_reports(output, report, payload)
        print(json.dumps({"mode": mode, "will_connect_opensearch": False, "bundles": 0}, ensure_ascii=False))
        return 0

    load_env(root)
    config = ensure_runtime_config()
    client, models_module = create_client(config)
    text_table = resolve_read_table_name(client, models_module, table_name_for(config))
    product_ids = requested or preview_by_query(args.query or "", args.top_k, root)
    bundles = []
    image_table = "image_text_docs"
    for product_id in product_ids[: args.top_k]:
        text_rows = search_rows(client, models_module, text_table, product_id, size=80, field_name="product_id")
        try:
            image_table = resolve_read_table_name(client, models_module, f"{config['instance_id']}_image_text_docs")
            image_rows = search_rows(client, models_module, image_table, product_id, size=40, field_name="product_id")
        except Exception:
            image_rows = []
        bundles.append(build_bundle(product_id, text_rows, image_rows))
    payload = {
        "mode": mode,
        "created_at": now_iso(),
        "query": args.query or "",
        "requested_product_ids": product_ids,
        "text_table": text_table,
        "image_text_table": image_table,
        "bundles": bundles,
    }
    write_reports(output, report, payload)
    print(json.dumps({"mode": mode, "bundles": len(bundles), "text_table": text_table}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
