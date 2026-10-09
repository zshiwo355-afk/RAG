from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"

for path in [REPO_ROOT, REPO_ROOT / "scripts", REPO_ROOT / "src"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def isolate_unit_tests_from_cloud(request, monkeypatch):
    if request.node.get_closest_marker("integration") is not None:
        return
    # CLI dry-runs can load .env, changing a later SQLite test into an OSS write.
    # The environment flag also reaches subprocess checks using python-dotenv.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in tuple(os.environ):
        if name.startswith(("KNOWLEDGE_", "OSS_", "OPENSEARCH_", "DASHSCOPE_")):
            monkeypatch.delenv(name, raising=False)

    def guarded(original):
        def no_network(connection, *args, **kwargs):
            if connection.family in {socket.AF_INET, socket.AF_INET6}:
                raise RuntimeError("unit tests must inject network clients; live checks require integration opt-in")
            return original(connection, *args, **kwargs)
        return no_network

    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, guarded(getattr(socket.socket, method)))


@pytest.fixture(autouse=True)
def offline_rerank_no_network(request, monkeypatch):
    if request.node.get_closest_marker("offline") is not None:
        from rag_app import rerank_service

        def unavailable(*args, **kwargs):
            raise RuntimeError("offline rerank fixture; tests may provide deterministic scores")

        monkeypatch.setattr(rerank_service, "call_rerank_model", unavailable)


@pytest.fixture
def temp_project(tmp_path: Path, fixture_dir: Path) -> Path:
    root = tmp_path / "project"
    (root / "素材").mkdir(parents=True)
    (root / "产品信息").mkdir()
    (root / "质检报告" / "测试品牌").mkdir(parents=True)
    (root / "output").mkdir()
    (root / "config").mkdir()
    shutil.copy2(fixture_dir / "sample_product.xlsx", root / "素材" / "sample_product.xlsx")
    shutil.copy2(fixture_dir / "sample_product.xlsx", root / "产品信息" / "sample_product.xlsx")
    shutil.copy2(fixture_dir / "sample_quality_report.pdf", root / "质检报告" / "测试品牌" / "sample_quality_report.pdf")
    return root


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
