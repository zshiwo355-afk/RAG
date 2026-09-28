import hashlib
import io
import sys
from types import SimpleNamespace

import pytest

from rag_app.knowledge_objects import KnowledgeObjectError, KnowledgeObjects, MAX_BYTES


pytestmark = pytest.mark.offline


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.acls = {}
        self.writes = []
        self.reads = []
        self.fail_write = False

    def object_exists(self, key):
        return key in self.objects

    def put_object(self, key, data, headers):
        if self.fail_write:
            raise RuntimeError("secret-token-and-internal-url")
        self.writes.append((key, headers))
        self.objects[key] = data
        self.acls[key] = headers["x-oss-object-acl"]

    def get_object_acl(self, key):
        return SimpleNamespace(acl=self.acls[key])

    def get_object(self, key):
        bucket = self

        class ShortReads(io.BytesIO):
            def read(self, size):
                bucket.reads.append(size)
                return super().read(min(size, 17))

        return ShortReads(self.objects[key])


def raw_reference(store, bucket, data):
    digest = hashlib.sha256(data).hexdigest()
    key = store.prefix + digest + ".txt"
    bucket.objects[key], bucket.acls[key] = data, "private"
    return {"object_key": key, "sha256": digest, "byte_length": len(data)}


def test_content_addressed_private_roundtrip_is_idempotent():
    bucket = FakeBucket()
    store = KnowledgeObjects(bucket)
    content = "# 完整方法\n\n先保留失败原因，再说明适用边界。" * 15
    ref = store.put(content)
    assert ref == store.put(content)
    assert store.get(ref) == content
    assert len(bucket.writes) == 1
    assert bucket.writes[0][1]["x-oss-forbid-overwrite"] == "true"
    assert bucket.acls[ref["object_key"]] == "private"
    assert max(bucket.reads) <= ref["byte_length"] + 1


@pytest.mark.parametrize("change", [
    {"object_key": "../outside.txt"}, {"object_key": "company-knowledge/bodies/other.txt"},
    {"object_key": "https://example.test/object"}, {"sha256": "z" * 64},
    {"byte_length": True}, {"byte_length": MAX_BYTES + 1}, {"url": "https://example.test"},
])
def test_forged_references_fail_before_read(change):
    bucket = FakeBucket()
    store = KnowledgeObjects(bucket)
    ref = raw_reference(store, bucket, b"body")
    with pytest.raises(ValueError, match="reference"):
        store.get({**ref, **change})
    assert bucket.reads == []


@pytest.mark.parametrize("acl", ["public-read", "public-read-write", "default"])
def test_existing_nonprivate_object_is_never_changed_or_reused(acl):
    bucket = FakeBucket()
    store = KnowledgeObjects(bucket)
    ref = raw_reference(store, bucket, b"body")
    bucket.acls[ref["object_key"]] = acl
    with pytest.raises(KnowledgeObjectError):
        store.put("body")
    assert bucket.writes == [] and bucket.acls[ref["object_key"]] == acl


def test_tampered_missing_and_oversized_objects_are_rejected():
    bucket = FakeBucket()
    store = KnowledgeObjects(bucket)
    ref = raw_reference(store, bucket, b"body")
    for data in (b"fake", b"b", b"body" * 100_000):
        bucket.objects[ref["object_key"]] = data
        with pytest.raises(KnowledgeObjectError):
            store.get(ref)
    assert max(bucket.reads) <= 5
    del bucket.objects[ref["object_key"]]
    with pytest.raises(KnowledgeObjectError):
        store.get(ref)


@pytest.mark.parametrize("data", [b"\xff", b" " * 5, b"a" * 500_001])
def test_matching_hash_does_not_bypass_utf8_and_character_limits(data):
    bucket = FakeBucket()
    store = KnowledgeObjects(bucket)
    with pytest.raises(KnowledgeObjectError):
        store.get(raw_reference(store, bucket, data))


@pytest.mark.parametrize("content", [None, "", "  ", "a" * 500_001, "\ud800"])
def test_invalid_body_never_uploads(content):
    bucket = FakeBucket()
    with pytest.raises(ValueError):
        KnowledgeObjects(bucket).put(content)
    assert bucket.writes == []


def test_upload_failure_is_sanitized_and_returns_no_reference():
    bucket = FakeBucket()
    bucket.fail_write = True
    with pytest.raises(KnowledgeObjectError) as error:
        KnowledgeObjects(bucket).put("body")
    assert "secret" not in str(error.value)
    assert bucket.objects == {}


def test_racing_writer_is_verified_after_forbidden_overwrite():
    bucket = FakeBucket()

    class AlreadyExists(Exception):
        status, code = 409, "FileAlreadyExists"

    def raced_put(key, data, headers):
        bucket.objects[key], bucket.acls[key] = data, "private"
        raise AlreadyExists

    bucket.put_object = raced_put
    store = KnowledgeObjects(bucket)
    ref = store.put("another writer stored this body")
    assert store.get(ref) == "another writer stored this body"


@pytest.mark.parametrize("endpoint", [
    "http://unsafe.test", "https://user:secret@host.test", "host.test/path", "host.test?query=1",
    "host.test#fragment", "host.test:abc", "host.test:65536", "host.test:0", "host.test:",
    "host.test\nother", "-host.test", "host..test",
])
def test_endpoint_requires_https_and_env_values_never_leak(monkeypatch, endpoint):
    for key, value in {"ENDPOINT": endpoint, "BUCKET": "private-bucket",
                       "ACCESS_KEY_ID": "secret-id", "ACCESS_KEY_SECRET": "secret-token"}.items():
        monkeypatch.setenv("KNOWLEDGE_OSS_" + key, value)
    with pytest.raises(KnowledgeObjectError) as error:
        KnowledgeObjects()
    assert "HTTPS" in str(error.value) and "secret" not in str(error.value)


def test_existing_bare_oss_endpoint_is_normalized_to_https(monkeypatch):
    created = {}
    bucket = FakeBucket()

    def make_bucket(auth, endpoint, bucket_name, **kwargs):
        created.update(endpoint=endpoint, bucket_name=bucket_name, auth=auth)
        return bucket

    monkeypatch.setitem(sys.modules, "oss2", SimpleNamespace(Auth=lambda *args: args, Bucket=make_bucket))
    for key, value in {"ENDPOINT": "oss-cn-beijing.aliyuncs.com", "BUCKET": "private-bucket",
                       "ACCESS_KEY_ID": "test-id", "ACCESS_KEY_SECRET": "test-secret"}.items():
        monkeypatch.delenv("KNOWLEDGE_OSS_" + key, raising=False)
        monkeypatch.setenv("OSS_" + key, value)
    assert KnowledgeObjects().bucket is bucket
    assert created == {"endpoint": "https://oss-cn-beijing.aliyuncs.com",
                       "bucket_name": "private-bucket", "auth": ("test-id", "test-secret")}


@pytest.mark.parametrize("prefix", ["", "/outside/", "../", "bodies/../", "bodies//", "bodies/%2f/"])
def test_invalid_prefix_is_rejected(prefix):
    with pytest.raises(ValueError):
        KnowledgeObjects(FakeBucket(), prefix=prefix)
