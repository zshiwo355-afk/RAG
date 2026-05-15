#!/usr/bin/env python3
"""
Export embedded Excel images and bind them back to cleaned product records.

The script reads output/products_cleaned.json, exports images that openpyxl can
see from .xlsx/.xlsm files, and writes:
  - output/products_cleaned.json with image_count/image_paths/image_anchor_cells
  - output/image_mapping.json
  - output/image_extract_report.md
"""

from __future__ import annotations

import argparse
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

try:
    from PIL import Image as PILImage
except Exception:  # Pillow is optional; openpyxl already returns image bytes.
    PILImage = None


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_SUFFIXES = {".xlsx", ".xlsm"}


def discover_excel_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and not path.name.startswith("~$")
    )


def slugify(value: str, max_length: int = 120) -> str:
    value = value.replace("/", "_").replace("\\", "_")
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_.")
    return (value or "unknown")[:max_length]


def source_slug(relative_file: str) -> str:
    path = Path(relative_file)
    no_suffix = str(path.with_suffix(""))
    return slugify(no_suffix)


def is_dispimg_formula(value: Any) -> bool:
    return isinstance(value, str) and "DISPIMG(" in value.upper()


def get_image_anchor(image: Any) -> tuple[int | None, int | None, str | None, str | None]:
    anchor = getattr(image, "anchor", None)
    if anchor is None:
        return None, None, None, "missing anchor"

    if isinstance(anchor, str):
        try:
            row, col = coordinate_to_tuple(anchor)
            return row, col, f"{get_column_letter(col)}{row}", None
        except Exception as exc:
            return None, None, None, f"invalid string anchor: {exc}"

    marker = getattr(anchor, "_from", None)
    if marker is None:
        return None, None, None, f"unsupported anchor type: {type(anchor).__name__}"

    try:
        row = marker.row + 1
        col = marker.col + 1
        return row, col, f"{get_column_letter(col)}{row}", None
    except Exception as exc:
        return None, None, None, f"failed to read anchor: {exc}"


def detect_image_extension(data: bytes, image_format: Any) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return "tif"

    fmt = str(image_format or "").lower().strip(".")
    if fmt in {"jpeg", "jpg"}:
        return "jpg"
    if fmt in {"png", "gif", "bmp", "tif", "tiff"}:
        return "tif" if fmt == "tiff" else fmt
    return "png"


def export_openpyxl_image(image: Any, output_path: Path) -> tuple[bool, str | None]:
    try:
        data = image._data()
    except Exception as exc:
        return False, f"openpyxl failed to read image bytes: {exc}"

    if not isinstance(data, bytes) or not data:
        return False, "openpyxl returned empty image bytes"

    # Validate/convert only when the byte signature is unclear. Most embedded
    # images can be preserved as-is, which avoids needless recompression.
    if PILImage is not None and output_path.suffix.lower() == ".png":
        try:
            with PILImage.open(io.BytesIO(data)) as pil_image:
                if pil_image.format and pil_image.format.lower() != "png":
                    output_path = output_path.with_suffix(f".{pil_image.format.lower()}")
                    output_path.write_bytes(data)
                    return True, None
        except Exception:
            pass

    output_path.write_bytes(data)
    return True, None


def scan_dispimg_formulas(ws, relative_file: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for row in ws.iter_rows():
        for cell in row:
            if not is_dispimg_formula(cell.value):
                continue
            markers.append(
                {
                    "source_type": "dispimg_formula",
                    "source_file": relative_file,
                    "source_sheet": ws.title,
                    "source_row": cell.row,
                    "source_col": cell.column,
                    "anchor_cell": cell.coordinate,
                    "image_index": None,
                    "image_path": None,
                    "exported": False,
                    "matched": False,
                    "match_status": "not_exported",
                    "error": "DISPIMG formula image marker is not exposed by openpyxl as embedded image bytes.",
                }
            )
    return markers


def load_products(products_path: Path) -> list[dict[str, Any]]:
    if not products_path.exists():
        raise FileNotFoundError(f"Missing cleaned products file: {products_path}")
    return json.loads(products_path.read_text(encoding="utf-8"))


def reset_product_image_fields(records: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    index: dict[tuple[str, str, int], dict[str, Any]] = {}
    for record in records:
        record["image_count"] = 0
        record["image_paths"] = []
        record["image_anchor_cells"] = []
        key = (record["source_file"], record["source_sheet"], int(record["source_row"]))
        index[key] = record
    return index


def append_image_to_record(record: dict[str, Any], image_path: str, anchor_cell: str) -> None:
    record.setdefault("image_paths", [])
    record.setdefault("image_anchor_cells", [])
    record["image_paths"].append(image_path)
    record["image_anchor_cells"].append(anchor_cell)
    record["image_count"] = len(record["image_paths"])
    record["has_image"] = True


def make_image_filename(
    relative_file: str,
    sheet_name: str,
    source_row: int | None,
    anchor_cell: str | None,
    image_index: int,
    extension: str,
) -> str:
    row_label = f"第{source_row}行" if source_row is not None else "未知行"
    anchor_label = anchor_cell or "未知锚点"
    stem = "__".join(
        [
            source_slug(relative_file),
            slugify(sheet_name),
            slugify(row_label),
            slugify(anchor_label),
            f"图{image_index:03d}",
        ]
    )
    return f"{stem}.{extension}"


def extract_images(
    root: Path,
    records: list[dict[str, Any]],
    images_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    images_dir.mkdir(parents=True, exist_ok=True)
    product_index = reset_product_image_fields(records)
    mapping: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "detected_embedded_images": 0,
        "exported_images": 0,
        "bound_images": 0,
        "unmatched_images": 0,
        "export_failed_images": 0,
        "dispimg_formula_markers": 0,
        "formula_markers_matched_to_records": 0,
        "errors": [],
    }

    for workbook_path in discover_excel_files(root):
        relative_file = str(workbook_path.relative_to(root))
        try:
            workbook = load_workbook(workbook_path, data_only=False)
        except Exception as exc:
            summary["errors"].append(
                {
                    "source_file": relative_file,
                    "source_sheet": None,
                    "error": f"failed to open workbook: {exc}",
                }
            )
            continue

        try:
            for ws in workbook.worksheets:
                formula_markers = scan_dispimg_formulas(ws, relative_file)
                for marker in formula_markers:
                    key = (marker["source_file"], marker["source_sheet"], marker["source_row"])
                    if key in product_index:
                        marker["matched"] = True
                        marker["match_status"] = "matched_not_exported"
                        summary["formula_markers_matched_to_records"] += 1
                    mapping.append(marker)
                summary["dispimg_formula_markers"] += len(formula_markers)

                for image_index, image in enumerate(getattr(ws, "_images", []), start=1):
                    summary["detected_embedded_images"] += 1
                    source_row, source_col, anchor_cell, anchor_error = get_image_anchor(image)
                    image_format = getattr(image, "format", None)
                    extension = detect_image_extension(b"", image_format)
                    filename = make_image_filename(
                        relative_file,
                        ws.title,
                        source_row,
                        anchor_cell,
                        image_index,
                        extension,
                    )
                    output_path = images_dir / filename

                    entry = {
                        "source_type": "openpyxl_image",
                        "source_file": relative_file,
                        "source_sheet": ws.title,
                        "source_row": source_row,
                        "source_col": source_col,
                        "anchor_cell": anchor_cell,
                        "image_index": image_index,
                        "image_path": str(output_path.relative_to(root)),
                        "exported": False,
                        "matched": False,
                        "match_status": "unmatched",
                        "error": anchor_error,
                    }

                    if anchor_error:
                        summary["export_failed_images"] += 1
                        entry["match_status"] = "export_failed"
                        mapping.append(entry)
                        continue

                    try:
                        data = image._data()
                        extension = detect_image_extension(data, image_format)
                        output_path = output_path.with_suffix(f".{extension}")
                        entry["image_path"] = str(output_path.relative_to(root))
                        output_path.write_bytes(data)
                        entry["exported"] = True
                        entry["error"] = None
                        summary["exported_images"] += 1
                    except Exception as exc:
                        summary["export_failed_images"] += 1
                        entry["match_status"] = "export_failed"
                        entry["error"] = f"failed to export image bytes: {exc}"
                        mapping.append(entry)
                        continue

                    key = (relative_file, ws.title, int(source_row))
                    record = product_index.get(key)
                    if record is None:
                        summary["unmatched_images"] += 1
                        entry["matched"] = False
                        entry["match_status"] = "unmatched"
                    else:
                        append_image_to_record(record, entry["image_path"], anchor_cell or "")
                        summary["bound_images"] += 1
                        entry["matched"] = True
                        entry["match_status"] = "matched"
                    mapping.append(entry)
        finally:
            workbook.close()

    for record in records:
        record["image_count"] = len(record.get("image_paths", []))
        record["image_paths"] = record.get("image_paths") or []
        record["image_anchor_cells"] = record.get("image_anchor_cells") or []
        record["has_image"] = bool(record.get("has_image")) or record["image_count"] > 0

    return mapping, summary


def group_rows_with_images(mapping: list[dict[str, Any]]) -> list[str]:
    grouped: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for item in mapping:
        if item.get("source_type") != "openpyxl_image" or not item.get("exported"):
            continue
        row = item.get("source_row")
        if row is None:
            continue
        grouped[(item["source_file"], item["source_sheet"], int(row))].append(item.get("anchor_cell") or "")

    lines = []
    for (source_file, source_sheet, source_row), anchors in sorted(grouped.items()):
        clean_anchors = ", ".join(anchor for anchor in anchors if anchor)
        lines.append(f"- `{source_file}` / `{source_sheet}` / 第 {source_row} 行: {len(anchors)} 张 ({clean_anchors})")
    return lines


def build_report(
    root: Path,
    records: list[dict[str, Any]],
    mapping: list[dict[str, Any]],
    summary: dict[str, Any],
) -> str:
    records_with_images = sum(1 for record in records if record.get("image_count", 0) > 0)
    openpyxl_items = [item for item in mapping if item.get("source_type") == "openpyxl_image"]
    unmatched = [item for item in openpyxl_items if item.get("exported") and not item.get("matched")]
    failed = [item for item in openpyxl_items if not item.get("exported")]
    formula_markers = [item for item in mapping if item.get("source_type") == "dispimg_formula"]

    per_file_counts = Counter(item["source_file"] for item in openpyxl_items if item.get("exported"))
    per_file_lines = [f"- `{source_file}`: {count} 张" for source_file, count in sorted(per_file_counts.items())]

    rows_lines = group_rows_with_images(mapping)
    unmatched_lines = [
        f"- `{item['source_file']}` / `{item['source_sheet']}` / {item.get('anchor_cell')}: {item.get('image_path')}"
        for item in unmatched
    ] or ["- 无"]
    failed_lines = [
        f"- `{item['source_file']}` / `{item['source_sheet']}` / {item.get('anchor_cell')}: {item.get('error')}"
        for item in failed
    ] or ["- 无"]

    formula_lines = []
    for item in formula_markers:
        formula_lines.append(
            f"- `{item['source_file']}` / `{item['source_sheet']}` / {item.get('anchor_cell')}: {item.get('match_status')}"
        )
    if not formula_lines:
        formula_lines = ["- 无"]

    return f"""# Excel 图片提取报告

## 总览

- 扫描目录：`{root}`
- 检测到 openpyxl 可读取的嵌入图片：{summary['detected_embedded_images']} 张
- 成功导出图片：{summary['exported_images']} 张
- 成功绑定图片：{summary['bound_images']} 张
- 已绑定到产品记录：{records_with_images} 条
- 未绑定图片：{summary['unmatched_images']} 张
- 导出失败图片：{summary['export_failed_images']} 张
- 检测到 DISPIMG 公式型图片标记：{summary['dispimg_formula_markers']} 个
- DISPIMG 标记匹配到产品记录但未导出：{summary['formula_markers_matched_to_records']} 个

## 每个文件导出图片数

{chr(10).join(per_file_lines) if per_file_lines else "- 无"}

## 存在图片的文件 / 行

{chr(10).join(rows_lines) if rows_lines else "- 无"}

## 未绑定图片

{chr(10).join(unmatched_lines)}

## 导出失败

{chr(10).join(failed_lines)}

## DISPIMG 公式型图片标记说明

这些单元格包含类似 `DISPIMG(...)` 的图片公式标记，但 openpyxl 未将其暴露为可直接导出的嵌入图片字节，因此本次不会生成真实图片文件。它们已记录在 `image_mapping.json`，但不计入成功导出图片数。

{chr(10).join(formula_lines)}

## 输出文件

- 图片目录：`output/images/`
- 图片映射：`output/image_mapping.json`
- 已更新产品数据：`output/products_cleaned.json`
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Excel embedded images and bind them to cleaned products.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--products", default="output/products_cleaned.json", help="Cleaned product JSON path.")
    parser.add_argument("--output", default="output", help="Output directory relative to root.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output_dir = (root / args.output).resolve()
    images_dir = output_dir / "images"
    products_path = (root / args.products).resolve()
    mapping_path = output_dir / "image_mapping.json"
    report_path = output_dir / "image_extract_report.md"

    records = load_products(products_path)
    mapping, summary = extract_images(root, records, images_dir)

    products_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(build_report(root, records, mapping, summary), encoding="utf-8")

    records_with_images = sum(1 for record in records if record.get("image_count", 0) > 0)
    print(f"Detected embedded images: {summary['detected_embedded_images']}")
    print(f"Exported images: {summary['exported_images']}")
    print(f"Bound images: {summary['bound_images']}")
    print(f"Product records with exported images: {records_with_images}")
    print(f"Unmatched exported images: {summary['unmatched_images']}")
    print(f"Output image directory: {images_dir}")


if __name__ == "__main__":
    main()
