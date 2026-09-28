import base64
import hashlib
import re
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from rag_app import knowledge_api
from rag_app.knowledge_trial_ui import create_app


pytestmark = pytest.mark.offline

PAYLOAD = '</script><img src=x onerror="alert(1)">'


class FakeService:
    def __init__(self):
        self.calls = []
        self.record = {
            "knowledge_id": "trial-only", "revision": 2, "title": PAYLOAD,
            "kind": "method", "snippet": PAYLOAD, "content": "完整正文\n" + PAYLOAD,
            "sources": [{"name": PAYLOAD, "locator": "第一节", "url": "javascript:alert(1)"}],
        }

    def search(self, query, top_k, **options):
        self.calls.append(("search", query, top_k, options))
        return [deepcopy(self.record)]

    def get(self, knowledge_id, *, revision=None):
        self.calls.append(("get", knowledge_id, revision))
        if knowledge_id == "trial-only" and revision in (None, 2):
            return deepcopy(self.record)
        return None


def catalog():
    return {
        "trial_id": "local-test", "asset_count": 1, "chunk_count": 3,
        "assets": [{"knowledge_id": "trial-only", "revision": 2, "title": PAYLOAD,
                    "kind": "method", "chars": 60, "content_ref": "must-not-leak"}],
        "sample_queries": [PAYLOAD], "database_url": "must-not-leak",
    }


def test_catalog_is_a_snapshot_with_only_public_fields():
    supplied = catalog()
    app = create_app(FakeService(), supplied)
    supplied["assets"][0]["title"] = "changed after app creation"
    supplied["sample_queries"].append("changed after app creation")
    with TestClient(app) as client:
        response = client.get("/api/trial/catalog")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "trial_id": "local-test", "asset_count": 1, "chunk_count": 3,
            "assets": [{"knowledge_id": "trial-only", "revision": 2, "title": PAYLOAD,
                        "kind": "method", "chars": 60}],
            "sample_queries": [PAYLOAD],
        }
        assert "must-not-leak" not in response.text


def test_search_and_versioned_read_use_only_explicit_service(monkeypatch):
    def unexpected_default_service():
        raise AssertionError("must not construct the default service")

    monkeypatch.setattr(knowledge_api, "KnowledgeService", unexpected_default_service)
    service = FakeService()
    with TestClient(create_app(service, catalog())) as client:
        result = client.post("/api/knowledge/search", json={
            "query": "如何复用本批方法？", "top_k": 5, "include_content": True,
        })
        assert result.status_code == 200
        assert result.json()["results"] == [service.record]
        full = client.get("/api/knowledge/trial-only?revision=2")
        assert full.json()["knowledge"] == service.record
        assert client.get("/api/knowledge/trial-only?revision=1").status_code == 404
        assert client.get("/api/knowledge/production-only").status_code == 404
    assert service.calls == [
        ("search", "如何复用本批方法？", 5,
         {"include_content": True, "department": None, "scenario": None}),
        ("get", "trial-only", 2), ("get", "trial-only", 1),
        ("get", "production-only", None),
    ]


def test_page_keeps_untrusted_data_out_of_html_and_uses_text_dom_sinks():
    with TestClient(create_app(FakeService(), catalog())) as client:
        response = client.get("/")
    page = response.text
    assert response.status_code == 200
    assert "隔离试用，正式未发布；只查本批新入库资料" in page
    assert PAYLOAD not in page
    assert "javascript:alert(1)" not in page
    assert "must-not-leak" not in page
    assert "innerHTML" not in page and "outerHTML" not in page
    assert "insertAdjacentHTML" not in page and "document.write" not in page
    assert "item.textContent = String(text)" in page
    assert "include_content: true" in page
    assert "element('pre', content)" in page
    assert 'aria-live="polite"' in page
    assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=", page)
    policy = response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in policy
    assert "default-src 'none'" in policy and "connect-src 'self'" in policy
    for tag in ("script", "style"):
        source = re.search(f"<{tag}>(.*?)</{tag}>", page, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
        assert f"{tag}-src 'sha256-{digest}'" in policy


def test_routes_expose_only_trial_page_catalog_and_read_only_knowledge():
    app = create_app(FakeService(), catalog())
    assert {(route.path, frozenset(route.methods)) for route in app.routes} == {
        ("/", frozenset({"GET"})),
        ("/api/trial/catalog", frozenset({"GET"})),
        ("/api/knowledge/search", frozenset({"POST"})),
        ("/api/knowledge/{knowledge_id}", frozenset({"GET"})),
    }
    with TestClient(app) as client:
        for path in ("/docs", "/openapi.json", "/api/rag/search", "/api/rag/answer",
                     "/api/rag/visual-ingest/run", "/api/knowledge/publish", "/api/upload"):
            assert client.post(path, json={}).status_code in (404, 405)
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)("/api/knowledge/trial-only").status_code == 405
