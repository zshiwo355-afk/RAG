"""Read Hermes increments without opening its mutating SessionDB wrapper.

The caller atomically persists returned events and checkpoint, then redacts the
allowed user/assistant text before any model or upload boundary. No old message
body is read to establish or repair a baseline.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3


_SOURCES = {"cli", "desktop", "tui", "acp", "api_server", "narra"}
_HARNESS = ("Review the conversation above and update the skill library",
            "Review the conversation above and consider saving to memory")


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _issue(code: str, session_id: str = "", **counts) -> dict:
    return {"code": code, "session_sha256": _hash(session_id), **counts}


def _anchor(connection, session_id: str, cursor: int) -> str:
    # Metadata only. O(history rows); replace with a Hermes change sequence if
    # very large sessions make this scan material. Never hash historical bodies.
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT id,active,compacted,role,timestamp FROM messages "
        "WHERE session_id=? AND id<=? ORDER BY id", (session_id, cursor)
    ):
        digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _text(raw: str | None) -> str | None:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return None
    if raw.startswith("\x00json:"):
        try:
            parts = json.loads(raw[6:])
        except (ValueError, TypeError):
            return None
        if not isinstance(parts, list):
            return None
        raw = "\n".join(part["text"] for part in parts
                        if isinstance(part, dict) and part.get("type") == "text"
                        and isinstance(part.get("text"), str))
    # Some providers put reasoning in inline tags rather than dedicated columns.
    raw = re.sub(r"<(think|thinking|analysis|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)",
                 "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    return raw


def snapshot(db_path, previous: dict | None, *, allowed_roots: list[str],
             enabled_at: float, max_messages: int = 1000,
             max_chars: int = 120000, excluded_sessions: list[str] = ()) -> dict:
    """Return bounded safe-role increments; never mutate the Hermes database.

    Original text may itself contain secrets: the core's mandatory redactor must
    run before durable queueing/model use. A limit never advances past an unread
    eligible message. Rewrites are deliberately recorded as gaps, not backfilled.
    """
    if (not math.isfinite(enabled_at) or enabled_at <= 0
            or type(max_messages) is not int or max_messages < 1
            or type(max_chars) is not int or max_chars < 1):
        raise ValueError("hermes_invalid_limits")
    path = Path(db_path).expanduser().resolve(strict=True)
    roots = sorted({str(Path(root).expanduser().resolve()) for root in allowed_roots})
    excluded = set(excluded_sessions)
    stamp = path.stat()
    identity = _hash([str(path), stamp.st_dev, stamp.st_ino])
    scope = _hash([roots, sorted(excluded), enabled_at])
    issues, events = [], []
    checkpoint = {"version": 1, "identity": identity, "scope": scope,
                  "enabled_at": enabled_at, "sessions": {}}
    reset = previous is None
    if previous is not None and (previous.get("version") != 1
            or previous.get("identity") != identity or previous.get("scope") != scope):
        reset = True
        issues.append(_issue("hermes_baseline_reset"))
    old_sessions = {} if reset else previous.get("sessions", {})
    chars = scanned = unscoped = 0
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        needed = {"messages": {"id", "session_id", "role", "content", "timestamp",
                               "active", "compacted", "observed", "tool_calls", "tool_name"},
                  "sessions": {"id", "source", "started_at", "parent_session_id", "cwd"}}
        for table, columns in needed.items():
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise ValueError("hermes_schema_unsupported")
        sessions = connection.execute(
            "SELECT id,source,started_at,parent_session_id,cwd FROM sessions ORDER BY id"
        ).fetchall()
        for session in sessions:
            sid = session["id"]
            raw_cwd = session["cwd"]
            cwd = str(Path(raw_cwd).expanduser().resolve()) if raw_cwd else ""
            in_scope = bool(cwd) and any(Path(cwd).is_relative_to(Path(root)) for root in roots)
            eligible = (in_scope and session["source"] in _SOURCES
                        and sid not in excluded and not sid.startswith("cron_"))
            if not in_scope:
                unscoped += 1
            current_max = connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?", (sid,)
            ).fetchone()[0]
            session_stamp = _hash([session["source"], session["started_at"],
                                   session["parent_session_id"], cwd, eligible])
            old = old_sessions.get(sid)
            baseline = reset or not eligible
            code = None
            if not baseline and old is None:
                if session["parent_session_id"]:
                    baseline, code = True, "hermes_fork_baseline"
                elif session["started_at"] < enabled_at:
                    baseline, code = True, "hermes_restored_history_baseline"
            elif not baseline and old is not None:
                if old.get("session_stamp") != session_stamp:
                    baseline, code = True, "hermes_session_scope_reset"
                elif (type(old.get("cursor")) is not int or old["cursor"] > current_max
                      or old.get("metaanchor") != _anchor(connection, sid, old["cursor"])):
                    baseline, code = True, "hermes_history_gap"
            if code:
                issues.append(_issue(code, sid))
            cursor = current_max if baseline else (old["cursor"] if old else 0)
            skip_harness_reply = False if baseline else bool((old or {}).get("skip_harness_reply"))
            if not baseline:
                metadata = connection.execute(
                    "SELECT id,role,timestamp,active,compacted,observed,"
                    "(COALESCE(tool_calls,'') NOT IN ('','[]','null')) AS has_calls,"
                    "(COALESCE(tool_name,'')<>'') AS has_tool "
                    "FROM messages WHERE session_id=? AND id>? ORDER BY id",
                    (sid, cursor))
                for row in metadata:
                    if scanned >= max_messages:
                        issues.append(_issue("hermes_message_limit", sid))
                        break
                    if (row["role"] not in {"user", "assistant"} or not row["active"]
                            or row["compacted"] or row["observed"]
                            or row["has_calls"] or row["has_tool"]):
                        cursor, scanned = row["id"], scanned + 1
                        continue
                    timestamp = row["timestamp"]
                    if (not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)
                            or timestamp < enabled_at):
                        issues.append(_issue("hermes_pre_enable_message", sid))
                        cursor, scanned = row["id"], scanned + 1
                        continue
                    # Check size without returning body bytes to this process.
                    size = connection.execute("SELECT length(CAST(content AS BLOB)) FROM messages WHERE id=?",
                                              (row["id"],)).fetchone()[0] or 0
                    if size > max_chars * 4:
                        issues.append(_issue("hermes_message_oversized", sid))
                        break
                    raw = connection.execute("SELECT content FROM messages WHERE id=?", (row["id"],)).fetchone()[0]
                    text = _text(raw)
                    if text and len(text) > max_chars - chars:
                        issues.append(_issue("hermes_character_limit", sid))
                        break
                    if text is None:
                        issues.append(_issue("hermes_content_unsupported", sid))
                    elif text and text.startswith(_HARNESS):
                        skip_harness_reply = True
                    elif skip_harness_reply and row["role"] == "assistant":
                        skip_harness_reply = False
                    elif text:
                        skip_harness_reply = False
                        events.append({"id": f"{sid}:{row['id']}", "source_session_id": sid,
                                       "role": row["role"], "text": text, "timestamp": float(timestamp),
                                       "locator": f"hermes-session:{sid}/message/{row['id']}",
                                       "cwd": cwd, "agent": "hermes"})
                        chars += len(text)
                    cursor, scanned = row["id"], scanned + 1
            checkpoint["sessions"][sid] = {"cursor": cursor, "metaanchor": _anchor(connection, sid, cursor),
                                            "session_stamp": session_stamp,
                                            "skip_harness_reply": skip_harness_reply}
        if unscoped:
            issues.append(_issue("hermes_sessions_outside_scope", count=unscoped))
        connection.rollback()
    finally:
        connection.close()
    return {"checkpoint": checkpoint, "events": events, "issues": issues}
