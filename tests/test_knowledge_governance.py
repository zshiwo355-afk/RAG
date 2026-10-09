from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from rag_app.knowledge_governance import GovernanceStore, fingerprint_entry
from rag_app.knowledge_receipts import LeaseLost
from rag_app.knowledge_store import AutomaticPublicationConflict, KnowledgeStore, normalize_entry


pytestmark = pytest.mark.offline


def entry(**changes):
    return {"knowledge_id": "source_a", "title": "会议准备", "content": "先确定决策目标，再准备证据。",
            "kind": "method", "contributor": "未确认", "sources": [{"name": "本次提交.md"}],
            "evidence": {"source_sharing": "company", "source_complete": True,
                         "source_input": "待讨论问题", "source_output": "证据清单"}, **changes}


@pytest.fixture
def system(tmp_path):
    catalogue = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    governance = GovernanceStore(catalogue)
    governance.initialize()
    return SimpleNamespace(catalogue=catalogue, governance=governance)


def submit(system, number, document=None, *, principal="user:a", source_id="agent:source",
           source_identity="asset:1", asset=None, base_revision=None, guard=None):
    document = document or entry()
    return system.governance.import_asset(document, {"attachment_sha256": []} if asset is None else asset,
        item_id="item" + str(number), receipt_id="receipt" + str(number), principal=principal,
        source_id=source_id, source_identity=source_identity, base_revision=base_revision, guard=guard)


def publish(system, result):
    record = result["record"]
    return system.catalogue.publish(record["knowledge_id"], record["revision"],
        expected_generation=result["expected_generation"], confirmed_by="test-reviewer")


def counts(system):
    with sqlite3.connect(system.catalogue.path) as connection:
        return tuple(connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                     for name in ("knowledge_assets", "knowledge_revisions", "knowledge_submissions"))


def test_same_source_reupload_preserves_payload_but_records_each_submission(system):
    first = submit(system, 1, entry(evidence={"receipt_id": "receipt1", "source_complete": True}))
    second = submit(system, 2, entry(sources=[{"name": "重新命名.md"}],
                                   evidence={"receipt_id": "receipt2", "source_complete": True}))
    assert counts(system) == (1, 1, 2)
    assert second["disposition"] == "duplicate"
    assert second["record"]["payload_hash"] == first["record"]["payload_hash"]
    assert system.catalogue.snapshot("source_a")["evidence"]["receipt_id"] == "receipt1"
    assert second["record"]["sources"] == first["record"]["sources"]


def test_same_item_retry_pins_generation_and_conflicting_request_is_rejected(system):
    first = submit(system, 1)
    publish(system, first)
    second = submit(system, 1)
    assert second["expected_generation"] == first["expected_generation"] == 1
    assert second["record"]["status"] == "published"
    assert counts(system) == (1, 1, 1)
    with pytest.raises(ValueError, match="submission_idempotency_conflict"):
        submit(system, 1, entry(content="不同请求不能复用item ID"))
    with pytest.raises(ValueError, match="submission_idempotency_conflict"):
        submit(system, 1, principal="user:b")


def test_changed_source_requires_base_and_replay_never_rewinds(system):
    first = submit(system, 1)
    publish(system, first)
    changed = submit(system, 2, entry(content="新版本：先收集问题，再筛选证据。"))
    assert changed["disposition"] == "new_revision" and changed["record"]["revision"] == 2
    assert changed["publication_allowed"] is False
    assert changed["reasons"] == ["source_base_revision_required"]
    assert system.catalogue.get_published("source_a")["revision"] == 1
    replay = submit(system, 3)
    assert replay["disposition"] == "old_replay" and replay["record"]["revision"] == 1
    assert replay["publication_allowed"] is False
    assert counts(system) == (1, 2, 3)
    assert system.catalogue.snapshot("source_a")["revision"] == 2


def test_matching_base_allows_further_rules_but_stale_base_does_not(system):
    first = submit(system, 1)
    publish(system, first)
    second = submit(system, 2, entry(content="经更新的第二版"), base_revision=1)
    assert second["record"]["revision"] == 2 and second["publication_allowed"] is True
    third = submit(system, 3, entry(content="来源落后仍保留为第三版候选"), base_revision=1)
    assert third["record"]["revision"] == 3
    assert third["reasons"] == ["source_base_revision_conflict"] and not third["publication_allowed"]


def test_new_source_cannot_claim_a_nonexistent_baseline(system):
    candidate = submit(system, 1, base_revision=1)
    assert candidate["reasons"] == ["source_base_revision_conflict"]
    assert candidate["record"]["revision"] == 1 and not candidate["publication_allowed"]


def test_new_submission_can_acknowledge_current_draft_but_item_retry_cannot(system):
    first = submit(system, 1)
    publish(system, first)
    changed = entry(content="没有基线的更新稿")
    unconfirmed = submit(system, 2, changed)
    assert unconfirmed["record"]["revision"] == 2 and not unconfirmed["publication_allowed"]
    assert submit(system, 3, changed, base_revision=1)["publication_allowed"] is False
    with pytest.raises(ValueError, match="submission_idempotency_conflict"):
        submit(system, 2, changed, base_revision=2)
    confirmed = submit(system, 4, changed, base_revision=2)
    assert confirmed["record"]["revision"] == 2
    assert confirmed["publication_allowed"] is True and confirmed["reasons"] == []
    assert confirmed["expected_generation"] == system.catalogue.snapshot("source_a")["generation"]
    assert counts(system) == (1, 2, 4)
    assert submit(system, 2, changed)["publication_allowed"] is False


def test_baseline_acknowledgement_keeps_other_conflicts_and_never_revives_withdrawn(system):
    first = submit(system, 1)
    publish(system, first)
    other = submit(system, 2, entry(knowledge_id="source_b", content="已经发布的相反做法"), principal="user:b")
    publish(system, other)
    changed = entry(content="A的下一版")
    unconfirmed = submit(system, 3, changed)
    assert set(unconfirmed["reasons"]) == {"possible_content_conflict", "source_base_revision_required"}
    confirmed = submit(system, 4, changed, base_revision=2)
    assert confirmed["reasons"] == ["possible_content_conflict"] and not confirmed["publication_allowed"]
    publish(system, confirmed)
    system.catalogue.withdraw("source_a")
    withdrawn = submit(system, 5, changed, base_revision=2)
    assert withdrawn["publication_allowed"] is False


def test_repeated_item_ignores_workflow_annotations_without_refreshing_pin(system):
    first = submit(system, 1)
    publish(system, first)
    updated = entry(evidence={**entry()["evidence"], "submitter_authenticated": True,
                             "automatic_rule_reasons": ["sharing_policy_required"], "auto_publish": True})
    replay = submit(system, 1, updated)
    assert replay["expected_generation"] == first["expected_generation"]
    assert counts(system) == (1, 1, 1)


def test_explicit_base_cannot_republish_a_withdrawn_current_revision(system):
    first = submit(system, 1)
    publish(system, first)
    system.catalogue.withdraw("source_a")
    repeated = submit(system, 2, base_revision=1)
    assert repeated["record"]["status"] == "withdrawn"
    assert repeated["reasons"] == ["previously_withdrawn_revision"]
    assert repeated["publication_allowed"] is False


@pytest.mark.parametrize("changes", [
    {"title": "相同正文的新标题"}, {"kind": "case"}, {"contributor": "另一个贡献者"},
    {"department": "研发"}, {"scenarios": ["新人上手"]},
    {"evidence": {**entry()["evidence"], "source_output": "不同输出"}},
])
def test_business_metadata_changes_create_a_source_revision(system, changes):
    submit(system, 1)
    updated = submit(system, 2, entry(**changes), base_revision=1)
    assert updated["record"]["revision"] == 2 and updated["publication_allowed"] is True


def test_private_equal_content_is_isolated_even_if_caller_reuses_knowledge_id(system):
    first = submit(system, 1)
    second = submit(system, 2, principal="user:b")
    assert second["disposition"] == "new"
    assert second["record"]["knowledge_id"] != first["record"]["knowledge_id"]
    assert second["record"]["sources"] == entry()["sources"]
    assert second["reasons"] == [] and second["duplicate_of_published"] is False
    assert counts(system) == (2, 2, 2)


def test_published_exact_match_reuses_revision_without_transferring_update_rights(system):
    first = submit(system, 1)
    publish(system, first)
    copy = submit(system, 2, entry(knowledge_id="source_b"), principal="user:b")
    assert copy["disposition"] == "duplicate_published" and copy["duplicate_of_published"]
    assert copy["record"]["knowledge_id"] == "source_a"
    assert not copy["publication_allowed"] and counts(system) == (1, 1, 2)
    update = submit(system, 3, entry(knowledge_id="source_b", content="B 的独立变化"), principal="user:b", base_revision=1)
    assert update["record"]["knowledge_id"] == "source_b"
    assert update["record"]["revision"] == 1 and not update["publication_allowed"]
    assert "shared_asset_update_requires_review" in update["reasons"]
    assert system.catalogue.get_published("source_a")["content"] == first["record"]["content"]


def test_withdrawn_publication_and_newer_private_revision_are_not_cross_user_matches(system):
    first = submit(system, 1)
    publish(system, first)
    changed = submit(system, 2, entry(content="只有A可见的新草稿"), base_revision=1)
    copy = submit(system, 3, entry(knowledge_id="source_b", content=changed["record"]["content"]), principal="user:b")
    assert copy["record"]["knowledge_id"] == "source_b" and not copy["duplicate_of_published"]
    system.catalogue.withdraw("source_a")
    another = submit(system, 4, entry(knowledge_id="source_c"), principal="user:c")
    assert another["record"]["knowledge_id"] == "source_c" and not another["duplicate_of_published"]


def test_attachment_bytes_and_unknown_manifest_block_false_duplicates(system):
    attachment = hashlib.sha256(b"attachment v1").hexdigest()
    first = submit(system, 1, asset={"attachment_sha256": [attachment]})
    changed = submit(system, 2, asset={"attachment_sha256": [hashlib.sha256(b"v2").hexdigest()]}, base_revision=1)
    assert changed["record"]["revision"] == 2
    unknown = submit(system, 3, asset={}, base_revision=2)
    assert unknown["publication_allowed"] is False and "attachment_manifest_unverified" in unknown["reasons"]
    another_unknown = submit(system, 4, asset={}, base_revision=3,
                             document=entry(evidence={**entry()["evidence"], "receipt_id": "receipt4"}))
    assert another_unknown["disposition"] == "new_revision"
    assert not another_unknown["publication_allowed"]
    assert first["record"]["revision"] == 1


def test_baseline_147_legacy_assets_is_additive_unknown_and_never_reads_objects(tmp_path):
    catalogue = KnowledgeStore(tmp_path / "legacy.sqlite3")
    for index in range(147):
        value = catalogue.import_draft(entry(knowledge_id="legacy_" + str(index), title="历史方法" + str(index)))
        catalogue.publish(value["knowledge_id"], 1, expected_generation=1, confirmed_by="legacy-reviewer")

    def snapshot():
        with sqlite3.connect(catalogue.path) as connection:
            return [connection.execute("SELECT * FROM " + table + " ORDER BY knowledge_id").fetchall()
                    for table in ("knowledge_assets", "knowledge_revisions")]

    original = snapshot()
    governance = GovernanceStore(catalogue)
    assert governance.initialize()["baselined_revisions"] == 147
    assert governance.initialize()["baselined_revisions"] == 0
    assert snapshot() == original
    with sqlite3.connect(catalogue.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_revision_fingerprints WHERE attachment_hash IS NULL").fetchone()[0] == 147
    system = SimpleNamespace(catalogue=catalogue, governance=governance)
    incoming = submit(system, 1, entry(title="历史方法0"))
    assert "legacy_attachment_manifest_unverified" in incoming["reasons"]
    assert incoming["publication_allowed"] is False and incoming["duplicate_of_published"] is False
    assert catalogue.published_count() == 147


def test_same_public_title_different_content_requires_review(system):
    first = submit(system, 1, entry(title="Meeting  Method"))
    publish(system, first)
    incoming = submit(system, 2, entry(knowledge_id="source_b", title="ＭＥＥＴＩＮＧ method", content="相反做法"), principal="user:b")
    assert incoming["reasons"] == ["possible_content_conflict"]
    assert incoming["publication_allowed"] is False


def publication_check(system, result):
    record = result["record"]
    with system.catalogue._connection(write=True) as connection:
        system.catalogue._lock(connection, "write:knowledge-governance")
        system.catalogue._lock(connection, record["knowledge_id"])
        system.governance.assert_publication_allowed(connection, record["knowledge_id"], record["revision"])


def test_two_private_equal_candidates_cannot_both_publish(system):
    first = submit(system, 1)
    second = submit(system, 2, entry(knowledge_id="source_b"), principal="user:b")
    assert first["publication_allowed"] and second["publication_allowed"]
    publication_check(system, first)
    publish(system, first)
    with pytest.raises(AutomaticPublicationConflict, match="published_duplicate_before_publication"):
        publication_check(system, second)
    assert system.catalogue.published_count() == 1
    assert system.catalogue.snapshot("source_b")["status"] == "draft"


def test_publication_rechecks_late_legacy_writer_without_sidecar_mutation(system):
    candidate = submit(system, 1)
    legacy = system.catalogue.import_draft(entry(knowledge_id="late_legacy", title="不同标题但同样正文"))
    system.catalogue.publish("late_legacy", 1, expected_generation=legacy["generation"], confirmed_by="legacy-reviewer")
    with pytest.raises(AutomaticPublicationConflict, match="possible_content_conflict"):
        publication_check(system, candidate)
    with sqlite3.connect(system.catalogue.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_revision_fingerprints WHERE knowledge_id='late_legacy'").fetchone()[0] == 0


def test_publication_rejects_unknown_attachments_and_title_conflict(system):
    unknown = submit(system, 1, asset={})
    with pytest.raises(AutomaticPublicationConflict, match="attachment_manifest_unverified"):
        publication_check(system, unknown)
    candidate = submit(system, 2, entry(knowledge_id="source_b", content="另一个正文"), principal="user:b")
    publish(system, unknown)
    with pytest.raises(AutomaticPublicationConflict, match="possible_content_conflict"):
        publication_check(system, candidate)


def test_guard_failure_rolls_back_catalogue_and_submission(system):
    def lost(connection):
        raise LeaseLost()

    with pytest.raises(LeaseLost):
        submit(system, 1, guard=lost)
    assert counts(system) == (0, 0, 0)
    submit(system, 1)
    with pytest.raises(LeaseLost):
        submit(system, 1, guard=lost)
    assert counts(system) == (1, 1, 1)


def test_submission_insert_failure_rolls_back_revision_and_retries_once(system, monkeypatch):
    original = system.governance._execute

    def fail(connection, sql, params=()):
        if "INSERT INTO knowledge_submissions" in sql:
            raise RuntimeError("simulated crash before submission commit")
        return original(connection, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(system.governance, "_execute", fail)
        with pytest.raises(RuntimeError, match="simulated crash"):
            submit(system, 1)
    assert counts(system) == (0, 0, 0)
    assert submit(system, 1)["record"]["revision"] == 1


def test_concurrent_reuploads_have_one_revision_and_all_receipts(system):
    def run(index):
        local = SimpleNamespace(catalogue=KnowledgeStore(system.catalogue.path))
        local.governance = GovernanceStore(local.catalogue)
        return submit(local, index, entry(evidence={**entry()["evidence"], "receipt_id": "r" + str(index)}))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))
    assert counts(system) == (1, 1, 8)
    assert {result["record"]["revision"] for result in results} == {1}
    assert sum(result["disposition"] == "new" for result in results) == 1


def test_concurrent_updates_from_one_base_allow_only_first_candidate(system):
    submit(system, 1)

    def run(index):
        local = SimpleNamespace(catalogue=KnowledgeStore(system.catalogue.path))
        local.governance = GovernanceStore(local.catalogue)
        return submit(local, index, entry(content="并发变化" + str(index)), base_revision=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [2, 3]))
    assert counts(system) == (1, 3, 3)
    assert sum(result["publication_allowed"] for result in results) == 1
    assert sum("source_base_revision_conflict" in result["reasons"] for result in results) == 1


def test_fingerprint_retains_business_boundaries_and_ignores_receipt_baseline():
    original = normalize_entry(entry())
    fingerprint = fingerprint_entry(original, {"attachment_sha256": []})
    volatile = normalize_entry(entry(evidence={**entry()["evidence"], "receipt_id": "next", "source_base_revision": 7}))
    assert fingerprint_entry(volatile, {"attachment_sha256": []}) == fingerprint
    changed = normalize_entry(entry(evidence={**entry()["evidence"], "source_sharing": "private"}))
    assert fingerprint_entry(changed, {"attachment_sha256": []}) != fingerprint
    assert fingerprint_entry(original)["attachment_hash"] is None
    assert fingerprint_entry(original)["comparison_hash"] != fingerprint["comparison_hash"]


def test_member_manifest_recognizes_main_and_required_unparsed_members():
    digest = hashlib.sha256(b"main").hexdigest()
    asset = {"source_path": "main.md", "members": [{"source_path": "main.md", "source_sha256": digest}]}
    assert fingerprint_entry(normalize_entry(entry()), asset)["attachment_hash"] == fingerprint_entry(
        normalize_entry(entry()), {"attachment_sha256": []})["attachment_hash"]
    asset["issues"] = [{"code": "referenced_material_unparsed"}]
    assert fingerprint_entry(normalize_entry(entry()), asset)["attachment_hash"] is None


@pytest.mark.parametrize("base", [True, 0, -1, "1", 2**63])
def test_rejects_untrusted_baseline_types_without_writes(system, base):
    with pytest.raises(ValueError, match="base_revision"):
        submit(system, 1, base_revision=base)
    assert counts(system) == (0, 0, 0)
