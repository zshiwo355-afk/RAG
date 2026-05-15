#!/usr/bin/env python3
"""
Local image path to public URL mapping and product-level image enrichment.

This module is read-only for source data. It loads:
- output/image_url_mapping.json
- output/products_enriched.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT as ROOT, safe_text


DEFAULT_MAPPING_PATH = "output/image_url_mapping.json"
DEFAULT_PRODUCTS_PATH = "output/products_enriched.json"


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def normalize_image_path(value: Any, root: Path | str = ROOT) -> str:
    text = safe_text(value).strip()
    if not text:
        return ""
    path = Path(text)
    root_path = Path(root).resolve()
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root_path).as_posix()
        except Exception:
            return path.as_posix()
    return path.as_posix()


def load_image_url_mapping(
    root: Path | str = ROOT,
    mapping_path: str = DEFAULT_MAPPING_PATH,
) -> dict[str, str]:
    root_path = Path(root).resolve()
    path = root_path / mapping_path
    if not path.exists():
        warn(f"图片 URL 映射文件不存在：{path}")
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warn(f"图片 URL 映射文件读取失败：{path}: {exc}")
        return {}

    mapping: dict[str, str] = {}
    if not isinstance(payload, dict):
        warn(f"图片 URL 映射文件格式不是 object：{path}")
        return mapping

    for key, value in payload.items():
        if isinstance(value, dict):
            image_path = normalize_image_path(value.get("image_path") or key, root_path)
            image_url = safe_text(value.get("image_url")).strip()
        else:
            image_path = normalize_image_path(key, root_path)
            image_url = safe_text(value).strip()
        if image_path and image_url:
            mapping[image_path] = image_url
    return mapping


def _list_texts(value: Any, root: Path | str = ROOT) -> list[str]:
    if not isinstance(value, list):
        return []
    results: list[str] = []
    for item in value:
        path = normalize_image_path(item, root)
        if path and path not in results:
            results.append(path)
    return results


def load_product_image_index(
    root: Path | str = ROOT,
    products_path: str = DEFAULT_PRODUCTS_PATH,
) -> dict[str, dict[str, Any]]:
    root_path = Path(root).resolve()
    path = root_path / products_path
    if not path.exists():
        warn(f"产品图片索引文件不存在：{path}")
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warn(f"产品图片索引文件读取失败：{path}: {exc}")
        return {}

    if not isinstance(payload, list):
        warn(f"产品图片索引文件格式不是 list：{path}")
        return {}

    index: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        product_id = safe_text(item.get("product_id")).strip()
        if not product_id:
            continue
        primary_image_path = normalize_image_path(item.get("primary_image_path"), root_path)
        image_paths = _list_texts(item.get("image_paths"), root_path)
        if primary_image_path and primary_image_path not in image_paths:
            image_paths.insert(0, primary_image_path)
        index[product_id] = {
            "primary_image_path": primary_image_path,
            "image_paths": image_paths,
        }
    return index


def resolve_image_url(
    image_path: str,
    mapping: dict[str, str] | None = None,
    root: Path | str = ROOT,
) -> str | None:
    normalized = normalize_image_path(image_path, root)
    if not normalized:
        return None
    url_mapping = mapping if mapping is not None else load_image_url_mapping(root)
    return url_mapping.get(normalized)


def _append_image(
    images: list[dict[str, str]],
    seen: set[str],
    image_path: str,
    source: str,
    mapping: dict[str, str],
    explicit_url: str = "",
    payload: dict[str, Any] | None = None,
    root: Path | str = ROOT,
) -> None:
    normalized = normalize_image_path(image_path, root)
    if not normalized or normalized in seen:
        return
    seen.add(normalized)
    raw = payload if isinstance(payload, dict) else {}
    images.append(
        {
            "image_id": safe_text(raw.get("image_id")).strip(),
            "image_path": normalized,
            "image_url": explicit_url or mapping.get(normalized, ""),
            "source": source,
            "source_kind": safe_text(raw.get("source_kind")).strip(),
            "source_file": safe_text(raw.get("source_file")).strip(),
            "source_doc_id": safe_text(raw.get("source_doc_id")).strip(),
            "source_sha1": safe_text(raw.get("source_sha1")).strip(),
            "source_url": safe_text(raw.get("source_url")).strip() or explicit_url or mapping.get(normalized, ""),
            "chunk_index": raw.get("chunk_index"),
        }
    )


def enrich_product_images(
    results: list[dict[str, Any]],
    image_url_mapping: dict[str, str] | None = None,
    product_image_index: dict[str, dict[str, Any]] | None = None,
    root: Path | str = ROOT,
) -> list[dict[str, Any]]:
    mapping = image_url_mapping if image_url_mapping is not None else load_image_url_mapping(root)
    product_index = product_image_index if product_image_index is not None else load_product_image_index(root)
    enriched: list[dict[str, Any]] = []

    for result in results:
        item = dict(result)
        product_id = safe_text(item.get("product_id")).strip()
        images: list[dict[str, str]] = []
        seen: set[str] = set()

        for existing in item.get("images") or []:
            if not isinstance(existing, dict):
                continue
            _append_image(
                images,
                seen,
                safe_text(existing.get("image_path")),
                safe_text(existing.get("source")).strip() or "retrieval",
                mapping,
                safe_text(existing.get("image_url")).strip(),
                existing,
                root,
            )

        for hit in item.get("image_hits") or []:
            _append_image(
                images,
                seen,
                safe_text(hit.get("image_path")),
                "image_hit",
                mapping,
                safe_text(hit.get("image_url")).strip(),
                hit.get("metadata") if isinstance(hit.get("metadata"), dict) else hit,
                root,
            )

        existing_path = normalize_image_path(item.get("best_image_path"), root)
        if existing_path:
            _append_image(
                images,
                seen,
                existing_path,
                "image_hit",
                mapping,
                safe_text(item.get("best_image_url")).strip(),
                {"image_path": existing_path, "image_url": item.get("best_image_url")},
                root,
            )

        product_images = product_index.get(product_id) or {}
        if not images:
            primary_path = normalize_image_path(product_images.get("primary_image_path"), root)
            if primary_path:
                _append_image(images, seen, primary_path, "products_enriched", mapping, "", {"image_path": primary_path}, root)
        if not images:
            for image_path in product_images.get("image_paths") or []:
                _append_image(images, seen, image_path, "products_enriched", mapping, "", {"image_path": image_path}, root)
                if images:
                    break

        if images:
            best = images[0]
            item["best_image_path"] = best.get("image_path", "")
            item["best_image_url"] = best.get("image_url", "")
            item["image_source"] = best.get("source") or "none"
        else:
            item["best_image_path"] = existing_path
            item["best_image_url"] = mapping.get(existing_path, "") if existing_path else ""
            item["image_source"] = "none"
            if existing_path:
                _append_image(
                    images,
                    seen,
                    existing_path,
                    "none",
                    mapping,
                    safe_text(item.get("best_image_url")).strip(),
                    {"image_path": existing_path, "image_url": item.get("best_image_url")},
                    root,
                )
        item["images"] = images
        enriched.append(item)

    return enriched
