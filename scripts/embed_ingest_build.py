#!/usr/bin/env python3
"""Embed standard ingest_build documents without writing OpenSearch.

This wrapper reads output/ingest_build/*/documents.json and writes local
embeddings JSONL files under output/ingest_embeddings/. It reuses the existing
DashScope text embedding request logic from scripts/embed_documents.py, but adds
dry-run by default, duplicate detection, and resumable append output.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from embed_documents import (  # type: ignore  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIMEOUT,
    DIMENSIONS,
    MODEL_NAME,
    embed_batch_with_retry,
    ensure_api_key,
    iter_batches,
    load_env,
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


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_documents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} 顶层必须是 list")
    return [item for item in payload if isinstance(item, dict)]


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


def selected_source_types(value: str) -> list[str]:
    if value == "all":
        return list(SOURCE_TYPES)
    return [value]


def metadata_of(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def normalize_document(document: dict[str, Any], source_type: str, index: int) -> tuple[dict[str, Any] | None, str | None]:
    metadata = metadata_of(document)
    text = safe_text(document.get("page_content") or document.get("text")).strip()
    doc_id = safe_text(metadata.get("doc_id") or document.get("doc_id")).strip()
    if not doc_id:
        return None, f"document #{index} missing doc_id"
    if not text:
        return None, f"{doc_id} text is empty"
    source_doc_id = safe_text(metadata.get("source_doc_id") or document.get("source_doc_id")).strip()
    source_sha1 = safe_text(metadata.get("source_sha1") or document.get("source_sha1")).strip()
    product_id = safe_text(metadata.get("product_id") or document.get("product_id")).strip()
    doc_type = safe_text(metadata.get("doc_type") or document.get("doc_type")).strip()
    field_name = safe_text(metadata.get("field_name") or document.get("field_name")).strip()
    content_sha1 = sha1_text(text)
    return {
        "doc_id": doc_id,
        "source_type": source_type,
        "source_doc_id": source_doc_id,
        "source_sha1": source_sha1,
        "product_id": product_id,
        "doc_type": doc_type,
        "field_name": field_name,
        "content_sha1": content_sha1,
        "page_content": text,
        "metadata": metadata,
    }, None


def existing_indexes(records: list[dict[str, Any]]) -> tuple[set[tuple[str, str, str]], dict[tuple[str, str], set[str]]]:
    exact: set[tuple[str, str, str]] = set()
    content_by_doc_model: dict[tuple[str, str], set[str]] = {}
    for record in records:
        doc_id = safe_text(record.get("doc_id")).strip()
        content_sha1 = safe_text(record.get("content_sha1")).strip()
        model = safe_text(record.get("embedding_model")).strip()
        if not doc_id or not model:
            continue
        if content_sha1:
            exact.add((doc_id, content_sha1, model))
            content_by_doc_model.setdefault((doc_id, model), set()).add(content_sha1)
    return exact, content_by_doc_model


def load_candidates(
    input_dir: Path,
    output_dir: Path,
    source_types: list[str],
    model: str,
    limit: int | None,
    force: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    all_candidates: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    source_stats: dict[str, Any] = {}
    remaining = limit

    for source_type in source_types:
        docs_path = input_dir / source_type / "documents.json"
        output_path = output_dir / source_type / "embeddings.jsonl"
        raw_docs = load_documents(docs_path)
        existing_records = read_jsonl(output_path)
        exact_existing, content_by_doc_model = existing_indexes(existing_records)
        embeddable: list[dict[str, Any]] = []
        changed = 0

        for index, raw_doc in enumerate(raw_docs, start=1):
            normalized, warning = normalize_document(raw_doc, source_type, index)
            if warning:
                warnings.append({"source_type": source_type, "warning": warning})
                continue
            assert normalized is not None
            if remaining is not None and remaining <= 0:
                continue
            key = (normalized["doc_id"], normalized["content_sha1"], model)
            doc_model_key = (normalized["doc_id"], model)
            old_hashes = content_by_doc_model.get(doc_model_key, set())
            if old_hashes and normalized["content_sha1"] not in old_hashes:
                normalized["embedding_plan_status"] = "changed"
                changed += 1
            elif key in exact_existing:
                normalized["embedding_plan_status"] = "existing"
            else:
                normalized["embedding_plan_status"] = "new"

            if not force and key in exact_existing:
                skipped_existing.append(normalized)
                continue

            embeddable.append(normalized)
            all_candidates.append(normalized)
            if remaining is not None:
                remaining -= 1

        source_stats[source_type] = {
            "documents_path": str(docs_path),
            "output_path": str(output_path),
            "input_documents": len(raw_docs),
            "existing_embeddings": len(existing_records),
            "skipped_existing": sum(1 for item in skipped_existing if item["source_type"] == source_type),
            "to_generate": len(embeddable),
            "changed": changed,
            "warnings": sum(1 for item in warnings if item["source_type"] == source_type),
        }

    return all_candidates, skipped_existing, warnings, source_stats


def print_plan(
    source_types: list[str],
    source_stats: dict[str, Any],
    model: str,
    output_dir: Path,
    batch_size: int,
    force: bool,
) -> None:
    print("Embedding plan:")
    print(f"- source_type: {', '.join(source_types)}")
    print(f"- embedding_model: {model}")
    print(f"- embedding_dim: {DIMENSIONS}")
    print(f"- batch_size: {batch_size}")
    print(f"- output_dir: {output_dir}")
    print(f"- force: {force}")
    for source_type in source_types:
        stats = source_stats[source_type]
        print(f"- {source_type}:")
        print(f"  input documents: {stats['input_documents']}")
        print(f"  existing embeddings: {stats['existing_embeddings']}")
        print(f"  skipped existing: {stats['skipped_existing']}")
        print(f"  changed content: {stats['changed']}")
        print(f"  to generate: {stats['to_generate']}")
        print(f"  output: {stats['output_path']}")
        if stats["warnings"]:
            print(f"  warnings: {stats['warnings']}")


def confirm_or_exit(yes: bool) -> None:
    if yes:
        print("Confirmation skipped because --yes was provided.")
        return
    answer = input("Type yes to call the embedding API and append local JSONL outputs: ").strip()
    if answer != "yes":
        raise SystemExit("Aborted. No embedding API call was executed.")


def write_embedding_record(path: Path, item: dict[str, Any], embedding: list[float], model: str) -> None:
    metadata = dict(item.get("metadata") or {})
    output = {
        "doc_id": item["doc_id"],
        "source_type": item["source_type"],
        "source_doc_id": item.get("source_doc_id") or "",
        "source_sha1": item.get("source_sha1") or "",
        "product_id": item.get("product_id") or "",
        "content_sha1": item["content_sha1"],
        "embedding_model": model,
        "embedding_dim": len(embedding),
        "text": item["page_content"],
        "page_content": item["page_content"],
        "doc_type": item.get("doc_type") or "",
        "field_name": item.get("field_name") or "",
        "metadata": metadata,
        "embedding": embedding,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(output, ensure_ascii=False) + "\n")


def write_report(
    path: Path,
    *,
    source_stats: dict[str, Any],
    warnings: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    success_count: int,
    elapsed_seconds: float,
    model: str,
    dry_run: bool,
) -> None:
    lines = [
        "# Ingest Build Embedding Report",
        "",
        f"- Mode: `{'dry-run' if dry_run else 'execute'}`",
        f"- Embedding model: `{model}`",
        f"- Embedding dim: {DIMENSIONS}",
        f"- Success embeddings: {success_count}",
        f"- Failed documents: {sum(item.get('count', 0) for item in failures)}",
        f"- Warnings: {len(warnings)}",
        f"- Elapsed seconds: {elapsed_seconds:.2f}",
        "",
        "## Source Types",
        "",
    ]
    for source_type, stats in source_stats.items():
        lines.extend(
            [
                f"### {source_type}",
                "",
                f"- Input documents: {stats['input_documents']}",
                f"- Existing embeddings: {stats['existing_embeddings']}",
                f"- Skipped existing: {stats['skipped_existing']}",
                f"- Changed content: {stats['changed']}",
                f"- To generate: {stats['to_generate']}",
                f"- Output: `{stats['output_path']}`",
                "",
            ]
        )
    lines.extend(["## Warnings", ""])
    if warnings:
        for warning in warnings[:50]:
            lines.append(f"- `{warning.get('source_type')}`: {warning.get('warning')}")
    else:
        lines.append("- None")
    lines.extend(["", "## Failures", ""])
    if failures:
        for failure in failures[:50]:
            lines.append(f"- batch {failure.get('batch_number')}: {failure.get('count')} docs; {failure.get('error')}")
    else:
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_execute(
    candidates: list[dict[str, Any]],
    output_dir: Path,
    model: str,
    batch_size: int,
    max_retries: int,
    retry_sleep: float,
    timeout: int,
) -> tuple[int, list[dict[str, Any]]]:
    success_count = 0
    failures: list[dict[str, Any]] = []
    total_batches = (len(candidates) + batch_size - 1) // batch_size
    for batch_index, (start, batch) in enumerate(iter_batches(candidates, batch_size), start=1):
        print(f"Embedding batch {batch_index}/{total_batches}: docs {start + 1}-{start + len(batch)}")
        request_batch = [
            {
                "doc_id": item["doc_id"],
                "page_content": item["page_content"],
                "metadata": item.get("metadata") or {},
            }
            for item in batch
        ]
        embeddings, error = embed_batch_with_retry(
            batch=request_batch,
            batch_number=batch_index,
            max_retries=max_retries,
            retry_sleep=retry_sleep,
            timeout=timeout,
        )
        if embeddings is None:
            failures.append(
                {
                    "batch_number": batch_index,
                    "start_index": start + 1,
                    "count": len(batch),
                    "error": error or "unknown error",
                    "doc_ids": [item["doc_id"] for item in batch],
                }
            )
            continue
        for item, embedding in zip(batch, embeddings):
            output_path = output_dir / item["source_type"] / "embeddings.jsonl"
            write_embedding_record(output_path, item, embedding, model)
            success_count += 1
    return success_count, failures


def print_embedding_status(output_dir: Path, source_types: list[str]) -> None:
    all_records: list[dict[str, Any]] = []
    for source_type in source_types:
        path = output_dir / source_type / "embeddings.jsonl"
        records = read_jsonl(path)
        print(f"- {source_type} embeddings.jsonl exists: {path.exists()}")
        print(f"  path: {path}")
        print(f"  records: {len(records)}")
        for record in records:
            copied = dict(record)
            copied["_source_type_from_path"] = source_type
            all_records.append(copied)

    doc_ids = [safe_text(record.get("doc_id")).strip() for record in all_records]
    doc_model_keys = [
        (safe_text(record.get("doc_id")).strip(), safe_text(record.get("embedding_model")).strip())
        for record in all_records
        if safe_text(record.get("doc_id")).strip()
    ]
    dim_counts = Counter(
        len(record.get("embedding"))
        for record in all_records
        if isinstance(record.get("embedding"), list)
    )
    source_type_counts = Counter(safe_text(record.get("source_type") or record.get("_source_type_from_path")).strip() for record in all_records)
    duplicate_doc_ids = sorted(doc_id for doc_id, count in Counter(doc_ids).items() if doc_id and count > 1)
    duplicate_doc_models = sorted(key for key, count in Counter(doc_model_keys).items() if key[0] and key[1] and count > 1)

    print("Embedding status:")
    print(f"- total embeddings: {len(all_records)}")
    print(f"- source_type counts: {json.dumps(dict(source_type_counts), ensure_ascii=False)}")
    print(f"- empty doc_id: {sum(1 for value in doc_ids if not value)}")
    print(f"- empty embedding: {sum(1 for record in all_records if not record.get('embedding'))}")
    print(f"- embedding_dim distribution: {json.dumps(dict(dim_counts), ensure_ascii=False)}")
    print(f"- duplicate doc_id: {len(duplicate_doc_ids)}")
    if duplicate_doc_ids:
        print(f"  samples: {duplicate_doc_ids[:10]}")
    print(f"- duplicate doc_id + embedding_model: {len(duplicate_doc_models)}")
    if duplicate_doc_models:
        print(f"  samples: {duplicate_doc_models[:10]}")
    print(f"- empty text: {sum(1 for record in all_records if not safe_text(record.get('text') or record.get('page_content')).strip())}")
    print("- Recent 10 embedding samples:")
    for record in all_records[-10:]:
        print(
            "  - "
            + json.dumps(
                {
                    "doc_id": record.get("doc_id"),
                    "source_type": record.get("source_type"),
                    "source_doc_id": record.get("source_doc_id"),
                    "product_id": record.get("product_id"),
                    "content_sha1": record.get("content_sha1"),
                    "embedding_model": record.get("embedding_model"),
                    "embedding_dim": record.get("embedding_dim") or (
                        len(record.get("embedding")) if isinstance(record.get("embedding"), list) else None
                    ),
                },
                ensure_ascii=False,
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed output/ingest_build documents into local JSONL files.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--source-type", default="all", choices=("all", *SOURCE_TYPES))
    parser.add_argument("--input-dir", default="output/ingest_build", help="Directory containing */documents.json.")
    parser.add_argument("--output-dir", default="output/ingest_embeddings", help="Directory for */embeddings.jsonl.")
    parser.add_argument("--model", default=MODEL_NAME, help="Embedding model. Defaults to the current project text model.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Embedding batch size.")
    parser.add_argument("--dry-run", action="store_true", help="Plan only. This is the default unless --execute is provided.")
    parser.add_argument("--execute", action="store_true", help="Call embedding API and append local embeddings JSONL.")
    parser.add_argument("--force", action="store_true", help="Generate even when doc_id + content_sha1 + model already exists.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N candidate documents.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation before calling embedding API.")
    parser.add_argument("--status", action="store_true", help="Check existing embedding JSONL files and exit.")
    parser.add_argument("--max-retries", type=int, default=3, help="Retry count per failed batch.")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Base retry sleep seconds.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP request timeout seconds.")
    args = parser.parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.max_retries <= 0:
        raise SystemExit("--max-retries must be positive")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.model != MODEL_NAME:
        raise SystemExit(f"当前 wrapper 只复用现有 embedding 逻辑，模型必须是 {MODEL_NAME!r}")
    return args


def main() -> int:
    args = parse_args()
    if args.dry_run and args.execute:
        raise SystemExit("--dry-run and --execute cannot be used together")
    root = Path(args.root).resolve()
    input_dir = resolve_path(root, args.input_dir)
    output_dir = resolve_path(root, args.output_dir)
    source_types = selected_source_types(args.source_type)
    report_path = output_dir / "embedding_report.md"

    if args.status:
        print_embedding_status(output_dir, source_types)
        return 0

    candidates, _skipped_existing, warnings, source_stats = load_candidates(
        input_dir=input_dir,
        output_dir=output_dir,
        source_types=source_types,
        model=args.model,
        limit=args.limit,
        force=args.force,
    )
    print_plan(source_types, source_stats, args.model, output_dir, args.batch_size, args.force)
    if warnings:
        print("Warnings:")
        for warning in warnings[:20]:
            print(f"- {warning['source_type']}: {warning['warning']}")
        if len(warnings) > 20:
            print(f"- ... {len(warnings) - 20} more")

    if not args.execute:
        print("Mode: dry-run; no embedding API call was executed and no embedding output was written.")
        write_report(
            report_path,
            source_stats=source_stats,
            warnings=warnings,
            failures=[],
            success_count=0,
            elapsed_seconds=0.0,
            model=args.model,
            dry_run=True,
        )
        print(f"Dry-run report: {report_path}")
        return 0

    if not candidates:
        print("No documents selected for embedding after duplicate detection.")
        write_report(
            report_path,
            source_stats=source_stats,
            warnings=warnings,
            failures=[],
            success_count=0,
            elapsed_seconds=0.0,
            model=args.model,
            dry_run=False,
        )
        return 0

    load_env(root)
    ensure_api_key()
    confirm_or_exit(args.yes)

    start_time = time.time()
    success_count, failures = run_execute(
        candidates=candidates,
        output_dir=output_dir,
        model=args.model,
        batch_size=args.batch_size,
        max_retries=args.max_retries,
        retry_sleep=args.retry_sleep,
        timeout=args.timeout,
    )
    elapsed_seconds = time.time() - start_time
    write_report(
        report_path,
        source_stats=source_stats,
        warnings=warnings,
        failures=failures,
        success_count=success_count,
        elapsed_seconds=elapsed_seconds,
        model=args.model,
        dry_run=False,
    )
    print(f"Embedding complete: success={success_count}, failed={sum(item['count'] for item in failures)}")
    print(f"Embedding report: {report_path}")
    print("OpenSearch push was not executed.")
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
