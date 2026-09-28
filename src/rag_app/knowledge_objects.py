"""Private, content-addressed company knowledge bodies in OSS."""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import urlsplit


MAX_CHARACTERS = 500_000
MAX_BYTES = 2_000_000
DEFAULT_PREFIX = "company-knowledge/bodies/"


class KnowledgeObjectError(RuntimeError):
    """Safe to report without SDK responses, credentials or object contents."""


class KnowledgeObjects:
    def __init__(self, bucket=None, *, prefix=None):
        prefix = prefix if prefix is not None else os.getenv("KNOWLEDGE_OSS_PREFIX", DEFAULT_PREFIX)
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", prefix):
            raise ValueError("invalid company knowledge object prefix")
        self.prefix = prefix.rstrip("/") + "/"
        if bucket is not None:
            self.bucket = bucket
            return
        config = {key: (os.getenv("KNOWLEDGE_OSS_" + key) or os.getenv("OSS_" + key) or "").strip()
                  for key in ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "ACCESS_KEY_SECRET")}
        if not all(config.values()):
            raise KnowledgeObjectError("company knowledge OSS configuration is incomplete")
        try:
            raw_endpoint = config["ENDPOINT"]
            if any(character.isspace() for character in raw_endpoint):
                raise ValueError
            endpoint = urlsplit(raw_endpoint if "://" in raw_endpoint else "https://" + raw_endpoint)
            hostname = endpoint.hostname or ""
            label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
            if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                    or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
                    or len(hostname) > 253 or not re.fullmatch(label + r"(?:\." + label + r")*", hostname)
                    or endpoint.netloc.endswith(":") or endpoint.port == 0):
                raise ValueError
            normalized_endpoint = "https://" + hostname + (f":{endpoint.port}" if endpoint.port is not None else "")
            import oss2

            self.bucket = oss2.Bucket(
                oss2.Auth(config["ACCESS_KEY_ID"], config["ACCESS_KEY_SECRET"]),
                normalized_endpoint, config["BUCKET"], connect_timeout=30,
            )
        except Exception:
            raise KnowledgeObjectError("company knowledge OSS requires a valid HTTPS configuration") from None

    def _reference(self, ref):
        if not isinstance(ref, dict) or set(ref) != {"object_key", "sha256", "byte_length"}:
            raise ValueError("invalid company knowledge object reference")
        digest, length = ref["sha256"], ref["byte_length"]
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(length) is not int or not 1 <= length <= MAX_BYTES
                or ref["object_key"] != self.prefix + digest + ".txt"):
            raise ValueError("invalid company knowledge object reference")
        return ref["object_key"]

    def put(self, content: str) -> dict:
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_CHARACTERS:
            raise ValueError("knowledge body must contain 1-500000 characters")
        try:
            data = content.encode("utf-8")
        except UnicodeError:
            raise ValueError("knowledge body must be valid UTF-8") from None
        if len(data) > MAX_BYTES:
            raise ValueError("knowledge body exceeds the byte limit")
        digest = hashlib.sha256(data).hexdigest()
        ref = {"object_key": self.prefix + digest + ".txt", "sha256": digest, "byte_length": len(data)}
        try:
            if not self.bucket.object_exists(ref["object_key"]):
                try:
                    self.bucket.put_object(ref["object_key"], data, headers={
                        "Content-Type": "text/plain; charset=utf-8",
                        "x-oss-object-acl": "private",
                        "x-oss-forbid-overwrite": "true",
                    })
                except Exception as exc:
                    # Another writer may have stored the same content after HEAD.
                    if getattr(exc, "status", None) != 409 or getattr(exc, "code", None) != "FileAlreadyExists":
                        raise
            self.get(ref)
        except Exception:
            raise KnowledgeObjectError("company knowledge body could not be stored and verified") from None
        return ref

    def get(self, ref: dict) -> str:
        key = self._reference(ref)
        try:
            if self.bucket.get_object_acl(key).acl != "private":
                raise KnowledgeObjectError("company knowledge body is not private")
            with self.bucket.get_object(key) as response:
                # Stream reads may be short; read to EOF or one byte beyond the
                # declared length, never buffering an unbounded remote object.
                remaining = ref["byte_length"] + 1
                parts = []
                while remaining:
                    block = response.read(min(65_536, remaining))
                    if not block:
                        break
                    if not isinstance(block, bytes) or len(block) > remaining:
                        raise KnowledgeObjectError("invalid company knowledge body response")
                    parts.append(block)
                    remaining -= len(block)
                data = b"".join(parts)
            if len(data) != ref["byte_length"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise KnowledgeObjectError("company knowledge body failed integrity verification")
            content = data.decode("utf-8")
            if not content.strip() or len(content) > MAX_CHARACTERS:
                raise KnowledgeObjectError("invalid company knowledge body length")
            return content
        except Exception:
            raise KnowledgeObjectError("company knowledge body could not be read and verified") from None
