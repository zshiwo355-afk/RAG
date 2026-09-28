#!/usr/bin/env python3
"""Remove only manifest-owned trial resources. Default: validate and preview.

No prefix listing/deletion, guessed object versions, database URLs in reports,
or shell commands. A failed cleanup retains its manifest for exact retries.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class CleanupError(ValueError):
    """Fixed, safe diagnostic code; never include provider/runtime contents."""


def require(condition, code):
    if not condition:
        raise CleanupError(code)


def read_manifest(trial):
    data = json.loads((trial / "trial.json").read_text(encoding="utf-8"))
    trial_id = data.get("trial_id")
    require(isinstance(trial_id, str) and re.fullmatch(r"[0-9a-f]{32}", trial_id), "invalid_trial_id")
    prefix = "company-knowledge/trials/" + trial_id + "/"
    require(data.get("prefix") == prefix, "invalid_trial_prefix")
    require(isinstance(data.get("assets"), list), "invalid_assets")
    owned, seen = [], set()
    for asset in data["assets"]:
        require(isinstance(asset, dict), "invalid_asset")
        key, digest = asset.get("knowledge_id"), asset.get("content_hash")
        require(isinstance(key, str) and re.fullmatch("trial_" + trial_id[:8] + r"_[0-9a-f]{24}", key)
                and key not in seen, "foreign_or_duplicate_asset_id")
        seen.add(key)
        require(type(asset.get("revision")) is int and asset["revision"] == 1, "invalid_revision")
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "invalid_content_hash")
        require(asset.get("object_key") == prefix + digest + ".txt", "foreign_object_key")
        revision_key = key + "__r1"
        require(asset.get("revision_key") == revision_key, "foreign_revision_key")
        ids = asset.get("chunk_ids")
        require(isinstance(ids, list) and all(isinstance(item, str) and
                re.fullmatch(re.escape(revision_key) + r"__c[1-9][0-9]*", item) for item in ids)
                and len(ids) == len(set(ids)), "foreign_or_duplicate_chunk_id")
        require(asset.get("state") in {"prepared", "loading", "ready", "error"}
                and type(asset.get("preflight_absent")) is bool, "invalid_asset_state")
        if asset["preflight_absent"] and asset["state"] in {"loading", "ready", "error"}:
            owned.append(asset)
    versions = data.get("created_object_versions")
    require(isinstance(versions, dict), "invalid_created_object_versions")
    owned_keys = {asset["object_key"] for asset in owned}
    for key, version in versions.items():
        require(key in owned_keys, "unowned_created_object")
        require(version is None or isinstance(version, str) and 0 < len(version) <= 1024
                and not any(char.isspace() or ord(char) < 32 for char in version), "invalid_object_version")
    return data, owned


def clean_index(cloud, assets, sleep):
    from rag_app.knowledge_index import (
        FILTER_BATCH_SIZE, VECTOR_DIMENSIONS, WRITE_BATCH_SIZE, _check_body,
    )
    from rag_app.retrieval_service import extract_result_items

    ids = [key for asset in assets for key in asset["chunk_ids"]]
    if not ids:
        return
    keys = [asset["revision_key"] for asset in assets if asset["chunk_ids"]]
    cloud._connect()
    datasource = cloud._datasource()
    for start in range(0, len(ids), WRITE_BATCH_SIZE):
        request = cloud.models.PushDocumentsRequest(headers={}, body=[
            {"cmd": "delete", "fields": {"id": key}} for key in ids[start:start + WRITE_BATCH_SIZE]])
        _check_body(cloud._call(lambda: cloud.client.push_documents(datasource, "id", request)), write=True)
    empty_checks = 0
    for attempt in range(31):
        found = False
        for start in range(0, len(ids), WRITE_BATCH_SIZE):
            request = cloud.models.FetchRequest(table_name=cloud.table, ids=ids[start:start + WRITE_BATCH_SIZE],
                                                include_vector=False, output_fields=["id"])
            response = cloud._call(lambda: cloud.client.fetch(request))
            _check_body(response)
            found |= bool(extract_result_items(response))
        for start in range(0, len(keys), FILTER_BATCH_SIZE):
            found |= bool(cloud._query([1.0] + [0.0] * (VECTOR_DIMENSIONS - 1),
                                      keys[start:start + FILTER_BATCH_SIZE], 1))
        empty_checks = 0 if found else empty_checks + 1
        if empty_checks == 2:
            return
        if attempt < 30:
            sleep(1)
    raise CleanupError("index_absence_unverified")


def object_absent(bucket, key, params):
    try:
        bucket.get_object_meta(key, params=params)
    except Exception as error:
        # Never interpret permission/network errors or a missing bucket as absence.
        if getattr(error, "code", None) in {"NoSuchKey", "NoSuchVersion"}:
            return True
        raise
    return False


def clean_object(bucket, key, versions, sleep):
    known = key in versions
    params = {"versionId": versions[key]} if known and versions[key] is not None else None
    if known and versions[key] is None:
        # An absent PUT version header means an unversioned write. It can later
        # become a null historical version if bucket versioning was enabled.
        mode = bucket.get_bucket_versioning().status
        require(mode in {None, "", "Enabled", "Suspended"}, "unknown_bucket_versioning")
        params = {"versionId": "null"} if mode in {"Enabled", "Suspended"} else None
    if known:
        bucket.delete_object(key, params=params)
    for check in range(2):
        require(object_absent(bucket, key, params), "object_absence_unverified" if known else "unknown_object_version")
        if check == 0:
            sleep(1)


def run_command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)


def clean_local(trial, trial_id, runtime, *, run, sleep):
    name = "rag-trial-" + trial_id[:12]
    container, volume = runtime.get("container_name"), runtime.get("volume_name")
    require(container == name and isinstance(volume, str) and
            re.fullmatch(re.escape(name) + r"(?:-[a-z0-9-]+)?", volume), "foreign_local_resource")
    present = {}
    # Check both labels before stopping/deleting either local resource.
    for kind, resource in (("container", container), ("volume", volume)):
        args = ["docker", "container", "ls", "-a"] if kind == "container" else ["docker", "volume", "ls"]
        result = run(args + ["--filter", "name=" + resource, "--format", "{{.Names}}" if kind == "container" else "{{.Name}}"])
        require(result.returncode == 0, "docker_inventory_unavailable")
        present[kind] = resource in result.stdout.splitlines()
        if present[kind]:
            result = run(["docker", kind, "inspect", resource])
            require(result.returncode == 0, "docker_inspect_failed")
            item, = json.loads(result.stdout)
            labels = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
            require(isinstance(labels, dict) and labels.get("com.rag.knowledge-trial") == trial_id,
                    "foreign_local_resource_label")
    pid = runtime.get("server_pid")
    if pid is not None:
        require(type(pid) is int and pid > 1 and pid != os.getpid(), "invalid_server_pid")
        result = run(["ps", "-p", str(pid), "-o", "command="])
        if result.returncode == 0:
            argv = shlex.split(result.stdout)
            expected = ["serve", "--trial", str(trial)]
            require(any(Path(arg).name == "knowledge_trial.py" and argv[start + 1:start + 4] == expected
                        for start, arg in enumerate(argv)), "foreign_server_process")
            os.kill(pid, signal.SIGTERM)
            for _ in range(25):
                result = run(["ps", "-p", str(pid), "-o", "command="])
                if result.returncode == 1 and not result.stdout.strip():
                    break
                sleep(0.2)
            else:
                raise CleanupError("server_shutdown_unverified")
        else:
            require(result.returncode == 1 and not result.stdout.strip(), "server_inspection_failed")
    if present["container"]:
        require(run(["docker", "container", "stop", container]).returncode == 0, "container_stop_failed")
        require(run(["docker", "container", "rm", container]).returncode == 0, "container_remove_failed")
    if present["volume"]:
        require(run(["docker", "volume", "rm", volume]).returncode == 0, "volume_remove_failed")


def _cleanup(trial, *, execute=False, cloud=None, objects=None, run=run_command, sleep=time.sleep):
    from rag_app.knowledge_intake import write_json

    data, assets = read_manifest(trial)
    keys = sorted({asset["object_key"] for asset in assets})
    result = {"trial_id": data["trial_id"], "status": "dry_run", "assets": len(assets),
              "chunk_ids": sum(len(asset["chunk_ids"]) for asset in assets),
              "recorded_object_versions": len(data["created_object_versions"]), "objects": len(keys), "errors": []}
    if not execute:
        return result
    # Load private runtime but never copy any of its fields into reports.
    runtime_path = trial / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else None
    data["status"] = "cleanup_in_progress"
    write_json(trial / "trial.json", data)
    try:
        if result["chunk_ids"]:
            if cloud is None:
                from rag_app.knowledge_index import KnowledgeIndex
                cloud = KnowledgeIndex()
            clean_index(cloud, assets, sleep)
        result["index_absent_checks"] = 2 if result["chunk_ids"] else 0
    except Exception as error:
        result["errors"].append({"stage": "index", "code": str(error) if isinstance(error, CleanupError) else "index_cleanup_failed"})
    for key in keys:
        try:
            if objects is None:
                from rag_app.knowledge_objects import KnowledgeObjects
                objects = KnowledgeObjects(prefix=data["prefix"])
            clean_object(objects.bucket, key, data["created_object_versions"], sleep)
            result.setdefault("objects_absent_checks", {})[key] = 2
        except Exception as error:
            result["errors"].append({"stage": "object", "object_key": key,
                                     "code": str(error) if isinstance(error, CleanupError) else "object_cleanup_failed"})
    if not result["errors"]:
        try:
            if runtime is not None:
                clean_local(trial, data["trial_id"], runtime, run=run, sleep=sleep)
            result["local_removed"] = runtime is not None
        except Exception as error:
            result["errors"].append({"stage": "local", "code": str(error) if isinstance(error, CleanupError) else "local_cleanup_failed"})
    result["status"] = "cleanup_failed" if result["errors"] else "cleaned"
    data["status"] = result["status"]
    write_json(trial / "cleanup.json", result)
    write_json(trial / "trial.json", data)
    return result


def cleanup(trial, *, execute=False, cloud=None, objects=None, run=run_command, sleep=time.sleep):
    trial = Path(trial).resolve()
    if trial.name == "trial.json":
        trial = trial.parent
    options = {"execute": execute, "cloud": cloud, "objects": objects, "run": run, "sleep": sleep}
    if not execute:
        return _cleanup(trial, **options)
    # Shared with prepare/load. Keep the lock file: unlinking would allow two
    # processes to lock different inodes for the same trial directory.
    with (trial / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CleanupError("trial_operation_in_progress") from None
        return _cleanup(trial, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True, help="Trial directory or its trial.json manifest.")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        # Dry-run never loads credentials or creates SDK clients.
        if args.execute:
            from rag_app.config import load_env
            load_env()
        result = cleanup(args.trial, execute=args.execute)
    except Exception as error:
        result = {"status": "cleanup_failed", "error": str(error) if isinstance(error, CleanupError) else "cleanup_input_or_runtime_failed"}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "cleanup_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
