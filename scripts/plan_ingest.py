#!/usr/bin/env python3
"""Plan ingestion candidates without calling embedding or OpenSearch.

This script is read-only with respect to source data and existing output files.
It only writes the requested plan JSONL file so operators can inspect what a
future ingestion run would touch.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ("素材", "产品信息", "质检报告")
DEFAULT_OUTPUT = "output/ingest_plan.jsonl"
PRODUCT_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
PDF_EXTENSIONS = {".pdf"}


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def normalize_path(path: Path | str) -> str:
    return Path(path).as_posix()


def relative_path(path: Path, root: Path) -> str:
    try:
        return normalize_path(path.resolve().relative_to(root))
    except ValueError:
        return normalize_path(path.resolve())


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_key(value: Any) -> str:
    text = safe_text(value).lower()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_product_ids_by_source(root: Path) -> dict[str, list[str]]:
    by_source: dict[str, set[str]] = {}

    lookup = load_json(root / "output/catalog/product_lookup.json")
    if isinstance(lookup, dict):
        by_product_id = lookup.get("by_product_id")
        if isinstance(by_product_id, dict):
            for product_id, item in by_product_id.items():
                if not isinstance(item, dict):
                    continue
                source_file = normalize_path(safe_text(item.get("source_file")).strip())
                if source_file and product_id:
                    by_source.setdefault(source_file, set()).add(safe_text(product_id).strip())

    products = load_json(root / "output/products_enriched.json")
    if isinstance(products, list):
        for item in products:
            if not isinstance(item, dict):
                continue
            product_id = safe_text(item.get("product_id")).strip()
            source_file = normalize_path(safe_text(item.get("source_file")).strip())
            if product_id and source_file:
                by_source.setdefault(source_file, set()).add(product_id)

    return {source: sorted(product_ids) for source, product_ids in by_source.items()}


def load_quality_report_map(root: Path) -> dict[str, str]:
    path = root / "config/quality_report_product_map.csv"
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for row in reader:
            rel_path = normalize_path(safe_text(row.get("relative_path")).strip())
            product_id = safe_text(row.get("product_id")).strip()
            if rel_path and product_id:
                mapping[rel_path] = product_id
    return mapping


def load_quality_report_manifest(root: Path) -> dict[str, str]:
    payload = load_json(root / "output/quality_report_manifest.json")
    records: list[dict[str, Any]] = []
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        for rel_path, item in payload.items():
            if isinstance(item, dict):
                copied = dict(item)
                copied.setdefault("relative_path", rel_path)
                records.append(copied)

    mapping: dict[str, str] = {}
    for item in records:
        rel_path = normalize_path(safe_text(item.get("relative_path")).strip())
        product_id = safe_text(item.get("product_id")).strip()
        if rel_path and product_id:
            mapping[rel_path] = product_id
    return mapping


def load_product_name_index(root: Path) -> list[dict[str, str]]:
    products = load_json(root / "output/products_enriched.json")
    if not isinstance(products, list):
        return []
    index: list[dict[str, str]] = []
    for item in products:
        if not isinstance(item, dict):
            continue
        product_id = safe_text(item.get("product_id")).strip()
        product_name = safe_text(item.get("product_name")).strip()
        brand = safe_text(item.get("brand")).strip()
        series = safe_text(item.get("series")).strip()
        keys = {
            compact_key(product_name),
            compact_key(brand + product_name),
            compact_key(product_name + brand),
            compact_key(brand + series),
            compact_key(series),
        }
        keys.discard("")
        if product_id and keys:
            index.append({"product_id": product_id, "keys": "\n".join(sorted(keys))})
    return index


def infer_pdf_product_id(
    rel_path: str,
    quality_map: dict[str, str],
    quality_manifest: dict[str, str],
    product_name_index: list[dict[str, str]],
) -> str:
    if rel_path in quality_map:
        return quality_map[rel_path]
    if rel_path in quality_manifest:
        return quality_manifest[rel_path]

    file_key = compact_key(Path(rel_path).stem)
    dir_key = compact_key(Path(rel_path).parent.name)
    combined_key = compact_key(Path(rel_path).parent.name + Path(rel_path).stem)
    candidates = []
    for item in product_name_index:
        keys = set(item["keys"].splitlines())
        if file_key in keys or combined_key in keys:
            candidates.append(item["product_id"])
            continue
        for key in keys:
            if key and key in file_key and (not dir_key or dir_key in key or key in combined_key):
                candidates.append(item["product_id"])
                break
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else "unknown"


def source_type_for(path: Path, root: Path, *, explicit_file: bool = False) -> str | None:
    suffix = path.suffix.lower()
    rel = relative_path(path, root)
    parts = Path(rel).parts
    if path.name.startswith("~$"):
        return None
    if explicit_file:
        if suffix in PRODUCT_EXTENSIONS:
            return "product_excel"
        if suffix in PDF_EXTENSIONS:
            return "quality_report_pdf"
        return None
    if suffix in PRODUCT_EXTENSIONS and parts and parts[0] in {"素材", "产品信息"}:
        return "product_excel"
    if suffix in PDF_EXTENSIONS and parts and parts[0] == "质检报告":
        return "quality_report_pdf"
    return None


def source_doc_id_for(source_type: str, source_sha1: str) -> str:
    if source_type == "quality_report_pdf":
        return f"pdf__{source_sha1}"
    if source_type == "product_excel":
        return f"product_excel__{source_sha1}"
    return f"source__{source_sha1}"


def impact_types_for(source_type: str) -> list[str]:
    if source_type == "product_excel":
        return ["product_text", "image_text", "image_vector"]
    if source_type == "quality_report_pdf":
        return ["quality_report", "product_text"]
    return []


def vector_tables_for(source_type: str) -> list[str]:
    if source_type == "product_excel":
        return ["text_docs", "image_text_docs", "image_vectors"]
    if source_type == "quality_report_pdf":
        return ["text_docs"]
    return []


def format_product_id(product_ids: list[str]) -> str:
    if not product_ids:
        return "unknown"
    if len(product_ids) == 1:
        return product_ids[0]
    preview = ",".join(product_ids[:3])
    if len(product_ids) > 3:
        return f"{preview},...(+{len(product_ids) - 3})"
    return preview


def discover_files(source: Path, root: Path) -> list[Path]:
    if source.is_file():
        return [source] if source_type_for(source, root, explicit_file=True) else []
    if not source.exists():
        raise FileNotFoundError(f"source 不存在：{source}")
    files = []
    for path in source.rglob("*"):
        if path.is_file() and source_type_for(path, root):
            files.append(path)
    return sorted(files)


def build_plan(root: Path, sources: list[Path]) -> list[dict[str, Any]]:
    product_ids_by_source = load_product_ids_by_source(root)
    quality_map = load_quality_report_map(root)
    quality_manifest = load_quality_report_manifest(root)
    product_name_index = load_product_name_index(root)

    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for source in sources:
        for path in discover_files(source, root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)

            source_type = source_type_for(resolved, root, explicit_file=source.is_file())
            if not source_type:
                continue

            rel_path = relative_path(resolved, root)
            source_sha1 = sha1_file(resolved)
            stat = resolved.stat()
            product_ids: list[str] = []
            if source_type == "product_excel":
                product_ids = product_ids_by_source.get(rel_path, [])
            elif source_type == "quality_report_pdf":
                inferred = infer_pdf_product_id(rel_path, quality_map, quality_manifest, product_name_index)
                product_ids = [] if inferred == "unknown" else [inferred]

            records.append(
                {
                    "source_path": rel_path,
                    "source_type": source_type,
                    "source_sha1": source_sha1,
                    "source_doc_id": source_doc_id_for(source_type, source_sha1),
                    "modified_time": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                    "size": stat.st_size,
                    "impact_types": impact_types_for(source_type),
                    "affected_types": impact_types_for(source_type),
                    "vector_tables": vector_tables_for(source_type),
                    "product_id": format_product_id(product_ids),
                    "product_ids": product_ids,
                }
            )
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for record in records:
            file_obj.write(json.dumps(record, ensure_ascii=False) + "\n")


def shorten(value: Any, limit: int) -> str:
    text = safe_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def print_table(records: list[dict[str, Any]]) -> None:
    columns = [
        ("source_path", 34),
        ("source_type", 18),
        ("product_id", 28),
        ("impact_types", 32),
        ("size", 10),
        ("source_sha1", 12),
    ]
    header = "  ".join(name.ljust(width) for name, width in columns)
    print(header)
    print("  ".join("-" * width for _name, width in columns))
    for record in records:
        row_values = []
        for name, width in columns:
            value: Any = record.get(name)
            if isinstance(value, list):
                value = ",".join(safe_text(item) for item in value)
            if name == "source_sha1":
                value = safe_text(value)[:10]
            row_values.append(shorten(value, width).ljust(width))
        print("  ".join(row_values))
    print(f"\nTotal candidates: {len(records)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan ingestion candidates without mutating vector data.")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Source file or directory. May be passed multiple times. Default scans 素材/, 产品信息/, 质检报告/.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output JSONL path. Default: output/ingest_plan.jsonl.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode. This is always read-only except writing the plan JSONL.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = ROOT.resolve()
    source_values = args.source or list(DEFAULT_SOURCES)
    sources = [(root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve() for value in source_values]
    output_path = (root / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output).resolve()

    records = build_plan(root, sources)
    write_jsonl(output_path, records)
    print_table(records)
    print(f"Plan JSONL: {output_path}")
    print("Mode: dry-run; no embedding, OpenSearch write, or delete was executed.")
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
