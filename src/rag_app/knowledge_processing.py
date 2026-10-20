"""Durable receipt processing, conservative publication rules and exception handling."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import tempfile
import threading
import time
import uuid
import zipfile

from . import knowledge_item_assignments as assignments
from .knowledge_intake import SOURCE_PROTOCOL, process_batch
from .knowledge_governance import GovernanceStore
from .knowledge_objects import KnowledgeObjects
from .knowledge_receipts import LeaseLost, ReceiptError, ReceiptObjects, ReceiptStore, _utc, validate_principal
from .knowledge_store import AutomaticPublicationConflict, KnowledgeStore, _uploader_position, validate_role_id
from .knowledge_service import KnowledgeService
from .knowledge_text import clean_text


RULE_VERSION = "receipt-rules-v2"
LEGACY_RULE_VERSION = "receipt-rules-v1"
STATUSES = {"queued", "running", "completed", "needs_review", "failed"}
JOB_FILTERS = STATUSES | {"attention", "pending_publication"}
RECEIPT_STATUSES = ("awaiting_upload", "pending_verification", "verified", "rejected", "failed")
ITEM_STATUSES = ("draft", "needs_review", "archived", "duplicate", "outdated", "indexing", "published")
LEASE_SECONDS = 90
MAX_ATTEMPTS = 5
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 2000
ARTIFACT_PREFIX = "knowledge-receipts/processed"
UPLOADER_ROLE_REASONS = {"uploader_role_not_found", "uploader_role_ambiguous",
                         "uploader_position_is_department", "uploader_department_mismatch", "uploader_role_conflict"}
PUBLIC_BLOCKING_REASONS = UPLOADER_ROLE_REASONS | {
    "source_relation_review", "structured_delivery_required", "sharing_scope_unconfirmed", "reuse_information_incomplete",
    "restricted_material", "manifest_redaction_review", "source_base_revision_required",
    "source_base_revision_conflict", "publication_version_conflict", "attachment_manifest_unverified",
    "previously_withdrawn_revision", "legacy_attachment_manifest_unverified", "possible_content_conflict",
    "shared_asset_update_requires_review", "unsupported_file_type", "processing_input_limit",
    "processing_member_limit", "processing_archive_limit", "invalid_archive", "processing_retries_exhausted",
    "processing_storage_unavailable", "processing_unavailable", "source_authorship_unverified",
    "source_incomplete", "source_version_conflict", "incomplete_content", "package_contains_credentials",
    "preview_unavailable", "existing_asset_requires_review", "existing_asset_or_invalid_draft",
    "no_parseable_items", "redaction_review", "unresolved_reference", "script_dependency_not_provided",
    "unsupported_format", "missing_attachment", "sensitive_content", "missing_provenance", "parse_failed",
    "empty_content", "no_reusable_content", "processing_failed", "manual_review_required", "review_required",
    "source_ingestion_source_conflict", "referenced_material_unparsed", "skill_package_missing",
    "skill_package_invalid", "unknown_role_id", "role_id_required", "role_metadata_mismatch",
    "invalid_role_id", "role_metadata_incomplete", "processing_parse_rejected",
}


def missing_references(issues, source_path=""):
    """Bounded package paths are diagnostic data, never executable repair instructions."""
    result = []
    for issue in issues:
        value = issue.get("reference")
        if not isinstance(value, str) or not 1 <= len(value) <= 500:
            continue
        value = value.replace("\\", "/")
        if value.startswith("../"):
            value = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), value))
        if (len(value) > 500 or value.startswith(("/", "~")) or any(part in {"", ".", ".."} for part in value.split("/")) or ":" in value
                or re.search(r"[\x00-\x1f\x7f?#]", value)):
            continue
        cleaned, redactions = clean_text(value)
        if any(redactions.values()) or cleaned != value:
            continue
        if value not in result:
            result.append(value)
        if len(result) == 20:
            break
    return result


class UploadRoleMetadata:
    """Bounded, read-only cache of declarations, never source documents or metadata."""

    def __init__(self, read=None):
        self.read = read or self._read
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="upload-roles")
        self.lock = threading.Lock()
        self.cache, self.pending = OrderedDict(), {}

    @staticmethod
    def _read(ref):
        # Each reader owns its HTTP session; do not change the upload worker's client.
        reader = KnowledgeObjects(ReceiptObjects().bucket, prefix=ARTIFACT_PREFIX)
        reader.bucket.timeout = 3
        return reader.get(ref)

    @staticmethod
    def _label(value):
        return value.strip() if (isinstance(value, str) and value.strip() and len(value) <= 200
            and not re.search(r"[\x00-\x1f\x7f]", value)) else None

    def _load(self, key, ref, receipt_id, item_id):
        projection = None
        try:
            metadata = json.loads(self.read(ref))
            evidence = metadata["entry"]["evidence"]
            if metadata["item"]["item_id"] == item_id and evidence["receipt_id"] == receipt_id:
                declared = evidence if evidence.get("source_protocol") == SOURCE_PROTOCOL else {}
                projection = (self._label(declared.get("source_uploader_position")),
                              self._label(declared.get("source_uploader_department")))
        except Exception:
            pass  # Metadata failures must not hide the receipt or become permanent cache entries.
        with self.lock:
            self.pending.pop(key, None)
            if projection is not None:
                self.cache[key] = projection
                self.cache.move_to_end(key)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)

    def project(self, rows, namespace, *, timeout=2):
        deadline, keys, pending = time.monotonic() + timeout, {}, set()
        with self.lock:
            for row in rows:
                try:
                    ref = json.loads(row["metadata_ref"])
                    if (set(ref) != {"object_key", "sha256", "byte_length"}
                            or not isinstance(ref["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
                            or type(ref["byte_length"]) is not int or not 0 < ref["byte_length"] <= 128 * 1024
                            or ref["object_key"] != ARTIFACT_PREFIX + "/" + ref["sha256"] + ".txt"):
                        continue
                except (ValueError, TypeError, KeyError):
                    continue
                key = (namespace, ref["sha256"], ref["byte_length"], row["receipt_id"], row["item_id"])
                keys[row["item_id"]] = key
                if key in self.cache:
                    self.cache.move_to_end(key)
                else:
                    if key not in self.pending and len(self.pending) < 16:
                        self.pending[key] = self.executor.submit(self._load, key, ref, row["receipt_id"], row["item_id"])
                    if key in self.pending:
                        pending.add(self.pending[key])
        if pending:
            # A shared pool continues bounded reads after this request's budget;
            # a per-request pool/context manager would block here on shutdown.
            wait(pending, timeout=max(0, deadline - time.monotonic()))
        with self.lock:
            return {item: self.cache[key] for item, key in keys.items() if key in self.cache}


UPLOAD_ROLE_METADATA = UploadRoleMetadata()


class ProcessingError(RuntimeError):
    def __init__(self, code, status_code=503):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_uploader_roles (
      principal TEXT PRIMARY KEY, role_id TEXT NOT NULL, version BIGINT NOT NULL,
      actor TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL, job_id TEXT NOT NULL,
      requeued INTEGER NOT NULL DEFAULT 0)""",
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

    display_metadata = None  # Enabled only for portal reads, never during upload/worker processing.

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
            for name, definition in (("version", "INTEGER NOT NULL DEFAULT 1"), ("resolution_json", "TEXT"), ("publication_json", "TEXT"), ("repair_json", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE knowledge_processing_items ADD COLUMN {name} {definition}")
        GovernanceStore(KnowledgeStore(self.path, database_url=self.database_url)).initialize()
        self.initialize_role_assignments(apply=True)

    def _event(self, connection, job_id, event_type, reason=None, actor=None):
        self._execute(connection, """INSERT INTO knowledge_processing_events
            (event_id,job_id,event_type,reason_code,actor,created_at) VALUES (?,?,?,?,?,?)""",
            (uuid.uuid4().hex, job_id, event_type, reason, actor, self.clock()))

    @staticmethod
    def _public_uploader_role(row, *, requeued=False):
        return {"role": {key: row[key] for key in ("role_id", "department", "name")} if row else None,
                "version": row["version"] if row else 0,
                "updated_at": _utc(row["updated_at"]) if row else None, "requeued": requeued}

    def initialize_role_assignments(self, *, apply=False):
        return assignments.initialize(self, apply=apply)

    def migrate_legacy_item_roles(self, *, apply=False):
        return assignments.migration(self, apply=apply)

    def get_item_uploader_role(self, job_id, item_id):
        return assignments.get_role(self, job_id, item_id)

    def set_item_uploader_role(self, job_id, item_id, role_id, expected_version, *, actor,
                               remember_rule=False, source_action="auto"):
        return assignments.set_role(self, job_id, item_id, role_id, expected_version,
                                    actor=actor, remember_rule=remember_rule, source_action=source_action)

    def get_uploader_role(self, job_id):
        result = self.get_item_uploader_role(job_id, assignments.single_item(self, job_id))
        return {key: result[key] for key in ("role", "version", "updated_at", "requeued")}

    def set_uploader_role(self, job_id, role_id, expected_version, *, actor):
        result = self.set_item_uploader_role(job_id, assignments.single_item(self, job_id),
                                            role_id, expected_version, actor=actor)
        return {key: result[key] for key in ("role", "version", "updated_at", "requeued")}

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
                (item_id,job_id,source_path,title,status,reason_codes,knowledge_id,revision,content_ref,metadata_ref,repair_json,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                status=excluded.status,reason_codes=excluded.reason_codes,knowledge_id=excluded.knowledge_id,
                revision=excluded.revision,content_ref=excluded.content_ref,metadata_ref=excluded.metadata_ref,
                repair_json=COALESCE(excluded.repair_json,knowledge_processing_items.repair_json),
                version=knowledge_processing_items.version+1,updated_at=excluded.updated_at""",
                (item["item_id"], row["job_id"], item["source_path"], item["title"], item["status"], _json(item["reason_codes"]),
                 item.get("knowledge_id"), item.get("revision"), _json(content_ref) if content_ref else None,
                 _json(metadata_ref) if metadata_ref else None,
                 _json({"missing_references": item["missing_references"],
                        **({"replacement_recheck": True} if item.get("replacement_recheck") else {})})
                 if "missing_references" in item else None, self.clock()))
            self._event(connection, row["job_id"], "item_" + item["status"], item["reason_codes"][0] if item["reason_codes"] else None)

    def finish(self, row, status, code=None):
        if status not in {"completed", "needs_review", "queued"}:
            raise ProcessingError("invalid_processing_status", 400)
        if status == "queued" and row["attempts"] >= MAX_ATTEMPTS:
            status = "failed"
        available = self.clock() + min(30 * 2 ** (row["attempts"] - 1), 300) if status == "queued" else 0
        with self._connection(write=True) as connection:
            self.fence(connection, row)
            if status in {"completed", "needs_review"}:
                for item in self._execute(connection, "SELECT i.item_id,i.repair_json FROM knowledge_processing_items i WHERE i.job_id=? AND "
                                          + self._replacement_pending(connection), (row["job_id"],)).fetchall():
                    repair = json.loads(item["repair_json"])
                    repair.pop("replacement_recheck", None)
                    self._execute(connection, "UPDATE knowledge_processing_items SET repair_json=? WHERE item_id=?",
                                  (_json(repair), item["item_id"]))
            self._execute(connection, """UPDATE knowledge_processing_jobs SET status=?,stage=?,error_code=?,
                available_at=?,lease_token=NULL,lease_until=0,updated_at=? WHERE job_id=?""",
                (status, status, code, available, self.clock(), row["job_id"]))
            self._event(connection, row["job_id"], status, code)

    @staticmethod
    def _summary(row):
        return {**{key: row[key] for key in ("job_id", "receipt_id", "principal", "source_id", "filename", "rule_version", "status", "stage", "attempts", "error_code")},
                "display_title": row.get("display_title"), "material_count": row.get("material_count", 0),
                "uploader_positions": row.get("uploader_positions", []), "uploader_departments": row.get("uploader_departments", []),
                "role_assignments": row.get("role_assignments", []),
                "role_assignments_total": row.get("role_assignments_total", 0),
                "role_assignments_updated_at": row.get("role_assignments_updated_at"),
                "attention_required": bool(row.get("attention_required", row["status"] in {"needs_review", "failed"})),
                "blocking_reasons": row.get("blocking_reasons", []),
                "blocking_reasons_truncated": row.get("blocking_reasons_truncated", False),
                "created_at": _utc(row["created_at"]), "updated_at": _utc(row["updated_at"]),
                "retryable": row["status"] == "failed", "published": row.get("published_count", 0) > 0,
                "counts": {state: row.get(state + "_count", 0) for state in ITEM_STATUSES}}

    def _blocking_reasons(self, connection, jobs):
        # One owner-filtered query covers all current items, independent of the
        # detail page. Historical events and completed/archived items are not blockers.
        reasons = {row["job_id"]: {row["error_code"]} for row in map(dict, jobs)
                   if row.get("attention_required", row["status"] in {"needs_review", "failed"})}
        if not reasons:
            return {}
        rows = self._execute(connection, """SELECT DISTINCT job_id,reason_codes,a.resume_pending,a.source_action,"""
            + self._replacement_pending(connection) + """ AS replacement_recheck
            FROM knowledge_processing_items i LEFT JOIN """ + self._assignment_table(connection) + """ a ON a.item_id=i.item_id
            WHERE i.status='needs_review' AND i.job_id IN (""" + ",".join("?" for _ in reasons) + ")", tuple(reasons)).fetchall()
        for row in rows:
            try:
                codes = self._current_reason_codes(row)
            except (TypeError, ValueError):
                codes = None
            if isinstance(codes, list):
                reasons[row["job_id"]].update(code if isinstance(code, str) else "manual_review_required" for code in codes)
            else:
                reasons[row["job_id"]].add("manual_review_required")
        result = {}
        for job_id, codes in reasons.items():
            # Only public fixed codes leave the summary boundary, never a parser's
            # raw error or accidentally stored source text. Unknowns remain visible.
            safe = sorted({code if code in PUBLIC_BLOCKING_REASONS else "manual_review_required"
                           for code in codes if code is not None} or {"manual_review_required"})
            result[job_id] = {"blocking_reasons": safe[:32], "blocking_reasons_truncated": len(safe) > 32}
        return result

    def _display_materials(self, connection, job_ids):
        # IDs must come from the caller's already owner-filtered rows. One query
        # covers the page; titles never require reading bodies or original files.
        job_ids = sorted({value for value in job_ids if value is not None})
        if not job_ids:
            return {}
        rows = self._execute(connection, """SELECT i.job_id,i.title,i.source_path,i.reason_codes,j.receipt_id,r.record_json,
                i.item_id,i.metadata_ref
            FROM knowledge_processing_items i JOIN knowledge_processing_jobs j ON j.job_id=i.job_id
            LEFT JOIN knowledge_submissions s ON s.item_id=i.item_id AND s.receipt_id=j.receipt_id
                AND s.principal=j.principal AND s.disposition IN ('new','new_revision')
                AND s.owned_knowledge_id=s.knowledge_id
            LEFT JOIN knowledge_revisions r ON r.knowledge_id=s.knowledge_id AND r.revision=s.revision
                AND r.knowledge_id=i.knowledge_id AND r.revision=i.revision
            WHERE i.job_id IN (""" + ",".join("?" for _ in job_ids) + """)
            AND (i.knowledge_id IS NOT NULL OR i.content_ref IS NOT NULL)
            ORDER BY i.source_path,i.item_id""", job_ids).fetchall()
        result, fallback = {}, []
        for row in rows:
            title = row["title"].strip()
            if (not title or row["source_path"].replace("\\", "/").rsplit("/", 1)[-1] == "交付清单.csv"
                    or "source_manifest" in json.loads(row["reason_codes"])):
                continue
            material = result.setdefault(row["job_id"], {"display_title": title, "material_count": 0,
                "uploader_positions": [], "uploader_departments": []})
            material["material_count"] += 1
            position = None
            if row["record_json"]:
                record = json.loads(row["record_json"])
                # A duplicate can point to somebody else's revision. Only its own
                # authenticated submission and receipt may supply this declaration.
                position = _uploader_position(record, {(record["knowledge_id"], record["revision"]): row["receipt_id"]})
                if position:
                    department = record.get("evidence", {}).get("source_uploader_department")
                    for key, value in (("uploader_positions", position), ("uploader_departments", department)):
                        if (isinstance(value, str) and value.strip() and len(value) <= 200
                                and not re.search(r"[\x00-\x1f\x7f]", value) and value.strip() not in material[key]):
                            material[key].append(value.strip())
            if not position and row["metadata_ref"]:
                fallback.append(row)
        if fallback and self.display_metadata is not None:
            namespace = _digest(_json([self.database_url or str(self.path.resolve()), ARTIFACT_PREFIX,
                os.getenv("KNOWLEDGE_RECEIPTS_OSS_ENDPOINT"), os.getenv("KNOWLEDGE_RECEIPTS_OSS_BUCKET")]))
            declarations = self.display_metadata.project(fallback, namespace)
            for row in fallback:
                for key, value in zip(("uploader_positions", "uploader_departments"), declarations.get(row["item_id"], ())):
                    values = result[row["job_id"]][key]
                    if value and value not in values:
                        values.append(value)
        return result

    def _assignment_table(self, connection):
        present = (connection.execute("SELECT to_regclass('knowledge_item_role_assignments') IS NOT NULL AS ready").fetchone()["ready"]
            if self.database_url else connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_item_role_assignments'").fetchone() is not None)
        return ("knowledge_item_role_assignments" if present else
            "(SELECT NULL AS item_id,NULL AS role_id,NULL AS confirmation_source,CAST(NULL AS DOUBLE PRECISION) AS updated_at,0 AS resume_pending,NULL AS source_action)")

    def _replacement_pending(self, connection):
        present = (connection.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='knowledge_processing_items' AND column_name='repair_json'").fetchone()
            if self.database_url else any(row["name"] == "repair_json" for row in connection.execute("PRAGMA table_info(knowledge_processing_items)")))
        if not present:
            return "0=1"
        return ("COALESCE(i.repair_json::jsonb->>'replacement_recheck','false')='true'" if self.database_url
                else "COALESCE(json_extract(i.repair_json,'$.replacement_recheck'),0)=1")

    def _attention_condition(self, connection):
        table = "(SELECT item_id FROM " + self._assignment_table(connection) + """ assignments WHERE resume_pending=1
            UNION SELECT i.item_id FROM knowledge_processing_items i WHERE """ + self._replacement_pending(connection) + ")"
        # A resumed item must not hide a sibling that still needs a person's help.
        return """(j.status IN ('needs_review','failed') OR (j.status IN ('queued','running')
            AND EXISTS (SELECT 1 FROM knowledge_processing_items resumed JOIN """ + table + """ a ON a.item_id=resumed.item_id
                WHERE resumed.job_id=j.job_id)
            AND EXISTS (SELECT 1 FROM knowledge_processing_items sibling WHERE sibling.job_id=j.job_id
                AND sibling.status='needs_review' AND NOT EXISTS (SELECT 1 FROM """ + table + """ pending
                    WHERE pending.item_id=sibling.item_id))))"""

    @staticmethod
    def _current_reason_codes(item):
        item = dict(item)
        codes = json.loads(item["reason_codes"])
        if item.get("replacement_recheck") and isinstance(codes, list):
            codes = [code for code in codes if code != "source_relation_review"]
        if item.get("resume_pending") and isinstance(codes, list):
            return [code for code in codes if not isinstance(code, str) or
                (code not in UPLOADER_ROLE_REASONS and not (code == 'source_relation_review' and item.get('source_action') == 'new'))]
        return codes

    def _role_joins(self, connection):
        assignment_table = self._assignment_table(connection)
        revision_role = "r.record_json::jsonb->>'role_id'" if self.database_url else "json_extract(r.record_json, '$.role_id')"
        # A material with a persisted revision keeps that revision's actual role.
        return "LEFT JOIN " + assignment_table + """ a ON a.item_id=i.item_id
            LEFT JOIN knowledge_revisions r ON r.knowledge_id=i.knowledge_id AND r.revision=i.revision
            LEFT JOIN knowledge_roles role ON role.role_id=CASE WHEN i.revision IS NOT NULL
                THEN """ + revision_role + " ELSE a.role_id END"

    def _role_summaries(self, connection, job_ids, item_ids=None):
        job_ids = sorted({value for value in job_ids if value is not None})
        if not job_ids or item_ids == []:
            return {}
        condition = "i.job_id IN (" + ",".join("?" for _ in job_ids) + ")"
        args = list(job_ids)
        if item_ids is not None:
            condition += " AND i.item_id IN (" + ",".join("?" for _ in item_ids) + ")"
            args.extend(item_ids)
        rows = self._execute(connection, """SELECT * FROM (SELECT i.job_id,i.item_id,i.title,role.role_id,role.department,role.name,
            CASE WHEN i.revision IS NOT NULL THEN 'material_revision' ELSE a.confirmation_source END AS confirmation_source,
            CASE WHEN i.revision IS NOT NULL THEN i.updated_at ELSE a.updated_at END AS updated_at,
            COUNT(*) OVER (PARTITION BY i.job_id) AS role_total,
            MAX(CASE WHEN i.revision IS NOT NULL THEN i.updated_at ELSE a.updated_at END) OVER (PARTITION BY i.job_id) AS roles_updated_at,
            ROW_NUMBER() OVER (PARTITION BY i.job_id ORDER BY i.source_path,i.item_id) AS role_rank
            FROM knowledge_processing_items i """ + self._role_joins(connection) + " WHERE " + condition
            + " AND role.role_id IS NOT NULL) roles" + (" WHERE role_rank<=5" if item_ids is None else "")
            + " ORDER BY job_id,role_rank", args).fetchall()
        result = {}
        for row in rows:
            summary = result.setdefault(row["job_id"], {"role_assignments": [], "role_assignments_total": row["role_total"],
                "role_assignments_updated_at": _utc(row["roles_updated_at"])})
            summary["role_assignments"].append({"item_id": row["item_id"], "title": row["title"][:200],
                "role": {key: row[key] for key in ("role_id", "department", "name")},
                "confirmation_source": row["confirmation_source"], "updated_at": _utc(row["updated_at"])})
        return result

    def _list_filters(self, *, q=None, department=None, role_id=None, processing_status=None,
                      created_from=None, created_to=None, receipt=False):
        conditions, args = [], []
        for value in (q, department):
            if value is not None and (not isinstance(value, str) or len(value) > 200 or re.search(r"[\x00-\x1f\x7f]", value)):
                raise ProcessingError("invalid_processing_query", 400)
        if role_id is not None and (not isinstance(role_id, str) or not re.fullmatch(r"role_[0-9a-f]{24}", role_id)):
            raise ProcessingError("invalid_processing_query", 400)
        if processing_status is not None and processing_status not in STATUSES | ({"none"} if receipt else set()):
            raise ProcessingError("invalid_processing_query", 400)
        dates = []
        for value in (created_from, created_to):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None
                if parsed is not None and parsed.utcoffset() is None:
                    raise ValueError("timezone required")
                dates.append(parsed.timestamp() if parsed is not None else None)
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise ProcessingError("invalid_processing_query", 400) from None
        if dates[0] is not None and dates[1] is not None and dates[0] >= dates[1]:
            raise ProcessingError("invalid_processing_query", 400)
        owner = "r" if receipt else "j"
        if q and q.strip():
            # Escape wildcards so a filename containing % or _ is searched literally.
            pattern = "%" + q.strip().lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            conditions.append("(LOWER(" + owner + ".filename) LIKE ? ESCAPE '!' OR EXISTS (SELECT 1 FROM knowledge_processing_items search_item WHERE search_item.job_id=j.job_id AND LOWER(search_item.title) LIKE ? ESCAPE '!'))")
            args.extend((pattern, pattern))
        if department or role_id:
            role_conditions, role_args = [], []
            for name, value in (("department", department), ("role_id", role_id)):
                if value:
                    role_conditions.append("role." + name + "=?")
                    role_args.append(value)
            with self._connection() as connection:
                joins = self._role_joins(connection)
            conditions.append("EXISTS (SELECT 1 FROM knowledge_processing_items i " + joins
                + " WHERE i.job_id=j.job_id AND " + " AND ".join(role_conditions) + ")")
            args.extend(role_args)
        if processing_status == "none":
            conditions.append("j.job_id IS NULL")
        elif processing_status:
            conditions.append("j.status=?")
            args.append(processing_status)
        for date, operation in zip(dates, (">=", "<")):
            if date is not None:
                conditions.append(owner + ".created_at" + operation + "?")
                args.append(date)
        return conditions, args

    def list_jobs(self, principal, status=None, limit=50, offset=0, **filters):
        if status is not None and status not in JOB_FILTERS or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ProcessingError("invalid_processing_query", 400)
        conditions, args = ["1=1"], []
        if principal is not None:
            conditions.append("j.principal=?")
            args.append(principal)
        if status == "pending_publication":
            conditions.append("EXISTS (SELECT 1 FROM knowledge_processing_items pending WHERE pending.job_id=j.job_id AND pending.status IN ('draft','indexing'))")
        elif status and status != "attention":
            conditions.append("j.status=?")
            args.append(status)
        extra, values = self._list_filters(**filters)
        conditions.extend(extra)
        args.extend(values)
        where = " AND ".join(conditions)
        counts = ",".join("(SELECT COUNT(*) FROM knowledge_processing_items i WHERE i.job_id=j.job_id AND i.status='" + state + "') AS " + state + "_count" for state in ITEM_STATUSES)
        with self._connection() as connection:
            attention = self._attention_condition(connection)
            if status == "attention":
                where += " AND " + attention
            total = self._execute(connection, "SELECT COUNT(*) AS n FROM knowledge_processing_jobs j WHERE " + where, args).fetchone()["n"]
            rows = self._execute(connection, "SELECT j.*," + attention + " AS attention_required," + counts + " FROM knowledge_processing_jobs j WHERE " + where + " ORDER BY j.created_at DESC,j.job_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            materials = self._display_materials(connection, (row["job_id"] for row in rows))
            blocking = self._blocking_reasons(connection, rows)
            roles = self._role_summaries(connection, (row["job_id"] for row in rows))
        return {"items": [self._summary({**dict(row), **materials.get(row["job_id"], {}),
            **blocking.get(row["job_id"], {}), **roles.get(row["job_id"], {})}) for row in rows], "total": total}

    def _receipt_counts(self, connection, principal):
        where, args = (" WHERE principal=?", (principal,)) if principal is not None else ("", ())
        rows = self._execute(connection, "SELECT status,COUNT(*) AS n FROM knowledge_upload_receipts" + where + " GROUP BY status", args).fetchall()
        counts = dict.fromkeys(RECEIPT_STATUSES, 0)
        for row in rows:
            state = "pending_verification" if row["status"] == "verifying" else row["status"]
            if state in counts:
                counts[state] += row["n"]
        return {"total": sum(row["n"] for row in rows), **counts}

    def list_receipts(self, principal, status=None, limit=20, offset=0, **filters):
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
        extra, values = self._list_filters(receipt=True, **filters)
        conditions.extend(extra)
        args.extend(values)
        where = " AND ".join(conditions)
        # Associate the latest owner-matching job before filtering or pagination.
        joined = """ FROM knowledge_upload_receipts r LEFT JOIN knowledge_processing_jobs j ON j.job_id=(
            SELECT p.job_id FROM knowledge_processing_jobs p WHERE p.receipt_id=r.receipt_id AND p.principal=r.principal
            ORDER BY p.created_at DESC,p.job_id DESC LIMIT 1) WHERE """ + where
        item_counts = ",".join("(SELECT COUNT(*) FROM knowledge_processing_items i WHERE i.job_id=j.job_id AND i.status='" + state + "') AS " + state + "_count" for state in ITEM_STATUSES)
        with self._connection() as connection:
            counts = self._receipt_counts(connection, principal)
            attention = self._attention_condition(connection)
            total = self._execute(connection, "SELECT COUNT(*) AS n" + joined, args).fetchone()["n"]
            rows = self._execute(connection, """SELECT r.receipt_id,r.filename,r.byte_length,
                CASE WHEN r.status='verifying' THEN 'pending_verification' ELSE r.status END AS status,r.error_code,
                r.created_at,r.updated_at,j.job_id,j.status AS job_status,j.stage AS job_stage,
                j.updated_at AS job_updated_at,j.error_code AS job_error_code,""" + attention + " AS attention_required," + item_counts + joined
                + " ORDER BY r.created_at DESC,r.receipt_id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            materials = self._display_materials(connection, (row["job_id"] for row in rows))
            roles = self._role_summaries(connection, (row["job_id"] for row in rows))
            blocking = self._blocking_reasons(connection, [{"job_id": row["job_id"], "status": row["job_status"],
                "error_code": row["job_error_code"], "attention_required": row["attention_required"]} for row in rows if row["job_id"]])
        return {"items": [{**dict(row), **materials.get(row["job_id"], {"display_title": None, "material_count": 0}),
            **roles.get(row["job_id"], {}),
            "counts": {state: row[state + "_count"] for state in ITEM_STATUSES},
            **blocking.get(row["job_id"], {"blocking_reasons": [], "blocking_reasons_truncated": False})}
            for row in rows], "total": total, "receipt_counts": counts}

    def get_job(self, job_id, principal, limit=100, item_offset=0, event_offset=0):
        if (type(limit) is not int or not 1 <= limit <= 100
                or any(type(value) is not int or not 0 <= value <= 1_000_000 for value in (item_offset, event_offset))):
            raise ProcessingError("invalid_processing_query", 400)
        with self._connection() as connection:
            row = self._job(connection, job_id, principal)
            counts = self._execute(connection, "SELECT status,COUNT(*) AS n FROM knowledge_processing_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()
            event_total = self._execute(connection, "SELECT COUNT(*) AS n FROM knowledge_processing_events WHERE job_id=?", (job_id,)).fetchone()["n"]
            items = self._execute(connection, "SELECT i.*,a.resume_pending,a.source_action," + self._replacement_pending(connection)
                + " AS replacement_recheck FROM knowledge_processing_items i LEFT JOIN "
                + self._assignment_table(connection) + " a ON a.item_id=i.item_id WHERE i.job_id=? ORDER BY i.source_path,i.item_id LIMIT ? OFFSET ?", (job_id, limit, item_offset)).fetchall()
            row["attention_required"] = self._execute(connection, "SELECT " + self._attention_condition(connection)
                + " AS required FROM knowledge_processing_jobs j WHERE j.job_id=?", (job_id,)).fetchone()["required"]
            events = self._execute(connection, "SELECT * FROM knowledge_processing_events WHERE job_id=? ORDER BY created_at,event_id LIMIT ? OFFSET ?", (job_id, limit, event_offset)).fetchall()
            row.update(self._display_materials(connection, (job_id,)).get(job_id, {}))
            row.update(self._blocking_reasons(connection, [row]).get(job_id, {}))
            row.update(self._role_summaries(connection, (job_id,)).get(job_id, {}))
            page_roles = self._role_summaries(connection, (job_id,), [item["item_id"] for item in items]).get(job_id, {})
            item_roles = {role["item_id"]: role for role in page_roles.get("role_assignments", [])}
        row.update({item["status"] + "_count": item["n"] for item in counts})
        return {**self._summary(row), "items": [{**self._public_item(item), "role_assignment": item_roles.get(item["item_id"]),
            "current_reason_codes": self._current_reason_codes(item),
            "rechecking": bool((item["resume_pending"] or item["replacement_recheck"]) and row["status"] in {"queued", "running"})} for item in items],
                "item_total": sum(item["n"] for item in counts), "event_total": event_total,
                "item_offset": item_offset, "event_offset": event_offset, "limit": limit,
                "events": [{**{key: event[key] for key in ("event_id", "event_type", "reason_code", "actor")}, "created_at": _utc(event["created_at"])} for event in events]}

    @staticmethod
    def _public_item(item):
        item = dict(item)
        resolution = json.loads(item["resolution_json"]) if item["resolution_json"] else {}
        repair = json.loads(item["repair_json"]) if item.get("repair_json") else {}
        return {**{key: item[key] for key in ("item_id", "source_path", "title", "status", "knowledge_id", "revision")},
                "knowledge_id": item["knowledge_id"] if item["revision"] is not None else None,
                "reason_codes": json.loads(item["reason_codes"]), "has_preview": item["content_ref"] is not None,
                "published": item["status"] == "published", "version": item["version"],
                "publication": json.loads(item["publication_json"]) if item["publication_json"] else None,
                "missing_references": repair.get("missing_references", []) if item["status"] == "needs_review" else [],
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
            attention = self._execute(connection, "SELECT COUNT(*) AS n FROM knowledge_processing_jobs j WHERE "
                + self._attention_condition(connection) + (" AND j.principal=?" if principal is not None else ""), params).fetchone()["n"]
        keys = {state: ("review_items" if state == "needs_review" else state + "_items") for state in ITEM_STATUSES}
        result = {"receipts": receipt_counts["total"], "receipt_counts": receipt_counts, "attention_jobs": attention,
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
                if item["publication_json"]:
                    raise ProcessingError("processing_resolution_conflict", 409)
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
                else:
                    target_job = self._job(connection, replacement_job_id, row["principal"], lock=True)
                    if target_job["status"] == "running":
                        raise ProcessingError("processing_resolution_conflict", 409)
                    if target_job["status"] in {"needs_review", "queued", "failed", "completed"}:
                        binding = (self._execute(connection, "SELECT * FROM knowledge_item_source_bindings WHERE item_id=?", (item_id,)).fetchone()
                                   if assignments.ready(self, connection) else None)
                        if target_job["status"] == "completed" and not (binding and binding["canonical_identity"]):
                            raise ProcessingError("processing_resolution_conflict", 409)
                        if binding and binding["canonical_identity"]:
                            targets = self._execute(connection, """SELECT i.*,b.canonical_identity,a.source_action,
                                (SELECT COUNT(*) FROM knowledge_submissions s WHERE s.item_id=i.item_id) AS submissions
                                FROM knowledge_processing_items i JOIN knowledge_item_source_bindings b ON b.item_id=i.item_id AND b.job_id=i.job_id
                                LEFT JOIN knowledge_item_role_assignments a ON a.item_id=i.item_id
                                WHERE i.job_id=? AND b.parser_identity=? AND b.principal=? AND b.external_source_id=?""",
                                (replacement_job_id, binding["parser_identity"], row["principal"], row["source_id"])).fetchall()
                            if target_job["status"] == "completed":
                                if (len(targets) != 1 or targets[0]["status"] not in {"published", "duplicate"}
                                        or targets[0]["canonical_identity"] != binding["canonical_identity"]):
                                    raise ProcessingError("processing_resolution_conflict", 409)
                            elif targets or target_job["status"] != "queued":
                                if (len(targets) != 1 or targets[0]["status"] != "needs_review"
                                        or targets[0]["revision"] is not None or targets[0]["publication_json"]
                                        or targets[0]["submissions"] or targets[0]["source_action"] == "new"
                                        or targets[0]["canonical_identity"] not in (None, binding["canonical_identity"])):
                                    raise ProcessingError("processing_resolution_conflict", 409)
                                target = targets[0]
                                repair = json.loads(target["repair_json"]) if target["repair_json"] else {}
                                repair["replacement_recheck"] = True
                                self._execute(connection, "UPDATE knowledge_processing_items SET repair_json=?,version=version+1,updated_at=? WHERE item_id=?",
                                              (_json(repair), self.clock(), target["item_id"]))
                                self._execute(connection, """UPDATE knowledge_processing_jobs
                                    SET status='queued',stage='queued',attempts=0,available_at=0,error_code=NULL,updated_at=? WHERE job_id=?""",
                                    (self.clock(), replacement_job_id))
                                self._event(connection, replacement_job_id, "retried", "replacement_submission", actor)
                resolution.update(replacement_receipt_id=replacement_receipt_id, replacement_job_id=replacement_job_id)
            changed = self._execute(connection, """UPDATE knowledge_processing_items
                SET status='archived',reason_codes=?,resolution_json=?,version=version+1,updated_at=?
                WHERE item_id=? AND job_id=? AND status='needs_review' AND version=?""",
                (_json(["replaced_by_submission" if action == "replace" else "reviewer_archived"]), _json(resolution),
                 self.clock(), item_id, job_id, expected_version))
            if changed.rowcount != 1:
                raise ProcessingError("processing_resolution_conflict", 409)
            self._event(connection, job_id, "item_resolved", "replaced_by_submission" if action == "replace" else "reviewer_archived", actor)
            remaining = self._execute(connection, "SELECT 1 FROM knowledge_processing_items WHERE job_id=? AND status IN ('needs_review','indexing') LIMIT 1", (job_id,)).fetchone()
            if not remaining and row["status"] in {"needs_review", "failed"}:
                self._execute(connection, "UPDATE knowledge_processing_jobs SET status='completed',stage='completed',error_code=NULL,updated_at=? WHERE job_id=? AND status=?", (self.clock(), job_id, row["status"]))
        return self._public_item(self.item_record(job_id, item_id, principal))

    def retry(self, job_id, principal, actor=None, *, reparse_manifest=False, expected_receipt_id=None,
              manifest_compatibility=None):
        if (type(reparse_manifest) is not bool or expected_receipt_id is not None and not reparse_manifest
                or manifest_compatibility not in (None, "revision_label")
                or manifest_compatibility is not None and not reparse_manifest):
            raise ProcessingError("processing_retry_not_allowed", 409)
        with self._connection(write=True) as connection:
            row = self._job(connection, job_id, principal, lock=True)
            if reparse_manifest:
                # Operator-only compatibility repair; the HTTP retry route never supplies this opt-in.
                if (row["status"] != "needs_review" or row["rule_version"] != RULE_VERSION
                        or row["receipt_id"] != expected_receipt_id or row["lease_token"] is not None
                        or row["lease_until"] != 0 or not isinstance(actor, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", actor)):
                    raise ProcessingError("processing_retry_not_allowed", 409)
                receipt = self._execute(connection, """SELECT 1 FROM knowledge_upload_receipts
                    WHERE receipt_id=? AND principal=? AND status='verified'""",
                    (expected_receipt_id, row["principal"])).fetchone()
                items = self._execute(connection, "SELECT * FROM knowledge_processing_items WHERE job_id=?", (job_id,)).fetchall()
                manifests = [item for item in items if Path(item["source_path"]).name == "交付清单.csv"]
                prior = self._execute(connection, """SELECT reason_code FROM knowledge_processing_events
                    WHERE job_id=? AND event_type='retried' AND reason_code IN
                    ('source_manifest_compatibility','source_revision_label_compatibility')""", (job_id,)).fetchall()
                reasons = [event["reason_code"] for event in prior]
                label_repair = manifest_compatibility == "revision_label"
                if label_repair and (reasons != ["source_manifest_compatibility"] or len(items) != 2):
                    raise ProcessingError("processing_retry_not_allowed", 409)
                if (receipt is None or len(manifests) != 1 or len(items) < 2
                        or any(item["status"] != "needs_review" or any(item[key] is not None for key in (
                            "knowledge_id", "revision", "content_ref", "publication_json", "resolution_json")) for item in items)
                        or any(set(json.loads(item["reason_codes"])) != (
                            {"parse_failed", "source_manifest_invalid"} if not label_repair and item["item_id"] == manifests[0]["item_id"]
                            else {"unassigned_needs_review"}) for item in items)):
                    raise ProcessingError("processing_retry_not_allowed", 409)
                # File-level failure rows have different IDs from successfully parsed assets.
                # Keep their existing audit events; discard only this pinned job's empty outputs.
                self._execute(connection, "DELETE FROM knowledge_processing_items WHERE job_id=?", (job_id,))
            result = self._execute(connection, """UPDATE knowledge_processing_jobs SET status='queued',stage='queued',
                attempts=0,available_at=0,error_code=NULL,updated_at=? WHERE job_id=? AND status=?""",
                (self.clock(), job_id, "needs_review" if reparse_manifest else "failed"))
            if result.rowcount != 1:
                raise ProcessingError("processing_retry_not_allowed", 409)
            reason = ("source_revision_label_compatibility" if manifest_compatibility == "revision_label"
                      else "source_manifest_compatibility") if reparse_manifest else None
            self._event(connection, job_id, "retried", reason,
                        actor=actor or row["principal"])
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
                 auto_publish=None, knowledge_index=None, files=None):
        self.store = store if store is not None else ProcessingStore()
        self.objects, self.artifacts, self.draft_objects = objects, artifacts, draft_objects
        self.auto_publish = os.getenv("KNOWLEDGE_AUTO_PUBLISH_ENABLED") == "1" if auto_publish is None else auto_publish
        self.knowledge_index = knowledge_index
        self.files = files

    @staticmethod
    def _automatic_reasons(entry, content):
        evidence = entry.get("evidence", {})
        reasons = set()
        sop = evidence.get("source_protocol") == "company-role-sop-v1" and entry.get("kind") == "process" and entry.get("role_id")
        if evidence.get("source_protocol") != SOURCE_PROTOCOL and not sop:
            reasons.add("structured_delivery_required")
        if evidence.get("source_sharing") != "全员":
            reasons.add("sharing_scope_unconfirmed")
        for field in ("source_purpose", "source_evidence_status", "source_locator", "source_audience", "source_inputs",
                      "source_outputs", "source_dependencies", "source_boundaries"):
            if not sop and str(evidence.get(field, "")).strip() in {"", "未取得", "未提供", "未确认", "未知", "待补充", "待补", "待确认", "待核验"}:
                reasons.add("reuse_information_incomplete")
        # These declarations describe a reusable delivery, not a claim that the
        # submitter authored it or that its business outcome has been verified.
        if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:密级\s*[:：]\s*(?:机密|秘密|保密)|confidential\s*:\s*true)\b", content):
            reasons.add("restricted_material")
        return reasons

    def _publication_guard(self, governance, connection, row, item):
        self.store.fence(connection, row)
        governance.assert_publication_allowed(connection, item["knowledge_id"], item["revision"])

    def _check_submission_candidates(self, row, assets):
        candidates = {_digest(row["job_id"] + "/asset/" + asset["candidate_id"])[:32]: asset["knowledge_id"]
                      for asset in assets}
        with self.store._connection() as connection:
            submissions = self.store._execute(connection,
                "SELECT item_id,source_identity FROM knowledge_submissions WHERE receipt_id=?",
                (row["receipt_id"],)).fetchall()
        for submission in submissions:
            if submission["item_id"] not in candidates:
                # A changed case boundary/member set may change candidate IDs.
                # Never silently import a new identity and abandon the old item.
                raise ProcessingError("processing_source_identity_changed", 422)
            with self.store._connection() as connection:
                expected = assignments.effective_identity(self.store, connection, submission["item_id"], candidates[submission["item_id"]])
            if expected != submission["source_identity"]:
                raise ProcessingError("processing_submission_mismatch", 422)

    def _resume_governed_asset(self, row, asset, item):
        """Resume the immutable submission, including a crash before item save.

        Parser upgrades may change text or classification for the same verified
        source bytes. They must not replace a durable submission's original
        body, decision or publication generation during a job retry.
        """
        with self.store._connection() as connection:
            submission = self.store._execute(connection,
                "SELECT * FROM knowledge_submissions WHERE item_id=?", (item["item_id"],)).fetchone()
            if submission is None:
                return False
            if (any(submission[key] != row[key] for key in ("receipt_id", "principal", "source_id"))
                    or submission["source_identity"] != assignments.effective_identity(self.store, connection, item["item_id"], asset["knowledge_id"])):
                raise ProcessingError("processing_submission_mismatch", 422)
            submission = dict(submission)
        if self.draft_objects is None:
            self.draft_objects = KnowledgeObjects()
        catalogue = KnowledgeStore(self.store.path, database_url=self.store.database_url, objects=self.draft_objects)
        governance = GovernanceStore(catalogue)
        snapshot = catalogue.snapshot(submission["knowledge_id"], submission["revision"])
        content = snapshot["content"]
        with catalogue._connection() as connection:
            governed = governance._result(connection, submission, content)
        entry = {key: value for key, value in governed["record"].items() if key != "content"}
        # All v2 imports persist the rule decision before attempting indexing.
        # Missing historical evidence is review-only, never silently approved.
        automatic_reasons = set(entry.get("evidence", {}).get("automatic_rule_reasons", ["processing_decision_unavailable"]))
        content_ref = self._artifacts().put(content)
        item["title"] = entry["title"]
        self._finish_governed_asset(row, entry, item, content_ref, governed, automatic_reasons, catalogue, governance)
        return True

    def _govern_asset(self, row, asset, entry, content, item, content_ref):
        if self.draft_objects is None:
            self.draft_objects = KnowledgeObjects()
        catalogue = KnowledgeStore(self.store.path, database_url=self.store.database_url, objects=self.draft_objects)
        governance = GovernanceStore(catalogue)
        try:
            identity = assignments.resolve_source(self.store, row, item, entry)
            entry = assignments.bind_references(self.store, row, item, entry, identity)
            entry = assignments.apply_role(self.store, item["item_id"], entry)
            entry = {**entry, "knowledge_id": identity}
            entry = catalogue.bind_role(entry, create_from_sop=True)
        except ProcessingError as exc:
            if exc.code != "source_relation_review":
                raise
            item.update(status="needs_review", reason_codes=[exc.code])
            metadata_ref = self._artifacts().put(_json({"entry": entry, "item": item, "content_ref": content_ref, "published": False}))
            self.store.save_item(row, item, content_ref, metadata_ref)
            return
        except ValueError as exc:
            code = str(exc) if str(exc) in UPLOADER_ROLE_REASONS | {"unknown_role_id", "role_id_required", "role_metadata_mismatch", "invalid_role_id"} else "role_metadata_incomplete"
            item.update(status="needs_review", reason_codes=[code])
            metadata_ref = self._artifacts().put(_json({"entry": entry, "item": item, "content_ref": content_ref, "published": False}))
            self.store.save_item(row, item, content_ref, metadata_ref)
            return
        evidence = dict(entry["evidence"])
        # Only the authenticated receipt endpoint sets the vector destination.
        # Source files and model-supplied metadata cannot choose a collection.
        evidence.pop("ingestion_source", None)
        receipt = ReceiptStore.get(self.store, row["principal"], row["receipt_id"])
        if receipt["ingestion_source"] == "agent_collector":
            evidence["ingestion_source"] = "agent_collector"
        automatic_reasons = self._automatic_reasons(entry, content)
        entry["evidence"] = {**evidence, "processing_rule": row["rule_version"],
                             "submitter_authenticated": True, "review_status": "automatic_rule_passed" if not automatic_reasons else "needs_review",
                             "publication_approved": not automatic_reasons, "automatic_rule_reasons": sorted(automatic_reasons)}
        # The declaration belongs to this authenticated receipt, not the document's audience.
        entry["evidence"].pop("uploader_attribution", None)
        position = evidence.get("source_uploader_position")
        if position and position not in {"未知", "未确认", "待补充", "未取得", "未提供", "待填写"}:
            entry["evidence"]["uploader_attribution"] = {
                "position": position, "source": "employee_declared", "receipt_id": row["receipt_id"]}
        base = evidence.get("source_base_revision")
        try:
            governed = governance.import_asset(
                {**entry, "content": content}, asset, item_id=item["item_id"], receipt_id=row["receipt_id"],
                principal=row["principal"], source_id=row["source_id"],
                # Intake identities already include stable source-v2 IDs and case
                # fragments; a shared file path would merge independent cases.
                source_identity=identity,
                base_revision=int(base) if base else None, guard=lambda connection: self.store.fence(connection, row))
        except ValueError as exc:
            if str(exc) not in {'source_ingestion_source_conflict', 'uploader_role_not_found',
                                'uploader_role_ambiguous', 'uploader_position_is_department',
                                'uploader_department_mismatch', 'uploader_role_conflict'}:
                raise
            item.update(status='needs_review', reason_codes=[str(exc)])
            metadata_ref = self._artifacts().put(_json({'entry': entry, 'item': item, 'content_ref': content_ref, 'published': False}))
            self.store.save_item(row, item, content_ref, metadata_ref)
            return
        self._finish_governed_asset(row, entry, item, content_ref, governed, automatic_reasons, catalogue, governance)

    def _finish_governed_asset(self, row, entry, item, content_ref, governed, automatic_reasons, catalogue, governance):
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
        if extension not in {".zip", ".md", ".txt", ".docx"}:
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
                archive.write(original, row["filename"] if extension == ".docx" else "source" + extension)
        try:
            with zipfile.ZipFile(package) as archive:
                members = archive.infolist()
                if (len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES
                        or any(item.file_size > MAX_MEMBER_BYTES or len(item.filename) > 1000 for item in members)):
                    raise ProcessingError("processing_archive_limit", 422)
        except (zipfile.BadZipFile, NotImplementedError):
            raise ProcessingError("invalid_archive", 422) from None
        return package

    def _asset_files(self, package, asset):
        from .knowledge_files import KnowledgeFiles
        from .knowledge_text import docx_html_preview
        if self.files is None:
            self.files = KnowledgeFiles(bucket=self.draft_objects.bucket if self.draft_objects is not None else None)
        with zipfile.ZipFile(package) as archive:
            data = archive.read(asset["source_path"])
        if _digest(data) != asset["source_sha256"]:
            raise ProcessingError("processing_original_changed", 422)
        filename = Path(asset["source_path"]).name
        refs = [self.files.put(data, filename, purpose="original")]
        if filename.lower().endswith(".docx"):
            preview = docx_html_preview(data, filename)
            refs.append(self.files.put(preview, Path(filename).stem + ".html", purpose="preview", media_type="text/html"))
        skill_path = asset.get("skill_package_path")
        if skill_path:
            with zipfile.ZipFile(package) as archive:
                skill_data = archive.read(skill_path)
            try:
                from .knowledge_files import validate_skill_package
                asset["skill_package_validation"] = validate_skill_package(skill_data)
                refs.append(self.files.put(skill_data, Path(skill_path).name, purpose="skill"))
            except ValueError:
                raise ProcessingError("skill_package_invalid", 422) from None
        return refs

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
        if row["rule_version"] == RULE_VERSION:
            self._check_submission_candidates(row, batch["assets"])
        self.store.stage(row, "persisting")
        file_rows = [item for report in batch["packages"] for item in report["files"]]
        assets = batch["assets"]
        report_issues = {issue["code"] for report in batch["packages"] for issue in report["issues"] if issue["code"] == "archive_failed"}
        archive_sensitive = any(item["state"] == "credential_skipped" for item in file_rows)
        manifest_sensitive = any(Path(item["name"]).name == "交付清单.csv" and any(item.get("redactions", {}).values()) for item in file_rows)
        # Unknown sources are retained as unapproved drafts, never asserted as authorship or publication consent.
        with self.store._connection() as connection:
            selected = {r["item_id"] for r in self.store._execute(connection,
                "SELECT a.item_id FROM knowledge_item_role_assignments a JOIN knowledge_processing_items i ON i.item_id=a.item_id WHERE i.job_id=? AND a.resume_pending=1",
                (row["job_id"],)).fetchall()} if assignments.ready(self.store, connection) else set()
            replacement_selected = {r["item_id"] for r in self.store._execute(connection,
                "SELECT i.item_id FROM knowledge_processing_items i WHERE i.job_id=? AND " + self.store._replacement_pending(connection),
                (row["job_id"],)).fetchall()}
            selected |= replacement_selected
        # Resolve every new candidate before importing any of its references.
        # A later collision must not let an earlier asset point at an old asset.
        with self.store._connection() as connection:
            submitted = {r["item_id"] for r in self.store._execute(connection,
                "SELECT item_id FROM knowledge_submissions WHERE receipt_id=?", (row["receipt_id"],)).fetchall()}
        prepared = {}
        for asset in assets:
            path = output / asset["body_file"]
            content = path.read_text(encoding="utf-8")
            if _digest(content) != asset["content_hash"]:
                raise ProcessingError("processing_artifact_changed", 422)
            metadata_path = output / asset["metadata_file"]
            metadata = json.loads(metadata_path.read_text())
            if _digest(metadata_path.read_bytes()) != asset["metadata_hash"]:
                raise ProcessingError("processing_artifact_changed", 422)
            prepared[asset["candidate_id"]] = (metadata, content)
            candidate = {"item_id": _digest(row["job_id"] + "/asset/" + asset["candidate_id"])[:32]}
            if (row["rule_version"] == RULE_VERSION and candidate["item_id"] not in submitted
                    and (not selected or candidate["item_id"] in selected)):
                entry = {**metadata, "knowledge_id": asset["knowledge_id"]}
                assignments.context(self.store, row, candidate, entry, content, asset)
                try:
                    assignments.resolve_source(self.store, row, candidate, entry)
                except ProcessingError as exc:
                    if exc.code != "source_relation_review":
                        raise
        for asset in assets:
            pulse()
            metadata, content = prepared[asset["candidate_id"]]
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
            item["missing_references"] = missing_references(asset["issues"], asset["source_path"])
            if item["item_id"] in replacement_selected:
                item["replacement_recheck"] = True
            try:
                previous = self.store.item_record(row["job_id"], item["item_id"], row["principal"])
            except ProcessingError as exc:
                if exc.status_code != 404:
                    raise
            else:
                if previous["status"] in {"published", "duplicate", "outdated", "archived"}:
                    continue
            if selected and item["item_id"] not in selected:
                continue
            if row["rule_version"] == RULE_VERSION and self._resume_governed_asset(row, asset, item):
                continue
            item["knowledge_id"] = asset["knowledge_id"]
            content_ref = self._artifacts().put(content) if content.strip() and len(content) <= 500_000 else None
            if content_ref is None:
                item.update(status="needs_review", reason_codes=sorted(set(item["reason_codes"]) | {"preview_unavailable"}))
            if item["status"] == "draft":
                # Files must be durably stored and verified before the revision
                # can become public; failure retries this same receipt.
                # Keep unbound legacy receipt fingerprints stable on retries.
                if entry.get("role_id") or entry.get("role_name") or asset.get("skill_package_path"):
                    entry["files"] = self._asset_files(package, asset)
                    if asset.get("skill_package_validation"):
                        entry["evidence"].update(delivery_scope="skill_package",
                                                 skill_package_validation=asset["skill_package_validation"])
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
        represented_paths = {path for asset in assets for path in (asset["source_path"], asset.get("skill_package_path")) if path}
        for file in file_rows:
            if selected:
                continue
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
        if selected:
            with self.store._connection(write=True) as connection:
                self.store.fence(connection, row)
                self.store._execute(connection, "UPDATE knowledge_item_role_assignments SET resume_pending=0 WHERE item_id IN (" + ",".join("?" for _ in selected) + ")", tuple(selected))
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
