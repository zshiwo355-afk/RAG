from __future__ import annotations

from pathlib import Path

import pytest

import plan_ingest


@pytest.mark.offline
def test_single_excel_generates_stable_plan(temp_project: Path) -> None:
    source = temp_project / "素材" / "sample_product.xlsx"

    first = plan_ingest.build_plan(temp_project, [source])
    second = plan_ingest.build_plan(temp_project, [source])

    assert len(first) == 1
    assert first[0]["source_type"] == "product_excel"
    assert first[0]["source_sha1"] == second[0]["source_sha1"]
    assert first[0]["source_doc_id"] == second[0]["source_doc_id"]


@pytest.mark.offline
def test_explicit_root_excel_generates_product_excel_plan(temp_project: Path, fixture_dir: Path) -> None:
    source = temp_project / "root_product.xlsx"
    source.write_bytes((fixture_dir / "sample_product.xlsx").read_bytes())

    records = plan_ingest.build_plan(temp_project, [source])

    assert len(records) == 1
    assert records[0]["source_path"] == "root_product.xlsx"
    assert records[0]["source_type"] == "product_excel"
    assert records[0]["source_sha1"]
    assert records[0]["source_doc_id"].startswith("product_excel__")


@pytest.mark.offline
def test_explicit_excel_type_can_be_filtered_as_product_excel(temp_project: Path, fixture_dir: Path) -> None:
    source = temp_project / "root_product.xlsx"
    source.write_bytes((fixture_dir / "sample_product.xlsx").read_bytes())

    records = plan_ingest.build_plan(temp_project, [source])
    product_records = [record for record in records if record["source_type"] == "product_excel"]

    assert len(product_records) == 1
    assert product_records[0]["source_type"] == "product_excel"


@pytest.mark.offline
def test_default_directory_scan_still_uses_whitelist_dirs(temp_project: Path, fixture_dir: Path) -> None:
    (temp_project / "unknown").mkdir()
    (temp_project / "unknown/root_product.xlsx").write_bytes((fixture_dir / "sample_product.xlsx").read_bytes())

    records = plan_ingest.build_plan(temp_project, [temp_project / "unknown"])

    assert records == []


@pytest.mark.offline
def test_unsupported_explicit_file_extension_is_ignored(temp_project: Path) -> None:
    source = temp_project / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    records = plan_ingest.build_plan(temp_project, [source])

    assert records == []


@pytest.mark.offline
def test_quality_report_directory_generates_plan(temp_project: Path) -> None:
    records = plan_ingest.build_plan(temp_project, [temp_project / "质检报告"])

    assert len(records) == 1
    assert records[0]["source_type"] == "quality_report_pdf"
    assert records[0]["source_path"].endswith("sample_quality_report.pdf")


@pytest.mark.offline
def test_plan_records_have_required_fields(temp_project: Path) -> None:
    records = plan_ingest.build_plan(temp_project, [temp_project / "素材" / "sample_product.xlsx"])
    required = {
        "source_path",
        "source_type",
        "source_sha1",
        "source_doc_id",
        "modified_time",
        "size",
        "affected_types",
    }

    assert required.issubset(records[0])
    assert records[0]["affected_types"] == records[0]["impact_types"]


@pytest.mark.offline
def test_dry_run_plan_does_not_call_embedding_or_opensearch(monkeypatch, temp_project: Path) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("external embedding/OpenSearch call should not happen")

    monkeypatch.setattr(plan_ingest, "load_product_ids_by_source", lambda _root: {})
    monkeypatch.setattr(plan_ingest, "load_quality_report_map", lambda _root: {})
    monkeypatch.setattr(plan_ingest, "load_quality_report_manifest", lambda _root: {})
    monkeypatch.setattr(plan_ingest, "load_product_name_index", lambda _root: {})
    monkeypatch.setattr(plan_ingest, "post_json", fail_if_called, raising=False)
    monkeypatch.setattr(plan_ingest, "create_client", fail_if_called, raising=False)

    records = plan_ingest.build_plan(temp_project, [temp_project / "素材" / "sample_product.xlsx"])

    assert len(records) == 1
