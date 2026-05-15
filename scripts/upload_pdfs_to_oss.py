#!/usr/bin/env python3
"""
Upload selected local PDF files to Alibaba Cloud OSS and generate URL mappings.

Outputs:
- output/pdf_url_mapping.json
- output/pdf_url_mapping.csv
- output/pdf_upload_report.md
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from upload_images_to_oss import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_SLEEP,
    ROOT,
    OssClient,
    build_image_url,
    load_oss_config,
    normalize_prefix,
    safe_text,
    to_relative_path,
    truncate,
    upload_with_retries,
)


DEFAULT_UPLOAD_PREFIX = "rag/pdfs/quality_reports/"
MAPPING_JSON_PATH = "output/pdf_url_mapping.json"
MAPPING_CSV_PATH = "output/pdf_url_mapping.csv"
REPORT_PATH = "output/pdf_upload_report.md"


def scan_pdfs(paths: list[str]) -> list[Path]:
    if not paths:
        raise ValueError("至少需要提供一个 --file")
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"PDF 文件不存在：{path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"文件不是 PDF：{path}")
        key = str(path)
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    return sorted(resolved)


def build_oss_key(path: Path, upload_prefix: str) -> str:
    relative = to_relative_path(path)
    return normalize_prefix(upload_prefix) + relative


def build_mapping(pdfs: list[Path], config: dict[str, str]) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for path in pdfs:
        relative_path = to_relative_path(path)
        oss_key = build_oss_key(path, config["upload_prefix"])
        mapping[relative_path] = {
            "source_file": relative_path,
            "oss_key": oss_key,
            "source_url": build_image_url(config["public_base_url"], oss_key),
        }
    return mapping


def write_mapping_json(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flat = {key: value["source_url"] for key, value in mapping.items()}
    path.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")


def write_mapping_csv(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["source_file", "oss_key", "source_url"])
        writer.writeheader()
        for item in mapping.values():
            writer.writerow(item)


def build_report(summary: dict[str, Any], samples: list[dict[str, str]], failures: list[dict[str, str]]) -> str:
    lines = [
        "# PDF OSS 上传报告",
        "",
        "## 总览",
        "",
        f"- dry_run：{summary['dry_run']}",
        f"- 扫描 PDF 总数：{summary['total_pdfs']}",
        f"- 计划上传数：{summary['planned_uploads']}",
        f"- 成功上传数：{summary['uploaded_count']}",
        f"- 已存在跳过数：{summary['skipped_existing_count']}",
        f"- 失败数：{summary['failed_count']}",
        f"- OSS bucket：{summary['bucket']}",
        f"- OSS prefix：{summary['upload_prefix']}",
        f"- 映射 JSON：{summary['mapping_json']}",
        f"- 映射 CSV：{summary['mapping_csv']}",
        "",
        "## 样例映射",
        "",
        "| source_file | oss_key | source_url |",
        "| --- | --- | --- |",
    ]
    for item in samples:
        lines.append(
            "| "
            + " | ".join(
                [
                    truncate(item["source_file"], 90),
                    truncate(item["oss_key"], 90),
                    truncate(item["source_url"], 120),
                ]
            )
            + " |"
        )
    if not samples:
        lines.append("| 无 |  |  |")

    lines.extend(["", "## 失败明细", "", "| source_file | oss_key | error |", "| --- | --- | --- |"])
    for item in failures:
        lines.append(
            "| "
            + " | ".join(
                [
                    truncate(item.get("source_file", ""), 90),
                    truncate(item.get("oss_key", ""), 90),
                    truncate(item.get("error", ""), 160),
                ]
            )
            + " |"
        )
    if not failures:
        lines.append("| 无 |  |  |")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_oss_config(require_credentials=not args.dry_run)
    config["upload_prefix"] = normalize_prefix(args.upload_prefix)
    pdfs = scan_pdfs(args.file)
    mapping = build_mapping(pdfs, config)

    uploaded_count = 0
    skipped_existing_count = 0
    failures: list[dict[str, str]] = []
    planned_uploads = len(pdfs)
    available_files: set[str] = set(mapping.keys()) if args.dry_run else set()

    if not args.dry_run:
        client = OssClient(config)
        planned_uploads = 0
        for path in pdfs:
            row = mapping[to_relative_path(path)]
            key = row["oss_key"]
            try:
                if client.object_exists(key):
                    skipped_existing_count += 1
                    available_files.add(row["source_file"])
                    continue
                planned_uploads += 1
                upload_with_retries(client, key, path, args.retries, args.retry_sleep)
                uploaded_count += 1
                available_files.add(row["source_file"])
            except Exception as exc:
                failures.append(
                    {
                        "source_file": row["source_file"],
                        "oss_key": key,
                        "error": safe_text(exc).strip(),
                    }
                )

    output_mapping = {source_file: mapping[source_file] for source_file in sorted(available_files)}
    write_mapping_json(ROOT / args.mapping_json, output_mapping)
    write_mapping_csv(ROOT / args.mapping_csv, output_mapping)

    summary = {
        "dry_run": args.dry_run,
        "total_pdfs": len(pdfs),
        "planned_uploads": planned_uploads,
        "uploaded_count": uploaded_count,
        "skipped_existing_count": skipped_existing_count,
        "failed_count": len(failures),
        "bucket": config["bucket"],
        "upload_prefix": config["upload_prefix"],
        "mapping_json": str((ROOT / args.mapping_json).resolve()),
        "mapping_csv": str((ROOT / args.mapping_csv).resolve()),
    }
    report = build_report(summary, list(output_mapping.values())[:10], failures)
    report_path = ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return {**summary, "report": str(report_path.resolve())}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload selected PDF files to OSS and generate URL mappings.")
    parser.add_argument("--file", action="append", required=True, help="PDF file path, can be passed multiple times.")
    parser.add_argument("--upload-prefix", default=DEFAULT_UPLOAD_PREFIX, help="OSS upload prefix for PDF files.")
    parser.add_argument("--dry-run", action="store_true", help="Only build mapping files without uploading.")
    parser.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help="Upload retry count.")
    parser.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP, help="Base retry sleep seconds.")
    parser.add_argument("--mapping-json", default=MAPPING_JSON_PATH, help="Output mapping JSON path.")
    parser.add_argument("--mapping-csv", default=MAPPING_CSV_PATH, help="Output mapping CSV path.")
    parser.add_argument("--report", default=REPORT_PATH, help="Output upload report path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.retries <= 0:
        raise ValueError("--retries 必须大于 0")
    try:
        summary = run(args)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(
        "Upload complete: "
        f"dry_run={summary['dry_run']}, "
        f"total={summary['total_pdfs']}, "
        f"planned={summary['planned_uploads']}, "
        f"uploaded={summary['uploaded_count']}, "
        f"skipped={summary['skipped_existing_count']}, "
        f"failed={summary['failed_count']}, "
        f"report={summary['report']}"
    )
    return 0 if summary["failed_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
