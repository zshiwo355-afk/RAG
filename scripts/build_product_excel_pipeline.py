#!/usr/bin/env python3
"""Run the local product Excel build pipeline in an isolated workspace.

This wrapper does not call embedding services and does not write OpenSearch. It
copies source Excel files into a pipeline workspace, runs existing local build
scripts against that workspace, then exports standard documents to
output/ingest_build/product_excel/.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRODUCT_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def rel_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return Path(path.name).as_posix()


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def discover_sources(root: Path, values: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in values:
        source = resolve_path(root, value)
        if source.is_file():
            if source.suffix.lower() in PRODUCT_EXTENSIONS and not source.name.startswith("~$"):
                files.append(source)
            continue
        if not source.exists():
            raise FileNotFoundError(f"source 不存在：{source}")
        files.extend(
            sorted(
                path
                for path in source.rglob("*")
                if path.is_file()
                and path.suffix.lower() in PRODUCT_EXTENSIONS
                and not path.name.startswith("~$")
            )
        )
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def command_text(command: list[str]) -> str:
    return " ".join(command)


def run_step(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    dry_run: bool,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    row = {
        "name": name,
        "command": command_text(command),
        "status": "planned" if dry_run else "running",
        "started_at": started,
        "finished_at": "",
        "error": "",
    }
    print(("Would run: " if dry_run else "Running: ") + command_text(command))
    if dry_run:
        return row
    try:
        subprocess.run(command, cwd=cwd, check=True)
        row["status"] = "success"
    except subprocess.CalledProcessError as exc:
        row["status"] = "failed"
        row["error"] = f"exit_code={exc.returncode}"
        raise
    finally:
        row["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return row


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                count += 1
    return count


def mirror_sources(root: Path, work_root: Path, sources: list[Path], dry_run: bool) -> list[str]:
    mirrored: list[str] = []
    for source in sources:
        rel_path = rel_to_root(source, root)
        target = work_root / rel_path
        mirrored.append(rel_path)
        print(f"{'Would copy' if dry_run else 'Copy'}: {source} -> {target}")
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return mirrored


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} 未生成：{path}")


def write_report(
    path: Path,
    *,
    sources: list[str],
    steps: list[dict[str, Any]],
    output_dir: Path,
    work_root: Path,
    skip_image_analysis: bool,
    reuse_existing_image_analysis: bool,
    dry_run: bool,
) -> None:
    docs_path = output_dir / "documents.json"
    needs_mapping_path = output_dir / "needs_mapping.jsonl"
    docs_count = len(load_json_list(docs_path))
    needs_mapping_count = count_jsonl(needs_mapping_path)
    lines = [
        "# Product Excel Pipeline Report",
        "",
        f"- Mode: `{'dry-run' if dry_run else 'execute'}`",
        f"- Work root: `{work_root}`",
        f"- Output dir: `{output_dir}`",
        f"- skip_image_analysis: `{skip_image_analysis}`",
        f"- reuse_existing_image_analysis: `{reuse_existing_image_analysis}`",
        f"- Standard documents: {docs_count}",
        f"- needs_mapping records: {needs_mapping_count}",
        "",
        "## Sources",
        "",
    ]
    lines.extend(f"- `{source}`" for source in sources)
    lines.extend(["", "## Steps", ""])
    for step in steps:
        lines.append(f"- `{step['name']}`: {step['status']} - `{step['command']}`")
        if step.get("error"):
            lines.append(f"  - error: {step['error']}")
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- Cleaned products: `{work_root / 'output/products_cleaned.json'}`",
            f"- Image mapping: `{work_root / 'output/image_mapping.json'}`",
            f"- Image analysis: `{work_root / 'output/image_analysis.jsonl'}`",
            f"- Enriched products: `{work_root / 'output/products_enriched.json'}`",
            f"- Product docs preview: `{work_root / 'output/documents_preview_v2.json'}`",
            f"- Standard documents: `{docs_path}`",
            f"- Needs mapping: `{needs_mapping_path}`",
            "",
            "## Notes",
            "",
            "- This wrapper does not call embedding and does not write OpenSearch.",
            "- Image AI analysis is skipped by default because it calls external models.",
            "- If no ready documents are produced, inspect `needs_mapping.jsonl` and source field mapping.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local product Excel pipeline without embedding or OpenSearch.")
    parser.add_argument("--root", default=str(ROOT), help="Project root.")
    parser.add_argument("--source", action="append", required=True, help="Excel file or directory. May be passed multiple times.")
    parser.add_argument("--output-dir", default="output/ingest_build/product_excel", help="Standard product Excel build output directory.")
    parser.add_argument("--work-dir", default="output/product_excel_pipeline", help="Isolated pipeline work directory.")
    parser.add_argument("--skip-image-analysis", action="store_true", default=True, help="Skip external image AI analysis. Default: true.")
    parser.add_argument("--reuse-existing-image-analysis", action="store_true", help="Copy existing output/image_analysis.jsonl into work dir if present.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned steps without executing.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation before executing.")
    return parser.parse_args()


def confirm_or_exit(args: argparse.Namespace, sources: list[Path]) -> None:
    if args.dry_run or args.yes:
        return
    print()
    print("This will write isolated pipeline outputs under:")
    print(f"- {resolve_path(Path(args.root).resolve(), args.work_dir)}")
    print(f"- {resolve_path(Path(args.root).resolve(), args.output_dir)}")
    print(f"Source Excel files: {len(sources)}")
    answer = input("Type yes to run the local Excel pipeline: ").strip()
    if answer != "yes":
        raise SystemExit("Aborted. No product Excel pipeline step was executed.")


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    work_root = resolve_path(root, args.work_dir)
    output_dir = resolve_path(root, args.output_dir)
    sources = discover_sources(root, args.source)
    if not sources:
        raise RuntimeError("没有找到可处理的 Excel 文件")
    confirm_or_exit(args, sources)

    source_rel_paths = mirror_sources(root, work_root, sources, args.dry_run)
    work_output = work_root / "output"
    steps: list[dict[str, Any]] = []

    steps.append(
        run_step(
            "excel_clean",
            [sys.executable, str(root / "scripts/excel_clean.py"), "--root", str(work_root), "--output", "output"],
            cwd=root,
            dry_run=args.dry_run,
        )
    )
    if not args.dry_run:
        require_file(work_output / "products_cleaned.json", "products_cleaned.json")

    steps.append(
        run_step(
            "extract_excel_images",
            [
                sys.executable,
                str(root / "scripts/extract_excel_images.py"),
                "--root",
                str(work_root),
                "--products",
                "output/products_cleaned.json",
                "--output",
                "output",
            ],
            cwd=root,
            dry_run=args.dry_run,
        )
    )
    if not args.dry_run:
        require_file(work_output / "image_mapping.json", "image_mapping.json")

    if args.reuse_existing_image_analysis and (root / "output/image_analysis.jsonl").exists():
        print("Reuse existing image analysis: output/image_analysis.jsonl")
        if not args.dry_run:
            shutil.copy2(root / "output/image_analysis.jsonl", work_output / "image_analysis.jsonl")
    elif args.skip_image_analysis:
        print("Image analysis skipped. Creating empty image_analysis.jsonl in work dir.")
        if not args.dry_run:
            (work_output / "image_analysis.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (work_output / "image_analysis.jsonl").write_text("", encoding="utf-8")
    else:
        raise RuntimeError("Image analysis is external-model work and is not enabled in this stage.")

    steps.append(
        run_step(
            "merge_image_into_products",
            [
                sys.executable,
                str(root / "scripts/merge_image_into_products.py"),
                "--root",
                str(work_root),
                "--products",
                "output/products_cleaned.json",
                "--mapping",
                "output/image_mapping.json",
                "--analysis",
                "output/image_analysis.jsonl",
                "--embedded",
                "output/images_embedded.jsonl",
                "--documents",
                "output/documents_preview.json",
            ],
            cwd=root,
            dry_run=args.dry_run,
        )
    )
    if not args.dry_run:
        require_file(work_output / "products_enriched.json", "products_enriched.json")

    steps.append(
        run_step(
            "build_documents",
            [
                sys.executable,
                str(root / "scripts/build_documents.py"),
                "--root",
                str(work_root),
                "--input",
                "output/products_enriched.json",
                "--output",
                "output",
                "--json-name",
                "documents_preview_v2.json",
                "--csv-name",
                "documents_preview_v2.csv",
                "--report-name",
                "document_build_report_v2.md",
            ],
            cwd=root,
            dry_run=args.dry_run,
        )
    )
    if not args.dry_run:
        require_file(work_output / "documents_preview_v2.json", "documents_preview_v2.json")

    build_doc_command = [
        sys.executable,
        str(root / "scripts/build_product_excel_docs.py"),
        "--root",
        str(work_root),
        "--products",
        "output/products_enriched.json",
        "--output-dir",
        str(output_dir),
    ]
    for source_rel_path in source_rel_paths:
        build_doc_command.extend(["--source", source_rel_path])
    steps.append(run_step("build_product_excel_docs", build_doc_command, cwd=root, dry_run=args.dry_run))
    if not args.dry_run:
        require_file(output_dir / "documents.json", "standard documents.json")

    report_path = output_dir / "build_report.md"
    if not args.dry_run:
        write_report(
            report_path,
            sources=source_rel_paths,
            steps=steps,
            output_dir=output_dir,
            work_root=work_root,
            skip_image_analysis=args.skip_image_analysis,
            reuse_existing_image_analysis=args.reuse_existing_image_analysis,
            dry_run=args.dry_run,
        )
    else:
        print(f"Would write report: {report_path}")

    print(f"Pipeline report: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ERROR] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)

