"""Reviewer-selected employee roles, tested against isolated receipts and storage."""
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from rag_app.knowledge_processing import ProcessingError, ProcessingService, ProcessingStore
from rag_app.knowledge_native import TextSubmission, source_package
from rag_app.knowledge_processing_api import get_processing_store, router
from test_knowledge_native import PAYLOAD
from test_knowledge_processing import BODY, system
from test_knowledge_processing_api import BASE, DASH, READ, REVIEW, SELF, TOKEN, headers
from test_knowledge_processing_phase3 import enable, intake, package, receipt_job
from test_knowledge_uploader_roles import published_record
from test_knowledge_roles import role_package

pytestmark = pytest.mark.offline


def blocked(system, *, principal='user:a', source='role-selection', body=BODY):
    receipt = intake(system, package(body, **{'上传员工岗位': 'SSC'}), principal=principal, source=source)
    assert system.service.run_once()
    return receipt, receipt_job(system, receipt)


def select(system, job, role, version=0):
    return system.store.set_uploader_role(job['job_id'], role['role_id'], version, actor='reviewer')


def test_selected_role_requeues_original_job_preserves_receipt_and_publishes_once(system):
    index = enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    receipt, before = blocked(system)
    assert before['counts']['draft'] == 0
    assert system.store.get_uploader_role(before['job_id']) == dict(role=None, version=0, updated_at=None, requeued=False)
    saved = select(system, before, role)
    assert saved['role'] == {key: role[key] for key in ('role_id', 'department', 'name')}
    assert saved['version'] == 1 and saved['requeued']
    assert select(system, before, role) == saved  # Exact HTTP retry, including while queued.
    after = system.store.get_job(before['job_id'], receipt['principal'])
    assert after['status'] == 'queued' and after['receipt_id'] == before['receipt_id']
    selected = next(item for item in after['items'] if item['role_assignment'])
    assert selected['role_assignment']['role']['role_id'] == role['role_id']
    assert selected['current_reason_codes'] == [] and selected['rechecking']
    assert not after['attention_required']
    assert system.store.list_jobs(receipt['principal'], status='attention')['total'] == 0
    assert system.store.stats(receipt['principal'])['attention_jobs'] == 0
    assert [{key: value for key, value in item.items() if key not in {'role_assignment', 'current_reason_codes', 'rechecking'}} for item in after['items']] == [
        {key: value for key, value in item.items() if key not in {'role_assignment', 'current_reason_codes', 'rechecking'}} for item in before['items']]
    assert {e['event_id'] for e in before['events']} <= {e['event_id'] for e in after['events']}
    assert system.service.run_once()
    record = published_record(system, receipt)
    assert record['role_id'] == role['role_id']
    assert record['evidence']['source_uploader_position'] == 'SSC'
    assert system.catalogue.published_uploader_position(record) == 'SSC'
    assert 'uploader_role_confirmation' not in record['evidence']
    with system.store._connection() as connection:
        confirmed = connection.execute('SELECT * FROM knowledge_item_role_assignments').fetchone()
        assert confirmed['version'] == 1 and confirmed['confirmation_source'] == 'manual'
    assert not system.service.run_once() and len(index.calls) == 1
    assert system.receipts.get(receipt['principal'], receipt['receipt_id'])['status'] == 'verified'
    assert select(system, before, role) == saved
    events = receipt_job(system, receipt)['events']
    assert len([e for e in events if e['event_type'] == 'item_uploader_role_selected']) == 1
    assert next(e for e in events if e['event_type'] == 'item_uploader_role_selected')['actor'] == 'reviewer'
    restarted = ProcessingStore(system.store.path, clock=system.store.clock)
    assert restarted.get_uploader_role(before['job_id']) == {**saved, 'requeued': False}


def test_selection_is_item_scoped_and_cannot_reassign_existing_source(system):
    index = enable(system)
    old_role = system.catalogue.register_role('共享中心', '流程专员')
    new_role = system.catalogue.register_role('运营部', 'AI策划')
    receipt, initial = blocked(system)
    select(system, initial, old_role)
    assert system.service.run_once()
    original = published_record(system, receipt)
    with pytest.raises(ProcessingError, match='uploader_role_assignment_locked'):
        select(system, initial, new_role, 1)
    assert system.catalogue.get_published(original['knowledge_id']) == original
    update = intake(system, package(BODY + '\n增加复核步骤。', base=1, **{'上传员工岗位': 'SSC'}), source=receipt['source_id'])
    assert system.service.run_once()
    assert published_record(system, update)['role_id'] == old_role['role_id']
    # Missing department cannot become a broad rule for everyone sharing this principal.
    system.store = ProcessingStore(system.store.path, clock=system.store.clock)
    system.service = ProcessingService(system.store, system.objects, artifacts=system.service.artifacts,
                                       draft_objects=system.drafts, auto_publish=True, knowledge_index=index)
    future = intake(system, package(BODY + '\n新项目的工作流程。', **{'上传员工岗位': 'SSC', '名称': '新项目方法'}), source='future-source')
    assert system.service.run_once()
    assert receipt_job(system, future)['status'] == 'needs_review'
    assert any('uploader_role_not_found' in item['reason_codes'] for item in receipt_job(system, future)['items'])
    other, other_job = blocked(system, principal='user:b')
    assert other_job['status'] == 'needs_review'
    assert system.store.get_uploader_role(other_job['job_id'])['version'] == 0
    assert any('uploader_role_not_found' in i['reason_codes'] for i in other_job['items'])


@pytest.mark.parametrize('position,department,reason', [
    ('流程专员', None, 'uploader_role_ambiguous'),
    ('共享中心', None, 'uploader_position_is_department'),
    ('流程专员', '不存在的部门', 'uploader_department_mismatch'),
])
def test_explicit_selection_resolves_other_unambiguous_role_failures(system, position, department, reason):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    if reason == 'uploader_role_ambiguous':
        system.catalogue.register_role('运营部', '流程专员')
    data = source_package(TextSubmission(**{**PAYLOAD, 'position': position, 'department': department}))
    receipt = intake(system, data)
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    assert any(item['reason_codes'] == [reason] for item in job['items'])
    assert select(system, job, role)['requeued']
    assert system.service.run_once()
    record = published_record(system, receipt)
    assert record['role_id'] == role['role_id']
    assert record['evidence']['source_uploader_position'] == position
    if department:
        assert record['evidence']['source_uploader_department'] == department
    # The employee need not rewrite the original declaration on later revisions.
    updated = source_package(TextSubmission(**{**PAYLOAD, 'position': position, 'department': department,
                                               'base_revision': 1, 'content': PAYLOAD['content'] + '\n补充交接检查。'}))
    next_receipt = intake(system, updated)
    assert system.service.run_once()
    next_record = published_record(system, next_receipt)
    assert next_record['knowledge_id'] == record['knowledge_id'] and next_record['revision'] == 2
    assert next_record['role_id'] == role['role_id']


@pytest.mark.parametrize('body,reason', [
    (BODY + '\n[背景资料](missing.md)', 'unresolved_reference'),
    (BODY + '\n运行 `scripts/missing.py`。', 'script_dependency_not_provided'),
])
def test_selecting_role_does_not_requeue_or_bypass_missing_materials(system, body, reason):
    index = enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    receipt, before = blocked(system, body=body)
    assert any(reason in i['reason_codes'] for i in before['items'])
    assert not select(system, before, role)['requeued']
    assert [{key: value for key, value in item.items() if key not in {'role_assignment', 'current_reason_codes', 'rechecking'}} for item in receipt_job(system, receipt)['items']] == [
        {key: value for key, value in item.items() if key not in {'role_assignment', 'current_reason_codes', 'rechecking'}} for item in before['items']]
    assert receipt_job(system, receipt)['status'] == 'needs_review'
    assert not system.service.run_once() and not index.calls
    future = intake(system, package(body, **{'上传员工岗位': 'SSC'}), source='future-missing')
    assert system.service.run_once()
    assert receipt_job(system, future)['status'] == 'needs_review'
    assert not index.calls and system.catalogue.published_count() == 0


def test_selection_versions_concurrent_retries_and_busy_jobs(system):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    other = system.catalogue.register_role('运营部', 'AI策划')
    _, job = blocked(system, body=BODY + '\n[附件](missing.md)')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: select(system, job, role), range(2)))
    assert results[0] == results[1] and results[0]['version'] == 1
    with pytest.raises(ProcessingError, match='uploader_role_version_conflict'):
        select(system, job, other)
    with pytest.raises(ProcessingError, match='uploader_role_not_found'):
        system.store.set_uploader_role(job['job_id'], 'role_' + '0' * 24, 1, actor='reviewer')
    assert system.store.get_uploader_role(job['job_id'])['version'] == 1
    # Different choices racing on the same observed version: exactly one wins.
    def race(choice):
        try:
            return select(system, job, choice, 1)
        except ProcessingError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [role, other]))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert 'uploader_role_version_conflict' in results
    pending = intake(system, source='not-yet-processed')
    system.store.enqueue_verified()
    busy = receipt_job(system, pending)
    for state in ('queued', 'running'):
        if state == 'running':
            assert system.store.claim()
        with pytest.raises(ProcessingError, match='uploader_role_job_busy'):
            select(system, busy, role, 2)


def test_existing_role_conflict_is_not_requeued_by_default_selection(system):
    enable(system)
    old_role = system.catalogue.register_role('总经办', 'AI架构师')
    new_role = system.catalogue.register_role('运营部', 'AI策划')
    first = intake(system, package(**{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    original = published_record(system, first)
    changed = intake(system, package(BODY + '\n职责变化。', base=1, **{'上传员工岗位': 'AI策划'}))
    assert system.service.run_once()
    job = receipt_job(system, changed)
    assert any(i['reason_codes'] == ['uploader_role_conflict'] for i in job['items'])
    assert not select(system, job, new_role)['requeued']
    assert not system.service.run_once()
    assert system.catalogue.get_published(original['knowledge_id']) == original


def test_requeued_role_failure_still_checks_publication_rules(system):
    index = enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    receipt = intake(system, package(**{'上传员工岗位': 'SSC', '共享范围': '未确认'}))
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    assert any(i['reason_codes'] == ['uploader_role_not_found'] for i in job['items'])
    assert select(system, job, role)['requeued']
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    assert result['status'] == 'needs_review'
    assert any('sharing_scope_unconfirmed' in i['reason_codes'] for i in result['items'])
    assert not index.calls and system.catalogue.published_count() == 0


def test_remembered_role_does_not_migrate_existing_unassigned_source(system):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    first = intake(system)
    assert system.service.run_once()
    original = published_record(system, first)
    assert not original.get('role_id')
    with pytest.raises(ProcessingError, match='uploader_role_assignment_locked'):
        select(system, receipt_job(system, first), role)
    updated = intake(system, package(BODY + '\n历史资料正常修订。', base=1))
    assert system.service.run_once()
    record = published_record(system, updated)
    assert record['revision'] == 2 and not record.get('role_id')


def test_explicit_new_source_role_does_not_inherit_another_items_selection(system):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    other = system.catalogue.register_role('运营部', '策划')
    original_receipt, job = blocked(system)
    select(system, job, role)
    assert system.service.run_once()
    original = published_record(system, original_receipt)
    conflicting = intake(system, role_package(other, body=BODY + '\n明确的新资料。'), source='explicit-other-role')
    assert system.service.run_once()
    assert published_record(system, conflicting)['role_id'] == other['role_id']
    matching = intake(system, role_package(role, body=BODY + '\n同岗的另一个流程。'), source='explicit-same-role')
    assert system.service.run_once()
    assert published_record(system, matching)['role_id'] == role['role_id']
    assert system.catalogue.get_published(original['knowledge_id']) == original


def test_old_schema_reads_do_not_migrate_and_explicit_initialization_enables_writes(system):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    _, job = blocked(system)
    with system.store._connection(write=True) as connection:
        connection.execute('DROP TABLE knowledge_item_role_assignments')
    assert system.store.get_uploader_role(job['job_id'])['version'] == 0
    future, _ = blocked(system, source='old-schema')
    with pytest.raises(ProcessingError, match='uploader_role_schema_required'):
        select(system, job, role)
    with system.store._connection() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_item_role_assignments'").fetchone() is None
    system.store.initialize_role_assignments(apply=True)
    assert select(system, job, role)['requeued']


def test_role_selection_api_requires_review_and_never_accepts_client_identity(system, monkeypatch):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    _, job = blocked(system)
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_PROCESSING_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_TOKEN', TOKEN)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_processing_store] = lambda: system.store
    path = BASE + '/jobs/' + job['job_id'] + '/uploader-role'
    body = dict(role_id=role['role_id'], expected_version=0)
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        for permission in (SELF, READ, DASH):
            assert client.get(path, headers=headers(permission)).status_code == 403
            assert client.post(path, headers=headers(permission), json=body).status_code == 403
        for extra in ({'principal': 'user:b'}, {'actor': 'spoof'}, {'expected_version': True},
                      {'expected_version': '0'}, {'expected_version': -1}, {'expected_version': 2147483648}, {'role_id': 'SSC'}):
            assert client.post(path, headers=headers(REVIEW), json={**body, **extra}).status_code == 422
        response = client.post(path, headers=headers(REVIEW, principal='reviewer'), json=body)
        assert response.status_code == 200 and response.json()['requeued']
        assert set(response.json()) == {'role', 'version', 'updated_at', 'requeued'}
        assert 'principal' not in response.text and 'actor' not in response.text
        assert client.get(path, headers=headers(REVIEW)).json()['role'] == response.json()['role']
        assert client.post(path, headers=headers(REVIEW), json={**body, 'expected_version': 2147483647}).status_code == 409
        assert client.get(BASE + '/jobs/' + '0' * 32 + '/uploader-role', headers=headers(REVIEW)).status_code == 404
    with system.store._connection() as connection:
        row = connection.execute('SELECT item_id,actor FROM knowledge_item_role_assignments').fetchone()
        assert row['actor'] == 'reviewer'
        assert connection.execute('SELECT COUNT(*) FROM knowledge_uploader_roles').fetchone()[0] == 0
