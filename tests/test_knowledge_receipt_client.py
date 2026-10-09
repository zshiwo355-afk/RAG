from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace

import pytest

import upload_knowledge_receipt as client
from test_knowledge_receipts import BODY, FakeBucket
from rag_app.knowledge_receipts import ReceiptObjects, ReceiptService, ReceiptStore

pytestmark = pytest.mark.offline


def test_client_retries_one_persistent_receipt_without_second_put(tmp_path):
    original = tmp_path / "synthetic.txt"
    original.write_bytes(BODY)
    database = tmp_path / "receipts.db"
    store = ReceiptStore(database)
    store.initialize()
    bucket = FakeBucket()
    service = ReceiptService(store, ReceiptObjects(bucket))
    puts = []

    def call(name, args):
        if name == client.PREPARE:
            return service.prepare("employee:a", args)
        if name == client.COMPLETE:
            return service.complete("employee:a", args["receipt_id"])
        raise AssertionError(name)

    def put(handle, grant, size):
        rid = grant["headers"]["x-oss-meta-receipt-id"]
        row = store.get("employee:a", rid)
        assert size == len(BODY)
        bucket.upload(row, handle.read())
        puts.append(rid)

    with original.open("rb") as handle:
        payload, stamp = client.describe_file(handle, original.name, "hermes:synthetic:1")
        first = client.upload(handle, payload, stamp, call, put=put, wait_seconds=0)
    assert first["status"] == "pending_verification" and not first["published"]
    # Simulate process restart using the persistent receipt DB.
    service = ReceiptService(ReceiptStore(database), ReceiptObjects(bucket))
    assert service.verify_once()
    with original.open("rb") as handle:
        payload, stamp = client.describe_file(handle, original.name, "hermes:synthetic:1")
        second = client.upload(handle, payload, stamp, call, put=put, wait_seconds=0)
    assert second["receipt_id"] == first["receipt_id"] and second["status"] == "verified"
    assert len(puts) == 1 and second["content_verified"] and not second["published"]


def test_lost_completion_can_resume_after_oss_already_has_the_object(tmp_path):
    path = tmp_path / "retry.txt"
    path.write_bytes(BODY)
    store = ReceiptStore(tmp_path / "receipts.db")
    store.initialize()
    bucket = FakeBucket()
    service = ReceiptService(store, ReceiptObjects(bucket))
    dropped = [False]
    attempts = []

    def call(name, args):
        if name == client.PREPARE:
            return service.prepare("employee:a", args)
        if not dropped[0]:
            dropped[0] = True
            raise OSError("connection_lost_before_complete")
        return service.complete("employee:a", args["receipt_id"])

    def put(handle, grant, size):
        row = store.get("employee:a", grant["headers"]["x-oss-meta-receipt-id"])
        attempts.append(row["receipt_id"])
        # Model the immutable OSS 409 path accepted by put_original.
        if row["object_key"] not in bucket.data:
            bucket.upload(row, handle.read())

    with path.open("rb") as handle:
        payload, stamp = client.describe_file(handle, path.name, "source:retry")
        with pytest.raises(OSError):
            client.upload(handle, payload, stamp, call, put=put, wait_seconds=0)
        result = client.upload(handle, payload, stamp, call, put=put, wait_seconds=0)
    assert result["status"] == "pending_verification" and not result["content_verified"]
    assert len(attempts) == 2 and len(set(attempts)) == 1 and len(bucket.data) == 1
    assert service.verify_once()
    assert service.status("employee:a", result["receipt_id"])["receipt"]["status"] == "verified"


def test_mutated_file_is_not_uploaded(tmp_path):
    path = tmp_path / "selected.txt"
    path.write_bytes(BODY)
    with path.open("rb") as handle:
        payload, stamp = client.describe_file(handle, path.name, "source:1")
        path.write_bytes(b"replacement")
        def call(name, args):
            return {"ok": True, "receipt": {**payload, "receipt_id": "a" * 32, "status": "awaiting_upload",
                    "content_verified": False, "published": False}, "upload": {"headers": {
                        "x-oss-meta-receipt-id": "a" * 32, "x-oss-meta-sha256": payload["sha256"]}}}
        with pytest.raises(client.UploadError, match="file_changed"):
            client.upload(handle, payload, stamp, call, put=lambda *a: pytest.fail("must not PUT"))


def test_hashing_is_bounded_when_source_keeps_growing(tmp_path):
    path = tmp_path / "growing.txt"
    path.write_bytes(b"x")
    with path.open("rb") as handle:
        class Growing:
            def fileno(self): return handle.fileno()
            def seek(self, offset): return handle.seek(offset)
            def read(self, size):
                assert size <= 2
                return b"x" * size
        with pytest.raises(client.UploadError, match="file_changed"):
            client.describe_file(Growing(), path.name, "source:1")


@pytest.mark.parametrize("url", ["http://private-test.oss-cn-beijing.aliyuncs.com/key",
    "https://attacker.example/key", "https://private-test.oss-cn-beijing.aliyuncs.com@attacker.example/key",
    "https://private-test.oss-cn-beijing.aliyuncs.com:444/key"])
def test_grant_rejects_non_oss_destination(url):
    with pytest.raises(client.UploadError, match="invalid_upload_grant"):
        client.put_original(io.BytesIO(BODY), {"method": "PUT", "url": url, "headers": {},
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}, len(BODY))


def test_put_keeps_signature_private_acl_exact_size_and_disables_redirects(monkeypatch):
    import requests
    seen = []
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def put(self, url, **kwargs):
            assert self.trust_env is False
            assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
            assert kwargs["headers"]["content-length"] == str(len(BODY))
            assert kwargs["data"].read() == BODY
            seen.append(url)
            class Response:
                status_code = 409
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return Response()
    monkeypatch.setattr(requests, "Session", Session)
    url = "https://private-test.oss-cn-beijing.aliyuncs.com/key?x-oss-signature=secret"
    client.put_original(io.BytesIO(BODY), {"method": "PUT", "url": url, "headers": {
        "Content-Type": "application/octet-stream", "Content-Length": str(len(BODY)),
        "x-oss-object-acl": "private", "x-oss-forbid-overwrite": "true"},
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}, len(BODY))
    assert seen == [url]


def test_dry_run_never_loads_hermes_and_reports_no_paths_or_credentials(tmp_path, capsys, monkeypatch):
    path = tmp_path / "internal-private-filename.txt"
    path.write_bytes(BODY)
    monkeypatch.setattr(client, "hermes_client", lambda *a: pytest.fail("dry-run must be offline"))
    assert client.main(["--file", str(path), "--source-id", "synthetic:1"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "dry_run"
    assert str(path) not in output and path.name not in output


def test_mcp_errors_do_not_echo_upstream_secrets():
    with pytest.raises(client.UploadError, match="^mcp_tool_failed$"):
        client.parse_result(SimpleNamespace(isError=True, content=[SimpleNamespace(text="secret-token")]))


@pytest.mark.parametrize("field", ["structuredContent", "structured_content"])
def test_official_sdk_result_names_are_supported(field):
    assert client.parse_result(SimpleNamespace(**{field: {"ok": True, "receipt": {}}})) == {"ok": True, "receipt": {}}
    with pytest.raises(client.UploadError, match="^mcp_tool_failed$"):
        client.parse_result(SimpleNamespace(is_error=True, structured_content={"ok": True, "secret": "fake-secret"}))
    with pytest.raises(client.UploadError, match="^invalid_mcp_result$"):
        client.parse_result(SimpleNamespace(result_type="input_required", structured_content={"ok": True}))


@pytest.mark.skipif(client.os.name != "posix", reason="private files require POSIX ownership and mode checks")
def test_private_environment_reads_only_selected_key_without_expansion(tmp_path, monkeypatch):
    path = tmp_path / "explicit.env"
    path.write_text('IGNORED="never-export-this"\nexport UPLOAD_TEST_TOKEN="fake-token_123"\n', encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv("UPLOAD_TEST_TOKEN", raising=False)
    monkeypatch.delenv("IGNORED", raising=False)
    assert client._http_token("UPLOAD_TEST_TOKEN", path) == "fake-token_123"
    assert "UPLOAD_TEST_TOKEN" not in client.os.environ and "IGNORED" not in client.os.environ
    path.write_text('UPLOAD_TEST_TOKEN=${OTHER_SECRET}\n', encoding="utf-8")
    with pytest.raises(client.UploadError, match="^token_env_missing_or_invalid$"):
        client._http_token("UPLOAD_TEST_TOKEN", path)
    path.write_text('UPLOAD_TEST_TOKEN=one\nUPLOAD_TEST_TOKEN=two\n', encoding="utf-8")
    with pytest.raises(client.UploadError, match="^token_env_missing_or_duplicate$"):
        client._http_token("UPLOAD_TEST_TOKEN", path)
    monkeypatch.setenv("UPLOAD_TEST_TOKEN", "inherited-token")
    assert client._http_token("UPLOAD_TEST_TOKEN", path) == "inherited-token"


@pytest.mark.skipif(client.os.name != "posix", reason="private files require POSIX ownership and mode checks")
def test_private_environment_rejects_readable_files_symlinks_and_windows(tmp_path, monkeypatch):
    path = tmp_path / "explicit.env"
    path.write_text("UPLOAD_TEST_TOKEN=fake-token\n", encoding="utf-8")
    monkeypatch.delenv("UPLOAD_TEST_TOKEN", raising=False)
    path.chmod(0o644)
    with pytest.raises(client.UploadError, match="^unsafe_private_env_file$"):
        client._http_token("UPLOAD_TEST_TOKEN", path)
    path.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(path)
    with pytest.raises(client.UploadError, match="^unsafe_private_env_file$"):
        client._http_token("UPLOAD_TEST_TOKEN", link)
    monkeypatch.setenv("UPLOAD_TEST_TOKEN", "inherited-token")
    with monkeypatch.context() as windows:
        windows.setattr(client.os, "name", "nt")
        assert client._http_token("UPLOAD_TEST_TOKEN") == "inherited-token"
        with pytest.raises(client.UploadError, match="^private_env_file_unsupported$"):
            client._http_token("UPLOAD_TEST_TOKEN", path)


@pytest.mark.parametrize("token", ["", "Bearer secret", "line\nbreak", "${OTHER}", "$(command)"])
def test_environment_token_cannot_inject_headers_or_commands(monkeypatch, token):
    monkeypatch.setenv("UPLOAD_TEST_TOKEN", token)
    with pytest.raises(client.UploadError, match="^token_env_missing_or_invalid$"):
        client._http_token("UPLOAD_TEST_TOKEN")


@pytest.mark.parametrize("url", [None, "http://example.test/mcp", "https://user:secret@example.test/mcp",
    "https://example.test:0/mcp", "https://example.test/mcp#secret", "https://example.test/mcp?token=secret",
    "https://example.test\\@other.test/mcp", "https://example.test/mcp\n"])
def test_http_mcp_rejects_unsafe_urls_before_loading_credentials(monkeypatch, url):
    monkeypatch.setattr(client, "_http_token", lambda *args: pytest.fail("must validate URL first"))
    with pytest.raises(client.UploadError, match="^invalid_mcp_url$"):
        with client.http_mcp_client(url, "UPLOAD_TEST_TOKEN"):
            pytest.fail("unsafe endpoint connected")


@pytest.mark.parametrize("version,http_module,timeout", [
    ("1.26.0", "httpx", timedelta(seconds=35)), ("2.0.0", "httpx2", 35.0)])
def test_official_sdk_dependency_and_timeout_branch(monkeypatch, version, http_module, timeout):
    monkeypatch.setattr(client.metadata, "version", lambda name: version)
    imported = []
    def module(name):
        imported.append(name)
        return SimpleNamespace(ClientSession="session", streamable_http_client="transport", PaginatedRequestParams="params")
    monkeypatch.setattr(client.importlib, "import_module", module)
    result = client._http_sdk()
    assert imported == [http_module, "mcp", "mcp.client.streamable_http", "mcp.types"]
    assert result[1:] == ("session", "transport", timeout, "params")


@pytest.mark.parametrize("version", ["1.25.0", "3.0.0", "unrecognised"])
def test_http_mcp_unsupported_sdk_fails_closed(monkeypatch, version):
    monkeypatch.setattr(client.metadata, "version", lambda name: version)
    with pytest.raises(client.UploadError, match="^http_mcp_sdk_unsupported$"):
        client._http_sdk()


def test_http_mcp_missing_runtime_has_fixed_error(monkeypatch):
    def absent(name):
        raise client.metadata.PackageNotFoundError("fake-private-detail")
    monkeypatch.setattr(client.metadata, "version", absent)
    with pytest.raises(client.UploadError, match="^http_mcp_runtime_missing$"):
        client._http_sdk()


@pytest.fixture
def fake_http_mcp(monkeypatch):
    from contextlib import asynccontextmanager
    import asyncio
    import logging
    import sys
    state = {"calls": [], "pages": [], "closed": [], "major": 1, "target": "https://example.test:443/mcp"}

    class HttpClient:
        def __init__(self, **options):
            state["options"] = options
        async def __aenter__(self): return self
        async def __aexit__(self, *args): state["closed"].append("http")

    @asynccontextmanager
    async def transport(url, *, http_client):
        assert url == "https://example.test/mcp"
        owner = asyncio.current_task()
        await state["options"]["event_hooks"]["request"][0](SimpleNamespace(url=state["target"]))
        try:
            yield ("reader", "writer", "session-id") if state["major"] == 1 else ("reader", "writer")
        finally:
            assert asyncio.current_task() is owner
            state["closed"].append("transport")

    class Session:
        def __init__(self, reader, writer, *, read_timeout_seconds):
            assert (reader, writer) == ("reader", "writer")
            assert read_timeout_seconds == (timedelta(seconds=35) if state["major"] == 1 else 35.0)
        async def __aenter__(self):
            self.owner = asyncio.current_task()
            return self
        async def __aexit__(self, *args):
            assert asyncio.current_task() is self.owner
            state["closed"].append("session")
        async def initialize(self): pass
        async def list_tools(self, *, params):
            state["pages"].append(params.cursor)
            names = [client.PREPARE] if params.cursor is None else [client.COMPLETE, client.STATUS]
            cursor_name = "nextCursor" if state["major"] == 1 else "next_cursor"
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names], **{cursor_name: "next" if params.cursor is None else None})
        async def call_tool(self, name, arguments):
            state["calls"].append((name, arguments))
            if state.get("reflect_secret"):
                logging.critical("fake-token-private")
                print("fake-token-private")
                print("fake-token-private", file=sys.stderr)
                raise RuntimeError("fake-token-private")
            result_name = "structuredContent" if state["major"] == 1 else "structured_content"
            return SimpleNamespace(**{result_name: {"ok": True, "receipt": {}}})

    monkeypatch.setenv("UPLOAD_TEST_TOKEN", "fake-token-private")
    monkeypatch.setattr(client, "_http_sdk", lambda: (SimpleNamespace(AsyncClient=HttpClient), Session, transport,
        timedelta(seconds=35) if state["major"] == 1 else 35.0, SimpleNamespace))
    return state


@pytest.mark.parametrize("major", [1, 2])
def test_http_mcp_uses_existing_identity_safe_transport_and_paginated_tools(fake_http_mcp, major):
    state = fake_http_mcp
    state["major"] = major
    with client.http_mcp_client("https://example.test/mcp", "UPLOAD_TEST_TOKEN") as call:
        assert call(client.STATUS, {"receipt_id": "a" * 32}) == {"ok": True, "receipt": {}}
        with pytest.raises(client.UploadError, match="^unsupported_tool$"):
            call("other_tool", {})
    options = state["options"]
    assert options["verify"] is True and options["trust_env"] is False and options["follow_redirects"] is False
    assert options["headers"] == {"Authorization": "Bearer fake-token-private"}
    assert state["pages"] == [None, "next"]
    assert state["closed"] == ["session", "transport", "http"] and len(state["calls"]) == 1


@pytest.mark.parametrize("destination", ["https://other.test/mcp", "https://example.test:444/mcp", "http://example.test/mcp"])
def test_http_mcp_guards_every_request_origin(fake_http_mcp, destination):
    fake_http_mcp["target"] = destination
    with pytest.raises(client.UploadError):
        with client.http_mcp_client("https://example.test/mcp", "UPLOAD_TEST_TOKEN"):
            pytest.fail("must not forward authentication")
    assert not fake_http_mcp["calls"]


def test_http_mcp_suppresses_reflected_credentials_and_restores_logging(fake_http_mcp, capsys):
    import logging
    previous = logging.root.manager.disable
    fake_http_mcp["reflect_secret"] = True
    with client.http_mcp_client("https://example.test/mcp", "UPLOAD_TEST_TOKEN") as call:
        with pytest.raises(client.UploadError, match="^http_mcp_call_failed$"):
            call(client.STATUS, {})
    output = capsys.readouterr()
    assert "fake-token" not in output.out + output.err
    assert logging.root.manager.disable == previous


def test_http_cli_dry_run_never_reads_credentials(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fixture.txt"
    path.write_bytes(BODY)
    monkeypatch.setattr(client, "_http_token", lambda *args: pytest.fail("dry-run cannot read credentials"))
    assert client.main(["--file", str(path), "--source-id", "source:1", "--transport", "http"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
