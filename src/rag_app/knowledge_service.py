"""Company knowledge lifecycle and retrieval, independent of product pipelines."""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from . import rerank_service
from .config import load_env
from .knowledge_index import KnowledgeIndex, chunk_document
from .knowledge_store import AutomaticPublicationConflict, KnowledgeStore, normalize_entry


PUBLIC_FIELDS = (
    "knowledge_id", "revision", "title", "content", "contributor", "kind",
    "sources", "evidence", "content_hash", "payload_hash", "created_at",
    "published_at", "confirmed_by", "status", "department", "scenarios", "chunking_version",
)
logger = logging.getLogger(__name__)


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Expose only reviewed content, never storage paths or draft counters."""
    return {key: record[key] for key in PUBLIC_FIELDS if key in record}


class KnowledgeService:
    def __init__(self, store: Optional[KnowledgeStore] = None, index: Optional[KnowledgeIndex] = None):
        if store is None:
            load_env()
        self.store = store if store is not None else KnowledgeStore()
        self.index = index if index is not None else KnowledgeIndex()

    def import_document(
        self, entry: dict[str, Any], execute: bool = False, *, allow_new_revision: bool = True,
    ) -> dict[str, Any]:
        normalized = normalize_entry(entry)
        if not execute:
            return {
                "status": "dry_run", "knowledge_id": normalized["knowledge_id"],
                "title": normalized["title"], "content_length": len(normalized["content"]),
                "source_count": len(normalized["sources"]),
                "content_hash": normalized["content_hash"],
                "will_call_embedding": False, "will_write_index": False,
            }
        return self.store.import_draft(entry, allow_new_revision=allow_new_revision)

    def index_draft(
        self, knowledge_id: str, revision: Optional[int] = None, execute: bool = False,
    ) -> dict[str, Any]:
        """Prepare an imported draft's vectors without approving or publishing it."""
        snapshot = self.store.snapshot(knowledge_id, revision)
        if snapshot["status"] != "draft":
            raise ValueError("only an unpublished draft can be prepared with index-draft")
        result = {
            "knowledge_id": knowledge_id, "revision": snapshot["revision"],
            "published": False, "will_publish": False,
        }
        if not execute:
            return {
                **result, "status": "dry_run", "chunk_count": len(chunk_document(snapshot)),
                "will_call_embedding": False, "will_write_index": False,
                "on_execute": "向量化并验证草稿索引，保持未发布",
            }
        verification = self.index.index_and_verify(snapshot)
        current = self.store.snapshot(knowledge_id, snapshot["revision"])
        if current["generation"] != snapshot["generation"] or current["status"] != "draft":
            # Concurrent changes do not promote staged chunks. The published
            # revision allowlist still decides which immutable revision is public.
            raise RuntimeError("knowledge changed during draft indexing; inspect the current revision")
        return {**result, "status": "indexed_draft", "verification": verification}

    def publish(
        self, knowledge_id: str, revision: Optional[int] = None,
        confirmed_by: str = "", execute: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(confirmed_by, str) or not confirmed_by.strip() or len(confirmed_by) > 200:
            raise ValueError("发布需要提供有效 confirmed_by（不超过 200 字符）")
        snapshot = self.store.snapshot(knowledge_id, revision)
        if not execute:
            return {
                "status": "dry_run", "knowledge_id": knowledge_id, "revision": snapshot["revision"],
                "chunk_count": len(chunk_document(snapshot)), "confirmed_by": confirmed_by.strip(),
                "will_call_embedding": False, "will_write_index": False,
                "on_execute": "写入知识索引并验证后切换正式版本",
            }
        # Cloud writes precede the transaction; a concurrent withdrawal/import
        # invalidates this snapshot, leaving any new cloud chunks unpublished.
        verification = self.index.index_and_verify(snapshot)
        published = self.store.publish(
            knowledge_id, snapshot["revision"], expected_generation=snapshot["generation"],
            confirmed_by=confirmed_by.strip(),
        )
        return {"status": "published", "knowledge": public_record(published), "verification": verification}

    def publish_automatic(
        self, knowledge_id: str, revision: int, *, expected_generation: int, confirmed_by: str,
        transaction_guard=None, transaction_complete=None,
    ) -> dict[str, Any]:
        """Execute a pinned processing decision; this path cannot roll back versions.

        transaction_guard(connection) validates the lease after the knowledge lock.
        transaction_complete(connection, published, verification) persists the
        processing outcome atomically with the publication pointer.
        """
        if not isinstance(confirmed_by, str) or not confirmed_by.strip() or len(confirmed_by) > 200:
            raise ValueError("发布需要提供有效 confirmed_by（不超过 200 字符）")
        if type(expected_generation) is not int or expected_generation < 1:
            raise ValueError("expected_generation must be a positive integer")
        if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
            raise ValueError("revision must be a positive int64")
        if any(callback is not None and not callable(callback)
               for callback in (transaction_guard, transaction_complete)):
            raise ValueError("publication transaction callbacks must be callable")
        snapshot = self.store.snapshot(knowledge_id, revision)
        if (snapshot["status"] == "published" and snapshot["generation"] != expected_generation + 1
                or snapshot["status"] != "published" and (
                    snapshot["status"] != "draft" or snapshot["generation"] != expected_generation)):
            raise AutomaticPublicationConflict("knowledge changed before automatic publication")
        verification = self.index.index_and_verify(snapshot, require_keyword=True)
        published = self.store.publish_automatic(
            knowledge_id, revision, expected_generation=expected_generation, confirmed_by=confirmed_by.strip(),
            transaction_guard=transaction_guard,
            transaction_complete=(lambda connection, record: transaction_complete(connection, record, verification))
            if transaction_complete is not None else None,
        )
        return {"status": "published", "knowledge": public_record(published), "verification": verification}

    def withdraw(self, knowledge_id: str, execute: bool = False) -> dict[str, Any]:
        snapshot = self.store.snapshot(knowledge_id)
        if not execute:
            return {"status": "dry_run", "knowledge_id": knowledge_id, "will_write_index": False}
        # Retrieval filters by the local published catalogue. No cloud delete
        # is needed to make withdrawn records inaccessible through this API.
        return self.store.withdraw(snapshot["knowledge_id"])

    def get(self, knowledge_id: str, revision: Optional[int] = None) -> Optional[dict[str, Any]]:
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**63 - 1):
            raise ValueError("revision must be a positive int64")
        record = self.store.get_published(knowledge_id)
        if record is None or (revision is not None and record["revision"] != revision):
            return None
        # Loading a body from object storage can outlive its publication. Recheck
        # the catalogue without downloading it again; never substitute a new body.
        if not any(item["knowledge_id"] == knowledge_id and item["revision"] == record["revision"]
                   for item in self.store.published_revisions()):
            return None
        return public_record(record)

    def list_assets(self) -> list[dict[str, Any]]:
        return self.store.list_assets()

    def export(self, knowledge_id: str, revision: Optional[int] = None) -> dict[str, Any]:
        record = self.store.snapshot(knowledge_id, revision)
        return public_record(record)

    def search(
        self, query: str, top_k: int = 10, *, include_content: bool = True,
        department: Optional[str] = None, scenario: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("query 必须为 1–4000 字符的非空文本")
        if type(top_k) is not int or not 1 <= top_k <= 30:
            raise ValueError("top_k 必须是 1–30 的整数")
        if type(include_content) is not bool:
            raise ValueError("include_content 必须为布尔值")
        filters = {"department": department, "scenario": scenario}
        for name, value in filters.items():
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 200:
                    raise ValueError(f"{name} 必须为 1–200 字符的非空文本")
                filters[name] = value.strip()
        allowed = self.store.published_revisions(**filters)
        if not allowed:
            return []
        allowed_pairs = {(item["knowledge_id"], item["revision"]) for item in allowed}
        rows = self.index.search(query.strip(), allowed, min(200, max(50, top_k * 5)))
        if not isinstance(rows, list):
            raise RuntimeError("知识检索响应无效")
        # OSS body reads dominate latency. Bound parallelism; all visibility and
        # content checks below still run, including checks after model calls.
        ids = list(dict.fromkeys(
            row["knowledge_id"] for row in rows if isinstance(row, dict)
            and isinstance(row.get("knowledge_id"), str) and type(row.get("revision")) is int
            and (row["knowledge_id"], row["revision"]) in allowed_pairs
        ))
        with ThreadPoolExecutor(max_workers=4) as pool:
            records: dict[str, Any] = dict(zip(ids, pool.map(self.store.get_published, ids)))
        chunks: dict[str, Any] = {}
        matches: dict[str, dict[str, Any]] = {}
        for position, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            knowledge_id = row.get("knowledge_id")
            try:
                revision = row.get("revision")
                if isinstance(revision, str) and revision.isdecimal():
                    revision = int(revision)
                score = float(row.get("score"))
            except (ValueError, TypeError, OverflowError):
                continue
            if (not isinstance(knowledge_id, str) or type(revision) is not int
                    or (knowledge_id, revision) not in allowed_pairs or not math.isfinite(score)):
                continue
            route_ranks = row.get("route_ranks", {"text_dense": position})
            if not isinstance(route_ranks, dict):
                continue
            route_ranks = {route: rank for route, rank in route_ranks.items()
                           if route in {"text_dense", "text_keyword"} and type(rank) in (int, float)
                           and 0 < rank <= 2**53 and math.isfinite(rank)}
            if not route_ranks:
                continue
            if knowledge_id not in records:
                records[knowledge_id] = self.store.get_published(knowledge_id)
            record = records[knowledge_id]
            if knowledge_id not in chunks:
                chunks[knowledge_id] = {
                    chunk["chunk_id"]: chunk for chunk in chunk_document(record)
                } if record else {}
            if record is None or record["revision"] != revision:
                continue
            chunk_id = row.get("chunk_id") or row.get("id")
            expected = chunks[knowledge_id].get(chunk_id) if isinstance(chunk_id, str) else None
            if not expected or row.get("source_text") != expected["source_text"] or row.get("content_hash") != expected["content_hash"]:
                continue
            match = matches.setdefault(knowledge_id, {"record": record, "ranks": {}, "hits": {}})
            for route, rank in route_ranks.items():
                match["ranks"][route] = min(rank, match["ranks"].get(route, rank))
            # Compare ranks, never dense and keyword raw scores. Duplicate chunks
            # neither boost a parent asset nor repeat its text in the rerank input.
            hit_rank = (-sum(1 / (60 + rank) for rank in route_ranks.values()), position, chunk_id)
            text = expected["source_text"]
            match["hits"][text] = min(hit_rank, match["hits"].get(text, hit_rank))
        # Recheck before sending any source text to the model: body loading and
        # cloud retrieval can outlive withdrawal or replacement of a revision.
        current_pairs = {(item["knowledge_id"], item["revision"]) for item in self.store.published_revisions(**filters)}
        candidates = []
        for knowledge_id, match in matches.items():
            record = match["record"]
            if (knowledge_id, record["revision"]) not in current_pairs:
                continue
            hits = sorted(match["hits"].items(), key=lambda item: item[1])
            asset = public_record(record)
            if not include_content:
                asset.pop("content", None)
            rrf_score = sum(1 / (60 + rank) for rank in match["ranks"].values())
            candidates.append({
                **asset, "snippet": hits[0][0], "chunk_id": hits[0][1][2],
                "retrieval_routes": sorted(match["ranks"]), "rrf_score": rrf_score,
                "rerank_score": None, "rerank_mode": "fallback_rrf", "score": rrf_score,
                "read_url": f"/api/knowledge/{knowledge_id}?revision={record['revision']}",
                "_rerank_text": "\n\n".join([
                    f"标题：{record['title'][:200]}", *(text for text, _ in hits[:3]),
                ])[:1800],
            })
        # Bound model work by assets, independent of how many chunks each has.
        candidates = sorted(candidates, key=lambda item: (-item["rrf_score"], item["knowledge_id"]))[:50]
        documents = [item.pop("_rerank_text") for item in candidates]
        if not candidates:
            return []
        try:
            scores = rerank_service.call_rerank_model(query.strip(), documents)
            if not isinstance(scores, list) or len(scores) != len(candidates) or any(type(score) is bool for score in scores):
                raise ValueError("incomplete rerank scores")
            scores = [float(score) for score in scores]
            if not all(math.isfinite(score) for score in scores):
                raise ValueError("nonfinite rerank scores")
            for item, score in zip(candidates, scores):
                item.update(score=score, rerank_score=score, rerank_mode="model")
        except Exception as exc:
            # SDK errors can contain credentials and documents; log only type.
            logger.warning("Company knowledge rerank unavailable; using RRF (%s)", type(exc).__name__)
        current_pairs = {(item["knowledge_id"], item["revision"]) for item in self.store.published_revisions(**filters)}
        results = [item for item in candidates if (item["knowledge_id"], item["revision"]) in current_pairs]
        return sorted(results, key=lambda item: (-item["score"], -item["rrf_score"], item["knowledge_id"]))[:top_k]
