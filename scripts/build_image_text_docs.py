#!/usr/bin/env python3
"""
Build semantic text documents from image analysis results for text-to-image search.

Input:
- output/image_analysis.jsonl

Outputs:
- output/image_text_docs.jsonl
- output/image_text_docs_report.md
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import time
from pathlib import Path
from typing import Any

from source_identity import build_image_source_doc_id, load_url_mapping, resolve_mapped_url


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OCR_MAX_CHARS = 1500
DEFAULT_IMAGE_URL_MAPPING = "output/image_url_mapping.json"
NOISE_LINE_PATTERNS = [
    re.compile(r"^[\W_]+$"),
    re.compile(r"^\d{1,4}$"),
    re.compile(r"^[A-Za-z]{1,2}$"),
    re.compile(r"^[A-Za-z0-9]{1,3}$"),
]


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def compact_whitespace(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def normalize_text(value: Any) -> str:
    text = safe_text(value)
    text = text.replace("\u3000", " ")
    lines = [compact_whitespace(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def is_noise_ocr_line(line: str) -> bool:
    stripped = compact_whitespace(line)
    if not stripped:
        return True
    if len(stripped) <= 3 and any(pattern.match(stripped) for pattern in NOISE_LINE_PATTERNS):
        return True
    if len(stripped) == 1 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", stripped):
        return True
    return False


def clean_ocr_text(value: Any, max_chars: int) -> str:
    text = normalize_text(value)
    if not text:
        return ""

    cleaned_lines: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = compact_whitespace(line)
        if is_noise_ocr_line(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "..."


def normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = [safe_text(item).strip() for item in value]
    elif isinstance(value, str):
        raw_items = [part.strip() for part in re.split(r"[,\n，、;；]+", value)]
    else:
        raw_items = []

    tags: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        cleaned = compact_whitespace(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        tags.append(cleaned)
    return tags


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在：{path}")

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path.name} 第 {line_number} 行顶层必须是 object")
            records.append(payload)
    return records


def build_source_text(record: dict[str, Any], ocr_max_chars: int) -> tuple[str, dict[str, Any]]:
    caption = normalize_text(record.get("caption"))
    tags = normalize_tags(record.get("visual_tags"))
    packaging_type = normalize_text(record.get("packaging_type"))
    gift_style = normalize_text(record.get("gift_style"))
    scene_hint = normalize_text(record.get("scene_hint"))
    ocr_text = clean_ocr_text(record.get("ocr_text"), ocr_max_chars)

    parts: list[str] = []
    if caption:
        parts.append(f"图片描述：{caption}")
    if tags:
        parts.append(f"视觉标签：{'、'.join(tags)}")
    if packaging_type:
        parts.append(f"包装类型：{packaging_type}")
    if gift_style:
        parts.append(f"礼赠风格：{gift_style}")
    if scene_hint:
        parts.append(f"场景提示：{scene_hint}")
    if ocr_text:
        parts.append(f"图片文字：{ocr_text}")

    return "\n".join(parts).strip(), {
        "caption": caption,
        "ocr_text": ocr_text,
        "visual_tags": tags,
        "packaging_type": packaging_type,
        "gift_style": gift_style,
        "scene_hint": scene_hint,
    }


def build_doc(
    record: dict[str, Any],
    index: int,
    ocr_max_chars: int,
    image_url_mapping: dict[str, str],
) -> dict[str, Any] | None:
    image_id = safe_text(record.get("image_id")).strip()
    if not image_id:
        image_id = f"image_text_{index:06d}"

    source_text, normalized = build_source_text(record, ocr_max_chars)
    if not source_text:
        return None

    image_path = safe_text(record.get("image_path")).strip()
    image_url = safe_text(record.get("image_url")).strip() or resolve_mapped_url(image_url_mapping, image_path)
    source_sha1 = safe_text(record.get("source_sha1")).strip()
    source_doc_id = safe_text(record.get("source_doc_id")).strip() or build_image_source_doc_id(source_sha1, image_id)

    metadata = {
        "id": image_id,
        "image_id": image_id,
        "product_id": safe_text(record.get("product_id")).strip(),
        "image_path": image_path,
        "image_url": image_url,
        "source_file": safe_text(record.get("source_file")).strip(),
        "source_sheet": safe_text(record.get("source_sheet")).strip(),
        "source_row": safe_text(record.get("source_row")).strip(),
        "anchor_cell": safe_text(record.get("anchor_cell")).strip(),
        "source_kind": safe_text(record.get("source_kind")).strip() or "excel_image",
        "source_doc_id": source_doc_id,
        "source_sha1": source_sha1,
        "source_url": safe_text(record.get("source_url")).strip() or image_url,
        "match_status": safe_text(record.get("match_status")).strip(),
        "analysis_status": safe_text(record.get("analysis_status")).strip(),
        "has_image_embedding": bool(record.get("has_embedding")),
    }

    return {
        "id": image_id,
        "image_id": image_id,
        "product_id": metadata["product_id"],
        "image_path": metadata["image_path"],
        "image_url": metadata["image_url"],
        "source_file": metadata["source_file"],
        "source_sheet": metadata["source_sheet"],
        "source_row": metadata["source_row"],
        "source_kind": metadata["source_kind"],
        "source_doc_id": metadata["source_doc_id"],
        "source_sha1": metadata["source_sha1"],
        "source_url": metadata["source_url"],
        "caption": normalized["caption"],
        "ocr_text": normalized["ocr_text"],
        "visual_tags": normalized["visual_tags"],
        "packaging_type": normalized["packaging_type"],
        "gift_style": normalized["gift_style"],
        "scene_hint": normalized["scene_hint"],
        "source_text": source_text,
        "metadata": metadata,
    }


def preview_text(value: str, limit: int = 120) -> str:
    text = value.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_report(
    input_total: int,
    output_total: int,
    skipped_empty: int,
    status_counts: Counter[str],
    samples: list[dict[str, Any]],
    elapsed_seconds: float,
    output_path: Path,
) -> str:
    status_lines = ["| analysis_status | 数量 |", "| --- | ---: |"]
    if status_counts:
        for status, count in status_counts.most_common():
            status_lines.append(f"| `{status}` | {count} |")
    else:
        status_lines.append("| 无 | 0 |")

    sample_lines = [
        "| # | id | product_id | image_path | source_text 预览 |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for index, sample in enumerate(samples[:5], start=1):
        sample_lines.append(
            f"| {index} | `{sample.get('id') or ''}` | `{sample.get('product_id') or ''}` | "
            f"`{sample.get('image_path') or ''}` | {preview_text(sample.get('source_text') or '')} |"
        )
    if not samples:
        sample_lines.append("| 1 | - | - | - | 无样本 |")

    return f"""# 图片语义文本构建报告

## 总览

- 输入记录数：{input_total}
- 输出记录数：{output_total}
- 空语义跳过数：{skipped_empty}
- 输出文件：`{output_path.as_posix()}`
- 总耗时：{elapsed_seconds:.2f} 秒

## analysis_status 分布

{chr(10).join(status_lines)}

## 前 5 条样本预览

{chr(10).join(sample_lines)}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build image semantic text docs from image_analysis.jsonl.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input", default="output/image_analysis.jsonl", help="Input image analysis JSONL.")
    parser.add_argument("--output", default="output/image_text_docs.jsonl", help="Output JSONL path.")
    parser.add_argument("--report", default="output/image_text_docs_report.md", help="Report path.")
    parser.add_argument("--ocr-max-chars", type=int, default=DEFAULT_OCR_MAX_CHARS, help="Max OCR chars per doc.")
    parser.add_argument("--limit", type=int, default=None, help="Only build first N input records for testing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    input_path = (root / args.input).resolve()
    output_path = (root / args.output).resolve()
    report_path = (root / args.report).resolve()
    image_url_mapping = load_url_mapping((root / DEFAULT_IMAGE_URL_MAPPING).resolve())
    start_time = time.time()

    if args.ocr_max_chars <= 0:
        raise ValueError("--ocr-max-chars 必须大于 0")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit 不能小于 0")

    records = load_jsonl_records(input_path)
    if args.limit is not None:
        records = records[: args.limit]

    status_counts = Counter(safe_text(item.get("analysis_status")).strip() or "unknown" for item in records)
    docs: list[dict[str, Any]] = []
    skipped_empty = 0
    for index, record in enumerate(records, start=1):
        doc = build_doc(record, index, args.ocr_max_chars, image_url_mapping)
        if doc is None:
            skipped_empty += 1
            continue
        docs.append(doc)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file_obj:
        for doc in docs:
            file_obj.write(json.dumps(doc, ensure_ascii=False) + "\n")

    elapsed_seconds = time.time() - start_time
    report = build_report(
        input_total=len(records),
        output_total=len(docs),
        skipped_empty=skipped_empty,
        status_counts=status_counts,
        samples=docs,
        elapsed_seconds=elapsed_seconds,
        output_path=output_path,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    print(f"Input records: {len(records)}")
    print(f"Output docs: {len(docs)}")
    print(f"Skipped empty: {skipped_empty}")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
