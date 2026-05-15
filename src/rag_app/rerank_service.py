#!/usr/bin/env python3
"""
Rerank product-level retrieval candidates.

This module does not call an LLM and does not modify OpenSearch. RRF is used
only to build the candidate pool; when the rerank model succeeds, final sorting
uses the real rerank score.
"""

from __future__ import annotations

import os
import re
import sys
import json
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import safe_text, truncate
from .image_url_service import enrich_product_images


DEFAULT_MAX_TEXT_CHARS = 1800
DEFAULT_RERANK_MODEL = "gte-rerank-v2"
DEFAULT_RERANK_FINAL_MODE = "rerank_only"
RERANK_TIE_EPSILON = 1e-6
RERANK_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
FALLBACK_MODELS = {"fallback", "rrf", "none"}
ROUTE_IMAGE_TEXT = "image_text"


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact_json(payload: Any, limit: int = 1200) -> str:
    return json.dumps(payload, ensure_ascii=False)[:limit]


def ensure_dashscope_api_key() -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，无法调用百炼 rerank API")
    return api_key


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


def append_unique(parts: list[str], seen: set[str], label: str, value: Any, limit: int) -> None:
    text = normalize_space(safe_text(value))
    if not text:
        return
    key = text.lower()
    if key in seen:
        return
    seen.add(key)
    parts.append(f"{label}：{truncate(text, limit)}")


def build_rerank_text(candidate: dict[str, Any]) -> str:
    """Convert a product-level candidate into compact rerank text."""
    parts: list[str] = []
    seen: set[str] = set()

    append_unique(parts, seen, "产品ID", candidate.get("product_id"), 120)
    routes = candidate.get("routes") or []
    if routes:
        append_unique(parts, seen, "召回来源", "、".join(safe_text(route) for route in routes), 120)

    append_unique(parts, seen, "核心文本", candidate.get("best_text"), 700)

    for hit in (candidate.get("doc_hits") or [])[:3]:
        append_unique(parts, seen, "文本命中", hit.get("source_text"), 420)

    for hit in (candidate.get("image_hits") or [])[:2]:
        append_unique(parts, seen, "图片语义命中", hit.get("source_text"), 420)

    append_unique(parts, seen, "图片引用", candidate.get("best_image_path"), 180)

    text = "\n".join(parts).strip()
    return truncate(text, DEFAULT_MAX_TEXT_CHARS)


def get_rerank_model() -> str:
    return os.getenv("RERANK_MODEL", DEFAULT_RERANK_MODEL).strip() or DEFAULT_RERANK_MODEL


def get_rerank_final_mode() -> str:
    mode = os.getenv("RERANK_FINAL_MODE", DEFAULT_RERANK_FINAL_MODE).strip().lower()
    if mode != DEFAULT_RERANK_FINAL_MODE:
        warn(f"RERANK_FINAL_MODE={mode!r} 暂不支持，使用 {DEFAULT_RERANK_FINAL_MODE}")
        return DEFAULT_RERANK_FINAL_MODE
    return mode


def call_rerank_model(query: str, documents: list[str]) -> list[float]:
    """Call Alibaba Cloud Bailian text rerank API and return scores by input order."""
    if not documents:
        return []

    model_name = get_rerank_model()
    if model_name.lower() in FALLBACK_MODELS:
        raise RuntimeError(f"RERANK_MODEL={model_name} requests fallback mode")

    payload = {
        "model": model_name,
        "input": {
            "query": query,
            "documents": documents,
        },
        "parameters": {
            "return_documents": False,
            "top_n": len(documents),
        },
    }
    headers = {
        "Authorization": f"Bearer {ensure_dashscope_api_key()}",
        "Content-Type": "application/json",
    }
    response_payload = post_json(RERANK_API_URL, headers, payload)

    if response_payload.get("code") or response_payload.get("message") and not response_payload.get("output"):
        raise RuntimeError(
            "百炼 rerank 调用失败："
            + compact_json(response_payload)
            + "。请到百炼控制台确认账号是否支持该 rerank 模型。"
        )

    output = response_payload.get("output") or {}
    results = output.get("results") if isinstance(output, dict) else None
    if not isinstance(results, list):
        raise RuntimeError(
            f"百炼 rerank 响应缺少 output.results：{compact_json(response_payload)}。"
            "请确认 RERANK_MODEL 是否为账号可用的文本排序模型。"
        )

    scores: list[float | None] = [None] * len(documents)
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        score = item.get("relevance_score")
        if isinstance(index, int) and 0 <= index < len(documents):
            try:
                scores[index] = float(score)
            except Exception:
                scores[index] = 0.0

    missing = [index for index, score in enumerate(scores) if score is None]
    if missing:
        raise RuntimeError(f"百炼 rerank 未返回全部候选分数，缺失 index={missing[:10]}")
    return [float(score) for score in scores]


def route_count(candidate: dict[str, Any]) -> int:
    return len(candidate.get("routes") or [])


def has_image_text_hit(candidate: dict[str, Any]) -> bool:
    routes = {safe_text(route).strip() for route in candidate.get("routes") or []}
    if ROUTE_IMAGE_TEXT in routes:
        return True
    for hit in candidate.get("image_hits") or []:
        if safe_text(hit.get("route")).strip() == ROUTE_IMAGE_TEXT:
            return True
    return False


def has_best_image_path(candidate: dict[str, Any]) -> bool:
    return bool(safe_text(candidate.get("best_image_path")).strip())


def sort_rerank_only(
    candidates: list[dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    """Sort by rerank_score, using RRF and route signals only as tie-breakers."""
    if top_n <= 0:
        raise ValueError("top_n 必须大于 0")
    rows = []
    for input_rank, candidate in enumerate(candidates):
        row = dict(candidate)
        row["_input_rank"] = input_rank
        rows.append(row)

    final_mode = get_rerank_final_mode()
    for row in rows:
        rrf_score = float(row.get("rrf_score") or row.get("score") or 0.0)
        rerank_score = float(row.get("rerank_score") or 0.0)
        metadata = dict(row.get("metadata") or {})
        metadata["rerank_final_mode"] = final_mode
        row.update(
            {
                "rrf_score": rrf_score,
                "rerank_score": rerank_score,
                "final_score": rerank_score,
                "metadata": metadata,
            }
        )

    rows.sort(
        key=lambda item: (
            round(float(item.get("final_score") or 0.0) / RERANK_TIE_EPSILON),
            route_count(item),
            1 if has_image_text_hit(item) else 0,
            1 if has_best_image_path(item) else 0,
            float(item.get("rrf_score") or 0.0),
            -int(item.get("_input_rank") or 0),
        ),
        reverse=True,
    )
    for final_rank, row in enumerate(rows[:top_n], start=1):
        row["final_rank"] = final_rank
        row.pop("_input_rank", None)
    return rows[:top_n]


def sort_rrf_fallback(candidates: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Keep the original RRF order when the model is unavailable."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        rrf_score = float(row.get("score") or row.get("rrf_score") or 0.0)
        row["rrf_score"] = rrf_score
        row["rerank_score"] = rrf_score
        row["final_score"] = rrf_score
        rows.append(row)
    for final_rank, row in enumerate(rows[:top_n], start=1):
        row["final_rank"] = final_rank
    return rows[:top_n]


def _fallback_rerank(
    candidates: list[dict[str, Any]],
    top_n: int,
    reason: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    model_name = get_rerank_model()
    for candidate in candidates:
        item = dict(candidate)
        rrf_score = float(item.get("score") or item.get("rrf_score") or 0.0)
        metadata = dict(item.get("metadata") or {})
        metadata["rerank_mode"] = "fallback_rrf"
        metadata["rerank_fallback_reason"] = reason
        metadata["rerank_model"] = model_name
        item.update(
            {
                "rrf_score": rrf_score,
                "rerank_score": rrf_score,
                "rerank_mode": "fallback_rrf",
                "metadata": metadata,
            }
        )
        results.append(item)
    return sort_rrf_fallback(results, top_n)


def _rerank_with_model(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int,
    model_name: str,
) -> list[dict[str, Any]]:
    documents = [build_rerank_text(candidate) for candidate in candidates]
    scores = call_rerank_model(query, documents)
    scored: list[dict[str, Any]] = []
    for candidate, score, document in zip(candidates, scores, documents):
        item = dict(candidate)
        rrf_score = float(item.get("score") or item.get("rrf_score") or 0.0)
        metadata = dict(item.get("metadata") or {})
        metadata["rerank_mode"] = "model"
        metadata["rerank_model"] = model_name
        metadata["rerank_text_preview"] = truncate(document, 300)
        item.update(
            {
                "rrf_score": rrf_score,
                "rerank_score": float(score),
                "rerank_mode": "model",
                "metadata": metadata,
            }
        )
        scored.append(item)

    return sort_rerank_only(scored, top_n)


def rerank_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int = 10,
) -> list[dict[str, Any]]:
    if top_n <= 0:
        raise ValueError("top_n 必须大于 0")
    if not candidates:
        return []

    model_name = get_rerank_model()
    if model_name.lower() in FALLBACK_MODELS:
        return enrich_product_images(
            _fallback_rerank(candidates, top_n, f"RERANK_MODEL={model_name}，使用 RRF 原始排序")
        )

    try:
        return enrich_product_images(_rerank_with_model(query, candidates, top_n, model_name))
    except Exception as exc:
        warn(f"rerank failed, fallback to RRF order: {exc}")
        return enrich_product_images(_fallback_rerank(candidates, top_n, safe_text(exc).strip()))
