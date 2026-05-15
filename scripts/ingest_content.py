#!/usr/bin/env python3
"""Unified ingestion entrypoint without changing legacy ingestion scripts.

Default behavior is dry-run planning only. Execute mode intentionally stops at
the existing local document-build scripts; embedding and OpenSearch push remain
explicit manual operations via the original scripts.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from rag_app.ingest_manifest import (  # noqa: E402
    append_manifest_record,
    load_manifest,
    summarize_manifest,
)
from plan_ingest import DEFAULT_SOURCES, build_plan, print_table, write_jsonl  # type: ignore  # noqa: E402


DEFAULT_PLAN_OUTPUT = "output/ingest_plan.jsonl"
DEFAULT_MANIFEST = "output/ingest_manifest.jsonl"
DEFAULT_BUILD_DIR = "output/ingest_build"
TYPE_CHOICES = ("auto", "product_excel", "quality_report")
PLAN_TYPE_TO_SOURCE_TYPE = {
    "product_excel": "product_excel",
    "quality_report": "quality_report_pdf",
}


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (ROOT / path).resolve()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path} 第 {line_number} 行顶层必须是 object")
            records.append(payload)
    return records


def load_documents_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} 顶层必须是 list")
    return [item for item in payload if isinstance(item, dict)]


def doc_id_of(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    return safe_text(metadata.get("doc_id") or document.get("doc_id")).strip()


def source_doc_id_of(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    return safe_text(metadata.get("source_doc_id") or document.get("source_doc_id")).strip()


def product_id_of(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    return safe_text(metadata.get("product_id") or document.get("product_id")).strip()


def make_manifest_indexes(
    records: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_doc_id: dict[str, list[dict[str, Any]]] = {}
    by_sha1: dict[str, list[dict[str, Any]]] = {}
    by_path: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        source_doc_id = safe_text(record.get("source_doc_id")).strip()
        source_sha1 = safe_text(record.get("source_sha1")).strip().lower()
        source_path = safe_text(record.get("source_path")).strip()
        if source_doc_id:
            by_doc_id.setdefault(source_doc_id, []).append(record)
        if source_sha1:
            by_sha1.setdefault(source_sha1, []).append(record)
        if source_path:
            by_path.setdefault(source_path, []).append(record)
    return by_doc_id, by_sha1, by_path


def latest_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return sorted(records, key=lambda item: safe_text(item.get("updated_at") or item.get("created_at")).strip())[-1]


def annotate_plan_with_manifest(
    plan_records: list[dict[str, Any]],
    manifest_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_doc_id, by_sha1, by_path = make_manifest_indexes(manifest_records)
    annotated: list[dict[str, Any]] = []
    for record in plan_records:
        item = dict(record)
        source_doc_id = safe_text(item.get("source_doc_id")).strip()
        source_sha1 = safe_text(item.get("source_sha1")).strip().lower()
        source_path = safe_text(item.get("source_path")).strip()
        doc_matches = by_doc_id.get(source_doc_id, [])
        sha1_matches = by_sha1.get(source_sha1, [])
        path_matches = by_path.get(source_path, [])
        latest_doc = latest_record(doc_matches)
        latest_path = latest_record(path_matches)
        success_same_sha1 = [
            match for match in sha1_matches if safe_text(match.get("status")).strip() == "success"
        ]

        item["is_seen_source_doc_id"] = bool(doc_matches)
        item["is_seen_source_sha1"] = bool(sha1_matches)
        item["manifest_status"] = safe_text((latest_doc or {}).get("status")).strip()
        item["manifest_updated_at"] = safe_text((latest_doc or {}).get("updated_at")).strip()

        if not source_doc_id or not source_sha1:
            action = "unknown"
        elif latest_doc and safe_text(latest_doc.get("source_sha1")).strip().lower() == source_sha1:
            action = "unchanged" if safe_text(latest_doc.get("status")).strip() == "success" else "new"
        elif latest_doc and safe_text(latest_doc.get("source_sha1")).strip().lower() != source_sha1:
            action = "changed"
        elif latest_path and safe_text(latest_path.get("source_sha1")).strip().lower() != source_sha1:
            action = "changed"
            item["manifest_status"] = safe_text(latest_path.get("status")).strip()
            item["manifest_updated_at"] = safe_text(latest_path.get("updated_at")).strip()
        elif success_same_sha1:
            action = "duplicate_content"
        elif doc_matches or sha1_matches:
            action = "unknown"
        else:
            action = "new"
        item["suggested_action"] = action
        annotated.append(item)
    return annotated


def filter_records(records: list[dict[str, Any]], requested_type: str) -> list[dict[str, Any]]:
    if requested_type == "auto":
        return records
    expected_source_type = PLAN_TYPE_TO_SOURCE_TYPE[requested_type]
    return [
        record
        for record in records
        if safe_text(record.get("source_type")).strip() == expected_source_type
    ]


def unique_sorted(values: list[str]) -> list[str]:
    return sorted({value for value in values if value})


def summarize_plan(records: list[dict[str, Any]]) -> dict[str, Any]:
    source_type_counts = Counter(safe_text(record.get("source_type")).strip() for record in records)
    impact_types: list[str] = []
    vector_tables: list[str] = []
    for record in records:
        for value in record.get("impact_types") or []:
            text = safe_text(value).strip()
            if text:
                impact_types.append(text)
        for value in record.get("vector_tables") or []:
            text = safe_text(value).strip()
            if text:
                vector_tables.append(text)
    return {
        "file_count": len(records),
        "source_type_counts": dict(source_type_counts),
        "impact_types": unique_sorted(impact_types),
        "vector_tables": unique_sorted(vector_tables),
        "suggested_action_counts": dict(
            Counter(safe_text(record.get("suggested_action")).strip() or "unknown" for record in records)
        ),
    }


def print_summary(records: list[dict[str, Any]]) -> None:
    summary = summarize_plan(records)
    print()
    print("Execution preview:")
    print(f"- Files to process: {summary['file_count']}")
    print(f"- Source types: {json.dumps(summary['source_type_counts'], ensure_ascii=False)}")
    print(f"- Impact types: {', '.join(summary['impact_types']) or 'none'}")
    print(f"- Vector tables: {', '.join(summary['vector_tables']) or 'none'}")
    print(f"- Suggested actions: {json.dumps(summary['suggested_action_counts'], ensure_ascii=False)}")


def print_plan_manifest_summary(records: list[dict[str, Any]]) -> None:
    action_counts = Counter(safe_text(record.get("suggested_action")).strip() or "unknown" for record in records)
    print(f"Manifest suggested actions: {json.dumps(dict(action_counts), ensure_ascii=False)}")
    changed = action_counts.get("changed", 0)
    duplicate = action_counts.get("duplicate_content", 0)
    unchanged = action_counts.get("unchanged", 0)
    if unchanged:
        print(f"- unchanged: {unchanged} already successful source(s) can be skipped.")
    if changed:
        print(f"- changed: {changed} source(s) appear to have changed since last manifest record.")
    if duplicate:
        print(f"- duplicate_content: {duplicate} source(s) share content sha1 with another manifest record.")


def source_roots(records: list[dict[str, Any]], source_type: str) -> list[str]:
    roots: set[str] = set()
    for record in records:
        if safe_text(record.get("source_type")).strip() != source_type:
            continue
        source_path = Path(safe_text(record.get("source_path")).strip())
        if not source_path.parts:
            continue
        if source_type == "quality_report_pdf":
            if len(source_path.parts) >= 2:
                roots.add(Path(*source_path.parts[:2]).as_posix())
            else:
                roots.add(source_path.parent.as_posix())
        elif source_type == "product_excel":
            roots.add(source_path.as_posix())
    return sorted(roots)


def confirm_or_exit(yes: bool) -> None:
    if yes:
        print("Confirmation skipped because --yes was provided.")
        return
    print()
    answer = input("Type yes to execute local build scripts: ").strip()
    if answer != "yes":
        raise SystemExit("Aborted. No build script was executed.")


def run_command(command: list[str]) -> str:
    print()
    print("Running: " + " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)
    return " ".join(command)


def copy_quality_report_outputs(build_dir: Path) -> list[str]:
    source_path = ROOT / "output/quality_report_documents.json"
    target_dir = build_dir / "quality_report"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "documents.json"
    docs = load_documents_json(source_path)
    target_path.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = target_dir / "build_report.md"
    report_path.write_text(
        "# Quality Report Ingest Build Report\n\n"
        f"- Documents: {len(docs)}\n"
        f"- Source: `{source_path}`\n"
        f"- Standard output: `{target_path}`\n",
        encoding="utf-8",
    )
    return [doc_id_of(doc) for doc in docs if doc_id_of(doc)]


def execute_quality_report(records: list[dict[str, Any]], build_dir: Path) -> tuple[list[str], list[str]]:
    roots = source_roots(records, "quality_report_pdf")
    if not roots:
        return [], []
    commands: list[str] = []
    print()
    print("Quality report build scope:")
    for root in roots:
        print(f"- {root}")
    print("Note: legacy build_quality_report_docs.py accepts directories, so a single PDF source runs its parent directory.")
    for input_dir in roots:
        commands.append(
            run_command(
                [
                    sys.executable,
                    "scripts/build_quality_report_docs.py",
                    "--input-dir",
                    input_dir,
                ]
            )
        )
    generated_doc_ids = copy_quality_report_outputs(build_dir)
    return commands, generated_doc_ids


def execute_product_excel(records: list[dict[str, Any]], build_dir: Path) -> tuple[list[str], list[str]]:
    roots = source_roots(records, "product_excel")
    if not roots:
        return [], []
    print()
    print("Product Excel sources in plan:")
    for root in roots:
        print(f"- {root}")
    print("Note: product Excel build runs the isolated local Excel pipeline and then writes standard documents.")
    command = [
        sys.executable,
        "scripts/build_product_excel_pipeline.py",
        "--output-dir",
        str((build_dir / "product_excel").relative_to(ROOT)),
        "--yes",
    ]
    for root in roots:
        command.extend(["--source", root])
    command_text = run_command(command)
    docs_path = build_dir / "product_excel" / "documents.json"
    docs = load_documents_json(docs_path)
    return [command_text], [doc_id_of(doc) for doc in docs if doc_id_of(doc)]


def records_to_process(records: list[dict[str, Any]], force: bool, skip_existing: bool) -> list[dict[str, Any]]:
    if force:
        return records
    selected = []
    for record in records:
        action = safe_text(record.get("suggested_action")).strip()
        if skip_existing and action == "unchanged":
            continue
        selected.append(record)
    return selected


def append_execution_manifest(
    records: list[dict[str, Any]],
    manifest_path: Path,
    status: str,
    status_detail: str,
    generated_doc_ids_by_source: dict[str, list[str]] | None = None,
    unassigned_doc_ids: list[str] | None = None,
) -> None:
    generated_doc_ids_by_source = generated_doc_ids_by_source or {}
    unassigned_doc_ids = unassigned_doc_ids or []
    detail = status_detail
    if unassigned_doc_ids:
        detail += f"; warning: unassigned_generated_doc_ids={unassigned_doc_ids[:20]}"
    for record in records:
        source_doc_id = safe_text(record.get("source_doc_id")).strip()
        source_path = safe_text(record.get("source_path")).strip()
        generated_doc_ids = generated_doc_ids_by_source.get(source_doc_id) or generated_doc_ids_by_source.get(source_path) or []
        append_manifest_record(
            {
                "source_doc_id": source_doc_id,
                "source_path": source_path,
                "source_sha1": record.get("source_sha1"),
                "source_type": record.get("source_type"),
                "product_id": record.get("product_id") or "unknown",
                "generated_doc_ids": generated_doc_ids,
                "embedding_model": None,
                "vector_tables": [],
                "status": status,
                "status_detail": detail,
            },
            manifest_path,
        )


def document_source_keys(document: dict[str, Any]) -> set[str]:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    keys = {
        safe_text(metadata.get("source_doc_id") or document.get("source_doc_id")).strip(),
        safe_text(metadata.get("source_file") or document.get("source_file")).strip(),
        safe_text(metadata.get("source_path") or document.get("source_path")).strip(),
    }
    return {Path(key).as_posix() for key in keys if key}


def assign_generated_doc_ids(
    records: list[dict[str, Any]],
    build_dir: Path,
) -> tuple[dict[str, list[str]], list[str]]:
    record_keys: dict[str, str] = {}
    for record in records:
        source_doc_id = safe_text(record.get("source_doc_id")).strip()
        source_path = safe_text(record.get("source_path")).strip()
        if source_doc_id:
            record_keys[source_doc_id] = source_doc_id
        if source_path:
            record_keys[Path(source_path).as_posix()] = source_doc_id or source_path

    assigned: dict[str, list[str]] = {safe_text(record.get("source_doc_id")).strip() or safe_text(record.get("source_path")).strip(): [] for record in records}
    unassigned: list[str] = []
    for document in iter_build_documents(build_dir):
        doc_id = doc_id_of(document)
        if not doc_id:
            continue
        matches = sorted({record_keys[key] for key in document_source_keys(document) if key in record_keys})
        if len(matches) == 1:
            assigned.setdefault(matches[0], []).append(doc_id)
        else:
            unassigned.append(doc_id)

    return {key: sorted(set(values)) for key, values in assigned.items() if key and values}, sorted(set(unassigned))


def execute_plan(
    records: list[dict[str, Any]],
    requested_type: str,
    yes: bool,
    manifest_path: Path,
    build_dir: Path,
    force: bool,
    skip_existing: bool,
) -> None:
    if not records:
        print("No matching plan records. Nothing to execute.")
        return
    process_records = records_to_process(records, force=force, skip_existing=skip_existing)
    print_summary(records)
    action_counts = Counter(safe_text(record.get("suggested_action")).strip() or "unknown" for record in records)
    if action_counts.get("changed"):
        print(f"[WARN] changed sources detected: {action_counts['changed']}")
    if action_counts.get("duplicate_content"):
        print(f"[WARN] duplicate_content sources detected: {action_counts['duplicate_content']}")
    if not force and skip_existing:
        skipped = len(records) - len(process_records)
        print(f"- Skipped unchanged by manifest: {skipped}")
    print(f"- Files selected for local build: {len(process_records)}")
    if not process_records:
        print("Nothing to execute after manifest filtering.")
        return
    confirm_or_exit(yes)

    commands: list[str] = []
    if requested_type in {"auto", "quality_report"}:
        quality_commands, quality_doc_ids = execute_quality_report(process_records, build_dir)
        commands.extend(quality_commands)
    if requested_type in {"auto", "product_excel"}:
        product_commands, product_doc_ids = execute_product_excel(process_records, build_dir)
        commands.extend(product_commands)

    status_detail = "local build scripts: " + ("; ".join(commands) if commands else "none")
    generated_doc_ids_by_source, unassigned_doc_ids = assign_generated_doc_ids(process_records, build_dir)
    assigned_count = sum(len(values) for values in generated_doc_ids_by_source.values())
    if not assigned_count:
        status_detail += "; generated_doc_ids unavailable or no ready documents were produced"
    if unassigned_doc_ids:
        status_detail += "; some generated_doc_ids could not be assigned to a source"
    append_execution_manifest(
        process_records,
        manifest_path,
        "success",
        status_detail,
        generated_doc_ids_by_source,
        unassigned_doc_ids,
    )
    print()
    print("Local build stage complete.")
    print("Embedding and OpenSearch push were not executed.")
    print(f"Standard build output: {build_dir}")
    print("Next manual scripts depend on the generated artifacts, for example:")
    print("- python3 scripts/embed_documents.py --input output/quality_report_documents.json --output output/quality_report_documents_embedded.jsonl")
    print("- python3 scripts/push_text_docs_to_opensearch.py --input output/quality_report_documents_embedded.jsonl --dry-run")


def print_manifest_status(manifest_path: Path) -> None:
    summary = summarize_manifest(manifest_path)
    print(f"Manifest: {manifest_path}")
    print(f"- Total records: {summary['total']}")
    print(f"- success: {summary['status_counts'].get('success', 0)}")
    print(f"- failed: {summary['status_counts'].get('failed', 0)}")
    print(f"- Status counts: {json.dumps(summary['status_counts'], ensure_ascii=False)}")
    print(f"- Source type counts: {json.dumps(summary['source_type_counts'], ensure_ascii=False)}")
    duplicate_sha1 = summary["duplicate_source_sha1"]
    changed_doc_ids = summary["changed_source_doc_ids"]
    changed_paths = summary["changed_source_paths"]
    print(f"- Duplicate source_sha1 groups: {len(duplicate_sha1)}")
    for sha1, items in list(duplicate_sha1.items())[:10]:
        paths = [safe_text(item.get("source_path")).strip() for item in items[:5]]
        print(f"  - {sha1}: {len(items)} records; paths={paths}")
    print(f"- Changed source_doc_id groups: {len(changed_doc_ids)}")
    for source_doc_id, items in list(changed_doc_ids.items())[:10]:
        sha1_values = sorted({safe_text(item.get("source_sha1")).strip() for item in items})
        print(f"  - {source_doc_id}: sha1={sha1_values}")
    print(f"- Changed source_path groups: {len(changed_paths)}")
    for source_path, items in list(changed_paths.items())[:10]:
        sha1_values = sorted({safe_text(item.get("source_sha1")).strip() for item in items})
        print(f"  - {source_path}: sha1={sha1_values}")
    print("- Recent 10 records:")
    for record in summary["recent_records"]:
        print(
            "  - "
            + json.dumps(
                {
                    "updated_at": record.get("updated_at"),
                    "status": record.get("status"),
                    "source_type": record.get("source_type"),
                    "source_path": record.get("source_path"),
                    "source_sha1": record.get("source_sha1"),
                    "source_doc_id": record.get("source_doc_id"),
                },
                ensure_ascii=False,
            )
        )


def iter_build_documents(build_dir: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for source_type in ["product_excel", "quality_report"]:
        path = build_dir / source_type / "documents.json"
        for document in load_documents_json(path):
            item = dict(document)
            item["_source_type"] = source_type
            documents.append(item)
    return documents


def print_build_check(build_dir: Path) -> None:
    product_docs = load_documents_json(build_dir / "product_excel" / "documents.json")
    quality_docs = load_documents_json(build_dir / "quality_report" / "documents.json")
    needs_mapping_path = build_dir / "product_excel" / "needs_mapping.jsonl"
    needs_mapping_rows = read_jsonl(needs_mapping_path)
    docs = []
    for item in product_docs:
        copied = dict(item)
        copied["_source_type"] = "product_excel"
        docs.append(copied)
    for item in quality_docs:
        copied = dict(item)
        copied["_source_type"] = "quality_report"
        docs.append(copied)

    doc_ids = [doc_id_of(doc) for doc in docs]
    source_doc_ids = [source_doc_id_of(doc) for doc in docs]
    product_ids = [product_id_of(doc) for doc in docs]
    product_source_files = [
        safe_text((doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}).get("source_file")).strip()
        for doc in product_docs
    ]
    doc_id_counts = Counter(doc_id for doc_id in doc_ids if doc_id)
    product_id_counts = Counter(product_id for product_id in product_ids if product_id and product_id != "unknown")
    duplicate_doc_ids = sorted(doc_id for doc_id, count in doc_id_counts.items() if count > 1)
    duplicate_product_ids = sorted(product_id for product_id, count in product_id_counts.items() if count > 1)

    print(f"Build dir: {build_dir}")
    print(f"- product_excel documents.json exists: {(build_dir / 'product_excel' / 'documents.json').exists()}")
    print(f"- product_excel needs_mapping.jsonl exists: {needs_mapping_path.exists()}")
    print(f"- product_excel documents: {len(product_docs)}")
    print(f"- product_excel needs_mapping: {len(needs_mapping_rows)}")
    print(f"- quality_report documents: {len(quality_docs)}")
    print(f"- empty doc_id: {sum(1 for value in doc_ids if not value)}")
    print(f"- empty source_doc_id: {sum(1 for value in source_doc_ids if not value)}")
    print(f"- empty product_id: {sum(1 for value in product_ids if not value or value == 'unknown')}")
    print(f"- product_excel missing source_file trace: {sum(1 for value in product_source_files if not value)}")
    print(f"- duplicate product_id groups: {len(duplicate_product_ids)}")
    if duplicate_product_ids:
        print("  - note: repeated product_id is expected when one product produces multiple documents.")
        print(f"  - samples: {duplicate_product_ids[:10]}")
    print(f"- duplicate doc_id: {len(duplicate_doc_ids)}")
    if duplicate_doc_ids:
        print(f"  - samples: {duplicate_doc_ids[:10]}")
    print("- Recent/generated document samples:")
    for doc in docs[-10:]:
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        print(
            "  - "
            + json.dumps(
                {
                    "source_type": doc.get("_source_type"),
                    "doc_id": doc_id_of(doc),
                    "source_doc_id": source_doc_id_of(doc),
                    "product_id": product_id_of(doc),
                    "doc_type": metadata.get("doc_type"),
                    "source_file": metadata.get("source_file"),
                },
                ensure_ascii=False,
            )
        )
    if needs_mapping_rows:
        print("- Needs mapping samples:")
        for row in needs_mapping_rows[:10]:
            print(
                "  - "
                + json.dumps(
                    {
                        "source_path": row.get("source_path"),
                        "source_sheet": row.get("source_sheet"),
                        "status": row.get("status"),
                        "row_count": row.get("row_count"),
                        "headers": row.get("headers"),
                    },
                    ensure_ascii=False,
                )
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or execute local ingestion build steps.")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Source file or directory. May be passed multiple times. Defaults to plan_ingest.py defaults.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only generate and print an ingest plan. This is the default unless --execute is provided.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute local build scripts after plan summary and confirmation. Does not run embedding or OpenSearch push.",
    )
    parser.add_argument(
        "--type",
        default="auto",
        choices=TYPE_CHOICES,
        help="Filter source type: auto, product_excel, or quality_report.",
    )
    parser.add_argument(
        "--plan-output",
        default=DEFAULT_PLAN_OUTPUT,
        help="Plan JSONL path. Default: output/ingest_plan.jsonl.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation in --execute mode.",
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST,
        help="Manifest JSONL path. Default: output/ingest_manifest.jsonl.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Execute matching records even if source_sha1 already has a successful manifest record.",
    )
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip records whose source_doc_id/source_sha1 are already marked success. Default: true.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print manifest summary and exit without planning or executing.",
    )
    parser.add_argument(
        "--build-dir",
        default=DEFAULT_BUILD_DIR,
        help="Standard local build output directory. Default: output/ingest_build.",
    )
    parser.add_argument(
        "--check-build",
        action="store_true",
        help="Check standard local build artifacts and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run and args.execute:
        raise SystemExit("--dry-run and --execute cannot be used together")

    output_path = resolve_path(args.plan_output)
    manifest_path = resolve_path(args.manifest)
    build_dir = resolve_path(args.build_dir)
    if args.status:
        print_manifest_status(manifest_path)
        return 0
    if args.check_build:
        print_build_check(build_dir)
        return 0

    source_values = args.source or list(DEFAULT_SOURCES)
    sources = [resolve_path(value) for value in source_values]

    if args.source or not args.execute or not output_path.exists():
        plan_records = build_plan(ROOT.resolve(), sources)
        plan_records = filter_records(plan_records, args.type)
        write_jsonl(output_path, plan_records)
    else:
        plan_records = filter_records(read_jsonl(output_path), args.type)

    manifest_records = load_manifest(manifest_path)
    plan_records = annotate_plan_with_manifest(plan_records, manifest_records)
    write_jsonl(output_path, plan_records)

    print_table(plan_records)
    print_plan_manifest_summary(plan_records)
    print(f"Plan JSONL: {output_path}")

    if not args.execute:
        print("Mode: dry-run; no embedding, OpenSearch write, delete, or local build script was executed.")
        return 0

    try:
        execute_plan(
            records=plan_records,
            requested_type=args.type,
            yes=args.yes,
            manifest_path=manifest_path,
            build_dir=build_dir,
            force=args.force,
            skip_existing=args.skip_existing,
        )
    except Exception as exc:
        process_records = records_to_process(plan_records, force=args.force, skip_existing=args.skip_existing)
        append_execution_manifest(process_records, manifest_path, "failed", f"local build failed: {exc}", {})
        raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
