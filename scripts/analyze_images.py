#!/usr/bin/env python3
"""
Analyze exported product images to add semantic metadata.

Current stage:
- do not re-run image embedding
- do not modify original images
- do not write to Qdrant
- add OCR, caption, visual tags, and optional semantic hints
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from embed_images import (
    ensure_api_key,
    enrich_mapping_item,
    image_file_to_data_uri,
    load_env,
    load_json_list,
    load_product_lookup,
    normalize_rel_path,
    post_json,
    resolve_image_path,
    safe_text,
)


ROOT = Path(__file__).resolve().parents[1]
CHAT_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_VISION_MODEL = "qwen3.6-plus"
DEFAULT_OCR_MODEL = "qwen-vl-ocr-latest"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_SLEEP = 2.0
DEFAULT_MIN_PIXELS = 256 * 256
DEFAULT_MAX_PIXELS = 32 * 32 * 8192
DEFAULT_JSON_MAX_TOKENS = 800
DEFAULT_OCR_MAX_TOKENS = 1200


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except Exception as exc:
                raise ValueError(f"JSONL 解析失败：{path} 第 {line_number} 行：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL 顶层必须是 object：{path} 第 {line_number} 行")
            records.append(payload)
    return records


def merge_input_items(
    root: Path,
    mapping_path: Path,
    embedded_path: Path,
    documents_path: Path,
) -> list[dict[str, Any]]:
    product_by_row, product_by_image = load_product_lookup(documents_path)
    raw_mapping = load_json_list(mapping_path, "image_mapping.json") if mapping_path.exists() else []
    mapping_items = [
        enrich_mapping_item(item, index, product_by_row, product_by_image)
        for index, item in enumerate(raw_mapping, start=1)
    ]

    embedded_rows = load_jsonl_records(embedded_path)
    embedded_by_path: dict[str, dict[str, Any]] = {}
    for row in embedded_rows:
        image_path = normalize_rel_path(row.get("image_path"))
        if image_path:
            embedded_by_path[image_path] = row

    items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for item in mapping_items:
        merged = dict(item)
        image_path = normalize_rel_path(item.get("image_path"))
        embedded = embedded_by_path.get(image_path)
        if embedded:
            merged["image_id"] = safe_text(embedded.get("image_id")).strip() or merged["image_id"]
            merged["product_id"] = embedded.get("product_id") or merged.get("product_id")
            merged["source_file"] = embedded.get("source_file") or merged.get("source_file")
            merged["source_sheet"] = embedded.get("source_sheet") or merged.get("source_sheet")
            merged["source_row"] = embedded.get("source_row") or merged.get("source_row")
            merged["anchor_cell"] = embedded.get("anchor_cell") or merged.get("anchor_cell")
            merged["matched"] = embedded.get("matched", merged.get("matched"))
            merged["match_status"] = embedded.get("match_status", merged.get("match_status"))
            merged["source_type"] = embedded.get("source_type") or merged.get("source_type")
            merged["image_index"] = embedded.get("image_index") or merged.get("image_index")
            merged["image_url"] = embedded.get("image_url") or merged.get("image_url")
            merged["source_url"] = embedded.get("source_url") or merged.get("source_url")
            merged["source_doc_id"] = embedded.get("source_doc_id") or merged.get("source_doc_id")
            merged["source_sha1"] = embedded.get("source_sha1") or merged.get("source_sha1")
            merged["mime_type"] = embedded.get("mime_type")
            merged["image_size_bytes"] = embedded.get("image_size_bytes")
            merged["has_embedding"] = True
        else:
            merged["has_embedding"] = False
        items.append(merged)
        if image_path:
            seen_paths.add(image_path)

    for row in embedded_rows:
        image_path = normalize_rel_path(row.get("image_path"))
        if not image_path or image_path in seen_paths:
            continue
        items.append(
            {
                "image_id": safe_text(row.get("image_id")).strip(),
                "image_path": image_path,
                "product_id": row.get("product_id"),
                "source_file": row.get("source_file"),
                "source_sheet": row.get("source_sheet"),
                "source_row": row.get("source_row"),
                "anchor_cell": row.get("anchor_cell"),
                "matched": row.get("matched"),
                "match_status": row.get("match_status"),
                "source_type": row.get("source_type"),
                "image_index": row.get("image_index"),
                "image_url": row.get("image_url"),
                "source_url": row.get("source_url"),
                "source_doc_id": row.get("source_doc_id"),
                "source_sha1": row.get("source_sha1"),
                "mime_type": row.get("mime_type"),
                "image_size_bytes": row.get("image_size_bytes"),
                "mapping_error": None,
                "has_embedding": True,
            }
        )

    return items


def build_chat_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def extract_message_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"响应缺少 choices：{json.dumps(response_payload, ensure_ascii=False)[:1200]}")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return safe_text(content).strip()


def find_first_json_object(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError("模型返回为空")

    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    if fenced.startswith("{") and fenced.endswith("}"):
        return fenced

    start_index = fenced.find("{")
    if start_index < 0:
        raise ValueError(f"模型返回中未找到 JSON 对象：{fenced[:500]}")

    depth = 0
    in_string = False
    escape = False
    for index in range(start_index, len(fenced)):
        char = fenced[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return fenced[start_index : index + 1]

    raise ValueError(f"模型返回中未找到完整 JSON 对象：{fenced[:500]}")


def parse_json_object(text: str) -> dict[str, Any]:
    payload = json.loads(find_first_json_object(text))
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 顶层不是 object")
    return payload


def normalize_tags(value: Any) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, list):
        raw_items = [safe_text(item).strip() for item in value]
    elif isinstance(value, str):
        raw_items = [part.strip() for part in re.split(r"[,\n，、;；]+", value)]
    else:
        raw_items = [safe_text(value).strip()] if safe_text(value).strip() else []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        cleaned = re.sub(r"\s+", " ", item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
        if len(normalized) >= 8:
            break
    return normalized


def normalize_analysis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ocr_text = safe_text(payload.get("ocr_text")).strip()
    caption = safe_text(payload.get("caption")).strip()
    visual_tags = normalize_tags(payload.get("visual_tags"))
    packaging_type = safe_text(payload.get("packaging_type")).strip()
    gift_style = safe_text(payload.get("gift_style")).strip()
    scene_hint = safe_text(payload.get("scene_hint")).strip()

    return {
        "ocr_text": ocr_text,
        "caption": caption,
        "visual_tags": visual_tags,
        "packaging_type": packaging_type,
        "gift_style": gift_style,
        "scene_hint": scene_hint,
    }


def chat_completion(
    api_key: str,
    model_name: str,
    messages: list[dict[str, Any]],
    timeout: int,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature

    _, response_payload = post_json(
        CHAT_API_URL,
        build_chat_headers(api_key),
        payload,
        timeout,
    )
    return extract_message_text(response_payload)


def is_non_retryable_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return (
        "http 400" in lowered
        or "http 401" in lowered
        or "http 403" in lowered
        or "invalidparameter" in lowered
        or "badrequest.toolarge" in lowered
        or "string value length" in lowered
        or "response_format" in lowered
    )


def run_with_retry(
    operation_name: str,
    image_id: str,
    max_retries: int,
    retry_sleep: float,
    func,
):
    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(), None
        except Exception as exc:
            last_error = safe_text(exc).strip() or "unknown error"
            print(
                f"[WARN] image_id={image_id} operation={operation_name} "
                f"attempt={attempt}/{max_retries} failed: {last_error}"
            )
            if is_non_retryable_error(last_error):
                break
            if attempt < max_retries:
                time.sleep(retry_sleep * attempt)
    return None, last_error


def extract_ocr_text(
    api_key: str,
    model_name: str,
    data_uri: str,
    timeout: int,
) -> str:
    prompt = (
        "请执行 OCR。尽量提取这张商品图片中全部可辨认文字，"
        "只返回纯文本，不要 JSON，不要 Markdown，不要解释。"
        "如果没有可辨认文字，返回空字符串。"
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                    "min_pixels": DEFAULT_MIN_PIXELS,
                    "max_pixels": DEFAULT_MAX_PIXELS,
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]
    return chat_completion(
        api_key=api_key,
        model_name=model_name,
        messages=messages,
        timeout=timeout,
        max_tokens=DEFAULT_OCR_MAX_TOKENS,
        temperature=0.0,
    )


def analyze_single_image(
    api_key: str,
    model_name: str,
    data_uri: str,
    timeout: int,
    ocr_hint: str,
) -> dict[str, Any]:
    prompt_parts = [
        "请分析这张商品图片，并输出一个 JSON 对象。",
        "JSON 字段固定为：ocr_text, caption, visual_tags, packaging_type, gift_style, scene_hint。",
        "要求：",
        "1. ocr_text：尽量识别图片中的文字；如果无文字，返回空字符串。",
        "2. caption：用一句自然语言描述图片主体、包装样式、配色和整体感觉。",
        "3. visual_tags：返回 3 到 8 个简洁中文标签，必须是字符串数组。",
        "4. packaging_type / gift_style / scene_hint：能判断则给出，不能判断返回空字符串。",
        "5. 不要编造看不清的细节，不确定就留空。",
        "6. 只输出 JSON 对象，不要额外解释。",
    ]
    if ocr_hint:
        prompt_parts.append(
            "已识别到的 OCR 候选如下，可能不完整或有误，请结合图片自行修正：\n"
            f"{ocr_hint}"
        )

    messages = [
        {
            "role": "system",
            "content": "你是一个擅长电商产品图像理解与结构化抽取的助手。请严格输出 JSON。",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                    "min_pixels": DEFAULT_MIN_PIXELS,
                    "max_pixels": DEFAULT_MAX_PIXELS,
                },
                {"type": "text", "text": "\n".join(prompt_parts)},
            ],
        },
    ]

    content = chat_completion(
        api_key=api_key,
        model_name=model_name,
        messages=messages,
        timeout=timeout,
        response_format={"type": "json_object"},
        max_tokens=DEFAULT_JSON_MAX_TOKENS,
        temperature=0.1,
    )
    return normalize_analysis_payload(parse_json_object(content))


def build_output_row(
    item: dict[str, Any],
    analysis: dict[str, Any],
    analysis_status: str,
    error: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "image_id": item.get("image_id"),
        "image_path": item.get("image_path"),
        "product_id": item.get("product_id"),
        "source_file": item.get("source_file"),
        "source_sheet": item.get("source_sheet"),
        "source_row": item.get("source_row"),
        "anchor_cell": item.get("anchor_cell"),
        "matched": item.get("matched"),
        "match_status": item.get("match_status"),
        "source_kind": item.get("source_type") or item.get("source_kind") or "excel_image",
        "has_embedding": item.get("has_embedding"),
        "image_url": item.get("image_url"),
        "source_url": item.get("source_url"),
        "source_doc_id": item.get("source_doc_id"),
        "source_sha1": item.get("source_sha1"),
        "ocr_text": analysis.get("ocr_text", ""),
        "caption": analysis.get("caption", ""),
        "visual_tags": analysis.get("visual_tags", []),
        "packaging_type": analysis.get("packaging_type", ""),
        "gift_style": analysis.get("gift_style", ""),
        "scene_hint": analysis.get("scene_hint", ""),
        "analysis_status": analysis_status,
        "warnings": warnings,
        "error": error,
    }


def write_jsonl_record(file_obj, row: dict[str, Any]) -> None:
    file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")


def preview_text(value: str, limit: int = 80) -> str:
    compact = re.sub(r"\s+", " ", safe_text(value)).strip()
    return compact if len(compact) <= limit else f"{compact[:limit]}..."


def build_report(
    rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    elapsed_seconds: float,
    vision_model: str,
    ocr_model: str,
) -> str:
    total_images = len(rows)
    success_rows = [row for row in rows if row.get("analysis_status") == "success"]
    failed_rows = [row for row in rows if row.get("analysis_status") != "success"]
    ocr_empty_count = sum(1 for row in rows if not safe_text(row.get("ocr_text")).strip())
    caption_empty_count = sum(1 for row in rows if not safe_text(row.get("caption")).strip())

    tag_counter = Counter()
    for row in success_rows:
        for tag in row.get("visual_tags") or []:
            cleaned = safe_text(tag).strip()
            if cleaned:
                tag_counter[cleaned] += 1

    sample_rows = success_rows[:10] if success_rows else rows[:10]
    sample_lines = [
        "| # | image_id | product_id | caption | visual_tags | OCR 预览 |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    if sample_rows:
        for index, row in enumerate(sample_rows, start=1):
            sample_lines.append(
                f"| {index} | `{row.get('image_id') or ''}` | `{row.get('product_id') or ''}` | "
                f"{preview_text(safe_text(row.get('caption')))} | "
                f"{', '.join(row.get('visual_tags') or [])} | "
                f"{preview_text(safe_text(row.get('ocr_text')))} |"
            )
    else:
        sample_lines.append("| 1 | - | - | 无 | 无 | 无 |")

    tag_lines = [
        "| 标签 | 次数 |",
        "| --- | ---: |",
    ]
    if tag_counter:
        for tag, count in tag_counter.most_common(20):
            tag_lines.append(f"| `{tag}` | {count} |")
    else:
        tag_lines.append("| 无 | 0 |")

    failure_reason_counter = Counter(
        safe_text(item.get("reason")).strip() or "unknown" for item in failures
    )
    failure_lines = [
        "| 失败原因 | 数量 |",
        "| --- | ---: |",
    ]
    if failure_reason_counter:
        for reason, count in failure_reason_counter.most_common():
            failure_lines.append(f"| `{reason}` | {count} |")
    else:
        failure_lines.append("| 无 | 0 |")

    return f"""# 图片分析执行报告

## 总览

- 图片总数：{total_images}
- 成功分析数：{len(success_rows)}
- 失败数：{len(failed_rows)}
- OCR 为空的图片数量：{ocr_empty_count}
- caption 为空的图片数量：{caption_empty_count}
- 视觉模型：`{vision_model}`
- OCR 模型：`{ocr_model}`
- 总耗时：{elapsed_seconds:.2f} 秒

## 前 10 条分析样例

{chr(10).join(sample_lines)}

## 常见标签统计

{chr(10).join(tag_lines)}

## 失败原因统计

{chr(10).join(failure_lines)}
"""


def run_dry_run(root: Path, items: list[dict[str, Any]], limit: int | None) -> None:
    selected_items = items[:limit] if limit else items
    total = len(selected_items)
    missing_image_path = 0
    image_not_found = 0
    analyzable = 0
    embedded_count = 0

    for item in selected_items:
        if item.get("has_embedding"):
            embedded_count += 1

        image_path = item.get("image_path")
        if not image_path:
            missing_image_path += 1
            continue

        abs_path = resolve_image_path(root, image_path)
        if abs_path is None or not abs_path.exists():
            image_not_found += 1
            continue

        try:
            _, mime_type, image_size_bytes = image_file_to_data_uri(abs_path)
        except Exception:
            image_not_found += 1
            continue

        analyzable += 1
        if analyzable <= 5:
            print(
                "[DRY-RUN]",
                item.get("image_id"),
                item.get("image_path"),
                f"mime={mime_type}",
                f"bytes={image_size_bytes}",
                f"has_embedding={item.get('has_embedding')}",
            )

    print(f"Loaded image items: {len(items)}")
    print(f"Selected image items: {total}")
    print(f"Items with image embeddings: {embedded_count}")
    print(f"Analyzable local images: {analyzable}")
    print(f"Missing image_path: {missing_image_path}")
    print(f"Missing/unreadable files: {image_not_found}")


def run_analysis(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    load_env(root)

    mapping_path = (root / args.input_mapping).resolve()
    embedded_path = (root / args.input_embedded).resolve()
    documents_path = (root / args.documents).resolve()
    output_path = (root / args.output).resolve()
    report_path = (root / args.report).resolve()

    items = merge_input_items(root, mapping_path, embedded_path, documents_path)
    if not items:
        raise FileNotFoundError(
            "未找到可分析的图片输入。请检查 output/image_mapping.json 或 output/images_embedded.jsonl 是否存在。"
        )
    if args.limit:
        items = items[: args.limit]

    print(f"Loaded image items: {len(items)}")
    print(f"Input mapping: {mapping_path}")
    print(f"Input embedded: {embedded_path}")

    if args.dry_run:
        print("Dry-run enabled: validating image inputs only, no API requests will be sent.")
        run_dry_run(root, items, args.limit)
        print(f"Would write JSONL to: {output_path}")
        print(f"Would write report to: {report_path}")
        return

    api_key = ensure_api_key()
    vision_model = args.vision_model or os.getenv("DASHSCOPE_VISION_MODEL", DEFAULT_VISION_MODEL)
    ocr_model = args.ocr_model or os.getenv("DASHSCOPE_OCR_MODEL", DEFAULT_OCR_MODEL)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    start_time = time.time()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    with output_path.open("w", encoding="utf-8") as file_obj:
        for index, item in enumerate(items, start=1):
            print(f"Analyzing image {index}/{len(items)}: {item.get('image_id')}")
            image_path = item.get("image_path")
            warnings: list[str] = []
            fatal_error: str | None = None
            analysis = {
                "ocr_text": "",
                "caption": "",
                "visual_tags": [],
                "packaging_type": "",
                "gift_style": "",
                "scene_hint": "",
            }

            if not image_path:
                fatal_error = safe_text(item.get("mapping_error")).strip() or "映射中没有 image_path"
                failures.append({"image_id": item.get("image_id"), "reason": "missing_image_path"})
                row = build_output_row(item, analysis, "failed", fatal_error, warnings)
                write_jsonl_record(file_obj, row)
                rows.append(row)
                continue

            abs_path = resolve_image_path(root, image_path)
            if abs_path is None or not abs_path.exists():
                fatal_error = f"图片不存在：{abs_path}"
                failures.append({"image_id": item.get("image_id"), "reason": "image_not_found"})
                row = build_output_row(item, analysis, "failed", fatal_error, warnings)
                write_jsonl_record(file_obj, row)
                rows.append(row)
                continue

            try:
                data_uri, _, _ = image_file_to_data_uri(abs_path)
            except Exception as exc:
                fatal_error = safe_text(exc).strip() or "图片读取失败"
                failures.append({"image_id": item.get("image_id"), "reason": "image_read_error"})
                row = build_output_row(item, analysis, "failed", fatal_error, warnings)
                write_jsonl_record(file_obj, row)
                rows.append(row)
                continue

            semantic_result, semantic_error = run_with_retry(
                operation_name="semantic",
                image_id=safe_text(item.get("image_id")),
                max_retries=args.max_retries,
                retry_sleep=args.retry_sleep,
                func=lambda: analyze_single_image(
                    api_key=api_key,
                    model_name=vision_model,
                    data_uri=data_uri,
                    timeout=args.timeout,
                    ocr_hint=analysis["ocr_text"],
                ),
            )
            if semantic_error:
                fatal_error = semantic_error
                failures.append({"image_id": item.get("image_id"), "reason": "semantic_api_error"})
            else:
                analysis.update(semantic_result or {})

            if not semantic_error and not safe_text(analysis.get("ocr_text")).strip():
                ocr_text, ocr_error = run_with_retry(
                    operation_name="ocr",
                    image_id=safe_text(item.get("image_id")),
                    max_retries=args.max_retries,
                    retry_sleep=args.retry_sleep,
                    func=lambda: extract_ocr_text(api_key, ocr_model, data_uri, args.timeout),
                )
                if ocr_error:
                    warnings.append(f"OCR fallback failed: {ocr_error}")
                elif ocr_text:
                    analysis["ocr_text"] = safe_text(ocr_text).strip()

            analysis_status = "success" if analysis["caption"] and analysis["visual_tags"] else "failed"
            if analysis_status != "success" and not fatal_error:
                fatal_error = "模型未返回完整 caption/visual_tags"
                failures.append({"image_id": item.get("image_id"), "reason": "incomplete_analysis"})

            row = build_output_row(item, analysis, analysis_status, fatal_error, warnings)
            write_jsonl_record(file_obj, row)
            rows.append(row)

    elapsed_seconds = time.time() - start_time
    report = build_report(
        rows=rows,
        failures=failures,
        elapsed_seconds=elapsed_seconds,
        vision_model=vision_model,
        ocr_model=ocr_model,
    )
    report_path.write_text(report, encoding="utf-8")

    success_count = sum(1 for row in rows if row.get("analysis_status") == "success")
    print(f"Analysis complete: success={success_count}, failed={len(rows) - success_count}")
    print(f"JSONL output: {output_path}")
    print(f"Report: {report_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze images with DashScope vision models.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument(
        "--input-mapping",
        default="output/image_mapping.json",
        help="Input image mapping JSON path.",
    )
    parser.add_argument(
        "--input-embedded",
        default="output/images_embedded.jsonl",
        help="Input embedded images JSONL path.",
    )
    parser.add_argument(
        "--documents",
        default="output/documents_preview.json",
        help="Optional documents preview JSON for product_id lookup fallback.",
    )
    parser.add_argument(
        "--output",
        default="output/image_analysis.jsonl",
        help="Output image analysis JSONL path.",
    )
    parser.add_argument(
        "--report",
        default="output/image_analysis_report.md",
        help="Output image analysis report path.",
    )
    parser.add_argument(
        "--vision-model",
        default="",
        help=f"Vision model for caption/tags. Default: env DASHSCOPE_VISION_MODEL or {DEFAULT_VISION_MODEL}.",
    )
    parser.add_argument(
        "--ocr-model",
        default="",
        help=f"OCR model for text extraction. Default: env DASHSCOPE_OCR_MODEL or {DEFAULT_OCR_MODEL}.",
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
        help="Retry count per failed API call.",
    )
    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=DEFAULT_RETRY_SLEEP,
        help="Base retry sleep seconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N images. 0 means all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate local structure only, without calling the API.",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.max_retries <= 0:
        raise SystemExit("--max-retries must be positive")
    if args.retry_sleep < 0:
        raise SystemExit("--retry-sleep must be non-negative")
    if args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    return args


if __name__ == "__main__":
    try:
        run_analysis(parse_args())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
