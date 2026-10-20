"""Employee-role assignment through the real receipt worker, with temporary storage only."""
import pytest

from rag_app.knowledge_native import TextSubmission, source_package
from test_knowledge_native import PAYLOAD
from test_knowledge_processing import BODY, system
from test_knowledge_processing_phase3 import enable, intake, package, receipt_job

pytestmark = pytest.mark.offline


def published_record(system, receipt):
    result = receipt_job(system, receipt)
    assert result['status'] == 'completed', result
    item = next(item for item in result['items'] if item['published'])
    return system.catalogue.get_published(item['knowledge_id'])


def test_old_skill_position_binds_existing_role_and_preserves_declaration(system):
    index = enable(system)
    role = system.catalogue.register_role('总经办', 'AI架构师')
    receipt = intake(system, package(**{'上传员工岗位': '  AI架构师  '}))
    assert system.service.run_once()
    record = published_record(system, receipt)
    assert record['role_id'] == role['role_id']
    assert (record['department'], record['role_name']) == ('总经办', 'AI架构师')
    assert record['evidence']['source_uploader_position'].strip() == 'AI架构师'
    assert system.catalogue.published_uploader_position(record).strip() == 'AI架构师'
    assert system.catalogue.roles()[0]['total'] == 1
    assert len(index.calls) == 1


@pytest.mark.parametrize('declaration', [{'上传员工岗位': 'AI架构师'}, {}])
def test_owned_source_update_keeps_role_when_old_call_omits_role_fields(system, declaration):
    enable(system)
    role = system.catalogue.register_role('总经办', 'AI架构师')
    first = intake(system, package(**{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    original = published_record(system, first)
    # A later same-name role must not undo this source's earlier assignment.
    system.catalogue.register_role('研发部', 'AI架构师')
    second = intake(system, package(body=BODY + '\n补充：交接前检查修订日期。', base=1, **declaration))
    assert system.service.run_once()
    updated = published_record(system, second)
    assert updated['knowledge_id'] == original['knowledge_id']
    assert updated['revision'] == 2 and updated['role_id'] == role['role_id']
    assert updated['department'] == '总经办'


def test_department_disambiguates_same_position_and_duplicate_skips_index(system):
    index = enable(system)
    role = system.catalogue.register_role('总经办', 'AI架构师')
    system.catalogue.register_role('研发部', 'AI架构师')
    data = source_package(TextSubmission(**{**PAYLOAD, 'position': 'AI架构师', 'department': '总经办'}))
    first = intake(system, data)
    assert system.service.run_once()
    record = published_record(system, first)
    assert record['role_id'] == role['role_id']
    assert record['evidence']['source_uploader_department'] == '总经办'
    repeated = intake(system, data)
    assert system.service.run_once()
    duplicate = next(item for item in receipt_job(system, repeated)['items'] if item['knowledge_id'])
    assert duplicate['status'] == 'duplicate' and duplicate['revision'] == 1
    assert system.catalogue.published_count() == 1 and len(index.calls) == 1


@pytest.mark.parametrize('position,department,duplicate_name,reason', [
    ('AI架构师', None, True, 'uploader_role_ambiguous'),
    ('AI架构师', '销售部', False, 'uploader_department_mismatch'),
    ('AI架构帅', None, False, 'uploader_role_not_found'),
    ('总经办', None, False, 'uploader_position_is_department'),
])
def test_unresolved_position_keeps_verified_receipt_without_guessing_or_indexing(
        system, position, department, duplicate_name, reason):
    index = enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    if duplicate_name:
        system.catalogue.register_role('研发部', 'AI架构师')
    roles = system.catalogue.roles()
    data = source_package(TextSubmission(**{**PAYLOAD, 'position': position, 'department': department}))
    receipt = intake(system, data)
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    assert result['status'] == 'needs_review'
    assert any(item['reason_codes'] == [reason] for item in result['items'])
    assert system.receipts.get(receipt['principal'], receipt['receipt_id'])['status'] == 'verified'
    assert system.catalogue.list_assets() == [] and not index.calls
    assert system.catalogue.roles() == roles


def test_changed_position_cannot_rebind_owned_source(system):
    index = enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    system.catalogue.register_role('运营部', 'AI策划')
    first = intake(system, package(**{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    before = published_record(system, first)
    changed = intake(system, package(body=BODY + '\n新的整理方法。', base=1, **{'上传员工岗位': 'AI策划'}))
    assert system.service.run_once()
    result = receipt_job(system, changed)
    assert result['status'] == 'needs_review'
    assert any(item['reason_codes'] == ['uploader_role_conflict'] for item in result['items'])
    assert system.catalogue.get_published(before['knowledge_id']) == before
    assert len(index.calls) == 1


def test_stale_baseline_and_other_employee_cannot_overwrite_role_bound_source(system):
    index = enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    data = package(**{'上传员工岗位': 'AI架构师'})
    first = intake(system, data)
    assert system.service.run_once()
    before = published_record(system, first)
    second = intake(system, package(body=BODY + '\n当前确认的方法。', base=1, **{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    current = published_record(system, second)
    stale = intake(system, package(body=BODY + '\n过期基线的改动。', base=1, **{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    assert any('source_base_revision_conflict' in item['reason_codes'] for item in receipt_job(system, stale)['items'])
    other = intake(system, package(body=BODY + '\n其他员工尝试覆盖。', base=2, **{'上传员工岗位': 'AI架构师'}), principal='user:b')
    assert system.service.run_once()
    assert receipt_job(system, other)['status'] == 'needs_review'
    unchanged = system.catalogue.get_published(before['knowledge_id'])
    assert (unchanged['revision'], unchanged['role_id'], unchanged['content']) == (2, current['role_id'], current['content'])
    assert system.catalogue.published_count() == 1 and len(index.calls) == 2


@pytest.mark.parametrize('old_unassigned', [False, True])
def test_interrupted_worker_resumes_frozen_assignment_without_matching_again(system, monkeypatch, old_unassigned):
    from rag_app.knowledge_store import KnowledgeStore

    index = enable(system)
    role = system.catalogue.register_role('总经办', 'AI架构师')
    receipt = intake(system, package(**{'上传员工岗位': 'AI架构师'}))
    index.failure = RuntimeError('offline vector service unavailable')
    with monkeypatch.context() as old:
        if old_unassigned:
            old.setattr(KnowledgeStore, 'bind_uploader_role', lambda self, connection, entry, previous=None, **kwargs: entry)
        assert system.service.run_once()
    pending = receipt_job(system, receipt)
    item = next(item for item in pending['items'] if item['knowledge_id'])
    frozen = system.catalogue.snapshot(item['knowledge_id'], item['revision'])
    assert frozen.get('role_id') == (None if old_unassigned else role['role_id'])
    assert frozen['status'] == 'draft'
    system.catalogue.register_role('研发部', 'AI架构师')
    index.failure = None
    system.now[0] += 301
    assert system.service.run_once()
    published = published_record(system, receipt)
    assert published.get('role_id') == frozen.get('role_id')
    assert published['payload_hash'] == frozen['payload_hash'] and published['revision'] == 1
    assert published['evidence']['uploader_attribution']['receipt_id'] == receipt['receipt_id']
