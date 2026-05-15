#!/usr/bin/env python3
"""Shared helpers for stable source/resource identity in ingestion scripts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", safe_text(value)).strip()


def metadata_of(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = safe_text(value).strip()
        if text:
            return text
    return ""


def normalize_sha1(value: Any) -> str:
    text = safe_text(value).strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", text):
        return text
    return ""


def normalize_path(value: Any) -> str:
    text = safe_text(value).strip()
    if not text:
        return ""
    return Path(text).as_posix()


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


def sha1_text(text: str, length: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_pdf_source_doc_id(source_sha1: Any) -> str:
    normalized = normalize_sha1(source_sha1)
    return f"pdf__{normalized}" if normalized else ""


def build_image_source_doc_id(source_sha1: Any, image_id: Any = "") -> str:
    normalized = normalize_sha1(source_sha1)
    if normalized:
        return f"imgsrc__{normalized}"
    image_text = safe_text(image_id).strip()
    return f"imgsrc__{image_text}" if image_text else ""


def load_url_mapping(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    mapping: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            mapped_path = normalize_path(value.get("image_path") or value.get("source_file") or key)
            mapped_url = safe_text(value.get("image_url") or value.get("source_url")).strip()
        else:
            mapped_path = normalize_path(key)
            mapped_url = safe_text(value).strip()
        if mapped_path and mapped_url:
            mapping[mapped_path] = mapped_url
    return mapping


def resolve_mapped_url(mapping: dict[str, str], path_value: Any) -> str:
    normalized = normalize_path(path_value)
    if not normalized:
        return ""
    return safe_text(mapping.get(normalized)).strip()


def infer_product_source_doc_id(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    product_id = first_non_empty(raw.get("product_id"), metadata.get("product_id"))
    doc_type = first_non_empty(raw.get("doc_type"), metadata.get("doc_type"))
    field_name = first_non_empty(raw.get("field_name"), metadata.get("field_name"))
    if not product_id:
        return ""
    if doc_type == "product_full":
        return f"product__{product_id}"
    if doc_type == "product_field":
        suffix = field_name or "field"
        return f"product__{product_id}__{suffix}"
    return f"product__{product_id}"


def infer_text_source_doc_id(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    explicit = first_non_empty(raw.get("source_doc_id"), metadata.get("source_doc_id"))
    if explicit:
        return explicit

    source_sha1 = normalize_sha1(first_non_empty(raw.get("source_sha1"), metadata.get("source_sha1")))
    doc_id = first_non_empty(raw.get("doc_id"), metadata.get("doc_id"))
    doc_type = first_non_empty(raw.get("doc_type"), metadata.get("doc_type"))

    if source_sha1 and doc_type == "quality_report":
        return f"pdf__{source_sha1}"

    if doc_id.startswith("supp__") and "__quality_report__" in doc_id:
        match = re.search(r"__quality_report__([0-9a-f]{12,40})__c\d+$", doc_id)
        if match:
            return f"pdf__{match.group(1)}"

    return infer_product_source_doc_id(raw)


def infer_text_source_kind(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    explicit = first_non_empty(raw.get("source_kind"), metadata.get("source_kind"))
    if explicit:
        return explicit

    doc_type = first_non_empty(raw.get("doc_type"), metadata.get("doc_type"))
    if doc_type == "quality_report":
        return "pdf_quality_report"
    if doc_type in {"product_full", "product_field"}:
        return "product_master"
    return ""


def infer_text_source_file(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    return normalize_path(first_non_empty(raw.get("source_file"), metadata.get("source_file")))


def infer_text_source_sha1(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    return normalize_sha1(first_non_empty(raw.get("source_sha1"), metadata.get("source_sha1")))


def infer_text_source_url(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    return first_non_empty(raw.get("source_url"), metadata.get("source_url"))


def infer_text_chunk_index(raw: dict[str, Any]) -> int:
    metadata = metadata_of(raw)
    value = normalize_int(first_non_empty(raw.get("chunk_index"), metadata.get("chunk_index")))
    return value or 1


def infer_image_source_doc_id(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    explicit = first_non_empty(raw.get("source_doc_id"), metadata.get("source_doc_id"))
    if explicit:
        return explicit

    image_id = first_non_empty(raw.get("image_id"), metadata.get("image_id"), raw.get("id"), metadata.get("id"))
    source_sha1 = normalize_sha1(first_non_empty(raw.get("source_sha1"), metadata.get("source_sha1")))
    return build_image_source_doc_id(source_sha1, image_id)


def infer_image_source_kind(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    explicit = first_non_empty(raw.get("source_kind"), metadata.get("source_kind"))
    if explicit:
        return explicit
    source_type = first_non_empty(raw.get("source_type"), metadata.get("source_type"))
    if source_type:
        return source_type
    return "excel_image"


def infer_image_source_file(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    return normalize_path(first_non_empty(raw.get("source_file"), metadata.get("source_file")))


def infer_image_source_sha1(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    return normalize_sha1(first_non_empty(raw.get("source_sha1"), metadata.get("source_sha1")))


def infer_image_source_url(raw: dict[str, Any]) -> str:
    metadata = metadata_of(raw)
    return first_non_empty(
        raw.get("source_url"),
        metadata.get("source_url"),
        raw.get("image_url"),
        metadata.get("image_url"),
    )


def infer_image_chunk_index(raw: dict[str, Any]) -> int:
    metadata = metadata_of(raw)
    value = normalize_int(first_non_empty(raw.get("chunk_index"), metadata.get("chunk_index")))
    return value or 1


def make_catalog_fingerprint(record: dict[str, Any]) -> str:
    parts = [
        compact_text(record.get("product_name")),
        compact_text(record.get("brand")),
        compact_text(record.get("series")),
        compact_text(record.get("spec")),
        compact_text(record.get("manufacturer")),
    ]
    joined = "||".join(parts).lower()
    return sha1_text(joined or "unknown_product", length=20)


def make_source_locator(record: dict[str, Any]) -> str:
    source_file = normalize_path(record.get("source_file"))
    source_sheet = safe_text(record.get("source_sheet")).strip()
    source_row = normalize_int(record.get("source_row"))
    row_text = str(source_row) if source_row is not None else ""
    return "||".join([source_file, source_sheet, row_text])
