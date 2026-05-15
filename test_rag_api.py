#!/usr/bin/env python3
"""Small smoke client for the RAG API."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import error as urllib_error
from urllib import request


BASE_URL = os.getenv("RAG_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
QUERY = os.getenv("RAG_API_TEST_QUERY", "红色礼盒春节送礼推荐哪些酒")


def post_json(path: str, payload: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        BASE_URL + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path: str, timeout: int = 30) -> dict[str, Any]:
    with request.urlopen(BASE_URL + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_payload(title: str, payload: dict[str, Any]) -> None:
    print(f"## {title}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    print()


def main() -> int:
    try:
        health = get_json("/api/rag/health")
        print_payload("health", health)

        search = post_json("/api/rag/search", {"query": QUERY, "top_k": 3})
        print_payload("search", search)

        answer = post_json("/api/rag/answer", {"query": QUERY, "top_k": 5, "debug": True})
        print_payload("answer", answer)

        ok = bool(health.get("ok")) and bool(search.get("ok")) and bool(answer.get("ok"))
        return 0 if ok else 1
    except urllib_error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return 1
    except Exception as exc:
        print(f"API smoke test failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

