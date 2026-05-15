#!/usr/bin/env python3
"""FastAPI wrapper for the RAG service."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_app.answer_service import answer_query
from rag_app.config import load_env, safe_text, truncate
from rag_app.image_url_service import load_image_url_mapping
from rag_app.rerank_service import rerank_candidates
from rag_app.retrieval_service import RetrievalService

try:
    from rag_app.visual_ingest_api import router as visual_ingest_router
except Exception:
    visual_ingest_router = None


load_env(ROOT)

logging.basicConfig(
    level=os.getenv("RAG_API_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rag_api")

app = FastAPI(title="RAG API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if visual_ingest_router is not None and hasattr(app, "include_router"):
    app.include_router(visual_ingest_router)

REQUIRED_ENV = (
    "DASHSCOPE_API_KEY",
    "OPENSEARCH_ENDPOINT",
    "OPENSEARCH_INSTANCE_ID",
    "OPENSEARCH_USERNAME",
    "OPENSEARCH_PASSWORD",
)


class AnswerRequest(BaseModel):
    query: str = Field(..., description="用户问题")
    top_k: int = Field(5, ge=1, le=10, description="返回产品数量；回答层最多推荐 5 个")
    debug: bool = Field(False, description="是否返回完整 debug")


class SearchRequest(BaseModel):
    query: str = Field(..., description="用户问题")
    top_k: int = Field(10, ge=1, le=30, description="返回候选产品数量")


def validate_query(query: str) -> str:
    normalized = safe_text(query).strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="query 不能为空")
    return normalized


def env_status() -> dict[str, Any]:
    load_env(ROOT)
    missing = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
    return {
        "ready": not missing,
        "missing": missing,
    }


def summarize_hit(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = hit.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "route": safe_text(hit.get("route")).strip(),
        "rank": hit.get("rank"),
        "score": hit.get("score"),
        "product_id": safe_text(hit.get("product_id")).strip(),
        "doc_id": safe_text(hit.get("doc_id")).strip(),
        "image_id": safe_text(hit.get("image_id")).strip(),
        "image_path": safe_text(hit.get("image_path")).strip(),
        "image_url": safe_text(hit.get("image_url")).strip(),
        "source_text": truncate(safe_text(hit.get("source_text")).strip(), 300),
        "metadata": {
            "doc_type": safe_text(metadata.get("doc_type")).strip(),
            "field_name": safe_text(metadata.get("field_name")).strip(),
            "source_kind": safe_text(metadata.get("source_kind")).strip(),
            "source_file": safe_text(metadata.get("source_file")).strip(),
            "source_doc_id": safe_text(metadata.get("source_doc_id")).strip(),
            "source_sha1": safe_text(metadata.get("source_sha1")).strip(),
            "source_url": safe_text(metadata.get("source_url")).strip(),
            "chunk_index": metadata.get("chunk_index"),
            "page_start": metadata.get("page_start"),
            "page_end": metadata.get("page_end"),
            "supplement_title": safe_text(metadata.get("supplement_title")).strip(),
        },
    }


def summarize_search_result(item: dict[str, Any]) -> dict[str, Any]:
    product_bundle = item.get("product_bundle") or {}
    return {
        "product_id": safe_text(item.get("product_id")).strip(),
        "final_score": float(item.get("final_score") or 0.0),
        "rrf_score": float(item.get("rrf_score") or item.get("score") or 0.0),
        "rerank_score": float(item.get("rerank_score") or 0.0),
        "rerank_mode": safe_text(item.get("rerank_mode")).strip(),
        "routes": item.get("routes") or [],
        "best_text": truncate(safe_text(item.get("best_text")).strip(), 700),
        "best_image_path": safe_text(item.get("best_image_path")).strip(),
        "best_image_url": safe_text(item.get("best_image_url")).strip(),
        "best_pdf_hit": item.get("best_pdf_hit") or {},
        "image_source": safe_text(item.get("image_source")).strip(),
        "images": item.get("images") or [],
        "pdf_reports": item.get("pdf_reports") or [],
        "product_bundle": product_bundle,
        "doc_hits": [summarize_hit(hit) for hit in (item.get("doc_hits") or [])[:3] if isinstance(hit, dict)],
        "image_hits": [summarize_hit(hit) for hit in (item.get("image_hits") or [])[:2] if isinstance(hit, dict)],
    }


@app.get("/api/rag/health")
def health() -> dict[str, Any]:
    env = env_status()
    mapping_path = ROOT / "output" / "image_url_mapping.json"
    image_mapping_ready = mapping_path.exists()
    mapping_count = 0
    if image_mapping_ready:
        try:
            mapping_count = len(load_image_url_mapping(ROOT))
        except Exception as exc:
            logger.warning("image mapping check failed: %s", exc)
            image_mapping_ready = False

    return {
        "ok": env["ready"] and image_mapping_ready,
        "service": "rag",
        "retrieval_ready": env["ready"],
        "rerank_ready": bool(os.getenv("DASHSCOPE_API_KEY", "").strip()),
        "answer_ready": bool(os.getenv("DASHSCOPE_API_KEY", "").strip()),
        "image_mapping_ready": image_mapping_ready,
        "image_mapping_count": mapping_count,
        "missing_env": env["missing"],
    }


@app.get("/health")
def simple_health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/rag/answer")
def rag_answer(request: AnswerRequest) -> dict[str, Any]:
    query = validate_query(request.query)
    try:
        payload = answer_query(query)
        products = payload.get("products") or []
        if isinstance(products, list) and request.top_k:
            products = products[: min(request.top_k, 5)]
        response = {
            "ok": True,
            "query": payload.get("query") or query,
            "answer": payload.get("answer") or "",
            "products": products,
            "product_bundles": payload.get("product_bundles") or [
                item.get("product_bundle") for item in products if isinstance(item, dict) and item.get("product_bundle")
            ],
            "debug": payload.get("debug") if request.debug else {},
        }
        if not request.debug and payload.get("debug", {}).get("llm_error"):
            response["message"] = "answer model failed; returned retrieval-based fallback"
        return response
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("answer request failed")
        return {
            "ok": False,
            "error": safe_text(exc).strip() or type(exc).__name__,
            "query": query,
        }


@app.post("/api/rag/search")
def rag_search(request: SearchRequest) -> dict[str, Any]:
    query = validate_query(request.query)
    try:
        service = RetrievalService(root=ROOT)
        retrieval_payload = service.retrieve(query, top_k=30)
        candidates = retrieval_payload.get("results") or []
        reranked = rerank_candidates(query, candidates, top_n=request.top_k)
        return {
            "ok": True,
            "query": query,
            "route_counts": retrieval_payload.get("route_counts") or {},
            "retrieval_count": len(candidates),
            "result_count": len(reranked),
            "products": [summarize_search_result(item) for item in reranked],
            "product_bundles": [item.get("product_bundle") for item in reranked if item.get("product_bundle")],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("search request failed")
        return {
            "ok": False,
            "error": safe_text(exc).strip() or type(exc).__name__,
            "query": query,
        }


@app.post("/api/rag/search-by-image")
def search_by_image() -> dict[str, Any]:
    return {
        "ok": False,
        "message": "search-by-image is not implemented yet",
    }


def main() -> None:
    import uvicorn

    host = os.getenv("RAG_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port_text = os.getenv("RAG_API_PORT", "8000").strip() or "8000"
    try:
        port = int(port_text)
    except ValueError:
        raise RuntimeError(f"RAG_API_PORT 必须是整数，当前值：{port_text}")
    uvicorn.run("rag_api:app", host=host, port=port)


if __name__ == "__main__":
    main()
