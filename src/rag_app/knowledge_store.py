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


def _uploader_position(record, receipts):
    snapshot = record.get("evidence", {}).get("uploader_attribution")
    if not isinstance(snapshot, dict) or snapshot.get("source") != "employee_declared":
        return None
    receipt = receipts.get((record["knowledge_id"], record["revision"]))
    position = snapshot.get("position")
    if (not receipt or snapshot.get("receipt_id") != receipt
            or not isinstance(position, str) or not position.strip() or len(position) > 200
            or re.search(r"[\x00-\x1f\x7f]", position)):
        return None
    return position


def knowledge_category(record):
    """Three navigation categories, independent of contributor attribution."""
    if record.get("category") in {"sop", "skill", "other"}:
        return record["category"]
    kind = str(record.get("kind", "")).strip().lower()
    if kind in {"process", "sop", "流程"}:
        return "sop"
    if kind == "skill" or record.get("source_type", record.get("evidence", {}).get("source_type")) == "Skill方法":
        return "skill"
    return "other"


def _role_label(value, field):
    value = _text(value, field)
    if len(value) > 200 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("invalid_" + field)
    return value


def validate_role_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"role_[0-9a-f]{24}", value):
        raise ValueError("invalid_role_id")
    return value


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
    if "role_id" in entry:
        payload["role_id"] = validate_role_id(entry["role_id"])
    if "role_name" in entry:
        payload["role_name"] = _role_label(entry["role_name"], "role_name")
    if "category" in entry:
        if entry["category"] not in {"sop", "skill", "other"}:
            raise ValueError("invalid_knowledge_category")
        payload["category"] = entry["category"]
    if "files" in entry:
        from .knowledge_files import validate_file_descriptor
        if not isinstance(entry["files"], list) or len(entry["files"]) > 20:
            raise ValueError("invalid_knowledge_files")
        payload["files"] = [validate_file_descriptor(item) for item in entry["files"]]
        if len({item["file_id"] for item in payload["files"]}) != len(payload["files"]):
            raise ValueError("duplicate_knowledge_file")
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
    """CREATE TABLE IF NOT EXISTS knowledge_roles (
        role_id TEXT PRIMARY KEY, department TEXT NOT NULL, name TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE (department, name)
    )""",
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
    """CREATE TABLE IF NOT EXISTS knowledge_graph_features (
        knowledge_id TEXT NOT NULL, algorithm_version TEXT NOT NULL,
        revision BIGINT NOT NULL, content_hash TEXT NOT NULL,
        published_at TEXT NOT NULL, features_json TEXT NOT NULL,
        PRIMARY KEY (knowledge_id, algorithm_version)
    )""",
)


class AutomaticPublicationConflict(RuntimeError):
    """A pinned processing decision no longer matches the current catalogue."""


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

    def _roles_ready(self, connection):
        if connection is None:
            return False
        if self.database_url:
            return connection.execute("SELECT to_regclass('knowledge_roles') IS NOT NULL AS ready").fetchone()["ready"]
        return connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_roles'").fetchone() is not None

    def register_role(self, department, name, *, existing_department=False):
        """Register an explicit role; portal additions require an existing department."""
        department, name = _role_label(department, "department"), _role_label(name, "role_name")
        role_id = "role_" + hashlib.sha256(_json([department, name]).encode()).hexdigest()[:24]
        with self._connection(write=True) as connection:
            if existing_department and not self._execute(connection,
                    "SELECT 1 FROM knowledge_roles WHERE department=? LIMIT 1", (department,)).fetchone():
                raise ValueError("role_department_not_found")
            self._execute(connection, """INSERT INTO knowledge_roles (role_id,department,name,created_at)
                VALUES (?,?,?,?) ON CONFLICT (department,name) DO NOTHING""",
                (role_id, department, name, datetime.now(timezone.utc).isoformat()))
            return dict(self._execute(connection, "SELECT role_id,department,name FROM knowledge_roles WHERE department=? AND name=?",
                                      (department, name)).fetchone())

    def role(self, role_id):
        validate_role_id(role_id)
        with self._connection() as connection:
            if not self._roles_ready(connection):
                return None
            row = self._execute(connection, "SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?", (role_id,)).fetchone()
            return dict(row) if row is not None else None

    def bind_role(self, entry, *, create_from_sop=False):
        """Validate explicit membership. Legacy records remain unassigned."""
        if not entry.get("role_id") and not entry.get("role_name"):
            return entry
        department = _role_label(entry.get("department"), "department")
        name = _role_label(entry.get("role_name"), "role_name")
        if entry.get("role_id"):
            role = self.role(entry["role_id"])
            if role is None:
                raise ValueError("unknown_role_id")
        elif create_from_sop and knowledge_category(entry) == "sop":
            role = self.register_role(department, name)
        else:
            raise ValueError("role_id_required")
        if (role["department"], role["name"]) != (department, name):
            raise ValueError("role_metadata_mismatch")
        return {**entry, "role_id": role["role_id"], "department": department, "role_name": name}

    def uploader_roles_ready(self, connection):
        if self.database_url:
            return connection.execute("SELECT to_regclass('knowledge_uploader_roles') IS NOT NULL AS ready").fetchone()["ready"]
        return connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_uploader_roles'").fetchone() is not None

    def uploader_role(self, connection, principal):
        # Reads on an older installation must not run a schema migration.
        if principal is None or not self.uploader_roles_ready(connection):
            return None
        row = self._execute(connection, """SELECT u.*,r.department,r.name FROM knowledge_uploader_roles u
            JOIN knowledge_roles r ON r.role_id=u.role_id WHERE u.principal=?""", (principal,)).fetchone()
        return dict(row) if row is not None else None

    def bind_uploader_role(self, connection, entry, previous=None, *, principal=None):
        """Resolve a declared employee position against existing roles only."""
        evidence = dict(entry.get("evidence", {}))
        evidence.pop("uploader_role_confirmation", None)
        entry = {**entry, "evidence": evidence}
        position = evidence.get("source_uploader_position")
        label = lambda value: " ".join(value.split()) if isinstance(value, str) else ""
        position, department = label(position), label(evidence.get("source_uploader_department"))
        # These legacy placeholders were never accepted as employee declarations.
        if position in {"未知", "未确认", "待补充", "未取得", "未提供", "待填写"}:
            position = ""
        if previous and previous.get("role_id"):
            fields = ("role_id", "department", "role_name")
            declared = label(previous.get("evidence", {}).get("source_uploader_position"))
            declared_department = label(previous.get("evidence", {}).get("source_uploader_department"))
            if (any(entry.get(field) and entry[field] != previous[field] for field in fields)
                    or position and position not in {label(previous["role_name"]), declared}
                    or department and department not in {label(previous["department"]), declared_department}):
                raise ValueError("uploader_role_conflict")
            return {**entry, **{field: previous[field] for field in fields}}
        if previous is not None:
            # A historically unassigned source remains unassigned; a new alias
            # is not authority to migrate its existing membership.
            if entry.get("role_id") or entry.get("role_name"):
                raise ValueError("uploader_role_conflict")
            return entry
        # Shared MCP credentials are transport identities, never employee defaults.
        # Trusted item/alias assignments are applied by the receipt worker before import.
        if entry.get("role_id") or entry.get("role_name"):
            return entry
        if not position:
            return entry
        roles = [dict(row) for row in connection.execute("SELECT role_id,department,name FROM knowledge_roles")]
        matches = [role for role in roles if label(role["name"]) == position]
        if not matches:
            code = "uploader_position_is_department" if any(label(role["department"]) == position for role in roles) else "uploader_role_not_found"
            raise ValueError(code)
        if department:
            matches = [role for role in matches if label(role["department"]) == department]
            if not matches:
                raise ValueError("uploader_department_mismatch")
        if len(matches) != 1:
            raise ValueError("uploader_role_ambiguous")
        role = matches[0]
        return {**entry, "role_id": role["role_id"], "department": role["department"], "role_name": role["name"]}

    def roles(self):
        with self._connection() as connection:
            if not self._roles_ready(connection):
                return []
            roles = [dict(row) for row in connection.execute("SELECT role_id,department,name FROM knowledge_roles ORDER BY department,name,role_id")]
        counts = {role["role_id"]: {"sop": 0, "skill": 0, "other": 0} for role in roles}
        for item in self.published_catalog():
            if item.get("role_id") in counts:
                counts[item["role_id"]][knowledge_category(item)] += 1
        return [{**role, "counts": counts[role["role_id"]], "total": sum(counts[role["role_id"]].values())} for role in roles]

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
        payload = normalize_entry(self.bind_role(normalize_entry(entry), create_from_sop=True))
        knowledge_id = payload["knowledge_id"]
        payload_hash = payload["payload_hash"]
        ref = None
        if self.body_storage == "oss":
            # Upload and verify before opening a catalogue transaction. A failed
            # write must never create a revision pointing at an unreadable body.
            ref = self._objects().put(payload["content"])
        with self._connection(write=True) as connection:
            self._lock(connection, knowledge_id)
            return self._import_draft(connection, payload, payload_hash, ref, allow_new_revision=allow_new_revision)

    def _import_draft(self, connection, payload, payload_hash, content_ref=None, *, allow_new_revision=True):
        """Import a normalized, verified body into the caller's locked transaction."""
        if type(allow_new_revision) is not bool or payload_hash != payload["payload_hash"]:
            raise ValueError("invalid normalized draft")
        knowledge_id = payload["knowledge_id"]
        stored_payload = dict(payload)
        if content_ref is not None:
            if (content_ref["sha256"] != payload["content_hash"]
                    or content_ref["byte_length"] != len(payload["content"].encode("utf-8"))):
                raise RuntimeError("company knowledge body reference does not match its content")
            stored_payload.pop("content")
            stored_payload["content_ref"] = content_ref
        elif self.body_storage == "oss":
            raise ValueError("OSS draft requires a verified body reference")
        asset = self._execute(connection, "SELECT * FROM knowledge_assets WHERE knowledge_id = ?", (knowledge_id,)).fetchone()
        if asset:
            latest = self._record(connection, knowledge_id, asset["latest_revision"], hydrate=False)
            if (latest.get("evidence", {}).get("ingestion_source", "legacy")
                    != payload.get("evidence", {}).get("ingestion_source", "legacy")):
                raise ValueError("source_ingestion_source_conflict")
            if latest["payload_hash"] == payload_hash:
                latest.pop("content_ref", None)
                return {**latest, "content": payload["content"], "generation": asset["generation"]}
            if not allow_new_revision:
                raise ValueError("batch import cannot create a new revision of an existing asset")
        revision = asset["latest_revision"] + 1 if asset else 1
        generation = asset["generation"] + 1 if asset else 1
        record = {**stored_payload, "revision": revision, "created_at": datetime.now(timezone.utc).isoformat()}
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
            return self._publish(connection, record, expected_generation, confirmed_by)

    def _publish(self, connection, record, generation, confirmed_by):
        knowledge_id, revision = record["knowledge_id"], record["revision"]
        published_at = datetime.now(timezone.utc).isoformat()
        generation += 1
        self._execute(connection, "UPDATE knowledge_revisions SET was_published = 1, confirmed_by = ?, published_at = ? WHERE knowledge_id = ? AND revision = ?", (confirmed_by, published_at, knowledge_id, revision))
        self._execute(connection, "UPDATE knowledge_assets SET published_revision = ?, generation = ?, confirmed_by = ?, published_at = ?, status = 'published' WHERE knowledge_id = ?", (revision, generation, confirmed_by, published_at, knowledge_id))
        return {**record, "generation": generation, "confirmed_by": confirmed_by, "published_at": published_at, "status": "published"}

    def publish_automatic(self, knowledge_id, revision, *, expected_generation, confirmed_by,
                          transaction_guard=None, transaction_complete=None):
        """Commit the latest candidate and its processing result in one transaction.

        guard only validates the processing lease after the knowledge lock. It
        must not acquire a preceding governance lock or perform remote I/O.
        complete(connection, published) must only write through that connection.
        """
        knowledge_id = _knowledge_id(knowledge_id)
        confirmed_by = _text(confirmed_by, "confirmed_by")
        if type(expected_generation) is not int or expected_generation < 1:
            raise ValueError("expected_generation must be a positive integer")
        if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
            raise ValueError("revision must be a positive int64")
        record = self.snapshot(knowledge_id, revision)
        with self._connection(write=True) as connection:
            # Match governance imports so cross-source publication checks remain
            # stable across automatic publishers until the pointer is committed.
            self._lock(connection, "write:knowledge-governance")
            self._lock(connection, knowledge_id)
            if transaction_guard is not None:
                transaction_guard(connection)
            asset = self._asset(connection, knowledge_id)
            if asset["latest_revision"] != revision:
                raise AutomaticPublicationConflict("automatic publication requires the latest revision")
            if asset["published_revision"] == revision:
                # Recovery must not increment the generation or replace attribution.
                if asset["generation"] != expected_generation + 1:
                    raise AutomaticPublicationConflict("knowledge changed after automatic publication")
                published = {**record, "generation": asset["generation"], "status": "published",
                             "confirmed_by": asset["confirmed_by"], "published_at": asset["published_at"]}
            else:
                if asset["generation"] != expected_generation or record["status"] != "draft":
                    raise AutomaticPublicationConflict("knowledge changed during automatic publication")
                published = self._publish(connection, record, expected_generation, confirmed_by)
            if transaction_complete is not None:
                transaction_complete(connection, published)
            return published

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
            # A new private draft changes generation, not the public version.
            if (current["published_revision"] != record["revision"]
                    or current["published_at"] != record["published_at"]):
                return None
            record["generation"] = current["generation"]
        return record

    def published_count(self) -> int:
        """Count current public assets without reading drafts, bodies or chunks."""
        with self._connection() as connection:
            if connection is None:
                return 0
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_assets WHERE published_revision IS NOT NULL"
            ).fetchone()
            return int(row["total"])

    def _uploader_receipts(self, connection):
        # Legacy catalogues have no governance table; declarations alone are not attribution.
        ready = (connection.execute("SELECT to_regclass('knowledge_submissions') AS name").fetchone()["name"]
                 if self.database_url else connection.execute(
                     "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_submissions'").fetchone())
        if not ready:
            return {}
        rows = connection.execute("""SELECT s.knowledge_id,s.revision,s.receipt_id FROM knowledge_submissions s
            JOIN knowledge_assets a ON a.knowledge_id=s.knowledge_id AND a.published_revision=s.revision
            WHERE s.disposition IN ('new','new_revision') AND s.owned_knowledge_id=s.knowledge_id""")
        return {(row["knowledge_id"], row["revision"]): row["receipt_id"] for row in rows}

    def published_uploader_position(self, record):
        with self._connection() as connection:
            return _uploader_position(record, self._uploader_receipts(connection)) if connection is not None else None

    def published_catalog(self) -> list[dict]:
        """Project current public metadata without hydrating OSS or newer drafts."""
        with self._connection() as connection:
            if connection is None:
                return []
            rows = connection.execute("""SELECT a.knowledge_id, a.published_revision AS revision,
                a.published_at, r.record_json FROM knowledge_assets a
                JOIN knowledge_revisions r ON a.knowledge_id = r.knowledge_id
                AND a.published_revision = r.revision
                ORDER BY a.published_at DESC, a.knowledge_id""")
            receipts = self._uploader_receipts(connection)
            result = []
            for row in rows:
                record = json.loads(row["record_json"])
                evidence = record.get("evidence", {})
                related = evidence.get("related_assets", [])
                result.append({
                    "knowledge_id": row["knowledge_id"], "revision": row["revision"],
                    "published_at": row["published_at"], "title": record["title"],
                    "kind": record.get("kind", "未知"), "source_type": evidence.get("source_type"),
                    "scenarios": record.get("scenarios", []),
                    "uploader_position": _uploader_position(record, receipts),
                    "role_id": record.get("role_id"), "department": record.get("department"),
                    "role_name": record.get("role_name"), "category": knowledge_category(record),
                    "related_knowledge_ids": sorted({item["knowledge_id"] for item in related
                        if isinstance(item, dict) and isinstance(item.get("knowledge_id"), str)})
                        if isinstance(related, list) else [],
                })
            public_ids = {item["knowledge_id"] for item in result}
            for item in result:
                item["related_knowledge_ids"] = [key for key in item["related_knowledge_ids"]
                    if key in public_ids and key != item["knowledge_id"]]
            return result

    def graph_internal_snapshot(self) -> list[dict]:
        """Private projection for derived graph features, never an HTTP payload."""
        with self._connection() as connection:
            if connection is None:
                return []
            rows = connection.execute("""SELECT a.knowledge_id, a.published_revision AS revision,
                a.published_at, r.record_json FROM knowledge_assets a
                JOIN knowledge_revisions r ON a.knowledge_id=r.knowledge_id
                AND a.published_revision=r.revision ORDER BY a.published_at DESC,a.knowledge_id""")
            receipts = self._uploader_receipts(connection)
            result = []
            for row in rows:
                record = json.loads(row["record_json"])
                evidence = record.get("evidence", {})
                related = evidence.get("related_assets", [])
                result.append({
                    "knowledge_id": row["knowledge_id"], "revision": row["revision"],
                    "published_at": row["published_at"], "content_hash": record["content_hash"],
                    "title": record["title"], "kind": record.get("kind", "未知"),
                    "source_type": evidence.get("source_type"), "uploader_position": _uploader_position(record, receipts),
                    "source_namespace": evidence.get("governance_receipt_id"),
                    "source_aliases": [{"name": source.get("name", ""), "locator": source.get("locator", "")}
                                       for source in record.get("sources", [])],
                    "recorded_related_ids": sorted({item["knowledge_id"] for item in related
                        if isinstance(item, dict) and isinstance(item.get("knowledge_id"), str)})
                        if isinstance(related, list) else [],
                })
            return result

    def _graph_features_ready(self, connection):
        if connection is None:
            return False
        if self.database_url:
            row = connection.execute("""SELECT cache.relnamespace=assets.relnamespace AS ready
                FROM pg_class cache JOIN pg_class assets ON assets.oid=to_regclass('knowledge_assets')
                WHERE cache.oid=to_regclass('knowledge_graph_features')""").fetchone()
            return bool(row and row["ready"])
        return connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_graph_features'").fetchone() is not None

    def graph_features(self, algorithm_version, *, limit=1000):
        """Load only derived features for current publications; never hydrate bodies."""
        with self._connection() as connection:
            if not self._graph_features_ready(connection):
                return []
            rows = self._execute(connection, """SELECT f.* FROM knowledge_graph_features f
                JOIN knowledge_assets a ON a.knowledge_id=f.knowledge_id
                AND a.published_revision=f.revision AND a.published_at=f.published_at
                WHERE f.algorithm_version=? ORDER BY a.published_at DESC,a.knowledge_id LIMIT ?""",
                (algorithm_version, limit))
            return [dict(row) for row in rows]

    def save_graph_features(self, record, algorithm_version, features):
        """A cache writer cannot replace a newer publication's features."""
        # Legacy catalogues stay read-only until explicit initialization on PG.
        # In particular a graph request must not initialize a missing table.
        with self._connection() as connection:
            if not self._graph_features_ready(connection):
                return False
        encoded = _json(features)
        with self._connection(write=True) as connection:
            if not self._graph_features_ready(connection):
                return False
            self._lock(connection, record["knowledge_id"])
            asset = self._asset(connection, record["knowledge_id"])
            if (asset["published_revision"] != record["revision"]
                    or asset["published_at"] != record["published_at"]):
                return False
            current = self._record(connection, record["knowledge_id"], record["revision"], hydrate=False)
            if current["content_hash"] != record["content_hash"]:
                return False
            self._execute(connection, """INSERT INTO knowledge_graph_features
                (knowledge_id,algorithm_version,revision,content_hash,published_at,features_json)
                VALUES (?,?,?,?,?,?) ON CONFLICT (knowledge_id,algorithm_version) DO UPDATE SET
                revision=excluded.revision,content_hash=excluded.content_hash,
                published_at=excluded.published_at,features_json=excluded.features_json""",
                (record["knowledge_id"], algorithm_version, record["revision"], record["content_hash"],
                 record["published_at"], encoded))
            return True

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

    def published_revisions(self, department=None, scenario=None, role_id=None, category=None) -> list[dict]:
        filters = {"department": department, "scenario": scenario, "role_id": role_id, "category": category}
        for name, value in filters.items():
            if value is not None:
                filters[name] = _text(value, name)
                if len(filters[name]) > 200:
                    raise ValueError(f"{name} exceeds the 200-character limit")
        if role_id is not None and role_id != "unknown":
            validate_role_id(role_id)
        if category is not None and category not in {"sop", "skill", "other"}:
            raise ValueError("invalid_knowledge_category")
        with self._connection() as connection:
            if connection is None:
                return []
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
                if role_id is not None and (metadata.get("role_id") or "unknown") != role_id:
                    continue
                if category is not None and knowledge_category(metadata) != category:
                    continue
                item = {"knowledge_id": row["knowledge_id"], "revision": row["revision"]}
                source = metadata.get("evidence", {}).get("ingestion_source", "legacy")
                if source != "legacy":
                    item["ingestion_source"] = source
                result.append(item)
            return result
