from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from rag_app.knowledge_receipts import (
    LEASE_SECONDS, MAX_ATTEMPTS, LeaseLost, ReceiptError, ReceiptObjects, ReceiptService, ReceiptStore,
)
from rag_app.knowledge_receipts_api import get_receipt_service, router


pytestmark = pytest.mark.offline
TOKEN = "test-only-independent-receipts-secret-00000000"
BODY = b"synthetic original, never company knowledge"


class FakeBucket:
    def __init__(self):
        self.data, self.meta, self.acls = {}, {}, {}
        self.acl, self.versioning = "private", None
        self.read_sizes, self.get_headers, self.signatures = [], [], []
        self.read_error = None

    def get_bucket_acl(self):
        return SimpleNamespace(acl=self.acl)

    def get_bucket_versioning(self):
        return SimpleNamespace(status=self.versioning)

    def sign_url(self, method, key, expires, headers, additional_headers):
        self.signatures.append((method, key, dict(headers), additional_headers))
        return "https://private-test.oss-cn-hangzhou.aliyuncs.com/" + key + "?x-oss-signature-version=OSS4-HMAC-SHA256&x-oss-additional-headers=content-length&x-oss-signature=synthetic"

    def upload(self, row, data=BODY):
        key = row["object_key"]
        if key in self.data:
            raise RuntimeError("forbidden_overwrite")
        self.data[key] = data
        self.meta[key] = {"x-oss-meta-receipt-id": row["receipt_id"], "x-oss-meta-sha256": row["sha256"]}
        self.acls[key] = "private"

    def head_object(self, key):
        if key not in self.data:
            error = RuntimeError("secret-sdk-url-and-body")
            error.status = 404
            raise error
        data = self.data[key]
        return SimpleNamespace(content_length=len(data), etag=hashlib.md5(data).hexdigest().upper(), headers=self.meta[key])

    def get_object_acl(self, key):
        return SimpleNamespace(acl=self.acls[key])

    def get_object(self, key, headers):
        if self.read_error:
            raise self.read_error
        self.get_headers.append(headers)
        assert headers == {"If-Match": '"' + self.head_object(key).etag + '"'}
        bucket = self

        class Stream(io.BytesIO):
            def read(self, size):
                bucket.read_sizes.append(size)
                return super().read(min(7, size))

        return Stream(self.data[key])


def payload(**changes):
    return {"idempotency_key": "sample-1", "source_id": "hermes:synthetic:session-1", "filename": "sample.json",
            "byte_length": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest(), **changes}


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    store = ReceiptStore(tmp_path / "receipts.db", clock=lambda: now[0])
    store.initialize()
    bucket = FakeBucket()
    service = ReceiptService(store, ReceiptObjects(bucket))
    return SimpleNamespace(store=store, bucket=bucket, service=service, now=now)


def prepared(system, **changes):
    response = system.service.prepare("user:a", payload(**changes))
    return system.store.get("user:a", response["receipt"]["receipt_id"])


def pending(system, **changes):
    row = prepared(system, **changes)
    system.bucket.upload(row)
    system.service.complete("user:a", row["receipt_id"])
    return system.store.get("user:a", row["receipt_id"])


def test_original_roundtrip_is_not_publication_and_streams_bounded(system):
    row = prepared(system)
    grant = system.service.prepare("user:a", payload())
    assert grant["receipt"]["receipt_id"] == row["receipt_id"]
    headers = grant["upload"]["headers"]
    assert headers["Content-Length"] == str(len(BODY))
    assert headers["x-oss-forbid-overwrite"] == "true" and headers["x-oss-object-acl"] == "private"
    with pytest.raises(ReceiptError, match="upload_not_found"):
        system.service.complete("user:a", row["receipt_id"])
    system.bucket.upload(row)
    receipt = system.service.complete("user:a", row["receipt_id"])
    assert receipt["receipt"]["status"] == "pending_verification"
    assert not receipt["receipt"]["content_verified"] and receipt["upload"] is None
    assert system.service.verify_once()
    receipt = system.service.status("user:a", row["receipt_id"])
    assert receipt["receipt"]["status"] == "verified" and receipt["receipt"]["content_verified"]
    assert not receipt["receipt"]["published"] and receipt["upload"] is None
    assert max(system.bucket.read_sizes) <= 65536
    assert system.service.prepare("user:a", payload())["upload"] is None
    assert system.service.complete("user:a", row["receipt_id"])["receipt"]["status"] == "verified"
    assert not system.service.verify_once()


def test_idempotency_is_atomic_payload_bound_and_principal_scoped(system):
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: system.store.prepare("user:a", payload()), range(12)))
    assert len({row["receipt_id"] for row in rows}) == 1
    for changes in ({"source_id": "other"}, {"filename": "other.txt"}, {"sha256": "0" * 64}, {"byte_length": 1}):
        with pytest.raises(ReceiptError, match="idempotency_conflict"):
            system.store.prepare("user:a", payload(**changes))
    other = system.store.prepare("user:b", payload())
    assert other["receipt_id"] != rows[0]["receipt_id"]
    assert other["object_key"] != rows[0]["object_key"]
    with pytest.raises(ReceiptError, match="receipt_not_found"):
        system.service.complete("user:b", rows[0]["receipt_id"])


def test_expired_lease_recovery_fences_previous_worker_and_limits_global_concurrency(system):
    row = pending(system)
    second = pending(system, idempotency_key="second")
    first = system.store.claim()
    assert first["receipt_id"] in {row["receipt_id"], second["receipt_id"]}
    assert system.store.claim() is None
    system.now[0] += LEASE_SECONDS + 1
    replacement = system.store.claim()
    assert replacement["receipt_id"] == first["receipt_id"]
    assert replacement["lease_token"] != first["lease_token"]
    with pytest.raises(LeaseLost):
        system.store.heartbeat(first)
    assert not system.store.finish(first, "verified")
    assert system.store.finish(replacement, "verified")
    assert system.store.claim() is not None


@pytest.mark.parametrize("issue,code", [("hash", "object_hash_mismatch"), ("size", "object_size_mismatch"),
                                        ("acl", "object_not_private"), ("metadata", "object_metadata_mismatch")])
def test_bad_original_is_rejected_without_publication(system, issue, code):
    row = prepared(system)
    system.bucket.upload(row, b"x" * len(BODY) if issue == "hash" else BODY)
    if issue == "size":
        system.bucket.data[row["object_key"]] += b"x"
    if issue == "acl":
        system.bucket.acls[row["object_key"]] = "public-read"
    if issue == "metadata":
        system.bucket.meta[row["object_key"]]["x-oss-meta-receipt-id"] = "wrong"
    system.service.complete("user:a", row["receipt_id"])
    system.service.verify_once()
    result = system.service.status("user:a", row["receipt_id"])
    assert result["receipt"]["status"] == "rejected"
    assert result["receipt"]["error_code"] == code
    assert not result["receipt"]["content_verified"] and not result["receipt"]["published"]
    assert not result["receipt"]["retryable"]


def test_changed_object_since_completion_is_rejected(system):
    row = pending(system)
    system.bucket.data[row["object_key"]] = b"x" * len(BODY)
    system.service.verify_once()
    assert system.service.status("user:a", row["receipt_id"])["receipt"]["error_code"] == "object_changed"


def test_transient_errors_backoff_stop_and_allow_explicit_retry(system):
    row = pending(system)
    system.bucket.read_error = RuntimeError("secret-credential-and-signed-url")
    for attempt in range(MAX_ATTEMPTS):
        assert system.service.verify_once()
        result = system.service.status("user:a", row["receipt_id"])
        assert "secret" not in str(result)
        assert not system.service.verify_once()
        system.now[0] += 301
    assert result["receipt"]["status"] == "failed" and result["receipt"]["retryable"]
    system.bucket.read_error = None
    system.service.complete("user:a", row["receipt_id"])
    assert system.service.verify_once()
    assert system.service.status("user:a", row["receipt_id"])["receipt"]["status"] == "verified"


def test_process_restart_recovers_pending_and_expired_work(system):
    row = pending(system)
    old = system.store.claim()
    system.now[0] += LEASE_SECONDS + 1
    restarted = ReceiptService(ReceiptStore(system.store.path, clock=system.store.clock), ReceiptObjects(system.bucket))
    assert restarted.verify_once()
    assert restarted.status("user:a", row["receipt_id"])["receipt"]["status"] == "verified"
    assert not system.store.finish(old, "rejected", "stale")


@pytest.mark.parametrize("acl,versioning,code", [("public-read", None, "receipt_bucket_not_private"),
    ("private", "Enabled", "receipt_bucket_versioning_unsupported"), ("private", "Suspended", "receipt_bucket_versioning_unsupported")])
def test_unsafe_bucket_never_gets_signed_grant(system, acl, versioning, code):
    system.bucket.acl, system.bucket.versioning = acl, versioning
    with pytest.raises(ReceiptError, match=code):
        prepared(system)
    assert system.bucket.signatures == []


def test_no_legacy_bucket_fallback(monkeypatch):
    for name in ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "ACCESS_KEY_SECRET"):
        monkeypatch.delenv("KNOWLEDGE_RECEIPTS_OSS_" + name, raising=False)
        monkeypatch.setenv("OSS_" + name, "legacy-secret-value")
    with pytest.raises(ReceiptError, match="receipt_oss_not_configured"):
        ReceiptObjects()
    monkeypatch.delenv("KNOWLEDGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_RECEIPTS_DEVELOPMENT", raising=False)
    with pytest.raises(ReceiptError, match="receipt_database_not_configured"):
        ReceiptStore()


def test_sdk_v4_signature_binds_length_and_private_immutable_headers(system):
    import oss2

    real = oss2.Bucket(oss2.AuthV4("test-id", "test-secret"), "https://oss-cn-hangzhou.aliyuncs.com",
                       "private-test", region="cn-hangzhou")
    system.bucket.sign_url = real.sign_url
    result = system.service.prepare("user:a", payload())
    query = parse_qs(urlsplit(result["upload"]["url"]).query)
    assert "content-length" in query["x-oss-additional-headers"][0].split(";")
    row = system.store.get("user:a", result["receipt"]["receipt_id"])
    original = result["upload"]["headers"]
    for header, value in (("Content-Length", "1"), ("x-oss-object-acl", "public-read"), ("x-oss-forbid-overwrite", "false")):
        changed = real.sign_url("PUT", row["object_key"], 900, headers={**original, header: value}, additional_headers={"content-length"})
        assert parse_qs(urlsplit(changed).query)["x-oss-signature"] != query["x-oss-signature"]


def test_api_auth_owner_boundary_extra_fields_and_disabled_default(system, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    calls = []
    app.dependency_overrides[get_receipt_service] = lambda: calls.append(True) or system.service
    path = "/api/knowledge-intake/uploads"
    with TestClient(app) as client:
        monkeypatch.delenv("KNOWLEDGE_RECEIPTS_ENABLED", raising=False)
        assert client.post(path, json=payload()).status_code == 503
        monkeypatch.setenv("KNOWLEDGE_RECEIPTS_ENABLED", "1")
        monkeypatch.setenv("KNOWLEDGE_RECEIPTS_TOKEN", "short")
        assert client.post(path, json=payload()).status_code == 503
        monkeypatch.setenv("KNOWLEDGE_RECEIPTS_TOKEN", TOKEN)
        assert client.post(path, json=payload()).status_code == 401
        assert calls == []
        headers = {"Authorization": "Bearer " + TOKEN, "X-Knowledge-Principal": "user:a"}
        first = client.post(path, json=payload(), headers=headers)
        assert first.status_code == 200
        receipt_path = path + "/" + first.json()["receipt"]["receipt_id"]
        for method, target, kwargs in (("get", receipt_path, {}), ("post", receipt_path + "/complete", {"json": {}})):
            assert getattr(client, method)(target, **kwargs).status_code == 401
            other = {**headers, "X-Knowledge-Principal": "user:b"}
            assert getattr(client, method)(target, headers=other, **kwargs).status_code == 404
        assert client.post(path, json=payload(principal="user:b"), headers=headers).status_code == 422
        assert client.post(receipt_path + "/complete", json={"object_key": "other"}, headers=headers).status_code == 422
        assert client.post(path, json=payload(byte_length=True), headers=headers).status_code == 422
        for filename in ("../other", "control\u0085.json", "bidi\u202e.json", "surrogate\ud800.json"):
            invalid = client.post(path, content=json.dumps(payload(filename=filename)),
                                  headers={**headers, "Content-Type": "application/json"})
            assert invalid.status_code == 422 and invalid.json() == {"detail": "invalid_upload_request"}
        assert client.post(path, json=payload(sha256="0" * 64), headers=headers).status_code == 409
        assert client.get(receipt_path, headers=headers).json()["upload"] is None


def test_schema_is_never_created_by_prepare_and_cli_requires_execute(tmp_path):
    path = tmp_path / "uninitialized.db"
    store = ReceiptStore(path)
    with pytest.raises(ReceiptError, match="not_initialized"):
        store.prepare("user:a", payload())
    assert not path.exists()
    sqlite3.connect(path).close()
    with pytest.raises(sqlite3.OperationalError):
        store.prepare("user:a", payload())
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    connection.close()
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts" / "knowledge_receipts.py"), "--sqlite", str(path), "init-db"]
    assert subprocess.run(command, capture_output=True, text=True).returncode == 0
    store.initialize()
    first = store.prepare("user:a", payload())
    assert subprocess.run(command + ["--execute"], capture_output=True, text=True).returncode == 0
    assert store.get("user:a", first["receipt_id"])["status"] == "awaiting_upload"
