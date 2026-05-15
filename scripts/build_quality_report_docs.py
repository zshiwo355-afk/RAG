#!/usr/bin/env python3
"""
Build quality report supplement documents from PDF files.

This script is intentionally isolated from the existing full rebuild pipeline:
- scan only 质检报告/**/*.pdf
- link each PDF to an existing product from output/products_enriched.json
- skip already processed unchanged PDFs via manifest
- mark changed PDFs without overwriting old supplement docs
- output only newly prepared docs compatible with embed_documents.py
"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import requests

from source_identity import build_pdf_source_doc_id


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = "质检报告"
DEFAULT_PRODUCTS = "output/products_enriched.json"
DEFAULT_MAP_CSV = "config/quality_report_product_map.csv"
DEFAULT_OUTPUT_JSON = "output/quality_report_documents.json"
DEFAULT_OUTPUT_MANIFEST = "output/quality_report_manifest.json"
DEFAULT_OUTPUT_UNMATCHED = "output/quality_report_unmatched.csv"

CHUNK_SIZE = 950
CHUNK_OVERLAP = 120
OCR_MODEL = "qwen-vl-ocr-latest"
OCR_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
OCR_TIMEOUT = 120
MIN_TEXT_CHARS = 200
MIN_CHINESE_CHARS = 40

PLACEHOLDERS = {"", "-", "--", "---", "—", "——", "暂无", "无", "未填写", "未标注", "nan", "None"}


@dataclass
class PageSegment:
    text: str
    page_start: int
    page_end: int


@dataclass
class ChunkRecord:
    text: str
    page_start: int
    page_end: int


class RecursiveCharacterTextSplitter:
    """Lightweight fallback splitter aligned with build_documents.py defaults."""

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


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def load_env(root: Path) -> None:
    env_path = root / ".env"
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path)
        return
    except Exception:
        pass

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ensure_api_key() -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未检测到 DASHSCOPE_API_KEY，OCR fallback 无法执行。")
    return api_key


def normalize_text(value: Any) -> str:
    text = safe_text(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_inline_text(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize_text(value)).strip()


def normalize_match_key(value: Any) -> str:
    text = normalize_inline_text(value).lower()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def slug_title(path: Path) -> str:
    return normalize_inline_text(path.stem)


def load_json_list(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"{label} 不存在：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{label} 顶层必须是 list：{path}")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{label} 第 {index} 项必须是 object：{path}")
        records.append(item)
    return records


def load_manual_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        mapping: dict[str, str] = {}
        for row in reader:
            relative_path = Path(safe_text(row.get("relative_path")).strip()).as_posix()
            product_id = safe_text(row.get("product_id")).strip()
            if relative_path and product_id:
                mapping[relative_path] = product_id
    return mapping


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"manifest 解析失败：{path}：{exc}") from exc

    records: list[dict[str, Any]]
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        records = []
        for rel_path, item in payload.items():
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied.setdefault("relative_path", rel_path)
            records.append(copied)
    else:
        raise ValueError(f"manifest 顶层必须是 list 或 object：{path}")

    indexed: dict[str, dict[str, Any]] = {}
    for item in records:
        relative_path = Path(safe_text(item.get("relative_path")).strip()).as_posix()
        if relative_path:
            indexed[relative_path] = item
    return indexed


def save_manifest(path: Path, records: dict[str, dict[str, Any]]) -> None:
    ordered = [records[key] for key in sorted(records)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_pdf_files(root: Path, input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    return sorted(path for path in input_dir.rglob("*.pdf") if path.is_file())


def count_chinese_chars(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def evaluate_text_quality(page_texts: list[str]) -> tuple[bool, str]:
    non_empty_pages = [text for text in page_texts if text]
    total_text = "\n".join(non_empty_pages)
    total_chars = len(re.sub(r"\s+", "", total_text))
    chinese_chars = count_chinese_chars(total_text)
    if not non_empty_pages:
        return False, "no_text"
    if total_chars < MIN_TEXT_CHARS:
        return False, f"text_too_short:{total_chars}"
    if chinese_chars < MIN_CHINESE_CHARS:
        return False, f"too_few_chinese:{chinese_chars}"
    return True, "ok"


def extract_pdf_text_pages(pdf_path: Path) -> list[str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        raise RuntimeError("未安装 pypdf，无法直接抽取 PDF 文本。") from exc

    reader = PdfReader(str(pdf_path))
    page_texts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        page_texts.append(normalize_text(text))
    return page_texts


def pixmap_to_data_url(pixmap: Any) -> str:
    png_bytes = pixmap.tobytes("png")
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def extract_message_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"OCR 响应缺少 choices：{json.dumps(response_payload, ensure_ascii=False)[:1200]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(safe_text(text).strip())
        return "\n".join(part for part in parts if part).strip()
    return safe_text(content).strip()


def ocr_page_texts(pdf_path: Path, timeout: int) -> list[str]:
    api_key = ensure_api_key()
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("未安装 PyMuPDF，无法执行 PDF OCR fallback。") from exc

    doc = fitz.open(pdf_path)
    page_texts: list[str] = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        data_url = pixmap_to_data_url(pix)
        payload = {
            "model": OCR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                            "min_pixels": 256 * 256,
                            "max_pixels": 32 * 32 * 8192,
                        },
                        {
                            "type": "text",
                            "text": (
                                "请执行 OCR。尽量提取这页 PDF 中全部可辨认文字，"
                                "只返回纯文本，不要 JSON，不要 Markdown，不要解释。"
                                "如果没有可辨认文字，返回空字符串。"
                            ),
                        },
                    ],
                }
            ],
            "max_tokens": 1200,
            "temperature": 0.0,
        }
        response = requests.post(
            OCR_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OCR HTTP {response.status_code}: {response.text[:800]}")
        page_texts.append(normalize_text(extract_message_text(response.json())))
    doc.close()
    return page_texts


def build_page_segments(page_texts: list[str], splitter: RecursiveCharacterTextSplitter) -> list[PageSegment]:
    segments: list[PageSegment] = []
    for page_number, page_text in enumerate(page_texts, start=1):
        cleaned = normalize_text(page_text)
        if not cleaned:
            continue
        if len(cleaned) <= CHUNK_SIZE:
            segments.append(PageSegment(text=cleaned, page_start=page_number, page_end=page_number))
            continue
        for chunk in splitter.split_text(cleaned):
            chunk = normalize_text(chunk)
            if chunk:
                segments.append(PageSegment(text=chunk, page_start=page_number, page_end=page_number))
    return segments


def combine_segments(
    segments: list[PageSegment],
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkRecord]:
    if not segments:
        return []

    chunks: list[ChunkRecord] = []
    current_text = ""
    current_start = segments[0].page_start
    current_end = segments[0].page_end

    for segment in segments:
        if not current_text:
            current_text = segment.text
            current_start = segment.page_start
            current_end = segment.page_end
            continue

        candidate = current_text + "\n\n" + segment.text
        if len(candidate) <= chunk_size:
            current_text = candidate
            current_end = segment.page_end
            continue

        chunks.append(ChunkRecord(text=current_text, page_start=current_start, page_end=current_end))
        overlap_text = normalize_text(current_text[-chunk_overlap:]) if chunk_overlap > 0 else ""
        if overlap_text:
            seeded = normalize_text(overlap_text + "\n\n" + segment.text)
            if len(seeded) <= chunk_size:
                current_text = seeded
                current_start = current_end
                current_end = segment.page_end
                continue
        current_text = segment.text
        current_start = segment.page_start
        current_end = segment.page_end

    if current_text:
        chunks.append(ChunkRecord(text=current_text, page_start=current_start, page_end=current_end))
    return chunks


def parse_docs_from_pages(page_texts: list[str]) -> list[ChunkRecord]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "；", "。", "，", "、", " ", ""],
    )
    segments = build_page_segments(page_texts, splitter)
    return combine_segments(segments, CHUNK_SIZE, CHUNK_OVERLAP)


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = normalize_inline_text(value)
        if text and text not in PLACEHOLDERS:
            return text
    return ""


def build_product_index(products: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    indexed: list[dict[str, Any]] = []
    for product in products:
        product_id = safe_text(product.get("product_id")).strip()
        product_name = first_nonempty(product.get("product_name"))
        if not product_id or not product_name:
            continue
        brand = first_nonempty(product.get("brand"))
        series = first_nonempty(product.get("series"))
        aliases = {
            normalize_match_key(product_name),
            normalize_match_key(first_nonempty(brand, "") + product_name),
            normalize_match_key(product_name + first_nonempty(brand, "")),
            normalize_match_key(first_nonempty(brand, "", product_name) + first_nonempty(series)),
        }
        aliases.discard("")
        item = {
            "product_id": product_id,
            "product_name": product_name,
            "brand": brand,
            "series": series,
            "aliases": sorted(aliases),
            "product_name_key": normalize_match_key(product_name),
            "brand_key": normalize_match_key(brand),
        }
        by_id[product_id] = item
        indexed.append(item)
    return by_id, indexed


def match_product(
    relative_path: str,
    manual_map: dict[str, str],
    products_by_id: dict[str, dict[str, Any]],
    indexed_products: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    mapped_product_id = manual_map.get(relative_path)
    if mapped_product_id:
        product = products_by_id.get(mapped_product_id)
        if product is not None:
            return product, "manual_map", [product]
        return None, "manual_map_missing_product", []

    rel_path = Path(relative_path)
    dir_key = normalize_match_key(rel_path.parent.name)
    file_key = normalize_match_key(rel_path.stem)
    combined_key = normalize_match_key(rel_path.parent.name + rel_path.stem)

    exact_matches = [
        item
        for item in indexed_products
        if item["product_name_key"] in {file_key, combined_key}
        or file_key in item["aliases"]
        or combined_key in item["aliases"]
    ]
    if len(exact_matches) == 1:
        return exact_matches[0], "exact_name", exact_matches
    if len(exact_matches) > 1:
        exact_brand_filtered = [
            item
            for item in exact_matches
            if not dir_key
            or not item["brand_key"]
            or dir_key in item["brand_key"]
            or item["brand_key"] in dir_key
            or item["brand_key"] in combined_key
        ]
        if len(exact_brand_filtered) == 1:
            return exact_brand_filtered[0], "exact_name_with_brand", exact_brand_filtered
        return None, "multiple_exact_matches", exact_matches

    fuzzy_matches = []
    for item in indexed_products:
        product_name_key = item["product_name_key"]
        brand_key = item["brand_key"]
        if not product_name_key:
            continue
        name_hit = product_name_key in file_key or product_name_key in combined_key
        if not name_hit:
            continue
        if dir_key:
            if not brand_key or dir_key in brand_key or brand_key in dir_key or brand_key in combined_key:
                fuzzy_matches.append(item)
                continue
        else:
            fuzzy_matches.append(item)

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], "dir_plus_filename", fuzzy_matches
    if len(fuzzy_matches) > 1:
        return None, "multiple_fuzzy_matches", fuzzy_matches
    return None, "no_match", []


def build_document_rows(
    product: dict[str, Any],
    source_file: str,
    source_sha1: str,
    source_kind: str,
    supplement_title: str,
    chunk_records: list[ChunkRecord],
) -> list[dict[str, Any]]:
    doc_rows: list[dict[str, Any]] = []
    chunk_total = len(chunk_records)
    sha12 = source_sha1[:12]
    for chunk_index, chunk in enumerate(chunk_records, start=1):
        doc_id = f"supp__{product['product_id']}__quality_report__{sha12}__c{chunk_index:03d}"
        source_doc_id = build_pdf_source_doc_id(source_sha1)
        doc_rows.append(
            {
                "page_content": chunk.text,
                "metadata": {
                    "doc_id": doc_id,
                    "product_id": product["product_id"],
                    "product_name": product["product_name"],
                    "doc_type": "quality_report",
                    "field_name": "quality_report",
                    "source_file": source_file,
                    "source_kind": source_kind,
                    "source_doc_id": source_doc_id,
                    "source_sha1": source_sha1,
                    "source_url": "",
                    "chunk_index": chunk_index,
                    "chunk_total": chunk_total,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "supplement_type": "quality_report",
                    "supplement_title": supplement_title,
                },
            }
        )
    return doc_rows


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_manifest_record(
    relative_path: str,
    file_sha1: str,
    status: str,
    message: str,
    product: dict[str, Any] | None = None,
    source_kind: str = "",
    doc_count: int = 0,
    processed_sha1: str | None = None,
    candidate_products: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "relative_path": relative_path,
        "status": status,
        "message": message,
        "current_sha1": file_sha1,
        "processed_sha1": processed_sha1 or (file_sha1 if status in {"success", "skipped_unchanged"} else ""),
        "product_id": product.get("product_id") if product else "",
        "product_name": product.get("product_name") if product else "",
        "source_kind": source_kind,
        "doc_count": doc_count,
        "candidate_product_ids": [item.get("product_id") for item in (candidate_products or [])],
        "candidate_product_names": [item.get("product_name") for item in (candidate_products or [])],
        "updated_at": now_iso(),
    }


def write_unmatched_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "relative_path",
        "supplement_title",
        "match_mode",
        "reason",
        "candidate_count",
        "candidate_product_ids",
        "candidate_product_names",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build quality report docs from PDF supplements.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, help="Relative quality report directory.")
    parser.add_argument("--products", default=DEFAULT_PRODUCTS, help="Existing products JSON path.")
    parser.add_argument("--map-csv", default=DEFAULT_MAP_CSV, help="Optional manual path->product_id CSV.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_JSON, help="Output JSON path.")
    parser.add_argument("--manifest", default=DEFAULT_OUTPUT_MANIFEST, help="Manifest JSON path.")
    parser.add_argument("--unmatched", default=DEFAULT_OUTPUT_UNMATCHED, help="Unmatched CSV path.")
    parser.add_argument("--ocr-timeout", type=int, default=OCR_TIMEOUT, help="OCR request timeout seconds.")
    args = parser.parse_args()
    if args.ocr_timeout <= 0:
        raise SystemExit("--ocr-timeout must be positive")
    return args


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    load_env(root)

    input_dir = (root / args.input_dir).resolve()
    products_path = (root / args.products).resolve()
    map_csv_path = (root / args.map_csv).resolve()
    output_path = (root / args.output).resolve()
    manifest_path = (root / args.manifest).resolve()
    unmatched_path = (root / args.unmatched).resolve()

    products = load_json_list(products_path, "products_enriched.json")
    products_by_id, indexed_products = build_product_index(products)
    manual_map = load_manual_map(map_csv_path)
    manifest = load_manifest(manifest_path)
    pdf_files = discover_pdf_files(root, input_dir)

    prepared_docs: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []

    print(f"Loaded products: {len(indexed_products)}")
    print(f"Loaded manual map rows: {len(manual_map)}")
    print(f"Found quality report PDFs: {len(pdf_files)}")

    for pdf_path in pdf_files:
        relative_path = pdf_path.relative_to(root).as_posix()
        supplement_title = slug_title(pdf_path)
        file_sha1 = sha1_file(pdf_path)
        previous = manifest.get(relative_path) or {}
        previous_processed_sha1 = safe_text(previous.get("processed_sha1")).strip()

        if previous_processed_sha1 and file_sha1 == previous_processed_sha1:
            manifest[relative_path] = build_manifest_record(
                relative_path=relative_path,
                file_sha1=file_sha1,
                status="skipped_unchanged",
                message="already processed with same sha1",
                product=products_by_id.get(safe_text(previous.get("product_id")).strip()) if previous.get("product_id") else None,
                source_kind=safe_text(previous.get("source_kind")).strip(),
                doc_count=int(previous.get("doc_count") or 0),
                processed_sha1=previous_processed_sha1,
            )
            continue

        if previous_processed_sha1 and file_sha1 != previous_processed_sha1:
            manifest[relative_path] = build_manifest_record(
                relative_path=relative_path,
                file_sha1=file_sha1,
                status="changed",
                message="sha1 changed after previous successful processing; not auto-overwriting",
                product=products_by_id.get(safe_text(previous.get("product_id")).strip()) if previous.get("product_id") else None,
                source_kind=safe_text(previous.get("source_kind")).strip(),
                doc_count=int(previous.get("doc_count") or 0),
                processed_sha1=previous_processed_sha1,
            )
            continue

        product, match_mode, candidates = match_product(
            relative_path=relative_path,
            manual_map=manual_map,
            products_by_id=products_by_id,
            indexed_products=indexed_products,
        )
        if product is None:
            reason = match_mode
            manifest[relative_path] = build_manifest_record(
                relative_path=relative_path,
                file_sha1=file_sha1,
                status="unmatched",
                message=reason,
                candidate_products=candidates,
            )
            unmatched_rows.append(
                {
                    "relative_path": relative_path,
                    "supplement_title": supplement_title,
                    "match_mode": match_mode,
                    "reason": reason,
                    "candidate_count": len(candidates),
                    "candidate_product_ids": "、".join(safe_text(item.get("product_id")) for item in candidates),
                    "candidate_product_names": "、".join(safe_text(item.get("product_name")) for item in candidates),
                }
            )
            continue

        print(f"Processing PDF: {relative_path} -> {product['product_id']}")
        try:
            direct_page_texts = extract_pdf_text_pages(pdf_path)
            text_ok, text_reason = evaluate_text_quality(direct_page_texts)
            if text_ok:
                page_texts = direct_page_texts
                source_kind = "pdf_text"
            else:
                print(f"  direct text quality low ({text_reason}), using OCR fallback")
                page_texts = ocr_page_texts(pdf_path, timeout=args.ocr_timeout)
                source_kind = "pdf_ocr"

            chunk_records = parse_docs_from_pages(page_texts)
            if not chunk_records:
                raise RuntimeError("解析后未得到可入库文本块")

            doc_rows = build_document_rows(
                product=product,
                source_file=relative_path,
                source_sha1=file_sha1,
                source_kind=source_kind,
                supplement_title=supplement_title,
                chunk_records=chunk_records,
            )
            prepared_docs.extend(doc_rows)
            manifest[relative_path] = build_manifest_record(
                relative_path=relative_path,
                file_sha1=file_sha1,
                status="success",
                message=f"prepared {len(doc_rows)} docs via {source_kind}",
                product=product,
                source_kind=source_kind,
                doc_count=len(doc_rows),
            )
        except Exception as exc:
            manifest[relative_path] = build_manifest_record(
                relative_path=relative_path,
                file_sha1=file_sha1,
                status="extract_failed",
                message=safe_text(exc).strip() or exc.__class__.__name__,
                product=product,
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(prepared_docs, ensure_ascii=False, indent=2), encoding="utf-8")
    write_unmatched_csv(unmatched_path, unmatched_rows)
    save_manifest(manifest_path, manifest)

    status_counter: dict[str, int] = {}
    for item in manifest.values():
        status = safe_text(item.get("status")).strip() or "unknown"
        status_counter[status] = status_counter.get(status, 0) + 1

    print(f"Prepared docs: {len(prepared_docs)}")
    print(f"Output JSON: {output_path}")
    print(f"Manifest JSON: {manifest_path}")
    print(f"Unmatched CSV: {unmatched_path}")
    print("Manifest status counts:")
    for status in sorted(status_counter):
        print(f"  - {status}: {status_counter[status]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
