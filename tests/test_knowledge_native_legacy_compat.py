"""Old collector contracts against temporary stores, real processing, and fake OSS."""
import hashlib

import pytest

from rag_app.knowledge_native import TextSubmission, source_package, submission_status, submit_text
from test_knowledge_native import PAYLOAD, process, submit, system


pytestmark = pytest.mark.offline


def receipt_items(system, receipt, principal="owner:a"):
    return submission_status(system.receipts, principal, receipt["receipt_id"])["processing"]["items"]


@pytest.mark.parametrize("department", [{}, {"department": None}])
def test_old_request_bytes_receipt_source_and_retry_survive_restart(system, department):
    request = TextSubmission(**{**PAYLOAD, **department})
    archive = source_package(request)
    expected_hash = "5ed5da741d65e80fe351f46c1a70aa643c8efd5bdd91dda1942f310226b9c80d"
    assert hashlib.sha256(archive).hexdigest() == expected_hash
    response = submit_text(system.receipts, "owner:a", request)
    receipt = response["receipt"]
    assert receipt["source_id"] == PAYLOAD["source_id"]
    assert receipt["sha256"] == expected_hash
    assert response["upload"] is None
    process(system)
    before = receipt_items(system, receipt)

    # A fresh service must retry the old frozen submission, not invent a source.
    from rag_app.knowledge_receipts import ReceiptService, ReceiptStore
    restarted = ReceiptService(ReceiptStore(system.receipts.store.path), system.receipts._objects())
    replay = submit_text(restarted, "owner:a", request)
    assert replay["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert replay["receipt"]["source_id"] == PAYLOAD["source_id"]
    assert replay["receipt"]["sha256"] == expected_hash
    assert replay["processing"]["items"] == before
    assert not restarted.verify_once()
    assert not system.processor.run_once()
    assert system.knowledge.store.published_count() == 1
    assert len(system.index.calls) == 1


def test_unbound_review_candidate_has_no_public_knowledge_id(system):
    response = submit(system, position="未登记的真实岗位")
    process(system)
    items = receipt_items(system, response["receipt"])
    blocked = [item for item in items if item["status"] == "needs_review"]
    assert blocked and "uploader_role_not_found" in blocked[0]["reason_codes"]
    assert all(item["revision"] is None for item in blocked)
    assert all(item["knowledge_id"] is None for item in blocked)
    with system.processor.store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_submissions").fetchone()[0] == 0
    assert system.knowledge.store.published_count() == 0
    assert not system.index.calls


@pytest.mark.parametrize("auto_publish, expected_status", [(False, "draft"), (True, "published")])
def test_bound_draft_and_publication_keep_real_revision_pointer(system, auto_publish, expected_status):
    system.processor.auto_publish = auto_publish
    response = submit(system)
    process(system)
    item = next(item for item in receipt_items(system, response["receipt"]) if item["revision"])
    assert item["status"] == expected_status
    assert item["revision"] == 1 and item["knowledge_id"]
    snapshot = system.knowledge.store.snapshot(item["knowledge_id"])
    assert snapshot["revision"] == item["revision"]
    assert snapshot["content"] == PAYLOAD["content"]
    assert item["published"] is auto_publish


def test_duplicate_keeps_original_publication_pointer_and_declared_attribution(system):
    response = submit(system)
    process(system)
    first = next(item for item in receipt_items(system, response["receipt"]) if item["published"])
    snapshot = system.knowledge.store.snapshot(first["knowledge_id"])
    duplicate = submit_text(system.receipts, "owner:other", TextSubmission(**PAYLOAD))
    process(system)
    item = next(item for item in receipt_items(system, duplicate["receipt"], "owner:other")
                if item["status"] == "duplicate")
    assert (item["knowledge_id"], item["revision"]) == (first["knowledge_id"], first["revision"])
    assert system.knowledge.store.snapshot(first["knowledge_id"]) == snapshot
    record = system.knowledge.get(first["knowledge_id"])
    assert record["evidence"]["uploader_position"] == PAYLOAD["position"]
    assert record["evidence"]["uploader_position_source"] == "employee_declared"
    assert system.knowledge.store.published_count() == 1
    assert len(system.index.calls) == 1


def test_shared_connection_collision_requires_explicit_new_source_and_preserves_original(system):
    second_role = system.knowledge.store.register_role("第二测试部门", "订单核对专员")
    first = submit(system, department="测试部门")
    process(system)
    original = next(item for item in receipt_items(system, first["receipt"]) if item["published"])
    snapshot = system.knowledge.store.snapshot(original["knowledge_id"])
    changes = dict(
        department="第二测试部门", position="订单核对专员", title="订单交接核对步骤",
        source_locator="订单项目中的交接会话",
        content="# 订单交接核对步骤\n\n输入是待交接订单清单和已审批交付记录。"
                "逐笔核对订单编号、交付日期及审批依据，将不一致项目记录为待补充，"
                "由业务负责人核实后再交接。输出包含原订单引用和核对结果的记录，"
                "仅用于订单资料交接，不替代业务审批。\n",
    )
    second = submit(system, **changes)
    assert first["receipt"]["source_id"] == second["receipt"]["source_id"] == PAYLOAD["source_id"]
    assert first["receipt"]["receipt_id"] != second["receipt"]["receipt_id"]
    process(system)
    result = submission_status(system.receipts, "owner:a", second["receipt"]["receipt_id"])["processing"]
    blocked = [item for item in result["items"] if item["status"] == "needs_review"]
    assert len(blocked) == 1 and "source_relation_review" in blocked[0]["reason_codes"]
    assert blocked[0]["knowledge_id"] is None and blocked[0]["revision"] is None
    assert system.knowledge.store.snapshot(original["knowledge_id"]) == snapshot
    saved = system.processor.store.set_item_uploader_role(
        result["job_id"], blocked[0]["item_id"], second_role["role_id"], 0,
        actor="synthetic-reviewer", source_action="new",
    )
    assert saved["requeued"]
    assert system.processor.run_once()
    published = [item for item in receipt_items(system, second["receipt"]) if item["published"]]
    assert len(published) == 1
    assert published[0]["knowledge_id"] != original["knowledge_id"]
    assert published[0]["role_id"] == second_role["role_id"]
    assert system.knowledge.store.snapshot(original["knowledge_id"]) == snapshot
    record = system.knowledge.get(published[0]["knowledge_id"])
    assert record["content"] == changes["content"]
    assert record["evidence"]["uploader_position"] == changes["position"]
    assert record["evidence"]["uploader_position_source"] == "employee_declared"
    assert system.knowledge.store.published_count() == 2


def test_shared_connection_content_change_without_revision_does_not_create_a_draft(system):
    first = submit(system, department="测试部门")
    process(system)
    original = next(item for item in receipt_items(system, first["receipt"]) if item["published"])
    snapshot = system.knowledge.store.snapshot(original["knowledge_id"])
    changed = submit(system, department="测试部门", content=PAYLOAD["content"] + "\n补充核对资料编号。\n")
    process(system)
    blocked = [item for item in receipt_items(system, changed["receipt"]) if item["status"] == "needs_review"]
    assert blocked
    assert all(item["knowledge_id"] is None and item["revision"] is None for item in blocked)
    assert system.knowledge.store.snapshot(original["knowledge_id"]) == snapshot
    assert system.knowledge.store.list_assets()[0]["latest_revision"] == 1
    assert system.knowledge.store.published_count() == 1
    assert len(system.index.calls) == 1


def test_later_alias_cannot_appear_as_confirmation_of_existing_publication(system):
    from rag_app.knowledge_item_assignments import declaration_key

    first = submit(system, department="测试部门")
    process(system)
    result = submission_status(system.receipts, "owner:a", first["receipt"]["receipt_id"])["processing"]
    item = next(item for item in result["items"] if item["published"])
    snapshot = system.knowledge.store.snapshot(item["knowledge_id"])
    other_role = system.knowledge.store.register_role("另外的部门", "另外的岗位")
    with system.processor.store._connection(write=True) as connection:
        connection.execute('''INSERT INTO knowledge_role_aliases
            (declaration_key,department,position,role_id,actor,updated_at)
            VALUES (?,?,?,?,?,?)''', (declaration_key("测试部门", PAYLOAD["position"]), "测试部门",
            PAYLOAD["position"], other_role["role_id"], "synthetic-reviewer", 1))
    displayed = system.processor.store.get_item_uploader_role(result["job_id"], item["item_id"])
    assert displayed["role"] is None or displayed["role"]["role_id"] == item["role_id"]
    assert displayed["confirmation_source"] != "alias"
    assert displayed["eligible"] is False
    assert system.knowledge.store.snapshot(item["knowledge_id"]) == snapshot
