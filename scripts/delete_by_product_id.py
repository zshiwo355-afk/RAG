#!/usr/bin/env python3
"""Delete OpenSearch records by product_id using local embedded artifacts as the id source of truth."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from push_text_docs_to_opensearch import (
    create_client,
    ensure_runtime_config,
    iter_batches,
    load_env,
    push_batch_with_retry,
    resolve_protocol,
    safe_text,
)


ROOT = Path(__file__).resolve().parents[1]
TABLE_INPUTS = {
    "text_docs": [
        "output/documents_embedded_v2.jsonl",
        "output/quality_report_documents_embedded.jsonl",
    ],
    "image_vectors": [
        "output/images_embedded.jsonl",
    ],
    "image_text_docs": [
        "output/image_text_embedded.jsonl",
    ],
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def collect_ids_for_product(root: Path, product_id: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {table: [] for table in TABLE_INPUTS}
    for table_name, rel_paths in TABLE_INPUTS.items():
        ids: set[str] = set()
        for rel_path in rel_paths:
            for record in load_jsonl(root / rel_path):
                record_product_id = safe_text(record.get("product_id")).strip()
                metadata = record.get("metadata") or {}
                if not record_product_id and isinstance(metadata, dict):
                    record_product_id = safe_text(metadata.get("product_id")).strip()
                if record_product_id != product_id:
                    continue
                if table_name == "text_docs":
                    record_id = safe_text(record.get("doc_id")).strip()
                elif table_name == "image_vectors":
                    record_id = safe_text(record.get("image_id")).strip()
                else:
                    record_id = safe_text(record.get("id")).strip() or safe_text(record.get("image_id")).strip()
                if record_id:
                    ids.add(record_id)
        result[table_name] = sorted(ids)
    return result


def build_delete_documents(record_ids: list[str]) -> list[dict[str, Any]]:
    return [{"cmd": "delete", "fields": {"id": record_id}} for record_id in record_ids]


def run_delete(
    config: dict[str, str],
    table_name: str,
    record_ids: list[str],
    batch_size: int,
    max_retries: int,
    retry_sleep: float,
) -> tuple[int, list[dict[str, Any]]]:
    if not record_ids:
        return 0, []
    client, models_module = create_client(config)
    failures: list[dict[str, Any]] = []
    success_count = 0
    full_table_name = f"{config['instance_id']}_{table_name}"
    total_batches = (len(record_ids) + batch_size - 1) // batch_size
    for batch_number, (start, batch_ids) in enumerate(iter_batches(record_ids, batch_size), start=1):
        end = start + len(batch_ids)
        print(f"Deleting {table_name} batch {batch_number}/{total_batches}: ids {start + 1}-{end}")
        ok, message, error_code = push_batch_with_retry(
            client=client,
            models_module=models_module,
            table_name=full_table_name,
            batch_documents=build_delete_documents(batch_ids),
            batch_number=batch_number,
            max_retries=max_retries,
            retry_sleep=retry_sleep,
        )
        if ok:
            success_count += len(batch_ids)
            continue
        failures.append(
            {
                "table_name": table_name,
                "batch_number": batch_number,
                "start_index": start + 1,
                "end_index": end,
                "count": len(batch_ids),
                "reason": message,
                "error_code": error_code,
                "sample_ids": batch_ids[:5],
            }
        )
    return success_count, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete OpenSearch records by product_id.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--product-id", required=True, help="Target product_id.")
    parser.add_argument("--batch-size", type=int, default=20, help="Delete batch size.")
    parser.add_argument("--max-retries", type=int, default=3, help="Retry count per failed batch.")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Base retry sleep seconds.")
    parser.add_argument("--execute", action="store_true", help="Actually delete from OpenSearch.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    load_env(root)

    collected = collect_ids_for_product(root, args.product_id)
    summary = {table: len(ids) for table, ids in collected.items()}
    print(json.dumps({"product_id": args.product_id, "matched_ids": summary}, ensure_ascii=False, indent=2))
    if not args.execute:
        print("Dry-run only. Pass --execute to delete from OpenSearch.")
        return 0

    config = ensure_runtime_config()
    print(f"OpenSearch protocol: {resolve_protocol(config['endpoint'])}")
    total_success = 0
    failures: list[dict[str, Any]] = []
    started = time.time()
    for table_name, record_ids in collected.items():
        success_count, failed = run_delete(
            config=config,
            table_name=table_name,
            record_ids=record_ids,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            retry_sleep=args.retry_sleep,
        )
        total_success += success_count
        failures.extend(failed)
    print(f"Delete complete: success={total_success}, failures={len(failures)}, elapsed={time.time() - started:.2f}s")
    if failures:
        print(json.dumps(failures, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
