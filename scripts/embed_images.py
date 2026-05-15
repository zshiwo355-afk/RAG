#!/usr/bin/env python3
"""
Embed exported product images with Alibaba Cloud DashScope multimodal embeddings.

Current stage:
- image embedding only
- no video embedding
- no Qdrant ingestion
- no RAG API

This script reads output/image_mapping.json, converts each exported local image
to a Base64 Data URI, and calls the official DashScope multimodal embedding HTTP
API with model qwen3-vl-embedding.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from source_identity import build_image_source_doc_id, load_url_mapping, resolve_mapped_url, sha1_file


ROOT = Path(__file__).resolve().parents[1]
API_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)
MODEL_NAME = "qwen3-vl-embedding"
VECTOR_DIMENSION = 1024
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_SLEEP = 2.0
DEFAULT_IMAGE_URL_MAPPING = "output/image_url_mapping.json"


def load_env(root: Path) -> None:
    """Load .env if python-dotenv is available; otherwise use a tiny fallback."""
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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def ensure_api_key() -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "未检测到 DASHSCOPE_API_KEY。请在项目根目录 .env 中添加：\n"
            "DASHSCOPE_API_KEY=你的百炼API Key"
        )
    return api_key


def load_json_list(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"{label} 不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{label} 顶层必须是 list：{path}")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{label} 第 {index} 项必须是 object：{path}")
        normalized.append(item)
    return normalized


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def normalize_rel_path(value: Any) -> str:
    text = safe_text(value).strip()
    if not text:
        return ""
    return Path(text).as_posix()


def make_source_key(source_file: Any, source_sheet: Any, source_row: Any) -> tuple[str, str, int | None]:
    row_value: int | None
    if source_row in (None, ""):
        row_value = None
    else:
        try:
            row_value = int(source_row)
        except Exception:
            row_value = None
    return safe_text(source_file).strip(), safe_text(source_sheet).strip(), row_value


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


def load_product_lookup(documents_path: Path) -> tuple[dict[tuple[str, str, int | None], str], dict[str, str]]:
    if not documents_path.exists():
        return {}, {}

    docs = load_json_list(documents_path, "documents_preview.json")
    product_by_row: dict[tuple[str, str, int | None], str] = {}
    product_by_image: dict[str, str] = {}

    for item in docs:
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        product_id = safe_text(metadata.get("product_id")).strip()
        if not product_id:
            continue

        key = make_source_key(
            metadata.get("source_file"),
            metadata.get("source_sheet"),
            metadata.get("source_row"),
        )
        prefer = metadata.get("doc_type") == "product_full"
        if key[2] is not None and (key not in product_by_row or prefer):
            product_by_row[key] = product_id

        image_paths = metadata.get("image_paths") or []
        if isinstance(image_paths, list):
            for image_path in image_paths:
                rel_path = normalize_rel_path(image_path)
                if rel_path and (rel_path not in product_by_image or prefer):
                    product_by_image[rel_path] = product_id

    return product_by_row, product_by_image


def sanitize_identifier(value: str, max_length: int = 64) -> str:
    compact = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return (compact or "image")[:max_length]


def generate_image_id(item: dict[str, Any], index: int) -> str:
    image_path = normalize_rel_path(item.get("image_path"))
    source_file = safe_text(item.get("source_file")).strip()
    source_sheet = safe_text(item.get("source_sheet")).strip()
    source_row = safe_text(item.get("source_row")).strip()
    anchor_cell = safe_text(item.get("anchor_cell")).strip()
    product_id = safe_text(item.get("product_id")).strip()
    image_index = safe_text(item.get("image_index")).strip()

    readable_parts = []
    if product_id:
        readable_parts.append(product_id)
    elif source_row:
        readable_parts.append(f"r{source_row}")
    if anchor_cell:
        readable_parts.append(anchor_cell.lower())
    if image_index:
        readable_parts.append(f"img{image_index}")
    if not readable_parts and image_path:
        readable_parts.append(Path(image_path).stem)
    if not readable_parts:
        readable_parts.append(f"image_{index}")

    readable = sanitize_identifier("_".join(readable_parts))
    digest_source = "||".join(
        [
            image_path,
            product_id,
            source_file,
            source_sheet,
            source_row,
            anchor_cell,
            str(index),
        ]
    )
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:12]
    return f"img_{readable}_{digest}"


def enrich_mapping_item(
    raw_item: dict[str, Any],
    index: int,
    product_by_row: dict[tuple[str, str, int | None], str],
    product_by_image: dict[str, str],
) -> dict[str, Any]:
    image_path = normalize_rel_path(raw_item.get("image_path"))
    source_file = safe_text(raw_item.get("source_file")).strip()
    source_sheet = safe_text(raw_item.get("source_sheet")).strip()
    source_row = raw_item.get("source_row")
    anchor_cell = safe_text(raw_item.get("anchor_cell")).strip() or None

    product_id = safe_text(raw_item.get("product_id")).strip() or None
    if not product_id and image_path:
        product_id = product_by_image.get(image_path)
    if not product_id:
        product_id = product_by_row.get(make_source_key(source_file, source_sheet, source_row))

    item = {
        "image_id": raw_item.get("image_id") or "",
        "image_path": image_path or None,
        "product_id": product_id or None,
        "source_file": source_file or None,
        "source_sheet": source_sheet or None,
        "source_row": normalize_int(source_row),
        "anchor_cell": anchor_cell,
        "matched": raw_item.get("matched"),
        "match_status": raw_item.get("match_status"),
        "source_type": raw_item.get("source_type"),
        "source_col": raw_item.get("source_col"),
        "image_index": raw_item.get("image_index"),
        "exported": raw_item.get("exported"),
        "image_url": raw_item.get("image_url"),
        "source_url": raw_item.get("source_url"),
        "source_doc_id": raw_item.get("source_doc_id"),
        "source_sha1": raw_item.get("source_sha1"),
        "mapping_error": raw_item.get("error"),
    }
    item["image_id"] = safe_text(item["image_id"]).strip() or generate_image_id(item, index)
    return item


def resolve_image_path(root: Path, image_path: str | None) -> Path | None:
    if not image_path:
        return None
    path = Path(image_path)
    if not path.is_absolute():
        path = root / path
    return path


def sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return "image/tiff"
    return None


def image_file_to_data_uri(image_path: Path) -> tuple[str, str, int]:
    try:
        data = image_path.read_bytes()
    except Exception as exc:
        raise RuntimeError(f"读取图片失败：{exc}") from exc

    if not data:
        raise RuntimeError("图片内容为空")

    mime_type = sniff_image_mime(data)
    if not mime_type:
        guessed_mime, _ = mimetypes.guess_type(image_path.name)
        mime_type = guessed_mime

    if not mime_type or not mime_type.startswith("image/"):
        raise RuntimeError(f"无法识别图片 MIME 类型：{image_path.name}")

    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", mime_type, len(data)


def compact_json(value: Any, limit: int = 1200) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else f"{text[:limit]}..."


def describe_api_error(payload: Any) -> str:
    if isinstance(payload, dict):
        code = safe_text(payload.get("code")).strip()
        message = payload.get("message")
        details = []
        if code:
            details.append(code)
        if isinstance(message, list):
            details.append("; ".join(safe_text(item).strip() for item in message if safe_text(item).strip()))
        elif message:
            details.append(safe_text(message).strip())
        if details:
            return " | ".join(part for part in details if part)
        return compact_json(payload)
    return safe_text(payload).strip() or "unknown error"


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> tuple[int, Any]:
    try:
        import requests  # type: ignore
    except Exception:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_obj = urllib_request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(request_obj, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
                status_code = response.getcode()
        except urllib_error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(raw_body) if raw_body.strip() else {}
            except Exception:
                error_payload = {"message": raw_body[:2000]}
            raise RuntimeError(f"HTTP {exc.code}: {describe_api_error(error_payload)}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"网络请求失败：{exc.reason}") from exc

        try:
            response_payload = json.loads(raw_body) if raw_body.strip() else {}
        except Exception as exc:
            raise RuntimeError(f"响应不是合法 JSON：{raw_body[:500]}") from exc
        return status_code, response_payload

    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response_text = response.text
    try:
        response_payload = response.json() if response_text.strip() else {}
    except Exception:
        response_payload = {"message": response_text[:2000]}

    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code}: {describe_api_error(response_payload)}"
        )
    return response.status_code, response_payload


def request_embedding(api_key: str, data_uri: str, timeout: int) -> list[float]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "input": {
            "contents": [
                {
                    "image": data_uri,
                }
            ]
        },
        "parameters": {
            "dimension": VECTOR_DIMENSION,
            "enable_fusion": False,
        },
    }

    status_code, response_payload = post_json(API_URL, headers, payload, timeout)
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"HTTP {status_code}: {describe_api_error(response_payload)}")

    output = response_payload.get("output") or {}
    if not isinstance(output, dict):
        raise RuntimeError(f"响应缺少 output：{compact_json(response_payload)}")

    embeddings = output.get("embeddings")
    if not isinstance(embeddings, list) or not embeddings:
        raise RuntimeError(f"响应缺少 embeddings：{compact_json(response_payload)}")

    embeddings = sorted(
        (item for item in embeddings if isinstance(item, dict)),
        key=lambda item: item.get("index", 0),
    )
    if not embeddings:
        raise RuntimeError(f"embeddings 为空：{compact_json(response_payload)}")

    embedding = embeddings[0].get("embedding")
    if not isinstance(embedding, list):
        raise RuntimeError(f"embedding 字段缺失：{compact_json(response_payload)}")
    if len(embedding) != VECTOR_DIMENSION:
        raise RuntimeError(
            f"返回向量维度不一致：期望 {VECTOR_DIMENSION}，实际 {len(embedding)}"
        )

    return embedding


def request_embedding_with_retry(
    api_key: str,
    data_uri: str,
    timeout: int,
    max_retries: int,
    retry_sleep: float,
    image_id: str,
) -> tuple[list[float] | None, str | None]:
    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return request_embedding(api_key, data_uri, timeout), None
        except Exception as exc:
            last_error = safe_text(exc).strip() or "unknown error"
            print(
                f"[WARN] image_id={image_id} attempt={attempt}/{max_retries} failed: {last_error}"
            )
            lowered = last_error.lower()
            non_retryable = (
                "http 400" in lowered
                or "http 401" in lowered
                or "http 403" in lowered
                or "invalidparameter" in lowered
                or "image size should be" in lowered
            )
            if non_retryable:
                break
            if attempt < max_retries:
                time.sleep(retry_sleep * attempt)
    return None, last_error


def make_failure(item: dict[str, Any], reason: str, message: str) -> dict[str, Any]:
    return {
        "image_id": item["image_id"],
        "image_path": item.get("image_path"),
        "product_id": item.get("product_id"),
        "source_file": item.get("source_file"),
        "source_sheet": item.get("source_sheet"),
        "source_row": item.get("source_row"),
        "anchor_cell": item.get("anchor_cell"),
        "matched": item.get("matched"),
        "match_status": item.get("match_status"),
        "reason": reason,
        "message": message,
    }


def write_jsonl_record(
    file_obj,
    item: dict[str, Any],
    embedding: list[float],
    mime_type: str,
    image_size_bytes: int,
) -> None:
    output = {
        "image_id": item["image_id"],
        "image_path": item.get("image_path"),
        "product_id": item.get("product_id"),
        "source_file": item.get("source_file"),
        "source_sheet": item.get("source_sheet"),
        "source_row": item.get("source_row"),
        "anchor_cell": item.get("anchor_cell"),
        "matched": item.get("matched"),
        "match_status": item.get("match_status"),
        "source_type": item.get("source_type"),
        "image_index": item.get("image_index"),
        "image_url": item.get("image_url"),
        "source_url": item.get("source_url"),
        "source_doc_id": item.get("source_doc_id"),
        "source_sha1": item.get("source_sha1"),
        "mime_type": mime_type,
        "image_size_bytes": image_size_bytes,
        "embedding": embedding,
    }
    file_obj.write(json.dumps(output, ensure_ascii=False) + "\n")


def preview_vector(values: list[float], limit: int = 8) -> str:
    return ", ".join(f"{float(value):.6f}" for value in values[:limit])


def build_report(
    total_images: int,
    attempted_images: int,
    product_linked_count: int,
    match_status_counts: Counter[str],
    success_count: int,
    failures: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    elapsed_seconds: float,
) -> str:
    failure_reason_counts = Counter(failure["reason"] for failure in failures)

    sample_lines = [
        "| # | image_id | product_id | image_path | 向量前 8 维 |",
        "| ---: | --- | --- | --- | --- |",
    ]
    if samples:
        for idx, sample in enumerate(samples[:5], start=1):
            sample_lines.append(
                f"| {idx} | `{sample['image_id']}` | `{sample.get('product_id') or ''}` | "
                f"`{sample.get('image_path') or ''}` | "
                f"[{preview_vector(sample['embedding'])}] |"
            )
    else:
        sample_lines.append("| 1 | - | - | - | 无成功样本 |")

    reason_lines = [
        "| 失败原因 | 数量 |",
        "| --- | ---: |",
    ]
    if failure_reason_counts:
        for reason, count in failure_reason_counts.most_common():
            reason_lines.append(f"| `{reason}` | {count} |")
    else:
        reason_lines.append("| 无 | 0 |")

    failure_lines = []
    for failure in failures[:20]:
        failure_lines.append(
            f"- `{failure['image_id']}` | `{failure.get('image_path') or ''}` | "
            f"`{failure['reason']}` | {failure['message']}"
        )
    if not failure_lines:
        failure_lines.append("- 无")

    match_status_lines = [
        "| match_status | 数量 |",
        "| --- | ---: |",
    ]
    if match_status_counts:
        for status, count in match_status_counts.most_common():
            match_status_lines.append(f"| `{status}` | {count} |")
    else:
        match_status_lines.append("| 无 | 0 |")

    return f"""# 图片 Embedding 执行报告

## 总览

- 图片总数：{total_images}
- 实际尝试 embedding 数：{attempted_images}
- 已关联 product_id 数：{product_linked_count}
- 成功 embedding 数：{success_count}
- 失败数：{len(failures)}
- 使用模型：`{MODEL_NAME}`
- 向量维度：{VECTOR_DIMENSION}
- enable_fusion：`false`
- 总耗时：{elapsed_seconds:.2f} 秒

## 匹配状态分布

{chr(10).join(match_status_lines)}

## 前 5 条样本预览

{chr(10).join(sample_lines)}

## 失败原因统计

{chr(10).join(reason_lines)}

## 失败明细（前 20 条）

{chr(10).join(failure_lines)}
"""


def run_dry_run(
    root: Path,
    items: list[dict[str, Any]],
) -> None:
    convertible = 0
    failures: list[dict[str, Any]] = []
    product_linked = 0
    match_status_counts = Counter(
        safe_text(item.get("match_status")).strip() or "unknown" for item in items
    )

    for item in items:
        if item.get("product_id"):
            product_linked += 1

        image_path = item.get("image_path")
        abs_path = resolve_image_path(root, image_path)

        if not image_path:
            failures.append(
                make_failure(
                    item,
                    "missing_image_path",
                    safe_text(item.get("mapping_error")).strip() or "映射中没有 image_path",
                )
            )
            continue

        if abs_path is None or not abs_path.exists():
            failures.append(
                make_failure(item, "image_not_found", f"图片不存在：{abs_path}")
            )
            continue

        try:
            _, mime_type, image_size_bytes = image_file_to_data_uri(abs_path)
        except Exception as exc:
            failures.append(
                make_failure(item, "image_read_error", safe_text(exc).strip())
            )
            continue

        convertible += 1
        if convertible <= 5:
            print(
                "[DRY-RUN]",
                item["image_id"],
                item.get("image_path"),
                f"mime={mime_type}",
                f"bytes={image_size_bytes}",
                f"product_id={item.get('product_id') or 'None'}",
            )

    print(f"Loaded mapping items: {len(items)}")
    print(f"Linked product_id count: {product_linked}")
    print(f"Convertible local images: {convertible}")
    print(f"Preflight failures: {len(failures)}")
    print("Match status counts:")
    for status, count in match_status_counts.most_common():
        print(f"  - {status}: {count}")

    failure_reason_counts = Counter(failure["reason"] for failure in failures)
    for reason, count in failure_reason_counts.most_common():
        print(f"  - {reason}: {count}")


def run_embedding(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    load_env(root)

    mapping_path = (root / args.input).resolve()
    documents_path = (root / args.documents).resolve()
    output_path = (root / args.output).resolve()
    report_path = (root / args.report).resolve()
    image_url_mapping_path = (root / DEFAULT_IMAGE_URL_MAPPING).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    raw_mapping = load_json_list(mapping_path, "image_mapping.json")
    product_by_row, product_by_image = load_product_lookup(documents_path)
    image_url_mapping = load_url_mapping(image_url_mapping_path)
    items = [
        enrich_mapping_item(item, index, product_by_row, product_by_image)
        for index, item in enumerate(raw_mapping, start=1)
    ]

    print(f"Loaded image mapping items: {len(items)}")
    print(f"Loaded product lookup rows: {len(product_by_row)}")
    print(f"Loaded product lookup image paths: {len(product_by_image)}")

    if args.dry_run:
        print("Dry-run enabled: validating image paths and Data URI conversion only.")
        run_dry_run(root, items)
        print(f"Would write JSONL to: {output_path}")
        print(f"Would write report to: {report_path}")
        return

    api_key = ensure_api_key()

    start_time = time.time()
    attempted_images = 0
    product_linked_count = sum(1 for item in items if item.get("product_id"))
    match_status_counts = Counter(
        safe_text(item.get("match_status")).strip() or "unknown" for item in items
    )
    success_count = 0
    failures: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []

    with output_path.open("w", encoding="utf-8") as file_obj:
        for index, item in enumerate(items, start=1):
            print(f"Embedding image {index}/{len(items)}: {item['image_id']}")

            image_path = item.get("image_path")
            abs_path = resolve_image_path(root, image_path)

            if not image_path:
                failures.append(
                    make_failure(
                        item,
                        "missing_image_path",
                        safe_text(item.get("mapping_error")).strip() or "映射中没有 image_path",
                    )
                )
                continue

            if abs_path is None or not abs_path.exists():
                failures.append(
                    make_failure(item, "image_not_found", f"图片不存在：{abs_path}")
                )
                continue

            try:
                data_uri, mime_type, image_size_bytes = image_file_to_data_uri(abs_path)
            except Exception as exc:
                failures.append(
                    make_failure(item, "image_read_error", safe_text(exc).strip())
                )
                continue

            attempted_images += 1
            source_sha1 = sha1_file(abs_path)
            image_url = resolve_mapped_url(image_url_mapping, item.get("image_path"))
            item["source_sha1"] = source_sha1
            item["source_doc_id"] = build_image_source_doc_id(source_sha1, item.get("image_id"))
            item["image_url"] = image_url
            item["source_url"] = image_url
            embedding, error = request_embedding_with_retry(
                api_key=api_key,
                data_uri=data_uri,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_sleep=args.retry_sleep,
                image_id=item["image_id"],
            )
            if embedding is None:
                failures.append(make_failure(item, "api_error", error or "unknown error"))
                continue

            write_jsonl_record(file_obj, item, embedding, mime_type, image_size_bytes)
            success_count += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "image_id": item["image_id"],
                        "product_id": item.get("product_id"),
                        "image_path": item.get("image_path"),
                        "match_status": item.get("match_status"),
                        "embedding": embedding,
                    }
                )

    elapsed_seconds = time.time() - start_time
    report = build_report(
        total_images=len(items),
        attempted_images=attempted_images,
        product_linked_count=product_linked_count,
        match_status_counts=match_status_counts,
        success_count=success_count,
        failures=failures,
        samples=samples,
        elapsed_seconds=elapsed_seconds,
    )
    report_path.write_text(report, encoding="utf-8")

    print(f"Embedding complete: success={success_count}, failed={len(failures)}")
    print(f"JSONL output: {output_path}")
    print(f"Report: {report_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Embed exported images with DashScope qwen3-vl-embedding."
    )
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument(
        "--input",
        default="output/image_mapping.json",
        help="Input image mapping JSON file.",
    )
    parser.add_argument(
        "--documents",
        default="output/documents_preview.json",
        help="Optional documents preview JSON for product_id lookup.",
    )
    parser.add_argument(
        "--output",
        default="output/images_embedded.jsonl",
        help="Output JSONL file.",
    )
    parser.add_argument(
        "--report",
        default="output/image_embedding_report.md",
        help="Output Markdown report file.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP request timeout seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="Retry count per failed image.",
    )
    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=DEFAULT_RETRY_SLEEP,
        help="Base retry sleep seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and image conversion without calling the API.",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.max_retries <= 0:
        raise SystemExit("--max-retries must be positive")
    if args.retry_sleep < 0:
        raise SystemExit("--retry-sleep must be non-negative")
    return args


if __name__ == "__main__":
    try:
        run_embedding(parse_args())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
