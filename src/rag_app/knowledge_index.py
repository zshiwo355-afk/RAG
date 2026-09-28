"""Cloud index for published company knowledge; never searches product tables.

Publication/current-version decisions belong to the local knowledge store. This
module only stages immutable chunks, verifies them, and searches an explicit
allowlist of revisions supplied by that store.
"""

from __future__ import annotations

import hashlib
from itertools import groupby
import math
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .knowledge_chunking import (
    LEGACY_VERSION, STRUCTURE_VERSION, STRUCTURE_V2_VERSION, split_structure, split_structure_v2,
)
from .opensearch_client import create_client, ensure_runtime_config
from .retrieval_service import (
    body_to_plain,
    build_text_query_string,
    embed_text_query,
    extract_result_items,
    flatten_result_item,
)
from .visual_ingest_api import chunk_text


TABLE_NAME = "company_knowledge"
HYBRID_TABLE_NAME = "company_knowledge_hybrid"
VECTOR_FIELD = "source_text_vector"
VECTOR_DIMENSIONS = 1024
MAX_TITLE_CHARS = 240
FILTER_BATCH_SIZE = 32
WRITE_BATCH_SIZE = 10
VERIFY_ATTEMPTS = 31
VERIFY_RETRY_DELAY = 1.0
RETRY_DELAY = 0.25
# Descriptive field types, not a create-table request. The CLI does not rebuild tables.
SCHEMA_FIELDS = {
    "id": "string (primary key)",
    "knowledge_id": "string",
    "revision": "int64",
    "revision_key": "string (filter attribute)",
    "chunk_id": "string",
    "chunk_index": "int64",
    "title": "string (lossless display text)",
    "source_text": "string (lossless chunk text)",
    "title_terms": "text (Chinese keyword index only)",
    "text_terms": "text (Chinese keyword index only)",
    "content_hash": "string",
    VECTOR_FIELD: "float[1024]",
}
OUTPUT_FIELDS = [name for name in SCHEMA_FIELDS if name not in {VECTOR_FIELD, "title_terms", "text_terms"}]
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_INSTANCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class KnowledgeIndexError(RuntimeError):
    """A sanitized indexing error safe to report through the CLI/API."""


def _identity(record: dict[str, Any]) -> tuple[str, int, str]:
    knowledge_id, revision = record.get("knowledge_id"), record.get("revision")
    if not isinstance(knowledge_id, str) or not _ID_PATTERN.fullmatch(knowledge_id):
        raise ValueError("knowledge_id must be a safe identifier of at most 80 characters")
    if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        raise ValueError("revision must be a positive int64")
    return knowledge_id, revision, f"{knowledge_id}__r{revision}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_document(record: dict[str, Any]) -> list[dict[str, Any]]:
    knowledge_id, revision, revision_key = _identity(record)
    content, title = record.get("content"), record.get("title")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be nonempty text")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be nonempty text")
    if record.get("content_hash") != _sha256(content):
        raise ValueError("document content_hash does not match content")
    version = record.get("chunking_version", LEGACY_VERSION)
    structured = []
    if version == LEGACY_VERSION:
        texts = chunk_text(content, 900, 120)
    elif version == STRUCTURE_VERSION:
        texts = [chunk["source_text"] for chunk in split_structure(content)]
    elif version == STRUCTURE_V2_VERSION:
        structured = split_structure_v2(content)
        texts = [chunk["source_text"] for chunk in structured]
    else:
        raise ValueError("unsupported chunking_version")
    chunks = []
    for index, text in enumerate(texts, start=1):
        chunk_id = f"{revision_key}__c{index}"
        chunks.append({
            "id": chunk_id,
            "knowledge_id": knowledge_id,
            "revision": revision,
            "revision_key": revision_key,
            "chunk_id": chunk_id,
            "chunk_index": index,
            "title": title.strip()[:MAX_TITLE_CHARS],
            "source_text": text,
            "content_hash": _sha256(text),
        })
        if version == STRUCTURE_V2_VERSION:
            # Embedding-only context; the cloud schema remains unchanged.
            chunks[-1]["section_path"] = structured[index - 1]["section_path"]
    if not chunks:
        raise ValueError("document has no indexable content")
    return chunks


def _vector(values: Any) -> list[float]:
    if not isinstance(values, list) or len(values) != VECTOR_DIMENSIONS:
        raise KnowledgeIndexError("embedding must have 1024 finite numeric dimensions")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise KnowledgeIndexError("embedding must have 1024 finite numeric dimensions")
    return [float(value) for value in values]


def _filter(revision_keys: list[str]) -> str:
    # Keys have already passed _identity; callers cannot inject a cloud expression.
    return "(" + " OR ".join(f'revision_key="{key}"' for key in revision_keys) + ")"


def _check_body(response: Any, *, write: bool = False) -> None:
    body = body_to_plain(getattr(response, "body", response))
    if not isinstance(body, dict):
        raise KnowledgeIndexError("OpenSearch returned an invalid response")
    status, code = body.get("status"), body.get("code")
    if (body.get("error") or body.get("errors") or code not in (None, 0, 200, "0", "200")
            or body.get("errorCode") not in (None, 0, "0")):
        raise KnowledgeIndexError("OpenSearch rejected the operation")
    if status is not None and str(status).upper() not in ("OK", "SUCCESS"):
        raise KnowledgeIndexError("OpenSearch rejected the operation")
    if write and status is None and code not in (0, 200, "0", "200"):
        raise KnowledgeIndexError("OpenSearch did not acknowledge the write")


class KnowledgeIndex:
    def __init__(self) -> None:
        self.client = None
        self.models = None
        self.table = ""
        self.instance_id = ""

    def _connect(self) -> None:
        if self.client is not None:
            return
        try:
            config = ensure_runtime_config()
            if any(not isinstance(config.get(key), str) or not config[key].strip()
                   for key in ("endpoint", "instance_id", "username", "password")):
                raise ValueError("incomplete configuration")
            instance = config["instance_id"]
            endpoint = config["endpoint"]
            url = urlsplit(endpoint if "://" in endpoint else "//" + endpoint)
            if (not _INSTANCE_PATTERN.fullmatch(instance) or not url.hostname
                    or url.scheme not in ("", "http", "https") or url.username
                    or url.password or url.query or url.fragment or url.path not in ("", "/")
                    or any(char.isspace() for char in endpoint)):
                raise ValueError("invalid configuration")
            table = os.getenv("KNOWLEDGE_TABLE", TABLE_NAME).strip()
            if table not in (TABLE_NAME, HYBRID_TABLE_NAME,
                             f"{instance}_{TABLE_NAME}", f"{instance}_{HYBRID_TABLE_NAME}"):
                raise ValueError("invalid knowledge table")
            client, models = create_client(config)
            from alibabacloud_tea_util.models import RuntimeOptions

            # SDK 1.1.17 has no with_options method; its request methods read this field.
            client._runtime_options = RuntimeOptions(
                connect_timeout=5000, read_timeout=15000, autoretry=False,
                max_attempts=1, ignore_ssl=False, max_idle_conns=10,
            )
            self.client, self.models, self.table, self.instance_id = client, models, table, instance
        except Exception:
            raise KnowledgeIndexError("invalid or unavailable company knowledge cloud configuration") from None

    def _datasource(self) -> str:
        source = os.getenv("KNOWLEDGE_PUSH_DATASOURCE", "").strip()
        target = self.table.removeprefix(f"{self.instance_id}_")
        if target not in (TABLE_NAME, HYBRID_TABLE_NAME) or source not in (target, f"{self.instance_id}_{target}"):
            raise KnowledgeIndexError("KNOWLEDGE_PUSH_DATASOURCE must explicitly name the company knowledge data source")
        return source

    def _call(self, operation: Callable[[], Any]) -> Any:
        # Retry the same immutable IDs only. Never try another table on failure.
        for attempt in range(2):
            try:
                return operation()
            except Exception:
                if attempt == 1:
                    raise KnowledgeIndexError("company knowledge cloud request failed") from None
                time.sleep(RETRY_DELAY)

    def _embed(self, text: str) -> list[float]:
        try:
            return _vector(embed_text_query(text))
        except Exception:
            raise KnowledgeIndexError("company knowledge embedding failed or returned an invalid vector") from None

    def _query(self, vector: list[float], keys: list[str], top_k: int) -> list[dict[str, Any]]:
        request = self.models.QueryRequest(
            table_name=self.table, index_name=VECTOR_FIELD, vector=vector,
            top_k=top_k, include_vector=False, output_fields=OUTPUT_FIELDS,
            order="DESC", filter=_filter(keys),
        )
        response = self._call(lambda: self.client.query(request))
        _check_body(response)
        try:
            rows = [flatten_result_item(row) for row in extract_result_items(response)]
        except Exception:
            raise KnowledgeIndexError("company knowledge query returned invalid results") from None
        return rows

    def _query_keyword(self, query: str, keys: list[str], top_k: int) -> list[dict[str, Any]]:
        text = self.models.TextQuery(
            query_string="(" + " OR ".join(build_text_query_string(field, query)
                                            for field in ("title_terms", "text_terms")) + ")",
            query_params={"default_op": "OR"}, filter=_filter(keys),
        )
        request = self.models.SearchRequest(
            table_name=self.table, size=top_k, order="DESC", output_fields=OUTPUT_FIELDS, text=text,
        )
        response = self._call(lambda: self.client.search(request))
        _check_body(response)
        try:
            rows = [flatten_result_item(row) for row in extract_result_items(response)]
        except Exception:
            raise KnowledgeIndexError("company knowledge keyword query returned invalid results") from None
        return rows

    def index_and_verify(self, record: dict[str, Any]) -> dict[str, Any]:
        chunks = chunk_document(record)
        self._connect()
        data_source = self._datasource()
        vectors = [self._embed("\n".join([chunk["title"], *chunk.get("section_path", []), chunk["source_text"]]))
                   for chunk in chunks]
        fields = [{name: chunk[name] for name in OUTPUT_FIELDS} for chunk in chunks]
        for start in range(0, len(chunks), WRITE_BATCH_SIZE):
            # TEXT analysis can discard non-BMP characters even in summaries.
            # Index separate copies; hashes/display always use the raw STRINGs.
            body = [{"cmd": "add", "fields": {**chunk, VECTOR_FIELD: vector,
                     "title_terms": chunk["title"], "text_terms": chunk["source_text"]}}
                    for chunk, vector in zip(fields[start:start + WRITE_BATCH_SIZE],
                                             vectors[start:start + WRITE_BATCH_SIZE])]
            request = self.models.PushDocumentsRequest(headers={}, body=body)
            response = self._call(lambda: self.client.push_documents(data_source, "id", request))
            _check_body(response, write=True)

        expected = {chunk["id"]: chunk for chunk in fields}
        verified = False
        for attempt in range(VERIFY_ATTEMPTS):
            found = {}
            for start in range(0, len(chunks), WRITE_BATCH_SIZE):
                ids = [chunk["id"] for chunk in chunks[start:start + WRITE_BATCH_SIZE]]
                request = self.models.FetchRequest(
                    table_name=self.table, ids=ids, include_vector=False, output_fields=OUTPUT_FIELDS,
                )
                response = self._call(lambda: self.client.fetch(request))
                _check_body(response)
                try:
                    rows = [flatten_result_item(row) for row in extract_result_items(response)]
                    found.update({row.get("id"): row for row in rows})
                except Exception:
                    raise KnowledgeIndexError("company knowledge verification returned invalid results") from None
            verified = all(doc_id in found and all(found[doc_id].get(key) == value
                           for key, value in chunk.items()) for doc_id, chunk in expected.items())
            if verified:
                break
            if attempt + 1 < VERIFY_ATTEMPTS:
                time.sleep(VERIFY_RETRY_DELAY)
        if not verified:
            raise KnowledgeIndexError("company knowledge write verification failed; revision was not published")

        recalled = False
        # Share at most 30 seconds of visibility waits across fetch and query;
        # network timeouts are separate. Neither phase repeats embedding/push.
        remaining_attempts = VERIFY_ATTEMPTS - attempt
        for attempt in range(remaining_attempts):
            hits = self._query(vectors[0], [chunks[0]["revision_key"]], min(10, len(chunks)))
            recalled = any(isinstance(hit.get("id"), str) and hit["id"] in expected and all(hit.get(key) == value
                           for key, value in expected[hit["id"]].items()) for hit in hits)
            if recalled:
                break
            if attempt + 1 < remaining_attempts:
                time.sleep(VERIFY_RETRY_DELAY)
        if not recalled:
            raise KnowledgeIndexError("company knowledge retrieval verification failed; revision was not published")
        return {
            "table": self.table, "data_source": data_source,
            "knowledge_id": record["knowledge_id"], "revision": record["revision"],
            "chunk_count": len(chunks), "verified_chunk_ids": list(expected),
            "retrieval_verified": True,
        }

    def search(self, query: str, allowed_versions: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        """Return the union of per-route candidates with global, separate ranks.

        Both routes must succeed; a missing text index is an error, never a
        silently dense-only result. Each route applies the published allowlist
        before retrieval and validates it again before contributing candidates.
        """
        if not allowed_versions:
            return []
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("query must contain 1 to 4000 characters")
        if type(top_k) is not int or not 1 <= top_k <= 200:
            raise ValueError("top_k must be between 1 and 200")
        identities = [_identity(record) for record in allowed_versions]
        allowed = {key: (knowledge_id, revision) for knowledge_id, revision, key in identities}
        self._connect()
        vector = self._embed(query.strip())
        routes: dict[str, dict[str, dict[str, Any]]] = {"text_dense": {}, "text_keyword": {}}
        keys = sorted(allowed)
        # ponytail: fixed chunk windows can favor long assets; split saturated
        # batches if target-scale tests show parent assets being crowded out.
        for start in range(0, len(keys), FILTER_BATCH_SIZE):
            batch_keys = keys[start:start + FILTER_BATCH_SIZE]
            for route, rows in (("text_dense", self._query(vector, batch_keys, top_k)),
                                ("text_keyword", self._query_keyword(query.strip(), batch_keys, top_k))):
                results = routes[route]
                for row in rows:
                    key = row.get("revision_key")
                    if key not in batch_keys:
                        continue
                    knowledge_id, revision = allowed[key]
                    index, text = row.get("chunk_index"), row.get("source_text")
                    if (row.get("knowledge_id") != knowledge_id or row.get("revision") != revision
                            or type(index) is not int or index < 1
                            or not isinstance(text, str) or not text.strip() or len(text) > 900
                            or row.get("content_hash") != _sha256(text)):
                        continue
                    chunk_id = f"{key}__c{index}"
                    if row.get("id") != chunk_id or row.get("chunk_id") != chunk_id:
                        continue
                    try:
                        score = float(row.get("score"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not math.isfinite(score):
                        continue
                    result = {name: row[name] for name in (
                        "knowledge_id", "revision", "chunk_id", "chunk_index", "source_text", "content_hash",
                    )}
                    result["score"] = score
                    if chunk_id not in results or results[chunk_id]["score"] < score:
                        results[chunk_id] = result
        combined: dict[str, dict[str, Any]] = {}
        dense_best: dict[tuple[str, int], float] = {}
        for row in routes["text_dense"].values():
            key = (row["knowledge_id"], row["revision"])
            dense_best[key] = max(dense_best.get(key, -math.inf), row["score"])
        for route, rows in routes.items():
            parents: dict[tuple[str, int], list[dict[str, Any]]] = {}
            for row in sorted(rows.values(), key=lambda row: (-row["score"], row["chunk_index"], row["chunk_id"])):
                parents.setdefault((row["knowledge_id"], row["revision"]), []).append(row)
            ranked = sorted(parents.values(), key=lambda hits: (
                -hits[0]["score"], -dense_best.get((hits[0]["knowledge_id"], hits[0]["revision"]), -math.inf),
                hits[0]["knowledge_id"], hits[0]["revision"],
            ))
            position = 0
            for _, tied in groupby(ranked, key=lambda hits: hits[0]["score"]):
                tied = list(tied)
                # The live text endpoint can return all-zero scores. Give tied
                # parents equal average ranks, never relevance based on their IDs.
                rank = position + (len(tied) + 1) / 2
                for hits in tied[:max(0, top_k - position)]:
                    for row in hits[:3]:
                        item = combined.setdefault(row["chunk_id"], {**row, "route_ranks": {}, "route_scores": {}})
                        item["route_ranks"][route] = rank
                        item["route_scores"][route] = row["score"]
                position += len(tied)
                if position >= top_k:
                    break
        # Do not compare keyword and vector raw scores or discard keyword-only
        # candidates. The service fuses once per parent after body validation.
        return sorted(combined.values(), key=lambda row: (
            -sum(1 / (60 + rank) for rank in row["route_ranks"].values()),
            -row["route_scores"].get("text_dense", -math.inf), row["chunk_id"],
        ))
