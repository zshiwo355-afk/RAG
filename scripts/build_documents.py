#!/usr/bin/env python3
"""
Build LangChain-ready text documents from enriched product records.

Current stage:
- rebuild text retrieval documents only
- no embeddings
- no Qdrant ingestion
- do not include image embedding arrays in page_content
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from source_identity import make_source_locator, sha1_file, sha1_text


ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

SPLITTER_BACKEND = "fallback"

try:
    from langchain_core.documents import Document  # type: ignore
    from langchain_text_splitters import RecursiveCharacterTextSplitter  # type: ignore

    SPLITTER_BACKEND = "langchain_text_splitters"
except Exception:
    @dataclass
    class Document:  # pragma: no cover - lightweight runtime fallback
        page_content: str
        metadata: dict[str, Any]

    class RecursiveCharacterTextSplitter:  # pragma: no cover - lightweight runtime fallback
        def __init__(self, chunk_size: int, chunk_overlap: int, separators: list[str]) -> None:
            self.chunk_size = chunk_size
            self.chunk_overlap = chunk_overlap
            self.separators = separators

        def split_text(self, text: str) -> list[str]:
            stripped = text.strip()
            if len(stripped) <= self.chunk_size:
                return [stripped] if stripped else []

            chunks: list[str] = []
            start = 0
            length = len(stripped)
            while start < length:
                target_end = min(start + self.chunk_size, length)
                end = target_end
                window = stripped[start:target_end]

                if target_end < length:
                    split_at = -1
                    for separator in self.separators:
                        if not separator:
                            continue
                        idx = window.rfind(separator)
                        if idx > 0:
                            split_at = start + idx + len(separator)
                            break
                    if split_at > start:
                        end = split_at

                chunk = stripped[start:end].strip()
                if chunk:
                    chunks.append(chunk)
                if end >= length:
                    break
                next_start = max(end - self.chunk_overlap, start + 1)
                if next_start <= start:
                    next_start = end
                start = next_start
            return chunks


PLACEHOLDERS = {"", "-", "--", "---", "—", "——", "暂无", "无", "未填写", "未标注", "nan", "None"}

BASE_FIELDS = [
    ("brand", "品牌"),
    ("series", "系列"),
    ("spec", "规格"),
    ("grade", "档次"),
    ("channel", "渠道"),
    ("price", "价格"),
    ("manufacturer", "生产厂家"),
    ("aroma_type", "香型"),
    ("positioning", "产品定位"),
]

FIELD_SPECS = [
    ("packaging_desc", "包装特点", "包装、工艺、设计理念、实物颜色等包装相关信息。"),
    ("quality_desc", "酒质介绍", "酒体、口感、酿造工艺、品质特点等信息。"),
    ("selling_points", "卖点与优势", "产品卖点、设计理念、价值塑造和推荐理由。"),
    ("differentiation", "差异化优势", "与其他产品对比时的差异化卖点。"),
    ("product_story", "产品故事", "产品名寓意、品牌故事、产品故事等内容。"),
    ("target_users", "目标人群", "产品面向的用户、人群、场景或消费特点。"),
    ("gift_attributes", "礼赠属性与适用场景", "礼赠、收藏、宴请、品鉴、节庆等场景属性。"),
    ("benchmark_product", "竞品对标", "对标竞品或竞品参考信息。"),
    ("other_notes", "其他高价值说明", "原表中未能归一但仍有业务价值的补充说明。"),
]

EXTRA_FIELD_MAPPING = {
    "产品与其他产品对比的差异化": "differentiation",
    "其它": "other_notes",
}

FUSION_ENABLED_FIELDS = {"product_full", "basic_info", "packaging_desc", "gift_attributes"}

NON_FUSED_REASONS = {
    "quality_desc": "以酒体与工艺描述为主，强行混入视觉信息会稀释核心风味语义。",
    "selling_points": "卖点字段本身已较长，额外插入图片 OCR 容易造成检索噪音。",
    "differentiation": "差异化通常来自业务对比，不宜用图片内容替代。",
    "product_story": "产品故事偏叙事，视觉 OCR 对该字段价值有限。",
    "target_users": "目标人群属于营销判断，不宜直接由图片语义决定。",
    "benchmark_product": "竞品对标应保持业务语义纯净，避免视觉信息干扰。",
    "other_notes": "补充说明来源不稳定，优先保持原始语义完整。",
}

OCR_PATTERNS = [
    ("酒精度", re.compile(r"酒精度[:：]?\s*([0-9]{1,2}(?:\.\d+)?\s*% ?vol)", re.IGNORECASE)),
    ("净含量", re.compile(r"净含量[:：]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:ml|mL|ML|L|l)(?:x[0-9]+)?)", re.IGNORECASE)),
    ("规格", re.compile(r"规格[:：]?\s*([^\n，,；;]{1,40})", re.IGNORECASE)),
    ("香型", re.compile(r"(?:^|[\n\s])香型[:：]?\s*([^\n]{1,24}?白酒)", re.IGNORECASE | re.MULTILINE)),
]


def is_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        text = normalize_text(value)
        return text not in PLACEHOLDERS
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_inline_text(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_joined_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(normalize_text(item) for item in value if is_nonempty(item))
    return normalize_text(value)


def clean_inline_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(normalize_inline_text(item) for item in value if is_nonempty(item))
    return normalize_inline_text(value)


def safe_id_part(value: Any, max_length: int = 48) -> str:
    text = normalize_text(value)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text).strip("_")
    return (text or "unknown")[:max_length]


def make_product_id(index: int, record: dict[str, Any]) -> str:
    existing = clean_inline_value(record.get("product_id"))
    if existing:
        return existing
    source = Path(record.get("source_file", "unknown")).stem
    row = record.get("source_row", "row")
    return f"prod_{index:04d}_{safe_id_part(source, 24)}_r{row}"


def make_product_excel_source_doc_id(record: dict[str, Any], fallback: str = "") -> str:
    source_sha1 = clean_inline_value(record.get("source_file_sha1")) or clean_inline_value(record.get("source_sha1"))
    if source_sha1:
        return f"product_excel__{source_sha1}"
    source_locator = make_source_locator(record)
    source_file = clean_inline_value(record.get("source_file"))
    return "product_excel__" + sha1_text(source_locator or source_file or fallback, length=40)


def make_product_source_doc_id(product_id: str, doc_type: str, field_name: str | None = None) -> str:
    if doc_type == "product_full":
        return f"product__{product_id}"
    return f"product__{product_id}__{field_name or 'field'}"


def format_lines(title: str, lines: list[tuple[str, Any]]) -> str:
    body = []
    for label, value in lines:
        if is_nonempty(value):
            body.append(f"{label}：{clean_inline_value(value)}")
    if not body:
        return ""
    return title + "\n" + "\n".join(body)


def unique_inline_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_inline_value(value)
        if not text or text in seen or text in PLACEHOLDERS:
            continue
        seen.add(text)
        result.append(text)
    return result


def shorten_text(value: Any, limit: int) -> str:
    text = normalize_inline_text(value)
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def get_extra_texts(record: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in (record.get("extra_fields") or {}).items():
        if not is_nonempty(raw_value):
            continue
        target_field = EXTRA_FIELD_MAPPING.get(raw_key)
        if not target_field:
            continue
        current = result.get(target_field)
        value = clean_joined_value(raw_value)
        if current:
            result[target_field] = current + "\n" + value
        else:
            result[target_field] = value
    return result


def clean_ocr_clue_value(value: str) -> str:
    text = normalize_inline_text(value)
    text = re.split(r"(?:净含量|规格|酒精度|执行标准|生产企业|配料表|食品名称|贮存方式|温馨提示)", text)[0]
    text = re.sub(r"[【\[\(（].*$", "", text)
    text = text.strip(" ：:;；，,】)]）")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_ocr_structured_clues(record: dict[str, Any]) -> list[str]:
    ocr_text = normalize_text(record.get("primary_image_ocr_text"))
    if not ocr_text:
        return []

    clues: list[str] = []
    seen_labels: set[str] = set()
    for label, pattern in OCR_PATTERNS:
        match = pattern.search(ocr_text)
        if not match:
            continue
        value = clean_ocr_clue_value(match.group(1))
        if not value:
            continue
        current_value = clean_inline_value(record.get({
            "酒精度": "extra_fields",
            "净含量": "spec",
            "规格": "spec",
            "香型": "aroma_type",
        }.get(label, "")))
        if current_value and value in current_value:
            continue
        if label in seen_labels:
            continue
        seen_labels.add(label)
        clues.append(f"{label}：{value}")

    product_name = clean_inline_value(record.get("product_name"))
    brand = clean_inline_value(record.get("brand"))
    for text, label in [(product_name, "产品名称"), (brand, "品牌")]:
        if text and normalize_inline_text(text) and normalize_inline_text(text) in normalize_inline_text(ocr_text):
            continue
    return clues[:4]


def build_primary_ocr_summary(record: dict[str, Any]) -> str:
    clues = extract_ocr_structured_clues(record)
    if clues:
        return "；".join(clues)

    ocr_text = normalize_text(record.get("primary_image_ocr_text"))
    if not ocr_text:
        return ""

    lines = [normalize_inline_text(line) for line in ocr_text.splitlines() if normalize_inline_text(line)]
    filtered: list[str] = []
    for line in lines:
        if len(line) < 2:
            continue
        filtered.append(line)
        if len(filtered) >= 3:
            break
    summary = "；".join(filtered)
    return shorten_text(summary, 160)


def build_image_semantic_section_for_full(record: dict[str, Any]) -> str:
    caption = shorten_text(record.get("primary_image_caption"), 220)
    tags = unique_inline_strings(record.get("primary_image_tags") or [])
    ocr_summary = build_primary_ocr_summary(record)

    lines = []
    if caption:
        lines.append(f"主图描述：{caption}")
    if tags:
        lines.append(f"主图标签：{'、'.join(tags[:8])}")
    if ocr_summary:
        lines.append(f"OCR摘要：{ocr_summary}")
    if not lines:
        return ""
    return "图片语义信息\n" + "\n".join(lines)


def build_basic_info_image_fusion(record: dict[str, Any]) -> str:
    clues = extract_ocr_structured_clues(record)
    if not clues:
        return ""
    return "图片识别补充\n" + "\n".join(clues)


def build_packaging_image_fusion(record: dict[str, Any]) -> str:
    caption = shorten_text(record.get("primary_image_caption"), 160)
    tags = unique_inline_strings(record.get("primary_image_tags") or [])
    ocr_summary = build_primary_ocr_summary(record)

    lines = []
    if caption:
        lines.append(f"主图描述：{caption}")
    packaging_tags = [tag for tag in tags if len(tag) <= 10][:6]
    if packaging_tags:
        lines.append(f"主图标签：{'、'.join(packaging_tags)}")
    if ocr_summary:
        lines.append(f"包装可见信息：{ocr_summary}")
    if not lines:
        return ""
    return "图片语义补充\n" + "\n".join(lines)


def build_gift_image_fusion(record: dict[str, Any]) -> str:
    caption = shorten_text(record.get("primary_image_caption"), 120)
    tags = unique_inline_strings(record.get("primary_image_tags") or [])
    gift_related = [
        tag
        for tag in tags
        if any(keyword in tag for keyword in ["礼", "商务", "节庆", "高端", "喜庆", "纪念", "收藏", "国风"])
    ]

    lines = []
    if caption:
        lines.append(f"主图呈现：{caption}")
    if gift_related:
        lines.append(f"礼赠相关标签：{'、'.join(gift_related[:6])}")
    if not lines:
        return ""
    return "图片语义补充\n" + "\n".join(lines)


def build_base_info_text(record: dict[str, Any], include_image_fusion: bool = False) -> tuple[str, bool]:
    lines = [("产品名称", record.get("product_name"))]
    lines.extend((label, record.get(field_name)) for field_name, label in BASE_FIELDS)
    sections = [format_lines("基础信息", lines)]

    did_fuse = False
    if include_image_fusion:
        image_fusion = build_basic_info_image_fusion(record)
        if image_fusion:
            sections.append(image_fusion)
            did_fuse = True

    return "\n\n".join(section for section in sections if section), did_fuse


def get_field_texts(record: dict[str, Any]) -> dict[str, str]:
    texts = {
        "packaging_desc": clean_joined_value(record.get("packaging_desc")),
        "quality_desc": clean_joined_value(record.get("quality_desc")),
        "selling_points": clean_joined_value(record.get("selling_points")),
        "product_story": clean_joined_value(record.get("product_story")),
        "target_users": clean_joined_value(record.get("target_users")),
        "gift_attributes": clean_joined_value(record.get("gift_attributes")),
        "benchmark_product": clean_joined_value(record.get("benchmark_product")),
    }
    texts.update(get_extra_texts(record))
    return {field: text for field, text in texts.items() if is_nonempty(text)}


def build_product_full_text(record: dict[str, Any]) -> tuple[str, bool]:
    sections = []
    base_info, _ = build_base_info_text(record, include_image_fusion=False)
    if base_info:
        sections.append(base_info)

    field_texts = get_field_texts(record)
    for field_name, field_label, _description in FIELD_SPECS:
        value = field_texts.get(field_name)
        if is_nonempty(value):
            sections.append(f"{field_label}\n{value}")

    image_section = build_image_semantic_section_for_full(record)
    did_fuse = bool(image_section)
    if image_section:
        sections.append(image_section)

    return "\n\n".join(section for section in sections if section), did_fuse


def make_metadata(
    record: dict[str, Any],
    product_id: str,
    doc_id: str,
    doc_type: str,
    field_name: str | None = None,
    field_label: str | None = None,
    chunk_index: int = 1,
    chunk_total: int = 1,
    split_by_langchain: bool = False,
    needs_review: bool = False,
    image_semantic_fused: bool = False,
) -> dict[str, Any]:
    source_locator = make_source_locator(record)
    record_source_sha1 = clean_inline_value(record.get("source_sha1"))
    source_file_sha1 = clean_inline_value(record.get("source_file_sha1"))
    source_sha1 = source_file_sha1 or record_source_sha1 or sha1_text(source_locator or product_id or doc_id, length=40)
    source_doc_id = make_product_excel_source_doc_id(record, fallback=product_id or doc_id)
    product_source_doc_id = make_product_source_doc_id(product_id, doc_type, field_name)
    metadata = {
        "doc_id": doc_id,
        "product_id": product_id,
        "doc_type": doc_type,
        "product_name": clean_inline_value(record.get("product_name")) or None,
        "brand": clean_inline_value(record.get("brand")) or None,
        "series": clean_inline_value(record.get("series")) or None,
        "spec": clean_inline_value(record.get("spec")) or None,
        "grade": clean_inline_value(record.get("grade")) or None,
        "channel": clean_inline_value(record.get("channel")) or None,
        "price": clean_inline_value(record.get("price")) or None,
        "source_kind": "product_excel",
        "source_type": "product_excel",
        "source_file": record.get("source_file"),
        "source_path": record.get("source_file"),
        "source_doc_id": source_doc_id,
        "source_sha1": source_sha1,
        "source_file_sha1": source_file_sha1 or source_sha1,
        "product_source_doc_id": product_source_doc_id,
        "product_doc_group_id": f"product__{product_id}",
        "source_url": "",
        "source_sheet": record.get("source_sheet"),
        "source_row": record.get("source_row"),
        "has_image": bool(record.get("has_image")),
        "image_count": int(record.get("image_count") or 0),
        "image_paths": record.get("image_paths") or [],
        "primary_image_path": record.get("primary_image_path"),
        "primary_image_caption": clean_inline_value(record.get("primary_image_caption")) or None,
        "primary_image_tags": record.get("primary_image_tags") or [],
        "image_analysis_status": record.get("image_analysis_status"),
        "chunk_index": chunk_index,
        "chunk_total": chunk_total,
        "split_by_langchain": split_by_langchain,
        "needs_review": needs_review,
        "image_semantic_fused": image_semantic_fused,
    }
    if field_name:
        metadata["field_name"] = field_name
        metadata["field_label"] = field_label or field_name
    if record_source_sha1 and record_source_sha1 != source_sha1:
        metadata["record_source_sha1"] = record_source_sha1
    return metadata


def build_field_document_text(record: dict[str, Any], field_name: str, field_label: str, field_value: str) -> tuple[str, bool]:
    context_lines = [
        ("产品名称", record.get("product_name")),
        ("品牌", record.get("brand")),
        ("系列", record.get("series")),
        ("规格", record.get("spec")),
    ]
    context = "\n".join(
        f"{label}：{clean_inline_value(value)}"
        for label, value in context_lines
        if is_nonempty(value)
    )

    sections = [context, f"{field_label}\n{field_value}".strip()]
    did_fuse = False

    if field_name == "packaging_desc":
        fusion = build_packaging_image_fusion(record)
        if fusion:
            sections.append(fusion)
            did_fuse = True
    elif field_name == "gift_attributes":
        fusion = build_gift_image_fusion(record)
        if fusion:
            sections.append(fusion)
            did_fuse = True

    return "\n\n".join(section for section in sections if section), did_fuse


def split_if_needed(text: str, splitter: RecursiveCharacterTextSplitter, threshold: int) -> tuple[list[str], bool]:
    if len(text) <= threshold:
        return [text], False
    chunks = [chunk.strip() for chunk in splitter.split_text(text) if chunk.strip()]
    return chunks or [text], len(chunks) > 1


def build_documents(records: list[dict[str, Any]], chunk_size: int, chunk_overlap: int, split_threshold: int) -> tuple[list[Document], dict[str, Any]]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "；", "。", "，", "、", " ", ""],
    )
    documents: list[Document] = []
    stats: dict[str, Any] = {
        "field_counts": Counter(),
        "split_field_counts": Counter(),
        "skipped_fields": Counter(),
        "records_needing_review": [],
        "fused_doc_counts": Counter(),
        "fused_field_counts": Counter(),
        "doc_type_counts": Counter(),
    }

    for index, record in enumerate(records, start=1):
        product_id = make_product_id(index, record)
        business_field_count = sum(
            1
            for field in [
                "product_name",
                "brand",
                "series",
                "spec",
                "grade",
                "channel",
                "packaging_desc",
                "quality_desc",
                "selling_points",
                "product_story",
                "target_users",
                "gift_attributes",
                "manufacturer",
                "positioning",
                "benchmark_product",
            ]
            if is_nonempty(record.get(field))
        )
        needs_review = business_field_count <= 5
        if needs_review:
            stats["records_needing_review"].append(
                {
                    "product_name": record.get("product_name"),
                    "source_file": record.get("source_file"),
                    "source_sheet": record.get("source_sheet"),
                    "source_row": record.get("source_row"),
                    "business_field_count": business_field_count,
                }
            )

        full_text, full_fused = build_product_full_text(record)
        full_doc_id = f"{product_id}__product_full"
        documents.append(
            Document(
                page_content=full_text,
                metadata=make_metadata(
                    record,
                    product_id,
                    full_doc_id,
                    "product_full",
                    chunk_index=1,
                    chunk_total=1,
                    needs_review=needs_review,
                    image_semantic_fused=full_fused,
                ),
            )
        )
        stats["doc_type_counts"]["product_full"] += 1
        if full_fused:
            stats["fused_doc_counts"]["product_full"] += 1

        base_info, base_info_fused = build_base_info_text(record, include_image_fusion=True)
        if base_info:
            doc_id = f"{product_id}__field__basic_info__001"
            documents.append(
                Document(
                    page_content=base_info,
                    metadata=make_metadata(
                        record,
                        product_id,
                        doc_id,
                        "product_field",
                        field_name="basic_info",
                        field_label="基础信息",
                        chunk_index=1,
                        chunk_total=1,
                        needs_review=needs_review,
                        image_semantic_fused=base_info_fused,
                    ),
                )
            )
            stats["field_counts"]["basic_info"] += 1
            stats["doc_type_counts"]["product_field"] += 1
            if base_info_fused:
                stats["fused_doc_counts"]["basic_info"] += 1
                stats["fused_field_counts"]["basic_info"] += 1

        field_texts = get_field_texts(record)
        for field_name, field_label, _description in FIELD_SPECS:
            field_value = field_texts.get(field_name)
            if not is_nonempty(field_value):
                stats["skipped_fields"][field_name] += 1
                continue

            field_doc_text, field_fused = build_field_document_text(record, field_name, field_label, field_value)
            chunks, did_split = split_if_needed(field_doc_text, splitter, split_threshold)
            chunk_total = len(chunks)
            if did_split:
                stats["split_field_counts"][field_name] += 1

            for chunk_index, chunk in enumerate(chunks, start=1):
                doc_id = f"{product_id}__field__{field_name}__{chunk_index:03d}"
                documents.append(
                    Document(
                        page_content=chunk,
                        metadata=make_metadata(
                            record,
                            product_id,
                            doc_id,
                            "product_field",
                            field_name=field_name,
                            field_label=field_label,
                            chunk_index=chunk_index,
                            chunk_total=chunk_total,
                            split_by_langchain=did_split,
                            needs_review=needs_review,
                            image_semantic_fused=field_fused,
                        ),
                    )
                )
                stats["field_counts"][field_name] += 1
                stats["doc_type_counts"]["product_field"] += 1
                if field_fused:
                    stats["fused_doc_counts"][field_name] += 1
                    stats["fused_field_counts"][field_name] += 1

    return documents, stats


def serialize_document(document: Document) -> dict[str, Any]:
    return {
        "page_content": document.page_content,
        "metadata": document.metadata,
    }


def attach_source_file_sha1(records: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    cache: dict[str, str] = {}
    enriched: list[dict[str, Any]] = []
    for record in records:
        source_file = clean_inline_value(record.get("source_file"))
        source_file_sha1 = ""
        if source_file:
            if source_file not in cache:
                path = (root / source_file).resolve()
                cache[source_file] = sha1_file(path) if path.is_file() else ""
            source_file_sha1 = cache[source_file]
        if source_file_sha1:
            updated = dict(record)
            updated["source_file_sha1"] = source_file_sha1
            enriched.append(updated)
        else:
            enriched.append(record)
    return enriched


def flatten_for_csv(document: Document) -> dict[str, Any]:
    metadata = document.metadata
    return {
        "doc_id": metadata.get("doc_id"),
        "doc_type": metadata.get("doc_type"),
        "field_name": metadata.get("field_name"),
        "product_id": metadata.get("product_id"),
        "product_name": metadata.get("product_name"),
        "brand": metadata.get("brand"),
        "series": metadata.get("series"),
        "spec": metadata.get("spec"),
        "grade": metadata.get("grade"),
        "channel": metadata.get("channel"),
        "price": metadata.get("price"),
        "source_file": metadata.get("source_file"),
        "source_sheet": metadata.get("source_sheet"),
        "source_row": metadata.get("source_row"),
        "has_image": metadata.get("has_image"),
        "image_count": metadata.get("image_count"),
        "image_paths": json.dumps(metadata.get("image_paths") or [], ensure_ascii=False),
        "primary_image_path": metadata.get("primary_image_path"),
        "primary_image_caption": metadata.get("primary_image_caption"),
        "primary_image_tags": json.dumps(metadata.get("primary_image_tags") or [], ensure_ascii=False),
        "image_analysis_status": metadata.get("image_analysis_status"),
        "chunk_index": metadata.get("chunk_index"),
        "chunk_total": metadata.get("chunk_total"),
        "split_by_langchain": metadata.get("split_by_langchain"),
        "image_semantic_fused": metadata.get("image_semantic_fused"),
        "needs_review": metadata.get("needs_review"),
        "content_length": len(document.page_content),
        "page_content": document.page_content,
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    }


def preview_text(text: str, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[:limit] + "..."


def build_report(
    records: list[dict[str, Any]],
    documents: list[Document],
    stats: dict[str, Any],
    output_dir: Path,
    json_name: str,
    csv_name: str,
    chunk_size: int,
    chunk_overlap: int,
    split_threshold: int,
) -> str:
    doc_type_counts = Counter(doc.metadata.get("doc_type") for doc in documents)
    field_counts = Counter(
        doc.metadata.get("field_name")
        for doc in documents
        if doc.metadata.get("doc_type") == "product_field"
    )
    fusion_doc_count = sum(1 for doc in documents if doc.metadata.get("image_semantic_fused"))
    docs_with_images = sum(1 for doc in documents if doc.metadata.get("image_count", 0) > 0)
    split_docs = [doc for doc in documents if doc.metadata.get("split_by_langchain")]
    split_field_counts = Counter(doc.metadata.get("field_name") for doc in split_docs)

    field_table = ["| field_name | 文档数 |", "| --- | ---: |"]
    for field_name, count in field_counts.most_common():
        field_table.append(f"| `{field_name}` | {count} |")

    split_table = ["| field_name | 发生二次切分的文档块数 |", "| --- | ---: |"]
    if split_field_counts:
        for field_name, count in split_field_counts.most_common():
            split_table.append(f"| `{field_name}` | {count} |")
    else:
        split_table.append("| 无 | 0 |")

    fused_fields_lines = []
    for field_name in ["product_full", "basic_info", "packaging_desc", "gift_attributes"]:
        fused_fields_lines.append(
            f"- `{field_name}`：{stats['fused_doc_counts'].get(field_name, 0)} 条文档融合了图片语义。"
        )

    non_fused_lines = [f"- `{field}`：{reason}" for field, reason in NON_FUSED_REASONS.items()]

    preview_lines = [
        "| # | doc_id | doc_type | field_name | product_name | length | fused | preview |",
        "| ---: | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for idx, doc in enumerate(documents[:10], start=1):
        metadata = doc.metadata
        preview_lines.append(
            "| {idx} | `{doc_id}` | {doc_type} | {field_name} | {product_name} | {length} | {fused} | {preview} |".format(
                idx=idx,
                doc_id=metadata.get("doc_id"),
                doc_type=metadata.get("doc_type"),
                field_name=metadata.get("field_name") or "",
                product_name=(metadata.get("product_name") or "").replace("|", "/"),
                length=len(doc.page_content),
                fused="yes" if metadata.get("image_semantic_fused") else "no",
                preview=preview_text(doc.page_content).replace("|", "/"),
            )
        )

    review_lines = []
    for item in stats["records_needing_review"][:20]:
        review_lines.append(
            f"- `{item['product_name']}`：{item['business_field_count']} 个业务字段，"
            f"{item['source_file']} / {item['source_sheet']} / row {item['source_row']}"
        )
    if not review_lines:
        review_lines.append("- 无。")

    return f"""# 文档切分构建报告 v2

## 总览

- 产品总数：{len(records)}
- 总文档数：{len(documents)}
- `product_full` 数量：{doc_type_counts.get('product_full', 0)}
- `product_field` 数量：{doc_type_counts.get('product_field', 0)}
- 各字段总文档数统计见下表
- 融合图片语义的文档数量：{fusion_doc_count}
- 有图文档数量：{docs_with_images}
- 输出 JSON：`{output_dir / json_name}`
- 输出 CSV：`{output_dir / csv_name}`
- 切分器：`RecursiveCharacterTextSplitter`
- 切分后端：`{SPLITTER_BACKEND}`

## 字段数量统计

{chr(10).join(field_table)}

## 图片语义融合字段

{chr(10).join(fused_fields_lines)}

说明：
- `product_full`：增加“图片语义信息”区块，融合主图 caption、tags 和 OCR 摘要。
- `basic_info`：仅融合 OCR 中稳定提取出的品牌/规格/酒精度/香型等结构化线索。
- `packaging_desc`：融合主图描述、标签和包装可见信息，增强包装检索能力。
- `gift_attributes`：仅融合与礼盒、商务、节庆、收藏相关的视觉语义。

## 未融合图片语义的字段及原因

{chr(10).join(non_fused_lines)}

## LangChain 递归切分参数

- `chunk_size`：{chunk_size}
- `chunk_overlap`：{chunk_overlap}
- 二次切分触发阈值：正文长度 > {split_threshold}
- 参数判断：v2 文档在原有产品文本上增加了适量图片语义，字段文档平均长度略有增长，因此将 `chunk_size` 设为 {chunk_size}，既能容纳图片语义补充，又能控制超长卖点/故事字段切分粒度。
- 二次切分字段统计：

{chr(10).join(split_table)}

## 需人工复核但不阻塞切分的记录

{chr(10).join(review_lines)}

## 前 10 条文档预览

{chr(10).join(preview_lines)}

## 是否适合进入主文本 embedding 阶段

适合进入主文本 embedding 阶段。v2 文档已经把图片 caption、标签和 OCR 摘要以受控方式融合进主文本语义，同时避免把图片路径、向量引用和 embedding 数组写进正文；这更适合后续主文本 RAG 的召回和图文关联。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build v2 text documents from enriched product data.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input", default="output/products_enriched.json", help="Input enriched product JSON.")
    parser.add_argument("--output", default="output", help="Output directory.")
    parser.add_argument("--json-name", default="documents_preview_v2.json", help="Output JSON filename.")
    parser.add_argument("--csv-name", default="documents_preview_v2.csv", help="Output CSV filename.")
    parser.add_argument("--report-name", default="document_build_report_v2.md", help="Output report filename.")
    parser.add_argument("--chunk-size", type=int, default=950, help="Chunk size for long field documents.")
    parser.add_argument("--chunk-overlap", type=int, default=120, help="Chunk overlap for long field documents.")
    parser.add_argument("--split-threshold", type=int, default=950, help="Only split field documents longer than this.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    input_path = (root / args.input).resolve()
    output_dir = (root / args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    records = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit(f"Input must be a list: {input_path}")

    records = attach_source_file_sha1(records, root)
    documents, stats = build_documents(records, args.chunk_size, args.chunk_overlap, args.split_threshold)

    json_path = output_dir / args.json_name
    csv_path = output_dir / args.csv_name
    report_path = output_dir / args.report_name

    json_path.write_text(
        json.dumps([serialize_document(doc) for doc in documents], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    csv_rows = [flatten_for_csv(doc) for doc in documents]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)

    report_path.write_text(
        build_report(
            records,
            documents,
            stats,
            output_dir,
            args.json_name,
            args.csv_name,
            args.chunk_size,
            args.chunk_overlap,
            args.split_threshold,
        ),
        encoding="utf-8",
    )

    doc_type_counts = Counter(doc.metadata.get("doc_type") for doc in documents)
    print(f"Products: {len(records)}")
    print(f"Documents: {len(documents)}")
    print(f"product_full: {doc_type_counts.get('product_full', 0)}")
    print(f"product_field: {doc_type_counts.get('product_field', 0)}")
    print(f"Fused image-semantic docs: {sum(1 for doc in documents if doc.metadata.get('image_semantic_fused'))}")
    print(f"Output JSON: {json_path}")
    print(f"Output CSV: {csv_path}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
