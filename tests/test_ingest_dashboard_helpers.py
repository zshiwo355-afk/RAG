from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rag_app import ingest_dashboard_helpers as helpers


@pytest.mark.offline
def test_sanitize_filename_blocks_path_traversal() -> None:
    assert helpers.sanitize_filename("../../evil.xlsx") == "evil.xlsx"
    assert "/" not in helpers.sanitize_filename("a/b/c.pdf")


@pytest.mark.offline
def test_save_upload_path_stays_under_upload_root(tmp_path: Path) -> None:
    info = helpers.save_upload(tmp_path, "../sample.xlsx", b"hello", timestamp="run-a")
    path = Path(info["path"])

    assert path.exists()
    assert path.resolve().is_relative_to((tmp_path / helpers.UPLOAD_ROOT).resolve())
    assert path.name == "sample.xlsx"


@pytest.mark.offline
def test_only_whitelisted_extensions_are_allowed() -> None:
    assert helpers.validate_extension("a.xlsx") == ".xlsx"
    assert helpers.validate_extension("a.pdf") == ".pdf"
    with pytest.raises(ValueError):
        helpers.validate_extension("a.exe")


@pytest.mark.offline
def test_sha1_file_is_correct(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"abc")

    assert helpers.sha1_file(path) == hashlib.sha1(b"abc").hexdigest()


@pytest.mark.offline
def test_build_command_returns_list_not_shell_string(tmp_path: Path) -> None:
    command = helpers.build_command(
        step="dry_run",
        uploaded_path=tmp_path / "a.xlsx",
        source_type="product_excel",
        run_dir=tmp_path / "run",
        limit=3,
    )

    assert isinstance(command, list)
    assert command[:3] == ["python3", "scripts/ingest_content.py", "--source"]
    assert " ".join(command) != command


@pytest.mark.offline
def test_dangerous_steps_require_confirmation_tokens() -> None:
    assert helpers.required_confirmation_token("embedding_execute") == "YES_EMBED"
    assert helpers.required_confirmation_token("push_execute") == "YES_PUSH"
    assert helpers.required_confirmation_token("dry_run") is None


@pytest.mark.offline
def test_yes_embed_and_yes_push_validation() -> None:
    assert helpers.validate_confirmation("embedding_execute", "YES_EMBED") is True
    assert helpers.validate_confirmation("embedding_execute", " yes_embed ") is False
    assert helpers.validate_confirmation("push_execute", "YES_PUSH") is True
    assert helpers.validate_confirmation("push_execute", "YES_EMBED") is False


@pytest.mark.offline
def test_run_log_writes_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "run_log.jsonl"
    helpers.append_run_log(
        log_path,
        {
            "run_id": "run-a",
            "step": "dry_run",
            "command": ["python3", "x.py"],
            "returncode": 0,
            "status": "success",
        },
    )

    rows = helpers.read_run_logs(log_path)
    assert rows[0]["run_id"] == "run-a"
    assert rows[0]["command"] == ["python3", "x.py"]


@pytest.mark.offline
def test_missing_report_returns_friendly_message(tmp_path: Path) -> None:
    report = helpers.read_report(tmp_path / "missing.md")

    assert report["exists"] is False
    assert report["message"] == "尚未生成"


@pytest.mark.offline
def test_jsonl_report_reads_first_50_rows(tmp_path: Path) -> None:
    path = tmp_path / "report.jsonl"
    path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(60)), encoding="utf-8")

    report = helpers.read_report(path)

    assert report["exists"] is True
    assert report["type"] == "jsonl"
    assert len(report["rows"]) == 50
    assert report["rows"][0]["i"] == 0
    assert report["rows"][-1]["i"] == 49
