"""Metadata-only role repair through real receipt processing and temporary storage."""
import json
import sqlite3

import pytest

from rag_app.knowledge_role_maintenance import apply_role_assignment, rollback_role_assignment
from rag_app.knowledge_governance import GovernanceStore
from rag_app.knowledge_index import chunk_document
from rag_app.knowledge_store import KnowledgeStore
from test_knowledge_processing import system
from test_knowledge_processing_phase3 import enable, intake, package, receipt_job

pytestmark = pytest.mark.offline


def historical(system, monkeypatch):
    index = enable(system)
    role = system.catalogue.register_role('总经办', 'AI架构师')
    data = package(**{'上传员工岗位': 'AI架构师'})
    receipt = intake(system, data)
    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeStore, 'bind_uploader_role', lambda self, connection, entry, previous=None, **kwargs: entry)
        assert system.service.run_once()
    item = next(item for item in receipt_job(system, receipt)['items'] if item['published'])
    record = system.catalogue.get_published(item['knowledge_id'])
    plan = {'decision': 'proposed', 'approved': True, 'knowledge_id': record['knowledge_id'],
            'expected_revision': record['revision'], 'expected_generation': record['generation'],
            'expected_content_hash': record['content_hash'], 'expected_payload_hash': record['payload_hash'],
            'original_role': dict.fromkeys(('role_id', 'department', 'role_name')),
            'proposed_role': {'role_id': role['role_id'], 'department': role['department'], 'role_name': role['name']},
            'receipt_id': receipt['receipt_id'], 'declared_position': 'AI架构师', 'comment': '审核备注仅存快照'}
    return record, plan, data, index


def test_metadata_repair_preserves_body_vectors_receipt_and_is_reversible(system, monkeypatch, tmp_path):
    before, plan, _, index = historical(system, monkeypatch)
    objects = dict(system.bucket.data)
    journal = tmp_path / 'repair.json'
    preview = apply_role_assignment(system.catalogue, {**plan, 'approved': False})
    assert preview['status'] == 'preview' and not journal.exists()
    assert system.catalogue.get_published(before['knowledge_id']) == before
    result = apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    assert result['status'] == 'applied'
    after = system.catalogue.get_published(before['knowledge_id'])
    assert after['role_id'] == plan['proposed_role']['role_id']
    assert after['generation'] == before['generation'] + 1 and after['revision'] == 1
    changed = {'role_id', 'department', 'role_name', 'payload_hash', 'generation'}
    assert {key: value for key, value in after.items() if key not in changed} == {
        key: value for key, value in before.items() if key not in changed}
    assert system.bucket.data == objects and len(index.calls) == 1
    assert chunk_document(before) == chunk_document(after)
    saved = json.loads(journal.read_text())
    assert saved['before']['submissions'][0]['request_hash'] == saved['after']['submissions'][0]['request_hash']
    assert saved['before']['fingerprint']['attachment_hash'] == saved['after']['fingerprint']['attachment_hash']
    assert system.catalogue.roles()[0]['total'] == 1
    assert rollback_role_assignment(system.catalogue, journal)['status'] == 'preview'
    assert rollback_role_assignment(system.catalogue, journal, execute=True)['status'] == 'rolled_back'
    restored = system.catalogue.get_published(before['knowledge_id'])
    assert restored == {**before, 'generation': after['generation'] + 1}
    assert system.bucket.data == objects and len(index.calls) == 1


def test_repair_preserves_original_retry_and_new_receipt_is_still_duplicate(system, monkeypatch, tmp_path):
    before, plan, data, index = historical(system, monkeypatch)
    journal = tmp_path / 'repair.json'
    apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    saved = json.loads(journal.read_text())
    original = saved['before']['submissions'][0]
    retry = GovernanceStore(system.catalogue).import_asset(before, {'attachment_sha256': []},
        **{key: original[key] for key in ('item_id', 'receipt_id', 'principal', 'source_id', 'source_identity')})
    assert retry['record']['revision'] == 1 and retry['expected_generation'] == original['expected_generation']
    assert retry['record']['role_id'] == plan['proposed_role']['role_id']
    assert not system.service.run_once() and len(index.calls) == 1
    repeated = intake(system, data)
    assert system.service.run_once()
    item = next(item for item in receipt_job(system, repeated)['items'] if item['knowledge_id'])
    assert item['status'] == 'duplicate' and item['revision'] == 1
    assert len(index.calls) == 1 and system.catalogue.get_published(before['knowledge_id'])['generation'] == 3
    # A new submission changes provenance even without a new version: rollback cannot erase it.
    with pytest.raises(ValueError, match='role_repair_snapshot_changed'):
        rollback_role_assignment(system.catalogue, journal, execute=True)


def test_repair_can_be_followed_by_normal_same_source_update(system, monkeypatch, tmp_path):
    before, plan, _, index = historical(system, monkeypatch)
    journal = tmp_path / 'repair.json'
    apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    updated = intake(system, package(body=before['content'] + '\n新增交接检查。', base=1, **{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    item = next(item for item in receipt_job(system, updated)['items'] if item['published'])
    after = system.catalogue.get_published(item['knowledge_id'])
    assert after['revision'] == 2 and after['role_id'] == plan['proposed_role']['role_id']
    assert len(index.calls) == 2
    with pytest.raises(ValueError, match='role_repair_snapshot_changed'):
        rollback_role_assignment(system.catalogue, journal, execute=True)


def database_bytes(system):
    with sqlite3.connect(system.catalogue.path) as connection:
        return '\n'.join(connection.iterdump())


@pytest.mark.parametrize('case,reason', [
    ('unapproved', 'role_repair_not_approved'), ('hold', 'role_repair_not_approved'),
    ('stale_generation', 'role_repair_version_conflict'), ('stale_content', 'role_repair_hash_conflict'),
    ('stale_payload', 'role_repair_hash_conflict'), ('unknown_role', 'role_repair_target_role_mismatch'),
    ('wrong_role_label', 'role_repair_target_role_mismatch'), ('later_revision', 'role_repair_first_revision_only'),
    ('newer_draft', 'role_repair_version_conflict'), ('withdrawn', 'role_repair_version_conflict'),
    ('indexing', 'role_repair_processing_incomplete'), ('missing_item', 'role_repair_processing_incomplete'),
    ('running_job', 'role_repair_processing_incomplete'), ('mismatched_comparison', 'role_repair_fingerprint_mismatch'),
])
def test_unsafe_repairs_fail_without_any_database_or_object_change(system, monkeypatch, tmp_path, case, reason):
    before, plan, _, index = historical(system, monkeypatch)
    if case == 'unapproved':
        plan['approved'] = False
    elif case == 'hold':
        plan['decision'] = 'test_hold'
    elif case == 'stale_generation':
        plan['expected_generation'] += 1
    elif case in {'stale_content', 'stale_payload'}:
        plan['expected_' + ('content' if case == 'stale_content' else 'payload') + '_hash'] = '0' * 64
    elif case == 'unknown_role':
        plan['proposed_role']['role_id'] = 'role_' + '0' * 24
    elif case == 'wrong_role_label':
        plan['proposed_role']['department'] = '其他部门'
    elif case == 'later_revision':
        plan['expected_revision'] = 2
    elif case == 'newer_draft':
        system.catalogue.import_draft({**before, 'content': before['content'] + '\n未发布新稿。'})
    elif case == 'withdrawn':
        system.catalogue.withdraw(before['knowledge_id'])
    else:
        with system.catalogue._connection(write=True) as connection:
            if case == 'indexing':
                connection.execute("UPDATE knowledge_processing_items SET status='indexing' WHERE knowledge_id IS NOT NULL")
            elif case == 'missing_item':
                connection.execute('DELETE FROM knowledge_processing_items WHERE knowledge_id IS NOT NULL')
            elif case == 'running_job':
                connection.execute("UPDATE knowledge_processing_jobs SET status='running'")
            elif case == 'mismatched_comparison':
                connection.execute("UPDATE knowledge_submissions SET comparison_hash=?", ('0' * 64,))
    unchanged, objects = database_bytes(system), dict(system.bucket.data)
    journal = tmp_path / 'repair.json'
    with pytest.raises(ValueError, match=reason):
        apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    assert database_bytes(system) == unchanged and system.bucket.data == objects
    assert not journal.exists() and len(index.calls) == 1


def test_backup_cannot_be_overwritten_and_modified_backup_cannot_restore_content(system, monkeypatch, tmp_path):
    before, plan, _, _ = historical(system, monkeypatch)
    journal = tmp_path / 'repair.json'
    journal.write_text('preserve this file')
    unchanged = database_bytes(system)
    with pytest.raises(FileExistsError):
        apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    assert database_bytes(system) == unchanged and journal.read_text() == 'preserve this file'
    journal.unlink()
    apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    unchanged = database_bytes(system)
    saved = json.loads(journal.read_text())
    record = json.loads(saved['before']['revision']['record_json'])
    record['title'] = '不能通过撤销改标题'
    saved['before']['revision']['record_json'] = json.dumps(record)
    journal.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='role_repair_payload_mismatch'):
        rollback_role_assignment(system.catalogue, journal, execute=True)
    assert database_bytes(system) == unchanged


@pytest.mark.parametrize('change', ['delete', 'rename'])
def test_target_role_is_rechecked_inside_application_transaction(system, monkeypatch, tmp_path, change):
    _, plan, _, _ = historical(system, monkeypatch)
    unchanged = database_bytes(system)
    original_lock = system.catalogue._lock

    def changed_role(connection, knowledge_id):
        original_lock(connection, knowledge_id)
        if knowledge_id == 'write:knowledge-governance':
            connection.execute('DELETE FROM knowledge_roles' if change == 'delete' else "UPDATE knowledge_roles SET name='已改名'")

    monkeypatch.setattr(system.catalogue, '_lock', changed_role)
    journal = tmp_path / 'repair.json'
    with pytest.raises(ValueError, match='role_repair_target_role_mismatch'):
        apply_role_assignment(system.catalogue, plan, backup_path=journal, execute=True)
    assert database_bytes(system) == unchanged and not journal.exists()
