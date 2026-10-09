#!/usr/bin/env python3
"""Explicit schema initialization and a bounded original verification worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rag_app.knowledge_receipts import ReceiptService, ReceiptStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, help="Explicit development/test database; production uses KNOWLEDGE_DATABASE_URL.")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-db")
    initialize.add_argument("--execute", action="store_true")
    worker = commands.add_parser("verify")
    mode = worker.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "init-db" and not args.execute:
        print(json.dumps({"ok": True, "status": "dry_run", "table": "knowledge_upload_receipts"}))
        return 0
    try:
        store = ReceiptStore(args.sqlite)
        if args.command == "init-db":
            store.initialize()
            print(json.dumps({"ok": True, "status": "initialized"}))
            return 0
        if os.getenv("KNOWLEDGE_RECEIPTS_ENABLED") != "1":
            print(json.dumps({"ok": False, "error_code": "receipts_disabled"}), file=sys.stderr)
            return 1
        service = ReceiptService(store)
        while True:
            processed = service.verify_once()
            if args.once:
                print(json.dumps({"ok": True, "task_claimed": processed}))
                return 0
            if not processed:
                time.sleep(5)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error_code": "receipt_command_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
