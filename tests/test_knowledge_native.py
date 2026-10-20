import hashlib
import io
import json
from types import SimpleNamespace
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from rag_app.knowledge_native import TextSubmission, source_package, submit_text, submission_status
from rag_app.knowledge_receipts_api import get_receipt_service, router
from rag_app.knowledge_processing import ProcessingService, ProcessingStore
from rag_app.knowledge_objects import KnowledgeObjects
from rag_app.knowledge_service import KnowledgeService
from rag_app.knowledge_receipts import ReceiptError, ReceiptObjects, ReceiptService, ReceiptStore
from rag_app.knowledge_store import KnowledgeStore
from test_knowledge_processing import Bucket
from test_knowledge_processing_phase3 import Index

pytestmark = pytest.mark.offline

PAYLOAD = dict(source_id="hermes:synthetic-work:1", title="交接资料核对方法", kind="方法",
    content="# 交接资料核对方法\n\n输入是待交接的项目资料。先按任务列出文件，再检查原文与附件是否齐全，最后逐项记录验证结果。发现缺少必要附件时，标记待补充，不把缺失内容写成已经完成。输出是带证据的核对清单，仅适用于岗位交接资料检查，不替代业务审批。\n",
    position="测试岗位", source_locator="合成验收会话中的完整工作方法", purpose="新人核对交接资料",
    evidence_status="合成方法样本，未宣称真实业务成效", audience="接手岗位的同事", inputs="待交接的项目资料",
    outputs="带依据的核对清单", dependencies="具有项目资料读取权限", boundaries="仅检查完整性，不替代审批",
    sharing_confirmed=True)
TOKEN = "native-test-only-token-0000000000000000"


class Storage(Bucket):
    def put_object(self, key, data, headers):
        if key in self.data:
            error = RuntimeError("existing immutable object")
            error.status, error.code = 409, "FileAlreadyExists"
            raise error
        super().put_object(key, data, headers)
        self.meta[key] = {k: v for k, v in headers.items() if k.startswith("x-oss-meta-")}


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BODY_STORAGE", "inline")
    bucket, index = Storage(), Index()
    path = tmp_path / 'shared.db'
    objects = ReceiptObjects(bucket)
    receipts = ReceiptService(ReceiptStore(path), objects)
    receipts.store.initialize()
    processor = ProcessingService(ProcessingStore(path), objects, auto_publish=True, knowledge_index=index,
                                  artifacts=KnowledgeObjects(bucket), draft_objects=KnowledgeObjects(bucket))
    processor.store.initialize()
    catalogue = KnowledgeStore(path, objects=KnowledgeObjects(bucket))
    catalogue.initialize()
    catalogue.register_role("测试部门", PAYLOAD["position"])
    knowledge = KnowledgeService(catalogue, index)
    return SimpleNamespace(receipts=receipts, processor=processor, knowledge=knowledge, bucket=bucket, index=index)


def submit(system, **changes):
    return submit_text(system.receipts, "owner:a", TextSubmission(**{**PAYLOAD, **changes}))


def process(system):
    assert system.receipts.verify_once()
    assert system.processor.run_once()


def test_native_submission_reuses_shared_catalogue_and_processing(system):
    response = submit(system)
    assert response["processing"] is None and response["upload"] is None
    assert response["receipt"]["status"] == "pending_verification"
    assert not system.bucket.signatures
    process(system)
    result = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    assert result["processing"]["counts"]["published"] == 1
    item = next(i for i in result["processing"]["items"] if i["published"])
    record = system.knowledge.get(item["knowledge_id"])
    assert record["evidence"]["uploader_position"] == "测试岗位"
    assert record["evidence"]["uploader_position_source"] == "employee_declared"
    assert record["content_hash"] == hashlib.sha256(record["content"].encode()).hexdigest()
    assert record["content"] == PAYLOAD["content"]
    assert system.knowledge.store.published_count() == 1
    stored = system.knowledge.store.get_published(item['knowledge_id'])
    assert stored['evidence']['ingestion_source'] == 'agent_collector'
    assert system.receipts.store.get('owner:a', response['receipt']['receipt_id'])['ingestion_source'] == 'agent_collector'
    assert not any(key.startswith('company-knowledge-employee-beta/') for key in system.bucket.data)
    assert not any(secret in json.dumps(result) for secret in ("object_key", "lease_token", "owner:a"))


def test_retry_does_not_create_more_receipts_or_versions(system):
    first = submit(system)
    assert submit(system)["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]
    process(system)
    assert submit(system)["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]
    assert not system.receipts.verify_once()
    assert not system.processor.run_once()
    assert system.knowledge.store.published_count() == 1
    assert len(system.index.calls) == 1


def test_receipt_status_is_bound_to_authenticated_employee(system):
    response = submit(system)
    with pytest.raises(ReceiptError, match="receipt_not_found"):
        submission_status(system.receipts, "owner:b", response["receipt"]["receipt_id"])


def test_sensitive_material_is_not_automatically_published(system):
    response = submit(system, content=PAYLOAD["content"] + "\npassword=synthetic-secret-12345\n")
    process(system)
    result = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    assert not result["processing"]["counts"]["published"]
    assert result["processing"]["status"] == "needs_review"
    assert not system.index.calls
    assert "synthetic-secret" not in json.dumps(result)


def test_repair_status_names_missing_material_and_preserves_old_processing_contract(system, monkeypatch):
    response = submit(system, content=PAYLOAD["content"] + "\n必需附件：[对照表](附件/对照表.md)\n")
    process(system)
    # Status must not fetch originals or processed documents to explain a blocker.
    monkeypatch.setattr(system.processor.artifacts, "get", lambda *_: pytest.fail("status read an object"))
    result = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    repair = result["repair"]
    assert repair["items_total"] == 1 and repair["truncated"] is False
    item = repair["items"][0]
    assert item["status"] == "needs_review" and item["version"] > 0
    assert "unresolved_reference" in item["reason_codes"]
    assert item["missing_references"] == ["附件/对照表.md"]
    assert item["title"] == PAYLOAD["title"]
    assert "missing_references" not in result["processing"]["items"][0]
    with pytest.raises(ReceiptError, match="receipt_not_found"):
        submission_status(system.receipts, "owner:b", response["receipt"]["receipt_id"])
    system.processor.store.resolve(result["processing"]["job_id"], item["item_id"], None,
        action="archive", expected_version=item["version"], reason="无需入库，保留原件", actor="reviewer")
    closed = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    assert closed["repair"] == {"items": [], "items_total": 0, "truncated": False}
    assert closed["processing"]["status"] == "completed"


def test_repair_status_supports_older_schema_and_bounded_pages(system):
    response = submit(system, content=PAYLOAD["content"] + "\n[附件](附件/对照表.md)\n")
    process(system)
    before = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    job_id = before["processing"]["job_id"]
    with system.processor.store._connection(write=True) as connection:
        template = dict(connection.execute(
            "SELECT * FROM knowledge_processing_items WHERE job_id=? AND status='needs_review'", (job_id,)).fetchone())
        for i in range(25):
            values = {**template, "item_id": f"{i:032x}", "source_path": f"正文/{i}.md"}
            connection.execute("INSERT INTO knowledge_processing_items (" + ",".join(values) + ") VALUES (" +
                ",".join("?" for _ in values) + ")", tuple(values.values()))
        connection.execute("ALTER TABLE knowledge_processing_items DROP COLUMN repair_json")
    result = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    assert result["repair"]["items_total"] == 26 and result["repair"]["truncated"]
    assert len(result["repair"]["items"]) == 20
    assert all(item["missing_references"] == [] for item in result["repair"]["items"])
    system.processor.store.initialize()
    with system.processor.store._connection() as connection:
        assert "repair_json" in {r["name"] for r in connection.execute("PRAGMA table_info(knowledge_processing_items)")}


def test_missing_reference_projection_is_safe_bounded_data():
    from rag_app.knowledge_processing import missing_references
    values = ["附件/样例.md", "../references/说明.md", "/etc/private", "../../private",
        "https://example.test/private?token=secret", "C:/private", "a//b", "./a", "a/../b", "~/.env", "a\nb"]
    assert missing_references([{"reference": value} for value in values], "正文/方法.md") == [
        "附件/样例.md", "references/说明.md"]
    assert len(missing_references([{"reference": f"附件/{i}.md"} for i in range(30)])) == 20
    assert missing_references([{"reference": "../" + "文" * 490}], "长" * 30 + "/正文/方法.md") == []


def test_repair_status_hides_resolved_source_relation_while_rechecking(system):
    response = submit(system, content=PAYLOAD["content"] + "\n[附件](附件/对照表.md)\n")
    process(system)
    first = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    item = first["repair"]["items"][0]
    with system.processor.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_items SET reason_codes=?,repair_json=? WHERE item_id=?",
            (json.dumps(["source_relation_review"]), json.dumps({"replacement_recheck": True}), item["item_id"]))
        connection.execute("UPDATE knowledge_processing_jobs SET status='queued' WHERE job_id=?",
            (first["processing"]["job_id"],))
    status = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    assert status["repair"]["items"][0]["reason_codes"] == []
    assert any(row["reason_codes"] == ["source_relation_review"] for row in status["processing"]["items"])


def test_status_returns_confirmed_role_even_when_attachment_blocks_publication(system):
    response = submit(system, position="原岗位待核对", content=PAYLOAD["content"] + "\n[附件](附件/对照表.md)\n")
    process(system)
    before = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    item = before["repair"]["items"][0]
    role = system.knowledge.store.register_role("测试部门", PAYLOAD["position"])
    job_id = before["processing"]["job_id"]
    selection = system.processor.store.get_item_uploader_role(job_id, item["item_id"])
    system.processor.store.set_item_uploader_role(job_id, item["item_id"], role["role_id"],
        selection["version"], actor="reviewer", remember_rule=False)
    result = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    pending = next(row for row in result["processing"]["items"] if row["item_id"] == item["item_id"])
    assert pending["role_id"] == role["role_id"]
    assert pending["department"] == "测试部门" and pending["role_name"] == PAYLOAD["position"]
    assert pending["knowledge_id"] is None and pending["revision"] is None and not pending["published"]


def test_repair_status_respects_mcp_byte_budget(system):
    response = submit(system, content=PAYLOAD["content"] + "\n[附件](附件/对照表.md)\n")
    process(system)
    first = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    with system.processor.store._connection(write=True) as connection:
        original = dict(connection.execute(
            "SELECT * FROM knowledge_processing_items WHERE job_id=? AND status='needs_review'",
            (first["processing"]["job_id"],)).fetchone())
        for i in range(25):
            values = {**original, "item_id": f"{i:032x}", "source_path": f"正文/{i}.md",
                "repair_json": json.dumps({"missing_references": [
                    "附件/" + "文" * 480 + str(j) + ".md" for j in range(20)]})}
            connection.execute("INSERT INTO knowledge_processing_items (" + ",".join(values) + ") VALUES (" +
                ",".join("?" for _ in values) + ")", tuple(values.values()))
    result = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 60_000
    assert result["repair"]["truncated"] and result["repair"]["items_total"] == 26
    assert result["repair"]["items"][0]["reason_codes"]


def test_same_source_revision_does_not_overwrite_without_a_base(system):
    first = submit(system)
    process(system)
    second = submit(system, content=PAYLOAD["content"] + "\n补充：核对文件修订日期。")
    process(system)
    result = submission_status(system.receipts, "owner:a", second["receipt"]["receipt_id"])
    assert result["processing"]["status"] == "needs_review"
    original = submission_status(system.receipts, "owner:a", first["receipt"]["receipt_id"])
    kid = next(i["knowledge_id"] for i in original["processing"]["items"] if i["published"])
    assert system.knowledge.get(kid)["revision"] == 1


def test_valid_update_keeps_identity_and_rejects_another_employees_overwrite(system):
    first = submit(system)
    process(system)
    first_job = submission_status(system.receipts, "owner:a", first["receipt"]["receipt_id"])["processing"]
    kid = next(i["knowledge_id"] for i in first_job["items"] if i["published"])
    revised = PAYLOAD["content"] + "\n补充步骤：核对版本日期后再交接。"
    second = submit(system, content=revised, base_revision=1)
    process(system)
    second_job = submission_status(system.receipts, "owner:a", second["receipt"]["receipt_id"])["processing"]
    assert any(i["knowledge_id"] == kid and i["revision"] == 2 and i["published"] for i in second_job["items"])
    assert system.knowledge.get(kid)["content"] == revised
    submit_text(system.receipts, "owner:b", TextSubmission(**{**PAYLOAD, "content": revised + "\n其他人的补充。", "base_revision": 2}))
    process(system)
    assert system.knowledge.get(kid)["revision"] == 2
    assert system.knowledge.get(kid)["content"] == revised


@pytest.mark.parametrize("changed", [False, True])
def test_text_endpoint_reuses_legacy_duplicate_but_cannot_revise_its_table(system, monkeypatch, changed):
    data = source_package(TextSubmission(**PAYLOAD))
    legacy = system.receipts.store.prepare("owner:a", {
        "idempotency_key": "old-uploader-1", "source_id": PAYLOAD["source_id"], "filename": "old-assets.zip",
        "byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    })
    system.bucket.upload(legacy, data)
    system.receipts.complete("owner:a", legacy["receipt_id"])
    process(system)
    old_job = submission_status(system.receipts, "owner:a", legacy["receipt_id"])["processing"]
    kid = next(item["knowledge_id"] for item in old_job["items"] if item["published"])
    before = system.knowledge.store.snapshot(kid)
    assert before["evidence"].get("ingestion_source", "legacy") == "legacy"
    assert len(system.index.calls) == 1

    monkeypatch.setenv("KNOWLEDGE_RECEIPTS_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_RECEIPTS_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_receipt_service] = lambda: system.receipts
    headers = {"Authorization": "Bearer " + TOKEN, "X-Knowledge-Principal": "owner:a"}
    request = {**PAYLOAD, "content": PAYLOAD["content"] + "\n新增核对步骤：检查资料修订日期。", "base_revision": 1} if changed else PAYLOAD
    with TestClient(app) as client:
        response = client.post("/api/knowledge-intake/text", json=request, headers=headers)
        assert response.status_code == 202
        rid = response.json()["receipt"]["receipt_id"]
        process(system)
        result = client.get("/api/knowledge-intake/uploads/" + rid, headers=headers).json()["processing"]
    assert system.receipts.store.get("owner:a", rid)["ingestion_source"] == "agent_collector"
    assert system.knowledge.store.snapshot(kid) == before
    assert system.knowledge.store.published_count() == 1 and len(system.index.calls) == 1
    if changed:
        assert result["status"] == "needs_review"
        assert any(item["status"] == "needs_review" and item["reason_codes"] == ["source_ingestion_source_conflict"]
                   for item in result["items"])
        job = system.processor.store.get_job(result["job_id"], "owner:a")
        assert job["attempts"] == 1 and not job["retryable"]
        assert not system.processor.run_once()
    else:
        assert result["status"] == "completed"
        assert any(item["status"] == "duplicate" and item["knowledge_id"] == kid and item["revision"] == 1
                   for item in result["items"])


def test_deterministic_package_keeps_position_and_complete_body():
    request = TextSubmission(**PAYLOAD)
    data = source_package(request)
    assert data == source_package(request)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.endswith(".md"))
        assert z.read(name).decode() == PAYLOAD["content"]
        assert "上传员工岗位" in z.read("交付清单.csv").decode()


def test_optional_department_keeps_old_receipt_bytes_and_reaches_intake(system):
    old = source_package(TextSubmission(**PAYLOAD))
    # Existing schedules must retry the same immutable package and receipt.
    assert hashlib.sha256(old).hexdigest() == "5ed5da741d65e80fe351f46c1a70aa643c8efd5bdd91dda1942f310226b9c80d"
    assert source_package(TextSubmission(**{**PAYLOAD, "department": None})) == old
    role = system.knowledge.store.register_role("测试部门", PAYLOAD["position"])
    response = submit(system, department=" 测试部门 ")
    process(system)
    status = submission_status(system.receipts, "owner:a", response["receipt"]["receipt_id"])
    item = next(i for i in status["processing"]["items"] if i["published"])
    assert (item["role_id"], item["department"], item["role_name"]) == (
        role["role_id"], "测试部门", PAYLOAD["position"])
    record = system.knowledge.get(item["knowledge_id"])
    assert record["role_id"] == role["role_id"]
    assert record["department"] == "测试部门"
    assert record["role_name"] == PAYLOAD["position"]
    assert record["evidence"]["uploader_position"] == PAYLOAD["position"]


@pytest.mark.parametrize("department", ["", " ", "部门\n另一部门", "x" * 201])
def test_invalid_department_is_rejected(department):
    with pytest.raises(ValueError):
        TextSubmission(**{**PAYLOAD, "department": department})


@pytest.mark.parametrize("changes", [{"position": "【填写自己的岗位】"}, {"sharing_confirmed": False},
    {"sharing_confirmed": 1}, {"content": " "}, {"content": "x" * 50001}, {"collection": "official"},
    {"principal": "owner:b"}, {"source_id": "../escape"}, {"base_revision": True}])
def test_invalid_and_cross_boundary_requests_are_rejected(changes):
    with pytest.raises(ValueError):
        TextSubmission(**{**PAYLOAD, **changes})


def test_api_authentication_validation_status_and_no_secrets(system, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RECEIPTS_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_RECEIPTS_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_receipt_service] = lambda: system.receipts
    headers = {"Authorization": "Bearer " + TOKEN, "X-Knowledge-Principal": "owner:a"}
    with TestClient(app) as client:
        assert client.post("/api/knowledge-intake/text", json=PAYLOAD).status_code == 401
        bad = client.post("/api/knowledge-intake/text", json={**PAYLOAD, "private": "secret-input"}, headers=headers)
        assert bad.status_code == 422 and "secret-input" not in bad.text
        result = client.post("/api/knowledge-intake/text", json=PAYLOAD, headers=headers)
        assert result.status_code == 202
        rid = result.json()["receipt"]["receipt_id"]
        assert client.get("/api/knowledge-intake/uploads/" + rid, headers=headers).status_code == 200
        other = {**headers, "X-Knowledge-Principal": "owner:b"}
        assert client.get("/api/knowledge-intake/uploads/" + rid, headers=other).status_code == 404
