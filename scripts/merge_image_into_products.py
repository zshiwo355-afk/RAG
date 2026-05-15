#!/usr/bin/env python3
"""
Backfill image metadata into product records.

Current stage:
- merge image paths, analysis results, and vector references into products
- do not write full embedding arrays into product files
- do not write to Qdrant
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

from embed_images import (
    enrich_mapping_item,
    load_json_list,
    load_product_lookup,
    normalize_rel_path,
    safe_text,
)


ROOT = Path(__file__).resolve().parents[1]
PRIMARY_SELECTION_RULE = (
    "优先选择分析成功的图片；其后按 caption 完整度、标签数量、OCR 是否存在、"
    "是否已有向量引用排序；若仍相同，则按原始 image_index/anchor_cell 顺序选择。"
)


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except Exception as exc:
                raise ValueError(f"JSONL 解析失败：{path} 第 {line_number} 行：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL 顶层必须是 object：{path} 第 {line_number} 行")
            records.append(payload)
    return records


def normalize_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def row_key(source_file: Any, source_sheet: Any, source_row: Any) -> tuple[str, str, int | None]:
    return (
        safe_text(source_file).strip(),
        safe_text(source_sheet).strip(),
        normalize_int(source_row),
    )


def image_record_key(item: dict[str, Any], index: int) -> str:
    image_id = safe_text(item.get("image_id")).strip()
    if image_id:
        return image_id
    image_path = normalize_rel_path(item.get("image_path"))
    if image_path:
        return f"path::{image_path}"
    return "synthetic::{source_file}::{source_sheet}::{source_row}::{anchor_cell}::{image_index}::{index}".format(
        source_file=safe_text(item.get("source_file")).strip(),
        source_sheet=safe_text(item.get("source_sheet")).strip(),
        source_row=safe_text(item.get("source_row")).strip(),
        anchor_cell=safe_text(item.get("anchor_cell")).strip(),
        image_index=safe_text(item.get("image_index")).strip(),
        index=index,
    )


def prefer_value(current: Any, incoming: Any) -> Any:
    if incoming is None:
        return current
    if isinstance(incoming, str) and not incoming.strip():
        return current
    if isinstance(incoming, list) and not incoming:
        return current
    if isinstance(incoming, dict) and not incoming:
        return current
    return incoming


def base_image_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "image_id": safe_text(item.get("image_id")).strip() or None,
        "image_path": normalize_rel_path(item.get("image_path")) or None,
        "product_id": item.get("product_id"),
        "source_file": item.get("source_file"),
        "source_sheet": item.get("source_sheet"),
        "source_row": normalize_int(item.get("source_row")),
        "anchor_cell": safe_text(item.get("anchor_cell")).strip() or None,
        "matched": item.get("matched"),
        "match_status": item.get("match_status"),
        "source_type": item.get("source_type"),
        "image_index": normalize_int(item.get("image_index")),
        "analysis_status": None,
        "caption": "",
        "visual_tags": [],
        "ocr_text": "",
        "packaging_type": "",
        "gift_style": "",
        "scene_hint": "",
        "analysis_error": None,
        "warnings": [],
        "has_embedding": False,
        "vector_ref": None,
        "mapping_error": item.get("mapping_error"),
    }


def merge_mapping_items(
    mapping_path: Path,
    documents_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str, int | None], str]]:
    product_by_row, product_by_image = load_product_lookup(documents_path)
    raw_mapping = load_json_list(mapping_path, "image_mapping.json") if mapping_path.exists() else []
    records: dict[str, dict[str, Any]] = {}

    for index, raw_item in enumerate(raw_mapping, start=1):
        enriched = enrich_mapping_item(raw_item, index, product_by_row, product_by_image)
        key = image_record_key(enriched, index)
        records[key] = base_image_record(enriched)

    return records, product_by_row


def overlay_analysis(records: dict[str, dict[str, Any]], analysis_path: Path) -> None:
    for index, item in enumerate(load_jsonl_records(analysis_path), start=1):
        key = image_record_key(item, index)
        record = records.setdefault(key, base_image_record(item))
        for field in [
            "image_id",
            "image_path",
            "product_id",
            "source_file",
            "source_sheet",
            "source_row",
            "anchor_cell",
            "matched",
            "match_status",
            "caption",
            "ocr_text",
            "packaging_type",
            "gift_style",
            "scene_hint",
        ]:
            record[field] = prefer_value(record.get(field), item.get(field))

        visual_tags = item.get("visual_tags")
        if isinstance(visual_tags, list) and visual_tags:
            record["visual_tags"] = [safe_text(tag).strip() for tag in visual_tags if safe_text(tag).strip()]

        record["analysis_status"] = prefer_value(record.get("analysis_status"), item.get("analysis_status"))
        record["analysis_error"] = prefer_value(record.get("analysis_error"), item.get("error"))
        warnings = item.get("warnings")
        if isinstance(warnings, list) and warnings:
            record["warnings"] = [safe_text(warning).strip() for warning in warnings if safe_text(warning).strip()]


def overlay_embeddings(records: dict[str, dict[str, Any]], embedded_path: Path) -> None:
    for index, item in enumerate(load_jsonl_records(embedded_path), start=1):
        key = image_record_key(item, index)
        record = records.setdefault(key, base_image_record(item))
        for field in [
            "image_id",
            "image_path",
            "product_id",
            "source_file",
            "source_sheet",
            "source_row",
            "anchor_cell",
            "matched",
            "match_status",
            "source_type",
        ]:
            record[field] = prefer_value(record.get(field), item.get(field))

        record["image_index"] = prefer_value(record.get("image_index"), normalize_int(item.get("image_index")))
        record["has_embedding"] = True
        record["vector_ref"] = {
            "image_id": record.get("image_id"),
            "image_path": record.get("image_path"),
            "product_id": record.get("product_id"),
            "source_file": record.get("source_file"),
            "source_sheet": record.get("source_sheet"),
            "source_row": record.get("source_row"),
            "anchor_cell": record.get("anchor_cell"),
            "matched": record.get("matched"),
            "match_status": record.get("match_status"),
        }


def stable_unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = safe_text(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def image_original_sort_key(record: dict[str, Any]) -> tuple[int, str, str]:
    image_index = normalize_int(record.get("image_index"))
    return (
        image_index if image_index is not None else 10**9,
        safe_text(record.get("anchor_cell")).strip(),
        safe_text(record.get("image_path")).strip(),
    )


def primary_image_sort_key(record: dict[str, Any]) -> tuple[int, int, int, int, int, int, str, str]:
    analysis_success = 1 if safe_text(record.get("analysis_status")).strip() == "success" else 0
    caption_score = len(safe_text(record.get("caption")).strip())
    tag_score = len(record.get("visual_tags") or [])
    ocr_score = 1 if safe_text(record.get("ocr_text")).strip() else 0
    vector_score = 1 if record.get("has_embedding") else 0
    matched_score = 1 if record.get("matched") is True else 0
    image_index = normalize_int(record.get("image_index"))
    return (
        -analysis_success,
        -caption_score,
        -tag_score,
        -ocr_score,
        -vector_score,
        -matched_score,
        f"{image_index:09d}" if image_index is not None else "999999999",
        safe_text(record.get("image_path")).strip(),
    )


def summarize_analysis_status(records: list[dict[str, Any]]) -> str:
    if not records:
        return "no_images"
    statuses = [safe_text(record.get("analysis_status")).strip() for record in records]
    success_count = sum(1 for status in statuses if status == "success")
    if success_count == len(records):
        return "all_success"
    if success_count > 0:
        return "partial_success"
    return "all_failed"


def backfill_products(
    products: list[dict[str, Any]],
    row_to_product_id: dict[tuple[str, str, int | None], str],
    image_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    images_by_product_id: dict[str, list[dict[str, Any]]] = {}
    for record in image_records.values():
        product_id = safe_text(record.get("product_id")).strip()
        if not product_id:
            continue
        images_by_product_id.setdefault(product_id, []).append(record)

    enriched_products: list[dict[str, Any]] = []
    for product in products:
        item = dict(product)
        product_key = row_key(product.get("source_file"), product.get("source_sheet"), product.get("source_row"))
        product_id = row_to_product_id.get(product_key)
        item["product_id"] = product_id

        image_records_for_product = list(images_by_product_id.get(product_id or "", []))
        image_records_for_product.sort(key=image_original_sort_key)
        primary_record = min(image_records_for_product, key=primary_image_sort_key) if image_records_for_product else None

        image_ids = stable_unique_strings([record.get("image_id") for record in image_records_for_product])
        image_paths = stable_unique_strings([record.get("image_path") for record in image_records_for_product])
        image_captions = stable_unique_strings([record.get("caption") for record in image_records_for_product])
        image_ocr_texts = stable_unique_strings([record.get("ocr_text") for record in image_records_for_product])

        image_tags = stable_unique_strings(
            [tag for record in image_records_for_product for tag in (record.get("visual_tags") or [])]
        )
        primary_image_tags = list(primary_record.get("visual_tags") or []) if primary_record else []

        vector_refs = [
            record["vector_ref"]
            for record in image_records_for_product
            if isinstance(record.get("vector_ref"), dict)
        ]

        item["image_ids"] = image_ids
        item["image_paths"] = image_paths
        item["primary_image_path"] = primary_record.get("image_path") if primary_record else None
        item["image_count"] = len(image_paths)
        item["image_captions"] = image_captions
        item["primary_image_caption"] = safe_text(primary_record.get("caption")).strip() if primary_record else ""
        item["image_tags"] = image_tags
        item["primary_image_tags"] = primary_image_tags
        item["image_ocr_texts"] = image_ocr_texts
        item["primary_image_ocr_text"] = safe_text(primary_record.get("ocr_text")).strip() if primary_record else ""
        item["image_analysis_status"] = summarize_analysis_status(image_records_for_product)
        item["image_vector_refs"] = vector_refs
        item["has_image"] = len(image_paths) > 0

        enriched_products.append(item)

    return enriched_products


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_cell(row.get(key)) for key in fieldnames})


def build_report(enriched_products: list[dict[str, Any]]) -> str:
    total_products = len(enriched_products)
    with_images = [item for item in enriched_products if (item.get("image_count") or 0) > 0]
    no_images = [item for item in enriched_products if (item.get("image_count") or 0) == 0]
    caption_products = [item for item in enriched_products if item.get("image_captions")]
    ocr_products = [item for item in enriched_products if item.get("image_ocr_texts")]
    tags_products = [item for item in enriched_products if item.get("image_tags")]
    multi_image_products = [item for item in enriched_products if (item.get("image_count") or 0) > 1]

    preview_lines = [
        "| # | product_id | product_name | image_count | primary_image_path | primary_image_caption | primary_image_tags |",
        "| ---: | --- | --- | ---: | --- | --- | --- |",
    ]
    for index, item in enumerate(with_images[:10], start=1):
        preview_lines.append(
            f"| {index} | `{item.get('product_id') or ''}` | "
            f"{safe_text(item.get('product_name')).strip()} | "
            f"{item.get('image_count') or 0} | "
            f"`{item.get('primary_image_path') or ''}` | "
            f"{safe_text(item.get('primary_image_caption')).strip()[:120]} | "
            f"{', '.join(item.get('primary_image_tags') or [])} |"
        )
    if len(preview_lines) == 2:
        preview_lines.append("| 1 | - | - | 0 | - | - | - |")

    analysis_counter = Counter(safe_text(item.get("image_analysis_status")).strip() for item in enriched_products)
    analysis_lines = [
        "| image_analysis_status | 数量 |",
        "| --- | ---: |",
    ]
    for status, count in analysis_counter.most_common():
        analysis_lines.append(f"| `{status or 'unknown'}` | {count} |")

    return f"""# 图片回填报告

## 总览

- 产品总数：{total_products}
- 成功回填图片的产品数：{len(with_images)}
- 无图片产品数：{len(no_images)}
- 成功回填 caption 的产品数：{len(caption_products)}
- 成功回填 OCR 的产品数：{len(ocr_products)}
- 成功回填 tags 的产品数：{len(tags_products)}
- 多图产品数：{len(multi_image_products)}

## primary_image_path 选择规则

- {PRIMARY_SELECTION_RULE}

## 图片分析状态分布

{chr(10).join(analysis_lines)}

## 前 10 条增强产品预览

{chr(10).join(preview_lines)}
"""


def run_merge(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    output_dir = root / "output"
    products_path = root / args.products
    mapping_path = root / args.mapping
    analysis_path = root / args.analysis
    embedded_path = root / args.embedded
    documents_path = root / args.documents

    products = load_json_list(products_path, "products_cleaned.json")
    image_records, row_to_product_id = merge_mapping_items(mapping_path, documents_path)
    overlay_analysis(image_records, analysis_path)
    overlay_embeddings(image_records, embedded_path)

    if documents_path.exists():
        documents_row_lookup, _ = load_product_lookup(documents_path)
        row_to_product_id = documents_row_lookup or row_to_product_id

    enriched_products = backfill_products(products, row_to_product_id, image_records)

    json_output_path = output_dir / "products_enriched.json"
    csv_output_path = output_dir / "products_enriched.csv"
    report_path = output_dir / "image_backfill_report.md"

    json_output_path.write_text(
        json.dumps(enriched_products, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(csv_output_path, enriched_products)
    report_path.write_text(build_report(enriched_products), encoding="utf-8")

    print(f"Products total: {len(enriched_products)}")
    print(f"Products with images: {sum(1 for item in enriched_products if (item.get('image_count') or 0) > 0)}")
    print(f"JSON output: {json_output_path}")
    print(f"CSV output: {csv_output_path}")
    print(f"Report: {report_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge image metadata back into products.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--products", default="output/products_cleaned.json", help="Products JSON path.")
    parser.add_argument("--mapping", default="output/image_mapping.json", help="Image mapping JSON path.")
    parser.add_argument("--analysis", default="output/image_analysis.jsonl", help="Image analysis JSONL path.")
    parser.add_argument("--embedded", default="output/images_embedded.jsonl", help="Image embedding JSONL path.")
    parser.add_argument(
        "--documents",
        default="output/documents_preview.json",
        help="Documents preview JSON path for product_id lookup.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        run_merge(parse_args())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
