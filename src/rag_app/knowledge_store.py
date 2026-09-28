"""Versioned company knowledge with an explicit publication pointer."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from .config import PROJECT_ROOT
from .knowledge_chunking import CHUNKING_VERSIONS


def _knowledge_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("knowledge_id must be 1-80 letters, digits, underscores or hyphens, starting with a letter or digit")
    return value


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("knowledge metadata must contain JSON-serializable values") from exc


def _text(value, field, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def normalize_entry(entry: dict) -> dict:
    """Validate a draft without touching storage and include its stable hashes."""
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")
    content = entry.get("content")
    _text(content, "content")
    if len(content) > 500_000:
        raise ValueError("content exceeds the 500000-character limit")
    sources = entry.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    normalized_sources = []
    for source in sources:
        if not isinstance(source, dict) or set(source) - {"name", "locator", "kind", "url"}:
            raise ValueError("sources accept only name, locator, kind and url; internal path fields are not allowed")
        normalized = {key: _text(value, f"source.{key}") for key, value in source.items()}
        if not any(normalized.get(key) for key in ("name", "locator", "url")):
            raise ValueError("each source needs a name, locator or url")
        for value in normalized.values():
            if value.startswith(("/", "~", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value) or value.lower().startswith("file:"):
                raise ValueError("sources must not expose local filesystem paths")
        if "url" in normalized:
            parsed = urlsplit(normalized["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("source.url must be an HTTP(S) URL without credentials")
        normalized_sources.append(normalized)
    evidence = entry.get("evidence", {})
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    payload = {
        "knowledge_id": _knowledge_id(entry.get("knowledge_id")),
        "title": _text(entry.get("title"), "title"),
        "content": content,
        "contributor": _text(entry.get("contributor"), "contributor", "未知"),
        "kind": _text(entry.get("kind"), "kind", "未知"),
        "sources": normalized_sources,
        "evidence": evidence,
    }
    if "chunking_version" in entry:
        if entry["chunking_version"] not in CHUNKING_VERSIONS:
            raise ValueError("unsupported chunking_version")
        payload["chunking_version"] = entry["chunking_version"]
    if "department" in entry:
        payload["department"] = _text(entry["department"], "department")
        if len(payload["department"]) > 200:
            raise ValueError("department exceeds the 200-character limit")
    if "scenarios" in entry:
        scenarios = entry["scenarios"]
        if not isinstance(scenarios, list) or len(scenarios) > 20:
            raise ValueError("scenarios must be a list of at most 20 unique labels")
        scenarios = [_text(value, "scenario") for value in scenarios]
        if any(len(value) > 200 for value in scenarios) or len(set(scenarios)) != len(scenarios):
            raise ValueError("scenarios must contain unique labels of at most 200 characters")
        payload["scenarios"] = scenarios
    encoded = _json(payload)
    # Round-trip makes the stored representation independent of caller mutation.
    return {
        **json.loads(encoded),
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "payload_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_assets (
        knowledge_id TEXT PRIMARY KEY, latest_revision BIGINT NOT NULL,
        published_revision BIGINT, generation BIGINT NOT NULL,
        confirmed_by TEXT, published_at TEXT,
        status TEXT NOT NULL DEFAULT 'draft'
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_revisions (
        knowledge_id TEXT NOT NULL, revision BIGINT NOT NULL,
        payload_hash TEXT NOT NULL, record_json TEXT NOT NULL,
        was_published INTEGER NOT NULL DEFAULT 0,
        confirmed_by TEXT, published_at TEXT,
        PRIMARY KEY (knowledge_id, revision)
    )""",
)


class KnowledgeStore:
    def __init__(self, path=None, *, database_url=None, objects=None):
        if path is not None and database_url is not None:
            raise ValueError("choose a local path or a PostgreSQL database URL")
        self.database_url = database_url if database_url is not None else (
            os.getenv("KNOWLEDGE_DATABASE_URL") if path is None else None
        )
        if self.database_url:
            try:
                parsed = urlsplit(self.database_url)
                if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname or not parsed.path.strip("/"):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("KNOWLEDGE_DATABASE_URL must be a PostgreSQL URL with a database name") from None
        self.path = None if self.database_url else Path(path) if path is not None else Path(
            os.getenv("KNOWLEDGE_DATA_DIR") or PROJECT_ROOT / "output" / "company_knowledge"
        ) / "knowledge.sqlite3"
        self.body_storage = os.getenv("KNOWLEDGE_BODY_STORAGE") or ("oss" if self.database_url else "inline")
        if self.body_storage not in {"inline", "oss"}:
            raise ValueError("KNOWLEDGE_BODY_STORAGE must be inline or oss")
        self.objects = objects
        if objects is not None:
            self.body_storage = "oss"

    def _objects(self):
        if self.objects is None:
            from .knowledge_objects import KnowledgeObjects

            self.objects = KnowledgeObjects()
        return self.objects

    def _execute(self, connection, sql, parameters=()):
        # All SQL here is static and contains no literal question marks.
        return connection.execute(sql.replace("?", "%s") if self.database_url else sql, parameters)

    def _lock(self, connection, knowledge_id):
        if self.database_url:
            lock_id = int.from_bytes(hashlib.sha256(knowledge_id.encode()).digest()[:8], "big", signed=True)
            self._execute(connection, "SELECT pg_advisory_xact_lock(?)", (lock_id,))

    def initialize(self):
        """Create the catalogue explicitly; PostgreSQL reads/writes never create it."""
        with self._connection(write=True, initialize=True):
            pass

    @contextmanager
    def _connection(self, *, write=False, initialize=False):
        if self.database_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError:
                raise RuntimeError("PostgreSQL knowledge storage requires psycopg") from None
            try:
                connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)
            except Exception:
                raise RuntimeError("company knowledge database connection failed") from None
            try:
                with connection.transaction():
                    if not write:
                        connection.execute("SET TRANSACTION READ ONLY")
                    if initialize:
                        self._lock(connection, "schema:company-knowledge")
                        for statement in _SCHEMA:
                            connection.execute(statement)
                    ready = connection.execute("SELECT to_regclass('knowledge_assets') IS NOT NULL AND to_regclass('knowledge_revisions') IS NOT NULL AS ready").fetchone()["ready"]
                    if write and not ready:
                        raise RuntimeError("company knowledge database is not initialized; run init-db first")
                    yield connection if ready else None
            finally:
                connection.close()
            return
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        else:
            # query_only prevents SQL writes; mode=rw lets SQLite coordinate its
            # journal with concurrent writers. mode=ro mixed with short-lived
            # write connections can fail with SQLITE_IOERR on the macOS build.
            # The existence guard plus mode=rw still prohibit creating a file.
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
            connection.execute("PRAGMA query_only = ON")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                for statement in _SCHEMA:
                    connection.execute(statement)
            # An initial import may have created the file but not committed its schema.
            ready = connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'knowledge_assets'").fetchone()
            yield connection if ready else None
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _asset(self, connection, knowledge_id):
        asset = self._execute(connection, "SELECT * FROM knowledge_assets WHERE knowledge_id = ?", (knowledge_id,)).fetchone() if connection else None
        if asset is None:
            raise ValueError("knowledge asset does not exist")
        return asset

    def _record(self, connection, knowledge_id, revision, *, hydrate=True):
        if type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer")
        row = self._execute(connection, """SELECT r.record_json, r.was_published, r.confirmed_by,
            r.published_at, a.published_revision
            FROM knowledge_revisions r JOIN knowledge_assets a USING (knowledge_id)
            WHERE r.knowledge_id = ? AND r.revision = ?""", (knowledge_id, revision)).fetchone()
        if row is None:
            raise ValueError("knowledge revision does not exist")
        status = "published" if revision == row["published_revision"] else "withdrawn" if row["was_published"] else "draft"
        record = {
            **json.loads(row["record_json"]), "status": status,
            "confirmed_by": row["confirmed_by"], "published_at": row["published_at"],
        }
        return self._hydrate(record) if hydrate else record

    def _hydrate(self, record):
        record = dict(record)
        ref = record.pop("content_ref", None)
        if ref is not None:
            try:
                record["content"] = self._objects().get(ref)
            except ValueError:
                raise RuntimeError("company knowledge body reference is invalid") from None
        content = record.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 500_000:
            raise RuntimeError("company knowledge revision has no valid full body")
        try:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        except UnicodeError:
            raise RuntimeError("company knowledge body is not valid UTF-8") from None
        if digest != record.get("content_hash"):
            raise RuntimeError("company knowledge body hash does not match its revision")
        return record

    def import_draft(self, entry: dict, *, allow_new_revision: bool = True) -> dict:
        if type(allow_new_revision) is not bool:
            raise ValueError("allow_new_revision must be a boolean")
        payload = normalize_entry(entry)
        knowledge_id = payload["knowledge_id"]
        payload_hash = payload["payload_hash"]
        stored_payload = dict(payload)
        if self.body_storage == "oss":
            # Upload and verify before opening a catalogue transaction. A failed
            # write must never create a revision pointing at an unreadable body.
            ref = self._objects().put(payload["content"])
            if ref["sha256"] != payload["content_hash"] or ref["byte_length"] != len(payload["content"].encode("utf-8")):
                raise RuntimeError("company knowledge body reference does not match its content")
            stored_payload.pop("content")
            stored_payload["content_ref"] = ref
        with self._connection(write=True) as connection:
            self._lock(connection, knowledge_id)
            asset = self._execute(connection, "SELECT * FROM knowledge_assets WHERE knowledge_id = ?", (knowledge_id,)).fetchone()
            if asset:
                latest = self._record(connection, knowledge_id, asset["latest_revision"], hydrate=False)
                if latest["payload_hash"] == payload_hash:
                    latest.pop("content_ref", None)
                    return {**latest, "content": payload["content"], "generation": asset["generation"]}
                if not allow_new_revision:
                    raise ValueError("batch import cannot create a new revision of an existing asset")
            revision = asset["latest_revision"] + 1 if asset else 1
            generation = asset["generation"] + 1 if asset else 1
            record = {
                **stored_payload, "revision": revision,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._execute(connection, "INSERT INTO knowledge_revisions (knowledge_id, revision, payload_hash, record_json) VALUES (?, ?, ?, ?)", (knowledge_id, revision, payload_hash, _json(record)))
            if asset:
                self._execute(connection, "UPDATE knowledge_assets SET latest_revision = ?, generation = ?, status = CASE WHEN published_revision IS NULL THEN 'draft' ELSE 'published' END WHERE knowledge_id = ?", (revision, generation, knowledge_id))
            else:
                self._execute(connection, "INSERT INTO knowledge_assets (knowledge_id, latest_revision, generation) VALUES (?, ?, ?)", (knowledge_id, revision, generation))
            record.pop("content_ref", None)
            return {**record, "content": payload["content"], "generation": generation, "status": "draft", "confirmed_by": None, "published_at": None}

    def snapshot(self, knowledge_id, revision=None) -> dict:
        knowledge_id = _knowledge_id(knowledge_id)
        with self._connection() as connection:
            try:
                asset = self._asset(connection, knowledge_id)
            except ValueError as exc:
                raise KeyError(knowledge_id) from exc
            record = self._record(connection, knowledge_id, asset["latest_revision"] if revision is None else revision, hydrate=False)
            record["generation"] = asset["generation"]
        return self._hydrate(record)

    def publish(self, knowledge_id, revision, *, expected_generation: int, confirmed_by: str) -> dict:
        knowledge_id = _knowledge_id(knowledge_id)
        confirmed_by = _text(confirmed_by, "confirmed_by")
        if type(expected_generation) is not int or expected_generation < 1:
            raise ValueError("expected_generation must be a positive integer")
        record = self.snapshot(knowledge_id, revision)
        with self._connection(write=True) as connection:
            self._lock(connection, knowledge_id)
            asset = self._asset(connection, knowledge_id)
            if asset["generation"] != expected_generation:
                raise RuntimeError("knowledge changed during publication; take a fresh snapshot and publish again")
            published_at = datetime.now(timezone.utc).isoformat()
            generation = expected_generation + 1
            self._execute(connection, "UPDATE knowledge_revisions SET was_published = 1, confirmed_by = ?, published_at = ? WHERE knowledge_id = ? AND revision = ?", (confirmed_by, published_at, knowledge_id, revision))
            self._execute(connection, "UPDATE knowledge_assets SET published_revision = ?, generation = ?, confirmed_by = ?, published_at = ?, status = 'published' WHERE knowledge_id = ?", (revision, generation, confirmed_by, published_at, knowledge_id))
            return {**record, "generation": generation, "confirmed_by": confirmed_by, "published_at": published_at, "status": "published"}

    def withdraw(self, knowledge_id) -> dict:
        knowledge_id = _knowledge_id(knowledge_id)
        with self._connection(write=True) as connection:
            self._lock(connection, knowledge_id)
            asset = self._asset(connection, knowledge_id)
            generation = asset["generation"] + 1
            self._execute(connection, "UPDATE knowledge_assets SET published_revision = NULL, generation = ?, confirmed_by = NULL, published_at = NULL, status = 'withdrawn' WHERE knowledge_id = ?", (generation, knowledge_id))
            return {"knowledge_id": knowledge_id, "published_revision": None, "generation": generation, "status": "withdrawn"}

    def get_published(self, knowledge_id) -> dict | None:
        knowledge_id = _knowledge_id(knowledge_id)
        with self._connection() as connection:
            if connection is None:
                return None
            asset = self._execute(connection, "SELECT * FROM knowledge_assets WHERE knowledge_id = ?", (knowledge_id,)).fetchone()
            if asset is None or asset["published_revision"] is None:
                return None
            record = {
                **self._record(connection, knowledge_id, asset["published_revision"], hydrate=False),
                "generation": asset["generation"], "confirmed_by": asset["confirmed_by"],
                "published_at": asset["published_at"],
            }
        record = self._hydrate(record)
        # OSS reads can outlive a withdrawal. Open a fresh transaction so neither
        # a SQLite snapshot nor an earlier publication pointer authorizes it.
        with self._connection() as connection:
            current = self._asset(connection, knowledge_id)
            if current["published_revision"] != record["revision"] or current["generation"] != record["generation"]:
                return None
        return record

    def list_assets(self) -> list[dict]:
        with self._connection() as connection:
            if connection is None:
                return []
            result = []
            for asset in connection.execute("SELECT * FROM knowledge_assets ORDER BY knowledge_id"):
                latest = self._record(connection, asset["knowledge_id"], asset["latest_revision"], hydrate=False)
                result.append({
                    **dict(asset), "title": latest["title"],
                    "has_draft": asset["latest_revision"] != asset["published_revision"],
                })
            return result

    def published_revisions(self, department=None, scenario=None) -> list[dict]:
        filters = {"department": department, "scenario": scenario}
        for name, value in filters.items():
            if value is not None:
                filters[name] = _text(value, name)
                if len(filters[name]) > 200:
                    raise ValueError(f"{name} exceeds the 200-character limit")
        with self._connection() as connection:
            if connection is None:
                return []
            if all(value is None for value in filters.values()):
                return [dict(row) for row in connection.execute("SELECT knowledge_id, published_revision AS revision FROM knowledge_assets WHERE published_revision IS NOT NULL ORDER BY knowledge_id")]
            rows = connection.execute("""SELECT a.knowledge_id, a.published_revision AS revision, r.record_json
                FROM knowledge_assets a JOIN knowledge_revisions r ON a.knowledge_id = r.knowledge_id
                AND a.published_revision = r.revision ORDER BY a.knowledge_id""")
            result = []
            for row in rows:
                metadata = json.loads(row["record_json"])
                if filters["department"] is not None and metadata.get("department") != filters["department"]:
                    continue
                if filters["scenario"] is not None and filters["scenario"] not in metadata.get("scenarios", []):
                    continue
                result.append({"knowledge_id": row["knowledge_id"], "revision": row["revision"]})
            return result
