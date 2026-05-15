from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import apply_ingest_change_plan
from conftest import read_jsonl, write_json


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeModels:
    class FetchRequest:
        def __init__(self, table_name, ids, include_vector=False, output_fields=None):
            self.table_name = table_name
            self.ids = ids
            self.include_vector = include_vector
            self.output_fields = output_fields or []

    class PushDocumentsRequest:
        def __init__(self, headers=None, body=None):
            self.headers = headers or {}
            self.body = body or []


class FakeClient:
    def __init__(self, existing=None, fail_delete=None, keep_after_delete=None):
        self.existing = set(existing or [])
        self.fail_delete = set(fail_delete or [])
        self.keep_after_delete = set(keep_after_delete or [])
        self.deleted: list[str] = []
        self.push_calls = 0
        self.fetch_calls = 0

    def fetch(self, request):
        self.fetch_calls += 1
        return FakeResponse({"result": [{"fields": {"id": doc_id, "doc_id": doc_id}} for doc_id in request.ids if doc_id in self.existing]})

    def push_documents(self, table_name, pk_field, request):
        self.push_calls += 1
        doc_id = request.body[0]["fields"]["id"]
        if doc_id in self.fail_delete:
            return FakeResponse({"status": "FAIL", "message": f"failed {doc_id}"})
        self.deleted.append(doc_id)
        if doc_id not in self.keep_after_delete:
            self.existing.discard(doc_id)
        return FakeResponse({"status": "OK"})


def safe_plan(**overrides) -> dict:
    plan = {
        "mode": "delete-source",
        "dry_run": True,
        "source_doc_id": "source-a",
        "source_sha1": "sha-a",
        "target_doc_ids": ["doc-a", "doc-b"],
        "target_indexes": ["inst_text_docs"],
        "product_ids": ["prod-a"],
        "risks": [],
        "blockers": [],
        "safe_to_execute": True,
    }
    plan.update(overrides)
    return plan


def run_main(monkeypatch, tmp_path: Path, args: list[str], client: FakeClient | None = None) -> tuple[int, FakeClient | None]:
    plan_path = tmp_path / "change_plan.json"
    report_path = tmp_path / "delete_report.md"
    json_path = tmp_path / "delete_report.json"
    manifest_path = tmp_path / "manifest.jsonl"
    argv = [
        "apply_ingest_change_plan.py",
        "--plan",
        str(plan_path),
        "--report-output",
        str(report_path),
        "--json-output",
        str(json_path),
        "--manifest",
        str(manifest_path),
    ] + args
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(apply_ingest_change_plan, "load_env", lambda root: None)
    monkeypatch.setattr(apply_ingest_change_plan, "ensure_runtime_config", lambda: {"instance_id": "inst"})
    used_client = client

    def fake_create_client(config):
        if used_client is None:
            raise AssertionError("OpenSearch client should not be created")
        return used_client, FakeModels

    monkeypatch.setattr(apply_ingest_change_plan, "create_client", fake_create_client)
    return apply_ingest_change_plan.main(), used_client


@pytest.mark.offline
def test_dry_run_does_not_delete_or_create_client(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan())

    code, _ = run_main(monkeypatch, tmp_path, ["--dry-run"])

    assert code == 0
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["executed"] is False
    assert not (tmp_path / "manifest.jsonl").exists()


@pytest.mark.offline
def test_missing_change_plan_refuses(monkeypatch, tmp_path: Path) -> None:
    code, _ = run_main(monkeypatch, tmp_path, ["--dry-run"])

    assert code == 2
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["refused"] is True
    assert "not found" in payload["refused_reasons"][0]


@pytest.mark.offline
@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        (safe_plan(safe_to_execute=False), "safe_to_execute must be true"),
        (safe_plan(blockers=["extra OpenSearch docs"]), "blockers must be empty"),
        (safe_plan(target_doc_ids=[]), "target_doc_ids must be a non-empty list"),
        (safe_plan(target_doc_ids=["doc-a", "doc-a"]), "duplicate doc_id"),
    ],
)
def test_unsafe_plans_refuse(monkeypatch, tmp_path: Path, plan: dict, reason: str) -> None:
    write_json(tmp_path / "change_plan.json", plan)

    code, _ = run_main(monkeypatch, tmp_path, ["--dry-run"])

    assert code == 2
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["refused"] is True
    assert any(reason in item for item in payload["refused_reasons"])


@pytest.mark.offline
def test_require_mode_mismatch_refuses(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(mode="delete-source"))

    code, _ = run_main(monkeypatch, tmp_path, ["--require-mode", "rollback-push"])

    assert code == 2
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert any("require-mode mismatch" in item for item in payload["refused_reasons"])


@pytest.mark.offline
def test_execute_without_yes_aborts_before_client(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan())
    monkeypatch.setattr("builtins.input", lambda prompt: "no")

    code, _ = run_main(monkeypatch, tmp_path, ["--execute"])

    assert code == 3
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["refused"] is True
    assert payload["refused_reasons"] == ["confirmation not received"]
    assert not (tmp_path / "manifest.jsonl").exists()


@pytest.mark.offline
def test_execute_yes_deletes_safe_plan_and_appends_manifest(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan())
    client = FakeClient(existing=["doc-a", "doc-b"])

    code, used_client = run_main(monkeypatch, tmp_path, ["--execute", "--yes"], client)

    assert code == 0
    assert used_client.deleted == ["doc-a", "doc-b"]
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["executed"] is True
    assert payload["deleted_doc_ids"] == ["doc-a", "doc-b"]
    rows = read_jsonl(tmp_path / "manifest.jsonl")
    assert rows[-1]["event_type"] == "delete"
    assert rows[-1]["delete_status"] == "success"
    assert rows[-1]["requested_doc_ids"] == ["doc-a", "doc-b"]
    assert rows[-1]["deleted_doc_ids"] == ["doc-a", "doc-b"]


@pytest.mark.offline
def test_execute_refuses_when_current_table_not_in_target_indexes(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_indexes=["other_text_docs"]))
    client = FakeClient(existing=["doc-a", "doc-b"])

    code, used_client = run_main(monkeypatch, tmp_path, ["--execute"], client)

    assert code == 2
    assert used_client.deleted == []
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["refused"] is True
    assert payload["current_table_name"] == "inst_text_docs"
    assert payload["target_index_matches_current_table"] is False
    assert "not listed" in payload["refused_reasons"][0]


@pytest.mark.offline
def test_yes_cannot_bypass_target_index_mismatch(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_indexes=["other_text_docs"]))
    client = FakeClient(existing=["doc-a", "doc-b"])

    code, used_client = run_main(monkeypatch, tmp_path, ["--execute", "--yes"], client)

    assert code == 2
    assert used_client.deleted == []
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["target_index_matches_current_table"] is False


@pytest.mark.offline
def test_matching_target_index_allows_execute(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_indexes=["inst_text_docs"]))
    client = FakeClient(existing=["doc-a", "doc-b"])

    code, used_client = run_main(monkeypatch, tmp_path, ["--execute", "--yes"], client)

    assert code == 0
    assert used_client.deleted == ["doc-a", "doc-b"]
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["target_index_matches_current_table"] is True


@pytest.mark.offline
def test_already_missing_doc_id_is_recorded(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_doc_ids=["doc-a", "doc-missing"]))
    client = FakeClient(existing=["doc-a"])

    code, used_client = run_main(monkeypatch, tmp_path, ["--execute", "--yes"], client)

    assert code == 0
    assert used_client.deleted == ["doc-a"]
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["already_missing_doc_ids"] == ["doc-missing"]
    assert payload["delete_status"] == "success"


@pytest.mark.offline
def test_single_delete_failure_continues(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_doc_ids=["doc-a", "doc-b", "doc-c"]))
    client = FakeClient(existing=["doc-a", "doc-b", "doc-c"], fail_delete=["doc-b"])

    code, used_client = run_main(monkeypatch, tmp_path, ["--execute", "--yes"], client)

    assert code == 0
    assert used_client.deleted == ["doc-a", "doc-c"]
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["deleted_doc_ids"] == ["doc-a", "doc-c"]
    assert payload["failed_doc_ids"] == ["doc-b"]
    assert payload["delete_status"] == "partial"


@pytest.mark.offline
def test_verify_after_records_verified_missing(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_doc_ids=["doc-a"]))
    client = FakeClient(existing=["doc-a"])

    code, _ = run_main(monkeypatch, tmp_path, ["--execute", "--yes", "--verify-after"], client)

    assert code == 0
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["verified_missing_doc_ids"] == ["doc-a"]
    assert payload["verify_failed_doc_ids"] == []
    assert client.fetch_calls == 2


@pytest.mark.offline
def test_verify_after_records_verify_failed(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan(target_doc_ids=["doc-a"]))
    client = FakeClient(existing=["doc-a"], keep_after_delete=["doc-a"])

    code, _ = run_main(monkeypatch, tmp_path, ["--execute", "--yes", "--verify-after"], client)

    assert code == 0
    payload = json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))
    assert payload["deleted_doc_ids"] == ["doc-a"]
    assert payload["verify_failed_doc_ids"] == ["doc-a"]
    assert payload["delete_status"] == "partial"


@pytest.mark.offline
def test_delete_report_markdown_and_json_are_generated(monkeypatch, tmp_path: Path) -> None:
    write_json(tmp_path / "change_plan.json", safe_plan())

    code, _ = run_main(monkeypatch, tmp_path, ["--dry-run"])

    assert code == 0
    assert "Ingest Change Delete Report" in (tmp_path / "delete_report.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "delete_report.json").read_text(encoding="utf-8"))["requested_doc_ids"] == ["doc-a", "doc-b"]
