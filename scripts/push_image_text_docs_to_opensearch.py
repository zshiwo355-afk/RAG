#!/usr/bin/env python3
"""
Batch-push image semantic text vectors to Alibaba Cloud OpenSearch.

Current stage:
- push image_text_docs vectors only
- no source data rewrite

Official references used for this script:
- OpenSearch vector search edition Python SDK:
  alibabacloud_ha3engine_vector==1.1.17
- Table name format: <instance_id>_<table_name>
- Push body format: [{"cmd": "add", "fields": {...}}, ...]
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import time
from pathlib import Path
from typing import Any

from push_image_vectors_to_opensearch import (
    DEFAULT_ABORT_CONSECUTIVE_FORBIDDEN,
    DEFAULT_BATCH_SIZE,
    DEFAULT_BATCH_SLEEP,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_SLEEP,
    PK_FIELD,
    VECTOR_DIMENSIONS,
    create_client,
    ensure_runtime_config,
    iter_batches,
    load_env,
    load_jsonl_records,
    normalize_vector,
    push_batch_with_retry,
    resolve_protocol,
    safe_text,
    truncate,
)
from source_identity import (
    infer_image_chunk_index,
    infer_image_source_doc_id,
    infer_image_source_file,
    infer_image_source_kind,
    infer_image_source_sha1,
    infer_image_source_url,
    load_url_mapping,
    resolve_mapped_url,
)


ROOT = Path(__file__).resolve().parents[1]
TABLE_NAME = "image_text_docs"
VECTOR_FIELD = "source_text_vector"
IMAGE_URL_MAPPING_PATH = "output/image_url_mapping.json"


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            return value
        if not isinstance(value, (str, list)):
            return value
    return ""


def normalize_tags(value: Any) -> str:
    if isinstance(value, list):
        parts = [safe_text(item).strip() for item in value]
    elif isinstance(value, str):
        parts = [part.strip() for part in value.replace(",", "、").split("、")]
    else:
        parts = []

    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        tags.append(part)
    return "、".join(tags)


def build_push_document(raw: dict[str, Any], index: int, image_url_mapping: dict[str, str] | None = None) -> dict[str, Any]:
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    doc_id = safe_text(first_non_empty(raw.get("id"), metadata.get("id"))).strip()
    if not doc_id:
        raise ValueError(f"第 {index} 条记录缺少 id")

    image_id = safe_text(first_non_empty(raw.get("image_id"), metadata.get("image_id"), doc_id)).strip()
    source_text = safe_text(first_non_empty(raw.get("source_text"), metadata.get("source_text"))).strip()
    if not source_text:
        raise ValueError(f"{doc_id} 的 source_text 为空")

    visual_tags_value = first_non_empty(raw.get("visual_tags"), metadata.get("visual_tags"))
    image_path = safe_text(first_non_empty(raw.get("image_path"), metadata.get("image_path"))).strip()
    image_url = safe_text(first_non_empty(raw.get("image_url"), metadata.get("image_url"))).strip()
    if not image_url:
        image_url = resolve_mapped_url(image_url_mapping or {}, image_path)
    source_url = infer_image_source_url(raw) or image_url
    fields = {
        "id": doc_id,
        "image_id": image_id,
        "product_id": safe_text(first_non_empty(raw.get("product_id"), metadata.get("product_id"))).strip(),
        "image_path": image_path,
        "image_url": image_url,
        "source_kind": infer_image_source_kind(raw),
        "source_file": infer_image_source_file(raw),
        "source_doc_id": infer_image_source_doc_id(raw),
        "source_sha1": infer_image_source_sha1(raw),
        "source_url": source_url,
        "chunk_index": infer_image_chunk_index(raw),
        "source_text": source_text,
        "caption": safe_text(first_non_empty(raw.get("caption"), metadata.get("caption"))).strip(),
        "ocr_text": safe_text(first_non_empty(raw.get("ocr_text"), metadata.get("ocr_text"))).strip(),
        "visual_tags": normalize_tags(visual_tags_value),
        VECTOR_FIELD: normalize_vector(raw.get("embedding"), doc_id),
    }
    return {
        "cmd": "add",
        "fields": fields,
    }


def build_samples_preview(documents: list[dict[str, Any]], sample_count: int = 3) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in documents[:sample_count]:
        fields = item.get("fields") or {}
        vector = fields.get(VECTOR_FIELD) or []
        samples.append(
            {
                "id": fields.get("id"),
                "image_id": fields.get("image_id"),
                "product_id": fields.get("product_id"),
                "image_path": truncate(safe_text(fields.get("image_path")), 80),
                "source_text_preview": truncate(safe_text(fields.get("source_text")), 120),
                "vector_preview": vector[:5],
            }
        )
    return samples


def build_report(
    input_total: int,
    selected_total: int,
    success_count: int,
    failure_records: list[dict[str, Any]],
    batch_size: int,
    batch_sleep: float,
    elapsed_seconds: float,
    table_name: str,
    samples: list[dict[str, Any]],
    dry_run: bool,
    fatal_error: str | None,
    selection_start: int,
    protocol: str | None,
) -> str:
    failure_counter = Counter(item.get("reason") or "unknown" for item in failure_records)
    failed_record_count = sum(int(item.get("count") or 0) for item in failure_records)
    lines = [
        "# image_text_docs 推送报告",
        "",
        "## 总览",
        "",
        f"- 输入记录总数：{input_total}",
        f"- 本次计划推送数：{selected_total}",
        f"- 本次起始偏移：{selection_start}",
        f"- 成功推送数：{success_count}",
        f"- 失败批次数：{len(failure_records)}",
        f"- 失败文档数：{failed_record_count}",
        f"- 目标表：`{table_name}`",
        f"- 主键字段：`{PK_FIELD}`",
        f"- 向量字段：`{VECTOR_FIELD}`",
        f"- 向量维度：{VECTOR_DIMENSIONS}",
        f"- 请求协议：`{protocol or 'N/A'}`",
        f"- 批大小：{batch_size}",
        f"- 批次间休眠：{batch_sleep:.2f} 秒",
        f"- 模式：`{'dry-run' if dry_run else 'push'}`",
        f"- 总耗时：{elapsed_seconds:.2f} 秒",
        "",
    ]

    if fatal_error:
        lines.extend(["## 致命错误", "", f"- {fatal_error}", ""])

    lines.extend(
        [
            "## 前 3 条样本预览",
            "",
            "| # | id | image_id | product_id | image_path | source_text 预览 | 向量前 5 维 |",
            "| ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, sample in enumerate(samples, start=1):
        vector_preview = ", ".join(f"{float(value):.6f}" for value in sample["vector_preview"])
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    f"`{safe_text(sample.get('id'))}`",
                    f"`{safe_text(sample.get('image_id'))}`",
                    f"`{safe_text(sample.get('product_id'))}`",
                    safe_text(sample.get("image_path")),
                    safe_text(sample.get("source_text_preview")),
                    f"[{vector_preview}]",
                ]
            )
            + " |"
        )
    if not samples:
        lines.append("| 1 | 无 | 无 | 无 | 无 | 无 | 无 |")

    lines.extend(["", "## 失败原因统计", "", "| 失败原因 | 数量 |", "| --- | ---: |"])
    if failure_counter:
        for reason, count in failure_counter.most_common():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| 无 | 0 |")

    lines.extend(["", "## 失败明细", ""])
    if failure_records:
        for item in failure_records:
            lines.append(
                "- "
                + f"batch={item.get('batch_number')} "
                + f"rows={item.get('start_index')}-{item.get('end_index')} "
                + f"count={item.get('count')} "
                + f"reason={item.get('reason')} "
                + f"sample_ids={item.get('sample_ids')}"
            )
    else:
        lines.append("- 无")

    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push image_text_embedded.jsonl to Alibaba Cloud OpenSearch image_text_docs table."
    )
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument(
        "--input",
        default="output/image_text_embedded.jsonl",
        help="Input JSONL file.",
    )
    parser.add_argument(
        "--report",
        default="output/push_image_text_docs_report.md",
        help="Output report path.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Batch size for SDK push.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="Retry count per failed batch.",
    )
    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=DEFAULT_RETRY_SLEEP,
        help="Base retry sleep seconds.",
    )
    parser.add_argument(
        "--batch-sleep",
        type=float,
        default=DEFAULT_BATCH_SLEEP,
        help="Sleep seconds between successful batches.",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Skip the first N prepared records and continue from there.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only push the first N records for testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate structure and generate report without calling OpenSearch.",
    )
    parser.add_argument(
        "--abort-on-consecutive-forbidden",
        type=int,
        default=DEFAULT_ABORT_CONSECUTIVE_FORBIDDEN,
        help="Abort after N consecutive 403 batches. Set 0 to disable.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    input_path = (root / args.input).resolve()
    report_path = (root / args.report).resolve()

    load_env(root)
    image_url_mapping = load_url_mapping((root / IMAGE_URL_MAPPING_PATH).resolve())
    start_time = time.time()
    fatal_error: str | None = None

    input_total = 0
    selected_total = 0
    success_count = 0
    failure_records: list[dict[str, Any]] = []
    table_name = "<instance_id>_image_text_docs"
    samples: list[dict[str, Any]] = []
    selection_start = 0
    protocol: str | None = None

    try:
        if args.batch_size <= 0:
            raise ValueError("--batch-size 必须大于 0")
        if args.start_offset < 0:
            raise ValueError("--start-offset 不能小于 0")
        if args.batch_sleep < 0:
            raise ValueError("--batch-sleep 不能小于 0")
        if args.abort_on_consecutive_forbidden < 0:
            raise ValueError("--abort-on-consecutive-forbidden 不能小于 0")

        raw_records = load_jsonl_records(input_path)
        input_total = len(raw_records)

        push_documents: list[dict[str, Any]] = []
        for index, raw_record in enumerate(raw_records, start=1):
            try:
                push_documents.append(build_push_document(raw_record, index, image_url_mapping))
            except Exception as exc:
                doc_id = safe_text(raw_record.get("id")).strip() or f"row_{index}"
                failure_records.append(
                    {
                        "batch_number": "prepare",
                        "start_index": index,
                        "end_index": index,
                        "count": 1,
                        "reason": str(exc),
                        "sample_ids": [doc_id],
                    }
                )

        if args.start_offset:
            push_documents = push_documents[args.start_offset :]
            selection_start = args.start_offset

        if args.limit is not None:
            if args.limit < 0:
                raise ValueError("--limit 不能小于 0")
            push_documents = push_documents[: args.limit]

        selected_total = len(push_documents)
        samples = build_samples_preview(push_documents)

        print(f"Loaded records: {input_total}")
        print(f"Prepared pushable records: {selected_total}")
        print(f"Start offset: {selection_start}")
        print(f"Batch size: {args.batch_size}")
        print(f"Batch sleep: {args.batch_sleep:.2f}s")

        if args.dry_run:
            print("Dry-run enabled. Skip OpenSearch push.")
        else:
            config = ensure_runtime_config()
            table_name = f"{config['instance_id']}_{TABLE_NAME}"
            protocol = resolve_protocol(config["endpoint"])
            client, models_module = create_client(config)

            total_batches = (selected_total + args.batch_size - 1) // args.batch_size if selected_total else 0
            consecutive_forbidden = 0
            for batch_index, (start, batch) in enumerate(iter_batches(push_documents, args.batch_size), start=1):
                end = start + len(batch)
                absolute_start = selection_start + start
                absolute_end = selection_start + end
                print(
                    f"Pushing batch {batch_index}/{total_batches}: records {absolute_start + 1}-{absolute_end}"
                )
                ok, message, error_code = push_batch_with_retry(
                    client=client,
                    models_module=models_module,
                    table_name=table_name,
                    batch_documents=batch,
                    batch_number=batch_index,
                    max_retries=args.max_retries,
                    retry_sleep=args.retry_sleep,
                )
                if ok:
                    consecutive_forbidden = 0
                    success_count += len(batch)
                    if args.batch_sleep > 0 and batch_index < total_batches:
                        time.sleep(args.batch_sleep)
                    continue
                failure_records.append(
                    {
                        "batch_number": batch_index,
                        "start_index": absolute_start + 1,
                        "end_index": absolute_end,
                        "count": len(batch),
                        "reason": message,
                        "error_code": error_code,
                        "sample_ids": [item["fields"]["id"] for item in batch[:5]],
                    }
                )
                if error_code == "403":
                    consecutive_forbidden += 1
                    if (
                        args.abort_on_consecutive_forbidden > 0
                        and consecutive_forbidden >= args.abort_on_consecutive_forbidden
                    ):
                        fatal_error = (
                            f"连续 {consecutive_forbidden} 个批次返回 403 Forbidden，"
                            "已提前停止，避免继续撞公网访问控制。"
                        )
                        print(f"[ERROR] {fatal_error}")
                        break
                else:
                    consecutive_forbidden = 0

        elapsed_seconds = time.time() - start_time
        report_content = build_report(
            input_total=input_total,
            selected_total=selected_total,
            success_count=success_count,
            failure_records=failure_records,
            batch_size=args.batch_size,
            batch_sleep=args.batch_sleep,
            elapsed_seconds=elapsed_seconds,
            table_name=table_name,
            samples=samples,
            dry_run=args.dry_run,
            fatal_error=fatal_error,
            selection_start=selection_start,
            protocol=protocol,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_content, encoding="utf-8")

        if args.dry_run:
            print(f"Dry-run complete. Report: {report_path}")
            return 0

        print(
            f"Push complete: success={success_count}, failed_batches={len(failure_records)}, report={report_path}"
        )
        return 0 if not failure_records and not fatal_error else 1

    except Exception as exc:
        fatal_error = str(exc)
        elapsed_seconds = time.time() - start_time
        report_content = build_report(
            input_total=input_total,
            selected_total=selected_total,
            success_count=success_count,
            failure_records=failure_records,
            batch_size=args.batch_size,
            batch_sleep=args.batch_sleep,
            elapsed_seconds=elapsed_seconds,
            table_name=table_name,
            samples=samples,
            dry_run=args.dry_run,
            fatal_error=fatal_error,
            selection_start=selection_start,
            protocol=protocol,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_content, encoding="utf-8")
        print(f"[ERROR] {fatal_error}")
        print(f"Report: {report_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
