"""Opt-in live PostgreSQL + private OSS + OpenSearch lifecycle check.

Run only with KNOWLEDGE_TEST_DATABASE_URL and KNOWLEDGE_TEST_CLOUD=1 explicitly
set. Uses existing cloud configuration, synthetic text and unique test IDs.
Reports contain cleanup identifiers/hashes, never credentials, URLs or bodies.
KNOWLEDGE_TEST_REPORT_DIR overrides the report directory's parent.
"""

import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_TEST_DATABASE_URL") or os.getenv("KNOWLEDGE_TEST_CLOUD") != "1",
    reason="explicit test database and cloud integration opt-in required",
)]


def test_live_knowledge_lifecycle_and_exact_cleanup(monkeypatch):
    import oss2
    import psycopg
    from psycopg import sql
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from rag_app.config import load_env
    from rag_app.knowledge_api import get_knowledge_service, router
    from rag_app.knowledge_index import KnowledgeIndex, VECTOR_DIMENSIONS, _check_body, chunk_document
    from rag_app.knowledge_objects import KnowledgeObjects
    from rag_app.knowledge_service import KnowledgeService
    from rag_app.knowledge_store import KnowledgeStore, normalize_entry
    from rag_app.retrieval_service import extract_result_items, flatten_result_item

    run_id = uuid4().hex
    schema, knowledge_id = "knowledge_test_" + run_id, "kgitest_" + run_id
    prefix = "company-knowledge/tests/" + run_id + "/"
    root = Path(os.getenv("KNOWLEDGE_TEST_REPORT_DIR") or
                Path(__file__).resolve().parents[1] / "output" / "knowledge_integration") / run_id
    root.mkdir(parents=True, exist_ok=False)
    entries, chunks, references = [], [], []
    for revision in (1, 2):
        body = (f"# 合成联调方法 {run_id}\n\n## 输入核对\n\n"
                + "先核对输入字段和原始证据，缺失时保留异常记录，不作成功保证。\n" * 24
                + f"\n## 例外处理\n\n这是第 {revision} 版合成测试资料。\n"
                + "遇到例外，核对来源和适用条件，并完整记录失败原因与后续动作。\n" * 16)
        entry = normalize_entry({
            "knowledge_id": knowledge_id, "title": "合成输入核对与例外处理方法",
            "content": body, "contributor": "集成测试", "kind": "method",
            "sources": [{"name": "合成测试，无公司真实资料"}],
            "chunking_version": "structure-v2", "evidence": {"synthetic": True},
        })
        entries.append(entry)
        chunks.extend(chunk_document({**entry, "revision": revision}))
        references.append({"object_key": prefix + entry["content_hash"] + ".txt",
                           "sha256": entry["content_hash"],
                           "byte_length": len(body.encode("utf-8"))})
    ids = [chunk["id"] for chunk in chunks]
    revision_keys = sorted({chunk["revision_key"] for chunk in chunks})
    unit_vector = [1.0] + [0.0] * (VECTOR_DIMENSIONS - 1)
    report = {"run_id": run_id, "schema": schema, "knowledge_id": knowledge_id,
              "object_prefix": prefix, "objects": references, "chunk_ids": ids,
              "revision_keys": revision_keys,
              "created_object_versions": {},
              "status": "prepared", "mutation_attempted": False, "checks": [],
              "cleanup": {}, "synthetic_only": True}
    manifest = root / "cleanup_manifest.json"

    def save():
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manifest)

    def require(condition, label):
        if not condition:
            raise AssertionError(label)

    def checked(label):
        report["checks"].append(label)
        save()

    def safe_failure(error, phase):
        result = {"stage": phase, "type": type(error).__name__}
        code = getattr(error, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", code):
            result["code"] = code
        status = getattr(error, "status", None)
        if status is None:
            status = getattr(error, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            result["http_status"] = status
        return result

    save()  # Persist every possible external cleanup target before any write.
    admin = objects = cloud = None
    owned = False
    primary = None
    cleanup_errors = []
    stage = "configuration"

    def fetch_test_ids():
        response = cloud.client.fetch(cloud.models.FetchRequest(
            table_name=cloud.table, ids=ids, include_vector=False, output_fields=["id"]))
        _check_body(response)
        return [flatten_result_item(item) for item in extract_result_items(response)]

    def object_head(key):
        try:
            return objects.bucket.get_object_meta(key)
        except oss2.exceptions.NoSuchKey:
            return None

    def verify_full_api(client, revision):
        response = client.post("/api/knowledge/search", json={
            "query": "如何核对输入字段、证据并处理例外？", "top_k": 1})
        require(response.status_code == 200, "search_status")
        results = response.json()["results"]
        require(len(results) == 1, "search_count")
        result = results[0]
        require(result.get("retrieval_routes") == ["text_dense", "text_keyword"], "both_text_routes_used")
        require(result.get("rerank_mode") == "model" and result.get("rerank_score") == result.get("score"),
                "real_rerank_used_without_fallback")
        require(result["knowledge_id"] == knowledge_id and result["revision"] == revision, "search_identity")
        require(result["content"] == entries[revision - 1]["content"], "full_search_body")
        require(result["content_hash"] == entries[revision - 1]["content_hash"], "full_search_hash")
        expected_chunks = {chunk["id"]: chunk["source_text"] for chunk in chunks
                           if chunk["revision"] == revision}
        require(result["chunk_id"] in expected_chunks and
                result["snippet"] == expected_chunks[result["chunk_id"]] and
                result["snippet"] != result["content"], "same_revision_multi_chunk_hit")
        require(result["read_url"] == f"/api/knowledge/{knowledge_id}?revision={revision}", "versioned_read_url")
        read = client.get(result["read_url"])
        require(read.status_code == 200, "read_status")
        require(read.json()["knowledge"]["content"] == result["content"], "read_matches_search")
        require(not ({"content_ref", "object_key"} & result.keys()), "private_reference_not_exposed")

    try:
        load_env()
        database_url = os.environ["KNOWLEDGE_TEST_DATABASE_URL"]
        parts = urlsplit(database_url)
        query = dict(parse_qsl(parts.query))
        query["options"] = f"-csearch_path={schema}"
        test_url = urlunsplit(parts._replace(query=urlencode(query)))
        objects = KnowledgeObjects(prefix=prefix)
        original_put = objects.bucket.put_object

        def tracked_put(key, *args, **kwargs):
            result = original_put(key, *args, **kwargs)
            # Record the version returned by this write, never a later HEAD's
            # current version. An ambiguous write must not delete an unknown one.
            report["created_object_versions"][key] = result.headers.get("x-oss-version-id")
            save()
            return result

        monkeypatch.setattr(objects.bucket, "put_object", tracked_put)
        cloud = KnowledgeIndex()
        cloud._connect()
        datasource = cloud._datasource()
        report.update(table=cloud.table, datasource=datasource)
        save()

        stage = "preflight"
        admin = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
        require(admin.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0] is None, "schema_collision")
        require(not fetch_test_ids(), "chunk_id_collision")
        for ref in references:
            require(object_head(ref["object_key"]) is None, "object_collision")
        checked("preflight_all_targets_absent")
        # A failed/forbidden preflight never authorizes cleanup of existing data.
        owned = True
        report.update(status="running", mutation_attempted=True)
        save()
        stage = "schema_initialization"
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        store = KnowledgeStore(database_url=test_url, objects=objects)
        store.initialize()
        store.initialize()
        service = KnowledgeService(store=store, index=cloud)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_knowledge_service] = lambda: service

        with TestClient(app) as client:
            for revision, entry in enumerate(entries, 1):
                stage = f"import_revision_{revision}"
                draft = service.import_document(entry, execute=True)
                require(draft["revision"] == revision, "import_revision")
                require(len(chunk_document(draft)) > 1, "requires_multiple_chunks")
                with store._connection() as connection:
                    row = store._execute(connection,
                        "SELECT record_json FROM knowledge_revisions WHERE knowledge_id = ? AND revision = ?",
                        (knowledge_id, revision)).fetchone()
                stored = json.loads(row["record_json"])
                require("content" not in stored and stored["content_ref"] == references[revision - 1], "postgres_reference_only")
                require(objects.get(stored["content_ref"]) == entry["content"], "oss_full_body")
                key = stored["content_ref"]["object_key"]
                require(objects.bucket.get_object_acl(key).acl == "private", "oss_private_acl")
                stage = f"anonymous_access_revision_{revision}"
                anonymous = oss2.Bucket(oss2.AnonymousAuth(), objects.bucket.endpoint,
                                        objects.bucket.bucket_name, connect_timeout=10)
                denied = False
                try:
                    with anonymous.get_object(key):
                        pass  # Never read or print a publicly accessible body.
                except oss2.exceptions.ServerError as error:
                    denied = error.status == 403
                require(denied, "anonymous_get_must_be_403")
                checked(f"revision_{revision}_private_oss_and_pg_reference")

                stage = f"draft_visibility_revision_{revision}"
                if revision == 1:
                    service.index_draft(knowledge_id, revision=1, execute=True)
                    require(client.get(f"/api/knowledge/{knowledge_id}?revision=1").status_code == 404, "draft_read_hidden")
                    search = client.post("/api/knowledge/search", json={"query": "输入核对"})
                    require(search.status_code == 200 and search.json()["results"] == [], "draft_search_hidden")
                else:
                    verify_full_api(client, 1)
                    require(client.get(f"/api/knowledge/{knowledge_id}?revision=2").status_code == 404, "new_draft_hidden")
                checked(f"revision_{revision}_draft_isolation")
                stage = f"publish_revision_{revision}"
                service.publish(knowledge_id, revision=revision, confirmed_by="合成测试确认", execute=True)
                verify_full_api(client, revision)
                checked(f"revision_{revision}_published_full_api_read")

            stage = "fresh_service_and_withdrawal"
            require(client.get(f"/api/knowledge/{knowledge_id}?revision=1").status_code == 404, "old_revision_hidden")
            service = KnowledgeService(
                store=KnowledgeStore(database_url=test_url, objects=KnowledgeObjects(prefix=prefix)),
                index=KnowledgeIndex())
            verify_full_api(client, 2)
            checked("fresh_service_reads_persisted_pg_oss_version")
            service.withdraw(knowledge_id, execute=True)
            require(client.get(f"/api/knowledge/{knowledge_id}?revision=2").status_code == 404, "withdrawn_read_hidden")
            search = client.post("/api/knowledge/search", json={"query": "输入核对"})
            require(search.status_code == 200 and search.json()["results"] == [], "withdrawn_search_hidden")
            checked("withdrawn_api_read_and_search_hidden")
        report["status"] = "passed_pending_cleanup"
    except BaseException as error:
        primary = safe_failure(error, stage)
        report.update(status="failed", failure=primary)
    finally:
        if owned:
            try:
                response = cloud.client.push_documents(datasource, "id", cloud.models.PushDocumentsRequest(
                    headers={}, body=[{"cmd": "delete", "fields": {"id": key}} for key in ids]))
                _check_body(response, write=True)
                empty_checks = 0
                for attempt in range(31):
                    fetched = fetch_test_ids()
                    queried = cloud._query(unit_vector, revision_keys, 1)
                    keyword_queried = cloud._query_keyword("输入核对", revision_keys, 1)
                    empty_checks = empty_checks + 1 if not fetched and not queried and not keyword_queried else 0
                    if empty_checks == 2:
                        break
                    if attempt < 30:
                        time.sleep(1)
                require(empty_checks == 2, "cloud_cleanup_unverified")
                report["cleanup"]["cloud_ids_absent_checks"] = empty_checks
                report["cleanup"]["cloud_query_absent_checks"] = empty_checks
                report["cleanup"]["cloud_keyword_absent_checks"] = empty_checks
            except BaseException as error:
                cleanup_errors.append(safe_failure(error, "cloud_ids"))
            for ref in references:
                try:
                    if ref["object_key"] in report["created_object_versions"]:
                        version = report["created_object_versions"][ref["object_key"]]
                        objects.bucket.delete_object(ref["object_key"], params={"versionId": version} if version else None)
                    else:
                        require(object_head(ref["object_key"]) is None, "unknown_object_creation_version")
                    for attempt in range(2):
                        require(object_head(ref["object_key"]) is None, "oss_cleanup_unverified")
                        if attempt == 0:
                            time.sleep(1)
                    report["cleanup"].setdefault("objects_absent_checks", {})[ref["sha256"]] = 2
                except BaseException as error:
                    cleanup_errors.append({**safe_failure(error, "oss_object"), "sha256": ref["sha256"]})
            try:
                admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
                for _ in range(2):
                    require(admin.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0] is None, "schema_cleanup_unverified")
                report["cleanup"]["schema_absent_checks"] = 2
            except BaseException as error:
                cleanup_errors.append(safe_failure(error, "schema"))
        else:
            report["cleanup"]["skipped_no_mutation"] = True
        if admin is not None:
            try:
                admin.close()
            except BaseException as error:
                cleanup_errors.append(safe_failure(error, "database_close"))
        report["cleanup"]["errors"] = cleanup_errors
        report["status"] = "failed" if primary or cleanup_errors else "passed_and_cleaned"
        try:
            save()
        except BaseException as error:
            cleanup_errors.append(safe_failure(error, "report_write"))
    if primary or cleanup_errors:
        # SDK/DB exception text can contain credentials. Keep both failure phases
        # in the report without exposing raw provider messages or tracebacks.
        phases = {"failure": primary, "cleanup_errors": cleanup_errors}
        pytest.fail(f"Knowledge integration failed: {json.dumps(phases)}; see {manifest}", pytrace=False)
