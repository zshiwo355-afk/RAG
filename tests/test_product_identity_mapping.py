from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import plan_product_identity_mapping as mapping
from conftest import write_json


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

    def search(self, _request):
        return FakeResponse({"result": [{"fields": row} for row in self.rows]})

    def get_table(self, _table_name):
        return FakeResponse({"result": {"fieldSchema": {"source_text_vector": "MULTI_FLOAT"}}})


def local_doc(product_id="local-a", product_name="大民族酒·复兴", spec="500ml*6") -> dict:
    return {
        "page_content": f"产品名称：{product_name}\n规格：{spec}",
        "metadata": {
            "doc_id": f"{product_id}__product_full",
            "product_id": product_id,
            "product_name": product_name,
            "spec": spec,
            "manufacturer": "贵州省仁怀市民族酒业有限公司",
            "source_doc_id": "source-a",
        },
    }


@pytest.mark.offline
def test_high_confidence_mapping_by_name_spec_manufacturer() -> None:
    local = mapping.document_identity(local_doc())
    candidate = {
        "doc_id": "old",
        "product_id": "prd_000053",
        "source_text": "产品名称：大民族酒·复兴\n规格：500ml*6\n生产厂家：贵州省仁怀市民族酒业有限公司",
    }

    result = mapping.choose_mapping(local, [candidate])

    assert result["confidence"] == "high"
    assert result["recommended_action"] == "map_to_existing"
    assert result["canonical_product_id"] == "prd_000053"
    assert result["needs_review"] is False


@pytest.mark.offline
def test_medium_confidence_requires_review() -> None:
    local = mapping.document_identity(local_doc(product_name="大民族酒·复兴", spec="500ml*6"))
    candidate = {
        "doc_id": "old",
        "product_id": "prd_000053",
        "source_text": "产品名称：大民族酒·复兴\n规格：500ml",
    }

    result = mapping.choose_mapping(local, [candidate])

    assert result["confidence"] in {"medium", "low"}
    assert result["recommended_action"] == "needs_review"
    assert result["needs_review"] is True


@pytest.mark.offline
def test_low_confidence_is_not_auto_mapping() -> None:
    local = mapping.document_identity(local_doc(product_name="大民族酒·复兴"))
    candidate = {"doc_id": "old", "product_id": "prd_x", "source_text": "产品名称：大民族酒·完全不同"}

    result = mapping.choose_mapping(local, [candidate])

    assert result["recommended_action"] == "needs_review"
    assert result["needs_review"] is True
    assert result["canonical_product_id"] == ""


@pytest.mark.offline
def test_dry_run_does_not_connect_opensearch(monkeypatch, tmp_path: Path) -> None:
    docs = tmp_path / "documents.json"
    write_json(docs, [local_doc()])

    def fail_create(_config):
        raise AssertionError("should not connect in dry-run")

    monkeypatch.setattr(mapping, "create_client", fail_create)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_product_identity_mapping.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs),
            "--source-doc-id",
            "source-a",
            "--dry-run",
        ],
    )

    assert mapping.main() == 0
    assert (tmp_path / "config/product_identity_map.csv").exists()


@pytest.mark.offline
def test_execute_uses_mock_and_writes_reports(monkeypatch, tmp_path: Path) -> None:
    docs = tmp_path / "documents.json"
    write_json(docs, [local_doc()])
    fake = FakeClient(
        [
            {
                "doc_id": "old",
                "product_id": "prd_000053",
                "source_text": "产品名称：大民族酒·复兴\n规格：500ml*6\n生产厂家：贵州省仁怀市民族酒业有限公司",
            }
        ]
    )
    monkeypatch.setattr(mapping, "load_env", lambda _root: None)
    monkeypatch.setattr(mapping, "ensure_runtime_config", lambda: {"instance_id": "inst"})
    monkeypatch.setattr(mapping, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_product_identity_mapping.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs),
            "--source-doc-id",
            "source-a",
            "--execute",
        ],
    )

    assert mapping.main() == 0
    payload = json.loads((tmp_path / "output/acceptance/dmz_product_mapping_candidates.json").read_text(encoding="utf-8"))
    assert payload["summary"]["map_to_existing"] == 1


@pytest.mark.offline
def test_source_name_is_written_to_custom_mapping(monkeypatch, tmp_path: Path) -> None:
    docs = tmp_path / "documents.json"
    write_json(docs, [local_doc()])
    map_path = tmp_path / "config/product_identity_map_srx.csv"
    fake = FakeClient(
        [
            {
                "doc_id": "old",
                "product_id": "prd_000053",
                "source_text": "产品名称：大民族酒·复兴\n规格：500ml*6\n生产厂家：贵州省仁怀市民族酒业有限公司",
            }
        ]
    )
    monkeypatch.setattr(mapping, "load_env", lambda _root: None)
    monkeypatch.setattr(mapping, "ensure_runtime_config", lambda: {"instance_id": "inst"})
    monkeypatch.setattr(mapping, "create_client", lambda _config: (fake, FakeModels))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_product_identity_mapping.py",
            "--root",
            str(tmp_path),
            "--documents",
            str(docs),
            "--source-doc-id",
            "source-a",
            "--execute",
            "--map-template",
            str(map_path),
            "--source-name",
            "石荣霄品牌产品信息表.xlsx",
        ],
    )

    assert mapping.main() == 0
    text = map_path.read_text(encoding="utf-8-sig")
    assert "石荣霄品牌产品信息表.xlsx" in text
