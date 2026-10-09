from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

import knowledge_collector_hermes as hermes


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,started_at REAL,
                              parent_session_id TEXT,cwd TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,
          role TEXT,content TEXT,timestamp REAL,active INTEGER DEFAULT 1,
          compacted INTEGER DEFAULT 0,observed INTEGER DEFAULT 0,tool_calls TEXT,
          tool_name TEXT,reasoning TEXT);
    """)
    connection.execute("INSERT INTO sessions VALUES ('old','desktop',50,NULL,?)", (str(root),))
    connection.commit()
    yield path, root, connection
    connection.close()


def add(connection, text, *, sid="old", role="user", timestamp=101, **columns):
    data = {"session_id": sid, "role": role, "content": text, "timestamp": timestamp, **columns}
    cursor = connection.execute(f"INSERT INTO messages ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                                list(data.values()))
    connection.commit()
    return cursor.lastrowid


def read(source, previous=None, **kwargs):
    path, root, _ = source
    return hermes.snapshot(path, previous, allowed_roots=[str(root)], enabled_at=100, **kwargs)


def codes(result):
    return {issue["code"] for issue in result["issues"]}


def test_initial_baseline_never_selects_body_then_append_is_incremental(source, monkeypatch):
    path, _, connection = source
    add(connection, "historical secret", timestamp=50)
    real_connect = sqlite3.connect
    def protected(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_READ and column in {"content", "reasoning"}
                            else sqlite3.SQLITE_OK)
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(hermes.sqlite3, "connect", protected)
        baseline = read(source)
    assert baseline["events"] == []
    assert "historical secret" not in json.dumps(baseline)
    before = path.read_bytes()
    new_id = add(connection, "new work")
    result = read(source, baseline["checkpoint"])
    assert result["events"] == [{"id": f"old:{new_id}", "source_session_id": "old", "role": "user",
        "text": "new work", "timestamp": 101.0, "locator": f"hermes-session:old/message/{new_id}",
        "cwd": str(source[1]), "agent": "hermes"}]
    after_append = path.read_bytes()
    assert after_append != before
    assert read(source, result["checkpoint"])["events"] == []
    assert path.read_bytes() == after_append


@pytest.mark.parametrize("rewrite", ["replace", "compact", "rewind"])
def test_metadata_mutation_creates_gap_without_reading_rewritten_body(source, rewrite, monkeypatch):
    _, _, connection = source
    add(connection, "old", timestamp=50)
    baseline = read(source)
    if rewrite == "replace":
        connection.execute("DELETE FROM messages")
    elif rewrite == "compact":
        connection.execute("UPDATE messages SET active=0,compacted=1")
    else:
        connection.execute("UPDATE messages SET active=0")
    add(connection, "old content reinserted at current timestamp")
    real_connect = sqlite3.connect
    def protected(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_READ and column == "content" else sqlite3.SQLITE_OK)
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(hermes.sqlite3, "connect", protected)
        result = read(source, baseline["checkpoint"])
    assert not result["events"]
    assert "hermes_history_gap" in codes(result)
    add(connection, "work after repaired baseline", timestamp=102)
    assert [event["text"] for event in read(source, result["checkpoint"])["events"]] == ["work after repaired baseline"]


def test_safe_roles_and_structured_text_never_return_tools_or_reasoning(source):
    _, _, connection = source
    baseline = read(source)
    for role in ["system", "developer", "tool", "session_meta"]:
        add(connection, "secret " + role, role=role)
    add(connection, "secret calling assistant", role="assistant", tool_calls='[{"name":"tool"}]')
    add(connection, "secret observed", observed=1)
    add(connection, "secret inactive", active=0)
    add(connection, "secret compacted", compacted=1)
    add(connection, "<think>secret thought</think>public answer", role="assistant", reasoning="secret reasoning")
    add(connection, "\x00json:" + json.dumps([
        {"type":"text", "text":"public text"}, {"type":"thinking", "text":"secret thinking"},
        {"type":"image_url", "image_url":{"url":"secret image"}}, {"type":"tool_use", "text":"secret tool"}]))
    result = read(source, baseline["checkpoint"])
    assert [event["text"] for event in result["events"]] == ["public answer", "public text"]
    assert "secret" not in json.dumps(result)


def test_new_root_is_collected_but_fork_and_restored_history_only_baseline(source):
    _, root, connection = source
    baseline = read(source)
    connection.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
        ("new", "desktop", 101, None, str(root)),
        ("fork", "desktop", 101, "old", str(root)),
        ("restored", "desktop", 20, None, str(root)),
    ])
    for sid in ("new", "fork", "restored"):
        add(connection, sid, sid=sid)
    result = read(source, baseline["checkpoint"])
    assert [event["text"] for event in result["events"]] == ["new"]
    assert codes(result) == {"hermes_fork_baseline", "hermes_restored_history_baseline"}


def test_scope_missing_directory_unknown_source_and_excluded_sessions(source):
    _, root, connection = source
    baseline = read(source)
    connection.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
        ("none", "desktop", 101, None, None),
        ("outside", "desktop", 101, None, str(root) + "-private"),
        ("child", "subagent", 101, "old", str(root)),
        ("cron_abc", "cli", 101, None, str(root)),
    ])
    for sid in ("none", "outside", "child", "cron_abc"):
        add(connection, "private", sid=sid)
    result = read(source, baseline["checkpoint"])
    assert not result["events"]
    assert result["issues"][-1]["count"] == 2
    add(connection, "new scope must not backfill")
    reset = read(source, result["checkpoint"], excluded_sessions=["old"])
    assert not reset["events"] and "hermes_baseline_reset" in codes(reset)


def test_changing_session_directory_establishes_new_start(source):
    _, root, connection = source
    baseline = read(source)
    add(connection, "before attachment")
    connection.execute("UPDATE sessions SET cwd=? WHERE id='old'", (str(root / "subproject"),))
    connection.commit()
    result = read(source, baseline["checkpoint"])
    assert not result["events"] and "hermes_session_scope_reset" in codes(result)


def test_pagination_never_skips_unread_messages_or_truncates_text(source):
    _, _, connection = source
    baseline = read(source)
    ids = [add(connection, value) for value in ("first", "second", "third")]
    first = read(source, baseline["checkpoint"], max_messages=1)
    assert [event["text"] for event in first["events"]] == ["first"]
    assert first["checkpoint"]["sessions"]["old"]["cursor"] == ids[0]
    second = read(source, first["checkpoint"], max_chars=6)
    assert [event["text"] for event in second["events"]] == ["second"]
    assert second["checkpoint"]["sessions"]["old"]["cursor"] == ids[1]
    third = read(source, second["checkpoint"])
    assert [event["text"] for event in third["events"]] == ["third"]


def test_oversized_structured_body_keeps_cursor_even_with_nul_prefix(source):
    _, _, connection = source
    baseline = read(source)
    add(connection, "\x00json:" + json.dumps([{"type":"text", "text":"x" * 500}]))
    result = read(source, baseline["checkpoint"], max_chars=10)
    assert not result["events"] and "hermes_message_oversized" in codes(result)
    assert result["checkpoint"]["sessions"]["old"]["cursor"] == 0
    assert len(read(source, result["checkpoint"])["events"][0]["text"]) == 500


def test_pre_enable_timestamp_and_harness_pair_are_not_collected(source):
    _, _, connection = source
    baseline = read(source)
    add(connection, "restored old", timestamp=50)
    add(connection, "Review the conversation above and update the skill library now")
    add(connection, "private curator result", role="assistant")
    result = read(source, baseline["checkpoint"], max_messages=2)
    assert not result["events"]
    result = read(source, result["checkpoint"])
    assert not result["events"]


def test_database_replacement_resets_instead_of_replaying(source, tmp_path):
    path, _, connection = source
    baseline = read(source)
    add(connection, "not replayed after restore")
    replacement = tmp_path / "copy.db"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    result = read(source, baseline["checkpoint"])
    assert not result["events"] and "hermes_baseline_reset" in codes(result)


def test_metadata_check_and_body_read_share_one_snapshot(source, monkeypatch):
    _, _, connection = source
    connection.execute("PRAGMA journal_mode=WAL")
    add(connection, "old", timestamp=50)
    baseline = read(source)
    add(connection, "new before concurrent rewrite")
    original_anchor = hermes._anchor
    rewritten = False
    def concurrent_rewrite(reader, sid, cursor):
        nonlocal rewritten
        value = original_anchor(reader, sid, cursor)
        if not rewritten:
            rewritten = True
            connection.execute("DELETE FROM messages")
            add(connection, "reinserted old history")
        return value
    with monkeypatch.context() as patch:
        patch.setattr(hermes, "_anchor", concurrent_rewrite)
        result = read(source, baseline["checkpoint"])
    assert [event["text"] for event in result["events"]] == ["new before concurrent rewrite"]
    next_result = read(source, result["checkpoint"])
    assert not next_result["events"] and "hermes_history_gap" in codes(next_result)


def test_unsupported_schema_and_invalid_limits_fail_without_modification(source):
    path, _, connection = source
    with pytest.raises(ValueError, match="hermes_invalid_limits"):
        read(source, max_messages=0)
    connection.execute("ALTER TABLE messages RENAME COLUMN active TO missing")
    connection.commit()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="hermes_schema_unsupported"):
        read(source)
    assert path.read_bytes() == before
