import json
from types import SimpleNamespace

import pytest
from alibabacloud_ha3engine_vector import models

import cleanup_knowledge_trial as clean
from rag_app.knowledge_index import KnowledgeIndex

pytestmark = pytest.mark.offline
TRIAL = "a" * 32
PREFIX = "company-knowledge/trials/" + TRIAL + "/"


def manifest(tmp_path):
    key = "trial_" + TRIAL[:8] + "_" + "b" * 24
    asset = {"knowledge_id": key, "revision": 1, "content_hash": "c" * 64,
             "revision_key": key + "__r1", "chunk_ids": [key + "__r1__c1"],
             "object_key": PREFIX + "c" * 64 + ".txt", "state": "ready", "preflight_absent": True}
    data = {"trial_id": TRIAL, "prefix": PREFIX, "assets": [asset],
            "created_object_versions": {asset["object_key"]: "created-version"}, "status": "ready"}
    save(tmp_path, data)
    return data


def save(tmp_path, data):
    (tmp_path / "trial.json").write_text(json.dumps(data))


class Client:
    def __init__(self):
        self.deleted, self.fetches, self.queries = [], [], []
        self.fail = False

    def push_documents(self, source, key, request):
        assert source == "company_knowledge" and key == "id"
        assert all(row["cmd"] == "delete" for row in request.body)
        self.deleted.extend(row["fields"]["id"] for row in request.body)
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_VALUE")
        return {"status": "OK"}

    def fetch(self, request):
        self.fetches.append(request.ids)
        return {"result": []}

    def query(self, request):
        self.queries.append(request.filter)
        return {"result": []}


class Missing(Exception):
    code = "NoSuchKey"


class Bucket:
    def __init__(self, mode=None):
        self.deleted, self.heads = [], []
        self.present, self.mode = True, mode

    def get_bucket_versioning(self):
        return SimpleNamespace(status=self.mode)

    def delete_object(self, key, params=None):
        self.deleted.append((key, params))
        self.present = False

    def get_object_meta(self, key, params=None):
        self.heads.append((key, params))
        if not self.present:
            raise Missing()
        return SimpleNamespace()


def sdk():
    cloud = KnowledgeIndex()
    cloud.client, cloud.models, cloud.table = Client(), models, "company_knowledge"
    cloud._datasource = lambda: "company_knowledge"
    cloud._call = lambda operation: operation()
    return cloud, SimpleNamespace(bucket=Bucket())


def no_local(*args):
    pytest.fail("no local processes should be touched")


def test_default_dry_run_does_not_read_runtime_or_construct_clients(tmp_path, capsys):
    manifest(tmp_path)
    (tmp_path / "runtime.json").write_text("PRIVATE_INVALID_JSON")
    assert clean.main(["--trial", str(tmp_path / "trial.json")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert not (tmp_path / "cleanup.json").exists()


def test_active_operation_lock_rejects_execute_before_manifest_or_cloud_changes(tmp_path):
    manifest(tmp_path)
    before = (tmp_path / "trial.json").read_bytes()
    with (tmp_path / "operation.lock").open("a") as lock:
        clean.fcntl.flock(lock, clean.fcntl.LOCK_EX | clean.fcntl.LOCK_NB)
        with pytest.raises(clean.CleanupError, match="trial_operation_in_progress"):
            clean.cleanup(tmp_path, execute=True, cloud=object(), objects=object(), run=no_local)
        assert clean.cleanup(tmp_path)["status"] == "dry_run"
    assert (tmp_path / "trial.json").read_bytes() == before
    assert not (tmp_path / "cleanup.json").exists()
    cloud, objects = sdk()
    assert clean.cleanup(tmp_path, execute=True, cloud=cloud, objects=objects, run=no_local,
                         sleep=lambda _: None)["status"] == "cleaned"


@pytest.mark.parametrize("field,value", [
    ("knowledge_id", "company_real_asset"), ("chunk_ids", ["outside__r1__c1"]),
    ("object_key", "company-knowledge/bodies/" + "c" * 64 + ".txt"),
    ("revision_key", "outside__r1"), ("revision", 2),
])
def test_foreign_target_is_rejected_before_any_mutation(tmp_path, field, value):
    data = manifest(tmp_path)
    data["assets"][0][field] = value
    save(tmp_path, data)
    with pytest.raises(clean.CleanupError):
        clean.cleanup(tmp_path, execute=True, cloud=object(), objects=object(), run=no_local)
    assert not (tmp_path / "cleanup.json").exists()


def test_exact_ids_versions_two_checks_and_retry(tmp_path):
    data = manifest(tmp_path)
    cloud, objects = sdk()
    asset = data["assets"][0]
    result = clean.cleanup(tmp_path, execute=True, cloud=cloud, objects=objects, run=no_local, sleep=lambda _: None)
    assert result["status"] == "cleaned"
    assert cloud.client.deleted == asset["chunk_ids"]
    assert cloud.client.fetches == [asset["chunk_ids"], asset["chunk_ids"]]
    assert cloud.client.queries == [f'(revision_key="{asset["revision_key"]}")'] * 2
    assert objects.bucket.deleted == [(asset["object_key"], {"versionId": "created-version"})]
    assert objects.bucket.heads == objects.bucket.deleted * 2
    assert clean.cleanup(tmp_path, execute=True, cloud=cloud, objects=objects, run=no_local,
                         sleep=lambda _: None)["status"] == "cleaned"


@pytest.mark.parametrize("mode,params", [(None, None), ("Enabled", {"versionId": "null"}),
                                          ("Suspended", {"versionId": "null"})])
def test_null_put_version_uses_current_bucket_versioning(tmp_path, mode, params):
    data = manifest(tmp_path)
    key = data["assets"][0]["object_key"]
    data["created_object_versions"][key] = None
    save(tmp_path, data)
    cloud, objects = sdk()
    objects.bucket.mode = mode
    assert clean.cleanup(tmp_path, execute=True, cloud=cloud, objects=objects, run=no_local,
                         sleep=lambda _: None)["status"] == "cleaned"
    assert objects.bucket.deleted == [(key, params)]


def test_unknown_version_is_not_deleted_and_failed_cloud_keeps_local_resources(tmp_path):
    data = manifest(tmp_path)
    data["created_object_versions"] = {}
    save(tmp_path, data)
    (tmp_path / "runtime.json").write_text(json.dumps({"database_url": "PRIVATE_DATABASE_URL"}))
    cloud, objects = sdk()
    cloud.client.fail = True
    result = clean.cleanup(tmp_path, execute=True, cloud=cloud, objects=objects, run=no_local, sleep=lambda _: None)
    assert result["status"] == "cleanup_failed" and not objects.bucket.deleted
    assert {error["code"] for error in result["errors"]} == {"index_cleanup_failed", "unknown_object_version"}
    assert "PRIVATE_" not in (tmp_path / "cleanup.json").read_text()


def test_no_preflight_or_no_write_is_never_owned(tmp_path):
    data = manifest(tmp_path)
    data["assets"][0]["preflight_absent"] = False
    save(tmp_path, data)
    with pytest.raises(clean.CleanupError, match="unowned_created_object"):
        clean.cleanup(tmp_path, execute=True, cloud=object())
    data["created_object_versions"] = {}
    save(tmp_path, data)
    assert clean.cleanup(tmp_path)["assets"] == 0
    data["assets"][0].update(preflight_absent=True, state="prepared")
    save(tmp_path, data)
    assert clean.cleanup(tmp_path)["assets"] == 0


def test_local_ownership_labels_and_process_identity_checked_before_delete(tmp_path, monkeypatch):
    name = "rag-trial-" + TRIAL[:12]
    runtime = {"container_name": name, "volume_name": name, "server_pid": 32145}
    calls, kills = [], []
    label, command = TRIAL, "python3 another_server.py"

    def run(args):
        calls.append(args)
        if args[0] == "ps":
            return SimpleNamespace(returncode=1 if kills else 0, stdout="" if kills else command)
        if "ls" in args:
            return SimpleNamespace(returncode=0, stdout=name + "\n")
        if "inspect" in args:
            labels = {"com.rag.knowledge-trial": label}
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Labels": labels, "Config": {"Labels": labels}}]))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(clean.os, "kill", lambda *args: kills.append(args))
    with pytest.raises(clean.CleanupError, match="foreign_server_process"):
        clean.clean_local(tmp_path, TRIAL, runtime, run=run, sleep=lambda _: None)
    assert not kills and not any("rm" in call or "stop" in call for call in calls)
    label = "another-trial"
    with pytest.raises(clean.CleanupError, match="foreign_local_resource_label"):
        clean.clean_local(tmp_path, TRIAL, runtime, run=run, sleep=lambda _: None)
    label = TRIAL
    command = f"python3 scripts/knowledge_trial.py serve --trial {tmp_path} --port 8788"
    clean.clean_local(tmp_path, TRIAL, runtime, run=run, sleep=lambda _: None)
    assert kills == [(32145, clean.signal.SIGTERM)]
    assert calls[-3:] == [["docker", "container", "stop", name], ["docker", "container", "rm", name],
                          ["docker", "volume", "rm", name]]
