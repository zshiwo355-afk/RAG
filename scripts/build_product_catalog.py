#!/usr/bin/env python3
"""
Build a stable product catalog without depending on traversal order.

Inputs:
- output/products_enriched.json or output/products_cleaned.json

Outputs:
- output/catalog/products_master.jsonl
- output/catalog/product_lookup.json
- output/catalog/product_catalog_report.md
- config/product_catalog_registry.csv
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from source_identity import (
    compact_text,
    make_catalog_fingerprint,
    make_source_locator,
    normalize_int,
    normalize_path,
    safe_text,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = "output/products_enriched.json"
DEFAULT_OUTPUT_DIR = "output/catalog"
DEFAULT_REGISTRY = "config/product_catalog_registry.csv"

REGISTRY_COLUMNS = [
    "product_id",
    "source_file",
    "source_sheet",
    "source_row",
    "product_name",
    "brand",
    "series",
    "spec",
    "manufacturer",
    "catalog_fingerprint",
    "source_locator",
    "status",
    "created_at",
    "updated_at",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json_list(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"{label} 不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{label} 顶层必须是 list：{path}")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{label} 第 {index} 项必须是 object")
        result.append(item)
    return result


def load_registry(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        return [{key: safe_text(value) for key, value in row.items()} for row in reader]


def save_registry(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=REGISTRY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: safe_text(row.get(column)) for column in REGISTRY_COLUMNS})


def build_registry_indexes(rows: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    by_locator: dict[str, dict[str, str]] = {}
    by_fingerprint: dict[str, dict[str, str]] = {}
    for row in rows:
        locator = safe_text(row.get("source_locator")).strip()
        fingerprint = safe_text(row.get("catalog_fingerprint")).strip()
        if locator:
            by_locator[locator] = row
        if fingerprint and fingerprint not in by_fingerprint:
            by_fingerprint[fingerprint] = row
    return by_locator, by_fingerprint


def next_product_id(existing_rows: list[dict[str, str]]) -> str:
    max_number = 0
    for row in existing_rows:
        product_id = safe_text(row.get("product_id")).strip()
        if product_id.startswith("prd_"):
            try:
                max_number = max(max_number, int(product_id.split("_", 1)[1]))
            except Exception:
                continue
    return f"prd_{max_number + 1:06d}"


def sort_key(record: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        normalize_path(record.get("source_file")),
        safe_text(record.get("source_sheet")).strip(),
        normalize_int(record.get("source_row")) or 0,
        compact_text(record.get("product_name")).lower(),
    )


def build_master_record(record: dict[str, Any], product_id: str, source_locator: str, fingerprint: str) -> dict[str, Any]:
    item = dict(record)
    item["product_id"] = product_id
    item["catalog_source_locator"] = source_locator
    item["catalog_fingerprint"] = fingerprint
    return item


def build_registry_row(record: dict[str, Any], product_id: str, source_locator: str, fingerprint: str, created_at: str) -> dict[str, str]:
    return {
        "product_id": product_id,
        "source_file": normalize_path(record.get("source_file")),
        "source_sheet": safe_text(record.get("source_sheet")).strip(),
        "source_row": safe_text(normalize_int(record.get("source_row")) or ""),
        "product_name": compact_text(record.get("product_name")),
        "brand": compact_text(record.get("brand")),
        "series": compact_text(record.get("series")),
        "spec": compact_text(record.get("spec")),
        "manufacturer": compact_text(record.get("manufacturer")),
        "catalog_fingerprint": fingerprint,
        "source_locator": source_locator,
        "status": "active",
        "created_at": created_at,
        "updated_at": now_iso(),
    }


def build_lookup(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_product_id = {}
    by_source_locator = {}
    by_fingerprint = {}
    for item in records:
        product_id = safe_text(item.get("product_id")).strip()
        locator = safe_text(item.get("catalog_source_locator")).strip()
        fingerprint = safe_text(item.get("catalog_fingerprint")).strip()
        if product_id:
            by_product_id[product_id] = {
                "product_name": safe_text(item.get("product_name")).strip(),
                "brand": safe_text(item.get("brand")).strip(),
                "source_file": normalize_path(item.get("source_file")),
                "source_sheet": safe_text(item.get("source_sheet")).strip(),
                "source_row": normalize_int(item.get("source_row")),
            }
        if locator:
            by_source_locator[locator] = product_id
        if fingerprint:
            by_fingerprint.setdefault(fingerprint, []).append(product_id)
    return {
        "by_product_id": by_product_id,
        "by_source_locator": by_source_locator,
        "by_fingerprint": by_fingerprint,
    }


def build_report(
    input_path: Path,
    master_path: Path,
    lookup_path: Path,
    registry_path: Path,
    total_records: int,
    reused_by_locator: int,
    reused_by_fingerprint: int,
    created_new: int,
) -> str:
    return f"""# Product Catalog Build Report

## Summary

- Input file: `{input_path}`
- Output master: `{master_path}`
- Output lookup: `{lookup_path}`
- Registry: `{registry_path}`
- Total products: {total_records}
- Reused by exact source locator: {reused_by_locator}
- Reused by business fingerprint: {reused_by_fingerprint}
- Newly assigned product_id: {created_new}

## Notes

- This phase-1 catalog removes traversal-order dependency from `product_id`.
- It does not rewrite existing `products_enriched.json`.
- Future rebuilds should consume `products_master.jsonl` as the product entity source of truth.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build stable product catalog from product records.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Input products JSON path.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Catalog output directory.")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Catalog registry CSV path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    input_path = (root / args.input).resolve()
    output_dir = (root / args.output_dir).resolve()
    registry_path = (root / args.registry).resolve()

    records = sorted(load_json_list(input_path, input_path.name), key=sort_key)
    registry_rows = load_registry(registry_path)
    by_locator, by_fingerprint = build_registry_indexes(registry_rows)

    reused_by_locator = 0
    reused_by_fingerprint = 0
    created_new = 0
    master_records: list[dict[str, Any]] = []
    updated_registry_rows: list[dict[str, str]] = []

    for record in records:
        locator = make_source_locator(record)
        fingerprint = make_catalog_fingerprint(record)
        existing = by_locator.get(locator)
        if existing is not None:
            product_id = safe_text(existing.get("product_id")).strip()
            reused_by_locator += 1
            created_at = safe_text(existing.get("created_at")).strip() or now_iso()
        else:
            existing = by_fingerprint.get(fingerprint)
            if existing is not None:
                product_id = safe_text(existing.get("product_id")).strip()
                reused_by_fingerprint += 1
                created_at = safe_text(existing.get("created_at")).strip() or now_iso()
            else:
                product_id = next_product_id(registry_rows + updated_registry_rows)
                created_new += 1
                created_at = now_iso()

        master_records.append(build_master_record(record, product_id, locator, fingerprint))
        updated_registry_rows.append(build_registry_row(record, product_id, locator, fingerprint, created_at))

    output_dir.mkdir(parents=True, exist_ok=True)
    master_path = output_dir / "products_master.jsonl"
    lookup_path = output_dir / "product_lookup.json"
    report_path = output_dir / "product_catalog_report.md"

    with master_path.open("w", encoding="utf-8") as file_obj:
        for item in master_records:
            file_obj.write(json.dumps(item, ensure_ascii=False) + "\n")

    lookup_path.write_text(json.dumps(build_lookup(master_records), ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(
        build_report(
            input_path=input_path,
            master_path=master_path,
            lookup_path=lookup_path,
            registry_path=registry_path,
            total_records=len(master_records),
            reused_by_locator=reused_by_locator,
            reused_by_fingerprint=reused_by_fingerprint,
            created_new=created_new,
        ),
        encoding="utf-8",
    )
    save_registry(registry_path, updated_registry_rows)

    print(f"Catalog records: {len(master_records)}")
    print(f"Reused by locator: {reused_by_locator}")
    print(f"Reused by fingerprint: {reused_by_fingerprint}")
    print(f"New product ids: {created_new}")
    print(f"Master output: {master_path}")
    print(f"Lookup output: {lookup_path}")
    print(f"Registry output: {registry_path}")


if __name__ == "__main__":
    main()

