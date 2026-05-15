#!/usr/bin/env python3
"""
Check OpenSearch vector score direction for image_vectors.

This script is read-only for OpenSearch. It reads the first local image vector,
queries image_vectors, and writes a Markdown report comparing the raw return
order with local score ascending/descending orders.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
SRC_DIR = ROOT / "src"
for path in (SCRIPTS_DIR, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from push_image_vectors_to_opensearch import (
    VECTOR_DIMENSIONS,
    create_client,
    ensure_runtime_config,
    load_env,
    load_jsonl_records,
    safe_text,
)
from rag_app.retrieval_service import body_to_plain, extract_result_items, flatten_result_item


VECTOR_FIELD = "source_image_vector"
LOGICAL_TABLE = "image_vectors"


def normalize_vector(values: Any, label: str) -> list[float]:
    if not isinstance(values, list):
        raise RuntimeError(f"{label} embedding 不是数组")
    if len(values) != VECTOR_DIMENSIONS:
        raise RuntimeError(f"{label} embedding 维度不正确：期望 {VECTOR_DIMENSIONS}，实际 {len(values)}")
    return [float(value) for value in values]


def load_query_vector(path: Path) -> tuple[dict[str, Any], list[float]]:
    records = load_jsonl_records(path)
    if not records:
        raise RuntimeError(f"{path} 没有记录")
    first = records[0]
    image_id = safe_text(first.get("image_id") or first.get("id")).strip()
    if not image_id:
        raise RuntimeError("第 1 条图片向量缺少 image_id")
    return first, normalize_vector(first.get("embedding"), image_id)


def make_client_and_tables(root: Path) -> tuple[Any, Any, list[str]]:
    load_env(root)
    config = ensure_runtime_config()
    client, models_module = create_client(config)
    return client, models_module, [f"{config['instance_id']}_{LOGICAL_TABLE}", LOGICAL_TABLE]


def parse_rows(response: Any) -> tuple[list[dict[str, Any]], Any]:
    plain = body_to_plain(getattr(response, "body", response))
    rows = [flatten_result_item(item) for item in extract_result_items(response)]
    return rows, plain


def query_image_vectors(
    client: Any,
    models_module: Any,
    table_names: list[str],
    vector: list[float],
    top_k: int,
    order: str | None,
) -> tuple[list[dict[str, Any]], Any, str, str]:
    last_error: Exception | None = None
    for table_name in table_names:
        for index_name in (VECTOR_FIELD, None):
            try:
                request = models_module.QueryRequest(
                    table_name=table_name,
                    index_name=index_name,
                    vector=vector,
                    top_k=top_k,
                    include_vector=False,
                    output_fields=["id", "image_id", "product_id", "image_path"],
                    order=order,
                )
                response = client.query(request)
                rows, plain = parse_rows(response)
                return rows, plain, table_name, index_name or "<default>"
            except Exception as exc:
                last_error = exc
    raise RuntimeError(str(last_error) if last_error else "image_vectors 查询失败")


def score_value(row: dict[str, Any]) -> float:
    try:
        return float(row.get("score"))
    except Exception:
        return float("nan")


def row_image_id(row: dict[str, Any]) -> str:
    return safe_text(row.get("image_id") or row.get("id")).strip()


def rank_of(rows: list[dict[str, Any]], image_id: str) -> int | None:
    for index, row in enumerate(rows, start=1):
        if row_image_id(row) == image_id:
            return index
    return None


def format_score(value: Any) -> str:
    try:
        return f"{float(value):.6f}"
    except Exception:
        return safe_text(value)


def rows_table(rows: list[dict[str, Any]], query_image_id: str) -> list[str]:
    lines = [
        "| rank | image_id | product_id | image_path | score | is_query |",
        "| ---: | --- | --- | --- | ---: | --- |",
    ]
    if not rows:
        lines.append("| 1 | 无 |  |  |  |  |")
        return lines
    for index, row in enumerate(rows, start=1):
        image_id = row_image_id(row)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    f"`{image_id}`",
                    f"`{safe_text(row.get('product_id'))}`",
                    f"`{safe_text(row.get('image_path'))}`",
                    format_score(row.get("score")),
                    "YES" if image_id == query_image_id else "",
                ]
            )
            + " |"
        )
    return lines


def summarize_direction(raw_rows: list[dict[str, Any]], query_image_id: str) -> dict[str, Any]:
    scores = [score_value(row) for row in raw_rows]
    is_ascending = all(scores[index] <= scores[index + 1] for index in range(len(scores) - 1))
    is_descending = all(scores[index] >= scores[index + 1] for index in range(len(scores) - 1))
    query_rank = rank_of(raw_rows, query_image_id)
    max_rank = rank_of(sorted(raw_rows, key=score_value, reverse=True), query_image_id)
    min_rank = rank_of(sorted(raw_rows, key=score_value), query_image_id)
    return {
        "is_ascending": is_ascending,
        "is_descending": is_descending,
        "query_rank": query_rank,
        "query_rank_score_desc": max_rank,
        "query_rank_score_asc": min_rank,
    }


def build_report(
    query_record: dict[str, Any],
    raw_rows: list[dict[str, Any]],
    raw_body: Any,
    table_name: str,
    index_name: str,
    desc_rows: list[dict[str, Any]],
    asc_rows: list[dict[str, Any]],
    explicit_desc_rows: list[dict[str, Any]] | None,
    explicit_asc_rows: list[dict[str, Any]] | None,
    explicit_desc_error: str,
    explicit_asc_error: str,
    elapsed_seconds: float,
) -> str:
    query_image_id = safe_text(query_record.get("image_id")).strip()
    summary = summarize_direction(raw_rows, query_image_id)
    raw_direction = "升序" if summary["is_ascending"] else "降序" if summary["is_descending"] else "非严格单调"

    score_meaning = "越大越相似"
    needs_query_order = summary["query_rank"] != summary["query_rank_score_desc"]
    conclusion = (
        "需要在向量查询中显式设置 `order='DESC'`，或在业务层按 score 降序理解/排序；"
        "RRF 阶段更稳妥的做法是使用已修正后的相似度降序 rank。"
        if needs_query_order
        else "原始 rank 已符合 score 降序，可以直接用于 RRF。"
    )

    lines = [
        "# OpenSearch score 方向检查报告",
        "",
        "## 总览",
        "",
        f"- 查询表：`{table_name}`",
        f"- 查询索引：`{index_name}`",
        f"- 查询向量来源 image_id：`{query_image_id}`",
        f"- 查询向量维度：{len(query_record.get('embedding') or [])}",
        "- 当前 `test_opensearch_retrieval.py` 对 OpenSearch 返回结果：没有手动按 score 排序；只按原始返回顺序输出。",
        f"- OpenSearch 原始返回顺序：score {raw_direction}",
        f"- query image_id 在原始返回中的排名：{summary['query_rank'] or '未命中'}",
        f"- query image_id 在 score 降序中的排名：{summary['query_rank_score_desc'] or '未命中'}",
        f"- query image_id 在 score 升序中的排名：{summary['query_rank_score_asc'] or '未命中'}",
        f"- InnerProduct / Cosine 类相似度 score 结论：{score_meaning}",
        f"- 是否疑似脚本排序方向写反：不是脚本手动排序写反，而是查询未显式指定 `order='DESC'` 时 OpenSearch 默认按 ASC 返回。",
        f"- 是否需要明确指定排序方式：{'需要' if needs_query_order else '不需要'}",
        f"- image_vectors 查询/排序逻辑是否需要修正：{'需要，将 QueryRequest.order 设置为 DESC' if needs_query_order else '暂不需要'}",
        f"- RRF 建议：{conclusion}",
        f"- 总耗时：{elapsed_seconds:.2f} 秒",
        "",
        "## OpenSearch 原始返回顺序",
        "",
        *rows_table(raw_rows, query_image_id),
        "",
        "## 本地按 score 升序对比",
        "",
        *rows_table(asc_rows, query_image_id),
        "",
        "## 本地按 score 降序对比",
        "",
        *rows_table(desc_rows, query_image_id),
        "",
        "## 显式 order=DESC 查询结果",
        "",
    ]
    if explicit_desc_rows is not None:
        lines.extend(rows_table(explicit_desc_rows, query_image_id))
    else:
        lines.append(f"- 查询失败：{explicit_desc_error}")
    lines.extend(["", "## 显式 order=ASC 查询结果", ""])
    if explicit_asc_rows is not None:
        lines.extend(rows_table(explicit_asc_rows, query_image_id))
    else:
        lines.append(f"- 查询失败：{explicit_asc_error}")

    lines.extend(
        [
            "",
            "## 原始响应摘要",
            "",
            "```json",
            json.dumps(raw_body, ensure_ascii=False, indent=2)[:4000],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check OpenSearch score direction for image_vectors.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--input", default="output/images_embedded.jsonl", help="Local image embedding JSONL.")
    parser.add_argument("--report", default="output/opensearch_score_direction_check.md", help="Report path.")
    parser.add_argument("--top-k", type=int, default=10, help="TopK for score direction check.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    input_path = (root / args.input).resolve()
    report_path = (root / args.report).resolve()

    if args.top_k <= 0:
        raise ValueError("--top-k 必须大于 0")

    started = time.time()
    query_record, query_vector = load_query_vector(input_path)
    client, models_module, table_names = make_client_and_tables(root)

    raw_rows, raw_body, table_name, index_name = query_image_vectors(
        client=client,
        models_module=models_module,
        table_names=table_names,
        vector=query_vector,
        top_k=args.top_k,
        order=None,
    )
    asc_rows = sorted(raw_rows, key=score_value)
    desc_rows = sorted(raw_rows, key=score_value, reverse=True)

    explicit_desc_rows = None
    explicit_asc_rows = None
    explicit_desc_error = ""
    explicit_asc_error = ""
    try:
        explicit_desc_rows, _, _, _ = query_image_vectors(
            client=client,
            models_module=models_module,
            table_names=[table_name],
            vector=query_vector,
            top_k=args.top_k,
            order="DESC",
        )
    except Exception as exc:
        explicit_desc_error = safe_text(exc).strip()

    try:
        explicit_asc_rows, _, _, _ = query_image_vectors(
            client=client,
            models_module=models_module,
            table_names=[table_name],
            vector=query_vector,
            top_k=args.top_k,
            order="ASC",
        )
    except Exception as exc:
        explicit_asc_error = safe_text(exc).strip()

    report_content = build_report(
        query_record=query_record,
        raw_rows=raw_rows,
        raw_body=raw_body,
        table_name=table_name,
        index_name=index_name,
        desc_rows=desc_rows,
        asc_rows=asc_rows,
        explicit_desc_rows=explicit_desc_rows,
        explicit_asc_rows=explicit_asc_rows,
        explicit_desc_error=explicit_desc_error,
        explicit_asc_error=explicit_asc_error,
        elapsed_seconds=time.time() - started,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_content, encoding="utf-8")

    query_image_id = safe_text(query_record.get("image_id")).strip()
    print(f"Query image_id: {query_image_id}")
    print(f"Raw rank: {rank_of(raw_rows, query_image_id)}")
    print(f"Score-desc rank: {rank_of(desc_rows, query_image_id)}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
