#!/usr/bin/env python3
"""Safely apply an ingest change_plan by deleting explicit OpenSearch doc_ids only."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rag_app.ingest_manifest import append_manifest_record  # noqa: E402
from push_ingest_embeddings import (  # type: ignore  # noqa: E402
    TABLE_NAME,
    fetch_documents_by_doc_id,
    table_name_for,
)
from push_text_docs_to_opensearch import (  # type: ignore  # noqa: E402
    PK_FIELD,
    build_push_request,
    create_client,
    ensure_runtime_config,
    is_success_response,
    load_env,
    safe_text,
)


DEFAULT_PLAN = "output/ingest_changes/change_plan.json"
DEFAULT_REPORT = "output/ingest_changes/delete_report.md"
DEFAULT_JSON_REPORT = "output/ingest_changes/delete_report.json"
DEFAULT_MANIFEST = "output/ingest_manifest.jsonl"
ALLOWED_MODES = {"delete-source", "rollback-push"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def load_plan(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        return {}, [f"change_plan not found: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"change_plan is not valid JSON: {exc}"]
    if not isinstance(payload, dict):
        return {}, ["change_plan top-level value must be an object"]
    return payload, []


def unique_texts(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    output: list[str] = []
    for value in values:
        text = safe_text(value).strip()
        if text:
            output.append(text)
    return output


def validate_plan(plan: dict[str, Any], *, require_mode: str = "") -> list[str]:
    reasons: list[str] = []
    mode = safe_text(plan.get("mode")).strip()
    if mode not in ALLOWED_MODES:
        reasons.append("mode must be delete-source or rollback-push")
    if require_mode and mode != require_mode:
        reasons.append(f"require-mode mismatch: expected {require_mode}, got {mode or '<empty>'}")
    if plan.get("safe_to_execute") is not True:
        reasons.append("safe_to_execute must be true")

    blockers = plan.get("blockers")
    if not isinstance(blockers, list):
        reasons.append("blockers must be a list")
    elif blockers:
        reasons.append("blockers must be empty")

    target_doc_ids = plan.get("target_doc_ids")
    if not isinstance(target_doc_ids, list) or not target_doc_ids:
        reasons.append("target_doc_ids must be a non-empty list")
    else:
        for index, doc_id in enumerate(target_doc_ids, start=1):
            if not isinstance(doc_id, str) or not doc_id.strip():
                reasons.append(f"target_doc_ids[{index}] must be a non-empty string")
        counts = Counter(doc_id.strip() for doc_id in target_doc_ids if isinstance(doc_id, str) and doc_id.strip())
        duplicates = sorted(doc_id for doc_id, count in counts.items() if count > 1)
        if duplicates:
            reasons.append(f"target_doc_ids contains duplicate doc_id: {duplicates[:10]}")

    target_indexes = plan.get("target_indexes")
    if not isinstance(target_indexes, list) or not unique_texts(target_indexes):
        reasons.append("target_indexes must be a non-empty list")
    return reasons


def apply_limit(doc_ids: list[str], limit: int | None) -> list[str]:
    if limit is None:
        return doc_ids
    return doc_ids[: max(limit, 0)]


def empty_report(
    plan: dict[str, Any] | None,
    *,
    started_at: str,
    executed: bool,
    dry_run: bool,
    requested_doc_ids: list[str] | None = None,
) -> dict[str, Any]:
    plan = plan or {}
    return {
        "mode": safe_text(plan.get("mode")).strip(),
        "executed": executed,
        "dry_run": dry_run,
        "source_doc_id": safe_text(plan.get("source_doc_id")).strip(),
        "source_sha1": safe_text(plan.get("source_sha1")).strip(),
        "target_indexes": unique_texts(plan.get("target_indexes")),
        "requested_doc_ids": requested_doc_ids if requested_doc_ids is not None else unique_texts(plan.get("target_doc_ids")),
        "deleted_doc_ids": [],
        "already_missing_doc_ids": [],
        "failed_doc_ids": [],
        "verified_missing_doc_ids": [],
        "verify_failed_doc_ids": [],
        "refused": False,
        "refused_reasons": [],
        "started_at": started_at,
        "finished_at": "",
        "product_ids": unique_texts(plan.get("product_ids")),
        "risks": unique_texts(plan.get("risks")),
        "blockers": plan.get("blockers") if isinstance(plan.get("blockers"), list) else [],
        "safe_to_execute": plan.get("safe_to_execute") is True,
        "delete_status": "",
        "failures": [],
    }


def print_dry_run_summary(plan: dict[str, Any], requested_doc_ids: list[str], allowed_execute: bool) -> None:
    current_table_name = safe_text(plan.get("_current_table_name")).strip()
    table_matches = plan.get("_current_table_matches")
    print("Ingest change delete plan:")
    print(f"- Plan mode: {safe_text(plan.get('mode')).strip()}")
    print(f"- Target indexes: {json.dumps(unique_texts(plan.get('target_indexes')), ensure_ascii=False)}")
    print(f"- Current table/index: {current_table_name or '<unavailable>'}")
    print(f"- Target index matches current table: {table_matches if table_matches is not None else 'unknown'}")
    print(f"- Target doc_ids: {len(requested_doc_ids)}")
    print(f"- Target doc_id samples: {json.dumps(requested_doc_ids[:20], ensure_ascii=False)}")
    print(f"- Source doc id: {safe_text(plan.get('source_doc_id')).strip()}")
    print(f"- Source sha1: {safe_text(plan.get('source_sha1')).strip()}")
    print(f"- Product ids: {json.dumps(unique_texts(plan.get('product_ids')), ensure_ascii=False)}")
    print(f"- Risks: {json.dumps(unique_texts(plan.get('risks')), ensure_ascii=False)}")
    blockers = plan.get("blockers") if isinstance(plan.get("blockers"), list) else []
    print(f"- Blockers: {json.dumps(blockers, ensure_ascii=False)}")
    print(f"- Safe to execute: {plan.get('safe_to_execute') is True}")
    print(f"- Execute allowed: {allowed_execute}")


def current_table_name_for_dry_run(root: Path) -> str:
    load_env(root)
    instance_id = os.getenv("OPENSEARCH_INSTANCE_ID", "").strip()
    if not instance_id:
        return ""
    return f"{instance_id}_{TABLE_NAME}"


def table_matches_plan(plan: dict[str, Any], current_table_name: str) -> bool:
    return bool(current_table_name and current_table_name in unique_texts(plan.get("target_indexes")))


def delete_document(client: Any, models_module: Any, table_name: str, doc_id: str) -> tuple[bool, str]:
    document = {"cmd": "delete", "fields": {"id": doc_id}}
    request = build_push_request(models_module, [document])
    response = client.push_documents(table_name, PK_FIELD, request)
    return is_success_response(getattr(response, "body", None))


def execute_delete(
    client: Any,
    models_module: Any,
    table_name: str,
    doc_ids: list[str],
    *,
    verify_after: bool,
) -> dict[str, Any]:
    deleted_doc_ids: list[str] = []
    already_missing_doc_ids: list[str] = []
    failed_doc_ids: list[str] = []
    failures: list[dict[str, Any]] = []

    existing = fetch_documents_by_doc_id(client, models_module, table_name, doc_ids)
    for index, doc_id in enumerate(doc_ids, start=1):
        if doc_id not in existing:
            already_missing_doc_ids.append(doc_id)
            continue
        try:
            ok, message = delete_document(client, models_module, table_name, doc_id)
            if ok:
                deleted_doc_ids.append(doc_id)
            else:
                failed_doc_ids.append(doc_id)
                failures.append({"doc_id": doc_id, "index": index, "reason": message})
        except Exception as exc:
            failed_doc_ids.append(doc_id)
            failures.append({"doc_id": doc_id, "index": index, "reason": str(exc)})

    verified_missing_doc_ids: list[str] = []
    verify_failed_doc_ids: list[str] = []
    if verify_after:
        verify_docs = fetch_documents_by_doc_id(client, models_module, table_name, doc_ids)
        for doc_id in doc_ids:
            if doc_id in verify_docs:
                verify_failed_doc_ids.append(doc_id)
            else:
                verified_missing_doc_ids.append(doc_id)

    return {
        "deleted_doc_ids": deleted_doc_ids,
        "already_missing_doc_ids": already_missing_doc_ids,
        "failed_doc_ids": failed_doc_ids,
        "verified_missing_doc_ids": verified_missing_doc_ids,
        "verify_failed_doc_ids": verify_failed_doc_ids,
        "failures": failures,
    }


def delete_status(payload: dict[str, Any]) -> str:
    requested = payload.get("requested_doc_ids") or []
    failed = set(payload.get("failed_doc_ids") or [])
    verify_failed = set(payload.get("verify_failed_doc_ids") or [])
    ok_count = len(payload.get("deleted_doc_ids") or []) + len(payload.get("already_missing_doc_ids") or [])
    if not requested:
        return "failed"
    if not failed and not verify_failed:
        return "success"
    if ok_count == 0 and len(failed) == len(requested):
        return "failed"
    return "partial"


def write_report(payload: dict[str, Any], md_path: Path, json_path: Path) -> None:
    payload["finished_at"] = payload.get("finished_at") or now_iso()
    lines = [
        "# Ingest Change Delete Report",
        "",
        f"- Mode: `{payload.get('mode')}`",
        f"- Executed: `{payload.get('executed')}`",
        f"- Dry run: `{payload.get('dry_run')}`",
        f"- Refused: `{payload.get('refused')}`",
        f"- Delete status: `{payload.get('delete_status') or 'n/a'}`",
        f"- Source doc id: `{payload.get('source_doc_id')}`",
        f"- Source sha1: `{payload.get('source_sha1')}`",
        f"- Target indexes: `{', '.join(payload.get('target_indexes') or [])}`",
        f"- Current table/index: `{payload.get('current_table_name') or '<unavailable>'}`",
        f"- Target index matches current table: `{payload.get('target_index_matches_current_table')}`",
        f"- Requested doc ids: {len(payload.get('requested_doc_ids') or [])}",
        f"- Deleted: {len(payload.get('deleted_doc_ids') or [])}",
        f"- Already missing: {len(payload.get('already_missing_doc_ids') or [])}",
        f"- Failed: {len(payload.get('failed_doc_ids') or [])}",
        f"- Verified missing: {len(payload.get('verified_missing_doc_ids') or [])}",
        f"- Verify failed: {len(payload.get('verify_failed_doc_ids') or [])}",
        f"- Safe to execute: `{payload.get('safe_to_execute')}`",
        "",
        "## Requested Doc ID Samples",
        "",
    ]
    requested = payload.get("requested_doc_ids") or []
    lines.extend(f"- `{doc_id}`" for doc_id in requested[:50])
    if not requested:
        lines.append("- None")

    for title, key in [
        ("Refused Reasons", "refused_reasons"),
        ("Product IDs", "product_ids"),
        ("Risks", "risks"),
        ("Blockers", "blockers"),
        ("Failed Doc IDs", "failed_doc_ids"),
        ("Verify Failed Doc IDs", "verify_failed_doc_ids"),
    ]:
        lines.extend(["", f"## {title}", ""])
        values = payload.get(key) or []
        if values:
            lines.extend(f"- `{item}`" for item in values[:50])
        else:
            lines.append("- None")

    failures = payload.get("failures") or []
    lines.extend(["", "## Failure Details", ""])
    if failures:
        for item in failures[:50]:
            lines.append(f"- `{item.get('doc_id')}`: {item.get('reason')}")
    else:
        lines.append("- None")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_delete_manifest(payload: dict[str, Any], manifest_path: Path) -> None:
    append_manifest_record(
        {
            "event_type": "delete",
            "delete_mode": payload.get("mode"),
            "source_doc_id": payload.get("source_doc_id"),
            "source_sha1": payload.get("source_sha1"),
            "target_indexes": payload.get("target_indexes"),
            "requested_doc_ids": payload.get("requested_doc_ids"),
            "deleted_doc_ids": payload.get("deleted_doc_ids"),
            "already_missing_doc_ids": payload.get("already_missing_doc_ids"),
            "failed_doc_ids": payload.get("failed_doc_ids"),
            "delete_status": payload.get("delete_status"),
            "deleted_at": payload.get("finished_at"),
            "status": payload.get("delete_status"),
        },
        manifest_path,
    )


def confirm_or_abort(yes: bool) -> bool:
    if yes:
        print("Confirmation skipped because --yes was provided.")
        return True
    answer = input("Type yes to delete the listed OpenSearch doc_ids: ").strip()
    return answer == "yes"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely delete explicit OpenSearch doc_ids from an ingest change plan.")
    parser.add_argument("--plan", default=DEFAULT_PLAN)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--require-mode", choices=sorted(ALLOWED_MODES), default="")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument("--json-output", default=DEFAULT_JSON_REPORT)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--verify-after", action="store_true")
    parser.add_argument("--root", default=str(ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    plan_path = resolve_path(root, args.plan)
    report_path = resolve_path(root, args.report_output)
    json_report_path = resolve_path(root, args.json_output)
    manifest_path = resolve_path(root, args.manifest)
    started_at = now_iso()
    execute = bool(args.execute)
    dry_run = not execute

    plan, load_reasons = load_plan(plan_path)
    validation_reasons = load_reasons or validate_plan(plan, require_mode=args.require_mode)
    requested_doc_ids = apply_limit(unique_texts(plan.get("target_doc_ids")), args.limit)
    current_table_name = current_table_name_for_dry_run(root) if not load_reasons else ""
    if plan:
        plan["_current_table_name"] = current_table_name
        plan["_current_table_matches"] = table_matches_plan(plan, current_table_name)
    payload = empty_report(plan, started_at=started_at, executed=False, dry_run=dry_run, requested_doc_ids=requested_doc_ids)
    payload["current_table_name"] = current_table_name
    payload["target_index_matches_current_table"] = plan.get("_current_table_matches") if plan else None

    if validation_reasons:
        payload["refused"] = True
        payload["refused_reasons"] = validation_reasons
        write_report(payload, report_path, json_report_path)
        for reason in validation_reasons:
            print(f"[REFUSED] {reason}")
        return 2

    print_dry_run_summary(plan, requested_doc_ids, allowed_execute=True)
    if dry_run:
        write_report(payload, report_path, json_report_path)
        print("Dry-run only. No OpenSearch connection or delete was executed.")
        return 0

    config = ensure_runtime_config()
    table_name = table_name_for(config)
    payload["current_table_name"] = table_name
    payload["target_index_matches_current_table"] = table_matches_plan(plan, table_name)
    if not payload["target_index_matches_current_table"]:
        payload["refused"] = True
        payload["refused_reasons"] = [f"current table/index {table_name} is not listed in change_plan target_indexes"]
        write_report(payload, report_path, json_report_path)
        print(f"[REFUSED] current table/index {table_name} is not listed in change_plan target_indexes")
        return 2

    if not confirm_or_abort(args.yes):
        payload["refused"] = True
        payload["refused_reasons"] = ["confirmation not received"]
        write_report(payload, report_path, json_report_path)
        print("Aborted. No OpenSearch delete was executed.")
        return 3

    client, models_module = create_client(config)
    result = execute_delete(client, models_module, table_name, requested_doc_ids, verify_after=args.verify_after)
    payload.update(result)
    payload["executed"] = True
    payload["delete_status"] = delete_status(payload)
    payload["finished_at"] = now_iso()
    write_report(payload, report_path, json_report_path)
    append_delete_manifest(payload, manifest_path)
    print(f"Delete complete: status={payload['delete_status']} deleted={len(payload['deleted_doc_ids'])} failed={len(payload['failed_doc_ids'])}")
    return 1 if payload["delete_status"] == "failed" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
