#!/usr/bin/env python3
"""Shared project paths, environment loading, and small text helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT
VENDOR_DIR = PROJECT_ROOT / ".vendor"
if VENDOR_DIR.exists() and str(VENDOR_DIR) not in sys.path:
    # Installed dependencies match the running Python; vendored binaries may not.
    sys.path.append(str(VENDOR_DIR))

OUTPUT_DIR = PROJECT_ROOT / "output"
VECTOR_DIMENSIONS = 1024


def load_env(root: Path | str = PROJECT_ROOT) -> None:
    """Load .env values without printing secrets."""
    root_path = Path(root).resolve()
    env_path = root_path / ".env"
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path)
        return
    except Exception:
        pass

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def truncate(text: str, limit: int = 120) -> str:
    text = safe_text(text).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
