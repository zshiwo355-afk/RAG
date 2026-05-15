#!/usr/bin/env python3
"""
Batch-push image vectors from images_embedded.jsonl to Alibaba Cloud OpenSearch.

Current stage:
- push image vectors only
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
import os
import sys
import time
from pathlib import Path
from typing import Any

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
VENDOR_DIR = ROOT / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_SLEEP = 2.0
DEFAULT_BATCH_SLEEP = 0.5
DEFAULT_ABORT_CONSECUTIVE_FORBIDDEN = 3
VECTOR_DIMENSIONS = 1024
PK_FIELD = "id"
TABLE_NAME = "image_vectors"
IMAGE_URL_MAPPING_PATH = "output/image_url_mapping.json"


def load_env(root: Path) -> None:
    """Load .env if python-dotenv exists; otherwise use a tiny fallback parser."""
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


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def truncate(text: str, limit: int = 120) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def ensure_runtime_config() -> dict[str, str]:
    config = {
        "endpoint": os.getenv("OPENSEARCH_ENDPOINT", "").strip(),
        "instance_id": os.getenv("OPENSEARCH_INSTANCE_ID", "").strip(),
        "username": os.getenv("OPENSEARCH_USERNAME", "").strip(),
        "password": os.getenv("OPENSEARCH_PASSWORD", "").strip(),
    }
    missing = [
        env_name
        for env_name, value in [
            ("OPENSEARCH_ENDPOINT", config["endpoint"]),
            ("OPENSEARCH_INSTANCE_ID", config["instance_id"]),
            ("OPENSEARCH_USERNAME", config["username"]),
            ("OPENSEARCH_PASSWORD", config["password"]),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            "缺少 OpenSearch 连接配置："
            + ", ".join(missing)
            + "。请在 .env 中补齐这些环境变量。"
        )
    config["endpoint"] = config["endpoint"].rstrip("/")
    return config


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
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"{path.name} 第 {line_number} 行顶层必须是 object")
            records.append(raw)
    return records


def normalize_vector(values: Any, image_id: str) -> list[float]:
    if not isinstance(values, list):
        raise ValueError(f"{image_id} 的 embedding 不是数组")
    if len(values) != VECTOR_DIMENSIONS:
        raise ValueError(
            f"{image_id} 的 embedding 维度不正确：期望 {VECTOR_DIMENSIONS}，实际 {len(values)}"
        )
    normalized: list[float] = []
    for index, value in enumerate(values):
        if not isinstance(value, (int, float)):
            raise ValueError(f"{image_id} 的 embedding 第 {index} 维不是数字")
        normalized.append(float(value))
    return normalized


def build_push_document(raw: dict[str, Any], index: int, image_url_mapping: dict[str, str] | None = None) -> dict[str, Any]:
    image_id = safe_text(raw.get("image_id")).strip()
    if not image_id:
        raise ValueError(f"第 {index} 条记录缺少 image_id")

    image_url = safe_text(raw.get("image_url")).strip()
    if not image_url:
        image_url = resolve_mapped_url(image_url_mapping or {}, raw.get("image_path"))
    source_url = infer_image_source_url(raw) or image_url

    fields = {
        "id": image_id,
        "image_id": image_id,
        "product_id": safe_text(raw.get("product_id")).strip(),
        "image_path": safe_text(raw.get("image_path")).strip(),
        "image_url": image_url,
        "source_file": safe_text(raw.get("source_file")).strip(),
        "source_sheet": safe_text(raw.get("source_sheet")).strip(),
        "source_row": safe_text(raw.get("source_row")).strip(),
        "anchor_cell": safe_text(raw.get("anchor_cell")).strip(),
        "match_status": safe_text(raw.get("match_status")).strip(),
        "source_kind": infer_image_source_kind(raw),
        "source_doc_id": infer_image_source_doc_id(raw),
        "source_sha1": infer_image_source_sha1(raw),
        "source_url": source_url,
        "chunk_index": infer_image_chunk_index(raw),
        "source_image_vector": normalize_vector(raw.get("embedding"), image_id),
    }
    return {
        "cmd": "add",
        "fields": fields,
    }


def iter_batches(items: list[dict[str, Any]], batch_size: int):
    for start in range(0, len(items), batch_size):
        yield start, items[start : start + batch_size]


def import_sdk_modules():
    try:
        from alibabacloud_ha3engine_vector.client import Client
        from alibabacloud_ha3engine_vector.models import Config
        from alibabacloud_ha3engine_vector import models
    except ImportError as exc:
        raise RuntimeError(
            "未安装阿里云 OpenSearch 向量检索版 Python SDK。"
            "请先安装：pip install alibabacloud_ha3engine_vector==1.1.17"
        ) from exc
    return Client, Config, models


def resolve_protocol(endpoint_value: str) -> str:
    return "HTTPS" if endpoint_value.lower().startswith("https://") else "HTTP"


def create_client(config: dict[str, str]):
    Client, Config, models = import_sdk_modules()
    endpoint_value = config["endpoint"]
    protocol = resolve_protocol(endpoint_value)
    client = Client(
        Config(
            endpoint=endpoint_value,
            instance_id=config["instance_id"],
            protocol=protocol,
            access_user_name=config["username"],
            access_pass_word=config["password"],
        )
    )
    return client, models


def build_push_request(models_module: Any, documents: list[dict[str, Any]]):
    try:
        return models_module.PushDocumentsRequest({}, documents)
    except Exception:
        request = models_module.PushDocumentsRequest()
        try:
            request.headers = {}
        except Exception:
            pass
        try:
            request.body = documents
        except Exception:
            setattr(request, "body", documents)
        return request


def body_to_plain_data(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except Exception:
            return text
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
    return str(body)


def is_success_response(payload: Any) -> tuple[bool, str]:
    plain = body_to_plain_data(payload)
    if isinstance(plain, dict):
        status = safe_text(plain.get("status")).strip().upper()
        code = plain.get("code")
        if status == "OK" or str(code) == "200":
            return True, json.dumps(plain, ensure_ascii=False)
        return False, json.dumps(plain, ensure_ascii=False)
    if isinstance(plain, str):
        normalized = plain.strip()
        if '"status"' in normalized and '"OK"' in normalized:
            return True, normalized
        if '"code"' in normalized and "200" in normalized:
            return True, normalized
        return False, normalized or "empty response"
    return False, safe_text(plain).strip() or "empty response"


def extract_error_code(message: str) -> str | None:
    lowered = message.lower()
    if "403" in lowered or "forbidden" in lowered:
        return "403"
    if "401" in lowered or "unauthorized" in lowered:
        return "401"
    if "429" in lowered or "too many requests" in lowered or "throttle" in lowered:
        return "429"
    if "timeout" in lowered:
        return "timeout"
    return None


def push_batch_with_retry(
    client: Any,
    models_module: Any,
    table_name: str,
    batch_documents: list[dict[str, Any]],
    batch_number: int,
    max_retries: int,
    retry_sleep: float,
) -> tuple[bool, str, str | None]:
    last_message = "unknown error"
    last_error_code: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            request = build_push_request(models_module, batch_documents)
            response = client.push_documents(table_name, PK_FIELD, request)
            ok, response_message = is_success_response(getattr(response, "body", None))
            if ok:
                return True, response_message, None
            last_message = response_message
        except Exception as exc:
            last_message = str(exc).strip() or exc.__class__.__name__

        last_error_code = extract_error_code(last_message)
        print(
            f"[WARN] batch={batch_number} attempt={attempt}/{max_retries} failed: {last_message}"
        )
        lowered = last_message.lower()
        non_retryable = (
            "401" in lowered
            or "403" in lowered
            or "invalid credentials" in lowered
            or "access denied" in lowered
        )
        if non_retryable or attempt == max_retries:
            break
        time.sleep(retry_sleep * attempt)
    return False, last_message, last_error_code


def build_samples_preview(documents: list[dict[str, Any]], sample_count: int = 3) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in documents[:sample_count]:
        fields = item.get("fields") or {}
        vector = fields.get("source_image_vector") or []
        samples.append(
            {
                "id": fields.get("id"),
                "image_id": fields.get("image_id"),
                "product_id": fields.get("product_id"),
                "image_path": truncate(safe_text(fields.get("image_path")), 80),
                "match_status": fields.get("match_status"),
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
        "# image_vectors 推送报告",
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
        f"- 向量字段：`source_image_vector`",
        f"- 向量维度：{VECTOR_DIMENSIONS}",
        f"- 请求协议：`{protocol or 'N/A'}`",
        f"- 批大小：{batch_size}",
        f"- 批次间休眠：{batch_sleep:.2f} 秒",
        f"- 模式：`{'dry-run' if dry_run else 'push'}`",
        f"- 总耗时：{elapsed_seconds:.2f} 秒",
        "",
    ]

    if fatal_error:
        lines.extend(
            [
                "## 致命错误",
                "",
                f"- {fatal_error}",
                "",
            ]
        )

    lines.extend(
        [
            "## 前 3 条样本预览",
            "",
            "| # | id | image_id | product_id | image_path | match_status | 向量前 5 维 |",
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
                    safe_text(sample.get("match_status")),
                    f"[{vector_preview}]",
                ]
            )
            + " |"
        )
    if not samples:
        lines.append("| 1 | 无 | 无 | 无 | 无 | 无 | 无 |")

    lines.extend(
        [
            "",
            "## 失败原因统计",
            "",
            "| 失败原因 | 数量 |",
            "| --- | ---: |",
        ]
    )
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
        description="Push images_embedded.jsonl to Alibaba Cloud OpenSearch image_vectors table."
    )
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument(
        "--input",
        default="output/images_embedded.jsonl",
        help="Input JSONL file.",
    )
    parser.add_argument(
        "--report",
        default="output/push_image_vectors_report.md",
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
    table_name = "<instance_id>_image_vectors"
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
                image_id = safe_text(raw_record.get("image_id")).strip() or f"row_{index}"
                failure_records.append(
                    {
                        "batch_number": "prepare",
                        "start_index": index,
                        "end_index": index,
                        "count": 1,
                        "reason": str(exc),
                        "sample_ids": [image_id],
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
