#!/usr/bin/env python3
"""Plan cleanup for generated outputs without deleting by default."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import shutil
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = "output/cleanup/cleanup_plan.json"
DEFAULT_REPORT = "output/cleanup/cleanup_plan.md"

PROTECTED_PREFIXES = [
    "output/ingest_build/",
    "output/ingest_embeddings/",
    "src/",
    "tests/",
]
PROTECTED_EXACT = {
    "output/ingest_manifest.jsonl",
    "output/review/product_catalog_registry_new_24.csv",
    "config/product_catalog_registry.csv",
    "config/product_catalog_registry.csv.bak_before_new_24",
    "requirements.txt",
    "Makefile",
    "docs/RAG_INGEST_FINAL_SUMMARY.md",
    "docs/INGESTION.md",
    "docs/INGEST_UI.md",
    "docs/UPDATE_STRATEGY.md",
}
KEEP_EXACT = {
    "output/acceptance/final_supplement_push_acceptance.md",
    "output/acceptance/final_supplement_push_acceptance.json",
    "output/acceptance/product_bundle_api_acceptance.md",
    "output/acceptance/product_bundle_api_acceptance.json",
    "output/review/product_mapping_review_49.xlsx",
    "output/review/product_mapping_review_49.csv",
    "output/review/product_mapping_review_49.md",
    "output/review/product_mapping_review_49.json",
    "output/review/product_mapping_review_49_summary.md",
}
KEEP_ACCEPTANCE_NAMES = {
    "dmz_product_mapping_candidates_full.json",
    "dmz_product_mapping_candidates_full.md",
    "srx_product_mapping_candidates_full.json",
    "srx_product_mapping_candidates_full.md",
}
CATEGORY_ZH = {
    "acceptance_report": "验收报告",
    "temp_pipeline": "临时流水线文件",
    "cache": "缓存文件",
    "old_report": "旧报告",
    "protected": "受保护文件",
    "keep": "建议保留",
}
ACTION_ZH = {
    "keep": "保留",
    "archive": "归档",
    "delete": "删除",
    "protected": "绝对保护",
}
REASON_ZH = {
    "core protected file": "核心文件，绝对保护。",
    "program file protected; script_review only": "程序文件，本轮只做脚本清单建议，不删除。",
    "latest acceptance/review artifact requested to keep": "最新验收或人工确认产物，按要求保留。",
    "mapping candidates useful until 49 needs_review rows are confirmed": "49 条 needs_review 确认完成前仍有参考价值，建议先归档。",
    "historical acceptance report": "历史验收报告，可归档减少 output 噪音。",
    "push/verify report; keep trace by archive before deleting": "push/verify 历史报告，建议先归档保留追溯。",
    "old ingest change plan/report": "旧 ingest 变更计划或报告，建议归档。",
    "current cleanup reports are kept; older cleanup scratch can be deleted": "当前清理计划保留，旧清理草稿可删除。",
    "temporary pipeline/test output; core ingest_build/ingest_embeddings are separately protected": "临时流水线或测试产物；核心 ingest_build/ingest_embeddings 已单独保护。",
    "runtime/test cache": "运行或测试缓存，可删除。",
    "temporary test/report output": "临时测试或报告产物，建议归档。",
    "not matched by cleanup rules": "未命中清理规则，默认保留。",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def rel_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def is_config_identity(path: str) -> bool:
    return path.startswith("config/product_identity_map") and path.endswith(".csv")


def is_source_document(path: Path) -> bool:
    return path.suffix.lower() in {".xlsx", ".xls", ".pdf", ".txt"} and not rel_path(ROOT, path).startswith("output/")


def protected_reason(path: str) -> str | None:
    if path in PROTECTED_EXACT or is_config_identity(path):
        return "core protected file"
    if path.startswith("scripts/") and path.endswith(".py"):
        return "program file protected; script_review only"
    for prefix in PROTECTED_PREFIXES:
        if path.startswith(prefix):
            return f"protected prefix {prefix}"
    return None


def reason_zh(reason: str) -> str:
    if reason.startswith("protected prefix "):
        return f"受保护目录：{reason.removeprefix('protected prefix ')}"
    return REASON_ZH.get(reason, reason)


def localize_record(record: dict[str, Any]) -> dict[str, Any]:
    item = dict(record)
    item["category_zh"] = CATEGORY_ZH.get(item.get("category"), item.get("category", ""))
    action = item.get("recommended_action", "")
    item["recommended_action_zh"] = "绝对保护" if item.get("category") == "protected" else ACTION_ZH.get(action, action)
    item["reason_zh"] = reason_zh(str(item.get("reason", "")))
    return item


def classify_path(root: Path, path: Path) -> dict[str, Any]:
    relative = rel_path(root, path)
    size = path.stat().st_size if path.is_file() else 0
    protection = protected_reason(relative)
    if protection:
        return {
            "path": relative,
            "category": "protected",
            "recommended_action": "keep",
            "reason": protection,
            "size_bytes": size,
        }
    if relative in KEEP_EXACT:
        return {
            "path": relative,
            "category": "keep",
            "recommended_action": "keep",
            "reason": "latest acceptance/review artifact requested to keep",
            "size_bytes": size,
        }
    name = path.name
    if relative.startswith("output/acceptance/") and name in KEEP_ACCEPTANCE_NAMES:
        return {
            "path": relative,
            "category": "acceptance_report",
            "recommended_action": "archive",
            "reason": "mapping candidates useful until 49 needs_review rows are confirmed",
            "size_bytes": size,
        }
    if relative.startswith("output/acceptance/") and path.suffix.lower() in {".md", ".json", ".jsonl"}:
        return {
            "path": relative,
            "category": "acceptance_report",
            "recommended_action": "archive",
            "reason": "historical acceptance report",
            "size_bytes": size,
        }
    if relative.startswith("output/ingest_push/") and (
        name.endswith("_report.md") or name.endswith("_report.json") or name in {"verify_report.md", "verify_report.json"}
    ):
        return {
            "path": relative,
            "category": "old_report",
            "recommended_action": "archive",
            "reason": "push/verify report; keep trace by archive before deleting",
            "size_bytes": size,
        }
    if relative.startswith("output/ingest_changes/") and (
        name.startswith("change_plan.") or name.startswith("delete_report.")
    ):
        return {
            "path": relative,
            "category": "old_report",
            "recommended_action": "archive",
            "reason": "old ingest change plan/report",
            "size_bytes": size,
        }
    if relative.startswith("output/cleanup/"):
        return {
            "path": relative,
            "category": "old_report",
            "recommended_action": "keep" if name in {"cleanup_plan.json", "cleanup_plan.md", "script_review.json", "script_review.md"} else "delete",
            "reason": "current cleanup reports are kept; older cleanup scratch can be deleted",
            "size_bytes": size,
        }
    if relative.startswith("output/product_excel_pipeline") or relative.startswith("output/ingest_build_test") or relative.startswith("output/ingest_embeddings_test"):
        return {
            "path": relative,
            "category": "temp_pipeline",
            "recommended_action": "archive",
            "reason": "temporary pipeline/test output; core ingest_build/ingest_embeddings are separately protected",
            "size_bytes": size,
        }
    if "__pycache__/" in relative or relative.endswith("/__pycache__") or relative.startswith(".pytest_cache/") or "/.pytest_cache/" in relative:
        return {
            "path": relative,
            "category": "cache",
            "recommended_action": "delete",
            "reason": "runtime/test cache",
            "size_bytes": size,
        }
    if relative.startswith("output/") and (
        relative.endswith("_test.jsonl")
        or relative.endswith("_report.md")
        or relative.endswith("_report.json")
        or "test" in relative
    ):
        return {
            "path": relative,
            "category": "old_report",
            "recommended_action": "archive",
            "reason": "temporary test/report output",
            "size_bytes": size,
        }
    return {
        "path": relative,
        "category": "keep",
        "recommended_action": "keep",
        "reason": "not matched by cleanup rules",
        "size_bytes": size,
    }


def scan_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for base in [root / "output", root / ".pytest_cache"]:
        if base.exists():
            candidates.extend(path for path in base.rglob("*") if path.is_file())
    for pycache in root.rglob("__pycache__"):
        if ".git" not in pycache.parts:
            candidates.extend(path for path in pycache.rglob("*") if path.is_file())
    for path in [root / "requirements.txt", root / "Makefile"]:
        if path.exists():
            candidates.append(path)
    for path in (root / "config").glob("product_identity_map*.csv"):
        candidates.append(path)
    for path in (root / "scripts").glob("*.py"):
        candidates.append(path)
    return sorted(set(candidates))


def build_plan(root: Path) -> list[dict[str, Any]]:
    return [classify_path(root, path) for path in scan_files(root)]


def write_plan_json(path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "created_at": now_iso(),
        "mode": "dry-run",
        "records": records,
        "summary": {
            "total": len(records),
            "delete": sum(1 for item in records if item["recommended_action"] == "delete"),
            "archive": sum(1 for item in records if item["recommended_action"] == "archive"),
            "keep": sum(1 for item in records if item["recommended_action"] == "keep"),
            "protected": sum(1 for item in records if item["category"] == "protected"),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def section(lines: list[str], title: str, records: list[dict[str, Any]]) -> None:
    lines.extend([f"## {title}", ""])
    if not records:
        lines.append("- None")
    for item in records[:200]:
        lines.append(f"- `{item['path']}` ({item['category']}, {item['size_bytes']} bytes): {item['reason']}")
    if len(records) > 200:
        lines.append(f"- ... {len(records) - 200} more")
    lines.append("")


def write_plan_md(path: Path, payload: dict[str, Any]) -> None:
    records = payload["records"]
    lines = [
        "# Cleanup Plan",
        "",
        f"- Created at: `{payload['created_at']}`",
        "- Mode: dry-run; no files were deleted or moved.",
        f"- Suggested delete: {payload['summary']['delete']}",
        f"- Suggested archive: {payload['summary']['archive']}",
        f"- Keep/protected: {payload['summary']['keep']}",
        "",
    ]
    section(lines, "建议保留", [item for item in records if item["recommended_action"] == "keep" and item["category"] != "protected"])
    section(lines, "建议归档", [item for item in records if item["recommended_action"] == "archive"])
    section(lines, "建议删除", [item for item in records if item["recommended_action"] == "delete"])
    section(lines, "绝对保护", [item for item in records if item["category"] == "protected"])
    section(lines, "需要人工确认", [item for item in records if item["recommended_action"] in {"archive", "delete"} and item["category"] != "cache"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_plan_json_zh(path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    localized = [localize_record(item) for item in records]
    payload = {
        "created_at": now_iso(),
        "mode": "dry-run",
        "records": localized,
        "summary": {
            "total": len(localized),
            "delete": sum(1 for item in localized if item["recommended_action"] == "delete"),
            "archive": sum(1 for item in localized if item["recommended_action"] == "archive"),
            "keep": sum(1 for item in localized if item["recommended_action"] == "keep"),
            "protected": sum(1 for item in localized if item["category"] == "protected"),
            "delete_zh": "删除",
            "archive_zh": "归档",
            "keep_zh": "保留",
            "protected_zh": "绝对保护",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def section_zh(lines: list[str], title: str, records: list[dict[str, Any]]) -> None:
    lines.extend([f"## {title}", ""])
    if not records:
        lines.append("- 无")
    for item in records[:200]:
        lines.append(
            f"- 文件路径：`{item['path']}`\n"
            f"  - 类型：{item['category_zh']}\n"
            f"  - 建议操作：{item['recommended_action_zh']}\n"
            f"  - 原因：{item['reason_zh']}\n"
            f"  - 文件大小：{format_size(int(item['size_bytes']))}"
        )
    if len(records) > 200:
        lines.append(f"- 另有 {len(records) - 200} 项未展开，详见 JSON。")
    lines.append("")


def write_plan_md_zh(path: Path, payload: dict[str, Any]) -> None:
    records = payload["records"]
    lines = [
        "# 清理计划（中文）",
        "",
        f"- 生成时间：`{payload['created_at']}`",
        "- 模式：dry-run，本次没有删除或移动任何文件。",
        f"- 建议删除：{payload['summary']['delete']}",
        f"- 建议归档：{payload['summary']['archive']}",
        f"- 保留/保护：{payload['summary']['keep']}",
        "",
    ]
    section_zh(lines, "绝对保护，不会删除", [item for item in records if item["category"] == "protected"])
    section_zh(lines, "建议保留", [item for item in records if item["recommended_action"] == "keep" and item["category"] != "protected"])
    section_zh(lines, "建议归档", [item for item in records if item["recommended_action"] == "archive"])
    section_zh(lines, "建议删除", [item for item in records if item["recommended_action"] == "delete"])
    section_zh(lines, "需要人工确认", [item for item in records if item["recommended_action"] in {"archive", "delete"} and item["category"] != "cache"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def classify_script(root: Path, path: Path) -> dict[str, Any]:
    name = path.name
    core = {
        "ingest_content.py",
        "embed_ingest_build.py",
        "push_ingest_embeddings.py",
        "build_supplement_documents_from_mapping.py",
        "plan_product_identity_mapping.py",
        "build_product_excel_docs.py",
        "build_quality_report_docs.py",
        "build_image_text_docs.py",
        "plan_cleanup_outputs.py",
        "build_product_mapping_review.py",
    }
    readonly = {"verify_retrieval.py", "check_existing_product_overlap.py", "preview_product_bundle.py", "plan_ingest_changes.py", "plan_ingest.py"}
    oneoff = {"interactive_answer_test.py"}
    duplicate = {"build_documents.py", "build_product_excel_pipeline.py"}
    if name in core:
        bucket = "核心保留"
        reason = "current ingest/retrieval/review workflow depends on it"
    elif name in readonly:
        bucket = "只读诊断，可保留"
        reason = "useful for diagnostics or dry-run previews"
    elif name in oneoff:
        bucket = "一次性验收，可考虑归档"
        reason = "manual smoke/acceptance helper"
    elif name in duplicate:
        bucket = "重复功能，可后续合并"
        reason = "overlaps newer staged ingest scripts"
    else:
        bucket = "不建议删除"
        reason = "program file; do not delete without separate review"
    return {
        "path": rel_path(root, path),
        "category": bucket,
        "recommendation": "keep" if bucket != "一次性验收，可考虑归档" else "consider_archive",
        "reason": reason,
        "size_bytes": path.stat().st_size,
    }


def write_script_review(root: Path, output_dir: Path) -> dict[str, Any]:
    records = [classify_script(root, path) for path in sorted((root / "scripts").glob("*.py"))]
    payload = {
        "created_at": now_iso(),
        "no_scripts_deleted": True,
        "records": records,
        "summary": {category: sum(1 for item in records if item["category"] == category) for category in sorted({item["category"] for item in records})},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "script_review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Script Review", "", "- No scripts were deleted.", ""]
    for category in ["核心保留", "只读诊断，可保留", "一次性验收，可考虑归档", "重复功能，可后续合并", "不建议删除"]:
        section_records = [item for item in records if item["category"] == category]
        lines.extend([f"## {category}", ""])
        if not section_records:
            lines.append("- None")
        for item in section_records:
            lines.append(f"- `{item['path']}`: {item['reason']}")
        lines.append("")
    (output_dir / "script_review.md").write_text("\n".join(lines), encoding="utf-8")
    return payload


def script_recommendation_zh(record: dict[str, Any]) -> str:
    if record.get("recommendation") == "consider_archive":
        return "可考虑归档"
    return "保留"


def script_reason_zh(record: dict[str, Any]) -> str:
    category = record.get("category")
    if category == "核心保留":
        return "当前构建、映射、确认或推送流程需要，建议保留。"
    if category == "只读诊断，可保留":
        return "只读诊断或 dry-run 预览脚本，不会直接写库，建议保留。"
    if category == "一次性验收，可考虑归档":
        return "主要用于一次性人工验收，可在确认不再使用后归档。"
    if category == "重复功能，可后续合并":
        return "功能与新分阶段脚本部分重叠，后续可梳理合并。"
    return "程序文件，不经过单独审查不建议删除。"


def script_role_zh(record: dict[str, Any]) -> str:
    name = Path(str(record.get("path", ""))).name
    roles = {
        "build_image_text_docs.py": "构建图片语义文本 documents。",
        "build_product_excel_docs.py": "从产品 Excel 构建 text_docs 文档。",
        "build_product_mapping_review.py": "生成 needs_review 人工确认表。",
        "build_quality_report_docs.py": "从质检报告构建 quality_report 文档。",
        "build_supplement_documents_from_mapping.py": "根据确认 mapping 生成 supplement 文档。",
        "embed_ingest_build.py": "对 ingest_build 文档生成本地 embeddings。",
        "ingest_content.py": "统一 ingest 入口和预检。",
        "plan_cleanup_outputs.py": "生成输出目录清理计划。",
        "plan_product_identity_mapping.py": "生成产品 identity mapping 候选。",
        "push_ingest_embeddings.py": "安全 push ingest embeddings 的脚本，默认 dry-run。",
        "check_existing_product_overlap.py": "只读检查新增产品与已有产品重叠。",
        "plan_ingest.py": "生成 ingest 计划。",
        "plan_ingest_changes.py": "生成 ingest 变更计划。",
        "preview_product_bundle.py": "只读预览 product bundle。",
        "verify_retrieval.py": "只读验证检索效果。",
        "interactive_answer_test.py": "交互式 answer 验收辅助脚本。",
        "build_documents.py": "旧版文档构建脚本。",
        "build_product_excel_pipeline.py": "旧版产品 Excel 流水线脚本。",
    }
    return roles.get(name, "项目脚本，需单独审查后再决定是否清理。")


def write_script_review_zh(root: Path, output_dir: Path) -> dict[str, Any]:
    records = [classify_script(root, path) for path in sorted((root / "scripts").glob("*.py"))]
    localized = []
    for record in records:
        item = dict(record)
        item["作用"] = script_role_zh(record)
        item["建议"] = script_recommendation_zh(record)
        item["为什么"] = script_reason_zh(record)
        localized.append(item)
    payload = {
        "created_at": now_iso(),
        "no_scripts_deleted": True,
        "records": localized,
        "summary": {category: sum(1 for item in localized if item["category"] == category) for category in sorted({item["category"] for item in localized})},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "script_review_中文.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 脚本清单（中文）", "", "- 本次没有删除任何脚本。", ""]
    for category in ["核心保留", "只读诊断，可保留", "一次性验收，可考虑归档", "重复功能，可后续合并", "不建议删除"]:
        section_records = [item for item in localized if item["category"] == category]
        lines.extend([f"## {category}", ""])
        if not section_records:
            lines.append("- 无")
        for item in section_records:
            lines.append(
                f"- 脚本路径：`{item['path']}`\n"
                f"  - 作用：{item['作用']}\n"
                f"  - 建议：{item['建议']}\n"
                f"  - 为什么：{item['为什么']}"
            )
        lines.append("")
    (output_dir / "script_review_中文.md").write_text("\n".join(lines), encoding="utf-8")
    return payload


def execute_plan(root: Path, records: list[dict[str, Any]], archive: bool) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_root = root / "archive" / f"cleanup_{timestamp}"
    archived: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in records:
        action = item["recommended_action"]
        if action not in {"delete", "archive"}:
            continue
        path = root / item["path"]
        if not path.exists() or not path.is_file():
            skipped.append({"path": item["path"], "reason": "missing"})
            continue
        if action == "archive":
            target = archive_root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            archived.append({"path": item["path"], "archived_to": rel_path(root, target), "size_bytes": item["size_bytes"]})
        elif archive:
            skipped.append({"path": item["path"], "reason": "delete candidate skipped during archive-only run"})
        else:
            path.unlink()
            deleted.append({"path": item["path"], "size_bytes": item["size_bytes"]})
    return {
        "created_at": now_iso(),
        "archive_root": rel_path(root, archive_root),
        "archive_only": archive,
        "archived_count": len(archived),
        "deleted_count": len(deleted),
        "skipped_count": len(skipped),
        "archived": archived,
        "deleted": deleted,
        "skipped": skipped,
    }


def write_archive_report(output_dir: Path, payload: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "final_cleanup_archive_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 最终清理归档报告",
        "",
        f"- 生成时间：`{payload['created_at']}`",
        f"- 归档目录：`{payload['archive_root']}`",
        f"- 归档文件数：{payload['archived_count']}",
        f"- 删除文件数：{payload['deleted_count']}",
        f"- 跳过文件数：{payload['skipped_count']}",
        f"- 模式：{'只归档，不删除 delete 候选' if payload.get('archive_only') else '按计划执行'}",
        "",
        "## 已归档文件",
        "",
    ]
    if not payload["archived"]:
        lines.append("- 无")
    for item in payload["archived"][:300]:
        lines.append(f"- `{item['path']}` -> `{item['archived_to']}`")
    if len(payload["archived"]) > 300:
        lines.append(f"- 另有 {len(payload['archived']) - 300} 项未展开，详见 JSON。")
    lines.extend(["", "## 跳过文件", ""])
    if not payload["skipped"]:
        lines.append("- 无")
    for item in payload["skipped"][:100]:
        lines.append(f"- `{item['path']}`：{item['reason']}")
    (output_dir / "final_cleanup_archive_report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate dry-run cleanup plan for generated outputs.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--keep-core", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report-output", default=DEFAULT_REPORT)
    parser.add_argument("--archive", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    records = build_plan(root)
    output = (root / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output).resolve()
    report = (root / args.report_output).resolve() if not Path(args.report_output).is_absolute() else Path(args.report_output).resolve()
    payload = write_plan_json(output, records)
    write_plan_md(report, payload)
    write_script_review(root, output.parent)
    zh_payload = write_plan_json_zh(output.parent / "cleanup_plan_中文.json", records)
    write_plan_md_zh(output.parent / "cleanup_plan_中文.md", zh_payload)
    write_script_review_zh(root, output.parent)
    archive_payload = None
    if args.execute or args.archive:
        archive_payload = execute_plan(root, records, archive=args.archive)
        write_archive_report(output.parent, archive_payload)
    print(
        json.dumps(
            {
                "mode": "execute" if args.execute or args.archive else "dry-run",
                "summary": payload["summary"],
                "output": str(output),
                "report": str(report),
                "archive_report": archive_payload,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
