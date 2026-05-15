"""Helpers for the local Streamlit ingest dashboard.

This module intentionally does not call embedding APIs or OpenSearch directly.
It only validates uploads, builds existing wrapper-script commands, reads
reports, and records subprocess results.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".pdf", ".txt", ".md"}
UPLOAD_ROOT = Path("uploads/ingest")
RUN_ROOT = Path("uploads/ingest_runs")
DANGEROUS_TOKENS = {
    "embedding_execute": "YES_EMBED",
    "push_execute": "YES_PUSH",
}


def now_id() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "").name
    name = name.replace("\x00", "")
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name).strip("._")
    if not name:
        raise ValueError("filename is empty after sanitization")
    return name


def validate_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported file extension: {suffix or '<none>'}")
    return suffix


def resolve_under(root: Path, path: Path) -> Path:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes allowed root: {path}") from exc
    return path_resolved


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_upload(root: Path, filename: str, data: bytes, timestamp: str | None = None) -> dict[str, Any]:
    safe_name = sanitize_filename(filename)
    validate_extension(safe_name)
    stamp = timestamp or now_id()
    upload_base = (root / UPLOAD_ROOT).resolve()
    target_dir = resolve_under(upload_base, upload_base / stamp)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = resolve_under(upload_base, target_dir / safe_name)
    if target_path.exists():
        raise FileExistsError(f"upload target already exists: {target_path}")
    target_path.write_bytes(data)
    return {
        "original_name": filename,
        "filename": safe_name,
        "size": target_path.stat().st_size,
        "sha1": sha1_file(target_path),
        "path": str(target_path),
        "upload_dir": str(target_dir),
    }


def source_type_for_embedding(source_type: str) -> str:
    return "all" if source_type == "auto" else source_type


def build_command(
    *,
    step: str,
    uploaded_path: str | Path,
    source_type: str,
    run_dir: str | Path,
    limit: int = 3,
    python_bin: str = "python3",
) -> list[str]:
    uploaded = str(uploaded_path)
    run_path = Path(run_dir)
    wrapper_source_type = source_type_for_embedding(source_type)
    commands: dict[str, list[str]] = {
        "dry_run": [
            python_bin,
            "scripts/ingest_content.py",
            "--source",
            uploaded,
            "--dry-run",
            "--type",
            source_type,
            "--plan-output",
            str(run_path / "ingest_plan.jsonl"),
        ],
        "build": [
            python_bin,
            "scripts/ingest_content.py",
            "--source",
            uploaded,
            "--execute",
            "--type",
            source_type,
            "--yes",
            "--plan-output",
            str(run_path / "ingest_plan.jsonl"),
        ],
        "check_build": [python_bin, "scripts/ingest_content.py", "--check-build"],
        "embedding_dry_run": [
            python_bin,
            "scripts/embed_ingest_build.py",
            "--source-type",
            wrapper_source_type,
            "--dry-run",
            "--limit",
            str(limit),
        ],
        "embedding_execute": [
            python_bin,
            "scripts/embed_ingest_build.py",
            "--source-type",
            wrapper_source_type,
            "--execute",
            "--limit",
            str(limit),
            "--yes",
        ],
        "push_dry_run": [
            python_bin,
            "scripts/push_ingest_embeddings.py",
            "--source-type",
            wrapper_source_type,
            "--dry-run",
            "--limit",
            str(limit),
        ],
        "check_opensearch": [python_bin, "scripts/push_ingest_embeddings.py", "--check-opensearch"],
        "push_execute": [
            python_bin,
            "scripts/push_ingest_embeddings.py",
            "--source-type",
            wrapper_source_type,
            "--execute",
            "--limit",
            str(limit),
            "--yes",
        ],
        "verify_pushed": [
            python_bin,
            "scripts/push_ingest_embeddings.py",
            "--verify-pushed",
            "--verify-limit",
            str(limit),
        ],
        "verify_retrieval": [
            python_bin,
            "scripts/verify_retrieval.py",
            "--execute",
            "--verify-limit",
            str(limit),
            "--yes",
        ],
    }
    if step not in commands:
        raise ValueError(f"unknown dashboard step: {step}")
    return commands[step]


def required_confirmation_token(step: str) -> str | None:
    return DANGEROUS_TOKENS.get(step)


def validate_confirmation(step: str, token: str) -> bool:
    expected = required_confirmation_token(step)
    if expected is None:
        return True
    return token.strip() == expected


def tail_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def append_run_log(log_path: Path, record: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_command(
    *,
    command: list[str],
    cwd: Path,
    run_id: str,
    step: str,
    log_path: Path,
    timeout: int | None = None,
) -> dict[str, Any]:
    if not isinstance(command, list):
        raise TypeError("command must be a list")
    started_at = now_iso()
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    finished_at = now_iso()
    record = {
        "run_id": run_id,
        "step": step,
        "command": command,
        "started_at": started_at,
        "finished_at": finished_at,
        "returncode": completed.returncode,
        "stdout_tail": tail_text(completed.stdout),
        "stderr_tail": tail_text(completed.stderr),
        "status": "success" if completed.returncode == 0 else "failed",
    }
    append_run_log(log_path, record)
    return record


def read_run_logs(log_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows[-limit:]


def read_report(path: Path, max_jsonl_rows: int = 50) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "message": "尚未生成", "path": str(path)}
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines()[:max_jsonl_rows]:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"raw": line})
        return {"exists": True, "type": "jsonl", "path": str(path), "rows": rows}
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"exists": True, "type": "text", "path": str(path), "content": f"JSON parse failed: {exc}"}
        return {"exists": True, "type": "json", "path": str(path), "json": payload}
    if suffix in {".md", ".markdown"}:
        return {"exists": True, "type": "markdown", "path": str(path), "content": path.read_text(encoding="utf-8")}
    return {"exists": True, "type": "text", "path": str(path), "content": path.read_text(encoding="utf-8")}


def dashboard_report_paths(root: Path, run_dir: Path) -> list[tuple[str, Path]]:
    return [
        ("当前 ingest_plan.jsonl", run_dir / "ingest_plan.jsonl"),
        ("product_excel build_report.md", root / "output/ingest_build/product_excel/build_report.md"),
        ("quality_report build_report.md", root / "output/ingest_build/quality_report/build_report.md"),
        ("embedding_report.md", root / "output/ingest_embeddings/embedding_report.md"),
        ("push_report.md", root / "output/ingest_push/push_report.md"),
        ("push_report.json", root / "output/ingest_push/push_report.json"),
        ("verify_report.md", root / "output/ingest_push/verify_report.md"),
        ("verify_report.json", root / "output/ingest_push/verify_report.json"),
        ("retrieval_verify_report.md", root / "output/ingest_push/retrieval_verify_report.md"),
        ("retrieval_verify_report.json", root / "output/ingest_push/retrieval_verify_report.json"),
        ("change_plan.md", root / "output/ingest_changes/change_plan.md"),
        ("delete_report.md", root / "output/ingest_changes/delete_report.md"),
    ]
