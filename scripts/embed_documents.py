#!/usr/bin/env python3
"""
Embed text documents with Alibaba Cloud Bailian / DashScope text-embedding-v4.

Current stage:
- text embedding only
- no image embedding
- no Qdrant ingestion
- no RAG API
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
    sys.path.insert(0, str(VENDOR_DIR))

MODEL_NAME = "text-embedding-v4"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DIMENSIONS = 1024
ENCODING_FORMAT = "float"
# DashScope currently rejects text-embedding-v4 batches larger than 10.
DEFAULT_BATCH_SIZE = 10
DEFAULT_TIMEOUT = 120


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
            "未检测到 DASHSCOPE_API_KEY。请在项目根目录 .env 中添加：DASHSCOPE_API_KEY=你的百炼APIKey"
        )
    return api_key


def load_documents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    docs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(docs, list):
        raise ValueError(f"{path.name} 顶层必须是 list")
    return docs


def normalize_document(raw_doc: dict[str, Any], index: int) -> dict[str, Any] | None:
    page_content = str(raw_doc.get("page_content") or "").strip()
    if not page_content:
        return None

    metadata = raw_doc.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    doc_id = metadata.get("doc_id") or f"doc_{index:06d}"
    return {
        "doc_id": doc_id,
        "product_id": metadata.get("product_id"),
        "doc_type": metadata.get("doc_type"),
        "field_name": metadata.get("field_name"),
        "page_content": page_content,
        "metadata": metadata,
    }


def iter_batches(items: list[dict[str, Any]], batch_size: int):
    for start in range(0, len(items), batch_size):
        yield start, items[start : start + batch_size]


def describe_api_error(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                str(error.get("code") or "").strip(),
                str(error.get("message") or "").strip(),
            ]
            return " | ".join(part for part in parts if part) or json.dumps(payload, ensure_ascii=False)[:1200]
        code = str(payload.get("code") or "").strip()
        message = str(payload.get("message") or "").strip()
        if code or message:
            return " | ".join(part for part in [code, message] if part)
        return json.dumps(payload, ensure_ascii=False)[:1200]
    return str(payload).strip() or "unknown error"


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> Any:
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
        raise RuntimeError(f"HTTP {exc.code}: {describe_api_error(error_payload)}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc.reason}") from exc

    try:
        return json.loads(raw_body) if raw_body.strip() else {}
    except Exception as exc:
        raise RuntimeError(f"响应不是合法 JSON：{raw_body[:500]}") from exc


def embed_batch(batch: list[dict[str, Any]], timeout: int) -> list[list[float]]:
    payload = {
        "model": MODEL_NAME,
        "input": [item["page_content"] for item in batch],
        "dimensions": DIMENSIONS,
        "encoding_format": ENCODING_FORMAT,
    }
    headers = {
        "Authorization": f"Bearer {ensure_api_key()}",
        "Content-Type": "application/json",
    }
    response_payload = post_json(f"{BASE_URL}/embeddings", headers, payload, timeout)
    data = response_payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"响应缺少 data：{json.dumps(response_payload, ensure_ascii=False)[:1200]}")

    data = sorted((item for item in data if isinstance(item, dict)), key=lambda item: item.get("index", 0))
    embeddings = [item.get("embedding") for item in data]
    if len(embeddings) != len(batch):
        raise RuntimeError(f"返回向量数量不一致：期望 {len(batch)}，实际 {len(embeddings)}")
    for embedding in embeddings:
        if not isinstance(embedding, list):
            raise RuntimeError("响应中的 embedding 不是数组")
        if len(embedding) != DIMENSIONS:
            raise RuntimeError(f"返回向量维度不一致：期望 {DIMENSIONS}，实际 {len(embedding)}")
    return embeddings


def embed_batch_with_retry(
    batch: list[dict[str, Any]],
    batch_number: int,
    max_retries: int,
    retry_sleep: float,
    timeout: int,
) -> tuple[list[list[float]] | None, str | None]:
    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return embed_batch(batch, timeout), None
        except Exception as exc:
            last_error = str(exc)
            print(f"[WARN] batch={batch_number} attempt={attempt}/{max_retries} failed: {last_error}")
            lowered = last_error.lower()
            non_retryable = (
                "http 400" in lowered
                or "http 401" in lowered
                or "http 403" in lowered
            )
            if non_retryable:
                break
            if attempt < max_retries:
                time.sleep(retry_sleep * attempt)
    return None, last_error


def write_jsonl_record(file_obj, item: dict[str, Any], embedding: list[float]) -> None:
    output = {
        "doc_id": item["doc_id"],
        "product_id": item.get("product_id"),
        "doc_type": item.get("doc_type"),
        "field_name": item.get("field_name"),
        "page_content": item["page_content"],
        "metadata": item.get("metadata") or {},
        "embedding": embedding,
    }
    file_obj.write(json.dumps(output, ensure_ascii=False) + "\n")


def build_report(
    total_docs: int,
    embeddable_docs: int,
    skipped_empty: int,
    success_count: int,
    failures: list[dict[str, Any]],
    batch_size: int,
    elapsed_seconds: float,
    samples: list[dict[str, Any]],
) -> str:
    sample_lines = [
        "| # | doc_id | doc_type | field_name | embedding 前 8 维 |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for idx, sample in enumerate(samples[:5], start=1):
        vector_preview = ", ".join(f"{value:.6f}" for value in sample["embedding"][:8])
        sample_lines.append(
            f"| {idx} | `{sample['doc_id']}` | {sample.get('doc_type') or ''} | "
            f"{sample.get('field_name') or ''} | [{vector_preview}] |"
        )

    failure_reason_counts = Counter()
    for failure in failures:
        failure_reason_counts[failure.get("error") or "unknown error"] += failure.get("count", 0)

    failure_reason_lines = ["| 失败原因 | 数量 |", "| --- | ---: |"]
    if failure_reason_counts:
        for reason, count in failure_reason_counts.most_common():
            failure_reason_lines.append(f"| `{reason}` | {count} |")
    else:
        failure_reason_lines.append("| 无 | 0 |")

    failure_lines = []
    for failure in failures[:20]:
        failure_lines.append(
            f"- batch {failure['batch_number']}：{failure['count']} 条失败，原因：{failure['error']}"
        )
    if not failure_lines:
        failure_lines.append("- 无")

    return f"""# Embedding 执行报告 v2

## 总览

- 输入文档总数：{total_docs}
- 非空可向量化文档数：{embeddable_docs}
- 跳过空正文文档数：{skipped_empty}
- 成功 embedding 数：{success_count}
- 失败数：{sum(item['count'] for item in failures)}
- 使用模型名：`{MODEL_NAME}`
- 向量维度：{DIMENSIONS}
- encoding_format：`{ENCODING_FORMAT}`
- 批大小：{batch_size}
- 总耗时：{elapsed_seconds:.2f} 秒

## 前 5 条样本预览

{chr(10).join(sample_lines)}

## 失败原因统计

{chr(10).join(failure_reason_lines)}

## 失败批次

{chr(10).join(failure_lines)}
"""


def run_embedding(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    load_env(root)

    input_path = (root / args.input).resolve()
    output_path = (root / args.output).resolve()
    report_path = (root / args.report).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    raw_docs = load_documents(input_path)
    normalized_docs = []
    skipped_empty = 0
    for index, raw_doc in enumerate(raw_docs, start=1):
        normalized = normalize_document(raw_doc, index)
        if normalized is None:
            skipped_empty += 1
            continue
        normalized_docs.append(normalized)

    print(f"Loaded documents: {len(raw_docs)}")
    print(f"Embeddable documents: {len(normalized_docs)}")
    print(f"Batch size: {args.batch_size}")

    if args.dry_run:
        print("Dry-run enabled: no API call, no embedding output written.")
        print(f"Would write JSONL to: {output_path}")
        print(f"Would write report to: {report_path}")
        return

    ensure_api_key()

    start_time = time.time()
    success_count = 0
    failures: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    total_batches = (len(normalized_docs) + args.batch_size - 1) // args.batch_size

    with output_path.open("w", encoding="utf-8") as file_obj:
        for batch_index, (start, batch) in enumerate(iter_batches(normalized_docs, args.batch_size), start=1):
            print(f"Embedding batch {batch_index}/{total_batches}: docs {start + 1}-{start + len(batch)}")
            embeddings, error = embed_batch_with_retry(
                batch=batch,
                batch_number=batch_index,
                max_retries=args.max_retries,
                retry_sleep=args.retry_sleep,
                timeout=args.timeout,
            )
            if embeddings is None:
                failures.append(
                    {
                        "batch_number": batch_index,
                        "start_index": start + 1,
                        "count": len(batch),
                        "error": error or "unknown error",
                        "doc_ids": [item["doc_id"] for item in batch],
                    }
                )
                continue

            for item, embedding in zip(batch, embeddings):
                write_jsonl_record(file_obj, item, embedding)
                success_count += 1
                if len(samples) < 5:
                    samples.append(
                        {
                            "doc_id": item["doc_id"],
                            "doc_type": item.get("doc_type"),
                            "field_name": item.get("field_name"),
                            "embedding": embedding,
                        }
                    )

    elapsed = time.time() - start_time
    report = build_report(
        total_docs=len(raw_docs),
        embeddable_docs=len(normalized_docs),
        skipped_empty=skipped_empty,
        success_count=success_count,
        failures=failures,
        batch_size=args.batch_size,
        elapsed_seconds=elapsed,
        samples=samples,
    )
    report_path.write_text(report, encoding="utf-8")

    print(f"Embedding complete: success={success_count}, failed={sum(item['count'] for item in failures)}")
    print(f"JSONL output: {output_path}")
    print(f"Report: {report_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed text documents with DashScope text-embedding-v4.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input", default="output/documents_preview_v2.json", help="Input documents JSON.")
    parser.add_argument("--output", default="output/documents_embedded_v2.jsonl", help="Output JSONL file.")
    parser.add_argument("--report", default="output/embedding_report_v2.md", help="Embedding report path.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Batch size for embedding calls.")
    parser.add_argument("--max-retries", type=int, default=3, help="Retry count per failed batch.")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Base retry sleep seconds.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP request timeout seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Validate input and plan batches without API calls.")
    args = parser.parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.max_retries <= 0:
        raise SystemExit("--max-retries must be positive")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
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
