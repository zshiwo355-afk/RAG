from __future__ import annotations

import pytest

from rag_app import answer_service, retrieval_service


def import_rag_api_with_runtime_stubs(monkeypatch):
    import importlib
    import sys
    import types

    fastapi = types.ModuleType("fastapi")

    class FakeFastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def add_middleware(self, *args, **kwargs):
            pass

        def get(self, *_args, **_kwargs):
            return lambda func: func

        def post(self, *_args, **_kwargs):
            return lambda func: func

    class FakeHTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.FastAPI = FakeFastAPI
    fastapi.HTTPException = FakeHTTPException
    middleware = types.ModuleType("fastapi.middleware")
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = object
    pydantic = types.ModuleType("pydantic")

    class FakeBaseModel:
        pass

    pydantic.BaseModel = FakeBaseModel
    pydantic.Field = lambda default, **_kwargs: default
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.middleware", middleware)
    monkeypatch.setitem(sys.modules, "fastapi.middleware.cors", cors)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)
    sys.modules.pop("rag_api", None)
    return importlib.import_module("rag_api")


@pytest.mark.offline
def test_product_bundle_groups_full_quality_report_and_supplement() -> None:
    bundle = retrieval_service.build_product_bundle(
        "prod-a",
        [
            {"doc_id": "full", "product_id": "prod-a", "doc_type": "product_full", "source_text": "产品名称：老产品"},
            {"doc_id": "qr", "product_id": "prod-a", "doc_type": "quality_report", "source_text": "检验结论：合格"},
            {"doc_id": "supp", "product_id": "prod-a", "doc_type": "supplement", "source_text": "最新补充资料：适合宴请"},
        ],
    )

    assert bundle["basic_info"][0]["doc_id"] == "full"
    assert bundle["quality_reports"][0]["doc_id"] == "qr"
    assert bundle["supplements"][0]["doc_id"] == "supp"
    assert {"full", "qr", "supp"}.issubset(set(bundle["doc_ids"]))


@pytest.mark.offline
def test_supplement_does_not_override_quality_report() -> None:
    bundle = retrieval_service.build_product_bundle(
        "prod-a",
        [
            {"doc_id": "qr", "product_id": "prod-a", "doc_type": "quality_report", "source_text": "质检报告：酒精度53%vol"},
            {"doc_id": "supp", "product_id": "prod-a", "doc_type": "supplement", "source_text": "补充资料：酒精度52%vol"},
        ],
    )

    assert bundle["quality_reports"][0]["source_text"] == "质检报告：酒精度53%vol"
    assert bundle["supplements"][0]["source_text"] == "补充资料：酒精度52%vol"


@pytest.mark.offline
def test_supplement_conflict_is_recorded() -> None:
    bundle = retrieval_service.build_product_bundle(
        "prod-a",
        [
            {"doc_id": "full", "product_id": "prod-a", "doc_type": "product_full", "source_text": "产品名称：老产品 品牌：旧品牌 规格：500ml"},
            {"doc_id": "supp", "product_id": "prod-a", "doc_type": "supplement", "source_text": "产品名称：新产品 品牌：新品牌 规格：100ml"},
        ],
    )

    fields = {item["field"] for item in bundle["conflicts"]}
    assert "product_name" in fields
    assert "brand" in fields
    assert "spec" in fields
    assert all(item["supplement_doc_id"] == "supp" for item in bundle["conflicts"])


@pytest.mark.offline
def test_needs_review_rows_do_not_join_bundle() -> None:
    bundle = retrieval_service.build_product_bundle(
        "prod-a",
        [
            {"doc_id": "full", "product_id": "prod-a", "doc_type": "product_full", "source_text": "ok"},
            {"doc_id": "review", "product_id": "prod-a", "doc_type": "supplement", "status": "needs_review", "source_text": "skip"},
        ],
    )

    assert [item["doc_id"] for item in bundle["basic_info"]] == ["full"]
    assert bundle["supplements"] == []
    assert "review" not in bundle["doc_ids"]


@pytest.mark.offline
def test_answer_context_prefers_bundle_and_mentions_conflicts() -> None:
    product = {
        "product_id": "prod-a",
        "routes": ["text_dense"],
        "best_text": "旧核心资料",
        "product_bundle": {
            "product_id": "prod-a",
            "basic_info": [{"source_text": "产品名称：老产品"}],
            "quality_reports": [{"source_text": "检验结论：合格"}],
            "supplements": [{"source_text": "补充资料：新包装"}],
            "conflicts": [{"field": "product_name", "old_value": "老产品", "supplement_value": "新产品", "supplement_doc_id": "supp", "severity": "high"}],
        },
    }

    context = answer_service.build_answer_context("介绍一下", [product])
    text = context["context_text"]

    assert "产品完整资料包-质检报告" in text
    assert "产品完整资料包-补充资料" in text
    assert "资料存在新旧差异" in text


@pytest.mark.offline
def test_api_search_summary_keeps_old_fields_and_adds_product_bundle(monkeypatch) -> None:
    rag_api = import_rag_api_with_runtime_stubs(monkeypatch)
    item = {
        "product_id": "prod-a",
        "final_score": 0.8,
        "routes": ["text_dense"],
        "best_text": "hello",
        "doc_hits": [],
        "image_hits": [],
        "product_bundle": {"product_id": "prod-a", "quality_reports": [{"source_text": "report"}]},
    }

    summary = rag_api.summarize_search_result(item)

    assert summary["product_id"] == "prod-a"
    assert "doc_hits" in summary
    assert "image_hits" in summary
    assert summary["product_bundle"]["product_id"] == "prod-a"
