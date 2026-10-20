#!/usr/bin/env python3
"""Explicit schema and bounded receipt worker; automatic publication is opt-in."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag_app.knowledge_processing import ProcessingService, ProcessingStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, help="Explicit local test/development database only.")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-db")
    initialize.add_argument("--execute", action="store_true")
    for name in ("init-role-assignments", "migrate-role-assignments"):
        migration = commands.add_parser(name)
        migration.add_argument("--execute", action="store_true", help="Apply the reviewed incremental migration; default is read-only preview.")
    review = commands.add_parser("review-published-roles", help="Read-only candidate report; never reassign published knowledge.")
    review.add_argument("--limit", type=int, default=100)
    review.add_argument("--offset", type=int, default=0)
    worker = commands.add_parser("work")
    mode = worker.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "init-db" and not args.execute:
        print(json.dumps({"ok": True, "status": "dry_run", "tables": ["knowledge_processing_jobs", "knowledge_processing_items", "knowledge_processing_events", "knowledge_revision_fingerprints", "knowledge_submissions"],
                          "additive_item_columns": ["version", "resolution_json", "publication_json"], "rewrite_existing_revisions": False}))
        return 0
    try:
        if args.command in {"init-role-assignments", "migrate-role-assignments", "review-published-roles"}:
            if ((args.sqlite is not None and not args.sqlite.is_file())
                    or args.sqlite is None and not os.getenv("KNOWLEDGE_DATABASE_URL")):
                print(json.dumps({"ok": False, "error_code": "explicit_existing_database_required"}))
                return 1
            store = ProcessingStore(args.sqlite)
            if args.command == "review-published-roles":
                if not 1 <= args.limit <= 1000 or not 0 <= args.offset <= 1_000_000:
                    print(json.dumps({"ok": False, "error_code": "invalid_review_page"}))
                    return 1
                from rag_app.knowledge_role_maintenance import review_published_role_assignments
                result = review_published_role_assignments(store, limit=args.limit, offset=args.offset)
                print(json.dumps({"ok": True, "status": "read_only", **result}, ensure_ascii=False))
                return 0
            method = (store.initialize_role_assignments if args.command == "init-role-assignments"
                      else store.migrate_legacy_item_roles)
            result = method(apply=args.execute)
            print(json.dumps({"ok": True, "status": "applied" if args.execute else "dry_run", **result}, ensure_ascii=False))
            return 0
        store = ProcessingStore(args.sqlite)
        if args.command == "init-db":
            store.initialize()
            print(json.dumps({"ok": True, "status": "initialized"}))
            return 0
        if os.getenv("KNOWLEDGE_PROCESSING_ENABLED") != "1":
            print(json.dumps({"ok": False, "error_code": "processing_disabled"}))
            return 1
        service = ProcessingService(store)
        while True:
            claimed = service.run_once()
            if args.once:
                print(json.dumps({"ok": True, "task_claimed": claimed, "automatic_publication_enabled": service.auto_publish}))
                return 0
            if not claimed:
                time.sleep(5)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error_code": "processing_command_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
