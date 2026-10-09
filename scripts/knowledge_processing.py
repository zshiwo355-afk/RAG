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
