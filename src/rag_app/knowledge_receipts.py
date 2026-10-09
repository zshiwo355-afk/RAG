"""Durable private-original receipts. Verification never publishes knowledge."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit
import uuid


MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
LEASE_SECONDS = 90
MAX_ATTEMPTS = 5
UPLOAD_SECONDS = 900
PREFIX = "knowledge-receipts/raw/"
IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
PRINCIPAL = r"[A-Za-z0-9_.:@-]{1,128}"


class ReceiptError(RuntimeError):
    def __init__(self, code, status_code=503):
        super().__init__(code)
        self.code, self.status_code = code, status_code


class LeaseLost(RuntimeError):
    pass


def validate_payload(payload):
    fields = {"idempotency_key", "source_id", "filename", "byte_length", "sha256"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ReceiptError("invalid_upload_request", 400)
    if any(not isinstance(payload[name], str) or not re.fullmatch(IDENTIFIER, payload[name])
           for name in ("idempotency_key", "source_id")):
        raise ReceiptError("invalid_upload_request", 400)
    filename = payload["filename"]
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 200 or filename in {".", ".."}
            or filename != filename.strip()
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} or c in "/\\" for c in filename)):
        raise ReceiptError("invalid_filename", 400)
    if type(payload["byte_length"]) is not int or not 1 <= payload["byte_length"] <= MAX_BYTES:
        raise ReceiptError("invalid_byte_length", 400)
    if not isinstance(payload["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]):
        raise ReceiptError("invalid_sha256", 400)
    return dict(payload)


def validate_principal(principal):
    if not isinstance(principal, str) or not re.fullmatch(PRINCIPAL, principal):
        raise ReceiptError("invalid_principal", 400)


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_upload_receipts (
      receipt_id TEXT PRIMARY KEY, principal TEXT NOT NULL, idempotency_key TEXT NOT NULL,
      source_id TEXT NOT NULL, filename TEXT NOT NULL, byte_length BIGINT NOT NULL,
      sha256 TEXT NOT NULL, payload_hash TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL, etag TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
      available_at DOUBLE PRECISION NOT NULL DEFAULT 0, error_code TEXT,
      created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
      UNIQUE(principal, idempotency_key)
    )""",
    "CREATE INDEX IF NOT EXISTS knowledge_upload_receipts_pending ON knowledge_upload_receipts(status, available_at, lease_until)",
)


class ReceiptStore:
    def __init__(self, path=None, *, database_url=None, clock=time.time):
        if path is not None and database_url is not None:
            raise ReceiptError("invalid_receipt_database")
        self.database_url = database_url or (os.getenv("KNOWLEDGE_DATABASE_URL") if path is None else None)
        self.path, self.clock = Path(path) if path is not None else None, clock
        if self.database_url:
            parsed = urlsplit(self.database_url)
            if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname or not parsed.path.strip("/"):
                raise ReceiptError("invalid_receipt_database")
        elif self.path is None:
            local = os.getenv("KNOWLEDGE_RECEIPTS_SQLITE_PATH")
            if os.getenv("KNOWLEDGE_RECEIPTS_DEVELOPMENT") != "1" or not local:
                raise ReceiptError("receipt_database_not_configured")
            self.path = Path(local)

    def _execute(self, connection, sql, parameters=()):
        return connection.execute(sql.replace("?", "%s") if self.database_url else sql, parameters)

    def _lock(self, connection, name):
        if self.database_url:
            lock_id = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
            self._execute(connection, "SELECT pg_advisory_xact_lock(?)", (lock_id,))

    @contextmanager
    def _connection(self, *, write=False, initialize=False):
        if self.database_url:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)
        else:
            if not initialize and not self.path.exists():
                raise ReceiptError("receipt_database_not_initialized")
            if initialize:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            else:
                connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True,
                                             timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
        try:
            if not self.database_url:
                if not write:
                    connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            elif not write:
                connection.execute("SET TRANSACTION READ ONLY")
            if initialize:
                self._lock(connection, "schema:knowledge-upload-receipts")
                for statement in _SCHEMA:
                    connection.execute(statement)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self._connection(write=True, initialize=True):
            pass

    def prepare(self, principal, payload):
        validate_principal(principal)
        payload = validate_payload(payload)
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        receipt_id, now = uuid.uuid4().hex, self.clock()
        owner = hashlib.sha256(principal.encode()).hexdigest()[:32]
        with self._connection(write=True) as connection:
            self._execute(connection, """INSERT INTO knowledge_upload_receipts
                (receipt_id, principal, idempotency_key, source_id, filename, byte_length,
                 sha256, payload_hash, object_key, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_upload', ?, ?)
                ON CONFLICT(principal, idempotency_key) DO NOTHING""",
                (receipt_id, principal, payload["idempotency_key"], payload["source_id"], payload["filename"],
                 payload["byte_length"], payload["sha256"], payload_hash, PREFIX + owner + "/" + receipt_id + "/source",
                 now, now))
            row = self._execute(connection, "SELECT * FROM knowledge_upload_receipts WHERE principal=? AND idempotency_key=?",
                                (principal, payload["idempotency_key"])).fetchone()
            if row["payload_hash"] != payload_hash:
                raise ReceiptError("idempotency_conflict", 409)
            return dict(row)

    def get(self, principal, receipt_id):
        validate_principal(principal)
        if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
            raise ReceiptError("receipt_not_found", 404)
        with self._connection() as connection:
            row = self._execute(connection, "SELECT * FROM knowledge_upload_receipts WHERE receipt_id=? AND principal=?",
                                (receipt_id, principal)).fetchone()
            if row is None:
                raise ReceiptError("receipt_not_found", 404)
            return dict(row)

    def enqueue(self, row, etag):
        with self._connection(write=True) as connection:
            self._execute(connection, """UPDATE knowledge_upload_receipts
                SET status='pending_verification', etag=?, attempts=0, available_at=0,
                    error_code=NULL, updated_at=?
                WHERE receipt_id=? AND principal=? AND status IN ('awaiting_upload', 'failed')""",
                (etag, self.clock(), row["receipt_id"], row["principal"]))
        return self.get(row["principal"], row["receipt_id"])

    def reject_upload(self, row, code):
        with self._connection(write=True) as connection:
            self._execute(connection, """UPDATE knowledge_upload_receipts SET status='rejected', error_code=?, updated_at=?
                WHERE receipt_id=? AND principal=? AND status IN ('awaiting_upload', 'failed')""",
                (code, self.clock(), row["receipt_id"], row["principal"]))
        return self.get(row["principal"], row["receipt_id"])

    def claim(self):
        now, token = self.clock(), uuid.uuid4().hex
        with self._connection(write=True) as connection:
            # ponytail: one global verification slot; raise the explicit limit only after measuring retrieval load.
            self._lock(connection, "worker:knowledge-upload-receipts")
            active = self._execute(connection, "SELECT receipt_id FROM knowledge_upload_receipts WHERE status='verifying' AND lease_until>? LIMIT 1", (now,)).fetchone()
            if active is not None:
                return None
            row = self._execute(connection, """SELECT * FROM knowledge_upload_receipts
                WHERE (status='pending_verification' AND available_at<=?) OR (status='verifying' AND lease_until<=?)
                ORDER BY created_at, receipt_id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            row = dict(row)
            if row["attempts"] >= MAX_ATTEMPTS:
                self._execute(connection, """UPDATE knowledge_upload_receipts SET status='failed', error_code='verification_retries_exhausted',
                    lease_token=NULL, lease_until=0, updated_at=? WHERE receipt_id=?""", (now, row["receipt_id"]))
                return None
            self._execute(connection, """UPDATE knowledge_upload_receipts SET status='verifying', attempts=attempts+1,
                lease_token=?, lease_until=?, updated_at=? WHERE receipt_id=?""", (token, now + LEASE_SECONDS, now, row["receipt_id"]))
            return {**row, "status": "verifying", "attempts": row["attempts"] + 1,
                    "lease_token": token, "lease_until": now + LEASE_SECONDS}

    def heartbeat(self, row):
        now = self.clock()
        with self._connection(write=True) as connection:
            result = self._execute(connection, """UPDATE knowledge_upload_receipts SET lease_until=?
                WHERE receipt_id=? AND status='verifying' AND lease_token=? AND lease_until>?""",
                (now + LEASE_SECONDS, row["receipt_id"], row["lease_token"], now))
            if result.rowcount != 1:
                raise LeaseLost()

    def finish(self, row, status, code=None):
        now = self.clock()
        if status == "pending_verification" and row["attempts"] >= MAX_ATTEMPTS:
            status = "failed"
        available = now + min(30 * 2 ** (row["attempts"] - 1), 300) if status == "pending_verification" else 0
        with self._connection(write=True) as connection:
            result = self._execute(connection, """UPDATE knowledge_upload_receipts SET status=?, error_code=?,
                available_at=?, lease_token=NULL, lease_until=0, updated_at=?
                WHERE receipt_id=? AND status='verifying' AND lease_token=? AND lease_until>?""",
                (status, code, available, now, row["receipt_id"], row["lease_token"], now))
            return result.rowcount == 1


class ReceiptObjects:
    def __init__(self, bucket=None):
        if bucket is not None:
            self.bucket = bucket
            return
        config = {name: (os.getenv("KNOWLEDGE_RECEIPTS_OSS_" + name) or "").strip()
                  for name in ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "ACCESS_KEY_SECRET")}
        if not all(config.values()):
            raise ReceiptError("receipt_oss_not_configured")
        if config["BUCKET"] in {os.getenv("OSS_BUCKET"), os.getenv("KNOWLEDGE_OSS_BUCKET")}:
            raise ReceiptError("receipt_bucket_must_be_independent")
        endpoint = urlsplit(config["ENDPOINT"])
        if (endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
                or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
                or endpoint.hostname != "oss-" + config["REGION"] + ".aliyuncs.com"
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", config["BUCKET"])):
            raise ReceiptError("invalid_receipt_oss_configuration")
        import oss2

        self.bucket = oss2.Bucket(oss2.AuthV4(config["ACCESS_KEY_ID"], config["ACCESS_KEY_SECRET"]),
                                  config["ENDPOINT"], config["BUCKET"], region=config["REGION"], connect_timeout=30)

    def assert_ready(self):
        try:
            if self.bucket.get_bucket_acl().acl != "private":
                raise ReceiptError("receipt_bucket_not_private")
            # OSS ignores forbid-overwrite when versioning is enabled OR suspended.
            if self.bucket.get_bucket_versioning().status not in (None, "", "Disabled"):
                raise ReceiptError("receipt_bucket_versioning_unsupported")
        except ReceiptError:
            raise
        except Exception:
            raise ReceiptError("receipt_storage_unavailable") from None

    def grant(self, row):
        self.assert_ready()
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(row["byte_length"]),
                   "x-oss-object-acl": "private", "x-oss-forbid-overwrite": "true",
                   "x-oss-meta-receipt-id": row["receipt_id"], "x-oss-meta-sha256": row["sha256"]}
        try:
            url = self.bucket.sign_url("PUT", row["object_key"], UPLOAD_SECONDS, headers=headers,
                                       additional_headers={"content-length"})
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            if (parsed.scheme != "https" or query.get("x-oss-signature-version") != ["OSS4-HMAC-SHA256"]
                    or "content-length" not in query.get("x-oss-additional-headers", [""])[0].split(";")):
                raise ReceiptError("receipt_signature_not_bound")
        except ReceiptError:
            raise
        except Exception:
            raise ReceiptError("receipt_signing_unavailable") from None
        return {"method": "PUT", "url": url, "headers": headers, "expires_at": _utc(time.time() + UPLOAD_SECONDS)}

    def inspect(self, row):
        self.assert_ready()
        try:
            head = self.bucket.head_object(row["object_key"])
            if head.content_length != row["byte_length"]:
                raise ReceiptError("object_size_mismatch", 422)
            if self.bucket.get_object_acl(row["object_key"]).acl != "private":
                raise ReceiptError("object_not_private", 422)
            headers = {key.lower(): value for key, value in head.headers.items()}
            if (headers.get("x-oss-meta-receipt-id") != row["receipt_id"]
                    or headers.get("x-oss-meta-sha256") != row["sha256"]):
                raise ReceiptError("object_metadata_mismatch", 422)
            if not isinstance(head.etag, str) or not re.fullmatch(r"[0-9A-Fa-f]{32}(?:-\d+)?", head.etag):
                raise ReceiptError("object_etag_invalid", 422)
            if row.get("etag") and head.etag != row["etag"]:
                raise ReceiptError("object_changed", 422)
            return head.etag
        except ReceiptError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise ReceiptError("upload_not_found", 409) from None
            raise ReceiptError("receipt_storage_unavailable") from None

    def verify(self, row, heartbeat, *, sink=None):
        self.inspect(row)
        digest, length, deadline = hashlib.sha256(), 0, time.monotonic() + 300
        try:
            with self.bucket.get_object(row["object_key"], headers={"If-Match": '"' + row["etag"] + '"'}) as response:
                while True:
                    heartbeat()
                    if time.monotonic() > deadline:
                        raise ReceiptError("verification_timed_out")
                    block = response.read(min(65536, row["byte_length"] + 1 - length))
                    if not block:
                        break
                    if not isinstance(block, bytes) or length + len(block) > row["byte_length"]:
                        raise ReceiptError("object_size_mismatch", 422)
                    length += len(block)
                    digest.update(block)
                    if sink is not None:
                        sink.write(block)
            if length != row["byte_length"]:
                raise ReceiptError("object_size_mismatch", 422)
            if digest.hexdigest() != row["sha256"]:
                raise ReceiptError("object_hash_mismatch", 422)
            self.inspect(row)
        except (ReceiptError, LeaseLost):
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 412:
                raise ReceiptError("object_changed", 422) from None
            raise ReceiptError("receipt_storage_unavailable") from None


class ReceiptService:
    def __init__(self, store=None, objects=None, *, max_bytes=None):
        self.store = store if store is not None else ReceiptStore()
        self.objects = objects
        try:
            self.max_bytes = int(os.getenv("KNOWLEDGE_RECEIPTS_MAX_BYTES", str(DEFAULT_MAX_BYTES))) if max_bytes is None else max_bytes
            if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_BYTES:
                raise ValueError()
        except ValueError:
            raise ReceiptError("invalid_receipt_size_limit") from None

    def _objects(self):
        if self.objects is None:
            self.objects = ReceiptObjects()
        return self.objects

    def _response(self, row, upload=None):
        receipt = {key: row[key] for key in ("receipt_id", "source_id", "filename", "byte_length", "sha256", "status", "error_code")}
        receipt.update(content_verified=row["status"] == "verified", published=False,
                       created_at=_utc(row["created_at"]), updated_at=_utc(row["updated_at"]),
                       retryable=row["status"] in {"awaiting_upload", "pending_verification", "failed"})
        return {"ok": True, "receipt": receipt, "upload": upload}

    def prepare(self, principal, payload):
        payload = validate_payload(payload)
        if payload["byte_length"] > self.max_bytes:
            raise ReceiptError("upload_too_large", 413)
        row = self.store.prepare(principal, payload)
        return self._response(row, self._objects().grant(row) if row["status"] == "awaiting_upload" else None)

    def status(self, principal, receipt_id):
        return self._response(self.store.get(principal, receipt_id))

    def complete(self, principal, receipt_id):
        row = self.store.get(principal, receipt_id)
        if row["status"] in {"awaiting_upload", "failed"}:
            try:
                etag = self._objects().inspect(row)
            except ReceiptError as exc:
                if exc.status_code == 422:
                    return self._response(self.store.reject_upload(row, exc.code))
                raise
            row = self.store.enqueue(row, etag)
        return self._response(row)

    def verify_once(self):
        row = self.store.claim()
        if row is None:
            return False
        last = [float("-inf")]

        def heartbeat():
            now = self.store.clock()
            if now - last[0] >= LEASE_SECONDS / 3:
                self.store.heartbeat(row)
                last[0] = now

        try:
            self._objects().verify(row, heartbeat)
            self.store.finish(row, "verified")
        except LeaseLost:
            pass
        except ReceiptError as exc:
            self.store.finish(row, "rejected" if exc.status_code == 422 else "pending_verification", exc.code)
        except Exception:
            self.store.finish(row, "pending_verification", "verification_unavailable")
        return True
