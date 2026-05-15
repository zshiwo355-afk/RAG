#!/usr/bin/env python3
"""
RAG answer layer: retrieve -> rerank -> context -> LLM answer.

This module does not modify OpenSearch, OSS, embeddings, or output data files.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import PROJECT_ROOT as ROOT, load_env, safe_text, truncate
from .rerank_service import DEFAULT_RERANK_MODEL, get_rerank_model, rerank_candidates
from .retrieval_service import RetrievalService


CHAT_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_ANSWER_MODEL = "qwen3.6-plus"
DEFAULT_CANDIDATE_TOP_K = 30
DEFAULT_RERANK_TOP_N = 10
DEFAULT_FINAL_TOP_N = 5
MAX_PRODUCT_CONTEXT_CHARS = 1200
MAX_BUNDLE_SECTION_CHARS = 360


SYSTEM_PROMPT = """你是白酒产品知识库销售助手。

必须遵守：
1. 只能基于给定产品资料回答。
2. 不知道就说资料里没有明确信息。
3. 不要编造价格、库存、销量、官方排名。
4. 推荐产品时必须说明推荐理由。
5. 最多推荐 5 个产品。
6. 如果产品有图片 URL，要在结构化结果里返回。
7. 回答要适合销售人员直接使用。
8. 不能把不同产品的信息混在一起。
9. 如果用户问包装、颜色、礼盒、节庆风格，要优先参考图片语义信息。
10. 如果资料存在新旧差异，要在回答中提示“资料存在新旧差异”，并优先保留质检报告信息。

请只输出一个 JSON object，不要输出 Markdown。JSON 格式：
{
  "answer": "自然语言回答",
  "products": [
    {
      "product_id": "...",
      "product_name": "...",
      "reason": "推荐理由",
      "best_image_url": "..."
    }
  ]
}
"""


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def compact_json(payload: Any, limit: int = 1200) -> str:
    return json.dumps(payload, ensure_ascii=False)[:limit]


def ensure_dashscope_api_key() -> str:
    load_env(ROOT)
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，无法调用回答模型")
    return api_key


def get_answer_model() -> str:
    load_env(ROOT)
    return os.getenv("ANSWER_MODEL", DEFAULT_ANSWER_MODEL).strip() or DEFAULT_ANSWER_MODEL


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


def extract_product_name(text: str) -> str:
    text = safe_text(text)
    patterns = [
        r"(?:产品名称|商品名称|酒名|名称|品名)[:：]\s*([^。；;\n，,]{2,80})",
        r"(?:品牌)[:：]\s*([^。；;\n，,]{2,60})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return truncate(first_line, 60)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", safe_text(text)).strip()


def add_unique_text(parts: list[str], seen: set[str], label: str, value: Any, limit: int) -> None:
    text = normalize_space(safe_text(value))
    if not text:
        return
    key = text.lower()
    if key in seen:
        return
    seen.add(key)
    parts.append(f"{label}：{truncate(text, limit)}")


def bundle_items_text(items: Any, limit: int = MAX_BUNDLE_SECTION_CHARS) -> str:
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        text = normalize_space(item.get("text_preview") or item.get("source_text"))
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        parts.append(truncate(text, limit))
    return "；".join(parts)


def summarize_bundle(bundle: Any) -> dict[str, str]:
    if not isinstance(bundle, dict):
        return {}
    return {
        "basic_info": bundle_items_text(bundle.get("basic_info")),
        "packaging": bundle_items_text(bundle.get("packaging")),
        "selling_points": bundle_items_text(bundle.get("selling_points")),
        "gift_attributes": bundle_items_text(bundle.get("gift_attributes")),
        "quality_reports": bundle_items_text(bundle.get("quality_reports")),
        "image_texts": bundle_items_text(bundle.get("image_texts")),
        "supplements": bundle_items_text(bundle.get("supplements")),
        "conflicts": "; ".join(
            f"{safe_text(item.get('field')).strip()}：旧={truncate(normalize_space(item.get('old_value')), 80)}，补充={truncate(normalize_space(item.get('supplement_value')), 80)}"
            for item in (bundle.get("conflicts") or [])[:3]
            if isinstance(item, dict)
        ),
    }


def source_type_for_route(route: str) -> str:
    return "image_text" if route == "image_text" else "text"


def build_sources(product: dict[str, Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for hit in (product.get("doc_hits") or [])[:2]:
        route = safe_text(hit.get("route")).strip()
        content = normalize_space(hit.get("source_text"))
        if not content:
            continue
        key = (route, content[:120])
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "type": source_type_for_route(route),
                "route": route,
                "content": truncate(content, 500),
                "source_kind": safe_text((hit.get("metadata") or {}).get("source_kind")).strip(),
                "source_file": safe_text((hit.get("metadata") or {}).get("source_file")).strip(),
                "source_doc_id": safe_text((hit.get("metadata") or {}).get("source_doc_id")).strip(),
                "source_url": safe_text((hit.get("metadata") or {}).get("source_url")).strip(),
                "image_url": safe_text(product.get("best_image_url")).strip()
                if route == "image_text"
                else "",
            }
        )

    for hit in (product.get("image_hits") or [])[:1]:
        route = safe_text(hit.get("route")).strip() or "image_text"
        content = normalize_space(hit.get("source_text"))
        if not content:
            continue
        key = (route, content[:120])
        if key in seen:
            continue
        sources.append(
            {
                "type": "image_text",
                "route": route,
                "content": truncate(content, 500),
                "source_kind": safe_text((hit.get("metadata") or {}).get("source_kind")).strip(),
                "source_file": safe_text((hit.get("metadata") or {}).get("source_file")).strip(),
                "source_doc_id": safe_text((hit.get("metadata") or {}).get("source_doc_id")).strip(),
                "source_url": safe_text((hit.get("metadata") or {}).get("source_url")).strip(),
                "image_url": safe_text(product.get("best_image_url")).strip(),
            }
        )
    return sources


def build_product_context(product: dict[str, Any], index: int) -> dict[str, Any]:
    best_text = safe_text(product.get("best_text")).strip()
    product_name = extract_product_name(best_text)
    sources = build_sources(product)
    bundle = product.get("product_bundle") or {}
    bundle_summary = summarize_bundle(bundle)
    parts: list[str] = []
    seen: set[str] = set()
    parts.append(f"[产品 {index}]")
    parts.append(f"product_id：{safe_text(product.get('product_id')).strip()}")
    if product_name:
        parts.append(f"产品名称：{product_name}")
    parts.append(f"routes：{', '.join(product.get('routes') or [])}")
    parts.append(f"rerank_score：{float(product.get('rerank_score') or product.get('final_score') or 0):.6f}")
    if product.get("best_image_url"):
        parts.append(f"图片URL：{safe_text(product.get('best_image_url')).strip()}")
    if bundle_summary.get("conflicts"):
        parts.append("资料存在新旧差异。")
        add_unique_text(parts, seen, "冲突摘要", bundle_summary.get("conflicts"), 260)
    add_unique_text(parts, seen, "产品完整资料包-基础资料", bundle_summary.get("basic_info"), 420)
    add_unique_text(parts, seen, "产品完整资料包-包装/介绍/卖点", "；".join(filter(None, [bundle_summary.get("packaging"), bundle_summary.get("selling_points")])), 520)
    add_unique_text(parts, seen, "产品完整资料包-礼品属性", bundle_summary.get("gift_attributes"), 300)
    add_unique_text(parts, seen, "产品完整资料包-质检报告", bundle_summary.get("quality_reports"), 420)
    add_unique_text(parts, seen, "产品完整资料包-补充资料", bundle_summary.get("supplements"), 420)
    add_unique_text(parts, seen, "产品完整资料包-图片语义", bundle_summary.get("image_texts"), 320)
    add_unique_text(parts, seen, "核心资料", best_text, 520)
    for source_index, source in enumerate(sources, start=1):
        label = "图片语义来源" if source.get("type") == "image_text" else "文本来源"
        add_unique_text(parts, seen, f"{label}{source_index}", source.get("content"), 360)
    context_text = truncate("\n".join(parts), MAX_PRODUCT_CONTEXT_CHARS)
    return {
        "product_id": safe_text(product.get("product_id")).strip(),
        "product_name": product_name,
        "context_text": context_text,
        "sources": sources,
        "bundle_summary": bundle_summary,
    }


def build_answer_context(query: str, reranked_results: list[dict[str, Any]]) -> dict[str, Any]:
    top_products = reranked_results[:DEFAULT_FINAL_TOP_N]
    product_contexts = [build_product_context(product, index) for index, product in enumerate(top_products, start=1)]
    context_text = "\n\n".join(item["context_text"] for item in product_contexts)
    user_prompt = (
        f"用户问题：{query}\n\n"
        "以下是检索和精排后的产品资料，只能基于这些资料回答。\n\n"
        f"{context_text}\n\n"
        "请根据资料给出销售人员可直接使用的推荐回答。"
    )
    return {
        "query": query,
        "context_text": context_text,
        "user_prompt": user_prompt,
        "product_contexts": product_contexts,
    }


def extract_json_object(text: str) -> dict[str, Any]:
    text = safe_text(text).strip()
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def call_answer_llm(context: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    model_name = get_answer_model()
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context["user_prompt"]},
        ],
        "temperature": 0.2,
        "top_p": 0.8,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {ensure_dashscope_api_key()}",
        "Content-Type": "application/json",
    }
    response_payload = post_json(CHAT_API_URL, headers, payload, timeout=timeout)
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"回答模型响应缺少 choices：{compact_json(response_payload)}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = safe_text((message or {}).get("content")).strip()
    parsed = extract_json_object(content)
    if not parsed:
        raise RuntimeError(f"回答模型未返回合法 JSON：{truncate(content, 500)}")
    return parsed


def fallback_answer(query: str, products: list[dict[str, Any]], error: str | None = None) -> str:
    if not products:
        return "资料库中没有找到足够相关的产品。"
    lines = ["根据当前资料，优先推荐以下产品："]
    for index, product in enumerate(products, start=1):
        name = extract_product_name(safe_text(product.get("best_text"))) or safe_text(product.get("product_id"))
        bundle_summary = summarize_bundle(product.get("product_bundle"))
        reason_source = bundle_summary.get("supplements") or bundle_summary.get("quality_reports") or product.get("best_text")
        reason = truncate(normalize_space(reason_source), 120)
        lines.append(f"{index}. {name}：{reason}")
        if (product.get("product_bundle") or {}).get("conflicts"):
            lines.append("   资料存在新旧差异，请以产品原始资料和质检报告为准核对关键字段。")
    if error:
        lines.append(f"说明：回答模型调用失败，以上为检索结果摘要。错误：{error}")
    return "\n".join(lines)


def build_structured_products(
    llm_payload: dict[str, Any],
    top_products: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = {safe_text(product.get("product_id")).strip(): product for product in top_products}
    context_by_id = {item["product_id"]: item for item in context["product_contexts"]}
    llm_products = llm_payload.get("products")
    reasons: dict[str, str] = {}
    names: dict[str, str] = {}
    if isinstance(llm_products, list):
        for item in llm_products:
            if not isinstance(item, dict):
                continue
            product_id = safe_text(item.get("product_id")).strip()
            if product_id in by_id:
                reasons[product_id] = safe_text(item.get("reason")).strip()
                names[product_id] = safe_text(item.get("product_name")).strip()

    structured: list[dict[str, Any]] = []
    for product in top_products:
        product_id = safe_text(product.get("product_id")).strip()
        product_context = context_by_id.get(product_id) or {}
        product_name = names.get(product_id) or product_context.get("product_name") or extract_product_name(
            safe_text(product.get("best_text"))
        )
        reason = reasons.get(product_id) or truncate(normalize_space(product.get("best_text")), 180)
        structured.append(
            {
                "product_id": product_id,
                "product_name": product_name,
                "reason": reason,
                "best_image_url": safe_text(product.get("best_image_url")).strip(),
                "best_image_path": safe_text(product.get("best_image_path")).strip(),
                "best_pdf_hit": product.get("best_pdf_hit") or {},
                "images": product.get("images") or [],
                "pdf_reports": product.get("pdf_reports") or [],
                "score": float(product.get("final_score") or product.get("rerank_score") or 0.0),
                "routes": product.get("routes") or [],
                "sources": product_context.get("sources") or build_sources(product),
                "product_bundle": product.get("product_bundle") or {},
            }
        )
    return structured


def answer_query(query: str) -> dict[str, Any]:
    started = time.time()
    query = safe_text(query).strip()
    answer_model = get_answer_model()
    if not query:
        return {
            "query": query,
            "answer": "请提供要查询的问题。",
            "products": [],
            "debug": {
                "retrieval_count": 0,
                "rerank_count": 0,
                "final_product_count": 0,
                "answer_model": answer_model,
                "rerank_model": get_rerank_model(),
                "fallback_used": False,
                "elapsed_seconds": 0.0,
                "error": "empty_query",
            },
        }

    retrieval_count = 0
    rerank_count = 0
    fallback_used = False
    try:
        service = RetrievalService(root=ROOT)
        retrieval_payload = service.retrieve(query, top_k=DEFAULT_CANDIDATE_TOP_K)
        candidates = retrieval_payload.get("results") or []
        retrieval_count = len(candidates)
    except Exception as exc:
        return {
            "query": query,
            "answer": f"检索失败：{safe_text(exc).strip()}",
            "products": [],
            "debug": {
                "retrieval_count": 0,
                "rerank_count": 0,
                "final_product_count": 0,
                "answer_model": answer_model,
                "rerank_model": get_rerank_model(),
                "fallback_used": False,
                "elapsed_seconds": time.time() - started,
                "error": safe_text(exc).strip(),
            },
        }

    if not candidates:
        return {
            "query": query,
            "answer": "资料库中没有找到足够相关的产品。",
            "products": [],
            "debug": {
                "retrieval_count": 0,
                "rerank_count": 0,
                "final_product_count": 0,
                "answer_model": answer_model,
                "rerank_model": get_rerank_model(),
                "fallback_used": False,
                "elapsed_seconds": time.time() - started,
            },
        }

    reranked = rerank_candidates(query, candidates, top_n=DEFAULT_RERANK_TOP_N)
    rerank_count = len(reranked)
    fallback_used = any((item.get("rerank_mode") or "") == "fallback_rrf" for item in reranked)
    top_products = reranked[:DEFAULT_FINAL_TOP_N]
    context = build_answer_context(query, top_products)

    llm_error = ""
    llm_payload: dict[str, Any] = {}
    try:
        llm_payload = call_answer_llm(context)
        answer = safe_text(llm_payload.get("answer")).strip()
        if not answer:
            answer = fallback_answer(query, top_products)
    except Exception as exc:
        llm_error = safe_text(exc).strip()
        warn(f"answer LLM failed: {llm_error}")
        answer = fallback_answer(query, top_products, llm_error)

    return {
        "query": query,
        "answer": answer,
        "products": build_structured_products(llm_payload, top_products, context),
        "product_bundles": [product.get("product_bundle") for product in top_products if product.get("product_bundle")],
        "debug": {
            "retrieval_count": retrieval_count,
            "rerank_count": rerank_count,
            "final_product_count": len(top_products),
            "answer_model": answer_model,
            "rerank_model": get_rerank_model() or DEFAULT_RERANK_MODEL,
            "fallback_used": fallback_used,
            "elapsed_seconds": time.time() - started,
            "llm_error": llm_error,
        },
    }


if __name__ == "__main__":
    payload = answer_query("红色礼盒春节送礼推荐哪些酒")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
