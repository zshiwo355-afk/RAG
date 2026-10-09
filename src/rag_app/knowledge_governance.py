"""Receipt provenance, exact matching and conservative source revision handling.

The two sidecar tables do not rewrite the original catalogue. This module never
publishes; a positive publication_allowed still requires the processing rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata

from .knowledge_receipts import validate_principal
from .knowledge_store import AutomaticPublicationConflict, KnowledgeStore, _json, normalize_entry


FINGERPRINT_VERSION = "knowledge-exact-v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_VOLATILE_EVIDENCE = {
    "receipt_id", "processing_rule", "processing", "review_status", "source_verified",
    "publication_approved", "source_approval", "model_review", "source_asset_id",
    "source_base_revision", "base_revision", "submission_id", "submitted_at",
    "submitter_authenticated", "automatic_rule_reasons", "auto_publish",
    "governance_attachment_hash", "governance_fingerprint_version", "governance_receipt_id",
}
_UNKNOWN_ATTACHMENTS = {
    "script_dependency_not_provided", "referenced_material_unparsed", "unresolved_reference",
    "missing_reference_acceptance_invalid",
}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_revision_fingerprints (
        knowledge_id TEXT NOT NULL, revision BIGINT NOT NULL,
        fingerprint_version TEXT NOT NULL, content_hash TEXT NOT NULL,
        attachment_hash TEXT, comparison_hash TEXT NOT NULL, title_key TEXT NOT NULL,
        PRIMARY KEY (knowledge_id, revision)
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_submissions (
        item_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
        receipt_id TEXT NOT NULL, principal TEXT NOT NULL, source_id TEXT NOT NULL,
        source_identity TEXT NOT NULL, source_key TEXT NOT NULL,
        owned_knowledge_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, revision BIGINT NOT NULL,
        comparison_hash TEXT NOT NULL, disposition TEXT NOT NULL, reasons_json TEXT NOT NULL,
        expected_generation BIGINT NOT NULL, publication_allowed INTEGER NOT NULL,
        duplicate_of_published INTEGER NOT NULL, created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS knowledge_fingerprints_content ON knowledge_revision_fingerprints(content_hash)",
    "CREATE INDEX IF NOT EXISTS knowledge_fingerprints_comparison ON knowledge_revision_fingerprints(comparison_hash)",
    "CREATE INDEX IF NOT EXISTS knowledge_submissions_source ON knowledge_submissions(source_key, created_at, item_id)",
)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _title_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _attachment_hash(asset):
    if asset is None:
        return None
    if not isinstance(asset, dict):
        raise ValueError("asset must be an object")
    if "attachment_sha256" in asset:
        hashes = asset["attachment_sha256"]
        if hashes is None:
            return None
    else:
        issues = {issue.get("code") for issue in asset.get("issues", []) if isinstance(issue, dict)}
        if issues & _UNKNOWN_ATTACHMENTS:
            return None
        members = asset.get("members")
        if not isinstance(members, list) or not members or not asset.get("source_path"):
            return None
        if any(not isinstance(member, dict) or not member.get("source_path")
               or not isinstance(member.get("source_sha256"), str)
               or not _HASH.fullmatch(member["source_sha256"]) for member in members):
            raise ValueError("invalid verified asset members")
        hashes = [member["source_sha256"] for member in members if member["source_path"] != asset["source_path"]]
    if (not isinstance(hashes, list) or len(hashes) > 2000
            or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in hashes)):
        raise ValueError("attachment_sha256 must contain verified SHA256 values")
    return _digest(sorted(hashes))


def fingerprint_entry(record, asset=None):
    """Compare business content; receipt/workflow provenance lives in submissions.

    Missing attachment evidence is different from a verified empty manifest.
    Existing catalogue hashes can be indexed without downloading OSS bodies.
    """
    content_hash = record.get("content_hash")
    if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
        raise ValueError("record requires a verified content_hash")
    metadata = {key: record[key] for key in (
        "title", "kind", "contributor", "department", "scenarios", "chunking_version",
    ) if key in record}
    metadata["evidence"] = {key: value for key, value in record.get("evidence", {}).items()
                            if key not in _VOLATILE_EVIDENCE}
    attachment_hash = _attachment_hash(asset)
    return {"fingerprint_version": FINGERPRINT_VERSION, "content_hash": content_hash,
            "attachment_hash": attachment_hash, "title_key": _title_key(record["title"]),
            "comparison_hash": _digest({"version": FINGERPRINT_VERSION, "content": content_hash,
                                         "attachments_known": attachment_hash is not None,
                                         "attachments": attachment_hash, "metadata": metadata})}


class GovernanceStore:
    def __init__(self, knowledge_store: KnowledgeStore):
        self.catalog = knowledge_store

    def _execute(self, connection, sql, params=()):
        return self.catalog._execute(connection, sql, params)

    def initialize(self):
        """Explicit, idempotent sidecar migration; never alters catalogue rows."""
        self.catalog.initialize()
        with self.catalog._connection(write=True) as connection:
            self.catalog._lock(connection, "schema:knowledge-governance")
            for statement in _SCHEMA:
                self._execute(connection, statement)
            return {"baselined_revisions": self._baseline(connection), "fingerprint_version": FINGERPRINT_VERSION}

    def _put_fingerprint(self, connection, knowledge_id, revision, fingerprint):
        self._execute(connection, """INSERT INTO knowledge_revision_fingerprints
            (knowledge_id,revision,fingerprint_version,content_hash,attachment_hash,comparison_hash,title_key)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(knowledge_id,revision) DO NOTHING""",
            (knowledge_id, revision, *(fingerprint[key] for key in (
                "fingerprint_version", "content_hash", "attachment_hash", "comparison_hash", "title_key"))))

    def _baseline(self, connection):
        rows = self._execute(connection, """SELECT r.knowledge_id,r.revision,r.record_json
            FROM knowledge_revisions r LEFT JOIN knowledge_revision_fingerprints f
            ON f.knowledge_id=r.knowledge_id AND f.revision=r.revision
            WHERE f.knowledge_id IS NULL""").fetchall()
        for row in rows:
            # Unknown historical attachment manifests remain NULL, not empty.
            self._put_fingerprint(connection, row["knowledge_id"], row["revision"],
                                  fingerprint_entry(json.loads(row["record_json"])))
        return len(rows)

    def assert_publication_allowed(self, connection, knowledge_id, revision):
        """Final read-only check while the caller holds governance/knowledge locks."""
        candidate = self._execute(connection, """SELECT * FROM knowledge_revision_fingerprints
            WHERE knowledge_id=? AND revision=?""", (knowledge_id, revision)).fetchone()
        if candidate is None:
            raise AutomaticPublicationConflict("publication_fingerprint_unavailable")
        if candidate["attachment_hash"] is None:
            raise AutomaticPublicationConflict("attachment_manifest_unverified")
        public = self._execute(connection, """SELECT a.knowledge_id,r.record_json,
            f.comparison_hash,f.content_hash,f.attachment_hash,f.title_key
            FROM knowledge_assets a JOIN knowledge_revisions r
            ON r.knowledge_id=a.knowledge_id AND r.revision=a.published_revision
            LEFT JOIN knowledge_revision_fingerprints f
            ON f.knowledge_id=r.knowledge_id AND f.revision=r.revision
            WHERE a.knowledge_id<>? AND
            (f.content_hash=? OR f.title_key=? OR f.knowledge_id IS NULL)""",
            (knowledge_id, candidate["content_hash"], candidate["title_key"])).fetchall()
        conflict = False
        for row in public:
            # A legacy writer may publish after the last baseline. Compare its
            # stored metadata without OSS reads or changing the sidecar here.
            other = dict(row) if row["comparison_hash"] is not None else fingerprint_entry(json.loads(row["record_json"]))
            if other["comparison_hash"] == candidate["comparison_hash"] and other["attachment_hash"] is not None:
                raise AutomaticPublicationConflict("published_duplicate_before_publication")
            if ((other["content_hash"] == candidate["content_hash"] or other["title_key"] == candidate["title_key"])
                    and other["comparison_hash"] != candidate["comparison_hash"]):
                conflict = True
        if conflict:
            raise AutomaticPublicationConflict("possible_content_conflict")

    @staticmethod
    def _identity(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
            raise ValueError("invalid " + label)
        return value

    def _result(self, connection, submission, content):
        record = self.catalog._record(connection, submission["knowledge_id"], submission["revision"], hydrate=False)
        record.pop("content_ref", None)
        # The caller already owns these submitted bytes; never hydrate a private
        # target or silently return another revision's body from OSS.
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != record["content_hash"]:
            raise RuntimeError("submission body does not match its bound revision")
        record.update(content=content, generation=submission["expected_generation"])
        return {"record": record, "disposition": submission["disposition"],
                "reasons": json.loads(submission["reasons_json"]),
                "expected_generation": submission["expected_generation"],
                "publication_allowed": bool(submission["publication_allowed"]),
                "duplicate_of_published": bool(submission["duplicate_of_published"]),
                "source_key": submission["source_key"]}

    def import_asset(self, entry, asset, *, item_id, receipt_id, principal, source_id,
                     source_identity, base_revision=None, guard=None):
        """Bind one submission and its draft atomically, retaining publication pins.

        base_revision must come from a trusted source baseline, never receipt
        arrival time. None deliberately creates review-only source updates.
        """
        validate_principal(principal)
        for value, name in ((item_id, "item_id"), (receipt_id, "receipt_id"),
                            (source_id, "source_id"), (source_identity, "source_identity")):
            self._identity(value, name)
        if base_revision is not None and (type(base_revision) is not int or not 1 <= base_revision <= 2**63 - 1):
            raise ValueError("base_revision must be a positive int64")
        payload = normalize_entry(entry)
        fingerprint = fingerprint_entry(payload, asset)
        source_key = _digest([principal, source_id, source_identity])
        request_hash = _digest([receipt_id, principal, source_id, source_identity,
                                payload["knowledge_id"], fingerprint, base_revision])
        content_ref = None
        if self.catalog.body_storage == "oss":
            content_ref = self.catalog._objects().put(payload["content"])
            if (content_ref["sha256"] != payload["content_hash"]
                    or content_ref["byte_length"] != len(payload["content"].encode("utf-8"))):
                raise RuntimeError("company knowledge body reference does not match its content")
        with self.catalog._connection(write=True) as connection:
            # ponytail: one governance writer matches the existing single worker;
            # use ordered fingerprint/source locks if real throughput requires it.
            self.catalog._lock(connection, "write:knowledge-governance")
            previous = self._execute(connection, "SELECT * FROM knowledge_submissions WHERE item_id=?", (item_id,)).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise ValueError("submission_idempotency_conflict")
                self.catalog._lock(connection, previous["knowledge_id"])
                if guard is not None:
                    guard(connection)
                return self._result(connection, previous, payload["content"])
            history = self._execute(connection, "SELECT * FROM knowledge_submissions WHERE source_key=? ORDER BY created_at,item_id",
                                    (source_key,)).fetchall()
            owned_id = history[0]["owned_knowledge_id"] if history else payload["knowledge_id"]
            if not history and self._execute(connection, "SELECT 1 FROM knowledge_assets WHERE knowledge_id=?", (owned_id,)).fetchone():
                # An incoming ID is not authority to adopt an existing private or
                # legacy asset. Keep the new source in its own stable namespace.
                owned_id = "kg_" + source_key[:48]
                if self._execute(connection, "SELECT 1 FROM knowledge_assets WHERE knowledge_id=?", (owned_id,)).fetchone():
                    raise ValueError("source_identity_conflict")
            self._baseline(connection)
            # Only current published pointers participate in cross-source lookup.
            published = self._execute(connection, """SELECT f.*,a.generation FROM knowledge_revision_fingerprints f
                JOIN knowledge_assets a ON a.knowledge_id=f.knowledge_id AND a.published_revision=f.revision
                WHERE f.content_hash=? OR f.title_key=? ORDER BY f.knowledge_id""",
                (fingerprint["content_hash"], fingerprint["title_key"])).fetchall()
            own_versions = [row for row in history if row["knowledge_id"] == owned_id]
            matching = next((row for row in reversed(own_versions)
                             if fingerprint["attachment_hash"] is not None
                             and row["comparison_hash"] == fingerprint["comparison_hash"]), None)
            public_match = next((row for row in published
                                 if row["comparison_hash"] == fingerprint["comparison_hash"]
                                 and row["attachment_hash"] is not None), None)
            target_id = matching["knowledge_id"] if matching else public_match["knowledge_id"] if public_match else owned_id
            self.catalog._lock(connection, target_id)
            if guard is not None:
                guard(connection)
            reasons, duplicate = [], False
            if matching is not None:
                current = self.catalog._asset(connection, owned_id)
                old = matching["revision"] != current["latest_revision"]
                disposition = "old_replay" if old else "duplicate"
                reasons = ["older_source_revision"] if old else json.loads(matching["reasons_json"])
                revision, generation = matching["revision"], matching["expected_generation"]
                allowed = not old and bool(matching["publication_allowed"])
                status = self.catalog._record(connection, owned_id, revision, hydrate=False)["status"]
                if (not old and base_revision == current["latest_revision"]
                        and status == "draft"
                        and set(reasons) & {"source_base_revision_required", "source_base_revision_conflict"}):
                    # A new submission may explicitly acknowledge the current
                    # candidate. An item retry above never refreshes this pin.
                    reasons = [reason for reason in reasons if reason not in {
                        "source_base_revision_required", "source_base_revision_conflict"}]
                    generation = current["generation"]
                    allowed = not reasons
                if not old and any(row["knowledge_id"] != owned_id
                                   and (row["title_key"] == fingerprint["title_key"] or row["content_hash"] == fingerprint["content_hash"])
                                   and row["comparison_hash"] != fingerprint["comparison_hash"] for row in published):
                    reasons.append("possible_content_conflict")
                    allowed = False
                if not old and current["published_revision"] == revision:
                    duplicate, allowed = True, False
                elif not old and status == "withdrawn":
                    reasons.append("previously_withdrawn_revision")
                    allowed = False
            elif public_match is not None:
                current = self.catalog._asset(connection, target_id)
                # A publisher or withdrawal does not take the governance lock.
                # Verify its pointer again after acquiring the knowledge lock.
                if current["published_revision"] != public_match["revision"] or current["generation"] != public_match["generation"]:
                    raise RuntimeError("published_match_changed_retry")
                disposition, duplicate, allowed = "duplicate_published", True, False
                revision, generation = public_match["revision"], public_match["generation"]
            else:
                current = self._execute(connection, "SELECT * FROM knowledge_assets WHERE knowledge_id=?", (owned_id,)).fetchone()
                if current is not None and not history:
                    raise ValueError("source_identity_conflict")
                if fingerprint["attachment_hash"] is None:
                    reasons.append("attachment_manifest_unverified")
                if current is not None:
                    if base_revision is None:
                        reasons.append("source_base_revision_required")
                    elif base_revision != current["latest_revision"]:
                        reasons.append("source_base_revision_conflict")
                elif history:
                    # Reusing another source's publication never grants update authority.
                    reasons.append("shared_asset_update_requires_review")
                elif base_revision is not None:
                    reasons.append("source_base_revision_conflict")
                if any(row["knowledge_id"] != owned_id and row["content_hash"] == fingerprint["content_hash"]
                       and row["attachment_hash"] is None for row in published):
                    reasons.append("legacy_attachment_manifest_unverified")
                if any(row["knowledge_id"] != owned_id
                       and (row["title_key"] == fingerprint["title_key"] or row["content_hash"] == fingerprint["content_hash"])
                       and row["comparison_hash"] != fingerprint["comparison_hash"] for row in published):
                    reasons.append("possible_content_conflict")
                payload = normalize_entry({**payload, "knowledge_id": owned_id,
                    "evidence": {**payload["evidence"], "governance_attachment_hash": fingerprint["attachment_hash"],
                                 "governance_fingerprint_version": FINGERPRINT_VERSION, "governance_receipt_id": receipt_id}})
                record = self.catalog._import_draft(connection, payload, payload["payload_hash"], content_ref)
                revision, generation = record["revision"], record["generation"]
                self._put_fingerprint(connection, owned_id, revision, fingerprint)
                disposition = "new_revision" if current is not None else "new"
                allowed = not reasons
            submission = {"item_id": item_id, "request_hash": request_hash, "receipt_id": receipt_id,
                          "principal": principal, "source_id": source_id, "source_identity": source_identity,
                          "source_key": source_key, "owned_knowledge_id": owned_id, "knowledge_id": target_id,
                          "revision": revision, "comparison_hash": fingerprint["comparison_hash"],
                          "disposition": disposition, "reasons_json": _json(sorted(set(reasons))),
                          "expected_generation": generation, "publication_allowed": int(allowed),
                          "duplicate_of_published": int(duplicate), "created_at": datetime.now(timezone.utc).isoformat()}
            self._execute(connection, """INSERT INTO knowledge_submissions
                (item_id,request_hash,receipt_id,principal,source_id,source_identity,source_key,owned_knowledge_id,
                 knowledge_id,revision,comparison_hash,disposition,reasons_json,expected_generation,
                 publication_allowed,duplicate_of_published,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(submission.values()))
            return self._result(connection, submission, payload["content"])
