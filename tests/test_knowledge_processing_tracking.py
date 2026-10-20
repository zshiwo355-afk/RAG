"""Receipt tracking projects current item state without extra detail calls or writes."""
from contextlib import contextmanager
import json

import pytest

from test_knowledge_processing import BODY, submit, system, zip_bytes
from test_knowledge_processing_display import display_api
from test_knowledge_processing_phase3 import enable, intake, receipt_job
from test_knowledge_item_assignments import mixed_package
from test_knowledge_receipt_history import BASE, headers

pytestmark = pytest.mark.offline


def test_receipt_summary_preserves_each_role_and_remaining_mixed_item_problem(system, display_api):
    enable(system)
    original_role = system.catalogue.register_role('业务部', '正式岗位')
    selected_role = system.catalogue.register_role('运营部', '流程专员')
    receipt = intake(system, mixed_package(), principal='alice')
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    published = next(item for item in before['items'] if item['status'] == 'published')
    original = system.catalogue.get_published(published['knowledge_id'])
    target = next(item for item in before['items'] if item['reason_codes'] == ['uploader_role_not_found'])
    system.store.set_item_uploader_role(before['job_id'], target['item_id'], selected_role['role_id'], 0, actor='reviewer')
    queued = system.store.list_jobs('alice', status='attention')
    assert queued['total'] == system.store.stats('alice')['attention_jobs'] == 1
    assert queued['items'][0]['status'] == 'queued' and queued['items'][0]['attention_required']
    assert queued['items'][0]['blocking_reasons'] == ['unresolved_reference']
    detail = system.store.get_job(before['job_id'], 'alice')
    resumed = next(item for item in detail['items'] if item['item_id'] == target['item_id'])
    assert resumed['current_reason_codes'] == [] and resumed['rechecking']
    assert resumed['reason_codes'] == ['uploader_role_not_found']
    sibling = next(item for item in detail['items'] if 'unresolved_reference' in item['reason_codes'])
    assert sibling['current_reason_codes'] == ['unresolved_reference'] and not sibling['rechecking']
    tracked = display_api.get(BASE + '/receipts', headers=headers()).json()['items'][0]
    assert tracked['attention_required'] and tracked['blocking_reasons'] == ['unresolved_reference']
    assert system.service.run_once()
    for path in ('/receipts', '/jobs', '/jobs/' + before['job_id']):
        response = display_api.get(BASE + path, headers=headers())
        assert response.status_code == 200
        row = response.json() if path.startswith('/jobs/') else response.json()['items'][0]
        roles = {entry['item_id']: entry for entry in row['role_assignments']}
        assert roles[published['item_id']]['role']['role_id'] == original_role['role_id']
        assert roles[target['item_id']]['role']['role_id'] == selected_role['role_id']
        assert 'unresolved_reference' in row['blocking_reasons']
        assert row['counts']['needs_review'] == 1
        assert 'uploader_role_not_found' not in row['blocking_reasons']
        assert 'actor' not in json.dumps(row['role_assignments'])
    assert system.catalogue.get_published(published['knowledge_id']) == original


def test_list_filters_apply_before_page_and_role_conditions_match_one_item(system):
    roles = [system.catalogue.register_role(department, name) for department, name in
             [('业务部', '设计师'), ('运营部', '流程专员')]]
    jobs = []
    for number, principal in enumerate(('alice', 'alice', 'alice', 'bob')):
        system.now[0] = 1000 + number * 100
        receipt = submit(system, zip_bytes({'first.md': BODY.replace('工作方法', f'交接方案{number}'),
            'second.md': BODY + f'\n第二份 {number}'}), filename=f'资料{number}.zip', principal=principal, source_id=str(number))
        assert system.service.run_once()
        jobs.append(receipt_job(system, receipt))
    with system.store._connection(write=True) as db:
        for number, job in enumerate(jobs):
            db.execute("UPDATE knowledge_processing_jobs SET status='needs_review' WHERE job_id=?", (job['job_id'],))
            db.execute("UPDATE knowledge_processing_items SET revision=NULL,status='needs_review',reason_codes='[\"unresolved_reference\"]' WHERE job_id=?", (job['job_id'],))
            for index, item in enumerate(job['items']):
                role = roles[index % 2]
                db.execute('''INSERT INTO knowledge_item_role_assignments
                    (item_id,role_id,version,actor,updated_at,confirmation_source,remember_rule,rule_status,source_action,requeued)
                    VALUES (?,?,1,'reviewer',1000,'manual',0,'none','auto',0)''', (item['item_id'], role['role_id']))
    for listing in (system.store.list_receipts, system.store.list_jobs):
        filters = dict(q='交接方案', department='业务部', role_id=roles[0]['role_id'],
            processing_status='needs_review', created_from='1970-01-01T00:16:40Z', created_to='1970-01-01T00:20:00Z')
        first = listing('alice', limit=1, **filters)
        second = listing('alice', limit=1, offset=1, **filters)
        assert first['total'] == second['total'] == 2
        assert [first['items'][0]['job_id'], second['items'][0]['job_id']] == [jobs[1]['job_id'], jobs[0]['job_id']]
        assert listing('alice', department='业务部', role_id=roles[1]['role_id'])['total'] == 0
        assert listing('alice', q='交接方案3')['total'] == 0
        assert listing(None, q='交接方案3')['total'] == 1
        assert listing('alice', q='资料1.zip')['total'] == 1
        assert listing('alice', q='%')['total'] == 0
    assert system.store.list_jobs('alice', status='pending_publication', processing_status='failed')['total'] == 0
    assert system.store.list_receipts('alice', processing_status='none')['total'] == 0


def test_tracking_summaries_use_one_batch_query_per_page_and_never_write(system, monkeypatch):
    for number in range(3):
        submit(system, BODY + str(number), principal='alice', source_id=str(number))
        assert system.service.run_once()
    statements = []
    original = system.store._connection
    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(system.store, '_connection', traced)
    for listing in (system.store.list_receipts, system.store.list_jobs):
        statements.clear()
        assert len(listing('alice')['items']) == 3
        assert len([sql for sql in statements if 'SELECT i.job_id,i.item_id,i.title,role.role_id' in sql]) == 1
        assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements)


@pytest.mark.parametrize('path', ['/receipts', '/jobs'])
@pytest.mark.parametrize('query', [
    {'created_from': '2026-10-20'}, {'created_from': '2026-10-20T00:00:00'},
    {'created_from': '2026-10-21T00:00:00+08:00', 'created_to': '2026-10-20T00:00:00+08:00'},
    {'department': 'a\nb'}, {'role_id': 'unknown'}, {'processing_status': 'published'},
    {'q': 'a' * 201},
])
def test_filter_validation_is_bounded_and_private(system, display_api, path, query):
    response = display_api.get(BASE + path, params=query, headers=headers())
    assert response.status_code == 422
    assert response.json()['detail'] == 'invalid_processing_request'


def test_role_summary_is_bounded_but_detail_page_keeps_every_item_role(system, display_api):
    receipt = submit(system, zip_bytes({f'{number:02}.md': BODY + str(number) for number in range(8)}),
                     filename='many.zip', principal='alice')
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    role = system.catalogue.register_role('业务部', '流程专员')
    with system.store._connection(write=True) as db:
        db.execute('UPDATE knowledge_processing_items SET revision=NULL WHERE job_id=?', (job['job_id'],))
        for item in job['items']:
            db.execute('''INSERT INTO knowledge_item_role_assignments
                (item_id,role_id,version,actor,updated_at,confirmation_source,remember_rule,rule_status,source_action,requeued)
                VALUES (?,?,1,'reviewer',1000,'manual',0,'none','auto',0)''', (item['item_id'], role['role_id']))
        db.execute('UPDATE knowledge_item_role_assignments SET updated_at=1100 WHERE item_id=?', (job['items'][-1]['item_id'],))
    for path in ('/receipts', '/jobs'):
        row = display_api.get(BASE + path, headers=headers()).json()['items'][0]
        assert len(row['role_assignments']) == 5 and row['role_assignments_total'] == 8
        assert row['role_assignments_updated_at'] == '1970-01-01T00:18:20Z'
        assert all(role['updated_at'] == '1970-01-01T00:16:40Z' for role in row['role_assignments'])
    detail = display_api.get(BASE + '/jobs/' + job['job_id'], params={'limit': 2, 'item_offset': 6}, headers=headers()).json()
    assert len(detail['role_assignments']) == 5 and detail['role_assignments_total'] == 8
    assert len(detail['items']) == 2
    assert all(item['role_assignment']['item_id'] == item['item_id'] for item in detail['items'])
    assert all(item['role_assignment']['role']['role_id'] == role['role_id'] for item in detail['items'])
