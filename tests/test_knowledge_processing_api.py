from __future__ import annotations

from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from rag_app.knowledge_processing import ProcessingError
from rag_app.knowledge_processing_api import get_catalog, get_processing_service, get_processing_store, router
from rag_app.knowledge_store import KnowledgeStore


pytestmark = pytest.mark.offline
TOKEN = "test-only-processing-gateway-secret-000000000"
JOB = "1" * 32
ITEM = "2" * 32
SELF = "company_knowledge.submissions.read"
READ = "company_knowledge.read"
DASH = "company_knowledge.dashboard.read"
REVIEW = "company_knowledge.review"


class Jobs:
    def __init__(self):
        self.calls = []
        self.job = {"job_id": JOB, "receipt_id": "3" * 32, "principal": "alice", "source_id": "notes:1",
                    "filename": "example.md", "rule_version": "receipt-rules-v1", "status": "failed",
                    "stage": "parse", "attempts": 5, "created_at": 1000, "updated_at": 1100,
                    "error_code": "processing_failed", "retryable": True,
                    "counts": {"draft": 1, "needs_review": 0, "archived": 0},
                    "object_key": "private-object", "lease_token": "private-lease",
                    "items": [{"item_id": ITEM, "source_path": "example.md", "status": "draft",
                               "title": "Example", "reason_codes": [], "knowledge_id": "draft", "revision": 1,
                               "object_key": "private-body"}],
                    "events": [{"event_id": "4" * 32, "event_type": "failed", "created_at": 1100,
                                "reason_code": "processing_failed", "debug": "private-debug"}]}

    def stats(self, principal):
        self.calls.append(("stats", principal))
        return dict(receipts=2, receipt_counts=dict(total=2, awaiting_upload=0, pending_verification=0, verified=1, rejected=1, failed=0), queued=0, running=0, completed=0, needs_review=0, failed=1,
                    draft_items=1, review_items=0, archived_items=0)

    def list_jobs(self, principal, **kwargs):
        self.calls.append(("list", principal, kwargs))
        return {"items": [deepcopy(self.job)] if principal in {None, "alice"} else [], "total": 1}

    def get_job(self, job_id, principal, **kwargs):
        self.calls.append(("detail", principal))
        if job_id != JOB or principal not in {None, "alice"}:
            raise ProcessingError("processing_job_not_found", 404)
        return deepcopy(self.job)

    def get_item(self, job_id, item_id, principal):
        self.get_job(job_id, principal)
        return {"item_id": item_id, "title": "Example", "content": "cleaned draft",
                "knowledge_id": "draft", "revision": 1, "object_key": "private-body"}

    def retry(self, job_id, principal, *, actor):
        self.calls.append(("retry", principal, actor))
        return deepcopy(self.job)

    def resolve(self, job_id, item_id, principal, **kwargs):
        self.calls.append(("resolve", job_id, item_id, principal, kwargs))
        if kwargs['expected_version'] != 1:
            raise ProcessingError('processing_resolution_conflict', 409)
        return {'item_id': item_id, 'status': 'archived', 'version': 2, 'published': False}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RECEIPTS_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_PROCESSING_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_RECEIPTS_TOKEN", TOKEN)
    jobs = Jobs()
    catalog = KnowledgeStore(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_processing_store] = lambda: jobs
    app.dependency_overrides[get_processing_service] = lambda: jobs
    app.dependency_overrides[get_catalog] = lambda: catalog
    with TestClient(app) as client:
        yield client, jobs, catalog


def headers(*permissions, principal="alice"):
    return {"Authorization": "Bearer " + TOKEN, "X-Knowledge-Principal": principal,
            "X-Knowledge-Permissions": ",".join(permissions)}


BASE = "/api/rag/knowledge-processing"


def test_resolve_requires_review_and_uses_trusted_actor_with_version_guard(api):
    client, jobs, _ = api
    path = BASE + f'/jobs/{JOB}/items/{ITEM}/resolve'
    body = dict(expected_version=1, action='archive', reason='保留原件')
    assert client.post(path, headers=headers(SELF), json=body).status_code == 403
    assert not jobs.calls
    response = client.post(path, headers=headers(REVIEW, principal='reviewer'), json=body)
    assert response.status_code == 200 and response.json()['version'] == 2
    assert jobs.calls[-1] == ('resolve', JOB, ITEM, None, {**body, 'replacement_receipt_id': None, 'actor': 'reviewer'})
    response = client.post(path, headers=headers(REVIEW), json={**body, 'expected_version': 2})
    assert response.status_code == 409 and response.json()['detail'] == 'processing_resolution_conflict'
    for extra in ({'actor': 'spoof'}, {'expected_version': True}, {'expected_version': '1'},
                  {'action': 'approve'}, {'content_ref': 'https://example.invalid/private'}):
        response = client.post(path, headers=headers(REVIEW), json={**body, **extra})
        assert response.status_code == 422 and response.json()['detail'] == 'invalid_processing_request'


def test_gateway_assertions_require_internal_auth_and_closed_permission_set(api):
    client, jobs, _ = api
    assert client.get(BASE + "/jobs", headers={"X-Knowledge-Principal": "alice", "X-Knowledge-Permissions": REVIEW}).status_code == 401
    for permissions in ((), ("*",), (SELF, SELF), (SELF, "company_knowledge.submit")):
        assert client.get(BASE + "/jobs", headers=headers(*permissions)).status_code == 403
    assert not jobs.calls


def test_personal_reads_are_owner_scoped_and_storage_details_are_never_returned(api):
    client, jobs, _ = api
    response = client.get(BASE + f"/jobs/{JOB}", headers=headers(SELF))
    assert response.status_code == 200
    assert jobs.calls == [("detail", "alice")]
    assert "private" not in response.text and "principal" not in response.json()
    assert response.json()["published"] is False
    assert response.json()["created_at"] == "1970-01-01T00:16:40Z"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(BASE + f"/jobs/{JOB}", headers=headers(SELF, principal="bob")).status_code == 404
    assert client.get(BASE + f"/jobs/{JOB}/items/{ITEM}", headers=headers(SELF, principal="bob")).status_code == 404
    preview = client.get(BASE + f"/jobs/{JOB}/items/{ITEM}", headers=headers(SELF))
    assert preview.json()["content"] == "cleaned draft" and "private" not in preview.text


def test_dashboard_permission_gives_aggregate_only_not_company_drafts(api):
    client, jobs, _ = api
    response = client.get(BASE + "/overview?scope=company", headers=headers(DASH))
    assert response.status_code == 200
    assert jobs.calls == [("stats", None)]
    assert response.json()["counts"]["published_assets"] == 0
    for path in ("/jobs?scope=company", f"/jobs/{JOB}?scope=company", f"/jobs/{JOB}/items/{ITEM}?scope=company"):
        assert client.get(BASE + path, headers=headers(DASH)).status_code == 403
    assert client.post(BASE + f"/jobs/{JOB}/retry", headers=headers(DASH), json={}).status_code == 403


def test_review_permission_reads_company_jobs_and_audits_retry_actor(api):
    client, jobs, _ = api
    response = client.get(BASE + "/jobs?scope=company&status=failed&limit=10&offset=20", headers=headers(REVIEW, principal="reviewer"))
    assert response.status_code == 200
    assert jobs.calls == [("list", None, {"status": "failed", "limit": 10, "offset": 20})]
    response = client.post(BASE + f"/jobs/{JOB}/retry", headers=headers(REVIEW, principal="reviewer"), json={})
    assert response.status_code == 200 and jobs.calls[-1] == ("retry", None, "reviewer")
    assert client.post(BASE + f"/jobs/{JOB}/retry", headers=headers(SELF), json={}).status_code == 403


def test_inputs_are_bounded_and_validation_errors_do_not_echo_secrets(api):
    client, jobs, _ = api
    for path in ("/jobs?limit=101", "/jobs?offset=-1", "/jobs?status=secret-value", "/jobs/not-a-job"):
        response = client.get(BASE + path, headers=headers(REVIEW))
        assert response.status_code == 422 and "secret-value" not in response.text
    response = client.post(BASE + f"/jobs/{JOB}/retry", headers=headers(REVIEW), json={"actor": "forged-secret"})
    assert response.status_code == 422 and "forged-secret" not in response.text
    assert not jobs.calls


def test_errors_do_not_expose_sdk_details_and_disabled_feature_fails_closed(api, monkeypatch):
    client, jobs, _ = api
    def fail(*args, **kwargs):
        raise RuntimeError("secret signed-OSS-URL")
    jobs.stats = fail
    response = client.get(BASE + "/overview", headers=headers(SELF))
    assert response.status_code == 503 and "secret" not in response.text
    monkeypatch.setenv("KNOWLEDGE_PROCESSING_ENABLED", "0")
    response = client.get(BASE + "/jobs", headers=headers(REVIEW))
    assert response.status_code == 503 and response.json()["detail"] == "processing_disabled"


def test_published_counts_are_catalogue_assets_and_respect_read_permission(api):
    client, _, catalog = api
    entry = {"knowledge_id": "method", "title": "方法", "content": "完整方法正文", "contributor": "未知",
             "kind": "method", "sources": [{"name": "method.md", "locator": "test:method"}]}
    draft = catalog.import_draft(entry)
    assert catalog.published_count() == 0
    snapshot = catalog.snapshot("method")
    catalog.publish("method", draft["revision"], expected_generation=snapshot["generation"], confirmed_by="test")
    catalog.import_draft({**entry, "content": "新版本草稿"})
    catalog.import_draft({**entry, "knowledge_id": "other", "title": "另一份草稿"})
    assert catalog.published_count() == 1
    assert client.get(BASE + "/overview", headers=headers(SELF)).json()["counts"]["published_assets"] is None
    assert client.get(BASE + "/overview", headers=headers(SELF, READ)).json()["counts"]["published_assets"] == 1
    catalog.withdraw("method")
    assert catalog.published_count() == 0


def _publish_catalog_entry(catalog, key, kind="method", **changes):
    entry = {"knowledge_id": key, "title": key + " 公开说明", "content": "正式知识完整正文",
             "kind": kind, "sources": [{"name": "source.md"}], **changes}
    draft = catalog.import_draft(entry)
    catalog.publish(key, draft["revision"], expected_generation=draft["generation"], confirmed_by="reviewer")
    return entry


def test_catalog_filters_keep_global_counts_and_missing_employee_positions_explicit(api):
    client, jobs, catalog = api
    _publish_catalog_entry(catalog, "SOP-a", "process", department="运营")
    _publish_catalog_entry(catalog, "skill-a", "method", evidence={"source_type": "Skill方法"})
    _publish_catalog_entry(catalog, "case-a", "reference_case", evidence={"source_type": "Skill方法"})
    _publish_catalog_entry(catalog, "other-a", "未知")
    response = client.get(BASE + "/catalog?q=sop&kind=sop&uploader_position=unknown&limit=1", headers=headers(READ))
    assert response.status_code == 200
    data = response.json()
    assert data["counts"] == {"published_assets": 4, "positions": 0, "unknown_position": 4}
    assert data["by_uploader_position"] == [{"key": "unknown", "label": "待补充岗位", "count": 4}]
    assert data["total"] == 1 and data["items"][0]["knowledge_id"] == "SOP-a"
    assert data["items"][0]["kind_label"] == "SOP / 流程"
    assert {item["key"]: item["count"] for item in data["by_kind"] if item["count"]} == {
        "sop": 1, "skill": 1, "case": 1, "other": 1}
    assert client.get(BASE + "/catalog/skill-a", headers=headers(READ)).json()["kind"] == "skill"
    assert client.get(BASE + "/catalog/case-a", headers=headers(READ)).json()["kind"] == "case"
    assert client.get(BASE + "/catalog?uploader_position=运营", headers=headers(READ)).json()["total"] == 0
    assert client.get(BASE + "/catalog?offset=4", headers=headers(READ)).json()["items"] == []
    assert not jobs.calls


def test_catalog_and_full_body_never_expose_newer_drafts_or_withdrawn_knowledge(api):
    client, _, catalog = api
    entry = _publish_catalog_entry(catalog, "method-a")
    catalog.import_draft({**entry, "title": "private-draft-title", "content": "private-draft-body"})
    catalog.import_draft({**entry, "knowledge_id": "only-draft", "content": "private-new-asset"})
    response = client.get(BASE + "/catalog", headers=headers(READ))
    assert response.status_code == 200 and response.json()["total"] == 1
    assert "private" not in response.text and "content" not in response.json()["items"][0]
    response = client.get(BASE + "/catalog/method-a", headers=headers(READ))
    assert response.status_code == 200 and response.json()["content"] == entry["content"]
    assert response.json()["revision"] == 1 and "private" not in response.text
    assert response.json()["uploader_position"] is None
    assert response.json()["scenarios"] == []
    assert response.json()["source_kind"] == "method"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(BASE + "/catalog/only-draft", headers=headers(READ)).status_code == 404
    catalog.withdraw("method-a")
    assert client.get(BASE + "/catalog/method-a", headers=headers(READ)).status_code == 404
    assert client.get(BASE + "/catalog", headers=headers(READ)).json()["counts"]["published_assets"] == 0


def test_catalog_requires_company_knowledge_read_and_bounds_inputs(api, monkeypatch):
    client, _, catalog = api
    for permission in (SELF, DASH):
        assert client.get(BASE + "/catalog", headers=headers(permission)).status_code == 403
        assert client.get(BASE + "/catalog/method-a", headers=headers(permission)).status_code == 403
    assert client.get(BASE + "/catalog", headers=headers(REVIEW)).status_code == 200
    assert client.get(BASE + "/catalog", headers={"X-Knowledge-Permissions": READ}).status_code == 401
    for query in ("limit=101", "offset=-1", "q=" + "a" * 201, "kind=" + "a" * 201,
                  "uploader_position=" + "a" * 201, "q=%0A", "kind=%00", "uploader_position=%7F"):
        response = client.get(BASE + "/catalog?" + query, headers=headers(READ))
        assert response.status_code == 422
        assert response.json()["detail"] == "invalid_processing_request"
    def fail():
        raise RuntimeError("secret-OSS-location")
    monkeypatch.setattr(catalog, "published_catalog", fail)
    response = client.get(BASE + "/catalog", headers=headers(READ))
    assert response.status_code == 503 and "secret" not in response.text


def test_graph_is_public_read_only_and_does_not_grant_private_processing_access(api, monkeypatch):
    client, jobs, _ = api
    from rag_app import knowledge_processing_api
    called = []
    monkeypatch.setattr(knowledge_processing_api._graph, "read", lambda *args: called.append(True) or {
        "nodes": [], "edges": [], "status": "ready", "counts": {"published_assets": 0, "indexed_assets": 0,
        "failed_assets": 0, "explicit_edges": 0, "related_edges": 0}, "truncated": False})
    for permission in (SELF, DASH):
        assert client.get(BASE + "/graph", headers=headers(permission)).status_code == 403
    assert not called
    assert client.get(BASE + "/graph", headers=headers(READ)).status_code == 200
    assert client.get(BASE + "/graph", headers=headers(REVIEW)).status_code == 200
    assert client.get(BASE + "/graph?scope=company", headers=headers(READ)).status_code == 422
    assert client.get(BASE + "/graph?q=private", headers=headers(READ)).status_code == 422
    assert len(called) == 2
    assert client.get(BASE + "/graph", headers={}).status_code == 401
    assert client.post(BASE + "/graph", headers=headers(REVIEW), json={}).status_code == 405
    assert not jobs.calls
