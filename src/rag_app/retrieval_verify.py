#!/usr/bin/env python3
"""Read-only helpers for post-push retrieval verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, safe_text, truncate
from .retrieval_service import RetrievalService


def normalize_hit(hit: dict[str, Any], fallback_rank: int = 0) -> dict[str, Any]:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    return {
        "doc_id": safe_text(hit.get("doc_id") or metadata.get("doc_id") or metadata.get("raw_id")).strip(),
        "score": hit.get("score"),
        "rank": hit.get("rank") or fallback_rank,
        "route": safe_text(hit.get("route")).strip(),
        "source_type": safe_text(hit.get("source_type") or metadata.get("source_type")).strip(),
        "product_id": safe_text(hit.get("product_id")).strip(),
        "source_doc_id": safe_text(hit.get("source_doc_id") or metadata.get("source_doc_id")).strip(),
        "source_text": safe_text(hit.get("source_text")).strip(),
        "text_preview": truncate(safe_text(hit.get("source_text")).strip(), 240),
        "metadata": metadata,
    }


def flatten_retrieval_results(payload: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for product in payload.get("results") or []:
        if not isinstance(product, dict):
            continue
        product_id = safe_text(product.get("product_id")).strip()
        for hit in product.get("doc_hits") or []:
            if isinstance(hit, dict):
                row = normalize_hit(hit, len(rows) + 1)
                row["product_id"] = row["product_id"] or product_id
                rows.append(row)
        if not product.get("doc_hits") and product.get("best_text"):
            rows.append(
                {
                    "doc_id": "",
                    "score": product.get("score") or product.get("final_score"),
                    "rank": len(rows) + 1,
                    "route": ",".join(product.get("routes") or []),
                    "source_type": "",
                    "product_id": product_id,
                    "source_doc_id": "",
                    "source_text": safe_text(product.get("best_text")).strip(),
                    "text_preview": truncate(product.get("best_text"), 240),
                    "metadata": product.get("metadata") if isinstance(product.get("metadata"), dict) else {},
                }
            )
    return rows[:top_k]


def search(query: str, top_k: int = 5, root: Path | str = PROJECT_ROOT) -> list[dict[str, Any]]:
    service = RetrievalService(root=root)
    payload = service.retrieve(query, top_k=max(top_k, 10))
    return flatten_retrieval_results(payload, top_k)
