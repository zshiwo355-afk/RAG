from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import plan_ingest_changes
from conftest import write_jsonl


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeModels:
    class FetchRequest:
        def __init__(self, table_name, ids, include_vector=False, output_fields=None):
            self.table_name = table_name
            self.ids = ids


class FakeClient:
    def __init__(self, rows=None, extra_rows=None):
        self.rows = rows or {}
        self.extra_rows = extra_rows or []

    def fetch(self, request):
        return FakeResponse({"result": [{"fields": self.rows[doc_id]} for doc_id in request.ids if doc_id in self.rows]})

    def search_by_source_doc_id(self, table_name, source_doc_id):
        return [row for row in self.extra_rows if row.get("source_doc_id") == source_doc_id]


def manifest_rows() -> list[dict]:
    return [
        {
            "source_doc_id": "source-a",
            "source_path": "素材/a.xlsx",
            "source_sha1": "old-sha",
            "source_type": "product_excel",
            "product_id": "prod-a",
            "generated_doc_ids": ["doc-a"],
            "pushed_doc_ids": [],
            "vector_tables": [],
            "status": "success",
        },
        {
            "event_type": "push",
            "source_doc_id": "source-a",
            "source_sha1": "old-sha",
            "source_type": "product_excel",
            "product_id": "prod-a",
            "generated_doc_ids": [],
            "pushed_doc_ids": ["doc-a", "doc-b"],
            "vector_tables": ["inst_text_docs"],
            "push_status": "success",
            "status": "success",
        },
        {
            "event_type": "verify_push",
            "source_doc_id": "source-a",
            "verified_doc_ids": ["doc-a"],
            "verify_status": "success",
            "status": "success",
        },
        {
            "event_type": "verify_retrieval",
            "source_doc_id": "source-a",
            "verified_doc_ids": ["doc-a"],
            "retrieval_status": "success",
            "status": "success",
        },
    ]


def write_manifest(path: Path) -> None:
    write_jsonl(path, manifest_rows())


@pytest.mark.offline
def test_inspect_source_finds_manifest_records(tmp_path: Path) -> None:
    records = plan_ingest_changes.records_for_filters(
        manifest_rows(),
        source_doc_id="source-a",
        source_sha1="",
        doc_ids=[],
    )

    plan = plan_ingest_changes.build_inspect_plan(records, type("Args", (), {"mode": "inspect-source", "source_doc_id": "source-a", "source_sha1": "", "doc_id": []})(), {})

    assert len(plan["manifest_records"]) == 4
    assert "doc-a" in plan["target_doc_ids"]
    assert plan["safe_to_execute"] is False


@pytest.mark.offline
def test_delete_source_dry_run_generates_target_doc_ids() -> None:
    args = type("Args", (), {"mode": "delete-source", "source_doc_id": "source-a", "source_sha1": "", "doc_id": []})()

    plan = plan_ingest_changes.build_delete_plan(manifest_rows(), args, {}, {})

    assert plan["target_doc_ids"] == ["doc-a", "doc-b"]
    assert "inst_text_docs" in plan["target_indexes"]


@pytest.mark.offline
def test_rollback_push_reads_push_report_doc_ids(tmp_path: Path) -> None:
    path = tmp_path / "push_report.json"
    path.write_text(json.dumps({"pushed_doc_ids": ["doc-x", "doc-y"]}), encoding="utf-8")

    assert plan_ingest_changes.collect_from_push_report(path) == ["doc-x", "doc-y"]


@pytest.mark.offline
def test_update_source_detects_unchanged_sha1(tmp_path: Path) -> None:
    root = tmp_path
    (root / "output").mkdir()
    write_jsonl(root / "output/ingest_plan.jsonl", [{"source_path": "素材/a.xlsx", "source_sha1": "old-sha"}])
    args = type("Args", (), {"mode": "update-source", "source_doc_id": "source-a", "source_sha1": "", "doc_id": []})()

    plan = plan_ingest_changes.build_update_plan(manifest_rows(), args, root)

    assert plan["latest_plan_source_sha1"] == "old-sha"
    assert "skip update" in plan["recommended_next_steps"][0]


@pytest.mark.offline
def test_update_source_detects_changed_sha1(tmp_path: Path) -> None:
    root = tmp_path
    (root / "output").mkdir()
    write_jsonl(root / "output/ingest_plan.jsonl", [{"source_path": "素材/a.xlsx", "source_sha1": "new-sha"}])
    args = type("Args", (), {"mode": "update-source", "source_doc_id": "source-a", "source_sha1": "", "doc_id": []})()

    plan = plan_ingest_changes.build_update_plan(manifest_rows(), args, root)

    assert plan["latest_plan_source_sha1"] == "new-sha"
    assert any("source_sha1 changed" in risk for risk in plan["risks"])


@pytest.mark.offline
def test_opensearch_mock_records_missing_and_extra_risk() -> None:
    records = manifest_rows()
    fake = FakeClient(
        rows={"doc-a": {"id": "doc-a", "doc_id": "doc-a", "source_doc_id": "source-a", "product_id": "prod-a"}},
        extra_rows=[
            {"id": "doc-a", "doc_id": "doc-a", "source_doc_id": "source-a"},
            {"id": "doc-extra", "doc_id": "doc-extra", "source_doc_id": "source-a"},
        ],
    )
    existing = plan_ingest_changes.fetch_existing_docs(fake, FakeModels, "inst_text_docs", ["doc-a", "doc-b"])
    extra = plan_ingest_changes.query_by_source_doc_id(fake, FakeModels, "inst_text_docs", "source-a")
    args = type("Args", (), {"mode": "delete-source", "source_doc_id": "source-a", "source_sha1": "", "doc_id": []})()

    plan = plan_ingest_changes.build_delete_plan(records, args, existing, extra)

    assert plan["missing_doc_ids"] == ["doc-b"]
    assert plan["unknown_doc_ids"] == ["doc-extra"]
    assert plan["safe_to_execute"] is False
    assert plan["blockers"]


@pytest.mark.offline
def test_change_plan_outputs_are_written(tmp_path: Path) -> None:
    plan = plan_ingest_changes.base_plan("delete-source", "source-a", "sha-a", True)
    plan["target_doc_ids"] = ["doc-a"]
    json_path = tmp_path / "change_plan.json"
    md_path = tmp_path / "change_plan.md"

    plan_ingest_changes.write_outputs(plan, json_path, md_path)

    assert json.loads(json_path.read_text(encoding="utf-8"))["target_doc_ids"] == ["doc-a"]
    assert "Ingest Change Plan" in md_path.read_text(encoding="utf-8")


@pytest.mark.offline
def test_execute_is_blocked(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_ingest_changes.py",
            "--mode",
            "delete-source",
            "--source-doc-id",
            "source-a",
            "--execute",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        plan_ingest_changes.main()
    assert "not implemented" in str(exc.value)


@pytest.mark.offline
def test_main_delete_source_writes_plan(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest)
    out = tmp_path / "change_plan.json"
    md = tmp_path / "change_plan.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_ingest_changes.py",
            "--mode",
            "delete-source",
            "--source-doc-id",
            "source-a",
            "--manifest",
            str(manifest),
            "--output",
            str(out),
            "--report-output",
            str(md),
        ],
    )

    assert plan_ingest_changes.main() == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["target_doc_ids"] == ["doc-a", "doc-b"]
