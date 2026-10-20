"""Private, immutable file objects; only stored revision descriptors may be signed."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from .knowledge_objects import KnowledgeObjects
from .knowledge_text import MAX_EXPANDED_BYTES, MAX_FILE_BYTES, clean_text, credential_name


DEFAULT_PREFIX = "company-knowledge/files/"
MEDIA_TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               ".pdf": "application/pdf", ".zip": "application/zip", ".md": "text/markdown",
               ".txt": "text/plain", ".html": "text/html", ".json": "application/json",
               ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class KnowledgeFileError(RuntimeError):
    """Fixed messages only: do not expose SDK errors or signed URLs."""


def _prefix(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", value):
        raise ValueError("invalid knowledge file prefix")
    return value.rstrip("/") + "/"


def _filename(value):
    try:
        length = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeError:
        raise ValueError("invalid knowledge filename") from None
    if (not isinstance(value, str) or not value.strip() or length > 255
            or value in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in value)):
        raise ValueError("invalid knowledge filename")
    return value


def _file_id(purpose, filename, digest):
    return "file_" + hashlib.sha256((purpose + "\0" + filename + "\0" + digest).encode("utf-8")).hexdigest()[:32]


def validate_file_descriptor(ref, *, prefix=DEFAULT_PREFIX):
    prefix = _prefix(prefix)
    fields = {"file_id", "filename", "media_type", "byte_length", "sha256", "purpose", "object_ref"}
    if not isinstance(ref, dict) or set(ref) != fields:
        raise ValueError("invalid knowledge file reference")
    filename = _filename(ref["filename"])
    digest, size, purpose, media = ref["sha256"], ref["byte_length"], ref["purpose"], ref["media_type"]
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or not 1 <= size <= MAX_FILE_BYTES
            or not isinstance(purpose, str) or purpose not in {"original", "preview", "skill"}
            or not isinstance(media, str) or media not in {*MEDIA_TYPES.values(), "application/octet-stream"}
            or purpose == "preview" and media != "text/html"
            or purpose == "skill" and media != "application/zip"
            or ref["file_id"] != _file_id(purpose, filename, digest)
            or ref["object_ref"] != {"object_key": prefix + purpose + "/" + digest,
                                     "sha256": digest, "byte_length": size}):
        raise ValueError("invalid knowledge file reference")
    return ref


def public_file_descriptor(ref, *, prefix=DEFAULT_PREFIX):
    return {key: value for key, value in validate_file_descriptor(ref, prefix=prefix).items() if key != "object_ref"}


def validate_skill_package(data: bytes) -> dict:
    """Verify packaged paths and Markdown dependencies without executing a skill."""
    from .knowledge_intake import _references, _resolve_reference, _safe_members

    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_FILE_BYTES:
        raise ValueError("invalid skill package size")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = _safe_members(archive)
            if not infos or len(infos) > 10000 or sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
                raise ValueError("skill package expansion limit")
            documents, files = {}, []
            for item in infos:
                name = item.filename.replace("\\", "/")
                if (name != str(PurePosixPath(name)) or item.flag_bits & 1 or item.file_size > MAX_FILE_BYTES
                        or any(credential_name(part) for part in PurePosixPath(name).parts)
                        or PurePosixPath(name).suffix.lower() in {".zip", ".7z", ".rar", ".gz"}):
                    raise ValueError("unsafe skill package member")
                raw = archive.read(item)
                text = raw.decode("utf-8", errors="replace")
                _, redactions = clean_text(text)
                if set(redactions) & {"private_key", "api_key", "bearer_token", "credential_assignment", "url_credentials"}:
                    raise ValueError("skill package contains credential candidates")
                documents[name] = text
                files.append({"path": name, "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
            entrypoints = [name for name in documents if PurePosixPath(name).name == "SKILL.md"]
            if len(entrypoints) != 1 or len(PurePosixPath(entrypoints[0]).parts) > 2 or not documents[entrypoints[0]].strip():
                raise ValueError("skill package requires one SKILL.md")
            entrypoint = entrypoints[0]
            root = posixpath.dirname(entrypoint)
            if root and any(not name.startswith(root + "/") for name in documents):
                raise ValueError("skill package files outside skill root")
            references = 0
            for name, text in documents.items():
                if PurePosixPath(name).suffix.lower() not in {".md", ".txt"}:
                    continue
                for reference in _references(text):
                    if reference.startswith("#") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", reference):
                        continue
                    target = _resolve_reference(name, reference, documents)
                    if target is None or root and not target.startswith(root + "/"):
                        raise ValueError("skill package local dependency missing")
                    references += 1
            return {"entrypoint": entrypoint, "files": files, "local_references_checked": references,
                    "dependency_scope": "packaged_markdown_links", "execution_verified": False}
    except ValueError:
        raise
    except Exception:
        raise ValueError("invalid skill package archive") from None


class KnowledgeFiles:
    def __init__(self, bucket=None, *, prefix=None):
        self.prefix = _prefix(prefix if prefix is not None else DEFAULT_PREFIX)
        self.bucket = bucket if bucket is not None else KnowledgeObjects().bucket

    def put(self, data: bytes, filename: str, purpose="original", media_type=None) -> dict:
        filename = _filename(filename)
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_FILE_BYTES:
            raise ValueError("knowledge file size limit")
        if not isinstance(purpose, str) or purpose not in {"original", "preview", "skill"}:
            raise ValueError("invalid knowledge file purpose")
        if purpose == "skill":
            validate_skill_package(data)
        digest = hashlib.sha256(data).hexdigest()
        ref = {"file_id": _file_id(purpose, filename, digest), "filename": filename,
               "media_type": media_type or MEDIA_TYPES.get(PurePosixPath(filename).suffix.lower(), "application/octet-stream"),
               "byte_length": len(data), "sha256": digest, "purpose": purpose,
               "object_ref": {"object_key": self.prefix + str(purpose) + "/" + digest,
                              "sha256": digest, "byte_length": len(data)}}
        validate_file_descriptor(ref, prefix=self.prefix)
        try:
            # OSS forbid-overwrite is ignored by versioned buckets.
            if hasattr(self.bucket, "get_bucket_versioning") and self.bucket.get_bucket_versioning().status not in (None, "", "Disabled"):
                raise KnowledgeFileError("knowledge file bucket versioning unsupported")
            key = ref["object_ref"]["object_key"]
            if not self.bucket.object_exists(key):
                try:
                    self.bucket.put_object(key, data, headers={
                        "Content-Type": ref["media_type"], "x-oss-object-acl": "private",
                        "x-oss-forbid-overwrite": "true", "x-oss-meta-sha256": digest,
                        "Content-Disposition": "attachment", "Cache-Control": "private, no-store",
                    })
                except Exception as exc:
                    if getattr(exc, "status", None) != 409 or getattr(exc, "code", None) != "FileAlreadyExists":
                        raise
            self.get(ref)
        except Exception:
            raise KnowledgeFileError("knowledge file could not be stored and verified") from None
        return ref

    def get(self, ref: dict) -> bytes:
        validate_file_descriptor(ref, prefix=self.prefix)
        try:
            key = ref["object_ref"]["object_key"]
            if self.bucket.get_object_acl(key).acl != "private":
                raise KnowledgeFileError("knowledge file is not private")
            with self.bucket.get_object(key) as response:
                remaining, parts = ref["byte_length"] + 1, []
                while remaining:
                    block = response.read(min(65536, remaining))
                    if not block:
                        break
                    if not isinstance(block, bytes) or len(block) > remaining:
                        raise KnowledgeFileError("invalid knowledge file response")
                    parts.append(block)
                    remaining -= len(block)
            data = b"".join(parts)
            if len(data) != ref["byte_length"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise KnowledgeFileError("knowledge file integrity check failed")
            return data
        except Exception:
            raise KnowledgeFileError("knowledge file could not be read and verified") from None

    def signed_url(self, ref: dict, disposition="attachment", expires=300) -> dict:
        validate_file_descriptor(ref, prefix=self.prefix)
        if (disposition not in {"attachment", "inline"} or type(expires) is not int or not 1 <= expires <= 900
                or disposition == "inline" and ref["purpose"] != "preview"):
            raise ValueError("invalid knowledge file download options")
        self.get(ref)
        try:
            url = self.bucket.sign_url("GET", ref["object_ref"]["object_key"], expires, params={
                "response-content-disposition": disposition + "; filename=\"download\"; filename*=UTF-8''" + quote(ref["filename"], safe=""),
                "response-cache-control": "private, no-store",
            })
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise KnowledgeFileError("invalid knowledge file signed URL")
        except Exception:
            raise KnowledgeFileError("knowledge file download unavailable") from None
        return {"url": url, "expires_in": expires}
