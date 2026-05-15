#!/usr/bin/env python3
"""Plan ingest delete/update/rollback changes without mutating OpenSearch."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rag_app.ingest_manifest import load_manifest  # noqa: E402
from push_ingest_embeddings import (  # type: ignore  # noqa: E402
    TABLE_NAME,
    fetch_documents_by_doc_id,
    load_env,
    safe_text,
    table_name_for,
)
from push_text_docs_to_opensearch import create_client, ensure_runtime_config  # type: ignore  # noqa: E402


DEFAULT_PUSH_REPORT = "output/ingest_push/push_report.json"
DEFAULT_MANIFEST = "output/ingest_manifest.jsonl"
DEFAULT_OUTPUT = "output/ingest_changes/change_plan.json"
DEFAULT_REPORT = "output/ingest_changes/change_plan.md"
DEFAULT_INGEST_PLAN = "output/ingest_plan.jsonl"
MODES = ("delete-source", "rollback-push", "update-source", "inspect-source")


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def unique(values: list[Any]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        text = safe_text(value).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def records_for_filters(
    records: list[dict[str, Any]],
    *,
    source_doc_id: str,
    source_sha1: str,
    doc_ids: list[str],
) -> list[dict[str, Any]]:
    doc_id_set = set(doc_ids)
    matched = []
    for record in records:
        generated = set(record.get("generated_doc_ids") or [])
        pushed = set(record.get("pushed_doc_ids") or [])
        if source_doc_id and safe_text(record.get("source_doc_id")).strip() == source_doc_id:
            matched.append(record)
        elif source_sha1 and safe_text(record.get("source_sha1")).strip() == source_sha1:
            matched.append(record)
        elif doc_id_set and (doc_id_set & (generated | pushed)):
            matched.append(record)
    return matched


def collect_doc_ids(records: list[dict[str, Any]], explicit_doc_ids: list[str] | None = None) -> list[str]:
    values = list(explicit_doc_ids or [])
    for record in records:
        values.extend(record.get("generated_doc_ids") or [])
        values.extend(record.get("pushed_doc_ids") or [])
    return unique(values)


def collect_from_push_report(path: Path) -> list[str]:
    payload = load_json(path)
    pushed = payload.get("pushed_doc_ids")
    return unique(pushed if isinstance(pushed, list) else [])


def latest_source_sha1(records: list[dict[str, Any]]) -> str:
    values = [safe_text(record.get("source_sha1")).strip() for record in records if safe_text(record.get("source_sha1")).strip()]
    return values[-1] if values else ""


def source_paths(records: list[dict[str, Any]]) -> list[str]:
    return unique([record.get("source_path") for record in records])


def find_latest_plan_sha1(root: Path, records: list[dict[str, Any]]) -> str:
    paths = set(source_paths(records))
    if not paths:
        return ""
    for row in read_jsonl(root / DEFAULT_INGEST_PLAN):
        if safe_text(row.get("source_path")).strip() in paths:
            return safe_text(row.get("source_sha1")).strip()
    return ""


def fetch_existing_docs(client: Any, models_module: Any, table_name: str, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not doc_ids:
        return {}
    return fetch_documents_by_doc_id(client, models_module, table_name, doc_ids)


def query_by_source_doc_id(client: Any, models_module: Any, table_name: str, source_doc_id: str) -> dict[str, dict[str, Any]]:
    if not source_doc_id:
        return {}
    method = getattr(client, "search_by_source_doc_id", None)
    if callable(method):
        rows = method(table_name, source_doc_id)
        return {safe_text(row.get("doc_id") or row.get("id")).strip(): row for row in rows if safe_text(row.get("doc_id") or row.get("id")).strip()}
    return {}


def base_plan(mode: str, source_doc_id: str, source_sha1: str, dry_run: bool) -> dict[str, Any]:
    return {
        "mode": mode,
        "dry_run": dry_run,
        "source_doc_id": source_doc_id,
        "source_sha1": source_sha1,
        "target_doc_ids": [],
        "target_indexes": [],
        "source_types": [],
        "product_ids": [],
        "opensearch_existing_doc_ids": [],
        "missing_doc_ids": [],
        "unknown_doc_ids": [],
        "risks": [],
        "blockers": [],
        "safe_to_execute": False,
        "recommended_next_steps": [],
        "manifest_records": [],
    }


def enrich_common(
    plan: dict[str, Any],
    records: list[dict[str, Any]],
    target_doc_ids: list[str],
    *,
    existing_docs: dict[str, dict[str, Any]] | None = None,
    extra_os_docs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    existing_docs = existing_docs or {}
    extra_os_docs = extra_os_docs or {}
    plan["target_doc_ids"] = unique(target_doc_ids)
    plan["target_indexes"] = unique([table for record in records for table in (record.get("vector_tables") or [])]) or [f"<instance_id>_{TABLE_NAME}"]
    plan["source_types"] = unique([record.get("source_type") for record in records])
    product_ids = [record.get("product_id") for record in records]
    product_ids.extend(row.get("product_id") for row in existing_docs.values())
    plan["product_ids"] = unique(product_ids)
    plan["opensearch_existing_doc_ids"] = unique(list(existing_docs.keys()) + list(extra_os_docs.keys()))
    plan["missing_doc_ids"] = [doc_id for doc_id in plan["target_doc_ids"] if doc_id not in existing_docs] if existing_docs else []
    plan["unknown_doc_ids"] = [doc_id for doc_id in extra_os_docs if doc_id not in plan["target_doc_ids"]]
    plan["manifest_records"] = records

    counts = Counter(plan["target_doc_ids"])
    duplicates = sorted(doc_id for doc_id, count in counts.items() if count > 1)
    if duplicates:
        plan["risks"].append(f"duplicate doc_id in target list: {duplicates[:10]}")
        plan["blockers"].append("duplicate target doc_id")
    if plan["unknown_doc_ids"]:
        plan["risks"].append("OpenSearch has doc_id for source_doc_id that is not present in manifest targets")
        plan["blockers"].append("opensearch contains extra doc_ids not tracked by manifest")
    if plan["missing_doc_ids"]:
        plan["risks"].append("manifest target doc_id not found in OpenSearch")
    if len(set(plan["product_ids"])) > 1:
        plan["risks"].append("multiple product_id values are involved")
    if not plan["target_doc_ids"] and plan["mode"] in {"delete-source", "rollback-push"}:
        plan["blockers"].append("no target_doc_ids found")

    plan["safe_to_execute"] = not plan["blockers"] and bool(plan["target_doc_ids"])
    return plan


def build_inspect_plan(records: list[dict[str, Any]], args: argparse.Namespace, existing_docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_doc_id = args.source_doc_id or (safe_text(records[-1].get("source_doc_id")).strip() if records else "")
    plan = base_plan(args.mode, source_doc_id, args.source_sha1 or latest_source_sha1(records), True)
    plan = enrich_common(plan, records, collect_doc_ids(records, args.doc_id), existing_docs=existing_docs)
    plan["safe_to_execute"] = False
    plan["recommended_next_steps"] = ["Inspect manifest_records and OpenSearch existence before planning delete/update."]
    return plan


def build_delete_plan(records: list[dict[str, Any]], args: argparse.Namespace, existing_docs: dict[str, dict[str, Any]], extra_os_docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    plan = base_plan(args.mode, args.source_doc_id, args.source_sha1 or latest_source_sha1(records), True)
    plan = enrich_common(plan, records, collect_doc_ids(records, args.doc_id), existing_docs=existing_docs, extra_os_docs=extra_os_docs)
    if not args.source_doc_id:
        plan["blockers"].append("delete-source requires --source-doc-id")
        plan["safe_to_execute"] = False
    plan["recommended_next_steps"] = [
        "Review change_plan.md/json.",
        "Confirm push_report, verify_report, and retrieval_verify_report for the same doc_ids.",
        "Do not delete in this stage; deletion execution is intentionally not implemented.",
    ]
    return plan


def build_rollback_plan(records: list[dict[str, Any]], args: argparse.Namespace, existing_docs: dict[str, dict[str, Any]], push_doc_ids: list[str]) -> dict[str, Any]:
    plan = base_plan(args.mode, args.source_doc_id, args.source_sha1 or latest_source_sha1(records), True)
    plan = enrich_common(plan, records, push_doc_ids, existing_docs=existing_docs)
    if not push_doc_ids:
        plan["blockers"].append("push_report has no pushed_doc_ids")
        plan["safe_to_execute"] = False
    if any(record.get("event_type") == "verify_retrieval" and safe_text(record.get("status")).strip() == "success" for record in records):
        plan["risks"].append("later retrieval verification success exists; rollback may remove verified searchable docs")
    plan["recommended_next_steps"] = [
        "Confirm this push_report is the exact push batch to roll back.",
        "Check whether later successful updates superseded these doc_ids.",
        "Do not delete in this stage; rollback execution is intentionally not implemented.",
    ]
    return plan


def build_update_plan(records: list[dict[str, Any]], args: argparse.Namespace, root: Path) -> dict[str, Any]:
    old_sha1 = args.source_sha1 or latest_source_sha1(records)
    new_sha1 = find_latest_plan_sha1(root, records) or args.source_sha1 or old_sha1
    plan = base_plan(args.mode, args.source_doc_id, old_sha1, True)
    plan = enrich_common(plan, records, collect_doc_ids(records, args.doc_id))
    plan["latest_plan_source_sha1"] = new_sha1
    if not old_sha1:
        plan["blockers"].append("old source_sha1 unavailable")
    if new_sha1 == old_sha1:
        plan["recommended_next_steps"] = ["source_sha1 unchanged; skip update unless build logic changed."]
        plan["safe_to_execute"] = False
    else:
        plan["risks"].append("source_sha1 changed; old and new document sets may overlap")
        plan["recommended_next_steps"] = [
            "Re-run ingest_content local build for the changed source.",
            "Re-run embed_ingest_build.",
            "Push new embeddings with safe dry-run first.",
            "Run verify-pushed and verify_retrieval.",
            "Only then plan deletion of old doc_ids.",
        ]
        plan["safe_to_execute"] = False
    return plan


def write_outputs(plan: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Ingest Change Plan",
        "",
        f"- Mode: `{plan['mode']}`",
        f"- Dry run: `{plan['dry_run']}`",
        f"- Source doc id: `{plan.get('source_doc_id') or ''}`",
        f"- Source sha1: `{plan.get('source_sha1') or ''}`",
        f"- Target doc ids: {len(plan['target_doc_ids'])}",
        f"- OpenSearch existing doc ids: {len(plan['opensearch_existing_doc_ids'])}",
        f"- Missing doc ids: {len(plan['missing_doc_ids'])}",
        f"- Unknown doc ids: {len(plan['unknown_doc_ids'])}",
        f"- Safe to execute: `{plan['safe_to_execute']}`",
        "",
        "## Target Doc IDs",
        "",
    ]
    lines.extend(f"- `{doc_id}`" for doc_id in plan["target_doc_ids"][:100])
    lines.extend(["", "## Risks", ""])
    if plan["risks"]:
        lines.extend(f"- {risk}" for risk in plan["risks"])
    else:
        lines.append("- None")
    lines.extend(["", "## Blockers", ""])
    if plan["blockers"]:
        lines.extend(f"- {blocker}" for blocker in plan["blockers"])
    else:
        lines.append("- None")
    lines.extend(["", "## Recommended Next Steps", ""])
    if plan["recommended_next_steps"]:
        lines.extend(f"- {step}" for step in plan["recommended_next_steps"])
    else:
        lines.append("- None")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan ingest delete/update/rollback changes without executing them.")
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--source-doc-id", default="")
    parser.add_argument("--source-sha1", default="")
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--from-push-report", default=DEFAULT_PUSH_REPORT)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--check-opensearch", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--root", default=str(ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.execute:
        raise SystemExit("execute deletion is not implemented in this stage")
    root = Path(args.root).resolve()
    load_env(root)
    manifest_path = resolve_path(root, args.manifest)
    push_report_path = resolve_path(root, args.from_push_report)
    output_path = resolve_path(root, args.output)
    report_path = resolve_path(root, args.report_output)

    records = load_manifest(manifest_path)
    push_doc_ids = collect_from_push_report(push_report_path) if args.mode == "rollback-push" else []
    if args.mode == "rollback-push":
        matched_records = records_for_filters(records, source_doc_id=args.source_doc_id, source_sha1=args.source_sha1, doc_ids=push_doc_ids)
        target_doc_ids = push_doc_ids
    else:
        matched_records = records_for_filters(records, source_doc_id=args.source_doc_id, source_sha1=args.source_sha1, doc_ids=args.doc_id)
        target_doc_ids = collect_doc_ids(matched_records, args.doc_id)

    existing_docs: dict[str, dict[str, Any]] = {}
    extra_os_docs: dict[str, dict[str, Any]] = {}
    if args.check_opensearch:
        config = ensure_runtime_config()
        table_name = table_name_for(config)
        client, models_module = create_client(config)
        existing_docs = fetch_existing_docs(client, models_module, table_name, target_doc_ids)
        if args.source_doc_id:
            extra_os_docs = query_by_source_doc_id(client, models_module, table_name, args.source_doc_id)

    if args.mode == "inspect-source":
        plan = build_inspect_plan(matched_records, args, existing_docs)
    elif args.mode == "delete-source":
        plan = build_delete_plan(matched_records, args, existing_docs, extra_os_docs)
    elif args.mode == "rollback-push":
        plan = build_rollback_plan(matched_records, args, existing_docs, push_doc_ids)
    else:
        plan = build_update_plan(matched_records, args, root)

    write_outputs(plan, output_path, report_path)
    print(f"Change plan: {output_path}")
    print(f"Report: {report_path}")
    print("Mode: dry-run; no OpenSearch write/delete was executed.")
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
