from __future__ import annotations

from pathlib import Path

import pytest

import ingest_content
import plan_ingest_changes
from rag_app.ingest_manifest import append_manifest_record
from conftest import read_jsonl, write_json


def plan_record(action: str = "new") -> dict:
    return {
        "source_path": "素材/sample_product.xlsx",
        "source_type": "product_excel",
        "source_sha1": "sha1-a",
        "source_doc_id": "product_excel__sha1-a",
        "modified_time": "2026-01-01T00:00:00+08:00",
        "size": 10,
        "impact_types": ["product_text"],
        "affected_types": ["product_text"],
        "vector_tables": ["text_docs"],
        "product_id": "unknown",
        "suggested_action": action,
    }


@pytest.mark.offline
def test_default_dry_run_does_not_execute(monkeypatch, tmp_path: Path) -> None:
    executed = {"value": False}
    monkeypatch.setattr(ingest_content, "ROOT", tmp_path)
    monkeypatch.setattr(ingest_content, "build_plan", lambda _root, _sources: [plan_record()])
    monkeypatch.setattr(ingest_content, "execute_plan", lambda *args, **kwargs: executed.__setitem__("value", True))

    monkeypatch.setattr(
        "sys.argv",
        ["ingest_content.py", "--source", "素材", "--plan-output", str(tmp_path / "plan.jsonl")],
    )

    assert ingest_content.main() == 0
    assert executed["value"] is False


@pytest.mark.offline
def test_execute_requires_yes_when_not_confirmed(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")

    with pytest.raises(SystemExit):
        ingest_content.confirm_or_exit(False)


@pytest.mark.offline
def test_records_to_process_skips_unchanged_by_default() -> None:
    records = [plan_record("unchanged"), plan_record("new")]

    selected = ingest_content.records_to_process(records, force=False, skip_existing=True)

    assert [item["suggested_action"] for item in selected] == ["new"]


@pytest.mark.offline
def test_records_to_process_force_keeps_unchanged() -> None:
    records = [plan_record("unchanged"), plan_record("new")]

    selected = ingest_content.records_to_process(records, force=True, skip_existing=True)

    assert len(selected) == 2


@pytest.mark.offline
def test_status_prints_manifest_summary(tmp_path: Path, capsys) -> None:
    manifest = tmp_path / "manifest.jsonl"
    append_manifest_record(
        {
            "source_doc_id": "source-a",
            "source_path": "素材/a.xlsx",
            "source_sha1": "sha-a",
            "source_type": "product_excel",
            "status": "success",
        },
        manifest,
    )

    ingest_content.print_manifest_status(manifest)
    output = capsys.readouterr().out

    assert "Total records: 1" in output
    assert "success: 1" in output


@pytest.mark.offline
def test_check_build_prints_standard_artifact_status(tmp_path: Path, capsys) -> None:
    build_dir = tmp_path / "ingest_build"
    write_json(
        build_dir / "product_excel" / "documents.json",
        [
            {
                "page_content": "hello",
                "metadata": {
                    "doc_id": "doc-a",
                    "source_doc_id": "source-a",
                    "product_id": "prod-a",
                    "source_file": "素材/a.xlsx",
                },
            }
        ],
    )
    write_json(build_dir / "quality_report" / "documents.json", [])
    (build_dir / "product_excel" / "needs_mapping.jsonl").write_text("", encoding="utf-8")

    ingest_content.print_build_check(build_dir)
    output = capsys.readouterr().out

    assert "product_excel documents: 1" in output
    assert "empty doc_id: 0" in output


@pytest.mark.offline
def test_manifest_generated_doc_ids_are_grouped_by_source(tmp_path: Path) -> None:
    records = [
        {**plan_record(), "source_path": "素材/a.xlsx", "source_doc_id": "source-a"},
        {**plan_record(), "source_path": "素材/b.xlsx", "source_doc_id": "source-b"},
    ]
    build_dir = tmp_path / "ingest_build"
    write_json(
        build_dir / "product_excel/documents.json",
        [
            {
                "page_content": "a",
                "metadata": {
                    "doc_id": "doc-a",
                    "source_doc_id": "source-a",
                    "product_source_doc_id": "product__prod-a",
                    "source_file": "素材/a.xlsx",
                },
            },
            {
                "page_content": "b",
                "metadata": {
                    "doc_id": "doc-b",
                    "source_doc_id": "source-b",
                    "product_source_doc_id": "product__prod-b",
                    "source_file": "素材/b.xlsx",
                },
            },
        ],
    )
    write_json(build_dir / "quality_report/documents.json", [])
    manifest = tmp_path / "manifest.jsonl"
    assigned, unassigned = ingest_content.assign_generated_doc_ids(records, build_dir)

    ingest_content.append_execution_manifest(records, manifest, "success", "local build scripts: test", assigned, unassigned)

    rows = read_jsonl(manifest)
    assert rows[0]["generated_doc_ids"] == ["doc-a"]
    assert rows[1]["generated_doc_ids"] == ["doc-b"]

    matched = plan_ingest_changes.records_for_filters(rows, source_doc_id="source-a", source_sha1="", doc_ids=[])
    args = type("Args", (), {"mode": "delete-source", "source_doc_id": "source-a", "source_sha1": "", "doc_id": []})()
    plan = plan_ingest_changes.build_delete_plan(matched, args, {}, {})
    assert plan["target_doc_ids"] == ["doc-a"]
    assert "doc-b" not in plan["target_doc_ids"]


@pytest.mark.offline
def test_unassigned_generated_doc_ids_are_not_attached_to_all_sources(tmp_path: Path) -> None:
    records = [
        {**plan_record(), "source_path": "素材/a.xlsx", "source_doc_id": "source-a"},
        {**plan_record(), "source_path": "素材/b.xlsx", "source_doc_id": "source-b"},
    ]
    build_dir = tmp_path / "ingest_build"
    write_json(
        build_dir / "product_excel/documents.json",
        [
            {"page_content": "orphan", "metadata": {"doc_id": "doc-orphan"}},
        ],
    )
    write_json(build_dir / "quality_report/documents.json", [])
    manifest = tmp_path / "manifest.jsonl"
    assigned, unassigned = ingest_content.assign_generated_doc_ids(records, build_dir)

    ingest_content.append_execution_manifest(records, manifest, "success", "local build scripts: test", assigned, unassigned)

    rows = read_jsonl(manifest)
    assert unassigned == ["doc-orphan"]
    assert rows[0]["generated_doc_ids"] == []
    assert rows[1]["generated_doc_ids"] == []
    assert "unassigned_generated_doc_ids" in rows[0]["status_detail"]
