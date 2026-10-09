"""Offline source fixtures only: no employee content or remote services."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "knowledge_collector_sources", Path(__file__).parents[1] / "scripts/knowledge_collector_sources.py")
sources = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sources)
ENABLED = 1_900_000_000.0


def write(path, *rows):
    with path.open("ab") as stream:
        for row in rows:
            stream.write(json.dumps(row).encode() + b"\n")


def meta(cwd, **extra):
    return {"type": "session_meta", "timestamp": ENABLED + 1,
            "payload": {"id": "session-one", "cwd": str(cwd), "timestamp": ENABLED + 1, **extra}}


def turn(cwd):
    return {"type": "turn_context", "timestamp": ENABLED + 1, "payload": {"cwd": str(cwd)}}


def message(agent, cwd, *, role="user", text="new work", timestamp=ENABLED + 2, **extra):
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    data = {"type": "message", "role": role, "id": str(timestamp) + role,
            "content": content, **extra}
    if agent == "codex":
        return {"type": "response_item", "timestamp": timestamp, "payload": data}
    return {**data, "timestamp": timestamp * 1000 if timestamp else timestamp,
            "sessionId": "session-one", "cwd": str(cwd)}


def source(tmp_path, agent, *, timestamp=ENABLED + 2):
    path = tmp_path / "source.jsonl"
    if agent == "codex":
        write(path, meta(tmp_path))
    write(path, message(agent, tmp_path, text="history", timestamp=timestamp))
    return path


@pytest.mark.parametrize("agent", ["codex", "workbuddy"])
def test_initial_eof_and_later_append_only(tmp_path, agent):
    path = source(tmp_path, agent)
    baseline = sources.baseline_jsonl(path, agent, ENABLED)
    assert not baseline["issues"]
    checkpoint = baseline["checkpoint"]
    assert checkpoint["offset"] == path.stat().st_size
    assert "history" not in json.dumps(checkpoint)
    if agent == "codex":
        write(path, turn(tmp_path))
    write(path, message(agent, tmp_path, role="assistant", text="new answer"))
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["new answer"]
    assert result["events"][0]["timestamp"] == ENABLED + 2
    assert not sources.scan_jsonl(path, result["next_checkpoint"], [tmp_path], ENABLED)["events"]


@pytest.mark.parametrize("agent", ["codex", "workbuddy"])
def test_provably_new_source_and_visible_roles_only(tmp_path, agent):
    path = source(tmp_path, agent)
    for role in ["system", "developer", "tool"]:
        write(path, message(agent, tmp_path, role=role, text="hidden"))
    write(path, message(agent, tmp_path, role="assistant", channel="analysis", text="hidden"))
    write(path, message(agent, tmp_path, role="assistant", phase="summary", text="hidden"))
    write(path, message(agent, tmp_path, role="assistant", recipient="other_agent", text="hidden"))
    write(path, message(agent, tmp_path, role="assistant", text="visible"))
    baseline = sources.baseline_jsonl(path, agent, ENABLED, new=True)
    assert baseline["checkpoint"]["offset"] == 0
    result = sources.scan_jsonl(path, baseline["checkpoint"], [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["history", "visible"]


@pytest.mark.parametrize("agent", ["codex", "workbuddy"])
def test_partial_row_waits_then_consumes_once(tmp_path, agent):
    path = source(tmp_path, agent)
    checkpoint = sources.baseline_jsonl(path, agent, ENABLED)["checkpoint"]
    if agent == "codex":
        write(path, turn(tmp_path))
        checkpoint = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)["next_checkpoint"]
    row = json.dumps(message(agent, tmp_path, text="complete later")).encode()
    with path.open("ab") as stream:
        stream.write(row[:30])
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert not result["events"] and result["next_checkpoint"]["offset"] == checkpoint["offset"]
    with path.open("ab") as stream:
        stream.write(row[30:] + b"\n")
    result = sources.scan_jsonl(path, result["next_checkpoint"], [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["complete later"]


def test_pre_activation_partial_row_never_captured(tmp_path):
    path = source(tmp_path, "codex")
    row = json.dumps(message("codex", tmp_path, text="pre activation partial")).encode()
    with path.open("ab") as stream:
        stream.write(row[:40])
    checkpoint = sources.baseline_jsonl(path, "codex", ENABLED)["checkpoint"]
    assert checkpoint["skip_partial"]
    with path.open("ab") as stream:
        stream.write(row[40:] + b"\n")
    write(path, turn(tmp_path), message("codex", tmp_path, text="fresh"))
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["fresh"]


@pytest.mark.parametrize("kind,code", [("rewrite", "source_prefix_changed"),
                                      ("truncate", "source_truncated"),
                                      ("replace", "source_replaced")])
def test_changed_source_never_rereads_history(tmp_path, kind, code):
    path = source(tmp_path, "workbuddy")
    checkpoint = sources.baseline_jsonl(path, "workbuddy", ENABLED)["checkpoint"]
    if kind == "rewrite":
        body = path.read_bytes().replace(b"history", b"changed")
        path.write_bytes(body)
    elif kind == "truncate":
        path.write_bytes(b"{}\n")
    else:
        original = path.read_bytes()
        path.rename(tmp_path / "old")
        path.write_bytes(original)
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert not result["events"] and result["next_checkpoint"] == checkpoint
    assert result["issues"] == [{"code": code}]


def test_same_identity_rename_is_safe(tmp_path):
    path = source(tmp_path, "codex")
    checkpoint = sources.baseline_jsonl(path, "codex", ENABLED)["checkpoint"]
    new_path = tmp_path / "renamed.jsonl"
    path.rename(new_path)
    write(new_path, turn(tmp_path), message("codex", tmp_path, text="after rename"))
    result = sources.scan_jsonl(new_path, checkpoint, [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["after rename"]


@pytest.mark.parametrize("fork", [{"forked_from_id": "old"}, {"parent_thread_id": "root"},
                                  {"source": {"subagent": {}}}])
def test_fork_or_subagent_is_explicitly_blocked(tmp_path, fork):
    path = tmp_path / "fork.jsonl"
    write(path, meta(tmp_path, **fork), message("codex", tmp_path))
    baseline = sources.baseline_jsonl(path, "codex", ENABLED, new=True)
    result = sources.scan_jsonl(path, baseline["checkpoint"], [tmp_path], ENABLED)
    assert not result["events"]
    assert result["issues"] == [{"code": "source_inherited_history_unverified"}]


def test_old_or_missing_timestamps_never_backfill(tmp_path):
    path = source(tmp_path, "workbuddy", timestamp=ENABLED - 1)
    baseline = sources.baseline_jsonl(path, "workbuddy", ENABLED, new=True)
    assert baseline["issues"] == [{"code": "source_predates_activation_baselined"}]
    write(path, message("workbuddy", tmp_path, text="old", timestamp=ENABLED - 1))
    write(path, message("workbuddy", tmp_path, text="new"))
    result = sources.scan_jsonl(path, baseline["checkpoint"], [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["new"]
    write(path, message("workbuddy", tmp_path, text="unknown", timestamp=None))
    failed = sources.scan_jsonl(path, result["next_checkpoint"], [tmp_path], ENABLED)
    assert failed["issues"] == [{"code": "source_event_time_unknown"}]
    assert failed["next_checkpoint"] == result["next_checkpoint"]


def test_allowed_roots_and_excluded_sessions(tmp_path):
    path = source(tmp_path, "codex")
    checkpoint = sources.baseline_jsonl(path, "codex", ENABLED, new=True)["checkpoint"]
    assert sources.scan_jsonl(path, checkpoint, [tmp_path / "another"], ENABLED)["issues"] == [
        {"code": "source_outside_allowed_roots"}]
    assert sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED,
                              excluded_sessions=("session-one",))["issues"] == [
        {"code": "source_session_excluded"}]


def test_cwd_can_leave_and_reenter_work_scope(tmp_path):
    path = source(tmp_path, "codex")
    checkpoint = sources.baseline_jsonl(path, "codex", ENABLED)["checkpoint"]
    private = tmp_path.parent / "private"
    write(path, turn(private),
          message("codex", private, text="private"))
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert not result["events"]
    assert result["issues"] == [{"code": "source_event_outside_allowed_roots"}]
    write(path, turn(tmp_path),
          message("codex", tmp_path, text="back to work"))
    result = sources.scan_jsonl(path, result["next_checkpoint"], [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["back to work"]


def test_size_limit_is_pending_and_does_not_advance(tmp_path):
    path = source(tmp_path, "workbuddy")
    assert sources.baseline_jsonl(path, "workbuddy", ENABLED, max_bytes=10) == {
        "checkpoint": None, "issues": [{"code": "source_too_large_pending"}]}
    checkpoint = sources.baseline_jsonl(path, "workbuddy", ENABLED)["checkpoint"]
    write(path, message("workbuddy", tmp_path))
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED, max_bytes=checkpoint["offset"])
    assert result["next_checkpoint"] == checkpoint
    assert result["issues"] == [{"code": "source_too_large_pending"}]


def test_non_text_blocks_and_reasoning_are_not_sources(tmp_path):
    path = source(tmp_path, "workbuddy")
    checkpoint = sources.baseline_jsonl(path, "workbuddy", ENABLED)["checkpoint"]
    write(path, {"type": "reasoning", "rawContent": "never emit", "timestamp": ENABLED * 1000},
          message("workbuddy", tmp_path, role="assistant", content=[
              {"type": "reasoning_text", "text": "never emit"},
              {"type": "output_text", "text": "safe visible answer"},
              {"type": "image", "text": "never emit"}]))
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["safe visible answer"]


def test_unknown_new_source_creation_time_stays_blocked(tmp_path):
    path = source(tmp_path, "workbuddy", timestamp=None)
    baseline = sources.baseline_jsonl(path, "workbuddy", ENABLED, new=True)
    assert baseline["issues"] == [{"code": "source_creation_time_unknown"}]
    assert not sources.scan_jsonl(path, baseline["checkpoint"], [tmp_path], ENABLED)["events"]


def test_existing_codex_waits_for_post_activation_directory_evidence(tmp_path):
    path = source(tmp_path, "codex")
    write(path, turn(tmp_path.parent / "private"))
    checkpoint = sources.baseline_jsonl(path, "codex", ENABLED)["checkpoint"]
    write(path, message("codex", tmp_path, text="unproven cwd"))
    result = sources.scan_jsonl(path, checkpoint, [tmp_path], ENABLED)
    assert not result["events"]
    assert result["issues"] == [{"code": "source_turn_context_required"}]
    write(path, turn(tmp_path), message("codex", tmp_path, text="verified work"))
    result = sources.scan_jsonl(path, result["next_checkpoint"], [tmp_path], ENABLED)
    assert [e["text"] for e in result["events"]] == ["verified work"]
