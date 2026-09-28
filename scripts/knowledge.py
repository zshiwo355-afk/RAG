#!/usr/bin/env python3
"""Maintain company knowledge from local files; mutations default to dry-run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
MAX_CONTENT_CHARS = 500_000
METADATA_FIELDS = {"title", "contributor", "kind", "sources", "evidence", "department", "scenarios", "chunking_version"}


class InputError(ValueError):
    """A local validation error with a safe, fixed message."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage knowledge drafts and publication. No URL downloads.",
        epilog="Storage uses KNOWLEDGE_DATABASE_URL or local KNOWLEDGE_DATA_DIR. Mutations require --execute.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    intake = commands.add_parser("intake", help="Parse ZIP receipts into local, unapproved full bodies and structural chunk previews.")
    intake.add_argument("--manifest", type=Path, required=True, help="JSON sources: path, stable source_id, optional contributor.")
    intake.add_argument("--out", type=Path, required=True, help="New generated output directory.")
    intake.add_argument("--reviews", type=Path, help="Optional evidence-anchored AI suggestion JSONL; never publication approval.")
    intake.add_argument("--resume", action="store_true", help="Reuse this batch's parse checkpoints with identical inputs.")
    batch_import = commands.add_parser("import-batch", help="Import eligible candidate suggestions as drafts, never publish.")
    batch_import.add_argument("--batch", type=Path, required=True)
    batch_import.add_argument("--execute", action="store_true")
    batch_publish = commands.add_parser("publish-batch", help="Publish only the exact reviewed revisions saved by import-batch.")
    batch_publish.add_argument("--batch", type=Path, required=True)
    batch_publish.add_argument("--confirmed-by", required=True, help="Person confirming this batch may be shared with all API users.")
    batch_publish.add_argument("--execute", action="store_true")
    judge = commands.add_parser("judge-batch", help="Assess every span of cleaned candidates through the configured answer model.")
    judge.add_argument("--batch", type=Path, required=True)
    judge.add_argument("--execute", action="store_true", help="Send cleaned text to the existing DashScope answer endpoint; cache suggestions locally.")
    judge.add_argument("--workers", type=int, choices=range(1, 9), default=1,
                       help="Maximum concurrent judgment calls (1-8; default 1).")
    initialize = commands.add_parser("init-db", help="Create the knowledge catalogue tables; existing records are preserved.")
    initialize.add_argument("--execute", action="store_true", help="Actually initialize the configured catalogue.")
    ingest = commands.add_parser("import", help="Import a UTF-8 .md or .txt document.")
    ingest.add_argument("--file", type=Path, required=True)
    ingest.add_argument("--id", required=True, help="Stable knowledge ID; cannot be set in metadata.")
    ingest.add_argument("--title", help="Overrides metadata.title.")
    ingest.add_argument("--contributor", help="Overrides metadata.contributor.")
    ingest.add_argument("--department", help="Optional department label; does not restrict access.")
    ingest.add_argument("--scenario", action="append", help="Repeatable usage scenario; replaces metadata.scenarios.")
    ingest.add_argument("--source-name", help="Replaces metadata.sources, together with --source-locator.")
    ingest.add_argument("--source-locator", help="Source reference, not an absolute local file path.")
    ingest.add_argument(
        "--metadata", type=Path,
        help="Optional JSON object: title, contributor, kind, sources, evidence, department, scenarios. CLI fields take precedence.",
    )
    ingest.add_argument("--execute", action="store_true", help="Save the draft to the configured catalogue and body storage.")
    index_draft = commands.add_parser("index-draft", help="Prepare vectors for an imported draft without publishing it.")
    index_draft.add_argument("--id", required=True)
    index_draft.add_argument("--revision", type=int)
    index_draft.add_argument("--execute", action="store_true", help="Embed, write and verify draft chunks; publication remains separate.")
    publish = commands.add_parser("publish", help="Publish after confirming content may be read by anyone with API access.")
    publish.add_argument("--id", required=True)
    publish.add_argument("--revision", type=int)
    publish.add_argument("--confirmed-by", required=True, help="Person confirming publication; attribution, not authentication.")
    publish.add_argument("--execute", action="store_true", help="Publish and verify the selected revision.")
    withdraw = commands.add_parser("withdraw", help="Withdraw a knowledge entry.")
    withdraw.add_argument("--id", required=True)
    withdraw.add_argument("--execute", action="store_true", help="Actually withdraw the entry.")
    commands.add_parser("list", help="List local records, including drafts.")
    export = commands.add_parser("export", help="Export the full local record, including content and metadata.")
    export.add_argument("--id", required=True)
    export.add_argument("--revision", type=int)
    export.add_argument("--output", type=Path, help="Optional JSON backup file; must not already exist.")
    return parser


def read_entry(args: argparse.Namespace) -> dict:
    if args.file.suffix.lower() not in {".md", ".txt"}:
        raise InputError("Input must be a local .md or .txt file.")
    try:
        with args.file.open("r", encoding="utf-8", errors="strict") as source:
            content = source.read(MAX_CONTENT_CHARS + 1)
    except (OSError, UnicodeError, ValueError):
        raise InputError("Cannot read the input file as UTF-8.") from None
    if len(content) > MAX_CONTENT_CHARS:
        raise InputError("Document exceeds the 500000-character limit.")
    entry = {}
    if args.metadata is not None:
        try:
            entry = json.loads(args.metadata.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, ValueError):
            raise InputError("Cannot read metadata as UTF-8 JSON.") from None
        if not isinstance(entry, dict) or set(entry) - METADATA_FIELDS:
            raise InputError("Metadata contains unsupported fields.")
    for field in ("title", "contributor"):
        if getattr(args, field) is not None:
            entry[field] = getattr(args, field)
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise InputError("A nonempty title and contributor are required via flags or metadata.")
    entry.update(knowledge_id=args.id, content=content)
    if args.department is not None:
        entry["department"] = args.department
    if args.scenario is not None:
        entry["scenarios"] = args.scenario
    entry.setdefault("kind", "method")
    if args.source_name is not None or args.source_locator is not None:
        source = {"name": args.source_name if args.source_name is not None else args.file.name}
        if args.source_locator is not None:
            source["locator"] = args.source_locator
        entry["sources"] = [source]
    elif "sources" not in entry:
        entry["sources"] = [{"name": args.file.name}]
    return entry


PINS = ("knowledge_id", "revision", "content_hash", "payload_hash")


def _batch_plan(output):
    """Reuse intake's eligibility and full model-window checks, without writes."""
    from rag_app.knowledge_intake import file_hash, import_batch
    from rag_app.knowledge_store import normalize_entry

    def inputs():
        return {name: file_hash(output / name) if (output / name).exists() else None
                for name in ("batch.json", "receipt.json", "model_reviews.json")}

    identity, entries = inputs(), []

    def inspect(entry, execute=False):
        normalized = normalize_entry(entry)
        entries.append(normalized)
        return normalized

    plan = import_batch(output, SimpleNamespace(import_document=inspect), execute=False)
    selected = [row for row in plan["results"] if row["status"] == "dry_run"]
    if (inputs() != identity or len(selected) != len(entries)
            or len({entry["knowledge_id"] for entry in entries}) != len(entries)):
        raise InputError("Batch inputs changed or contain conflicting asset identities.")
    targets = {row["candidate_id"]: entry for row, entry in zip(selected, entries)}
    if len(targets) != len(entries):
        raise InputError("Batch candidate identities must be unique.")
    return identity, targets, plan


def _batch_receipt(path, identity, targets, *, required=False):
    if not path.exists():
        if required:
            raise InputError("Run import-batch --execute first; its import.json receipt is required.")
        return {"version": 1, "inputs": identity, "results": []}
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("version") != 1 or receipt.get("inputs") != identity or not isinstance(receipt.get("results"), list):
        raise InputError("Batch receipt does not match the current inputs.")
    seen = set()
    for row in receipt["results"]:
        candidate = row["candidate_id"]
        expected = targets.get(candidate)
        if (candidate in seen or expected is None
                or any(row.get(key) != expected[key] for key in ("knowledge_id", "content_hash", "payload_hash"))
                or type(row.get("revision")) is not int or row["revision"] < 1
                or type(row.get("generation")) is not int or row["generation"] < 1
                or row.get("status") not in {"draft", "published"}):
            raise InputError("Batch receipt contains an unreviewed or changed asset.")
        seen.add(candidate)
    return receipt


def _pinned_snapshot(store, imported):
    current = store.snapshot(imported["knowledge_id"])
    generation = imported["generation"] + int(imported["status"] == "draft" and current["status"] == "published")
    if (any(current.get(key) != imported[key] for key in PINS)
            or current["status"] not in {"draft", "published"}
            or current["generation"] != generation
            or imported["status"] == "published" and current["status"] != "published"):
        raise InputError("Imported revision changed, was withdrawn, or has a newer version; refusing to resume.")
    return current


def import_batch_with_receipt(output, service, *, execute=False):
    from rag_app.knowledge_intake import write_json

    output = Path(output).resolve()
    identity, targets, plan = _batch_plan(output)
    if not execute:
        return plan
    path = output / "import.json"
    receipt = _batch_receipt(path, identity, targets)
    completed = {row["candidate_id"]: row for row in receipt["results"]}
    # Refuse changed resumptions before importing any new asset.
    for row in completed.values():
        _pinned_snapshot(service.store, row)
    write_json(path, receipt)
    for candidate, entry in targets.items():
        if candidate in completed:
            continue
        record = service.import_document(entry, execute=True, allow_new_revision=False)
        row = {"candidate_id": candidate, **{key: record[key] for key in (*PINS, "generation", "status")}}
        # Validate the exact returned revision before recording it for publication.
        if (any(record[key] != entry[key] for key in ("knowledge_id", "content_hash", "payload_hash"))
                or type(record["revision"]) is not int or record["revision"] < 1
                or type(record["generation"]) is not int or record["generation"] < 1
                or record["status"] not in {"draft", "published"}):
            raise InputError("Imported record did not match the reviewed candidate.")
        receipt["results"].append(row)
        write_json(path, receipt)
    return {**plan, "results": receipt["results"] + [row for row in plan["results"] if row["status"] == "failed"],
            "receipt": "import.json"}


def publish_batch(output, service, *, confirmed_by, execute=False):
    from rag_app.knowledge_intake import write_json

    if not isinstance(confirmed_by, str) or not confirmed_by.strip() or len(confirmed_by) > 200:
        raise InputError("A nonempty confirmed-by of at most 200 characters is required.")
    confirmed_by = confirmed_by.strip()
    output = Path(output).resolve()
    identity, targets, _ = _batch_plan(output)
    imported = _batch_receipt(output / "import.json", identity, targets, required=True)["results"]
    if not imported:
        raise InputError("No successfully imported, reviewed candidates are available to publish.")
    store = service.store
    if execute and (urlsplit(getattr(store, "database_url", "") or "").scheme not in {"postgres", "postgresql"}
                    or getattr(store, "body_storage", None) != "oss"):
        raise InputError("Batch publication requires PostgreSQL and OSS; local SQLite/inline storage is not allowed.")
    path = output / "publication.json"
    receipt = _batch_receipt(path, identity, targets)
    if receipt.get("confirmed_by", confirmed_by) != confirmed_by:
        raise InputError("Batch publication confirmation differs from its saved receipt.")
    receipt["confirmed_by"] = confirmed_by
    completed = {row["candidate_id"]: row for row in receipt["results"]}
    imported_ids = {row["candidate_id"] for row in imported}
    if set(completed) - imported_ids:
        raise InputError("Publication receipt contains a candidate without an import receipt.")
    planned = []
    for item in imported:
        current = _pinned_snapshot(store, item)
        saved = completed.get(item["candidate_id"])
        if saved and (current["status"] != "published"
                      or any(saved.get(key) != current[key] for key in (*PINS, "generation", "status"))):
            raise InputError("Published batch item changed; refusing to resume.")
        planned.append({"candidate_id": item["candidate_id"], **{key: current[key] for key in PINS},
                        "action": "skip_published" if current["status"] == "published" else "publish"})
    if not execute:
        return {"results": planned, "will_call_embedding": False, "will_write_index": False}
    write_json(path, receipt)
    for item in imported:
        try:
            current = _pinned_snapshot(store, item)
            previous = completed.get(item["candidate_id"], {})
            verification = previous.get("verification")
            verification_status = previous.get("verification_status", "recovered_without_verification_receipt")
            if current["status"] != "published":
                def pinned_snapshot(knowledge_id, revision=None):
                    record = store.snapshot(knowledge_id, revision)
                    if (any(record.get(key) != current[key] for key in (*PINS, "generation", "status"))):
                        raise InputError("Imported revision changed before publication.")
                    return record

                # Pin the service's own snapshot, closing the gap after preflight.
                # The store's existing generation transaction guards later races.
                service.store = SimpleNamespace(snapshot=pinned_snapshot, publish=store.publish)
                try:
                    result = service.publish(item["knowledge_id"], revision=item["revision"], confirmed_by=confirmed_by, execute=True)
                finally:
                    service.store = store
                checked = result.get("verification", {})
                verification = {key: checked[key] for key in (
                    "table", "data_source", "chunk_count", "verified_chunk_ids", "retrieval_verified",
                ) if key in checked}
                verification_status = "verified" if verification.get("retrieval_verified") is True else "publication_returned_without_verification"
                current = _pinned_snapshot(store, item)
                if current["status"] != "published":
                    raise InputError("Publication was not confirmed by the catalogue.")
            completed[item["candidate_id"]] = {"candidate_id": item["candidate_id"],
                                               **{key: current[key] for key in (*PINS, "generation", "status")},
                                               "verification_status": verification_status,
                                               **({"verification": verification} if verification is not None else {})}
            receipt["results"] = list(completed.values())
            write_json(path, receipt)
        except Exception:
            raise InputError("Batch publication stopped; completed items are retained in publication.json.") from None
    return {"results": planned, "published": len(completed), "receipt": "publication.json"}


def main(argv=None, service=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "intake":
            sys.path.insert(0, str(ROOT / "src"))
            from rag_app.knowledge_intake import process_batch, read_reviews, read_sources
            batch = process_batch(read_sources(args.manifest), args.out, reviews=read_reviews(args.reviews), resume=args.resume)
            print(json.dumps({"status": "candidate_output", "command": args.command, "result": batch["summary"], "output": str(args.out)}, ensure_ascii=False))
            return 0
        if args.command == "judge-batch":
            sys.path.insert(0, str(ROOT / "src"))
            from rag_app.knowledge_judge import judge_batch
            print(json.dumps({"command": args.command, "result": judge_batch(args.batch, execute=args.execute, workers=args.workers)}, ensure_ascii=False))
            return 0
        # Validate local input before constructing a service or touching its store.
        entry = read_entry(args) if args.command == "import" else None
        if service is None:
            sys.path.insert(0, str(ROOT / "src"))
            from rag_app.knowledge_service import KnowledgeService
            service = KnowledgeService()
        if args.command == "import-batch":
            result = import_batch_with_receipt(args.batch, service, execute=args.execute)
        elif args.command == "publish-batch":
            result = publish_batch(args.batch, service, confirmed_by=args.confirmed_by, execute=args.execute)
        elif args.command == "init-db":
            if args.execute:
                service.store.initialize()
            result = {"initialized": args.execute, "will_write_index": False, "will_call_embedding": False}
        elif args.command == "import":
            result = service.import_document(entry, execute=args.execute)
        elif args.command == "index-draft":
            result = service.index_draft(args.id, revision=args.revision, execute=args.execute)
        elif args.command == "publish":
            result = service.publish(args.id, revision=args.revision, confirmed_by=args.confirmed_by, execute=args.execute)
        elif args.command == "withdraw":
            result = service.withdraw(args.id, execute=args.execute)
        elif args.command == "list":
            result = service.list_assets()
        else:
            result = service.export(args.id, revision=args.revision)
            if args.output is not None:
                payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                try:
                    with args.output.open("x", encoding="utf-8") as output:
                        output.write(payload)
                except FileExistsError:
                    raise InputError("Export output already exists; nothing was overwritten.") from None
        status = "dry_run" if hasattr(args, "execute") and not args.execute else "ok"
        print(json.dumps({"status": status, "command": args.command, "result": result}, ensure_ascii=False))
        return 0
    except Exception as error:
        # Provider exceptions may contain credentials; never echo their text or traceback.
        message = str(error) if isinstance(error, InputError) else "Knowledge operation failed; check inputs and service configuration."
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
