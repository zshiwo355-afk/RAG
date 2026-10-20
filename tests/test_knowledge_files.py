import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

from rag_app.knowledge_files import KnowledgeFileError, KnowledgeFiles, public_file_descriptor, validate_skill_package
from rag_app.knowledge_text import MAX_FILE_BYTES


pytestmark = pytest.mark.offline


def package(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


class FileBucket:
    def __init__(self):
        self.objects, self.acls, self.writes, self.signatures = {}, {}, [], []
        self.versioning = "Disabled"

    def get_bucket_versioning(self):
        return SimpleNamespace(status=self.versioning)

    def object_exists(self, key):
        return key in self.objects

    def put_object(self, key, data, headers):
        self.objects[key], self.acls[key] = data, headers["x-oss-object-acl"]
        self.writes.append((key, headers))

    def get_object_acl(self, key):
        return SimpleNamespace(acl=self.acls[key])

    def get_object(self, key):
        class ShortRead(io.BytesIO):
            def read(self, length):
                return super().read(min(length, 17))
        return ShortRead(self.objects[key])

    def sign_url(self, method, key, expires, params):
        self.signatures.append((method, key, expires, params))
        return "https://private-oss.example.test/" + key + "?signature=offline"


def test_original_bytes_private_immutable_and_download_hash():
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    original = bytes(range(256)) * 80
    ref = files.put(original, "总经办_岗位.docx")
    assert files.put(original, "总经办_岗位.docx") == ref
    assert len(bucket.writes) == 1
    assert bucket.writes[0][1]["x-oss-forbid-overwrite"] == "true"
    assert files.get(ref) == original
    assert hashlib.sha256(files.get(ref)).hexdigest() == ref["sha256"]
    assert "object_ref" not in public_file_descriptor(ref)
    result = files.signed_url(ref)
    assert result["expires_in"] == 300
    method, key, expires, params = bucket.signatures[-1]
    assert (method, key, expires) == ("GET", ref["object_ref"]["object_key"], 300)
    assert params["response-content-disposition"].startswith("attachment; filename=")
    assert "filename*=UTF-8''%E6" in params["response-content-disposition"]
    assert "response-content-type" not in params  # OSS rejects this response override.
    assert bucket.writes[0][1]["Content-Type"] == ref["media_type"]
    assert params["response-cache-control"] == "private, no-store"
    with pytest.raises(ValueError):
        files.signed_url(ref, "inline")


def test_preview_and_complete_skill_are_separate_objects_and_purposes():
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    method = files.put(b"# Method only", "SKILL.md")
    skill = files.put(package({"SKILL.md": "# Skill\n读取 [检查](references/check.md)", "references/check.md": "核对输入。"}), "skill.zip", purpose="skill")
    preview = files.put(b"<html>safe generated preview</html>", "sop.html", purpose="preview")
    assert len(bucket.objects) == 3
    assert method["purpose"] == "original" and skill["purpose"] == "skill"
    assert files.signed_url(preview, "inline")["url"].startswith("https://")
    assert "response-content-type" not in bucket.signatures[-1][3]
    assert bucket.writes[-1][1]["Content-Type"] == "text/html"
    with pytest.raises(ValueError):
        files.put(b"method", "SKILL.md", purpose="skill")


@pytest.mark.parametrize("field,value", [
    ("object_ref", {"object_key": "../existing-private-file", "sha256": "a" * 64, "byte_length": 1}),
    ("file_id", "file_other"), ("purpose", "other"), ("byte_length", True),
    ("filename", "bad\r\nX-Header: value"), ("media_type", "text/html\r\nX-Header: value"),
])
def test_forged_reference_never_signs(field, value):
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    ref = files.put(b"original", "sop.docx")
    with pytest.raises(ValueError):
        files.signed_url({**ref, field: value})
    assert bucket.signatures == []


@pytest.mark.parametrize("data", [b"wrong", b"o" * 100000, b""])
def test_tampering_or_oversize_never_signs(data):
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    ref = files.put(b"original", "sop.docx")
    bucket.objects[ref["object_ref"]["object_key"]] = data
    with pytest.raises(KnowledgeFileError):
        files.signed_url(ref)
    assert bucket.signatures == []


def test_nonprivate_or_versioned_bucket_never_reuses_or_overwrites():
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    ref = files.put(b"original", "sop.docx")
    key = ref["object_ref"]["object_key"]
    bucket.acls[key] = "public-read"
    with pytest.raises(KnowledgeFileError):
        files.put(b"original", "sop.docx")
    assert bucket.acls[key] == "public-read" and len(bucket.writes) == 1
    bucket.versioning = "Enabled"
    with pytest.raises(KnowledgeFileError):
        files.put(b"another", "sop.docx")
    assert len(bucket.writes) == 1


@pytest.mark.parametrize("filename,data", [("../sop.docx", b"a"), ("sop.docx", b""), ("sop.docx", b"a" * (MAX_FILE_BYTES + 1))])
def test_invalid_input_never_stores(filename, data):
    bucket = FileBucket()
    with pytest.raises(ValueError):
        KnowledgeFiles(bucket).put(data, filename)
    assert bucket.writes == []


def test_errors_never_echo_sdk_details():
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    def fail(*args, **kwargs):
        raise RuntimeError("private-token-and-url")
    bucket.put_object = fail
    with pytest.raises(KnowledgeFileError) as exc:
        files.put(b"original", "sop.docx")
    assert "private-token" not in str(exc.value)


def test_skill_package_checks_dependencies_without_running_scripts():
    data = package({"demo/SKILL.md": "# Skill\n读取 [检查](references/check.md)。运行 `scripts/check.py`。",
                    "demo/references/check.md": "输入检查。", "demo/scripts/check.py": "raise RuntimeError('must not execute')"})
    result = validate_skill_package(data)
    assert result['entrypoint'] == 'demo/SKILL.md'
    assert result['local_references_checked'] == 2
    assert result['execution_verified'] is False and len(result['files']) == 3


@pytest.mark.parametrize('members', [
    {'README.md': '# No skill'},
    {'SKILL.md': 'See [missing](references/check.md)'},
    {'SKILL.md': '# Skill', '../escape.py': 'pass'},
    {'SKILL.md': '# Skill', '.env': 'NOT_A_REAL_SECRET=example'},
    {'SKILL.md': '# Skill', 'scripts/upload.py': 'password="private-example-secret"'},
    {'SKILL.md': '# Skill', 'nested.zip': b'PK unchecked archive'},
    {'demo/SKILL.md': '# Skill', 'outside.md': 'outside'},
])
def test_incomplete_or_unsafe_skill_package_never_stores(members):
    bucket = FileBucket()
    with pytest.raises(ValueError):
        KnowledgeFiles(bucket).put(package(members), 'skill.zip', purpose='skill')
    assert bucket.writes == []
