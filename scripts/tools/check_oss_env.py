#!/usr/bin/env python3
"""Check required OSS environment variables without printing secret values."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_app.config import load_env


REQUIRED = (
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_PUBLIC_BASE_URL",
)
OPTIONAL_DEFAULTS = {
    "OSS_UPLOAD_PREFIX": "rag/images/",
}


def main() -> int:
    load_env(ROOT)
    missing = [name for name in REQUIRED if not os.getenv(name, "").strip()]
    print("OSS environment check")
    if missing:
        print("Missing required variables:")
        for name in missing:
            print(f"- {name}")
    else:
        print("All required OSS variables are set.")

    print("Optional variables:")
    for name, default in OPTIONAL_DEFAULTS.items():
        value = os.getenv(name, "").strip() or default
        print(f"- {name}: {'set' if os.getenv(name, '').strip() else f'default={value}'}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

