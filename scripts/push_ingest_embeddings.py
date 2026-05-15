#!/usr/bin/env python3
"""Safely push output/ingest_embeddings text embeddings to OpenSearch.

Default mode is dry-run. This wrapper targets the existing text_docs table and
does not delete OpenSearch data. It preserves the old push scripts and adds a
small safety layer for standard ingest embeddings.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rag_app.ingest_manifest import append_manifest_record  # noqa: E402
from push_text_docs_to_opensearch import (  # type: ignore  # noqa: E402
    PK_FIELD,
    TABLE_NAME,
    VECTOR_DIMENSIONS,
    build_push_request,
    create_client,
    ensure_runtime_config,
    is_success_response,
    load_env,
    resolve_protocol,
)


SOURCE_TYPES = (
    "product_excel",
    "quality_report",
    "product_excel_supplement",
    "product_excel_supplement_srx",
    "product_excel_new_products",
    "product_excel_supplement_reviewed",
    "product_excel_new_products_final",
)
VECTOR_FIELD = "source_text_vector"
DEFAULT_REPORT = "output/ingest_push/push_report.md"
DEFAULT_VERIFY_REPORT = "output/ingest_push/verify_report.md"
DEFAULT_MANIFEST = "output/ingest_manifest.jsonl"
DEFAULT_BATCH_SIZE = 1


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def selected_source_types(value: str) -> list[str]:
    if value == "all":
        return list(SOURCE_TYPES)
    return [value]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def json_report_path(markdown_report_path: Path) -> Path:
    if markdown_report_path.suffix:
        return markdown_report_path.with_suffix(".json")
    return markdown_report_path.parent / f"{markdown_report_path.name}.json"


def load_embedding_records(input_dir: Path, source_types: list[str], limit: int | None) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    records: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    remaining = limit
    for source_type in source_types:
        path = input_dir / source_type / "embeddings.jsonl"
        paths[source_type] = path
        for row in read_jsonl(path):
            if remaining is not None and remaining <= 0:
                break
            item = dict(row)
            item.setdefault("source_type", source_type)
            item["_input_path"] = str(path)
            records.append(item)
            if remaining is not None:
                remaining -= 1
    return records, paths


def embedding_dim(record: dict[str, Any]) -> int:
    explicit = record.get("embedding_dim")
    if isinstance(explicit, int):
        return explicit
    embedding = record.get("embedding")
    if isinstance(embedding, list):
        return len(embedding)
    return 0


def validate_embedding(record: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    doc_id = safe_text(record.get("doc_id")).strip()
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    text = safe_text(record.get("page_content") or record.get("text")).strip()
    vector = record.get("embedding")
    if not doc_id:
        warnings.append(f"row {index} missing doc_id")
    if not isinstance(vector, list) or not vector:
        warnings.append(f"{doc_id or 'row ' + str(index)} missing embedding")
    elif len(vector) != VECTOR_DIMENSIONS:
        warnings.append(f"{doc_id} embedding_dim mismatch: expected {VECTOR_DIMENSIONS}, got {len(vector)}")
    if not text:
        warnings.append(f"{doc_id or 'row ' + str(index)} missing text/page_content")
    for field in ["doc_id", "product_id", "source_doc_id", "source_sha1"]:
        value = safe_text(record.get(field) or metadata.get(field)).strip()
        if not value:
            warnings.append(f"{doc_id or 'row ' + str(index)} missing {field}")
    if not doc_id or not isinstance(vector, list) or not vector or not text:
        return None, warnings
    return record, warnings


def build_push_document(record: dict[str, Any], index: int) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    doc_id = safe_text(record.get("doc_id")).strip()
    text = safe_text(record.get("page_content") or record.get("text")).strip()
    vector = record.get("embedding")
    if not doc_id:
        raise ValueError(f"第 {index} 条记录缺少 doc_id")
    if not text:
        raise ValueError(f"{doc_id} 缺少 page_content/text")
    if not isinstance(vector, list) or not vector:
        raise ValueError(f"{doc_id} 缺少 embedding")
    if len(vector) != VECTOR_DIMENSIONS:
        raise ValueError(f"{doc_id} embedding 维度不正确：期望 {VECTOR_DIMENSIONS}，实际 {len(vector)}")

    source_type = safe_text(record.get("source_type")).strip()
    source_doc_id = safe_text(record.get("source_doc_id") or metadata.get("source_doc_id")).strip()
    source_sha1 = safe_text(record.get("source_sha1") or metadata.get("source_sha1")).strip()
    product_id = safe_text(record.get("product_id") or metadata.get("product_id")).strip()
    fields = {
        "id": doc_id,
        "doc_id": doc_id,
        "product_id": product_id,
        "doc_type": safe_text(record.get("doc_type") or metadata.get("doc_type")).strip(),
        "field_name": safe_text(record.get("field_name") or metadata.get("field_name")).strip(),
        "source_kind": safe_text(metadata.get("source_kind") or source_type).strip(),
        "source_file": safe_text(metadata.get("source_file")).strip(),
        "source_doc_id": source_doc_id,
        "source_sha1": source_sha1,
        "source_url": safe_text(metadata.get("source_url")).strip(),
        "chunk_index": metadata.get("chunk_index"),
        "page_start": metadata.get("page_start"),
        "page_end": metadata.get("page_end"),
        "supplement_title": safe_text(metadata.get("supplement_title")).strip(),
        "source_text": text,
        VECTOR_FIELD: [float(value) for value in vector],
        "source_type": source_type,
        "content_sha1": safe_text(record.get("content_sha1")).strip(),
        "embedding_model": safe_text(record.get("embedding_model")).strip(),
        "embedding_dim": embedding_dim(record),
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }
    return {"cmd": "add", "fields": fields}


def table_name_for(config: dict[str, str] | None) -> str:
    if not config:
        return f"<instance_id>_{TABLE_NAME}"
    return f"{config['instance_id']}_{TABLE_NAME}"


def resolve_push_target_table(client: Any, models_module: Any, config: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """Choose the writable table only when it is schema-verifiable.

    Retrieval in this project uses the logical `text_docs` route. Some SDK
    operations can search the prefixed table name, but if schema lookup cannot
    prove it is the same table/alias, do not choose it as a write target.
    """
    candidates = [TABLE_NAME, table_name_for(config)]
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        info = get_mapping_info(client, models_module, candidate)
        dim = infer_mapping_vector_dim(info)
        raw_text = json.dumps(info.get("raw") or {}, ensure_ascii=False)
        checked.append(
            {
                "table_name": candidate,
                "mapping_ok": bool(info.get("ok")),
                "mapping_vector_dim": dim,
                "vector_field_present": VECTOR_FIELD in raw_text,
                "error": info.get("error"),
            }
        )
        if info.get("ok") and VECTOR_FIELD in raw_text:
            return candidate, checked
    return TABLE_NAME, checked


def body_to_plain(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return body
    for method_name in ("to_map", "to_dict"):
        method = getattr(body, method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return body


def extract_result_items(response: Any) -> list[dict[str, Any]]:
    plain = body_to_plain(getattr(response, "body", response))
    if not isinstance(plain, dict):
        return []
    for key in ("result", "results", "items", "docs", "documents"):
        value = plain.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def flatten_item(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields")
    if isinstance(fields, dict):
        result = dict(fields)
    else:
        result = dict(item)
    return result


def fetch_existing_doc_ids(client: Any, models_module: Any, table_name: str, doc_ids: list[str]) -> set[str]:
    if not doc_ids:
        return set()
    try:
        request = models_module.FetchRequest(
            table_name=table_name,
            ids=doc_ids,
            include_vector=False,
            output_fields=["id", "doc_id"],
        )
        response = client.fetch(request)
        found = set()
        for item in extract_result_items(response):
            flat = flatten_item(item)
            row_id = safe_text(flat.get("id") or flat.get("doc_id")).strip()
            if row_id:
                found.add(row_id)
        return found
    except Exception as exc:
        print(f"[WARN] failed to fetch existing doc_ids: {exc}")
        return set()


def fetch_documents_by_doc_id(client: Any, models_module: Any, table_name: str, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not doc_ids:
        return {}
    request = models_module.FetchRequest(
        table_name=table_name,
        ids=doc_ids,
        include_vector=True,
        output_fields=[
            "id",
            "doc_id",
            "source_text",
            "page_content",
            "text",
            "source_type",
            "source_doc_id",
            "source_sha1",
            "product_id",
            "content_sha1",
            "embedding_model",
            "embedding_dim",
            VECTOR_FIELD,
            "metadata",
        ],
    )
    response = client.fetch(request)
    found: dict[str, dict[str, Any]] = {}
    for item in extract_result_items(response):
        flat = flatten_item(item)
        doc_id = safe_text(flat.get("doc_id") or flat.get("id")).strip()
        if doc_id:
            found[doc_id] = flat
    return found


def get_mapping_info(client: Any, models_module: Any, table_name: str) -> dict[str, Any]:
    for method_name in ("get_table", "describe_table", "get_index", "get_schema"):
        method = getattr(client, method_name, None)
        if callable(method):
            try:
                response = method(table_name)
                return {"ok": True, "method": method_name, "raw": body_to_plain(getattr(response, "body", response))}
            except TypeError:
                continue
            except Exception as exc:
                return {"ok": False, "method": method_name, "error": str(exc)}
    return {"ok": True, "method": "unavailable", "raw": {}}


def infer_mapping_vector_dim(mapping_info: dict[str, Any]) -> int | None:
    text = json.dumps(mapping_info.get("raw") or {}, ensure_ascii=False)
    if VECTOR_FIELD not in text:
        return None
    for key in ("dimension", "dimensions", "dim"):
        marker = f'"{key}"'
        if marker not in text:
            continue
        # Keep this conservative; mapping shapes vary by SDK version.
        try:
            payload = mapping_info.get("raw") or {}
            stack = [payload]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    if item.get(key) is not None:
                        return int(item[key])
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
        except Exception:
            return None
    return None


def count_table_documents(client: Any, models_module: Any, table_name: str) -> int | None:
    for method_name in ("count", "get_table_doc_count"):
        method = getattr(client, method_name, None)
        if callable(method):
            try:
                response = method(table_name)
                plain = body_to_plain(getattr(response, "body", response))
                if isinstance(plain, dict):
                    for key in ("count", "total", "doc_count"):
                        if key in plain:
                            return int(plain[key])
                if isinstance(plain, int):
                    return plain
            except Exception:
                return None
    return None


def analyze_records(
    records: list[dict[str, Any]],
    *,
    existing_doc_ids: set[str],
    force: bool,
    skip_existing: bool,
) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    valid_records: list[dict[str, Any]] = []
    push_documents: list[dict[str, Any]] = []
    doc_ids = [safe_text(row.get("doc_id")).strip() for row in records]
    duplicate_doc_ids = sorted(doc_id for doc_id, count in Counter(doc_ids).items() if doc_id and count > 1)
    dim_counts = Counter(embedding_dim(row) for row in records)
    source_type_counts = Counter(safe_text(row.get("source_type")).strip() or "unknown" for row in records)

    for index, record in enumerate(records, start=1):
        valid, row_warnings = validate_embedding(record, index)
        for warning in row_warnings:
            warnings.append({"doc_id": safe_text(record.get("doc_id")).strip(), "warning": warning})
        if valid is None:
            continue
        valid_records.append(valid)
        try:
            push_documents.append(build_push_document(valid, index))
        except Exception as exc:
            warnings.append({"doc_id": safe_text(record.get("doc_id")).strip(), "warning": str(exc)})

    existing_selected = {
        doc_id
        for doc_id in [safe_text(doc.get("fields", {}).get("id")).strip() for doc in push_documents]
        if doc_id in existing_doc_ids
    }
    selected_documents: list[dict[str, Any]] = []
    skipped_existing = 0
    planned_upserts = 0
    for document in push_documents:
        doc_id = safe_text(document.get("fields", {}).get("id")).strip()
        if doc_id in existing_selected:
            if force or not skip_existing:
                planned_upserts += 1
                selected_documents.append(document)
            else:
                skipped_existing += 1
            continue
        selected_documents.append(document)

    return {
        "input_total": len(records),
        "valid_total": len(valid_records),
        "source_type_counts": dict(source_type_counts),
        "empty_doc_id": sum(1 for value in doc_ids if not value),
        "empty_embedding": sum(1 for row in records if not row.get("embedding")),
        "embedding_dim_counts": dict(sorted(dim_counts.items())),
        "duplicate_doc_ids": duplicate_doc_ids,
        "warnings": warnings,
        "push_documents": push_documents,
        "selected_documents": selected_documents,
        "existing_doc_ids": existing_selected,
        "planned_new": len(selected_documents) - planned_upserts,
        "planned_skip": skipped_existing,
        "planned_upsert": planned_upserts,
    }


def print_summary(summary: dict[str, Any], paths: dict[str, Path], table_name: str, mapping_dim: int | None) -> None:
    print("Ingest embedding push plan:")
    print("- Input files:")
    for source_type, path in paths.items():
        print(f"  - {source_type}: {path}")
    print(f"- Source type counts: {json.dumps(summary['source_type_counts'], ensure_ascii=False)}")
    print(f"- Total embeddings: {summary['input_total']}")
    print(f"- Valid embeddings: {summary['valid_total']}")
    print(f"- Empty doc_id: {summary['empty_doc_id']}")
    print(f"- Empty embedding: {summary['empty_embedding']}")
    print(f"- Embedding dim distribution: {json.dumps(summary['embedding_dim_counts'], ensure_ascii=False)}")
    print(f"- Duplicate doc_id: {len(summary['duplicate_doc_ids'])}")
    print(f"- Target OpenSearch table: {table_name}")
    print(f"- Expected vector field: {VECTOR_FIELD}")
    print(f"- Mapping vector dim: {mapping_dim if mapping_dim is not None else 'unknown'}")
    print(f"- Planned new: {summary['planned_new']}")
    print(f"- Planned skip existing: {summary['planned_skip']}")
    print(f"- Planned upsert: {summary['planned_upsert']}")
    print(f"- Metadata warnings: {len(summary['warnings'])}")
    missing_source_doc_id = sum(
        1 for doc in summary["push_documents"] if not safe_text(doc.get("fields", {}).get("source_doc_id")).strip()
    )
    missing_product_id = sum(
        1 for doc in summary["push_documents"] if not safe_text(doc.get("fields", {}).get("product_id")).strip()
    )
    print(f"- Empty source_doc_id: {missing_source_doc_id}")
    print(f"- Empty product_id: {missing_product_id}")
    for warning in summary["warnings"][:20]:
        print(f"  [WARN] {warning['doc_id']}: {warning['warning']}")


def confirm_or_exit(yes: bool) -> None:
    if yes:
        print("Confirmation skipped because --yes was provided.")
        return
    answer = input("Type yes to push ingest embeddings to OpenSearch: ").strip()
    if answer != "yes":
        raise SystemExit("Aborted. No OpenSearch write was executed.")


def push_one_document(client: Any, models_module: Any, table_name: str, document: dict[str, Any]) -> tuple[bool, str]:
    request = build_push_request(models_module, [document])
    response = client.push_documents(table_name, PK_FIELD, request)
    ok, message = is_success_response(getattr(response, "body", None))
    return ok, message


def execute_push(client: Any, models_module: Any, table_name: str, documents: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    success_doc_ids: list[str] = []
    failures: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        doc_id = safe_text(document.get("fields", {}).get("id")).strip()
        try:
            ok, message = push_one_document(client, models_module, table_name, document)
            if ok:
                success_doc_ids.append(doc_id)
            else:
                failures.append({"doc_id": doc_id, "index": index, "reason": message})
        except Exception as exc:
            failures.append({"doc_id": doc_id, "index": index, "reason": str(exc)})
    return success_doc_ids, failures


def build_report_payload(
    *,
    mode: str,
    table_name: str,
    summary: dict[str, Any],
    success_doc_ids: list[str],
    failures: list[dict[str, Any]],
    mapping_dim: int | None,
    check_result: dict[str, Any] | None,
    source_type: str,
) -> dict[str, Any]:
    selected_doc_ids = [
        safe_text(document.get("fields", {}).get("id")).strip()
        for document in summary.get("selected_documents", [])
        if safe_text(document.get("fields", {}).get("id")).strip()
    ]
    skipped_doc_ids = sorted(summary.get("existing_doc_ids") or [])
    failed_doc_ids = [safe_text(item.get("doc_id")).strip() for item in failures if safe_text(item.get("doc_id")).strip()]
    return {
        "mode": mode,
        "target_index": table_name,
        "target_table": table_name,
        "vector_field": VECTOR_FIELD,
        "retrieval_table": (check_result or {}).get("retrieval_table") or TABLE_NAME,
        "push_target_table": (check_result or {}).get("push_target_table") or table_name,
        "table_alignment_ok": (check_result or {}).get("table_alignment_ok"),
        "source_type": source_type,
        "input_count": summary["input_total"],
        "valid_count": summary["valid_total"],
        "planned_new": summary["planned_new"],
        "planned_skip": summary["planned_skip"],
        "planned_upsert": summary["planned_upsert"],
        "success_count": len(success_doc_ids),
        "failed_count": len(failures),
        "skipped_count": summary["planned_skip"],
        "pushed_doc_ids": success_doc_ids,
        "failed_doc_ids": failed_doc_ids,
        "skipped_doc_ids": skipped_doc_ids,
        "selected_doc_ids": selected_doc_ids,
        "warnings": summary["warnings"],
        "mapping_vector_dim": mapping_dim,
        "check_result": check_result,
    }


def write_report(
    path: Path,
    *,
    mode: str,
    table_name: str,
    summary: dict[str, Any],
    success_doc_ids: list[str],
    failures: list[dict[str, Any]],
    elapsed_seconds: float,
    mapping_dim: int | None,
    check_result: dict[str, Any] | None,
    source_type: str,
) -> dict[str, Any]:
    payload = build_report_payload(
        mode=mode,
        table_name=table_name,
        summary=summary,
        success_doc_ids=success_doc_ids,
        failures=failures,
        mapping_dim=mapping_dim,
        check_result=check_result,
        source_type=source_type,
    )
    lines = [
        "# Ingest Embeddings Push Report",
        "",
        f"- Mode: `{mode}`",
        f"- Target table: `{table_name}`",
        f"- Vector field: `{VECTOR_FIELD}`",
        f"- Expected vector dim: {VECTOR_DIMENSIONS}",
        f"- Mapping vector dim: {mapping_dim if mapping_dim is not None else 'unknown'}",
        f"- Input embeddings: {summary['input_total']}",
        f"- Valid embeddings: {summary['valid_total']}",
        f"- Planned new: {summary['planned_new']}",
        f"- Planned skip existing: {summary['planned_skip']}",
        f"- Planned upsert: {summary['planned_upsert']}",
        f"- Success pushed: {len(success_doc_ids)}",
        f"- Failed: {len(failures)}",
        f"- Duplicate doc_id: {len(summary['duplicate_doc_ids'])}",
        f"- Warnings: {len(summary['warnings'])}",
        f"- Elapsed seconds: {elapsed_seconds:.2f}",
        "",
        "## OpenSearch Check",
        "",
    ]
    if check_result:
        for key, value in check_result.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- Not executed")
    lines.extend(["", "## Warning Samples", ""])
    if summary["warnings"]:
        for warning in summary["warnings"][:50]:
            lines.append(f"- `{warning['doc_id']}`: {warning['warning']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Failure Samples", ""])
    if failures:
        for failure in failures[:50]:
            lines.append(f"- `{failure.get('doc_id')}`: {failure.get('reason')}")
    else:
        lines.append("- None")
    lines.extend(["", "## Success Samples", ""])
    if success_doc_ids:
        for doc_id in success_doc_ids[:50]:
            lines.append(f"- `{doc_id}`")
    else:
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path = json_report_path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def append_push_manifest(records: list[dict[str, Any]], manifest_path: Path, success_doc_ids: list[str], failures: list[dict[str, Any]], table_name: str) -> None:
    docs_by_source: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in records:
        doc_id = safe_text(record.get("doc_id")).strip()
        if doc_id not in success_doc_ids:
            continue
        key = (safe_text(record.get("source_type")).strip(), safe_text(record.get("source_doc_id")).strip())
        docs_by_source[key].append(doc_id)
    for (source_type, source_doc_id), pushed_doc_ids in docs_by_source.items():
        append_manifest_record(
            {
                "event_type": "push",
                "source_type": source_type,
                "source_doc_id": source_doc_id,
                "source_path": "",
                "source_sha1": "",
                "product_id": "unknown",
                "generated_doc_ids": [],
                "pushed_doc_ids": pushed_doc_ids,
                "embedding_model": "",
                "vector_tables": [table_name],
                "status": "success",
                "push_status": "success",
                "push_detail": f"pushed={len(pushed_doc_ids)}, failures={len(failures)}",
                "pushed_at": now_iso(),
            },
            manifest_path,
        )


def check_opensearch(client: Any, models_module: Any, table_name: str, local_dims: set[int]) -> dict[str, Any]:
    mapping_info = get_mapping_info(client, models_module, table_name)
    mapping_dim = infer_mapping_vector_dim(mapping_info)
    dim_mismatch = bool(mapping_dim and local_dims and any(dim != mapping_dim for dim in local_dims if dim))
    if dim_mismatch:
        raise RuntimeError(f"embedding_dim 与 OpenSearch mapping 不匹配：local={sorted(local_dims)}, mapping={mapping_dim}")
    warnings = []
    if mapping_dim is None:
        warnings.append(f"{VECTOR_FIELD} mapping_dim unknown; cannot confirm local embedding dim compatibility")
    result = {
        "connectable": True,
        "table_name": table_name,
        "retrieval_table": TABLE_NAME,
        "push_target_table": table_name,
        "table_alignment_ok": table_name == TABLE_NAME,
        "table_exists": mapping_info.get("ok", True),
        "document_count": count_table_documents(client, models_module, table_name),
        "vector_field": VECTOR_FIELD,
        "mapping_vector_dim": mapping_dim,
        "local_embedding_dims": sorted(local_dims),
        "dim_match": mapping_dim is not None and not dim_mismatch,
        "warnings": warnings,
    }
    return result


def ensure_execute_safety(check_result: dict[str, Any] | None) -> None:
    if not check_result:
        raise RuntimeError("missing OpenSearch check_result; refusing execute")
    if not check_result.get("table_alignment_ok"):
        raise RuntimeError(
            "push target table does not match retrieval table: "
            f"push={check_result.get('push_target_table')}, retrieval={check_result.get('retrieval_table')}"
        )
    if check_result.get("mapping_vector_dim") is None:
        raise RuntimeError(f"{VECTOR_FIELD} mapping_dim unknown; refusing execute")
    if check_result.get("dim_match") is not True:
        raise RuntimeError(
            "embedding_dim 与 OpenSearch mapping 未确认匹配："
            f"local={check_result.get('local_embedding_dims')}, mapping={check_result.get('mapping_vector_dim')}"
        )


def normalize_metadata(value: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str) and value.strip():
        try:
            payload = json.loads(value)
        except Exception:
            return {}, "metadata is not valid JSON"
        if isinstance(payload, dict):
            return payload, None
    return {}, "metadata is not dict"


def verify_document_fields(doc_id: str, row: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    required = [
        "doc_id",
        "source_type",
        "source_doc_id",
        "source_sha1",
        "product_id",
        "content_sha1",
        "embedding_model",
        "embedding_dim",
    ]
    for field in required:
        value = row.get(field)
        if value in (None, "", []):
            warnings.append(f"{doc_id} missing {field}")
    if not safe_text(row.get("source_text") or row.get("text") or row.get("page_content")).strip():
        warnings.append(f"{doc_id} missing text/source_text/page_content")
    vector_present = VECTOR_FIELD in row
    vector = row.get(VECTOR_FIELD)
    if vector_present and (not isinstance(vector, list) or not vector):
        warnings.append(f"{doc_id} {VECTOR_FIELD} is empty or not list")
    try:
        expected_dim = int(row.get("embedding_dim") or 0)
    except Exception:
        expected_dim = 0
    if vector_present and isinstance(vector, list) and expected_dim and len(vector) != expected_dim:
        warnings.append(f"{doc_id} vector length {len(vector)} != embedding_dim {expected_dim}")
    if "metadata" in row:
        _metadata, metadata_warning = normalize_metadata(row.get("metadata"))
        if metadata_warning:
            warnings.append(f"{doc_id} {metadata_warning}")
    return warnings


def resolve_verify_doc_ids(
    *,
    explicit_doc_ids: list[str],
    report_json_path: Path,
    verify_limit: int,
) -> list[str]:
    if explicit_doc_ids:
        return explicit_doc_ids[:verify_limit]
    payload = load_json(report_json_path)
    doc_ids = payload.get("pushed_doc_ids")
    if not isinstance(doc_ids, list):
        doc_ids = []
    return [safe_text(doc_id).strip() for doc_id in doc_ids if safe_text(doc_id).strip()][:verify_limit]


def verify_pushed_documents(
    client: Any,
    models_module: Any,
    table_name: str,
    doc_ids: list[str],
) -> dict[str, Any]:
    found_rows = fetch_documents_by_doc_id(client, models_module, table_name, doc_ids)
    results = []
    missing_doc_ids = []
    warning_count = 0
    for doc_id in doc_ids:
        row = found_rows.get(doc_id)
        if not row:
            missing_doc_ids.append(doc_id)
            results.append({"doc_id": doc_id, "status": "missing", "warnings": []})
            continue
        warnings = verify_document_fields(doc_id, row)
        warning_count += len(warnings)
        results.append(
            {
                "doc_id": doc_id,
                "status": "found",
                "warnings": warnings,
                "embedding_dim": row.get("embedding_dim"),
                "vector_length": len(row.get(VECTOR_FIELD)) if isinstance(row.get(VECTOR_FIELD), list) else 0,
            }
        )
    if missing_doc_ids:
        status = "partial"
    elif warning_count:
        status = "partial"
    else:
        status = "success"
    return {
        "verify_status": status,
        "target_index": table_name,
        "vector_field": VECTOR_FIELD,
        "requested_doc_ids": doc_ids,
        "verified_doc_ids": [item["doc_id"] for item in results if item["status"] == "found"],
        "missing_doc_ids": missing_doc_ids,
        "warning_count": warning_count,
        "results": results,
        "verified_at": now_iso(),
    }


def write_verify_report(markdown_path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Ingest Push Verify Report",
        "",
        f"- Verify status: `{payload.get('verify_status')}`",
        f"- Target table: `{payload.get('target_index')}`",
        f"- Vector field: `{payload.get('vector_field')}`",
        f"- Requested doc_ids: {len(payload.get('requested_doc_ids') or [])}",
        f"- Found doc_ids: {len(payload.get('verified_doc_ids') or [])}",
        f"- Missing doc_ids: {len(payload.get('missing_doc_ids') or [])}",
        f"- Warning count: {payload.get('warning_count')}",
        f"- Verified at: `{payload.get('verified_at')}`",
        "",
        "## Results",
        "",
    ]
    for item in payload.get("results") or []:
        lines.append(f"- `{item.get('doc_id')}`: {item.get('status')}")
        for warning in item.get("warnings") or []:
            lines.append(f"  - warning: {warning}")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path = json_report_path(markdown_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_verify_manifest(payload: dict[str, Any], manifest_path: Path) -> None:
    append_manifest_record(
        {
            "event_type": "verify_push",
            "source_doc_id": "",
            "source_path": "",
            "source_sha1": "",
            "source_type": "",
            "product_id": "unknown",
            "generated_doc_ids": [],
            "verified_doc_ids": payload.get("verified_doc_ids") or [],
            "missing_doc_ids": payload.get("missing_doc_ids") or [],
            "warning_count": payload.get("warning_count") or 0,
            "status": payload.get("verify_status") or "unknown",
            "verify_status": payload.get("verify_status") or "unknown",
            "verified_at": payload.get("verified_at") or now_iso(),
        },
        manifest_path,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely push output/ingest_embeddings to OpenSearch text_docs.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--source-type", default="all", choices=("all", *SOURCE_TYPES))
    parser.add_argument("--input-dir", default="output/ingest_embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Plan only. Default unless --execute is provided.")
    parser.add_argument("--execute", action="store_true", help="Write selected embeddings to OpenSearch.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--force", action="store_true", help="Allow upsert when doc_id already exists.")
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--check-opensearch", action="store_true", help="Check OpenSearch connection/table/mapping only.")
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--verify-pushed", action="store_true", help="Read push_report.json and verify pushed doc_ids.")
    parser.add_argument("--verify-doc-id", action="append", default=[], help="Verify one doc_id. May be passed multiple times.")
    parser.add_argument("--verify-from-report", default=None, help="Read pushed_doc_ids from a push_report.json path.")
    parser.add_argument("--verify-limit", type=int, default=10, help="Verify at most N doc_ids. Default: 10.")
    parser.add_argument("--verify-output", default=DEFAULT_VERIFY_REPORT, help="Verify report markdown path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run and args.execute:
        raise SystemExit("--dry-run and --execute cannot be used together")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.verify_limit <= 0:
        raise SystemExit("--verify-limit must be positive")

    root = Path(args.root).resolve()
    input_dir = resolve_path(root, args.input_dir)
    report_path = resolve_path(root, args.report_output)
    manifest_path = resolve_path(root, args.manifest)
    verify_output_path = resolve_path(root, args.verify_output)
    source_types = selected_source_types(args.source_type)
    start = time.time()
    load_env(root)

    if args.verify_pushed or args.verify_doc_id:
        config = ensure_runtime_config()
        client, models_module = create_client(config)
        table_name, _checked_tables = resolve_push_target_table(client, models_module, config)
        report_json = (
            resolve_path(root, args.verify_from_report)
            if args.verify_from_report
            else json_report_path(report_path)
        )
        doc_ids = resolve_verify_doc_ids(
            explicit_doc_ids=args.verify_doc_id,
            report_json_path=report_json,
            verify_limit=args.verify_limit,
        )
        if not doc_ids:
            raise RuntimeError("没有可验证的 doc_id。请提供 --verify-doc-id 或确认 push_report.json 中有 pushed_doc_ids。")
        payload = verify_pushed_documents(client, models_module, table_name, doc_ids)
        write_verify_report(verify_output_path, payload)
        append_verify_manifest(payload, manifest_path)
        print(f"Verify complete: status={payload['verify_status']}, found={len(payload['verified_doc_ids'])}, missing={len(payload['missing_doc_ids'])}, warnings={payload['warning_count']}")
        print(f"Verify report: {verify_output_path}")
        return 0

    config = None
    client = None
    models_module = None
    table_name = TABLE_NAME
    check_result: dict[str, Any] | None = None
    mapping_dim: int | None = None
    existing_doc_ids: set[str] = set()

    records, paths = load_embedding_records(input_dir, source_types, args.limit)
    local_dims = {embedding_dim(record) for record in records if embedding_dim(record)}

    if args.execute or args.check_opensearch:
        config = ensure_runtime_config()
        client, models_module = create_client(config)
        table_name, checked_tables = resolve_push_target_table(client, models_module, config)
        check_result = check_opensearch(client, models_module, table_name, local_dims)
        check_result["checked_tables"] = checked_tables
        mapping_dim = check_result.get("mapping_vector_dim")
    else:
        table_name = TABLE_NAME

    if client is not None and models_module is not None and records:
        existing_doc_ids = fetch_existing_doc_ids(
            client,
            models_module,
            table_name,
            [safe_text(record.get("doc_id")).strip() for record in records if safe_text(record.get("doc_id")).strip()],
        )

    summary = analyze_records(records, existing_doc_ids=existing_doc_ids, force=args.force, skip_existing=args.skip_existing)
    print_summary(summary, paths, table_name, mapping_dim)

    if args.check_opensearch:
        print("OpenSearch check:")
        print(json.dumps(check_result, ensure_ascii=False, indent=2))
        write_report(
            report_path,
            mode="check-opensearch",
            table_name=table_name,
            summary=summary,
            success_doc_ids=[],
            failures=[],
            elapsed_seconds=time.time() - start,
            mapping_dim=mapping_dim,
            check_result=check_result,
            source_type=args.source_type,
        )
        return 0

    if not args.execute:
        write_report(
            report_path,
            mode="dry-run",
            table_name=table_name,
            summary=summary,
            success_doc_ids=[],
            failures=[],
            elapsed_seconds=time.time() - start,
            mapping_dim=mapping_dim,
            check_result=check_result,
            source_type=args.source_type,
        )
        print("Mode: dry-run; no OpenSearch write was executed.")
        print(f"Report: {report_path}")
        return 0

    ensure_execute_safety(check_result)
    confirm_or_exit(args.yes)
    assert client is not None and models_module is not None
    success_doc_ids, failures = execute_push(client, models_module, table_name, summary["selected_documents"])
    append_push_manifest(records, manifest_path, success_doc_ids, failures, table_name)
    write_report(
        report_path,
        mode="execute",
        table_name=table_name,
        summary=summary,
        success_doc_ids=success_doc_ids,
        failures=failures,
        elapsed_seconds=time.time() - start,
        mapping_dim=mapping_dim,
        check_result=check_result,
        source_type=args.source_type,
    )
    print(f"Push complete: success={len(success_doc_ids)}, failed={len(failures)}")
    print(f"Report: {report_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
