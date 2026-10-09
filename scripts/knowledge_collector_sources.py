"""Read only new user/assistant text from scoped local Codex/WorkBuddy JSONL.

Existing files are baselined at EOF. Historical bytes are hashed, never
decoded for content, retained or uploaded; only first-row identity metadata
is parsed. Rehashing the consumed prefix detects in-place rewrites at the
cost of a bounded (64 MiB per file) sequential read on each scan.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024


def _time(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            result = float(value) / (1000 if value > 100_000_000_000 else 1)
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            result = dt.timestamp()
        else:
            return None
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _hash(stream: Any, size: int) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("source_changed_during_read")
        remaining -= len(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def _metadata(row: dict[str, Any], agent: str) -> dict[str, Any] | None:
    if agent == "codex":
        data = row.get("payload")
        if row.get("type") != "session_meta" or not isinstance(data, dict):
            return None
        source = data.get("source")
        inherited = bool(data.get("parent_thread_id") or data.get("forked_from_id"))
        inherited = inherited or bool(data.get("subagent_history_start_ordinal"))
        inherited = inherited or (isinstance(source, dict) and "subagent" in source)
        session_id = data.get("id") or data.get("session_id")
        return {"session_id": session_id, "cwd": data.get("cwd"),
                "created_at": _time(data.get("timestamp") or row.get("timestamp")),
                "inherited": inherited}
    if agent == "workbuddy" and row.get("type") == "message":
        return {"session_id": row.get("sessionId"), "cwd": row.get("cwd"),
                "created_at": _time(row.get("timestamp")), "inherited": False}
    return None


def _issue(code: str) -> dict[str, str]:
    return {"code": code}


def baseline_jsonl(path: str | Path, agent: str, enabled_at: float, *,
                   new: bool = False, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Register EOF, or begin a provably new, non-inherited session at zero.

    A newly discovered pre-activation session is baselined at EOF, allowing
    later appends but no automatic historical backfill. Unknown creation time
    and inherited/forked sessions remain blocked with an explicit issue.
    """
    if agent not in {"codex", "workbuddy"} or not math.isfinite(enabled_at):
        raise ValueError("invalid_source_configuration")
    path = Path(path)
    try:
        if path.is_symlink():
            return {"checkpoint": None, "issues": [_issue("source_symlink_unsupported")]}
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > max_bytes:
                return {"checkpoint": None, "issues": [_issue("source_too_large_pending")]}
            first = stream.readline(MAX_LINE_BYTES + 1)
            if len(first) > MAX_LINE_BYTES or not first.endswith(b"\n"):
                return {"checkpoint": None, "issues": [_issue("source_metadata_pending")]}
            row = json.loads(first)
            meta = _metadata(row, agent) if isinstance(row, dict) else None
            if not meta or not all(isinstance(meta[k], str) and meta[k] for k in ("session_id", "cwd")):
                return {"checkpoint": None, "issues": [_issue("source_schema_unsupported")]}
            issues = []
            block_reason = None
            if meta["inherited"]:
                block_reason = "source_inherited_history_unverified"
            elif new and meta["created_at"] is None:
                block_reason = "source_creation_time_unknown"
            if block_reason:
                issues.append(_issue(block_reason))
            elif new and meta["created_at"] < enabled_at:
                issues.append(_issue("source_predates_activation_baselined"))
            start_new = new and not block_reason and meta["created_at"] >= enabled_at
            offset = 0 if start_new else before.st_size
            prefix = _hash(stream, offset)
            skip_partial = False
            if offset:
                stream.seek(offset - 1)
                skip_partial = stream.read(1) != b"\n"
            after = os.fstat(stream.fileno())
            named = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
                    named.st_dev, named.st_ino) != (after.st_dev, after.st_ino) or path.is_symlink():
                return {"checkpoint": None, "issues": [_issue("source_changed_during_read")]}
            checkpoint = {"agent": agent, "session_id": meta["session_id"], "cwd": meta["cwd"],
                          "offset": offset, "prefix_sha256": prefix, "dev": before.st_dev,
                          "ino": before.st_ino, "skip_partial": skip_partial,
                          "block_reason": block_reason,
                          "context_verified": agent != "codex" or start_new}
            return {"checkpoint": checkpoint, "issues": issues}
    except (OSError, ValueError, UnicodeError):
        return {"checkpoint": None, "issues": [_issue("source_unreadable_or_invalid")]}


def _in_roots(cwd: str, roots: list[str | Path]) -> bool:
    candidate = Path(cwd)
    if not candidate.is_absolute():
        return False
    candidate = candidate.resolve()
    return any(candidate.is_relative_to(Path(root).resolve()) for root in roots)


def scan_jsonl(path: str | Path, checkpoint: dict[str, Any], allowed_roots: list[str | Path],
               enabled_at: float, *, excluded_sessions: tuple[str, ...] = (),
               max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Return complete new text events; caller persists only after durable staging.

    A rename preserving file identity is accepted. Replacement, truncation,
    earlier-byte rewrite or schema uncertainty returns issues without moving
    the checkpoint. Unknown/private record kinds are never text sources.
    """
    path = Path(path)
    result: dict[str, Any] = {"events": [], "next_checkpoint": dict(checkpoint), "issues": []}

    def stop(code: str) -> dict[str, Any]:
        result["events"] = []
        result["next_checkpoint"] = dict(checkpoint)
        result["issues"] = [_issue(code)]
        return result

    if checkpoint.get("block_reason"):
        return stop(checkpoint["block_reason"])
    if checkpoint["session_id"] in excluded_sessions:
        return stop("source_session_excluded")
    if not _in_roots(checkpoint["cwd"], allowed_roots):
        return stop("source_outside_allowed_roots")
    try:
        if path.is_symlink():
            return stop("source_symlink_unsupported")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (checkpoint["dev"], checkpoint["ino"]):
                return stop("source_replaced")
            if before.st_size < checkpoint["offset"]:
                return stop("source_truncated")
            if before.st_size > max_bytes:
                return stop("source_too_large_pending")
            if _hash(stream, checkpoint["offset"]) != checkpoint["prefix_sha256"]:
                return stop("source_prefix_changed")
            current_cwd = checkpoint.get("active_cwd", checkpoint["cwd"])
            context_verified = checkpoint.get("context_verified", False)
            skip_partial = checkpoint["skip_partial"]
            offset = checkpoint["offset"]
            while stream.tell() < before.st_size:
                start = stream.tell()
                raw = stream.readline(min(MAX_LINE_BYTES + 1, before.st_size - start))
                if len(raw) > MAX_LINE_BYTES:
                    return stop("source_line_too_large_pending")
                if not raw.endswith(b"\n"):
                    break
                offset = stream.tell()
                if skip_partial:
                    skip_partial = False
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict):
                    return stop("source_schema_unsupported")
                agent = checkpoint["agent"]
                if agent == "codex":
                    data = row.get("payload")
                    if not isinstance(data, dict):
                        continue
                    if row.get("type") == "session_meta" and (
                            data.get("id") or data.get("session_id")) != checkpoint["session_id"]:
                        return stop("source_session_identity_changed")
                    if row.get("type") in {"session_meta", "turn_context"}:
                        context_time = _time(row.get("timestamp"))
                        context_verified = context_time is not None and context_time >= enabled_at
                        if row.get("type") == "session_meta" and start != 0:
                            return stop("source_session_metadata_replayed")
                        if context_verified and isinstance(data.get("cwd"), str):
                            current_cwd = data["cwd"]
                        else:
                            context_verified = False
                        continue
                    if row.get("type") != "response_item":
                        continue
                else:
                    data = row
                    if row.get("sessionId") and row["sessionId"] != checkpoint["session_id"]:
                        return stop("source_session_identity_changed")
                    if isinstance(row.get("cwd"), str):
                        current_cwd = row["cwd"]
                        context_verified = True
                    elif row.get("type") == "message":
                        context_verified = False
                if data.get("type") != "message" or data.get("role") not in {"user", "assistant"}:
                    continue
                if data.get("channel") in {"analysis", "summary"} or data.get("phase") in {"analysis", "summary"}:
                    continue
                if data.get("recipient") not in {None, "all"}:
                    continue
                timestamp = _time(row.get("timestamp"))
                if timestamp is None:
                    return stop("source_event_time_unknown")
                if timestamp < enabled_at:
                    continue
                if not context_verified:
                    result["issues"].append(_issue("source_turn_context_required"))
                    continue
                if not _in_roots(current_cwd, allowed_roots):
                    result["issues"].append(_issue("source_event_outside_allowed_roots"))
                    continue
                content = data.get("content")
                if not isinstance(content, list):
                    return stop("source_message_schema_unsupported")
                # Only explicit visible text; never providerData, tools, images or reasoning.
                parts = [part["text"] for part in content if isinstance(part, dict)
                         and part.get("type") in {"input_text", "output_text"}
                         and isinstance(part.get("text"), str)]
                if not parts:
                    continue
                identifier = data.get("id") or str(row.get("ordinal", start))
                result["events"].append({"id": str(identifier), "source_session_id": checkpoint["session_id"],
                                         "role": data["role"], "text": "\n".join(parts),
                                         "timestamp": timestamp, "locator": f"{agent}:{checkpoint['session_id']}:{identifier}",
                                         "cwd": current_cwd, "agent": agent})
            prefix = _hash(stream, offset)
            after = os.fstat(stream.fileno())
            named = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
                    named.st_dev, named.st_ino) != (after.st_dev, after.st_ino) or path.is_symlink():
                return stop("source_changed_during_read")
            result["next_checkpoint"].update(offset=offset, prefix_sha256=prefix,
                                             skip_partial=skip_partial, active_cwd=current_cwd,
                                             context_verified=context_verified)
            return result
    except (OSError, ValueError, UnicodeError):
        return stop("source_unreadable_or_invalid")
