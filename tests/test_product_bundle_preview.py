from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import preview_product_bundle as bundle


class FakeTextQuery:
    def __init__(self, query_string, query_params=None):
        self.query_string = query_string
        self.query_params = query_params or {}


class FakeSearchRequest:
    def __init__(self, table_name, size, output_fields, text):
        self.table_name = table_name
        self.size = size
        self.output_fields = output_fields
        self.text = text


class FakeModels:
    TextQuery = FakeTextQuery
    SearchRequest = FakeSearchRequest


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.search_calls = []

    def search(self, request):
        self.search_calls.append(request)
        return FakeResponse({"result": [{"fields": row} for row in self.rows]})

    def get_table(self, _table_name):
        return FakeResponse({"result": {"fieldSchema": {"source_text_vector": "MULTI_FLOAT"}}})


@pytest.mark.offline
def test_build_bundle_includes_quality_report() -> None:
    rows = [
        {"doc_id": "full", "product_id": "prd-a", "doc_type": "product_full", "source_text": "full"},
        {"doc_id": "pack", "product_id": "prd-a", "doc_type": "product_field", "field_name": "packaging_desc", "source_text": "pack"},
        {"doc_id": "qr", "product_id": "prd-a", "doc_type": "quality_report", "source_text": "report"},
    ]

    result = bundle.build_bundle("prd-a", rows)

    assert len(result["basic_info"]) == 1
    assert len(result["packaging"]) == 1
    assert len(result["quality_reports"]) == 1
    assert "qr" in result["doc_ids"]


@pytest.mark.offline
def test_dry_run_does_not_connect(monkeypatch, tmp_path: Path) -> None:
    def fail_create(_config):
        raise AssertionError("should not connect in dry-run")

    monkeypatch.setattr(bundle, "create_client", fail_create)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preview_product_bundle.py",
            "--root",
            str(tmp_path),
            "--product-id",
            "prd-a",
            "--dry-run",
        ],
    )

    assert bundle.main() == 0


@pytest.mark.offline
def test_execute_with_product_id_uses_mock(monkeypatch, tmp_path: Path) -> None:
    fake = FakeClient(
        [
            {"doc_id": "full", "product_id": "prd-a", "doc_type": "product_full", "source_text": "full"},
            {"doc_id": "qr", "product_id": "prd-a", "doc_type": "quality_report", "source_text": "report"},
        ]
    )
    monkeypatch.setattr(bundle, "load_env", lambda _root: None)
    monkeypatch.setattr(bundle, "ensure_runtime_config", lambda: {"instance_id": "inst"})
    monkeypatch.setattr(bundle, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preview_product_bundle.py",
            "--root",
            str(tmp_path),
            "--product-id",
            "prd-a",
            "--execute",
        ],
    )

    assert bundle.main() == 0
    payload = json.loads((tmp_path / "output/acceptance/product_bundle_preview.json").read_text(encoding="utf-8"))
    assert payload["bundles"][0]["product_id"] == "prd-a"
    assert len(payload["bundles"][0]["quality_reports"]) == 1
    assert fake.search_calls
