from __future__ import annotations

from pathlib import Path

import pytest

import plan_cleanup_outputs as cleanup


def touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.offline
def test_cleanup_plan_protects_core_and_dry_run_does_not_delete(tmp_path: Path) -> None:
    protected_build = tmp_path / "output/ingest_build/product_excel/documents.json"
    protected_embeddings = tmp_path / "output/ingest_embeddings/product_excel/embeddings.jsonl"
    protected_mapping = tmp_path / "config/product_identity_map.csv"
    temp_report = tmp_path / "output/acceptance/old_report.json"
    cache_file = tmp_path / ".pytest_cache/v/cache/nodeids"
    for path in [protected_build, protected_embeddings, protected_mapping, temp_report, cache_file]:
        touch(path)
    touch(tmp_path / "scripts/example.py")
    touch(tmp_path / "Makefile")
    touch(tmp_path / "requirements.txt")

    records = cleanup.build_plan(tmp_path)
    by_path = {item["path"]: item for item in records}

    assert by_path["output/ingest_build/product_excel/documents.json"]["recommended_action"] == "keep"
    assert by_path["output/ingest_embeddings/product_excel/embeddings.jsonl"]["recommended_action"] == "keep"
    assert by_path["config/product_identity_map.csv"]["recommended_action"] == "keep"
    assert by_path["output/acceptance/old_report.json"]["recommended_action"] == "archive"
    assert by_path[".pytest_cache/v/cache/nodeids"]["recommended_action"] == "delete"
    assert temp_report.exists()
    assert cache_file.exists()


@pytest.mark.offline
def test_cleanup_plan_writes_reports_and_script_review_without_deleting_scripts(tmp_path: Path) -> None:
    script_path = tmp_path / "scripts/ingest_content.py"
    touch(script_path, "print('ok')\n")
    touch(tmp_path / "output/acceptance/final_supplement_push_acceptance.json")
    records = cleanup.build_plan(tmp_path)
    payload = cleanup.write_plan_json(tmp_path / "output/cleanup/cleanup_plan.json", records)
    cleanup.write_plan_md(tmp_path / "output/cleanup/cleanup_plan.md", payload)
    script_payload = cleanup.write_script_review(tmp_path, tmp_path / "output/cleanup")

    assert (tmp_path / "output/cleanup/cleanup_plan.json").exists()
    assert (tmp_path / "output/cleanup/cleanup_plan.md").exists()
    assert (tmp_path / "output/cleanup/script_review.json").exists()
    assert (tmp_path / "output/cleanup/script_review.md").exists()
    assert script_path.exists()
    assert script_payload["no_scripts_deleted"] is True
    assert any(item["path"] == "scripts/ingest_content.py" for item in script_payload["records"])


@pytest.mark.offline
def test_chinese_cleanup_plan_localizes_fields_and_keeps_core(tmp_path: Path) -> None:
    touch(tmp_path / "output/ingest_build/product_excel/documents.json")
    touch(tmp_path / "output/ingest_embeddings/product_excel/embeddings.jsonl")
    touch(tmp_path / "output/acceptance/old.json")
    touch(tmp_path / "scripts/ingest_content.py")
    records = cleanup.build_plan(tmp_path)

    payload = cleanup.write_plan_json_zh(tmp_path / "output/cleanup/cleanup_plan_中文.json", records)
    cleanup.write_plan_md_zh(tmp_path / "output/cleanup/cleanup_plan_中文.md", payload)
    script_payload = cleanup.write_script_review_zh(tmp_path, tmp_path / "output/cleanup")

    by_path = {item["path"]: item for item in payload["records"]}
    assert by_path["output/ingest_build/product_excel/documents.json"]["recommended_action"] == "keep"
    assert by_path["output/ingest_embeddings/product_excel/embeddings.jsonl"]["recommended_action"] == "keep"
    assert by_path["output/acceptance/old.json"]["recommended_action_zh"] == "归档"
    assert by_path["output/acceptance/old.json"]["category_zh"] == "验收报告"
    assert (tmp_path / "output/cleanup/cleanup_plan_中文.md").exists()
    assert script_payload["no_scripts_deleted"] is True
    assert (tmp_path / "output/cleanup/script_review_中文.md").exists()


@pytest.mark.offline
def test_archive_mode_moves_only_archive_candidates_and_keeps_delete_candidates(tmp_path: Path) -> None:
    archive_report = tmp_path / "output/acceptance/old.json"
    cache_file = tmp_path / ".pytest_cache/v/cache/nodeids"
    registry_delta = tmp_path / "output/review/product_catalog_registry_new_24.csv"
    summary_doc = tmp_path / "docs/RAG_INGEST_FINAL_SUMMARY.md"
    for path in [archive_report, cache_file, registry_delta, summary_doc]:
        touch(path)

    records = cleanup.build_plan(tmp_path)
    payload = cleanup.execute_plan(tmp_path, records, archive=True)
    cleanup.write_archive_report(tmp_path / "output/cleanup", payload)

    assert not archive_report.exists()
    assert cache_file.exists()
    assert registry_delta.exists()
    assert summary_doc.exists()
    assert payload["archived_count"] == 1
    assert payload["deleted_count"] == 0
    assert (tmp_path / payload["archive_root"] / "output/acceptance/old.json").exists()
    assert (tmp_path / "output/cleanup/final_cleanup_archive_report.json").exists()
