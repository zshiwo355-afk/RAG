#!/usr/bin/env python3
"""
Read-only multi-route retrieval service for product-level recall.

This module does not call an LLM, does not push data, and does not modify
OpenSearch. It only embeds the incoming query, searches existing OpenSearch
tables, and fuses route results at product_id level.
"""

from __future__ import annotations

from collections import defaultdict
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import PROJECT_ROOT as ROOT, VECTOR_DIMENSIONS, load_env, safe_text, truncate
from .opensearch_client import create_client, ensure_runtime_config

TEXT_EMBEDDING_MODEL = "text-embedding-v4"
QWEN_VL_EMBEDDING_MODEL = "qwen3-vl-embedding"
TEXT_EMBEDDING_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
MULTIMODAL_EMBEDDING_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)

ROUTE_TEXT_DENSE = "text_dense"
ROUTE_TEXT_KEYWORD = "text_keyword"
ROUTE_IMAGE_TEXT = "image_text"
ROUTE_IMAGE_VECTOR = "image_vector"


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def compact_json(payload: Any, limit: int = 1200) -> str:
    return json.dumps(payload, ensure_ascii=False)[:limit]


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_obj = urllib_request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(request_obj, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(raw_body) if raw_body.strip() else {}
        except Exception:
            error_payload = {"message": raw_body[:2000]}
        raise RuntimeError(f"HTTP {exc.code}: {compact_json(error_payload)}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc.reason}") from exc

    try:
        return json.loads(raw_body) if raw_body.strip() else {}
    except Exception as exc:
        raise RuntimeError(f"响应不是合法 JSON：{raw_body[:500]}") from exc


def ensure_dashscope_api_key() -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY")
    return api_key


def normalize_embedding(values: Any, label: str) -> list[float]:
    if not isinstance(values, list):
        raise RuntimeError(f"{label} embedding 不是数组")
    if len(values) != VECTOR_DIMENSIONS:
        raise RuntimeError(f"{label} embedding 维度不正确：期望 {VECTOR_DIMENSIONS}，实际 {len(values)}")
    return [float(value) for value in values]


def embed_text_query(query: str) -> list[float]:
    """Embed a user query with text-embedding-v4 for text_docs dense search."""
    payload = {
        "model": TEXT_EMBEDDING_MODEL,
        "input": [query],
        "dimensions": VECTOR_DIMENSIONS,
        "encoding_format": "float",
    }
    headers = {
        "Authorization": f"Bearer {ensure_dashscope_api_key()}",
        "Content-Type": "application/json",
    }
    response_payload = post_json(TEXT_EMBEDDING_URL, headers, payload)
    data = response_payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"text-embedding-v4 响应缺少 data：{compact_json(response_payload)}")
    return normalize_embedding(data[0].get("embedding"), TEXT_EMBEDDING_MODEL)


def embed_multimodal_text_query(query: str) -> list[float]:
    """Embed a user query with qwen3-vl-embedding text input for image_text_docs."""
    payload = {
        "model": QWEN_VL_EMBEDDING_MODEL,
        "input": {
            "contents": [
                {
                    "text": query,
                }
            ]
        },
        "parameters": {
            "dimension": VECTOR_DIMENSIONS,
            "enable_fusion": False,
        },
    }
    headers = {
        "Authorization": f"Bearer {ensure_dashscope_api_key()}",
        "Content-Type": "application/json",
    }
    response_payload = post_json(MULTIMODAL_EMBEDDING_URL, headers, payload)
    output = response_payload.get("output") or {}
    embeddings = output.get("embeddings") if isinstance(output, dict) else None
    if not isinstance(embeddings, list) or not embeddings:
        raise RuntimeError(f"qwen3-vl-embedding 响应缺少 embeddings：{compact_json(response_payload)}")
    first = sorted((item for item in embeddings if isinstance(item, dict)), key=lambda item: item.get("index", 0))[0]
    return normalize_embedding(first.get("embedding"), QWEN_VL_EMBEDDING_MODEL)


def body_to_plain(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return body
    for method_name in ("to_map", "to_dict"):
        method = getattr(body, method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return body


def compact_resource_text(value: Any) -> str:
    return safe_text(value).strip()


def normalize_metadata_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            payload = json.loads(value)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def normalize_space(text: Any) -> str:
    return re.sub(r"\s+", " ", safe_text(text)).strip()


def text_preview(text: Any, limit: int = 420) -> str:
    return truncate(normalize_space(text), limit)


def bundle_row(row: dict[str, Any]) -> dict[str, Any]:
    metadata = normalize_metadata_value(row.get("metadata"))
    merged = dict(metadata)
    merged.update({key: value for key, value in row.items() if value not in (None, "")})
    text = safe_text(merged.get("source_text") or merged.get("page_content") or merged.get("text")).strip()
    return {
        "doc_id": compact_resource_text(merged.get("doc_id") or merged.get("id")),
        "product_id": compact_resource_text(merged.get("product_id")),
        "doc_type": compact_resource_text(merged.get("doc_type")),
        "field_name": compact_resource_text(merged.get("field_name")),
        "source_kind": compact_resource_text(merged.get("source_kind")),
        "source_file": compact_resource_text(merged.get("source_file")),
        "source_doc_id": compact_resource_text(merged.get("source_doc_id")),
        "source_sha1": compact_resource_text(merged.get("source_sha1")),
        "source_url": compact_resource_text(merged.get("source_url")),
        "chunk_index": merged.get("chunk_index"),
        "page_start": merged.get("page_start"),
        "page_end": merged.get("page_end"),
        "supplement_title": compact_resource_text(merged.get("supplement_title")),
        "status": compact_resource_text(merged.get("status")),
        "mapping_status": compact_resource_text(merged.get("mapping_status")),
        "source_text": text,
        "text_preview": text_preview(text),
        "image_id": compact_resource_text(merged.get("image_id")),
        "image_path": compact_resource_text(merged.get("image_path")),
        "image_url": compact_resource_text(merged.get("image_url")),
        "metadata": metadata,
    }


def is_needs_review_row(row: dict[str, Any]) -> bool:
    metadata = normalize_metadata_value(row.get("metadata"))
    values = [
        row.get("doc_type"),
        row.get("field_name"),
        row.get("status"),
        row.get("mapping_status"),
        metadata.get("status"),
        metadata.get("mapping_status"),
    ]
    return any(compact_resource_text(value).lower() == "needs_review" for value in values)


def field_target(row: dict[str, Any]) -> str:
    doc_type = compact_resource_text(row.get("doc_type"))
    field_name = compact_resource_text(row.get("field_name"))
    if doc_type == "quality_report":
        return "quality_reports"
    if doc_type == "supplement":
        return "supplements"
    if doc_type == "product_full":
        return "basic_info"
    if field_name in {"basic_info", "product_info", "summary"}:
        return "basic_info"
    if field_name in {"packaging", "packaging_desc", "package", "package_info", "包装"}:
        return "packaging"
    if field_name in {"selling_points", "sell_point", "卖点", "介绍", "description"}:
        return "selling_points"
    if field_name in {"gift_attributes", "gift", "gift_scene", "礼品属性"}:
        return "gift_attributes"
    return "supplements" if doc_type == "supplement" else "basic_info"


def extract_bundle_field(row: dict[str, Any], field: str) -> str:
    metadata = normalize_metadata_value(row.get("metadata"))
    direct_keys = {
        "product_id": ("product_id", "canonical_product_id"),
        "product_name": ("product_name", "name", "产品名称", "商品名称", "品名"),
        "brand": ("brand", "品牌"),
        "spec": ("spec", "specification", "规格", "规格型号"),
    }
    for key in direct_keys[field]:
        value = row.get(key) or metadata.get(key)
        if compact_resource_text(value):
            return normalize_space(value)
    patterns = {
        "product_name": [r"(?:产品名称|商品名称|酒名|名称|品名)[:：]\s*([^。；;\n，,]{2,80})"],
        "brand": [r"(?:品牌)[:：]\s*([^。；;\n，,]{2,60})"],
        "spec": [r"(?:规格型号|规格|净含量)[:：]\s*([^。；;\n，,]{2,80})"],
    }
    text = safe_text(row.get("source_text"))
    for pattern in patterns.get(field, []):
        match = re.search(pattern, text)
        if match:
            return normalize_space(match.group(1))
    return ""


def conflict_severity(field: str) -> str:
    return "high" if field in {"product_id", "product_name"} else "medium"


def detect_bundle_conflicts(text_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_rows = [row for row in text_rows if compact_resource_text(row.get("doc_type")) in {"product_full", "product_field"}]
    supplement_rows = [row for row in text_rows if compact_resource_text(row.get("doc_type")) == "supplement"]
    conflicts: list[dict[str, Any]] = []
    for field in ("product_id", "product_name", "brand", "spec"):
        old_value = ""
        for row in old_rows:
            old_value = extract_bundle_field(row, field)
            if old_value:
                break
        if not old_value:
            continue
        for row in supplement_rows:
            supplement_value = extract_bundle_field(row, field)
            if not supplement_value:
                continue
            if normalize_space(old_value).lower() == normalize_space(supplement_value).lower():
                continue
            conflicts.append(
                {
                    "field": field,
                    "old_value": old_value,
                    "supplement_value": supplement_value,
                    "supplement_doc_id": compact_resource_text(row.get("doc_id")),
                    "severity": conflict_severity(field),
                }
            )
    return conflicts


def build_product_bundle(product_id: str, text_rows: list[dict[str, Any]], image_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    image_rows = image_rows or []
    bundle: dict[str, Any] = {
        "product_id": product_id,
        "basic_info": [],
        "packaging": [],
        "selling_points": [],
        "gift_attributes": [],
        "quality_reports": [],
        "image_texts": [],
        "supplements": [],
        "conflicts": [],
        "source_doc_ids": [],
        "doc_ids": [],
    }
    doc_ids: set[str] = set()
    source_doc_ids: set[str] = set()
    normalized_text_rows: list[dict[str, Any]] = []
    for raw_row in text_rows:
        row = bundle_row(raw_row)
        if product_id and row.get("product_id") != product_id:
            continue
        if is_needs_review_row(row):
            continue
        normalized_text_rows.append(row)
        target = field_target(row)
        bundle[target].append(row)
        if row.get("doc_id"):
            doc_ids.add(row["doc_id"])
        if row.get("source_doc_id"):
            source_doc_ids.add(row["source_doc_id"])
    for raw_row in image_rows:
        row = bundle_row(raw_row)
        if product_id and row.get("product_id") != product_id:
            continue
        if is_needs_review_row(row):
            continue
        bundle["image_texts"].append(row)
        for key in ("doc_id", "image_id"):
            if row.get(key):
                doc_ids.add(row[key])
        if row.get("source_doc_id"):
            source_doc_ids.add(row["source_doc_id"])
    bundle["conflicts"] = detect_bundle_conflicts(normalized_text_rows)
    bundle["source_doc_ids"] = sorted(source_doc_ids)
    bundle["doc_ids"] = sorted(doc_ids)
    return bundle


def build_image_resource(hit: dict[str, Any]) -> dict[str, Any] | None:
    image_id = compact_resource_text(hit.get("image_id"))
    image_path = compact_resource_text(hit.get("image_path"))
    metadata = hit.get("metadata") or {}
    if not image_id and not image_path:
        return None
    return {
        "image_id": image_id,
        "image_path": image_path,
        "image_url": compact_resource_text(hit.get("image_url")) or compact_resource_text(metadata.get("image_url")),
        "source_kind": compact_resource_text(metadata.get("source_kind")),
        "source_file": compact_resource_text(metadata.get("source_file")),
        "source_doc_id": compact_resource_text(metadata.get("source_doc_id")),
        "source_sha1": compact_resource_text(metadata.get("source_sha1")),
        "source_url": compact_resource_text(metadata.get("source_url")),
        "chunk_index": metadata.get("chunk_index"),
    }


def build_pdf_resource(hit: dict[str, Any]) -> dict[str, Any] | None:
    metadata = hit.get("metadata") or {}
    if compact_resource_text(metadata.get("doc_type")) != "quality_report":
        return None
    source_doc_id = compact_resource_text(metadata.get("source_doc_id"))
    source_file = compact_resource_text(metadata.get("source_file"))
    if not source_doc_id and not source_file:
        return None
    return {
        "source_doc_id": source_doc_id,
        "pdf_path": source_file,
        "pdf_url": compact_resource_text(metadata.get("source_url")),
        "source_kind": compact_resource_text(metadata.get("source_kind")),
        "source_file": source_file,
        "source_sha1": compact_resource_text(metadata.get("source_sha1")),
        "chunk_index": metadata.get("chunk_index"),
        "page_start": metadata.get("page_start"),
        "page_end": metadata.get("page_end"),
        "supplement_title": compact_resource_text(metadata.get("supplement_title")),
    }


def append_unique_resource(bucket: list[dict[str, Any]], seen: set[tuple[str, str]], resource: dict[str, Any], key_fields: tuple[str, str]) -> None:
    key = tuple(compact_resource_text(resource.get(field)) for field in key_fields)
    if not any(key):
        return
    if key in seen:
        return
    seen.add(key)
    bucket.append(resource)


def extract_result_items(response: Any) -> list[dict[str, Any]]:
    plain = body_to_plain(getattr(response, "body", response))
    if not isinstance(plain, dict):
        raise RuntimeError(f"OpenSearch 响应不是 object：{safe_text(plain)[:800]}")
    for key in ("result", "results", "items", "docs", "documents"):
        value = plain.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    raise RuntimeError(f"OpenSearch 响应中未找到结果列表：{compact_json(plain)}")


def flatten_result_item(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    source = dict(fields)
    for key, value in item.items():
        if key not in source and key != "fields":
            source[key] = value
    if "score" not in source:
        source["score"] = item.get("__score__") or item.get("_score")
    return source


def fetch_by_ids(
    client: Any,
    models_module: Any,
    table_name: str,
    ids: list[str],
    output_fields: list[str],
) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    request = models_module.FetchRequest(
        table_name=table_name,
        ids=ids,
        include_vector=False,
        output_fields=output_fields,
    )
    response = client.fetch(request)
    fetched: dict[str, dict[str, Any]] = {}
    for item in extract_result_items(response):
        flat = flatten_result_item(item)
        row_id = safe_text(flat.get("id")).strip()
        if row_id:
            fetched[row_id] = flat
    return fetched


def enrich_rows_by_fetch(
    client: Any,
    models_module: Any,
    table_name: str,
    rows: list[dict[str, Any]],
    output_fields: list[str],
) -> list[dict[str, Any]]:
    ids = [safe_text(row.get("id")).strip() for row in rows if safe_text(row.get("id")).strip()]
    try:
        fetched = fetch_by_ids(client, models_module, table_name, ids, output_fields)
    except Exception as exc:
        warn(f"fetch detail failed for table={table_name}: {exc}")
        return rows
    enriched: list[dict[str, Any]] = []
    for row in rows:
        row_id = safe_text(row.get("id")).strip()
        merged = dict(row)
        for key, value in (fetched.get(row_id) or {}).items():
            if merged.get(key) in (None, "", "null"):
                merged[key] = value
            elif key != "score":
                merged.setdefault(key, value)
        enriched.append(merged)
    return enriched


class RetrievalService:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root).resolve()
        load_env(self.root)
        self.config = ensure_runtime_config()
        self.client, self.models = create_client(self.config)
        self.tables = {
            "text_docs": self._table_candidates("text_docs"),
            "image_text_docs": self._table_candidates("image_text_docs"),
            "image_vectors": self._table_candidates("image_vectors"),
        }

    def _table_candidates(self, logical_name: str) -> list[str]:
        # Push API used <instance_id>_<table>, while the query API in this
        # project currently resolves logical names. Try the required prefixed
        # form first, then fall back to the logical table name if needed.
        return [f"{self.config['instance_id']}_{logical_name}", logical_name]

    def _query_vector(
        self,
        table_names: list[str],
        vector_field: str,
        vector: list[float],
        top_k: int,
        output_fields: list[str],
    ) -> tuple[list[dict[str, Any]], str]:
        last_error: Exception | None = None
        for table_name in table_names:
            for index_name in (vector_field, None):
                try:
                    request = self.models.QueryRequest(
                        table_name=table_name,
                        index_name=index_name,
                        vector=vector,
                        top_k=top_k,
                        include_vector=False,
                        output_fields=output_fields,
                        order="DESC",
                    )
                    response = self.client.query(request)
                    rows = [flatten_result_item(item) for item in extract_result_items(response)]
                    rows = enrich_rows_by_fetch(self.client, self.models, table_name, rows, output_fields)
                    return rows, table_name
                except Exception as exc:
                    last_error = exc
        raise RuntimeError(str(last_error) if last_error else "向量查询失败")

    def _query_keyword(
        self,
        table_names: list[str],
        query: str,
        top_k: int,
        output_fields: list[str],
        field_name: str = "source_text",
    ) -> tuple[list[dict[str, Any]], str]:
        text = self.models.TextQuery(
            query_string=build_text_query_string(field_name, query),
            query_params={"default_op": "OR"},
        )
        last_error: Exception | None = None
        for table_name in table_names:
            try:
                request = self.models.SearchRequest(
                    table_name=table_name,
                    size=top_k,
                    output_fields=output_fields,
                    text=text,
                )
                response = self.client.search(request)
                return [flatten_result_item(item) for item in extract_result_items(response)], table_name
            except Exception as exc:
                last_error = exc
        raise RuntimeError(str(last_error) if last_error else "关键词查询失败")

    def _normalize_hit(self, route: str, rank: int, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "route": route,
            "rank": rank,
            "score": row.get("score"),
            "product_id": safe_text(row.get("product_id")).strip(),
            "doc_id": safe_text(row.get("doc_id") or row.get("id")).strip(),
            "image_id": safe_text(row.get("image_id")).strip(),
            "image_path": safe_text(row.get("image_path")).strip(),
            "image_url": safe_text(row.get("image_url")).strip(),
            "source_text": safe_text(row.get("source_text")).strip(),
            "metadata": {
                "raw_id": row.get("id"),
                "doc_type": row.get("doc_type"),
                "field_name": row.get("field_name"),
                "caption": row.get("caption"),
                "ocr_text": row.get("ocr_text"),
                "source_kind": row.get("source_kind"),
                "source_file": row.get("source_file"),
                "source_doc_id": row.get("source_doc_id"),
                "source_sha1": row.get("source_sha1"),
                "source_url": row.get("source_url"),
                "chunk_index": row.get("chunk_index"),
                "page_start": row.get("page_start"),
                "page_end": row.get("page_end"),
                "supplement_title": row.get("supplement_title"),
                "image_url": row.get("image_url"),
                "image_path": row.get("image_path"),
                "table_name": row.get("_table_name"),
            },
        }

    def _normalize_route_results(self, route: str, rows: list[dict[str, Any]], table_name: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            row = dict(row)
            row["_table_name"] = table_name
            hit = self._normalize_hit(route, rank, row)
            if not hit["product_id"]:
                warn(f"{route} rank={rank} missing product_id, skipped")
                continue
            normalized.append(hit)
        return normalized

    def search_text_dense(self, query: str, top_k: int = 50) -> list[dict[str, Any]]:
        try:
            vector = embed_text_query(query)
            rows, table_name = self._query_vector(
                table_names=self.tables["text_docs"],
                vector_field="source_text_vector",
                vector=vector,
                top_k=top_k,
                output_fields=[
                    "id",
                    "product_id",
                    "doc_id",
                    "doc_type",
                    "field_name",
                    "source_text",
                    "source_kind",
                    "source_file",
                    "source_doc_id",
                    "source_sha1",
                    "source_url",
                    "chunk_index",
                    "page_start",
                    "page_end",
                    "supplement_title",
                    "image_path",
                    "image_url",
                ],
            )
            return self._normalize_route_results(ROUTE_TEXT_DENSE, rows, table_name)
        except Exception as exc:
            warn(f"{ROUTE_TEXT_DENSE} failed: {exc}")
            return []

    def search_text_keyword(self, query: str, top_k: int = 50) -> list[dict[str, Any]]:
        try:
            rows, table_name = self._query_keyword(
                table_names=self.tables["text_docs"],
                query=query,
                top_k=top_k,
                output_fields=[
                    "id",
                    "product_id",
                    "doc_id",
                    "doc_type",
                    "field_name",
                    "source_text",
                    "source_kind",
                    "source_file",
                    "source_doc_id",
                    "source_sha1",
                    "source_url",
                    "chunk_index",
                    "page_start",
                    "page_end",
                    "supplement_title",
                    "image_path",
                    "image_url",
                ],
            )
            return self._normalize_route_results(ROUTE_TEXT_KEYWORD, rows, table_name)
        except Exception as exc:
            warn(f"{ROUTE_TEXT_KEYWORD} failed: {exc}")
            return []

    def search_image_text(self, query: str, top_k: int = 30) -> list[dict[str, Any]]:
        try:
            vector = embed_multimodal_text_query(query)
            rows, table_name = self._query_vector(
                table_names=self.tables["image_text_docs"],
                vector_field="source_text_vector",
                vector=vector,
                top_k=top_k,
                output_fields=[
                    "id",
                    "product_id",
                    "image_id",
                    "image_path",
                    "image_url",
                    "source_text",
                    "caption",
                    "ocr_text",
                    "source_kind",
                    "source_file",
                    "source_doc_id",
                    "source_sha1",
                    "source_url",
                    "chunk_index",
                ],
            )
            return self._normalize_route_results(ROUTE_IMAGE_TEXT, rows, table_name)
        except Exception as exc:
            warn(f"{ROUTE_IMAGE_TEXT} failed: {exc}")
            return []

    def search_image_by_existing_vector(self, vector: list[float], top_k: int = 30) -> list[dict[str, Any]]:
        try:
            vector = normalize_embedding(vector, ROUTE_IMAGE_VECTOR)
            rows, table_name = self._query_vector(
                table_names=self.tables["image_vectors"],
                vector_field="source_image_vector",
                vector=vector,
                top_k=top_k,
                output_fields=[
                    "id",
                    "product_id",
                    "image_id",
                    "image_path",
                    "image_url",
                    "source_kind",
                    "source_file",
                    "source_doc_id",
                    "source_sha1",
                    "source_url",
                    "chunk_index",
                ],
            )
            return self._normalize_route_results(ROUTE_IMAGE_VECTOR, rows, table_name)
        except Exception as exc:
            warn(f"{ROUTE_IMAGE_VECTOR} failed: {exc}")
            return []

    def _search_bundle_rows(
        self,
        table_names: list[str],
        product_id: str,
        top_k: int,
        output_fields: list[str],
    ) -> list[dict[str, Any]]:
        try:
            rows, _table_name = self._query_keyword(
                table_names=table_names,
                query=product_id,
                top_k=top_k,
                output_fields=output_fields,
                field_name="product_id",
            )
            return rows
        except Exception as exc:
            warn(f"product_bundle lookup failed for product_id={product_id}: {exc}")
            return []

    def fetch_product_bundle(self, product_id: str) -> dict[str, Any]:
        product_id = safe_text(product_id).strip()
        if not product_id:
            return build_product_bundle("", [], [])
        text_rows = self._search_bundle_rows(
            table_names=self.tables["text_docs"],
            product_id=product_id,
            top_k=120,
            output_fields=[
                "id",
                "doc_id",
                "product_id",
                "doc_type",
                "field_name",
                "source_text",
                "source_kind",
                "source_file",
                "source_doc_id",
                "source_sha1",
                "source_url",
                "chunk_index",
                "page_start",
                "page_end",
                "supplement_title",
                "status",
                "mapping_status",
                "metadata",
            ],
        )
        wanted_doc_types = {"product_full", "product_field", "quality_report", "supplement"}
        text_rows = [
            row
            for row in text_rows
            if compact_resource_text(row.get("doc_type") or normalize_metadata_value(row.get("metadata")).get("doc_type"))
            in wanted_doc_types
        ]
        image_rows = self._search_bundle_rows(
            table_names=self.tables["image_text_docs"],
            product_id=product_id,
            top_k=80,
            output_fields=[
                "id",
                "doc_id",
                "product_id",
                "image_id",
                "image_path",
                "image_url",
                "source_text",
                "caption",
                "ocr_text",
                "source_kind",
                "source_file",
                "source_doc_id",
                "source_sha1",
                "source_url",
                "chunk_index",
                "metadata",
            ],
        )
        return build_product_bundle(product_id, text_rows, image_rows)

    def attach_product_bundles(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for product in products:
            product_id = safe_text(product.get("product_id")).strip()
            if not product_id:
                continue
            product["product_bundle"] = self.fetch_product_bundle(product_id)
        return products

    def retrieve(self, query: str, top_k: int = 10) -> dict[str, Any]:
        started = time.time()
        text_dense = self.search_text_dense(query, top_k=50)
        text_keyword = self.search_text_keyword(query, top_k=50)
        image_text = self.search_image_text(query, top_k=30)
        fused = rrf_fuse([text_dense, text_keyword, image_text])
        results = self.attach_product_bundles(fused[:top_k])
        return {
            "query": query,
            "route_counts": {
                ROUTE_TEXT_DENSE: len(text_dense),
                ROUTE_TEXT_KEYWORD: len(text_keyword),
                ROUTE_IMAGE_TEXT: len(image_text),
                ROUTE_IMAGE_VECTOR: 0,
            },
            "total_products": len(fused),
            "results": results,
            "product_bundles": [item.get("product_bundle") for item in results if item.get("product_bundle")],
            "elapsed_seconds": time.time() - started,
        }


def build_text_query_string(field_name: str, query_text: str) -> str:
    # OpenSearch vector SDK TextQuery uses HA3 query syntax. Current table text
    # index is source_text; if schema changes, adjust this function only.
    escaped = query_text.replace("\\", "\\\\").replace("'", "\\'")
    return f"{field_name}:'{escaped}'"


def rrf_fuse(result_lists: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    seen_hits: defaultdict[str, set[tuple[str, str, str]]] = defaultdict(set)

    for results in result_lists:
        for hit in results:
            product_id = safe_text(hit.get("product_id")).strip()
            if not product_id:
                continue
            route = safe_text(hit.get("route")).strip()
            rank = int(hit.get("rank") or 0)
            if rank <= 0:
                continue

            item = products.setdefault(
                product_id,
                {
                    "product_id": product_id,
                    "score": 0.0,
                    "routes": [],
                    "doc_hits": [],
                    "image_hits": [],
                    "images": [],
                    "pdf_reports": [],
                    "best_pdf_hit": {},
                    "best_text": "",
                    "best_image_path": "",
                    "best_image_url": "",
                    "metadata": {"route_scores": {}, "route_best_rank": {}},
                },
            )

            item["score"] += 1.0 / (k + rank)
            if route and route not in item["routes"]:
                item["routes"].append(route)
            item["metadata"]["route_scores"][route] = item["metadata"]["route_scores"].get(route, 0.0) + (
                1.0 / (k + rank)
            )
            previous_rank = item["metadata"]["route_best_rank"].get(route)
            if previous_rank is None or rank < previous_rank:
                item["metadata"]["route_best_rank"][route] = rank

            hit_key = (route, safe_text(hit.get("doc_id")), safe_text(hit.get("image_id")))
            if hit_key in seen_hits[product_id]:
                continue
            seen_hits[product_id].add(hit_key)

            if hit.get("source_text"):
                item["doc_hits"].append(hit)
                if not item["best_text"]:
                    item["best_text"] = truncate(hit["source_text"], 600)

            image_resource = build_image_resource(hit)
            if image_resource is not None:
                image_seen = seen_hits.setdefault(f"{product_id}__images", set())
                append_unique_resource(item["images"], image_seen, image_resource, ("source_doc_id", "image_path"))

            pdf_resource = build_pdf_resource(hit)
            if pdf_resource is not None:
                pdf_seen = seen_hits.setdefault(f"{product_id}__pdfs", set())
                append_unique_resource(item["pdf_reports"], pdf_seen, pdf_resource, ("source_doc_id", "pdf_path"))
                if not item["best_pdf_hit"]:
                    item["best_pdf_hit"] = pdf_resource

            if hit.get("image_id") or hit.get("image_path"):
                item["image_hits"].append(hit)
                if not item["best_image_path"] and hit.get("image_path"):
                    item["best_image_path"] = hit["image_path"]
                if not item["best_image_url"] and hit.get("image_url"):
                    item["best_image_url"] = hit["image_url"]

            if not item["best_image_url"] and item["images"]:
                item["best_image_url"] = safe_text(item["images"][0].get("image_url")).strip()
            if not item["best_image_path"] and item["images"]:
                item["best_image_path"] = safe_text(item["images"][0].get("image_path")).strip()

    fused = sorted(products.values(), key=lambda item: item["score"], reverse=True)
    for item in fused:
        item["score"] = float(item["score"])
        item["routes"] = sorted(item["routes"])
        item["doc_hits"] = item["doc_hits"][:8]
        item["image_hits"] = item["image_hits"][:8]
        item["images"] = item["images"][:8]
        item["pdf_reports"] = item["pdf_reports"][:8]
    return fused


_DEFAULT_SERVICE: RetrievalService | None = None


def get_default_service() -> RetrievalService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = RetrievalService()
    return _DEFAULT_SERVICE


def search_text_dense(query: str, top_k: int = 50) -> list[dict[str, Any]]:
    return get_default_service().search_text_dense(query, top_k)


def search_text_keyword(query: str, top_k: int = 50) -> list[dict[str, Any]]:
    return get_default_service().search_text_keyword(query, top_k)


def search_image_text(query: str, top_k: int = 30) -> list[dict[str, Any]]:
    return get_default_service().search_image_text(query, top_k)


def search_image_by_existing_vector(vector: list[float], top_k: int = 30) -> list[dict[str, Any]]:
    return get_default_service().search_image_by_existing_vector(vector, top_k)


def retrieve(query: str, top_k: int = 10) -> dict[str, Any]:
    return get_default_service().retrieve(query, top_k)


def fetch_product_bundle(product_id: str) -> dict[str, Any]:
    return get_default_service().fetch_product_bundle(product_id)
