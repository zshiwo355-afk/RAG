#!/usr/bin/env python3
"""
Embed image semantic text docs with DashScope qwen3-vl-embedding.

This script embeds OCR/caption/tag text derived from images. It does not re-embed
the image files themselves.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / ".vendor"
if VENDOR_DIR.exists():
    sys.path.append(str(VENDOR_DIR))

API_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)
MODEL_NAME = "qwen3-vl-embedding"
VECTOR_DIMENSION = 1024
DEFAULT_BATCH_SIZE = 10
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_SLEEP = 2.0


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
            "未检测到 DASHSCOPE_API_KEY。请在项目根目录 .env 中添加：DASHSCOPE_API_KEY=你的百炼API Key"
        )
    return api_key


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def compact_json(payload: Any, limit: int = 1200) -> str:
    return json.dumps(payload, ensure_ascii=False)[:limit]


def describe_api_error(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                safe_text(error.get("code")).strip(),
                safe_text(error.get("message")).strip(),
            ]
            return " | ".join(part for part in parts if part) or compact_json(payload)
        code = safe_text(payload.get("code")).strip()
        message = safe_text(payload.get("message")).strip()
        if code or message:
            return " | ".join(part for part in [code, message] if part)
        return compact_json(payload)
    return safe_text(payload).strip() or "unknown error"


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_obj = urllib_request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(request_obj, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            status_code = response.status
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


def normalize_doc(raw_doc: dict[str, Any], index: int) -> dict[str, Any] | None:
    source_text = safe_text(raw_doc.get("source_text")).strip()
    if not source_text:
        return None

    image_id = safe_text(raw_doc.get("image_id") or raw_doc.get("id")).strip()
    if not image_id:
        image_id = f"image_text_{index:06d}"

    metadata = raw_doc.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    return {
        "id": safe_text(raw_doc.get("id")).strip() or image_id,
        "image_id": image_id,
        "product_id": safe_text(raw_doc.get("product_id")).strip(),
        "image_path": safe_text(raw_doc.get("image_path")).strip(),
        "image_url": safe_text(raw_doc.get("image_url")).strip(),
        "source_kind": safe_text(raw_doc.get("source_kind")).strip(),
        "source_file": safe_text(raw_doc.get("source_file")).strip(),
        "source_doc_id": safe_text(raw_doc.get("source_doc_id")).strip(),
        "source_sha1": safe_text(raw_doc.get("source_sha1")).strip(),
        "source_url": safe_text(raw_doc.get("source_url")).strip(),
        "source_text": source_text,
        "metadata": metadata,
        "caption": safe_text(raw_doc.get("caption")).strip(),
        "ocr_text": safe_text(raw_doc.get("ocr_text")).strip(),
        "visual_tags": raw_doc.get("visual_tags") if isinstance(raw_doc.get("visual_tags"), list) else [],
        "packaging_type": safe_text(raw_doc.get("packaging_type")).strip(),
        "gift_style": safe_text(raw_doc.get("gift_style")).strip(),
        "scene_hint": safe_text(raw_doc.get("scene_hint")).strip(),
    }


def iter_batches(items: list[dict[str, Any]], batch_size: int):
    for start in range(0, len(items), batch_size):
        yield start, items[start : start + batch_size]


def request_embeddings(api_key: str, batch: list[dict[str, Any]], timeout: int) -> list[list[float]]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "input": {
            "contents": [
                {
                    "text": item["source_text"],
                }
                for item in batch
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

    response_items = output.get("embeddings")
    if not isinstance(response_items, list):
        raise RuntimeError(f"响应缺少 embeddings：{compact_json(response_payload)}")

    response_items = sorted(
        (item for item in response_items if isinstance(item, dict)),
        key=lambda item: item.get("index", 0),
    )
    embeddings = [item.get("embedding") for item in response_items]
    if len(embeddings) != len(batch):
        raise RuntimeError(f"返回向量数量不一致：期望 {len(batch)}，实际 {len(embeddings)}")

    normalized: list[list[float]] = []
    for item, embedding in zip(batch, embeddings):
        if not isinstance(embedding, list):
            raise RuntimeError(f"{item['id']} 的 embedding 不是数组")
        if len(embedding) != VECTOR_DIMENSION:
            raise RuntimeError(
                f"{item['id']} 的 embedding 维度不正确：期望 {VECTOR_DIMENSION}，实际 {len(embedding)}"
            )
        normalized.append([float(value) for value in embedding])
    return normalized


def request_embeddings_with_retry(
    api_key: str,
    batch: list[dict[str, Any]],
    batch_number: int,
    timeout: int,
    max_retries: int,
    retry_sleep: float,
) -> tuple[list[list[float]] | None, str | None]:
    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return request_embeddings(api_key, batch, timeout), None
        except Exception as exc:
            last_error = safe_text(exc).strip() or "unknown error"
            print(f"[WARN] batch={batch_number} attempt={attempt}/{max_retries} failed: {last_error}")
            lowered = last_error.lower()
            non_retryable = (
                "http 400" in lowered
                or "http 401" in lowered
                or "http 403" in lowered
                or "invalidparameter" in lowered
            )
            if non_retryable:
                break
            if attempt < max_retries:
                time.sleep(retry_sleep * attempt)
    return None, last_error


def write_jsonl_record(file_obj, item: dict[str, Any], embedding: list[float]) -> None:
    output = {
        "id": item["id"],
        "image_id": item["image_id"],
        "product_id": item.get("product_id"),
        "image_path": item.get("image_path"),
        "image_url": item.get("image_url"),
        "source_kind": item.get("source_kind"),
        "source_file": item.get("source_file"),
        "source_doc_id": item.get("source_doc_id"),
        "source_sha1": item.get("source_sha1"),
        "source_url": item.get("source_url"),
        "source_text": item["source_text"],
        "metadata": item.get("metadata") or {},
        "caption": item.get("caption"),
        "ocr_text": item.get("ocr_text"),
        "visual_tags": item.get("visual_tags") or [],
        "packaging_type": item.get("packaging_type"),
        "gift_style": item.get("gift_style"),
        "scene_hint": item.get("scene_hint"),
        "embedding": embedding,
    }
    file_obj.write(json.dumps(output, ensure_ascii=False) + "\n")


def preview_vector(values: list[float], limit: int = 8) -> str:
    return ", ".join(f"{float(value):.6f}" for value in values[:limit])


def build_report(
    input_total: int,
    embeddable_total: int,
    skipped_empty: int,
    selected_total: int,
    success_count: int,
    failures: list[dict[str, Any]],
    batch_size: int,
    elapsed_seconds: float,
    samples: list[dict[str, Any]],
    output_path: Path,
) -> str:
    failure_reason_counts = Counter()
    for failure in failures:
        failure_reason_counts[failure.get("error") or "unknown error"] += failure.get("count", 0)

    sample_lines = [
        "| # | id | image_id | product_id | image_path | embedding 前 8 维 |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for index, sample in enumerate(samples[:5], start=1):
        sample_lines.append(
            f"| {index} | `{sample['id']}` | `{sample['image_id']}` | `{sample.get('product_id') or ''}` | "
            f"`{sample.get('image_path') or ''}` | [{preview_vector(sample['embedding'])}] |"
        )
    if not samples:
        sample_lines.append("| 1 | - | - | - | - | 无成功样本 |")

    failure_reason_lines = ["| 失败原因 | 数量 |", "| --- | ---: |"]
    if failure_reason_counts:
        for reason, count in failure_reason_counts.most_common():
            failure_reason_lines.append(f"| `{reason}` | {count} |")
    else:
        failure_reason_lines.append("| 无 | 0 |")

    failure_lines = []
    for failure in failures[:20]:
        failure_lines.append(
            f"- batch={failure.get('batch_number')} rows={failure.get('start_index')}-{failure.get('end_index')} "
            f"count={failure.get('count')} error={failure.get('error')} sample_ids={failure.get('sample_ids')}"
        )
    if not failure_lines:
        failure_lines.append("- 无")

    return f"""# 图片语义文本 Embedding 执行报告

## 总览

- 输入记录数：{input_total}
- 可向量化记录数：{embeddable_total}
- 空文本跳过数：{skipped_empty}
- 本次计划向量化数：{selected_total}
- 成功 embedding 数：{success_count}
- 失败数：{sum(int(item.get('count') or 0) for item in failures)}
- 使用模型：`{MODEL_NAME}`
- 向量维度：{VECTOR_DIMENSION}
- enable_fusion：`false`
- 批大小：{batch_size}
- 输出文件：`{output_path.as_posix()}`
- 总耗时：{elapsed_seconds:.2f} 秒

## 前 5 条样本预览

{chr(10).join(sample_lines)}

## 失败原因统计

{chr(10).join(failure_reason_lines)}

## 失败明细（前 20 条）

{chr(10).join(failure_lines)}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed image semantic text docs with qwen3-vl-embedding.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input", default="output/image_text_docs.jsonl", help="Input JSONL file.")
    parser.add_argument("--output", default="output/image_text_embedded.jsonl", help="Output JSONL file.")
    parser.add_argument("--report", default="output/image_text_embedding_report.md", help="Report path.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Batch size.")
    parser.add_argument("--limit", type=int, default=None, help="Only embed the first N docs for testing.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Retry count per failed batch.")
    parser.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP, help="Base retry sleep seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    input_path = (root / args.input).resolve()
    output_path = (root / args.output).resolve()
    report_path = (root / args.report).resolve()

    load_env(root)
    start_time = time.time()

    if args.batch_size <= 0:
        raise ValueError("--batch-size 必须大于 0")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit 不能小于 0")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0")
    if args.max_retries <= 0:
        raise ValueError("--max-retries 必须大于 0")
    if args.retry_sleep < 0:
        raise ValueError("--retry-sleep 不能小于 0")

    raw_records = load_jsonl_records(input_path)
    docs: list[dict[str, Any]] = []
    skipped_empty = 0
    for index, raw_record in enumerate(raw_records, start=1):
        doc = normalize_doc(raw_record, index)
        if doc is None:
            skipped_empty += 1
            continue
        docs.append(doc)

    selected_docs = docs[: args.limit] if args.limit is not None else docs
    api_key = ensure_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    success_count = 0
    total_batches = (len(selected_docs) + args.batch_size - 1) // args.batch_size if selected_docs else 0

    with output_path.open("w", encoding="utf-8") as file_obj:
        for batch_number, (start, batch) in enumerate(iter_batches(selected_docs, args.batch_size), start=1):
            end = start + len(batch)
            print(f"Embedding batch {batch_number}/{total_batches}: docs {start + 1}-{end}")
            embeddings, error = request_embeddings_with_retry(
                api_key=api_key,
                batch=batch,
                batch_number=batch_number,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_sleep=args.retry_sleep,
            )
            if embeddings is None:
                failures.append(
                    {
                        "batch_number": batch_number,
                        "start_index": start + 1,
                        "end_index": end,
                        "count": len(batch),
                        "error": error or "unknown error",
                        "sample_ids": [item["id"] for item in batch[:5]],
                    }
                )
                continue

            for item, embedding in zip(batch, embeddings):
                write_jsonl_record(file_obj, item, embedding)
                success_count += 1
                if len(samples) < 5:
                    sample = dict(item)
                    sample["embedding"] = embedding
                    samples.append(sample)

    elapsed_seconds = time.time() - start_time
    report = build_report(
        input_total=len(raw_records),
        embeddable_total=len(docs),
        skipped_empty=skipped_empty,
        selected_total=len(selected_docs),
        success_count=success_count,
        failures=failures,
        batch_size=args.batch_size,
        elapsed_seconds=elapsed_seconds,
        samples=samples,
        output_path=output_path,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    print(f"Input records: {len(raw_records)}")
    print(f"Embeddable docs: {len(docs)}")
    print(f"Selected docs: {len(selected_docs)}")
    print(f"Success embeddings: {success_count}")
    print(f"Failures: {sum(int(item.get('count') or 0) for item in failures)}")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
