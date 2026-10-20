"""Item attribution and source isolation through the real offline receipt worker."""
import pytest

from rag_app.knowledge_native import TextSubmission, source_package
from test_knowledge_native import PAYLOAD
from test_knowledge_processing import system
from test_knowledge_processing_phase3 import enable, intake, receipt_job
from test_knowledge_uploader_roles import published_record

pytestmark = pytest.mark.offline


def native(system, *, department='品牌部', position='设计师', source='work-20261019-001', **changes):
    data = source_package(TextSubmission(**{**PAYLOAD, 'source_id': source,
        'department': department, 'position': position, **changes}))
    receipt = intake(system, data, source=source)
    assert system.service.run_once()
    return receipt, receipt_job(system, receipt)


def pending(job):
    return next(item for item in job['items'] if item['status'] == 'needs_review')


def test_shared_connection_external_number_does_not_merge_complete_different_declarations(system):
    enable(system)
    leader = system.catalogue.register_role('郑州分公司', '负责人')
    designer = system.catalogue.register_role('品牌部', '设计师')
    first, _ = native(system, department='郑州分公司', position='负责人', title='分公司经营方法')
    original = published_record(system, first)
    second, job = native(system, title='设计师交付方法', content=PAYLOAD['content'] + '\n记录设计交付。')
    item = pending(job)
    assert item['reason_codes'] == ['source_relation_review']
    assert item['knowledge_id'] is None and item['revision'] is None
    saved = system.store.set_item_uploader_role(job['job_id'], item['item_id'], designer['role_id'], 0,
                                               actor='reviewer', source_action='new')
    assert saved['requeued']
    assert system.service.run_once()
    record = published_record(system, second)
    assert record['knowledge_id'] != original['knowledge_id']
    assert original['role_id'] == leader['role_id'] and record['role_id'] == designer['role_id']
    assert system.catalogue.get_published(original['knowledge_id']) == original
    assert system.receipts.get('user:a', second['receipt_id'])['source_id'] == 'work-20261019-001'


def assign(system, job, role, **kwargs):
    item = pending(job)
    return system.store.set_item_uploader_role(job['job_id'], item['item_id'], role['role_id'],
                                               kwargs.pop('version', 0), actor='reviewer', **kwargs)


def test_reviewed_exact_alias_survives_restart_without_shared_principal_default(system):
    from rag_app.knowledge_processing import ProcessingStore, ProcessingService
    index = enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    first, job = native(system, department='原部门', position='SSC')
    saved = assign(system, job, role, remember_rule=True)
    assert saved['remembered'] and saved['rule_status'] == 'saved'
    assert saved['remaining_reasons'] == [] and system.service.run_once()
    assert published_record(system, first)['role_id'] == role['role_id']
    system.store = ProcessingStore(system.store.path, clock=system.store.clock)
    system.service = ProcessingService(system.store, system.objects, artifacts=system.service.artifacts,
        draft_objects=system.drafts, auto_publish=True, knowledge_index=index)
    future, future_job = native(system, department='原部门', position='SSC', source='later', title='另一项目',
                                content=PAYLOAD['content'] + '\n下一项目核对。')
    record = published_record(system, future)
    assert record['role_id'] == role['role_id']
    assert record['evidence']['source_uploader_department'] == '原部门'
    item = next(i for i in future_job['items'] if i['status'] == 'published')
    confirmed = system.store.get_item_uploader_role(future_job['job_id'], item['item_id'])
    assert confirmed['confirmation_source'] == 'alias' and confirmed['version'] == 1
    _, other = native(system, department='其他部门', position='SSC', source='other', title='其他人材料')
    assert pending(other)['reason_codes'] == ['uploader_role_not_found']
    with system.store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM knowledge_uploader_roles').fetchone()[0] == 0


def test_alias_conflict_keeps_first_rule_and_manual_choices_are_item_scoped(system):
    enable(system)
    first_role = system.catalogue.register_role('共享中心', '流程专员')
    other_role = system.catalogue.register_role('运营部', '策划')
    first, one = native(system, department='原部门', position='SSC', source='one')
    second, two = native(system, department='原部门', position='SSC', source='two', title='第二项',
                         content=PAYLOAD['content'] + '\n另一用途。')
    assert assign(system, one, first_role, remember_rule=True)['rule_status'] == 'saved'
    conflict = assign(system, two, other_role, remember_rule=True)
    assert conflict['rule_status'] == 'conflict' and not conflict['remembered']
    assert system.service.run_once() and system.service.run_once()
    assert published_record(system, first)['role_id'] == first_role['role_id']
    assert published_record(system, second)['role_id'] == other_role['role_id']
    with system.store._connection() as connection:
        assert connection.execute('SELECT role_id FROM knowledge_role_aliases').fetchone()['role_id'] == first_role['role_id']


def test_missing_department_cannot_save_broad_alias(system):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    _, job = native(system, department=None, position='SSC')
    result = assign(system, job, role, remember_rule=True)
    assert not result['can_remember'] and not result['remembered'] and result['rule_status'] == 'incomplete'
    with system.store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM knowledge_role_aliases').fetchone()[0] == 0


def test_two_confirmed_sources_can_each_update_with_unique_original_relation(system):
    enable(system)
    leader = system.catalogue.register_role('郑州分公司', '负责人')
    designer = system.catalogue.register_role('品牌部', '设计师')
    first, _ = native(system, department='郑州分公司', position='负责人', title='经营方法', source_locator='work:first')
    original = published_record(system, first)
    second, job = native(system, title='设计方法', source_locator='work:second', content=PAYLOAD['content'] + '\n设计步骤。')
    assign(system, job, designer, source_action='new')
    assert system.service.run_once()
    second_record = published_record(system, second)
    update, _ = native(system, department='郑州分公司', position='负责人', title='经营方法', source_locator='work:first',
                       content=PAYLOAD['content'] + '\n经营复核步骤。', base_revision=1)
    updated = published_record(system, update)
    assert updated['knowledge_id'] == original['knowledge_id'] and updated['revision'] == 2
    update_two, _ = native(system, title='设计方法', source_locator='work:second',
        content=PAYLOAD['content'] + '\n设计步骤。\n设计复核。', base_revision=1)
    updated_two = published_record(system, update_two)
    assert updated_two['knowledge_id'] == second_record['knowledge_id'] and updated_two['revision'] == 2
    assert updated['role_id'] == leader['role_id'] and updated_two['role_id'] == designer['role_id']


def mixed_package():
    import csv
    import io
    import zipfile
    from test_knowledge_processing import BODY, zip_bytes
    from test_knowledge_processing_phase3 import package
    files, rows = {}, []
    for number, position, body in ((1, '正式岗位', BODY), (2, 'SSC', BODY + '\n第二份方法。'),
                                    (3, 'SSC', BODY + '\n[必需文档](missing.md)')):
        raw = package(body, **{'资产编号': 'ITEM-' + str(number), '名称': '材料' + str(number),
                              '正文路径': '正文/' + str(number) + '.md', '上传员工岗位': position})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            rows.extend(csv.DictReader(io.StringIO(archive.read('交付清单.csv').decode())))
            files.update({name: archive.read(name).decode() for name in archive.namelist() if name != '交付清单.csv'})
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files['交付清单.csv'] = output.getvalue()
    return zip_bytes(files)


def test_selecting_one_mixed_item_preserves_published_missing_and_archived_siblings(system):
    from rag_app.knowledge_processing import ProcessingError
    enable(system)
    role = system.catalogue.register_role('部门', '正式岗位')
    receipt = intake(system, mixed_package())
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    target = next(i for i in before['items'] if i['reason_codes'] == ['uploader_role_not_found'])
    siblings = [i for i in before['items'] if i['item_id'] != target['item_id']]
    assert {i['status'] for i in siblings} >= {'published', 'needs_review', 'archived'}
    with pytest.raises(ProcessingError, match='uploader_role_item_required'):
        system.store.get_uploader_role(before['job_id'])
    saved = system.store.set_item_uploader_role(before['job_id'], target['item_id'], role['role_id'], 0, actor='reviewer')
    assert saved['requeued'] and system.service.run_once()
    after = receipt_job(system, receipt)
    assert [i for i in after['items'] if i['item_id'] != target['item_id']] == siblings
    assert next(i for i in after['items'] if i['item_id'] == target['item_id'])['status'] == 'published'
    assert after['status'] == 'needs_review'


def test_confirmed_role_clears_only_resolved_role_reasons_in_saved_item(system):
    import json
    enable(system)
    role = system.catalogue.register_role('部门', '正式岗位')
    receipt = intake(system, mixed_package())
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    target = next(i for i in before['items'] if 'unresolved_reference' in i['reason_codes'])
    siblings = [i for i in before['items'] if i['item_id'] != target['item_id']]
    # Older checks may have recorded both a missing role and a missing attachment.
    with system.store._connection(write=True) as connection:
        connection.execute('UPDATE knowledge_processing_items SET reason_codes=? WHERE item_id=?',
                           (json.dumps(['uploader_role_not_found', 'unresolved_reference']), target['item_id']))
    saved = system.store.set_item_uploader_role(before['job_id'], target['item_id'], role['role_id'], 0, actor='reviewer')
    assert not saved['requeued']
    assert saved['remaining_reasons'] == ['unresolved_reference']
    after = receipt_job(system, receipt)
    changed = next(i for i in after['items'] if i['item_id'] == target['item_id'])
    assert changed['reason_codes'] == ['unresolved_reference']
    assert changed['status'] == 'needs_review' and changed['version'] == target['version'] + 1
    assert [i for i in after['items'] if i['item_id'] != target['item_id']] == siblings
    assert system.store.get_item_uploader_role(before['job_id'], target['item_id'])['remaining_reasons'] == ['unresolved_reference']
    assert after['status'] == 'needs_review' and not system.service.run_once()


@pytest.mark.parametrize('already_processed,change', [
    (False, None), (True, None), (True, 'selected'), (False, 'declaration'),
    (False, 'matching_declaration'), (False, 'ambiguous'),
])
def test_explicit_replacement_preserves_reviewed_roles_and_conflicts(system, already_processed, change):
    import io
    import zipfile
    from test_knowledge_processing import BODY, zip_bytes
    from test_knowledge_processing_phase3 import package, asset_item
    enable(system)
    role = system.catalogue.register_role('品牌部', '设计师')
    other_role = system.catalogue.register_role('运营部', '运营专员')
    original = package(BODY + '\n[必需的核对清单](missing.md)', **{'上传员工岗位': 'SSC'})
    first = intake(system, original)
    assert system.service.run_once()
    job = receipt_job(system, first)
    item = asset_item(job)
    assert item['revision'] is None and 'unresolved_reference' in item['reason_codes']
    saved = assign(system, job, role, remember_rule=False)
    assert not saved['requeued'] and not saved['remembered']
    second_job = None
    if change == 'ambiguous':
        second = intake(system, original)
        assert system.service.run_once()
        second_job = receipt_job(system, second)
        assign(system, second_job, other_role, remember_rule=False)
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    if change in {'declaration', 'matching_declaration'}:
        files['交付清单.csv'] = files['交付清单.csv'].replace(b'SSC', ('运营专员' if change == 'declaration' else '设计师').encode())
    files['正文/missing.md'] = '# 核对清单\n记录来源、验收人和附件完整性。\n'
    replacement = intake(system, zip_bytes(files))
    if already_processed:
        assert system.service.run_once()
        assert asset_item(receipt_job(system, replacement))['reason_codes'] == ['source_relation_review']
        if change == 'selected':
            assign(system, receipt_job(system, replacement), other_role, remember_rule=False)
    item = asset_item(receipt_job(system, first))
    system.store.resolve(job['job_id'], item['item_id'], None, expected_version=item['version'],
        action='replace', reason='补齐附件并保留已确认岗位', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    if second_job:
        second_item = asset_item(system.store.get_job(second_job['job_id'], None))
        system.store.resolve(second_job['job_id'], second_item['item_id'], None, expected_version=second_item['version'],
            action='replace', reason='同份补件存在不同旧岗位选择', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert system.service.run_once()
    result = receipt_job(system, replacement)
    repaired = asset_item(result)
    if change in {'declaration', 'ambiguous'}:
        assert repaired['status'] == 'needs_review'
        assert repaired['reason_codes'] == ['uploader_role_conflict' if change == 'declaration' else 'uploader_role_ambiguous']
        assert system.catalogue.published_count() == 0
        return
    expected = other_role if change == 'selected' else role
    assert repaired['status'] == 'published', repaired['reason_codes']
    assert system.catalogue.get_published(repaired['knowledge_id'])['role_id'] == expected['role_id']
    confirmed = system.store.get_item_uploader_role(result['job_id'], repaired['item_id'])
    assert confirmed['role']['role_id'] == expected['role_id'] and not confirmed['remembered']
    with system.store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM knowledge_role_aliases').fetchone()[0] == 0


def test_saved_choice_retains_conflict_with_published_source_role(system):
    enable(system)
    system.catalogue.register_role('原部门', '原岗位')
    chosen = system.catalogue.register_role('新部门', '新岗位')
    first, _ = native(system, department='原部门', position='原岗位')
    original = published_record(system, first)
    receipt, job = native(system, department='新部门', position='新岗位', base_revision=1,
                          content=PAYLOAD['content'] + '\n职责调整。')
    item = pending(job)
    assert item['reason_codes'] == ['uploader_role_conflict']
    saved = assign(system, job, chosen)
    assert not saved['requeued'] and saved['remaining_reasons'] == ['uploader_role_conflict']
    assert system.store.get_item_uploader_role(job['job_id'], item['item_id'])['remaining_reasons'] == ['uploader_role_conflict']
    after = receipt_job(system, receipt)['items']
    assert [{k: v for k, v in item.items() if k != 'role_assignment'} for item in after] == [
        {k: v for k, v in item.items() if k != 'role_assignment'} for item in job['items']]
    assert next(i for i in after if i['item_id'] == item['item_id'])['role_assignment']['role']['role_id'] == chosen['role_id']
    assert not system.service.run_once() and system.catalogue.get_published(original['knowledge_id']) == original


def test_legacy_migration_is_preview_only_idempotent_and_ignores_tied_choices(system):
    from rag_app.knowledge_processing import ProcessingError
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    other = system.catalogue.register_role('运营部', '策划')
    receipt, job = native(system, department='原部门', position='SSC')
    with system.store._connection(write=True) as connection:
        system.store._event(connection, job['job_id'], 'uploader_role_selected', role['role_id'], 'old-reviewer')
    before = system.store.path.read_bytes()
    preview = system.store.migrate_legacy_item_roles()
    assert preview['counts']['eligible'] == 1 and system.store.path.read_bytes() == before
    applied = system.store.migrate_legacy_item_roles(apply=True)
    assert applied['items'][0]['applied']
    assert system.store.migrate_legacy_item_roles()['counts']['already_migrated'] == 1
    assert system.service.run_once()
    assert published_record(system, receipt)['role_id'] == role['role_id']
    _, tied = native(system, department='原部门', position='SSC', source='tied')
    with system.store._connection(write=True) as connection:
        system.store._event(connection, tied['job_id'], 'uploader_role_selected', role['role_id'], 'old-reviewer')
        system.store._event(connection, tied['job_id'], 'uploader_role_selected', other['role_id'], 'old-reviewer')
    preview = system.store.migrate_legacy_item_roles()
    assert next(i for i in preview['items'] if i['job_id'] == tied['job_id'])['status'] == 'ambiguous'
    with pytest.raises(ProcessingError, match='source_action_not_allowed'):
        assign(system, tied, role, source_action='new')


def test_alias_cannot_override_explicit_canonical_role_without_item_confirmation(system):
    from test_knowledge_roles import role_package
    from test_knowledge_processing import BODY
    from rag_app.knowledge_item_assignments import declaration_key
    enable(system)
    selected = system.catalogue.register_role('共享中心', '流程专员')
    explicit = system.catalogue.register_role('运营部', '策划')
    # Simulate a previously reviewed exact declaration rule. Source canonical
    # fields still conflict and require a new explicit item decision.
    import csv
    import io
    import zipfile
    from test_knowledge_processing import zip_bytes
    raw = role_package(explicit, body=BODY)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {name: archive.read(name).decode() for name in archive.namelist()}
    rows = list(csv.DictReader(io.StringIO(files['交付清单.csv'])))
    rows[0]['上传员工部门'] = '声明部门'
    position = rows[0]['上传员工岗位']
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files['交付清单.csv'] = output.getvalue()
    with system.store._connection(write=True) as connection:
        connection.execute('INSERT INTO knowledge_role_aliases VALUES (?,?,?,?,?,?)',
            (declaration_key('声明部门', position), '声明部门', position, selected['role_id'], 'reviewer', system.store.clock()))
    receipt = intake(system, zip_bytes(files))
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    item = pending(job)
    assert item['reason_codes'] == ['uploader_role_conflict'] and item['revision'] is None
    version = system.store.get_item_uploader_role(job['job_id'], item['item_id'])['version']
    result = system.store.set_item_uploader_role(job['job_id'], item['item_id'], selected['role_id'], version, actor='reviewer')
    assert result['requeued'] and system.service.run_once()
    assert published_record(system, receipt)['role_id'] == selected['role_id']


def test_shared_principal_legacy_default_is_never_applied_to_new_upload(system):
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    with system.store._connection(write=True) as connection:
        connection.execute('INSERT INTO knowledge_uploader_roles VALUES (?,?,?,?,?,?,?)',
                           ('user:a', role['role_id'], 9, 'old-reviewer', 1000, 'old-job', 0))
    _, job = native(system, department='另一个部门', position='SSC')
    assert pending(job)['reason_codes'] == ['uploader_role_not_found']
    selected = system.store.get_item_uploader_role(job['job_id'], pending(job)['item_id'])
    assert selected['role'] is None and selected['version'] == 0


def test_new_alias_cannot_migrate_historically_unassigned_source(system, monkeypatch):
    from rag_app.knowledge_store import KnowledgeStore
    from rag_app.knowledge_item_assignments import declaration_key
    enable(system)
    role = system.catalogue.register_role('共享中心', '流程专员')
    with monkeypatch.context() as previous_release:
        previous_release.setattr(KnowledgeStore, 'bind_uploader_role', lambda self, connection, entry, previous=None, **kwargs: entry)
        first, _ = native(system, department='原部门', position='SSC')
    original = published_record(system, first)
    assert not original.get('role_id')
    with system.store._connection(write=True) as connection:
        connection.execute('INSERT INTO knowledge_role_aliases VALUES (?,?,?,?,?,?)',
            (declaration_key('原部门', 'SSC'), '原部门', 'SSC', role['role_id'], 'reviewer', system.store.clock()))
    updated, job = native(system, department='原部门', position='SSC', base_revision=1,
                           content=PAYLOAD['content'] + '\n历史来源新修订。')
    assert pending(job)['reason_codes'] == ['uploader_role_conflict']
    assert system.catalogue.get_published(original['knowledge_id']) == original
    assert system.catalogue.snapshot(original['knowledge_id'])['revision'] == 1


def referenced_package(*, a=True, changed=False):
    import csv
    import io
    import zipfile
    from test_knowledge_processing import BODY, zip_bytes
    from test_knowledge_processing_phase3 import package
    members = []
    if a:
        members.append(package(BODY + '\n参考[另一个方法](b.md)。', **{
            '资产编号': 'A', '名称': '关联方法甲', '正文路径': '正文/a.md', '上传员工岗位': '正式岗位'}))
    members.append(package(BODY + ('\n乙资料新步骤。' if changed else '\n乙资料原步骤。'), **{
        '资产编号': 'B', '名称': '新资料乙' if changed else '关联方法乙', '正文路径': '正文/b.md', '上传员工岗位': '正式岗位'}))
    rows, files = [], {}
    for member in members:
        with zipfile.ZipFile(io.BytesIO(member)) as archive:
            rows.extend(csv.DictReader(io.StringIO(archive.read('交付清单.csv').decode())))
            files.update({name: archive.read(name).decode() for name in archive.namelist() if name != '交付清单.csv'})
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files['交付清单.csv'] = output.getvalue()
    return zip_bytes(files)


def test_referenced_later_collision_cannot_publish_link_to_old_asset(system):
    enable(system)
    role = system.catalogue.register_role('部门', '正式岗位')
    first = intake(system, referenced_package(a=False), source='bundle')
    assert system.service.run_once()
    original = published_record(system, first)
    second = intake(system, referenced_package(changed=True), source='bundle')
    assert system.service.run_once()
    job = receipt_job(system, second)
    a = next(i for i in job['items'] if i['source_path'] == '正文/a.md')
    b = next(i for i in job['items'] if i['source_path'] == '正文/b.md')
    assert a['reason_codes'] == b['reason_codes'] == ['source_relation_review']
    assert system.catalogue.published_count() == 1
    assert system.store.set_item_uploader_role(job['job_id'], b['item_id'], role['role_id'], 0,
        actor='reviewer', source_action='new')['requeued']
    assert system.service.run_once()
    middle = receipt_job(system, second)
    new_b = next(i for i in middle['items'] if i['item_id'] == b['item_id'])
    assert new_b['status'] == 'published' and new_b['knowledge_id'] != original['knowledge_id']
    assert system.store.set_item_uploader_role(job['job_id'], a['item_id'], role['role_id'], 0,
        actor='reviewer', source_action='new')['requeued']
    assert system.service.run_once()
    new_a = next(i for i in receipt_job(system, second)['items'] if i['item_id'] == a['item_id'])
    assert new_a['status'] == 'published'
    record = system.catalogue.get_published(new_a['knowledge_id'])
    assert any(ref['knowledge_id'] == new_b['knowledge_id'] for ref in record['evidence']['related_assets'])
    assert all(ref['knowledge_id'] != original['knowledge_id'] for ref in record['evidence']['related_assets'])
    assert system.catalogue.get_published(original['knowledge_id']) == original


def test_first_package_references_resolve_without_manual_confirmation(system):
    enable(system)
    system.catalogue.register_role('部门', '正式岗位')
    receipt = intake(system, referenced_package(), source='new-bundle')
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    assert job['counts']['published'] == 2 and job['status'] == 'completed'
    items = {i['source_path']: i for i in job['items']}
    record = system.catalogue.get_published(items['正文/a.md']['knowledge_id'])
    assert any(ref['knowledge_id'] == items['正文/b.md']['knowledge_id'] for ref in record['evidence']['related_assets'])


def test_deployment_migration_freezes_old_choice_without_resuming_any_submitted_source_bucket(system):
    enable(system)
    original_role = system.catalogue.register_role('原部门', '原岗位')
    chosen = system.catalogue.register_role('品牌部', '设计师')
    first, _ = native(system, department='原部门', position='原岗位')
    original = published_record(system, first)
    pending_receipt, job = native(system, title='独立设计资料', content=PAYLOAD['content'] + '\n设计用途。')
    item = pending(job)
    # Represents the old release's conflict code before source collision detection.
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_items SET reason_codes='[\"uploader_role_conflict\"]' WHERE item_id=?", (item['item_id'],))
        system.store._event(connection, job['job_id'], 'uploader_role_selected', chosen['role_id'], 'old-reviewer')
    preview = system.store.migrate_legacy_item_roles()
    result = next(r for r in preview['items'] if r['job_id'] == job['job_id'])
    assert result['status'] == 'eligible' and not result['will_requeue']
    applied = system.store.migrate_legacy_item_roles(apply=True)
    assert applied['items'][0]['applied'] and not applied['items'][0]['requeued']
    assert not system.service.run_once()
    assert system.catalogue.get_published(original['knowledge_id']) == original
    # Normal item review remains usable: first recheck exposes source ambiguity,
    # then an explicit independent-source confirmation can publish only the new asset.
    version = system.store.get_item_uploader_role(job['job_id'], item['item_id'])['version']
    saved = system.store.set_item_uploader_role(job['job_id'], item['item_id'], chosen['role_id'], version, actor='reviewer')
    assert saved['requeued'] and system.service.run_once()
    assert pending(receipt_job(system, pending_receipt))['reason_codes'] == ['source_relation_review']
    saved = system.store.set_item_uploader_role(job['job_id'], item['item_id'], chosen['role_id'], version + 1,
                                               actor='reviewer', source_action='new')
    assert saved['requeued'] and system.service.run_once()
    assert published_record(system, pending_receipt)['knowledge_id'] != original['knowledge_id']
    assert system.catalogue.get_published(original['knowledge_id']) == original
