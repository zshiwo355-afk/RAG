#!/usr/bin/env python3
"""Read-only retrieval verification for recently pushed ingest documents."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rag_app.ingest_manifest import append_manifest_record  # noqa: E402
from rag_app import retrieval_verify  # noqa: E402
from push_ingest_embeddings import (  # type: ignore  # noqa: E402
    TABLE_NAME,
    fetch_documents_by_doc_id,
    load_env,
    safe_text,
    table_name_for,
)
from push_text_docs_to_opensearch import create_client, ensure_runtime_config  # type: ignore  # noqa: E402


DEFAULT_PUSH_REPORT = "output/ingest_push/push_report.json"
DEFAULT_OUTPUT = "output/ingest_push/retrieval_verify_report.md"
DEFAULT_JSON_OUTPUT = "output/ingest_push/retrieval_verify_report.json"
DEFAULT_MANIFEST = "output/ingest_manifest.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def selected_doc_ids(explicit_doc_ids: list[str], report_path: Path, limit: int) -> list[str]:
    if explicit_doc_ids:
        return [doc_id for doc_id in explicit_doc_ids if doc_id][:limit]
    payload = load_json(report_path)
    pushed_doc_ids = payload.get("pushed_doc_ids")
    if not isinstance(pushed_doc_ids, list):
        pushed_doc_ids = []
    return [safe_text(doc_id).strip() for doc_id in pushed_doc_ids if safe_text(doc_id).strip()][:limit]


def text_from_row(row: dict[str, Any]) -> str:
    for key in ("source_text", "page_content", "text"):
        text = safe_text(row.get(key)).strip()
        if text:
            return text
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if isinstance(metadata, dict):
        for key in ("product_name", "source_text", "text", "field_name"):
            text = safe_text(metadata.get(key)).strip()
            if text:
                return text
    return ""


def make_auto_query(row: dict[str, Any], max_chars: int = 80) -> str:
    text = text_from_row(row)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return safe_text(row.get("doc_id") or row.get("id")).strip()
    return text[:max_chars]


def fetch_target_documents(client: Any, models_module: Any, table_name: str, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
    return fetch_documents_by_doc_id(client, models_module, table_name, doc_ids)


def normalize_result(row: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": row.get("rank") or rank,
        "doc_id": safe_text(row.get("doc_id")).strip(),
        "score": row.get("score"),
        "source_type": safe_text(row.get("source_type")).strip(),
        "product_id": safe_text(row.get("product_id")).strip(),
        "source_doc_id": safe_text(row.get("source_doc_id")).strip(),
        "text_preview": safe_text(row.get("text_preview") or row.get("source_text")).strip()[:240],
    }


def run_retrieval_query(query: str, top_k: int, root: Path) -> list[dict[str, Any]]:
    rows = retrieval_verify.search(query, top_k=top_k, root=root)
    return [normalize_result(row, index) for index, row in enumerate(rows, start=1)]


def verify_doc_queries(
    *,
    doc_ids: list[str],
    fetched_docs: dict[str, dict[str, Any]],
    top_k: int,
    root: Path,
) -> list[dict[str, Any]]:
    results = []
    for doc_id in doc_ids:
        row = fetched_docs.get(doc_id)
        if not row:
            results.append(
                {
                    "doc_id": doc_id,
                    "auto_query": "",
                    "hit": False,
                    "rank": None,
                    "top_k_doc_ids": [],
                    "top_k_scores": [],
                    "warning": "doc_id not found in OpenSearch fetch",
                }
            )
            continue
        query = make_auto_query(row)
        top_rows = run_retrieval_query(query, top_k, root)
        top_doc_ids = [safe_text(item.get("doc_id")).strip() for item in top_rows]
        rank = next((index + 1 for index, found_doc_id in enumerate(top_doc_ids) if found_doc_id == doc_id), None)
        results.append(
            {
                "doc_id": doc_id,
                "auto_query": query,
                "hit": rank is not None,
                "rank": rank,
                "top_k_doc_ids": top_doc_ids,
                "top_k_scores": [item.get("score") for item in top_rows],
                "top_k_results": top_rows,
                "warning": "" if rank is not None else "target doc_id not found in top_k",
            }
        )
    return results


def verify_manual_queries(queries: list[str], top_k: int, root: Path) -> list[dict[str, Any]]:
    rows = []
    for query in queries:
        rows.append({"query": query, "top_k_results": run_retrieval_query(query, top_k, root)})
    return rows


def build_payload(
    *,
    mode: str,
    doc_results: list[dict[str, Any]],
    manual_results: list[dict[str, Any]],
    top_k: int,
    warnings: list[str],
) -> dict[str, Any]:
    hit_doc_ids = [item["doc_id"] for item in doc_results if item.get("hit")]
    missed_doc_ids = [item["doc_id"] for item in doc_results if not item.get("hit")]
    if warnings:
        status = "partial"
    elif missed_doc_ids:
        status = "partial"
    else:
        status = "success"
    return {
        "mode": mode,
        "retrieval_status": status,
        "doc_id_count": len(doc_results),
        "query_count": len(manual_results) + sum(1 for item in doc_results if item.get("auto_query")),
        "top_k": top_k,
        "hit_count": len(hit_doc_ids),
        "miss_count": len(missed_doc_ids),
        "hit_doc_ids": hit_doc_ids,
        "missed_doc_ids": missed_doc_ids,
        "doc_results": doc_results,
        "manual_query_results": manual_results,
        "warnings": warnings,
        "verified_at": now_iso(),
    }


def write_report(markdown_path: Path, json_path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Retrieval Verify Report",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Retrieval status: `{payload['retrieval_status']}`",
        f"- Verified doc_id count: {payload['doc_id_count']}",
        f"- Query count: {payload['query_count']}",
        f"- Top K: {payload['top_k']}",
        f"- Hit count: {payload['hit_count']}",
        f"- Miss count: {payload['miss_count']}",
        f"- Warning count: {len(payload['warnings'])}",
        f"- Verified at: `{payload['verified_at']}`",
        "",
        "## Doc ID Verification",
        "",
    ]
    for item in payload["doc_results"]:
        lines.append(
            f"- `{item['doc_id']}`: {'hit' if item.get('hit') else 'miss'}"
            + (f", rank={item.get('rank')}" if item.get("rank") else "")
        )
        if item.get("auto_query"):
            lines.append(f"  - auto_query: {item['auto_query']}")
        if item.get("top_k_doc_ids"):
            lines.append(f"  - top_k_doc_ids: {item['top_k_doc_ids']}")
        if item.get("warning"):
            lines.append(f"  - warning: {item['warning']}")
    lines.extend(["", "## Manual Queries", ""])
    for item in payload["manual_query_results"]:
        lines.append(f"- Query: {item['query']}")
        for row in item["top_k_results"]:
            lines.append(
                f"  - rank={row.get('rank')} doc_id=`{row.get('doc_id')}` score={row.get('score')} "
                f"product_id=`{row.get('product_id')}` source_doc_id=`{row.get('source_doc_id')}`"
            )
            if row.get("text_preview"):
                lines.append(f"    text: {row['text_preview']}")
    if payload["warnings"]:
        lines.extend(["", "## Warnings", ""])
        for warning in payload["warnings"]:
            lines.append(f"- {warning}")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_manifest_event(payload: dict[str, Any], manifest_path: Path) -> None:
    append_manifest_record(
        {
            "event_type": "verify_retrieval",
            "source_doc_id": "",
            "source_path": "",
            "source_sha1": "",
            "source_type": "",
            "product_id": "unknown",
            "generated_doc_ids": [],
            "status": payload["retrieval_status"],
            "retrieval_status": payload["retrieval_status"],
            "verified_doc_ids": payload["hit_doc_ids"],
            "missing_doc_ids": payload["missed_doc_ids"],
            "warning_count": len(payload["warnings"]),
            "verified_at": payload["verified_at"],
            "status_detail": f"query_count={payload['query_count']}, top_k={payload['top_k']}",
        },
        manifest_path,
    )


def confirm_or_exit(yes: bool) -> None:
    if yes:
        print("Confirmation skipped because --yes was provided.")
        return
    answer = input("Type yes to run read-only retrieval verification: ").strip()
    if answer != "yes":
        raise SystemExit("Aborted. No retrieval verification was executed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only retrieval verification for pushed ingest documents.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--source-type", default="all", choices=("all", "product_excel", "quality_report"))
    parser.add_argument("--from-push-report", default=DEFAULT_PUSH_REPORT)
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--auto-query", action="store_true")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--verify-limit", type=int, default=10)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--json-output", default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run and args.execute:
        raise SystemExit("--dry-run and --execute cannot be used together")
    if args.top_k <= 0:
        raise SystemExit("--top-k must be positive")
    if args.verify_limit <= 0:
        raise SystemExit("--verify-limit must be positive")

    root = Path(args.root).resolve()
    load_env(root)
    report_path = resolve_path(root, args.from_push_report)
    output_path = resolve_path(root, args.output)
    json_output_path = resolve_path(root, args.json_output)
    manifest_path = resolve_path(root, args.manifest)
    doc_ids = selected_doc_ids(args.doc_id, report_path, args.verify_limit)
    queries = [safe_text(query).strip() for query in args.query if safe_text(query).strip()]

    print("Retrieval verify plan:")
    print(f"- doc_ids: {doc_ids}")
    print(f"- manual queries: {queries}")
    print(f"- auto_query: {args.auto_query}")
    print(f"- top_k: {args.top_k}")
    print(f"- output: {output_path}")
    if not args.execute:
        payload = build_payload(mode="dry-run", doc_results=[], manual_results=[], top_k=args.top_k, warnings=[])
        payload["planned_doc_ids"] = doc_ids
        payload["planned_queries"] = queries
        write_report(output_path, json_output_path, payload)
        print("Mode: dry-run; no OpenSearch, embedding, or API retrieval call was executed.")
        return 0

    confirm_or_exit(args.yes)
    config = ensure_runtime_config()
    table_name = table_name_for(config)
    client, models_module = create_client(config)
    fetched_docs = fetch_target_documents(client, models_module, table_name, doc_ids)
    warnings = []
    for doc_id in doc_ids:
        if doc_id not in fetched_docs:
            warnings.append(f"{doc_id} not found by fetch before retrieval")
    doc_results = verify_doc_queries(doc_ids=doc_ids, fetched_docs=fetched_docs, top_k=args.top_k, root=root)
    manual_results = verify_manual_queries(queries, args.top_k, root)
    payload = build_payload(
        mode="execute",
        doc_results=doc_results,
        manual_results=manual_results,
        top_k=args.top_k,
        warnings=warnings,
    )
    write_report(output_path, json_output_path, payload)
    append_manifest_event(payload, manifest_path)
    print(
        f"Retrieval verify complete: status={payload['retrieval_status']}, "
        f"hit={payload['hit_count']}, miss={payload['miss_count']}"
    )
    print(f"Report: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
