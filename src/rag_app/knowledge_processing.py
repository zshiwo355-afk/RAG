"""Durable receipt processing, conservative publication rules and exception handling."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
import zipfile

from .knowledge_intake import SOURCE_PROTOCOL, process_batch
from .knowledge_governance import GovernanceStore
from .knowledge_objects import KnowledgeObjects
from .knowledge_receipts import LeaseLost, ReceiptError, ReceiptObjects, ReceiptStore, _utc
from .knowledge_store import AutomaticPublicationConflict, KnowledgeStore
from .knowledge_service import KnowledgeService
from .knowledge_text import clean_text


RULE_VERSION = "receipt-rules-v2"
LEGACY_RULE_VERSION = "receipt-rules-v1"
STATUSES = {"queued", "running", "completed", "needs_review", "failed"}
RECEIPT_STATUSES = ("awaiting_upload", "pending_verification", "verified", "rejected", "failed")
ITEM_STATUSES = ("draft", "needs_review", "archived", "duplicate", "outdated", "indexing", "published")
LEASE_SECONDS = 90
MAX_ATTEMPTS = 5
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 2000
ARTIFACT_PREFIX = "knowledge-receipts/processed"


class ProcessingError(RuntimeError):
    def __init__(self, code, status_code=503):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_processing_jobs (
      job_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, principal TEXT NOT NULL,
      source_id TEXT NOT NULL, filename TEXT NOT NULL, rule_version TEXT NOT NULL,
      status TEXT NOT NULL, stage TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
      available_at DOUBLE PRECISION NOT NULL DEFAULT 0, error_code TEXT,
      created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
      UNIQUE(receipt_id, rule_version))""",
    """CREATE TABLE IF NOT EXISTS knowledge_processing_items (
      item_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source_path TEXT NOT NULL,
      title TEXT NOT NULL, status TEXT NOT NULL, reason_codes TEXT NOT NULL,
      knowledge_id TEXT, revision BIGINT, content_ref TEXT, metadata_ref TEXT,
      updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_processing_events (
      event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, event_type TEXT NOT NULL,
      reason_code TEXT, actor TEXT, created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS knowledge_processing_pending ON knowledge_processing_jobs(status, available_at, lease_until)",
    "CREATE INDEX IF NOT EXISTS knowledge_processing_owner ON knowledge_processing_jobs(principal, created_at)",
    "CREATE INDEX IF NOT EXISTS knowledge_processing_items_job ON knowledge_processing_items(job_id)",
    "CREATE INDEX IF NOT EXISTS knowledge_processing_events_job ON knowledge_processing_events(job_id, created_at)",
)


class ProcessingStore(ReceiptStore):
    """principal=None is internal administrator scope; callers must authorize it."""

    def initialize(self):
        # Receipts must already be explicitly initialized; reads never create tables.
        with self._connection(write=True) as connection:
            self._execute(connection, "SELECT receipt_id FROM knowledge_upload_receipts LIMIT 1")
            self._lock(connection, "schema:knowledge-processing")
            for statement in _SCHEMA:
                connection.execute(statement)
            columns = ({row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='knowledge_processing_items'").fetchall()}
                if self.database_url else {row["name"] for row in connection.execute("PRAGMA table_info(knowledge_processing_items)").fetchall()})
            for name, definition in (("version", "INTEGER NOT NULL DEFAULT 1"), ("resolution_json", "TEXT"), ("publication_json", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE knowledge_processing_items ADD COLUMN {name} {definition}")
        GovernanceStore(KnowledgeStore(self.path, database_url=self.database_url)).initialize()

    def _event(self, connection, job_id, event_type, reason=None, actor=None):
        self._execute(connection, """INSERT INTO knowledge_processing_events
            (event_id,job_id,event_type,reason_code,actor,created_at) VALUES (?,?,?,?,?,?)""",
            (uuid.uuid4().hex, job_id, event_type, reason, actor, self.clock()))

    def enqueue_verified(self, rule_version=RULE_VERSION):
        if rule_version != RULE_VERSION:
            raise ProcessingError("unsupported_processing_rule", 400)
        with self._connection(write=True) as connection:
            self._lock(connection, "enqueue:knowledge-processing")
            rows = self._execute(connection, """SELECT r.* FROM knowledge_upload_receipts r
                WHERE r.status='verified' AND NOT EXISTS (SELECT 1 FROM knowledge_processing_jobs j
                WHERE j.receipt_id=r.receipt_id)
                ORDER BY r.created_at,r.receipt_id LIMIT 100""").fetchall()
            for row in rows:
                job_id, now = uuid.uuid4().hex, self.clock()
                self._execute(connection, """INSERT INTO knowledge_processing_jobs
                    (job_id,receipt_id,principal,source_id,filename,rule_version,status,stage,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'queued','queued',?,?)""",
                    (job_id, row["receipt_id"], row["principal"], row["source_id"], row["filename"], rule_version, now, now))
                self._event(connection, job_id, "queued")
            return len(rows)

    def _job(self, connection, job_id, principal, *, lock=False):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ProcessingError("processing_job_not_found", 404)
        row = self._execute(connection, "SELECT * FROM knowledge_processing_jobs WHERE job_id=?" +
                            (" FOR UPDATE" if lock and self.database_url else ""), (job_id,)).fetchone()
        if row is None or principal is not None and row["principal"] != principal:
            raise ProcessingError("processing_job_not_found", 404)
        return dict(row)

    def fence(self, connection, row):
        # A write on the lease row holds the fence through the draft transaction.
        result = self._execute(connection, """UPDATE knowledge_processing_jobs SET lease_until=lease_until
            WHERE job_id=? AND status='running' AND lease_token=? AND lease_until>?""",
            (row["job_id"], row["lease_token"], self.clock()))
        if result.rowcount != 1:
            raise LeaseLost()

    def claim(self):
        now = self.clock()
        with self._connection(write=True) as connection:
            self._lock(connection, "worker:knowledge-processing")
            if self._execute(connection, "SELECT 1 FROM knowledge_processing_jobs WHERE status='running' AND lease_until>? LIMIT 1", (now,)).fetchone():
                return None
            rows = self._execute(connection, """SELECT * FROM knowledge_processing_jobs
                WHERE rule_version IN (?,?) AND ((status='queued' AND available_at<=?) OR (status='running' AND lease_until<=?))
                ORDER BY created_at,job_id LIMIT 100""" + (" FOR UPDATE" if self.database_url else ""), (RULE_VERSION, LEGACY_RULE_VERSION, now, now)).fetchall()
            # A heartbeat/finish may have held a row lock while our SELECT waited.
            # PostgreSQL rechecks locked rows; also recheck the global slot on a fresh snapshot.
            now = self.clock()
            if self._execute(connection, "SELECT 1 FROM knowledge_processing_jobs WHERE status='running' AND lease_until>? LIMIT 1", (now,)).fetchone():
                return None
            for record in rows:
                row = dict(record)
                if row["attempts"] >= MAX_ATTEMPTS:
                    self._execute(connection, """UPDATE knowledge_processing_jobs SET status='failed',stage='failed',
                        error_code='processing_retries_exhausted',lease_token=NULL,lease_until=0,updated_at=? WHERE job_id=?""", (now, row["job_id"]))
                    self._event(connection, row["job_id"], "failed", "processing_retries_exhausted")
                    continue
                token = uuid.uuid4().hex
                changed = self._execute(connection, """UPDATE knowledge_processing_jobs SET status='running',stage='downloading',
                    attempts=attempts+1,lease_token=?,lease_until=?,updated_at=?
                    WHERE job_id=? AND status=? AND attempts=? AND lease_until=?""",
                    (token, now + LEASE_SECONDS, now, row["job_id"], row["status"], row["attempts"], row["lease_until"]))
                if changed.rowcount != 1:
                    continue
                self._event(connection, row["job_id"], "claimed")
                return {**row, "status": "running", "attempts": row["attempts"] + 1, "lease_token": token}
        return None

    def heartbeat(self, row):
        with self._connection(write=True) as connection:
            self.fence(connection, row)
            self._execute(connection, "UPDATE knowledge_processing_jobs SET lease_until=? WHERE job_id=?",
                          (self.clock() + LEASE_SECONDS, row["job_id"]))

    def stage(self, row, stage):
        if stage not in {"downloading", "parsing", "persisting", "importing", "indexing"}:
            raise ProcessingError("invalid_processing_stage", 400)
        with self._connection(write=True) as connection:
            self.fence(connection, row)
            self._execute(connection, "UPDATE knowledge_processing_jobs SET stage=?,updated_at=? WHERE job_id=?", (stage, self.clock(), row["job_id"]))
            self._event(connection, row["job_id"], stage)

    def save_item(self, row, item, content_ref=None, metadata_ref=None):
        with self._connection(write=True) as connection:
            self.fence(connection, row)
            self._execute(connection, """INSERT INTO knowledge_processing_items
                (item_id,job_id,source_path,title,status,reason_codes,knowledge_id,revision,content_ref,metadata_ref,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                status=excluded.status,reason_codes=excluded.reason_codes,knowledge_id=excluded.knowledge_id,
                revision=excluded.revision,content_ref=excluded.content_ref,metadata_ref=excluded.metadata_ref,
                version=knowledge_processing_items.version+1,updated_at=excluded.updated_at""",
                (item["item_id"], row["job_id"], item["source_path"], item["title"], item["status"], _json(item["reason_codes"]),
                 item.get("knowledge_id"), item.get("revision"), _json(content_ref) if content_ref else None,
                 _json(metadata_ref) if metadata_ref else None, self.clock()))
            self._event(connection, row["job_id"], "item_" + item["status"], item["reason_codes"][0] if item["reason_codes"] else None)

    def finish(self, row, status, code=None):
        if status not in {"completed", "needs_review", "queued"}:
            raise ProcessingError("invalid_processing_status", 400)
        if status == "queued" and row["attempts"] >= MAX_ATTEMPTS:
            status = "failed"
        available = self.clock() + min(30 * 2 ** (row["attempts"] - 1), 300) if status == "queued" else 0
        with self._connection(write=True) as connection:
            self.fence(connection, row)
            self._execute(connection, """UPDATE knowledge_processing_jobs SET status=?,stage=?,error_code=?,
                available_at=?,lease_token=NULL,lease_until=0,updated_at=? WHERE job_id=?""",
                (status, status, code, available, self.clock(), row["job_id"]))
            self._event(connection, row["job_id"], status, code)

    @staticmethod
    def _summary(row):
        return {**{key: row[key] for key in ("job_id", "receipt_id", "principal", "source_id", "filename", "rule_version", "status", "stage", "attempts", "error_code")},
                "created_at": _utc(row["created_at"]), "updated_at": _utc(row["updated_at"]),
                "retryable": row["status"] == "failed", "published": row.get("published_count", 0) > 0,
                "counts": {state: row.get(state + "_count", 0) for state in ITEM_STATUSES}}

    def list_jobs(self, principal, status=None, limit=50, offset=0):
        if status is not None and status not in STATUSES or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ProcessingError("invalid_processing_query", 400)
        conditions, args = ["1=1"], []
        if principal is not None:
            conditions.append("j.principal=?")
            args.append(principal)
        if status:
            conditions.append("j.status=?")
            args.append(status)
        where = " AND ".join(conditions)
        counts = ",".join("(SELECT COUNT(*) FROM knowledge_processing_items i WHERE i.job_id=j.job_id AND i.status='" + state + "') AS " + state + "_count" for state in ITEM_STATUSES)
        with self._connection() as connection:
            total = self._execute(connection, "SELECT COUNT(*) AS n FROM knowledge_processing_jobs j WHERE " + where, args).fetchone()["n"]
            rows = self._execute(connection, "SELECT j.*," + counts + " FROM knowledge_processing_jobs j WHERE " + where + " ORDER BY j.created_at DESC,j.job_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [self._summary(dict(row)) for row in rows], "total": total}

    def _receipt_counts(self, connection, principal):
        where, args = (" WHERE principal=?", (principal,)) if principal is not None else ("", ())
        rows = self._execute(connection, "SELECT status,COUNT(*) AS n FROM knowledge_upload_receipts" + where + " GROUP BY status", args).fetchall()
        counts = dict.fromkeys(RECEIPT_STATUSES, 0)
        for row in rows:
            state = "pending_verification" if row["status"] == "verifying" else row["status"]
            if state in counts:
                counts[state] += row["n"]
        return {"total": sum(row["n"] for row in rows), **counts}

    def list_receipts(self, principal, status=None, limit=20, offset=0):
        if (status is not None and status not in RECEIPT_STATUSES or type(limit) is not int
                or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000):
            raise ProcessingError("invalid_processing_query", 400)
        conditions, args = ["1=1"], []
        if principal is not None:
            conditions.append("r.principal=?")
            args.append(principal)
        if status == "pending_verification":
            conditions.append("r.status IN ('pending_verification','verifying')")
        elif status:
            conditions.append("r.status=?")
            args.append(status)
        where = " AND ".join(conditions)
        with self._connection() as connection:
            counts = self._receipt_counts(connection, principal)
            total = self._execute(connection, "SELECT COUNT(*) AS n FROM knowledge_upload_receipts r WHERE " + where, args).fetchone()["n"]
            # One row per original, including receipts that never passed verification.
            # Match the owner as well as the receipt; use the newest job if rules changed.
            rows = self._execute(connection, """SELECT r.receipt_id,r.filename,r.byte_length,
                CASE WHEN r.status='verifying' THEN 'pending_verification' ELSE r.status END AS status,r.error_code,
                r.created_at,r.updated_at,j.job_id,j.status AS job_status FROM knowledge_upload_receipts r
                LEFT JOIN knowledge_processing_jobs j ON j.job_id=(SELECT p.job_id FROM knowledge_processing_jobs p
                    WHERE p.receipt_id=r.receipt_id AND p.principal=r.principal
                    ORDER BY p.created_at DESC,p.job_id DESC LIMIT 1)
                WHERE """ + where + " ORDER BY r.created_at DESC,r.receipt_id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "receipt_counts": counts}

    def get_job(self, job_id, principal, limit=100, item_offset=0, event_offset=0):
        if (type(limit) is not int or not 1 <= limit <= 100
                or any(type(value) is not int or not 0 <= value <= 1_000_000 for value in (item_offset, event_offset))):
            raise ProcessingError("invalid_processing_query", 400)
        with self._connection() as connection:
            row = self._job(connection, job_id, principal)
            counts = self._execute(connection, "SELECT status,COUNT(*) AS n FROM knowledge_processing_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()
            event_total = self._execute(connection, "SELECT COUNT(*) AS n FROM knowledge_processing_events WHERE job_id=?", (job_id,)).fetchone()["n"]
            items = self._execute(connection, "SELECT * FROM knowledge_processing_items WHERE job_id=? ORDER BY source_path,item_id LIMIT ? OFFSET ?", (job_id, limit, item_offset)).fetchall()
            events = self._execute(connection, "SELECT * FROM knowledge_processing_events WHERE job_id=? ORDER BY created_at,event_id LIMIT ? OFFSET ?", (job_id, limit, event_offset)).fetchall()
        row.update({item["status"] + "_count": item["n"] for item in counts})
        return {**self._summary(row), "items": [self._public_item(item) for item in items],
                "item_total": sum(item["n"] for item in counts), "event_total": event_total,
                "item_offset": item_offset, "event_offset": event_offset, "limit": limit,
                "events": [{**{key: event[key] for key in ("event_id", "event_type", "reason_code", "actor")}, "created_at": _utc(event["created_at"])} for event in events]}

    @staticmethod
    def _public_item(item):
        resolution = json.loads(item["resolution_json"]) if item["resolution_json"] else {}
        return {**{key: item[key] for key in ("item_id", "source_path", "title", "status", "knowledge_id", "revision")},
                "reason_codes": json.loads(item["reason_codes"]), "has_preview": item["content_ref"] is not None,
                "published": item["status"] == "published", "version": item["version"],
                "publication": json.loads(item["publication_json"]) if item["publication_json"] else None,
                **{key: resolution[key] for key in ("replacement_receipt_id", "replacement_job_id") if key in resolution}}

    def item_record(self, job_id, item_id, principal):
        with self._connection() as connection:
            self._job(connection, job_id, principal)
            item = self._execute(connection, "SELECT * FROM knowledge_processing_items WHERE job_id=? AND item_id=?", (job_id, item_id)).fetchone()
            if item is None:
                raise ProcessingError("processing_item_not_found", 404)
            return dict(item)

    def stats(self, principal):
        where, params = (" WHERE principal=?", (principal,)) if principal is not None else ("", ())
        with self._connection() as connection:
            receipt_counts = self._receipt_counts(connection, principal)
            jobs = self._execute(connection, "SELECT status,COUNT(*) AS n FROM knowledge_processing_jobs" + where + " GROUP BY status", params).fetchall()
            items = self._execute(connection, "SELECT i.status,COUNT(*) AS n FROM knowledge_processing_items i JOIN knowledge_processing_jobs j ON j.job_id=i.job_id" + (" WHERE j.principal=?" if principal is not None else "") + " GROUP BY i.status", params).fetchall()
        keys = {state: ("review_items" if state == "needs_review" else state + "_items") for state in ITEM_STATUSES}
        result = {"receipts": receipt_counts["total"], "receipt_counts": receipt_counts,
                  **{state: 0 for state in STATUSES}, **{key: 0 for key in keys.values()}}
        result.update({row["status"]: row["n"] for row in jobs})
        result.update({keys[row["status"]]: row["n"] for row in items})
        return result

    def mark_published(self, connection, row, item, published, verification):
        # Runs inside the catalogue pointer transaction, after the lease fence.
        proof = {"rule_version": row["rule_version"], "knowledge_id": published["knowledge_id"],
                 "revision": published["revision"], "content_hash": published["content_hash"],
                 "confirmed_by": published["confirmed_by"], "published_at": published["published_at"],
                 "verification": verification}
        changed = self._execute(connection, """UPDATE knowledge_processing_items
            SET status='published',reason_codes=?,publication_json=?,version=version+1,updated_at=?
            WHERE item_id=? AND job_id=? AND knowledge_id=? AND revision=? AND status IN ('indexing','published')""",
            (_json(["automatic_rules_passed"]), _json(proof), self.clock(), item["item_id"], row["job_id"], published["knowledge_id"], published["revision"]))
        if changed.rowcount != 1:
            raise ProcessingError("publication_checkpoint_conflict", 409)
        self._event(connection, row["job_id"], "item_published", "automatic_rules_passed", "rule:" + row["rule_version"])

    def resolve(self, job_id, item_id, principal, *, expected_version, action, reason,
                replacement_receipt_id=None, actor):
        if (type(expected_version) is not int or not 1 <= expected_version <= 2147483647
                or action not in {"archive", "replace"} or not isinstance(reason, str)
                or not 1 <= len(reason.strip()) <= 500 or not actor
                or action == "archive" and replacement_receipt_id is not None
                or action == "replace" and (not isinstance(replacement_receipt_id, str)
                                            or not re.fullmatch(r"[0-9a-f]{32}", replacement_receipt_id))):
            raise ProcessingError("invalid_processing_resolution", 400)
        with self._connection(write=True) as connection:
            self._lock(connection, "enqueue:knowledge-processing")
            row = self._job(connection, job_id, principal, lock=True)
            resolution = {"action": action, "reason": clean_text(reason.strip())[0], "actor": actor}
            item = self._execute(connection, "SELECT * FROM knowledge_processing_items WHERE job_id=? AND item_id=?",
                                 (job_id, item_id)).fetchone()
            if item is None:
                raise ProcessingError("processing_item_not_found", 404)
            if row["status"] in {"queued", "running"} or item["status"] != "needs_review" or item["version"] != expected_version:
                raise ProcessingError("processing_resolution_conflict", 409)
            if action == "replace":
                receipt = self._execute(connection, "SELECT * FROM knowledge_upload_receipts WHERE receipt_id=? AND principal=?",
                                        (replacement_receipt_id, row["principal"])).fetchone()
                if receipt is None:
                    raise ProcessingError("replacement_receipt_not_found", 404)
                if (receipt["status"] != "verified" or receipt["receipt_id"] == row["receipt_id"]
                        or receipt["source_id"] != row["source_id"]):
                    raise ProcessingError("replacement_receipt_not_eligible", 409)
                replacement = self._execute(connection, "SELECT job_id FROM knowledge_processing_jobs WHERE receipt_id=? ORDER BY created_at LIMIT 1",
                                            (replacement_receipt_id,)).fetchone()
                replacement_job_id = replacement["job_id"] if replacement else uuid.uuid4().hex
                if replacement is None:
                    self._execute(connection, """INSERT INTO knowledge_processing_jobs
                        (job_id,receipt_id,principal,source_id,filename,rule_version,status,stage,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,'queued','queued',?,?)""",
                        (replacement_job_id, receipt["receipt_id"], receipt["principal"], receipt["source_id"], receipt["filename"], RULE_VERSION, self.clock(), self.clock()))
                    self._event(connection, replacement_job_id, "queued", "replacement_submission", actor)
                resolution.update(replacement_receipt_id=replacement_receipt_id, replacement_job_id=replacement_job_id)
            changed = self._execute(connection, """UPDATE knowledge_processing_items
                SET status='archived',reason_codes=?,resolution_json=?,version=version+1,updated_at=?
                WHERE item_id=? AND job_id=? AND status='needs_review' AND version=?""",
                (_json(["replaced_by_submission" if action == "replace" else "reviewer_archived"]), _json(resolution),
                 self.clock(), item_id, job_id, expected_version))
            if changed.rowcount != 1:
                raise ProcessingError("processing_resolution_conflict", 409)
            self._event(connection, job_id, "item_resolved", "replaced_by_submission" if action == "replace" else "reviewer_archived", actor)
            remaining = self._execute(connection, "SELECT 1 FROM knowledge_processing_items WHERE job_id=? AND status='needs_review' LIMIT 1", (job_id,)).fetchone()
            if not remaining and row["status"] == "needs_review":
                self._execute(connection, "UPDATE knowledge_processing_jobs SET status='completed',stage='completed',updated_at=? WHERE job_id=? AND status='needs_review'", (self.clock(), job_id))
        return self._public_item(self.item_record(job_id, item_id, principal))

    def retry(self, job_id, principal, actor=None):
        with self._connection(write=True) as connection:
            row = self._job(connection, job_id, principal, lock=True)
            result = self._execute(connection, """UPDATE knowledge_processing_jobs SET status='queued',stage='queued',
                attempts=0,available_at=0,error_code=NULL,updated_at=? WHERE job_id=? AND status='failed'""", (self.clock(), job_id))
            if result.rowcount != 1:
                raise ProcessingError("processing_retry_not_allowed", 409)
            self._event(connection, job_id, "retried", actor=actor or row["principal"])
        return self.get_job(job_id, principal)


class _FencedDraftStore(KnowledgeStore):
    def __init__(self, processing, job, objects):
        super().__init__(processing.path, database_url=processing.database_url, objects=objects)
        self.processing, self.job = processing, job

    def _lock(self, connection, knowledge_id):
        super()._lock(connection, knowledge_id)
        self.processing.fence(connection, self.job)


class ProcessingService:
    def __init__(self, store=None, objects=None, *, artifacts=None, draft_objects=None,
                 auto_publish=None, knowledge_index=None):
        self.store = store if store is not None else ProcessingStore()
        self.objects, self.artifacts, self.draft_objects = objects, artifacts, draft_objects
        self.auto_publish = os.getenv("KNOWLEDGE_AUTO_PUBLISH_ENABLED") == "1" if auto_publish is None else auto_publish
        self.knowledge_index = knowledge_index

    @staticmethod
    def _automatic_reasons(entry, content):
        evidence = entry.get("evidence", {})
        reasons = set()
        if evidence.get("source_protocol") != SOURCE_PROTOCOL:
            reasons.add("structured_delivery_required")
        if evidence.get("source_sharing") != "全员":
            reasons.add("sharing_scope_unconfirmed")
        for field in ("source_purpose", "source_evidence_status", "source_locator", "source_audience", "source_inputs",
                      "source_outputs", "source_dependencies", "source_boundaries"):
            if str(evidence.get(field, "")).strip() in {"", "未取得", "未提供", "未确认", "未知", "待补充", "待补", "待确认", "待核验"}:
                reasons.add("reuse_information_incomplete")
        # These declarations describe a reusable delivery, not a claim that the
        # submitter authored it or that its business outcome has been verified.
        if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:密级\s*[:：]\s*(?:机密|秘密|保密)|confidential\s*:\s*true)\b", content):
            reasons.add("restricted_material")
        return reasons

    def _publication_guard(self, governance, connection, row, item):
        self.store.fence(connection, row)
        governance.assert_publication_allowed(connection, item["knowledge_id"], item["revision"])

    def _govern_asset(self, row, asset, entry, content, item, content_ref):
        if self.draft_objects is None:
            self.draft_objects = KnowledgeObjects()
        catalogue = KnowledgeStore(self.store.path, database_url=self.store.database_url, objects=self.draft_objects)
        governance = GovernanceStore(catalogue)
        evidence = entry["evidence"]
        automatic_reasons = self._automatic_reasons(entry, content)
        entry["evidence"] = {**evidence, "processing_rule": row["rule_version"],
                             "submitter_authenticated": True, "review_status": "automatic_rule_passed" if not automatic_reasons else "needs_review",
                             "publication_approved": not automatic_reasons, "automatic_rule_reasons": sorted(automatic_reasons)}
        base = evidence.get("source_base_revision")
        governed = governance.import_asset(
            {**entry, "content": content}, asset, item_id=item["item_id"], receipt_id=row["receipt_id"],
            principal=row["principal"], source_id=row["source_id"],
            # Intake identities already include stable source-v2 IDs and case
            # fragments; a shared file path would merge independent cases.
            source_identity=asset["knowledge_id"],
            base_revision=int(base) if base else None, guard=lambda connection: self.store.fence(connection, row))
        record = governed["record"]
        item.update(knowledge_id=record["knowledge_id"], revision=record["revision"])
        reasons = set(governed["reasons"])
        disposition = governed["disposition"]
        if disposition == "old_replay":
            item.update(status="outdated", reason_codes=sorted(reasons or {"outdated_submission"}))
        elif governed["duplicate_of_published"]:
            item.update(status="duplicate", reason_codes=["published_duplicate"])
        elif reasons:
            item.update(status="needs_review", reason_codes=sorted(reasons))
        elif self.auto_publish and automatic_reasons:
            item.update(status="needs_review", reason_codes=sorted(automatic_reasons))
        elif self.auto_publish and governed["publication_allowed"]:
            item.update(status="indexing", reason_codes=["automatic_rules_passed"])
        else:
            item.update(status="draft", reason_codes=["automatic_publication_disabled"])
        metadata_ref = self._artifacts().put(_json({"entry": entry, "item": item, "content_ref": content_ref,
                                                   "rule_version": row["rule_version"], "disposition": disposition,
                                                   "expected_generation": governed["expected_generation"]}))
        self.store.save_item(row, item, content_ref, metadata_ref)
        if item["status"] == "indexing":
            self.store.stage(row, "indexing")
            service = KnowledgeService(catalogue, self.knowledge_index)
            try:
                service.publish_automatic(record["knowledge_id"], record["revision"],
                    expected_generation=governed["expected_generation"], confirmed_by="rule:" + row["rule_version"],
                    transaction_guard=lambda connection: self._publication_guard(governance, connection, row, item),
                    transaction_complete=lambda connection, published, verification: self.store.mark_published(
                        connection, row, item, published, verification))
            except AutomaticPublicationConflict as exc:
                reason = str(exc) if str(exc) in {"published_duplicate_before_publication", "possible_content_conflict"} else "publication_version_conflict"
                item.update(status="needs_review", reason_codes=[reason])
                self.store.save_item(row, item, content_ref, metadata_ref)
            # Infrastructure failures are retried by the job lease with the same
            # submission and pinned catalogue generation; never silently approved.

    def _objects(self):
        if self.objects is None:
            self.objects = ReceiptObjects()
        return self.objects

    def _artifacts(self):
        if self.artifacts is None:
            self._objects().assert_ready()
            self.artifacts = KnowledgeObjects(self._objects().bucket, prefix=ARTIFACT_PREFIX)
        return self.artifacts

    @contextmanager
    def _heartbeat(self, row):
        stopped, lost = threading.Event(), []
        last = [float("-inf")]

        def renew():
            while not stopped.wait(LEASE_SECONDS / 3):
                try:
                    self.store.heartbeat(row)
                except Exception:
                    lost.append(True)
                    return

        def pulse():
            if lost:
                raise LeaseLost()
            if time.monotonic() - last[0] >= LEASE_SECONDS / 3:
                self.store.heartbeat(row)
                last[0] = time.monotonic()

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield pulse
        finally:
            stopped.set()
            thread.join(timeout=1)

    def _item(self, row, identity, path, title, status, reasons):
        return {"item_id": _digest(row["job_id"] + "/" + identity)[:32], "source_path": clean_text(path)[0][:1000],
                "title": clean_text(title)[0][:240], "status": status,
                "reason_codes": sorted(set(reasons)), "knowledge_id": None, "revision": None}

    def _reject(self, row, code):
        item = self._item(row, "original", row["filename"], row["filename"], "needs_review", [code])
        report = self._artifacts().put(_json({"rule_version": row["rule_version"], "item": item, "published": False}))
        self.store.save_item(row, item, metadata_ref=report)
        self.store.finish(row, "needs_review", code)

    def _package(self, row, receipt, directory, pulse):
        extension = Path(row["filename"]).suffix.lower()
        if extension not in {".zip", ".md", ".txt"}:
            raise ProcessingError("unsupported_file_type", 422)
        if receipt["byte_length"] > MAX_INPUT_BYTES:
            raise ProcessingError("processing_input_limit", 422)
        original = directory / ("original" + extension)
        with original.open("xb") as target:
            self._objects().verify(receipt, pulse, sink=target)
        pulse()
        if extension == ".zip":
            package = original
        else:
            if receipt["byte_length"] > MAX_MEMBER_BYTES:
                raise ProcessingError("processing_member_limit", 422)
            package = directory / "input.zip"
            with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(original, "source" + extension)
        try:
            with zipfile.ZipFile(package) as archive:
                members = archive.infolist()
                if (len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES
                        or any(item.file_size > MAX_MEMBER_BYTES or len(item.filename) > 1000 for item in members)):
                    raise ProcessingError("processing_archive_limit", 422)
        except (zipfile.BadZipFile, NotImplementedError):
            raise ProcessingError("invalid_archive", 422) from None
        return package

    def _process(self, row, directory, pulse):
        receipt = ReceiptStore.get(self.store, row["principal"], row["receipt_id"])
        if receipt["status"] != "verified":
            raise ProcessingError("receipt_not_verified", 422)
        package = self._package(row, receipt, directory, pulse)
        self.store.stage(row, "parsing")
        namespace = "receipt_" + _digest(_json([row["principal"], row["source_id"]]))[:48]
        source = {"path": package, "source_id": namespace, "package": row["filename"],
                  "sha256": receipt["sha256"], "contributor": "未确认", "text_encodings": {}}
        output = directory / "parsed"
        try:
            batch = process_batch([source], output)
        except (ValueError, UnicodeError, zipfile.BadZipFile):
            raise ProcessingError("processing_parse_rejected", 422) from None
        pulse()
        self.store.stage(row, "persisting")
        file_rows = [item for report in batch["packages"] for item in report["files"]]
        assets = batch["assets"]
        report_issues = {issue["code"] for report in batch["packages"] for issue in report["issues"] if issue["code"] == "archive_failed"}
        archive_sensitive = any(item["state"] == "credential_skipped" for item in file_rows)
        manifest_sensitive = any(Path(item["name"]).name == "交付清单.csv" and any(item.get("redactions", {}).values()) for item in file_rows)
        # Unknown sources are retained as unapproved drafts, never asserted as authorship or publication consent.
        for asset in assets:
            pulse()
            path = output / asset["body_file"]
            content = path.read_text(encoding="utf-8")
            if _digest(content) != asset["content_hash"]:
                raise ProcessingError("processing_artifact_changed", 422)
            metadata_path = output / asset["metadata_file"]
            metadata = json.loads(metadata_path.read_text())
            if _digest(metadata_path.read_bytes()) != asset["metadata_hash"]:
                raise ProcessingError("processing_artifact_changed", 422)
            reasons = {issue["code"] for issue in asset["issues"]} | report_issues
            if archive_sensitive:
                reasons.add("package_contains_credentials")
            if manifest_sensitive:
                reasons.add("manifest_redaction_review")
            if asset["state"] != "draft_ready":
                reasons.add("incomplete_content")
            if metadata.get("evidence", {}).get("source_complete") is not True:
                reasons.add("source_incomplete")
            entry = {**metadata, "knowledge_id": asset["knowledge_id"], "contributor": "未确认"}
            entry["evidence"] = {**entry.get("evidence", {}), "receipt_id": row["receipt_id"],
                                 "processing_rule": row["rule_version"], "review_status": "needs_review",
                                 "source_verified": False, "publication_approved": False}
            item = self._item(row, "asset/" + asset["candidate_id"], asset["source_path"], entry["title"],
                              "needs_review" if reasons else "draft", reasons or ["source_authorship_unverified"])
            try:
                previous = self.store.item_record(row["job_id"], item["item_id"], row["principal"])
            except ProcessingError as exc:
                if exc.status_code != 404:
                    raise
            else:
                if previous["status"] in {"published", "duplicate", "outdated", "archived"}:
                    continue
            item["knowledge_id"] = asset["knowledge_id"]
            content_ref = self._artifacts().put(content) if content.strip() and len(content) <= 500_000 else None
            if content_ref is None:
                item.update(status="needs_review", reason_codes=sorted(set(item["reason_codes"]) | {"preview_unavailable"}))
            if item["status"] == "draft":
                self.store.stage(row, "importing")
                if row["rule_version"] == RULE_VERSION:
                    self._govern_asset(row, asset, entry, content, item, content_ref)
                    continue
                if self.draft_objects is None:
                    self.draft_objects = KnowledgeObjects()
                draft_store = _FencedDraftStore(self.store, row, self.draft_objects)
                try:
                    draft = draft_store.import_draft({**entry, "content": content}, allow_new_revision=False)
                    if draft["status"] != "draft":
                        item.update(status="needs_review", reason_codes=["existing_asset_requires_review"])
                    else:
                        item["revision"] = draft["revision"]
                except ValueError:
                    item.update(status="needs_review", reason_codes=["existing_asset_or_invalid_draft"])
            pulse()
            metadata_ref = self._artifacts().put(_json({"entry": entry, "item": item, "content_ref": content_ref, "published": False}))
            self.store.save_item(row, item, content_ref, metadata_ref)
        represented_paths = {asset["source_path"] for asset in assets}
        for file in file_rows:
            pulse()
            # Asset rows already account for their main source; do not double-count them as archived files.
            if file["name"] in represented_paths:
                continue
            state, disposition = file["state"], file["disposition"]
            has_redactions = bool(file.get("redactions")) and any(file["redactions"].values())
            needs_review = state not in {"parsed", "script_not_ingested"} or disposition in {"unassigned_needs_review", "not_parsed"} or has_redactions
            if state == "script_not_ingested":
                needs_review = False
            reasons = ["redaction_review"] if has_redactions else [state if state != "parsed" else disposition]
            reasons.extend(issue["code"] for issue in file.get("issues", []))
            if file.get("error_code"):
                reasons.append(file["error_code"])
            item = self._item(row, "file/" + file["name"], file["name"], Path(file["name"]).name,
                              "needs_review" if needs_review else "archived", reasons)
            try:
                previous = self.store.item_record(row["job_id"], item["item_id"], row["principal"])
            except ProcessingError as exc:
                if exc.status_code != 404:
                    raise
            else:
                if previous["resolution_json"]:
                    continue
            metadata_ref = self._artifacts().put(_json({"item": item, "published": False}))
            self.store.save_item(row, item, metadata_ref=metadata_ref)
        if not assets and not file_rows:
            self._reject(row, "no_parseable_items")
            return
        result = self.store.get_job(row["job_id"], row["principal"])
        self.store.finish(row, "needs_review" if result["counts"]["needs_review"] else "completed")

    def run_once(self):
        self.store.enqueue_verified()
        row = self.store.claim()
        if row is None:
            return False
        try:
            with self._heartbeat(row) as pulse, tempfile.TemporaryDirectory(prefix="knowledge-processing-") as temporary:
                self._process(row, Path(temporary), pulse)
        except LeaseLost:
            pass
        except (ProcessingError, ReceiptError) as exc:
            try:
                if exc.status_code == 422:
                    self._reject(row, exc.code)
                else:
                    self.store.finish(row, "queued", "processing_storage_unavailable")
            except LeaseLost:
                pass
            except Exception:
                try:
                    self.store.finish(row, "queued", "processing_storage_unavailable")
                except LeaseLost:
                    pass
        except Exception:
            try:
                self.store.finish(row, "queued", "processing_unavailable")
            except LeaseLost:
                pass
        return True

    def get_item(self, job_id, item_id, principal):
        item = self.store.item_record(job_id, item_id, principal)
        if item["content_ref"] is None:
            raise ProcessingError("processing_preview_unavailable", 409)
        try:
            content = self._artifacts().get(json.loads(item["content_ref"]))
        except Exception:
            raise ProcessingError("processing_preview_unavailable", 503) from None
        with self.store._connection() as connection:
            current = self.store._execute(connection, "SELECT published_revision FROM knowledge_assets WHERE knowledge_id=?", (item["knowledge_id"],)).fetchone() if item["knowledge_id"] else None
        return {**self.store._public_item(item), "content": content,
                "currently_published": current is not None and current["published_revision"] == item["revision"]}
