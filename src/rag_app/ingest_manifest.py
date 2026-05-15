#!/usr/bin/env python3
"""Small JSONL manifest helpers for ingestion bookkeeping.

This module is intentionally lightweight. It does not call embedding services,
OpenSearch, OSS, or any ingestion scripts; it only reads and appends local JSONL
records.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from collections import Counter, defaultdict

from .config import OUTPUT_DIR, safe_text


DEFAULT_MANIFEST_PATH = OUTPUT_DIR / "ingest_manifest.jsonl"

MANIFEST_FIELDS = (
    "source_doc_id",
    "source_path",
    "source_sha1",
    "source_type",
    "product_id",
    "generated_doc_ids",
    "embedding_model",
    "vector_tables",
    "status",
    "status_detail",
    "event_type",
    "pushed_doc_ids",
    "push_status",
    "push_detail",
    "pushed_at",
    "verify_status",
    "retrieval_status",
    "verified_doc_ids",
    "missing_doc_ids",
    "warning_count",
    "verified_at",
    "delete_mode",
    "target_indexes",
    "requested_doc_ids",
    "deleted_doc_ids",
    "already_missing_doc_ids",
    "failed_doc_ids",
    "delete_status",
    "deleted_at",
    "created_at",
    "updated_at",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_iso()
    normalized = {field: record.get(field) for field in MANIFEST_FIELDS}
    normalized["source_doc_id"] = safe_text(normalized.get("source_doc_id")).strip()
    normalized["source_path"] = safe_text(normalized.get("source_path")).strip()
    normalized["source_sha1"] = safe_text(normalized.get("source_sha1")).strip()
    normalized["source_type"] = safe_text(normalized.get("source_type")).strip()
    normalized["product_id"] = safe_text(normalized.get("product_id")).strip() or "unknown"
    normalized["embedding_model"] = safe_text(normalized.get("embedding_model")).strip()
    normalized["status"] = safe_text(normalized.get("status")).strip() or "planned"
    normalized["status_detail"] = safe_text(normalized.get("status_detail")).strip()
    normalized["event_type"] = safe_text(normalized.get("event_type")).strip()
    normalized["push_status"] = safe_text(normalized.get("push_status")).strip()
    normalized["push_detail"] = safe_text(normalized.get("push_detail")).strip()
    normalized["pushed_at"] = safe_text(normalized.get("pushed_at")).strip()
    normalized["verify_status"] = safe_text(normalized.get("verify_status")).strip()
    normalized["retrieval_status"] = safe_text(normalized.get("retrieval_status")).strip()
    normalized["verified_at"] = safe_text(normalized.get("verified_at")).strip()
    normalized["delete_mode"] = safe_text(normalized.get("delete_mode")).strip()
    normalized["delete_status"] = safe_text(normalized.get("delete_status")).strip()
    normalized["deleted_at"] = safe_text(normalized.get("deleted_at")).strip()
    try:
        normalized["warning_count"] = int(normalized.get("warning_count") or 0)
    except Exception:
        normalized["warning_count"] = 0
    normalized["created_at"] = safe_text(normalized.get("created_at")).strip() or timestamp
    normalized["updated_at"] = safe_text(normalized.get("updated_at")).strip() or timestamp

    pushed_doc_ids = normalized.get("pushed_doc_ids")
    if not isinstance(pushed_doc_ids, list):
        pushed_doc_ids = []
    normalized["pushed_doc_ids"] = [safe_text(item).strip() for item in pushed_doc_ids if safe_text(item).strip()]

    verified_doc_ids = normalized.get("verified_doc_ids")
    if not isinstance(verified_doc_ids, list):
        verified_doc_ids = []
    normalized["verified_doc_ids"] = [safe_text(item).strip() for item in verified_doc_ids if safe_text(item).strip()]

    missing_doc_ids = normalized.get("missing_doc_ids")
    if not isinstance(missing_doc_ids, list):
        missing_doc_ids = []
    normalized["missing_doc_ids"] = [safe_text(item).strip() for item in missing_doc_ids if safe_text(item).strip()]

    generated_doc_ids = normalized.get("generated_doc_ids")
    if not isinstance(generated_doc_ids, list):
        generated_doc_ids = []
    normalized["generated_doc_ids"] = [safe_text(item).strip() for item in generated_doc_ids if safe_text(item).strip()]

    vector_tables = normalized.get("vector_tables")
    if not isinstance(vector_tables, list):
        vector_tables = []
    normalized["vector_tables"] = [safe_text(item).strip() for item in vector_tables if safe_text(item).strip()]

    target_indexes = normalized.get("target_indexes")
    if not isinstance(target_indexes, list):
        target_indexes = []
    normalized["target_indexes"] = [safe_text(item).strip() for item in target_indexes if safe_text(item).strip()]

    for field in ["requested_doc_ids", "deleted_doc_ids", "already_missing_doc_ids", "failed_doc_ids"]:
        values = normalized.get(field)
        if not isinstance(values, list):
            values = []
        normalized[field] = [safe_text(item).strip() for item in values if safe_text(item).strip()]
    return normalized


def load_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> list[dict[str, Any]]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        return []

    records: list[dict[str, Any]] = []
    with manifest_path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{manifest_path} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{manifest_path} 第 {line_number} 行顶层必须是 object")
            records.append(payload)
    return records


def append_manifest_record(
    record: dict[str, Any],
    path: Path | str = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_record(record)
    with manifest_path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(normalized, ensure_ascii=False) + "\n")
    return normalized


def find_by_source_doc_id(
    source_doc_id: str,
    path: Path | str = DEFAULT_MANIFEST_PATH,
) -> list[dict[str, Any]]:
    expected = safe_text(source_doc_id).strip()
    if not expected:
        return []
    return [
        record
        for record in load_manifest(path)
        if safe_text(record.get("source_doc_id")).strip() == expected
    ]


def find_by_source_sha1(
    source_sha1: str,
    path: Path | str = DEFAULT_MANIFEST_PATH,
) -> list[dict[str, Any]]:
    expected = safe_text(source_sha1).strip().lower()
    if not expected:
        return []
    return [
        record
        for record in load_manifest(path)
        if safe_text(record.get("source_sha1")).strip().lower() == expected
    ]


def find_by_source_path(
    source_path: str,
    path: Path | str = DEFAULT_MANIFEST_PATH,
) -> list[dict[str, Any]]:
    expected = safe_text(source_path).strip()
    if not expected:
        return []
    return [
        record
        for record in load_manifest(path)
        if safe_text(record.get("source_path")).strip() == expected
    ]


def sort_key_updated(record: dict[str, Any]) -> str:
    return safe_text(record.get("updated_at") or record.get("created_at")).strip()


def find_latest_by_source_doc_id(
    source_doc_id: str,
    path: Path | str = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any] | None:
    records = find_by_source_doc_id(source_doc_id, path)
    if not records:
        return None
    return sorted(records, key=sort_key_updated)[-1]


def group_by_source_sha1(
    path: Path | str = DEFAULT_MANIFEST_PATH,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in load_manifest(path):
        source_sha1 = safe_text(record.get("source_sha1")).strip().lower()
        if source_sha1:
            grouped[source_sha1].append(record)
    return dict(grouped)


def summarize_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    records = load_manifest(path)
    status_counts = Counter(safe_text(record.get("status")).strip() or "unknown" for record in records)
    source_type_counts = Counter(safe_text(record.get("source_type")).strip() or "unknown" for record in records)

    duplicate_sha1 = {
        sha1: items
        for sha1, items in group_by_source_sha1(path).items()
        if len({safe_text(item.get("source_doc_id")).strip() for item in items}) > 1
    }

    by_source_doc_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        source_doc_id = safe_text(record.get("source_doc_id")).strip()
        if source_doc_id:
            by_source_doc_id[source_doc_id].append(record)

    changed_source_doc_ids = {}
    for source_doc_id, items in by_source_doc_id.items():
        sha1_values = {
            safe_text(item.get("source_sha1")).strip().lower()
            for item in items
            if safe_text(item.get("source_sha1")).strip()
        }
        if len(sha1_values) > 1:
            changed_source_doc_ids[source_doc_id] = items

    by_source_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        source_path = safe_text(record.get("source_path")).strip()
        if source_path:
            by_source_path[source_path].append(record)

    changed_source_paths = {}
    for source_path, items in by_source_path.items():
        sha1_values = {
            safe_text(item.get("source_sha1")).strip().lower()
            for item in items
            if safe_text(item.get("source_sha1")).strip()
        }
        if len(sha1_values) > 1:
            changed_source_paths[source_path] = items

    recent_records = sorted(records, key=sort_key_updated, reverse=True)[:10]
    return {
        "total": len(records),
        "status_counts": dict(status_counts),
        "source_type_counts": dict(source_type_counts),
        "recent_records": recent_records,
        "duplicate_source_sha1": duplicate_sha1,
        "changed_source_doc_ids": changed_source_doc_ids,
        "changed_source_paths": changed_source_paths,
    }
