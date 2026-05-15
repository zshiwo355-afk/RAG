from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

import build_product_excel_pipeline


@pytest.mark.offline
def test_pipeline_dry_run_only_prints_plan(monkeypatch, temp_project: Path, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_product_excel_pipeline.py",
            "--root",
            str(temp_project),
            "--source",
            "素材/sample_product.xlsx",
            "--work-dir",
            "output/work",
            "--output-dir",
            "output/ingest_build/product_excel",
            "--dry-run",
        ],
    )

    assert build_product_excel_pipeline.main() == 0
    output = capsys.readouterr().out

    assert "Would run:" in output
    assert not (temp_project / "output/work").exists()


@pytest.mark.offline
def test_pipeline_default_skips_image_analysis(monkeypatch, temp_project: Path, capsys) -> None:
    def fake_run_step(name, command, *, cwd, dry_run):
        work_output = temp_project / "output/work/output"
        work_output.mkdir(parents=True, exist_ok=True)
        if name == "excel_clean":
            (work_output / "products_cleaned.json").write_text("[]", encoding="utf-8")
        elif name == "extract_excel_images":
            (work_output / "image_mapping.json").write_text("{}", encoding="utf-8")
        elif name == "merge_image_into_products":
            (work_output / "products_enriched.json").write_text("[]", encoding="utf-8")
        elif name == "build_documents":
            (work_output / "documents_preview_v2.json").write_text("[]", encoding="utf-8")
        elif name == "build_product_excel_docs":
            out = temp_project / "output/ingest_build/product_excel"
            out.mkdir(parents=True, exist_ok=True)
            (out / "documents.json").write_text("[]", encoding="utf-8")
            (out / "needs_mapping.jsonl").write_text("", encoding="utf-8")
        return {"name": name, "command": " ".join(command), "status": "success", "started_at": "", "finished_at": "", "error": ""}

    monkeypatch.setattr(build_product_excel_pipeline, "run_step", fake_run_step)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_product_excel_pipeline.py",
            "--root",
            str(temp_project),
            "--source",
            "素材/sample_product.xlsx",
            "--work-dir",
            "output/work",
            "--output-dir",
            "output/ingest_build/product_excel",
            "--yes",
        ],
    )

    assert build_product_excel_pipeline.main() == 0
    output = capsys.readouterr().out

    assert "Image analysis skipped" in output
    assert (temp_project / "output/work/output/image_analysis.jsonl").exists()
    assert (temp_project / "output/work/output/products_cleaned.json").exists()
    assert (temp_project / "output/work/output/products_enriched.json").exists()
    assert (temp_project / "output/work/output/documents_preview_v2.json").exists()
    assert (temp_project / "output/ingest_build/product_excel/documents.json").exists()
    assert (temp_project / "output/ingest_build/product_excel/build_report.md").exists()


@pytest.mark.offline
def test_pipeline_reuses_existing_image_analysis(monkeypatch, temp_project: Path, capsys) -> None:
    (temp_project / "output/image_analysis.jsonl").write_text('{"id":"old"}\n', encoding="utf-8")

    def fake_run_step(name, command, *, cwd, dry_run):
        work_output = temp_project / "output/work/output"
        work_output.mkdir(parents=True, exist_ok=True)
        targets = {
            "excel_clean": "products_cleaned.json",
            "extract_excel_images": "image_mapping.json",
            "merge_image_into_products": "products_enriched.json",
            "build_documents": "documents_preview_v2.json",
        }
        if name in targets:
            (work_output / targets[name]).write_text("[]" if targets[name] != "image_mapping.json" else "{}", encoding="utf-8")
        if name == "build_product_excel_docs":
            out = temp_project / "output/ingest_build/product_excel"
            out.mkdir(parents=True, exist_ok=True)
            (out / "documents.json").write_text("[]", encoding="utf-8")
            (out / "needs_mapping.jsonl").write_text("", encoding="utf-8")
        return {"name": name, "command": " ".join(command), "status": "success", "started_at": "", "finished_at": "", "error": ""}

    monkeypatch.setattr(build_product_excel_pipeline, "run_step", fake_run_step)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_product_excel_pipeline.py",
            "--root",
            str(temp_project),
            "--source",
            "素材/sample_product.xlsx",
            "--work-dir",
            "output/work",
            "--output-dir",
            "output/ingest_build/product_excel",
            "--reuse-existing-image-analysis",
            "--yes",
        ],
    )

    assert build_product_excel_pipeline.main() == 0
    assert "Reuse existing image analysis" in capsys.readouterr().out
    assert (temp_project / "output/work/output/image_analysis.jsonl").read_text(encoding="utf-8") == '{"id":"old"}\n'


@pytest.mark.offline
def test_write_report_records_failed_step(tmp_path: Path) -> None:
    report = tmp_path / "build_report.md"
    build_product_excel_pipeline.write_report(
        report,
        sources=["素材/sample_product.xlsx"],
        steps=[
            {
                "name": "excel_clean",
                "command": "python script",
                "status": "failed",
                "error": "exit_code=1",
            }
        ],
        output_dir=tmp_path / "ingest_build/product_excel",
        work_root=tmp_path / "work",
        skip_image_analysis=True,
        reuse_existing_image_analysis=False,
        dry_run=False,
    )

    text = report.read_text(encoding="utf-8")
    assert "failed" in text
    assert "exit_code=1" in text


@pytest.mark.offline
def test_run_step_failure_marks_status(monkeypatch, tmp_path: Path) -> None:
    def raise_called_process_error(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["fake"])

    monkeypatch.setattr(build_product_excel_pipeline.subprocess, "run", raise_called_process_error)

    with pytest.raises(subprocess.CalledProcessError):
        build_product_excel_pipeline.run_step("bad_step", ["fake"], cwd=tmp_path, dry_run=False)
