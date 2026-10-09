#!/usr/bin/env python3
"""Upload one selected original through Hermes or an existing HTTP MCP identity.

Dry-run by default. Repeating the same source/file bytes reuses the same receipt.
No conversation discovery, cleaning, indexing, or publication happens here.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import urlsplit


PREPARE = "prepare_company_knowledge_upload"
COMPLETE = "complete_company_knowledge_upload"
STATUS = "get_company_knowledge_upload_status"
TOOLS = {PREPARE, COMPLETE, STATUS}
MAX_BYTES = 512 * 1024 * 1024


class UploadError(Exception):
    """Only fixed, non-sensitive error codes may be emitted by this client."""


def file_stamp(handle):
    info = os.fstat(handle.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def describe_file(handle, filename, source_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", source_id):
        raise UploadError("invalid_source_id")
    if (not filename or filename != filename.strip() or len(filename) > 200 or filename in {".", ".."}
            or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in filename)):
        raise UploadError("invalid_filename")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_BYTES:
        raise UploadError("invalid_file_size_or_type")
    before = file_stamp(handle)
    digest = hashlib.sha256()
    handle.seek(0)
    length = 0
    while True:
        chunk = handle.read(min(1024 * 1024, info.st_size + 1 - length))
        if not chunk:
            break
        length += len(chunk)
        if length > info.st_size:
            raise UploadError("file_changed")
        digest.update(chunk)
    if length != info.st_size or file_stamp(handle) != before:
        raise UploadError("file_changed")
    handle.seek(0)
    payload = dict(source_id=source_id, filename=filename, byte_length=info.st_size, sha256=digest.hexdigest())
    identity = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    payload["idempotency_key"] = "file-" + hashlib.sha256(identity).hexdigest()
    return payload, before


def parse_result(result):
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise UploadError("mcp_tool_failed")
    if getattr(result, "result_type", "complete") != "complete":
        raise UploadError("invalid_mcp_result")
    value = getattr(result, "structuredContent", None)
    if value is None:
        value = getattr(result, "structured_content", None)
    if value is None:
        blocks = getattr(result, "content", [])
        if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
            raise UploadError("invalid_mcp_result")
        try:
            value = json.loads(blocks[0].text)
        except (ValueError, TypeError):
            raise UploadError("invalid_mcp_result") from None
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise UploadError("invalid_mcp_result")
    return value


def _http_origin(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in url
                or not url.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        port = 443 if parsed.port is None else parsed.port
        if not 1 <= port <= 65535:
            raise ValueError()
        return parsed.scheme, parsed.hostname.lower(), port
    except (TypeError, ValueError, AttributeError):
        raise UploadError("invalid_mcp_url") from None


def _http_token(token_env, env_file=None):
    """Read only the named key; never interpolate, source, or mutate an environment."""
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise UploadError("invalid_token_env")
    # chmod does not establish Windows ACL privacy. Use the inherited env there.
    if env_file is not None and (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")):
        raise UploadError("private_env_file_unsupported")
    token = os.environ.get(token_env)
    if token is None and env_file is not None:
        try:
            descriptor = os.open(env_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 65536):
                    raise UploadError("unsafe_private_env_file")
                contents = stream.read(65537)
                if len(contents.encode("utf-8")) > 65536:
                    raise UploadError("unsafe_private_env_file")
                values = []
                for line in contents.splitlines():
                    match = re.fullmatch(r"\s*(?:export\s+)?" + re.escape(token_env) + r"\s*=\s*(.*?)\s*", line)
                    if match:
                        value = match[1]
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                            value = value[1:-1]
                        values.append(value)
                if len(values) != 1:
                    raise UploadError("token_env_missing_or_duplicate")
                token = values[0]
        except (OSError, UnicodeError):
            raise UploadError("unsafe_private_env_file") from None
    if not isinstance(token, str) or not 1 <= len(token) <= 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", token):
        raise UploadError("token_env_missing_or_invalid")
    return token


def _http_sdk():
    """Official SDK 1.26+ and 2.x have different transports and timeout types."""
    try:
        version = re.match(r"(\d+)\.(\d+)(?:\.|$)", metadata.version("mcp"))
        if not version or not (int(version[1]) == 2 or int(version[1]) == 1 and int(version[2]) >= 26):
            raise UploadError("http_mcp_sdk_unsupported")
        major = int(version[1])
        http = importlib.import_module("httpx2" if major == 2 else "httpx")
        session = importlib.import_module("mcp").ClientSession
        transport = importlib.import_module("mcp.client.streamable_http").streamable_http_client
        params = importlib.import_module("mcp.types").PaginatedRequestParams
        return http, session, transport, 35.0 if major == 2 else timedelta(seconds=35), params
    except (ImportError, AttributeError, metadata.PackageNotFoundError):
        raise UploadError("http_mcp_runtime_missing") from None


@contextmanager
def http_mcp_client(url, token_env, *, env_file=None):
    """Use an existing identity with the official SDK, without an Agent dependency.

    Intended for the dedicated uploader process: dependency output is suppressed
    for the context lifetime so credential headers and reflected errors stay private.
    """
    origin = _http_origin(url)
    if urlsplit(url).query:
        raise UploadError("invalid_mcp_url")
    token = _http_token(token_env, env_file)
    previous_logging = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            http, Session, transport, timeout, Params = _http_sdk()
            try:
                from anyio.from_thread import start_blocking_portal
            except ImportError:
                raise UploadError("http_mcp_runtime_missing") from None

            async def same_origin(request):
                if _http_origin(str(request.url)) != origin:
                    raise UploadError("mcp_origin_mismatch")

            @asynccontextmanager
            async def connection():
                async with http.AsyncClient(headers={"Authorization": "Bearer " + token}, verify=True,
                                            trust_env=False, follow_redirects=False, timeout=30,
                                            event_hooks={"request": [same_origin]}) as client:
                    async with transport(url, http_client=client) as streams:
                        # SDK 1.x adds a session-id accessor; SDK 2.x yields two streams.
                        async with Session(streams[0], streams[1], read_timeout_seconds=timeout) as session:
                            async def discover():
                                await session.initialize()
                                discovered, cursor, seen = set(), None, set()
                                for _ in range(32):
                                    page = await session.list_tools(params=Params(cursor=cursor))
                                    discovered.update(tool.name for tool in page.tools)
                                    if TOOLS <= discovered:
                                        return
                                    cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
                                    if not cursor or cursor in seen:
                                        break
                                    seen.add(cursor)
                                raise UploadError("upload_tools_not_available")

                            await asyncio.wait_for(discover(), 35)
                            yield session

            # AnyIO keeps transport enter/exit in the same task (SDK cancel scopes).
            with start_blocking_portal() as portal:
                with portal.wrap_async_context_manager(connection()) as session:
                    async def invoke(name, arguments):
                        return await asyncio.wait_for(session.call_tool(name, arguments), 35)

                    def call(name, arguments):
                        if name not in TOOLS:
                            raise UploadError("unsupported_tool")
                        try:
                            return parse_result(portal.call(invoke, name, arguments))
                        except UploadError:
                            raise
                        except Exception:
                            raise UploadError("http_mcp_call_failed") from None

                    yield call
    except UploadError:
        raise
    except Exception:
        raise UploadError("http_mcp_client_failed") from None
    finally:
        logging.disable(previous_logging)


def check_receipt(result, payload, receipt_id=None):
    receipt = result.get("receipt", {})
    if (not isinstance(receipt, dict)
            or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("receipt_id", "")))
            or receipt_id is not None and receipt.get("receipt_id") != receipt_id
            or any(receipt.get(key) != payload[key] for key in ("source_id", "filename", "byte_length", "sha256"))
            or receipt.get("published") is not False
            or receipt.get("status") not in {"awaiting_upload", "pending_verification", "verifying", "verified", "rejected", "failed"}
            or receipt.get("content_verified") is not (receipt.get("status") == "verified")):
        raise UploadError("receipt_contract_error")
    return receipt


def put_original(handle, grant, byte_length):
    try:
        url = grant["url"]
        parsed = urlsplit(url)
        headers = {key.lower(): value for key, value in grant["headers"].items()}
        expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
        valid = (
            grant["method"] == "PUT" and parsed.scheme == "https"
            and not parsed.username and not parsed.password and not parsed.fragment
            and parsed.port in (None, 443) and parsed.path not in ("", "/")
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}\.oss-[a-z0-9-]+\.aliyuncs\.com", parsed.hostname or "")
            and len(headers) == len(grant["headers"])
            and set(headers) <= {"content-type", "content-length", "x-oss-object-acl", "x-oss-forbid-overwrite", "x-oss-security-token", "x-oss-meta-receipt-id", "x-oss-meta-sha256"}
            and all(isinstance(v, str) and not any(c in v for c in "\r\n") for v in headers.values())
            and headers.get("content-length") == str(byte_length)
            and headers.get("content-type") == "application/octet-stream"
            and headers.get("x-oss-object-acl") == "private"
            and headers.get("x-oss-forbid-overwrite") == "true"
            and expires > datetime.now(timezone.utc)
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise UploadError("invalid_upload_grant")
    import requests
    # Signed credentials go only to the validated OSS URL, never into logs.
    with requests.Session() as session:
        session.trust_env = False
        handle.seek(0)
        with session.put(url, data=handle, headers=headers, allow_redirects=False,
                         timeout=(10, 60), stream=True) as response:
            # A previous attempt may already have PUT the immutable object.
            # complete + independent hash verification decide whether it is valid.
            if response.status_code not in {200, 201, 204, 409}:
                raise UploadError("oss_upload_failed")


@contextmanager
def hermes_client(root, server_name):
    """Use Hermes' existing resolved configuration without writing it."""
    if not (root / "hermes_cli" / "mcp_config.py").is_file():
        raise UploadError("hermes_runtime_missing")
    sys.path.insert(0, str(root))
    from hermes_cli.mcp_config import _get_mcp_servers, _resolve_mcp_server_config
    from tools.mcp_tool import _connect_server, _ensure_mcp_loop, _run_on_mcp_loop, _stop_mcp_loop_if_idle

    config = _get_mcp_servers().get(server_name)
    if not isinstance(config, dict) or config.get("enabled") is False:
        raise UploadError("hermes_mcp_not_configured")
    config = dict(_resolve_mcp_server_config(config))
    parsed = urlsplit(config.get("url", ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UploadError("hermes_mcp_requires_https")
    if config.get("ssl_verify", True) is not True:
        raise UploadError("hermes_mcp_requires_tls_verification")
    config["sampling"] = {"enabled": False}
    config["elicitation"] = {"enabled": False}
    _ensure_mcp_loop()
    server = None
    try:
        server = _run_on_mcp_loop(asyncio.wait_for(_connect_server(server_name, config), 30), timeout=35)
        if not TOOLS <= {tool.name for tool in server._tools}:
            raise UploadError("upload_tools_not_available")

        def call(name, arguments):
            if name not in TOOLS:
                raise UploadError("unsupported_tool")
            async def invoke():
                async with server._rpc_lock:
                    return await asyncio.wait_for(server.session.call_tool(name, arguments), 35)
            return parse_result(_run_on_mcp_loop(invoke(), timeout=40))

        yield call
    finally:
        try:
            if server is not None:
                _run_on_mcp_loop(server.shutdown(), timeout=15)
        finally:
            _stop_mcp_loop_if_idle()


def upload(handle, payload, stamp, call, *, put=put_original, wait_seconds=60):
    result = call(PREPARE, payload)
    receipt = check_receipt(result, payload)
    receipt_id = receipt["receipt_id"]
    if receipt["status"] == "awaiting_upload":
        grant = result.get("upload")
        headers = {key.lower(): value for key, value in grant.get("headers", {}).items()} if isinstance(grant, dict) else {}
        if (headers.get("x-oss-meta-receipt-id") != receipt_id
                or headers.get("x-oss-meta-sha256") != payload["sha256"]):
            raise UploadError("receipt_grant_mismatch")
        if file_stamp(handle) != stamp:
            raise UploadError("file_changed")
        put(handle, grant, payload["byte_length"])
        if file_stamp(handle) != stamp:
            raise UploadError("file_changed")
        receipt = check_receipt(call(COMPLETE, {"receipt_id": receipt_id}), payload, receipt_id)
    elif receipt["status"] == "failed" and receipt.get("retryable") is True:
        receipt = check_receipt(call(COMPLETE, {"receipt_id": receipt_id}), payload, receipt_id)
    deadline = time.monotonic() + max(0, wait_seconds)
    while receipt["status"] in {"pending_verification", "verifying"} and time.monotonic() < deadline:
        time.sleep(min(2, max(0, deadline - time.monotonic())))
        receipt = check_receipt(call(STATUS, {"receipt_id": receipt_id}), payload, receipt_id)
    # No filename, source, URL, credential headers, or untrusted error text in output.
    return {key: receipt[key] for key in ("receipt_id", "status", "content_verified", "published")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--server", default="company-mcp")
    parser.add_argument("--hermes-root", type=Path, default=Path.home() / ".hermes/hermes-agent")
    parser.add_argument("--transport", choices=("hermes", "http"), default="hermes")
    parser.add_argument("--mcp-url")
    parser.add_argument("--token-env", default="COMPANY_MCP_TOKEN")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=60, choices=range(0, 301), metavar="0..300")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        with args.file.open("rb") as handle:
            payload, stamp = describe_file(handle, args.file.name, args.source_id)
            if not args.execute:
                print(json.dumps({"ok": True, "status": "dry_run", "byte_length": payload["byte_length"],
                                  "sha256": payload["sha256"], "published": False}))
                return 0
            # Native discovery may log authentication details. This is a dedicated
            # CLI process: suppress dependency output, return only our safe result.
            logging.disable(logging.CRITICAL)
            with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                client = (http_mcp_client(args.mcp_url, args.token_env, env_file=args.env_file)
                          if args.transport == "http" else hermes_client(args.hermes_root, args.server))
                with client as call:
                    result = upload(handle, payload, stamp, call, wait_seconds=args.wait_seconds)
            print(json.dumps({"ok": result["content_verified"], **result}))
            return 0 if result["content_verified"] else 2
    except UploadError as exc:
        print(json.dumps({"ok": False, "error_code": str(exc)}), file=sys.stderr)
    except Exception:
        print(json.dumps({"ok": False, "error_code": "upload_client_failed"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
