"""Receipt-to-publication checks with no ambient credentials or network access."""
import csv
import hashlib
import io
import json
import re
import uuid

import pytest

from rag_app.knowledge_intake import SOURCE_COLUMNS, SOURCE_GOVERNANCE_COLUMNS, SOURCE_UPLOADER_COLUMN, process_batch
from rag_app.knowledge_processing import LEGACY_RULE_VERSION, ProcessingError, ProcessingService
from rag_app.knowledge_receipts import ReceiptService
from test_knowledge_processing import BODY, job, submit, system, zip_bytes

pytestmark = pytest.mark.offline


class Index:
    def __init__(self):
        self.calls = []
        self.failure = None

    def index_and_verify(self, record, *, require_keyword=False):
        self.calls.append((record['knowledge_id'], record['revision'], require_keyword))
        if self.failure:
            raise self.failure
        assert require_keyword
        return {'chunk_count': 1, 'retrieval_verified': True, 'keyword_retrieval_verified': True}


def enable(system):
    index = Index()
    system.service = ProcessingService(system.store, system.objects, artifacts=system.service.artifacts,
                                       draft_objects=system.drafts, auto_publish=True, knowledge_index=index)
    return index


def package(body=BODY, base='', **changes):
    row = dict(zip(SOURCE_COLUMNS, ['WORK-001', '新人工作资料核对方法', '方法', '交付', '新人核对工作资料',
        '正文/方法.md', '工作项目的方法说明', '仅方法说明，未宣称业务效果', '以项目实际要求为准', '']))
    row.update({'基线修订': str(base), '共享范围': '全员', '使用对象': '同事与新人', '输入': '工作资料',
                '输出': '核对结果与证据', '依赖与权限': '具备项目访问权限', '适用边界': '不替代业务审批'})
    row.update(changes)
    output = io.StringIO(newline='')
    fields = [*SOURCE_COLUMNS, *SOURCE_GOVERNANCE_COLUMNS]
    if SOURCE_UPLOADER_COLUMN in row:
        fields.append(SOURCE_UPLOADER_COLUMN)
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerow(row)
    return zip_bytes({'交付清单.csv': output.getvalue(), row['正文路径']: body})


def intake(system, data=None, principal='user:a', source='hermes:work'):
    data = package() if data is None else data
    receipt = system.receipts.prepare(principal, dict(idempotency_key=uuid.uuid4().hex, source_id=source,
        filename='assets.zip', byte_length=len(data), sha256=hashlib.sha256(data).hexdigest()))
    system.bucket.upload(receipt, data)
    receipts = ReceiptService(system.receipts, system.objects)
    receipts.complete(principal, receipt['receipt_id'])
    assert receipts.verify_once()
    return receipt


def receipt_job(system, receipt):
    rows = system.store.list_jobs(receipt['principal'])['items']
    summary = next(row for row in rows if row['receipt_id'] == receipt['receipt_id'])
    return system.store.get_job(summary['job_id'], receipt['principal'])


def asset_item(result):
    return next(item for item in result['items'] if item['source_path'] == '正文/方法.md')


def test_structured_delivery_publishes_and_repeat_keeps_one_revision(system):
    index = enable(system)
    first = intake(system)
    assert system.service.run_once()
    result = receipt_job(system, first)
    item = asset_item(result)
    assert result['status'] == 'completed' and item['published']
    assert item['publication']['verification']['keyword_retrieval_verified']
    assert system.catalogue.get_published(item['knowledge_id'])['content'] == BODY
    assert system.catalogue.published_count() == 1 and len(index.calls) == 1
    repeated = intake(system)
    assert system.service.run_once()
    duplicate = asset_item(receipt_job(system, repeated))
    assert duplicate['status'] == 'duplicate' and duplicate['revision'] == 1
    assert system.catalogue.list_assets()[0]['latest_revision'] == 1
    assert len(index.calls) == 1
    assert system.store.stats('user:a')['duplicate_items'] == 1


def test_uploader_position_uses_authenticated_receipt_not_audience(system, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from rag_app.knowledge_graph import KnowledgeGraph
    from rag_app.knowledge_api import get_knowledge_service, router as knowledge_router
    from rag_app.knowledge_processing_api import _catalog_kind, get_catalog, router
    from rag_app.knowledge_service import KnowledgeService
    from test_knowledge_graph import Queued
    from test_knowledge_processing_api import BASE, READ, TOKEN, headers

    index = enable(system)
    system.catalogue.register_role('产品部', '产品经理')
    receipt = intake(system, package(**{'上传员工岗位': '产品经理', '使用对象': '销售'}))
    assert system.service.run_once()
    item = asset_item(receipt_job(system, receipt))
    record = system.catalogue.get_published(item['knowledge_id'])
    assert system.catalogue.published_uploader_position(record) == '产品经理'
    assert record['evidence']['source_audience'] == '销售'
    assert record['evidence']['uploader_attribution'] == {
        'position': '产品经理', 'source': 'employee_declared', 'receipt_id': receipt['receipt_id']}
    with system.store._connection() as connection:
        submission = connection.execute('SELECT principal,receipt_id FROM knowledge_submissions').fetchone()
    assert submission['principal'] == 'user:a' and submission['receipt_id'] == receipt['receipt_id']
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_PROCESSING_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_TOKEN', TOKEN)
    app = FastAPI()
    app.include_router(router)
    app.include_router(knowledge_router)
    app.dependency_overrides[get_catalog] = lambda: system.catalogue
    app.dependency_overrides[get_knowledge_service] = lambda: KnowledgeService(system.catalogue, index)
    with TestClient(app) as client:
        catalog = client.get(BASE + '/catalog?uploader_position=产品经理', headers=headers(READ)).json()
        assert catalog['counts'] == {'published_assets': 1, 'positions': 1, 'unknown_position': 0}
        assert catalog['by_uploader_position'] == [{'key': '产品经理', 'label': '产品经理', 'count': 1}]
        assert catalog['total'] == 1 and catalog['items'][0]['uploader_position'] == '产品经理'
        detail = client.get(BASE + '/catalog/' + item['knowledge_id'], headers=headers(READ)).json()
        assert detail['uploader_position'] == '产品经理'
        assert client.get(BASE + '/catalog?uploader_position=销售', headers=headers(READ)).json()['total'] == 0
        employee_detail = client.get('/api/knowledge/' + item['knowledge_id']).json()['knowledge']
        assert employee_detail['evidence']['uploader_position'] == '产品经理'
        assert employee_detail['evidence']['uploader_position_source'] == 'employee_declared'
        assert 'user:a' not in json.dumps(employee_detail)
    queue = Queued()
    graph = KnowledgeGraph(executor=queue)
    graph.read(system.catalogue, _catalog_kind)
    queue.finish()
    assert graph.read(system.catalogue, _catalog_kind)['nodes'][0]['uploader_position'] == '产品经理'


@pytest.mark.parametrize('position', [None, '', '未知', '待补充'])
def test_legacy_or_missing_uploader_position_remains_unknown(system, position):
    from rag_app.knowledge_service import KnowledgeService
    enable(system)
    receipt = intake(system, package(**({'上传员工岗位': position} if position is not None else {})))
    assert system.service.run_once()
    item = asset_item(receipt_job(system, receipt))
    assert item['published']
    assert system.catalogue.published_uploader_position(system.catalogue.get_published(item['knowledge_id'])) is None
    assert system.catalogue.published_catalog()[0]['uploader_position'] is None
    detail = KnowledgeService(system.catalogue).get(item['knowledge_id'])
    assert detail['evidence']['uploader_position'] is None
    assert detail['evidence']['uploader_position_source'] is None


def test_position_changes_and_other_uploaders_do_not_rewrite_duplicate_attribution(system):
    index = enable(system)
    system.catalogue.register_role('产品部', '产品经理')
    first = intake(system, package(**{'上传员工岗位': '产品经理'}))
    assert system.service.run_once()
    original = asset_item(receipt_job(system, first))
    changed = intake(system, package(**{'上传员工岗位': '研发'}))
    assert system.service.run_once()
    conflict = asset_item(receipt_job(system, changed))
    assert conflict['status'] == 'needs_review' and conflict['reason_codes'] == ['source_relation_review']
    for principal in ('user:a', 'user:b'):
        receipt = intake(system, package(**{'上传员工岗位': '产品经理'}), principal=principal)
        assert system.service.run_once()
        item = asset_item(receipt_job(system, receipt))
        assert item['status'] == 'duplicate' and item['revision'] == 1
        assert item['knowledge_id'] == original['knowledge_id']
    assert len(index.calls) == 1 and system.catalogue.published_count() == 1
    assert system.catalogue.published_catalog()[0]['uploader_position'] == '产品经理'
    assert system.catalogue.get_published(original['knowledge_id'])['evidence']['uploader_attribution']['receipt_id'] == first['receipt_id']


def test_new_private_revision_cannot_change_published_position(system):
    enable(system)
    system.catalogue.register_role('产品部', '产品经理')
    receipt = intake(system, package(**{'上传员工岗位': '产品经理'}))
    assert system.service.run_once()
    original = asset_item(receipt_job(system, receipt))
    changed = intake(system, package(BODY + '\n新的工作方法。', base=1,
        **{'上传员工岗位': '', '共享范围': '未确认'}))
    assert system.service.run_once()
    assert asset_item(receipt_job(system, changed))['revision'] == 2
    assert 'uploader_attribution' not in system.catalogue.snapshot(original['knowledge_id'], 2)['evidence']
    assert system.catalogue.published_catalog()[0]['uploader_position'] == '产品经理'
    assert system.catalogue.graph_internal_snapshot()[0]['uploader_position'] == '产品经理'
    assert system.catalogue.published_uploader_position(system.catalogue.get_published(original['knowledge_id'])) == '产品经理'
    system.catalogue.withdraw(original['knowledge_id'])
    assert system.catalogue.published_catalog() == [] and system.catalogue.graph_internal_snapshot() == []


def test_anonymous_manifest_and_forged_attribution_are_not_trusted(system, tmp_path):
    from rag_app.knowledge_store import KnowledgeStore
    from rag_app.knowledge_service import KnowledgeService, public_record

    archive = tmp_path / 'anonymous.zip'
    archive.write_bytes(package(**{'上传员工岗位': '产品经理'}))
    output = tmp_path / 'anonymous-output'
    batch = process_batch([{'path': archive, 'source_id': 'anonymous', 'package': archive.name,
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'contributor': '未确认'}], output)
    asset = batch['assets'][0]
    entry = json.loads((output / asset['metadata_file']).read_text())
    assert entry['evidence']['source_uploader_position'] == '产品经理'
    entry.update(knowledge_id=asset['knowledge_id'], content=(output / asset['body_file']).read_text())
    legacy = KnowledgeStore(tmp_path / 'legacy.sqlite3')
    draft = legacy.import_draft(entry)
    legacy.publish(draft['knowledge_id'], draft['revision'], expected_generation=draft['generation'], confirmed_by='test')
    assert legacy.published_catalog()[0]['uploader_position'] is None

    enable(system)
    system.catalogue.register_role('产品部', '产品经理')
    receipt = intake(system, package(**{'上传员工岗位': '产品经理'}))
    assert system.service.run_once()
    original = system.catalogue.get_published(asset_item(receipt_job(system, receipt))['knowledge_id'])
    forged = system.catalogue.import_draft({**entry, 'knowledge_id': 'forged', 'evidence': {
        **entry['evidence'], 'uploader_position': '伪造岗位', 'uploader_position_source': 'employee_declared',
        'submitter_authenticated': True,
        'uploader_attribution': original['evidence']['uploader_attribution']}})
    system.catalogue.publish('forged', forged['revision'], expected_generation=forged['generation'], confirmed_by='test')
    assert system.catalogue.published_uploader_position(system.catalogue.get_published('forged')) is None
    assert next(row for row in system.catalogue.graph_internal_snapshot() if row['knowledge_id'] == 'forged')['uploader_position'] is None
    detail = KnowledgeService(system.catalogue).get('forged')
    assert detail['evidence']['uploader_position'] is None and detail['evidence']['uploader_position_source'] is None
    assert 'uploader_position' not in public_record(forged)['evidence']


@pytest.mark.parametrize('position', ['a' * 201, '产品\n研发'])
def test_invalid_uploader_position_does_not_publish(system, position):
    index = enable(system)
    receipt = intake(system, package(**{'上传员工岗位': position}))
    assert system.service.run_once()
    assert system.catalogue.published_count() == 0 and not index.calls
    assert receipt_job(system, receipt)['status'] in {'needs_review', 'failed'}


def legacy_manifest_rejection(system, monkeypatch):
    from rag_app import knowledge_intake

    index = enable(system)
    system.catalogue.register_role('产品部', '产品经理')
    receipt = intake(system, package(**{'上传员工岗位': '产品经理'}))
    with monkeypatch.context() as legacy:
        legacy.setattr(knowledge_intake, 'SOURCE_UPLOADER_COLUMN', '__unsupported_legacy_column__')
        assert system.service.run_once()
    result = receipt_job(system, receipt)
    assert result['status'] == 'needs_review' and len(result['items']) == 2
    assert {tuple(item['reason_codes']) for item in result['items']} == {
        ('parse_failed', 'source_manifest_invalid'), ('unassigned_needs_review',)}
    assert all(item['knowledge_id'] is None and item['revision'] is None for item in result['items'])
    assert not index.calls
    return index, receipt, result


def test_operator_reparses_same_manifest_receipt_without_stale_items_or_other_job_changes(system, monkeypatch):
    index, receipt, before = legacy_manifest_rejection(system, monkeypatch)
    other_receipt = intake(system, package(BODY + '\n另一份待确认资料。', **{'共享范围': '未确认'}), source='other:source')
    assert system.service.run_once()
    other_before = receipt_job(system, other_receipt)
    with pytest.raises(ProcessingError, match='processing_retry_not_allowed'):
        system.store.retry(before['job_id'], None, actor='operator:test')
    queued = system.store.retry(before['job_id'], None, actor='operator:test',
                               reparse_manifest=True, expected_receipt_id=receipt['receipt_id'])
    assert queued['status'] == 'queued' and queued['item_total'] == 0
    old_events = {event['event_id'] for event in before['events']}
    assert old_events <= {event['event_id'] for event in queued['events']}
    assert any(event['event_type'] == 'retried' and event['reason_code'] == 'source_manifest_compatibility'
               and event['actor'] == 'operator:test' for event in queued['events'])
    with pytest.raises(ProcessingError, match='processing_retry_not_allowed'):
        system.store.retry(before['job_id'], None, actor='operator:test',
                           reparse_manifest=True, expected_receipt_id=receipt['receipt_id'])
    assert system.service.run_once()
    after = receipt_job(system, receipt)
    assert after['job_id'] == before['job_id'] and after['receipt_id'] == receipt['receipt_id']
    assert after['status'] == 'completed' and len(after['items']) == 2
    assert {item['status'] for item in after['items']} == {'archived', 'published'}
    old_body_id = next(item['item_id'] for item in before['items'] if item['source_path'] == '正文/方法.md')
    assert all(item['item_id'] != old_body_id for item in after['items'])
    assert system.catalogue.published_catalog()[0]['uploader_position'] == '产品经理'
    assert len(index.calls) == 1 and system.catalogue.published_count() == 1
    assert system.receipts.get(receipt['principal'], receipt['receipt_id'])['sha256'] == receipt['sha256']
    assert receipt_job(system, other_receipt) == other_before
    with pytest.raises(ProcessingError, match='processing_retry_not_allowed'):
        system.store.retry(after['job_id'], None, actor='operator:test',
                           reparse_manifest=True, expected_receipt_id=receipt['receipt_id'])


@pytest.mark.parametrize('change', [
    'wrong_receipt', 'no_actor', 'nonverified_receipt', 'other_reason', 'resolution',
    'knowledge_id', 'revision', 'content_ref', 'publication', 'missing_manifest',
])
def test_manifest_reparse_refuses_changed_or_already_materialized_work(system, monkeypatch, change):
    _, receipt, result = legacy_manifest_rejection(system, monkeypatch)
    manifest = next(item for item in result['items'] if item['source_path'] == '交付清单.csv')
    with system.store._connection(write=True) as connection:
        if change == 'nonverified_receipt':
            connection.execute("UPDATE knowledge_upload_receipts SET status='rejected' WHERE receipt_id=?", (receipt['receipt_id'],))
        elif change == 'missing_manifest':
            connection.execute('DELETE FROM knowledge_processing_items WHERE item_id=?', (manifest['item_id'],))
        elif change in {'other_reason', 'resolution', 'knowledge_id', 'revision', 'content_ref', 'publication'}:
            field, value = {
                'other_reason': ('reason_codes', '["sharing_scope_unconfirmed"]'),
                'resolution': ('resolution_json', '{"action":"archive","reason":"already reviewed"}'),
                'knowledge_id': ('knowledge_id', 'existing-asset'), 'revision': ('revision', 1),
                'content_ref': ('content_ref', '{}'), 'publication': ('publication_json', '{}'),
            }[change]
            connection.execute('UPDATE knowledge_processing_items SET ' + field + '=? WHERE item_id=?', (value, manifest['item_id']))
    before = system.store.get_job(result['job_id'], None)
    with pytest.raises(ProcessingError, match='processing_retry_not_allowed'):
        system.store.retry(result['job_id'], None, actor=None if change == 'no_actor' else 'operator:test',
                           reparse_manifest=True, expected_receipt_id='f' * 32 if change == 'wrong_receipt' else receipt['receipt_id'])
    assert system.store.get_job(result['job_id'], None) == before


def test_manifest_reparse_concurrent_requests_only_enqueue_once(system, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    _, receipt, result = legacy_manifest_rejection(system, monkeypatch)
    def retry():
        try:
            return system.store.retry(result['job_id'], None, actor='operator:test', reparse_manifest=True,
                                      expected_receipt_id=receipt['receipt_id'])['status']
        except ProcessingError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: retry(), range(2)))
    assert sorted(outcomes) == ['processing_retry_not_allowed', 'queued']
    after = system.store.get_job(result['job_id'], None)
    assert sum(event['event_type'] == 'retried' for event in after['events']) == 1


def test_manifest_reparse_cleanup_rolls_back_if_audit_write_fails(system, monkeypatch):
    _, receipt, before = legacy_manifest_rejection(system, monkeypatch)
    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(system.store, '_event', unavailable)
    with pytest.raises(RuntimeError, match='synthetic audit failure'):
        system.store.retry(before['job_id'], None, actor='operator:test', reparse_manifest=True,
                           expected_receipt_id=receipt['receipt_id'])
    assert system.store.get_job(before['job_id'], None) == before


def test_calendar_label_first_delivery_publishes_without_claiming_a_base_revision(system):
    from rag_app.knowledge_service import KnowledgeService
    index = enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    receipt = intake(system, package(base='2026-10', **{'上传员工岗位': 'AI架构师'}))
    assert system.service.run_once()
    item = asset_item(receipt_job(system, receipt))
    assert item['published'] and item['revision'] == 1
    detail = KnowledgeService(system.catalogue, index).get(item['knowledge_id'])
    assert detail['evidence']['source_revision_label'] == '2026-10'
    assert 'source_base_revision' not in detail['evidence']
    assert detail['evidence']['uploader_position'] == 'AI架构师'
    assert detail['evidence']['uploader_position_source'] == 'employee_declared'
    assert system.receipts.get(receipt['principal'], receipt['receipt_id'])['sha256'] == receipt['sha256']


def test_calendar_label_update_requires_real_base_and_preserves_publication(system):
    index = enable(system)
    system.catalogue.register_role('产品部', '产品经理')
    first = intake(system, package(**{'上传员工岗位': '产品经理'}))
    assert system.service.run_once()
    first_item = asset_item(receipt_job(system, first))
    published = system.catalogue.get_published(first_item['knowledge_id'])
    changed = intake(system, package(BODY + '\n补充资料后必须复核输出。', base='2026-10', **{'上传员工岗位': '产品经理'}))
    assert system.service.run_once()
    item = asset_item(receipt_job(system, changed))
    assert item['knowledge_id'] is None and item['revision'] is None
    assert item['status'] == 'needs_review' and 'source_relation_review' in item['reason_codes']
    current = system.catalogue.get_published(first_item['knowledge_id'])
    assert current['revision'] == 1 and current['content_hash'] == published['content_hash']
    assert system.catalogue.published_uploader_position(current) == '产品经理'
    assert len(index.calls) == 1


def test_calendar_label_change_alone_does_not_create_a_revision(system):
    index = enable(system)
    first = intake(system, package(base='2026-10'))
    assert system.service.run_once()
    second = intake(system, package(base='2026-11'))
    assert system.service.run_once()
    assert asset_item(receipt_job(system, second))['status'] == 'duplicate'
    assert asset_item(receipt_job(system, second))['revision'] == 1
    assert len(index.calls) == 1 and system.catalogue.published_count() == 1


@pytest.mark.parametrize('base', ['2026-00', '2026-13', '2026-1', '2026-10-01', '0000-01', 'v1', '0', '-1', '1000000000'])
def test_invalid_revision_is_rejected_with_durable_manifest_reason(system, base):
    index = enable(system)
    receipt = intake(system, package(base=base))
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    manifest = next(item for item in result['items'] if item['source_path'] == '交付清单.csv')
    assert result['status'] == 'needs_review'
    assert 'source_manifest_invalid_base_revision' in manifest['reason_codes']
    assert all(item['knowledge_id'] is None for item in result['items'])
    assert not index.calls and system.catalogue.published_count() == 0


def legacy_revision_label_rejection(system, monkeypatch):
    from rag_app import knowledge_intake
    index = enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    receipt = intake(system, package(base='2026-10', **{'上传员工岗位': 'AI架构师'}))
    with monkeypatch.context() as legacy:
        legacy.setattr(knowledge_intake, 'SOURCE_UPLOADER_COLUMN', '__unsupported_legacy_column__')
        assert system.service.run_once()
    first = receipt_job(system, receipt)
    system.store.retry(first['job_id'], None, actor='operator:test', reparse_manifest=True,
                       expected_receipt_id=receipt['receipt_id'])
    with monkeypatch.context() as legacy:
        legacy.setattr(knowledge_intake, 'SOURCE_REVISION_LABEL', re.compile(r'(?!)'))
        assert system.service.run_once()
    # The deployed predecessor did not persist its report-level semantic error.
    with system.store._connection(write=True) as connection:
        connection.execute('UPDATE knowledge_processing_items SET reason_codes=? WHERE job_id=?',
                           ('["unassigned_needs_review"]', first['job_id']))
    result = receipt_job(system, receipt)
    assert result['status'] == 'needs_review' and len(result['items']) == 2
    return index, receipt, result


def test_second_compatibility_reparse_preserves_receipt_and_history_and_only_runs_once(system, monkeypatch):
    index, receipt, before = legacy_revision_label_rejection(system, monkeypatch)
    queued = system.store.retry(before['job_id'], None, actor='operator:test', reparse_manifest=True,
                               expected_receipt_id=receipt['receipt_id'], manifest_compatibility='revision_label')
    assert queued['status'] == 'queued' and queued['item_total'] == 0
    assert {event['event_id'] for event in before['events']} <= {event['event_id'] for event in queued['events']}
    assert sum(event['reason_code'] == 'source_revision_label_compatibility' for event in queued['events']) == 1
    with pytest.raises(ProcessingError):
        system.store.retry(before['job_id'], None, actor='operator:test', reparse_manifest=True,
                           expected_receipt_id=receipt['receipt_id'], manifest_compatibility='revision_label')
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    assert result['job_id'] == before['job_id'] and result['status'] == 'completed'
    assert asset_item(result)['published'] and len(index.calls) == 1
    assert system.receipts.get(receipt['principal'], receipt['receipt_id'])['sha256'] == receipt['sha256']
    assert system.catalogue.published_catalog()[0]['uploader_position'] == 'AI架构师'


@pytest.mark.parametrize('change', ['no_previous', 'two_previous', 'already_repaired', 'knowledge_id', 'content_ref', 'publication_json', 'resolution_json', 'other_reason'])
def test_second_reparse_refuses_ambiguous_history_or_materialized_outputs(system, monkeypatch, change):
    _, receipt, result = legacy_revision_label_rejection(system, monkeypatch)
    with system.store._connection(write=True) as connection:
        if change == 'no_previous':
            connection.execute("DELETE FROM knowledge_processing_events WHERE job_id=? AND reason_code='source_manifest_compatibility'", (result['job_id'],))
        elif change in {'two_previous', 'already_repaired'}:
            system.store._event(connection, result['job_id'], 'retried',
                                'source_manifest_compatibility' if change == 'two_previous' else 'source_revision_label_compatibility')
        else:
            field = 'reason_codes' if change == 'other_reason' else change
            value = '["other"]' if change == 'other_reason' else '{}'
            connection.execute('UPDATE knowledge_processing_items SET ' + field + '=? WHERE job_id=?', (value, result['job_id']))
    before = receipt_job(system, receipt)
    with pytest.raises(ProcessingError):
        system.store.retry(result['job_id'], None, actor='operator:test', reparse_manifest=True,
                           expected_receipt_id=receipt['receipt_id'], manifest_compatibility='revision_label')
    assert receipt_job(system, receipt) == before


def test_structured_docx_publishes_extracted_text_and_preserves_original(system):
    from test_knowledge_text import docx
    index = enable(system)
    text = '新人先核对资料，再保存验证依据。'
    original = docx('<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>')
    data = package(original, **{'正文路径': '正文/方法.docx'})
    receipt = intake(system, data)
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    item = next(item for item in result['items'] if item['source_path'] == '正文/方法.docx')
    assert item['status'] == 'published' and len(index.calls) == 1
    published = system.catalogue.get_published(item['knowledge_id'])
    assert text in published['content'] and published['evidence']['source_protocol'] == 'company-assets-source-v2'
    assert system.bucket.data[receipt['object_key']] == data


def pdf_document(text=None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    if text:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 20 100 Td (' + text + ') Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    else:
        image = DecodedStreamObject()
        image.set_data(b'\x00')
        image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                      NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                      NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Im0'): writer._add_object(image)})})
        stream = DecodedStreamObject()
        stream.set_data(b'q 40 0 0 40 20 100 cm /Im0 Do Q')
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_structured_pdf_publishes_extracted_text_and_preserves_original(system):
    index = enable(system)
    text = 'Verify input and save evidence before delivery.'
    data = package(pdf_document(text), **{'正文路径': '正文/方法.pdf'})
    receipt = intake(system, data)
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    item = next(item for item in result['items'] if item['source_path'] == '正文/方法.pdf')
    assert item['status'] == 'published' and len(index.calls) == 1
    assert text in system.catalogue.get_published(item['knowledge_id'])['content']
    assert system.bucket.data[receipt['object_key']] == data


def test_image_only_pdf_needs_ocr_and_never_publishes(system):
    index = enable(system)
    data = package(pdf_document(), **{'正文路径': '正文/扫描件.pdf'})
    receipt = intake(system, data)
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    item = next(item for item in result['items'] if item['source_path'] == '正文/扫描件.pdf')
    assert item['status'] == 'needs_review' and 'needs_ocr' in item['reason_codes']
    assert not index.calls and system.catalogue.published_count() == 0
    assert system.bucket.data[receipt['object_key']] == data


@pytest.mark.parametrize('suffix', ['docx', 'pdf'])
def test_document_format_extension_does_not_bypass_sharing_rules(system, suffix):
    from test_knowledge_text import docx
    index = enable(system)
    body = (docx('<w:p><w:r><w:t>新人核对资料，保存验证依据。</w:t></w:r></w:p>')
            if suffix == 'docx' else pdf_document('Verify input and save evidence.'))
    source_path = '正文/方法.' + suffix
    receipt = intake(system, package(body, **{'正文路径': source_path, '共享范围': ''}))
    assert system.service.run_once()
    item = next(item for item in receipt_job(system, receipt)['items'] if item['source_path'] == source_path)
    assert item['status'] == 'needs_review' and 'sharing_scope_unconfirmed' in item['reason_codes']
    assert not index.calls and system.catalogue.published_count() == 0


@pytest.mark.parametrize('suffix', ['docx', 'pdf'])
def test_document_secrets_are_redacted_for_review_and_never_published(system, suffix):
    from test_knowledge_text import docx
    index = enable(system)
    text = 'password=synthetic-only-secret'
    body = docx('<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>') if suffix == 'docx' else pdf_document(text)
    source_path = '正文/方法.' + suffix
    receipt = intake(system, package(body, **{'正文路径': source_path}))
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    item = next(item for item in result['items'] if item['source_path'] == source_path)
    assert item['status'] == 'needs_review' and 'redaction_review' in item['reason_codes']
    preview = system.service.get_item(result['job_id'], item['item_id'], 'user:a')['content']
    assert 'synthetic-only-secret' not in preview and '[已隐藏:' in preview
    assert not index.calls and system.catalogue.published_count() == 0


@pytest.mark.parametrize('suffix', ['md', 'txt'])
def test_structured_text_documents_keep_automatic_publication(system, suffix):
    enable(system)
    source_path = '正文/方法.' + suffix
    receipt = intake(system, package(BODY, **{'正文路径': source_path}))
    assert system.service.run_once()
    item = next(item for item in receipt_job(system, receipt)['items'] if item['source_path'] == source_path)
    assert item['status'] == 'published'
    assert system.catalogue.get_published(item['knowledge_id'])['content'] == BODY


def test_ordered_update_publishes_new_revision_old_replay_never_rolls_back(system):
    enable(system)
    original = package()
    first = intake(system, original)
    system.service.run_once()
    asset = asset_item(receipt_job(system, first))
    changed = intake(system, package(BODY + '\n保存核对记录供下一位同事使用。', base=1))
    system.service.run_once()
    assert asset_item(receipt_job(system, changed))['revision'] == 2
    assert system.catalogue.get_published(asset['knowledge_id'])['revision'] == 2
    replay = intake(system, original)
    system.service.run_once()
    assert asset_item(receipt_job(system, replay))['status'] == 'outdated'
    assert system.catalogue.get_published(asset['knowledge_id'])['revision'] == 2
    assert system.catalogue.snapshot(asset['knowledge_id'], 1)['content'] == BODY


def test_unordered_update_and_sensitive_delivery_stay_out_of_public_knowledge(system):
    enable(system)
    first = intake(system)
    system.service.run_once()
    asset = asset_item(receipt_job(system, first))
    changed = intake(system, package(BODY + '\n修改顺序未知。'))
    system.service.run_once()
    item = asset_item(receipt_job(system, changed))
    assert item['status'] == 'needs_review' and 'source_relation_review' in item['reason_codes']
    assert system.catalogue.get_published(asset['knowledge_id'])['revision'] == 1
    sensitive = intake(system, package(BODY + '\npassword=private-synthetic-secret\n'), source='different')
    system.service.run_once()
    item = asset_item(receipt_job(system, sensitive))
    assert item['status'] == 'needs_review' and not item['published']
    assert system.catalogue.published_count() == 1


def test_index_failure_keeps_pointer_and_retry_uses_same_draft(system):
    index = enable(system)
    first = intake(system)
    system.service.run_once()
    asset = asset_item(receipt_job(system, first))
    index.failure = RuntimeError('synthetic index unavailable')
    changed = intake(system, package(BODY + '\n补充操作说明。', base=1))
    system.service.run_once()
    assert receipt_job(system, changed)['status'] == 'queued'
    assert system.catalogue.get_published(asset['knowledge_id'])['revision'] == 1
    index.failure = None
    system.now[0] += 31
    system.service.run_once()
    item = asset_item(receipt_job(system, changed))
    assert item['status'] == 'published' and item['revision'] == 2
    assert system.catalogue.list_assets()[0]['latest_revision'] == 2


@pytest.mark.parametrize('interruption', ['submission', 'indexing', 'published'])
def test_docx_parser_upgrade_resumes_persisted_submission_without_rewriting_it(system, monkeypatch, interruption):
    from rag_app import knowledge_text
    from test_knowledge_text import docx, paragraph, table

    index = enable(system)
    document = docx(paragraph('岗位标准作业程序') +
                    table([['所属部门', '总经办', '岗位名称', 'AI架构师']]) +
                    paragraph('工作方法', 'Heading1') + paragraph('核对输入，保存验证依据。'))
    # This legacy method is now recognized as an SOP, so the current parser
    # also reports a category mismatch. Recovery must use the old decision.
    receipt = intake(system, package(document, **{'正文路径': '正文/方法.docx'}))

    def old_docx(archive, issues):
        # The pre-role parser emitted plain paragraphs, including Word headings.
        body = knowledge_text.ET.fromstring(archive.read('word/document.xml')).find(knowledge_text.W + 'body')
        paragraph_number = table_number = 0
        for block in body:
            if block.tag == knowledge_text.W + 'p':
                paragraph_number += 1
                yield knowledge_text._paragraph(block), {'kind': 'paragraph', 'paragraph': paragraph_number}
            else:
                assert block.tag == knowledge_text.W + 'tbl'
                table_number += 1
                for number, cells in enumerate(knowledge_text._table_rows(block), 1):
                    yield '[表格 %d 行 %d] %s' % (table_number, number, json.dumps(cells, ensure_ascii=False)), {
                        'kind': 'table_row', 'table': table_number, 'row': number}

    def interrupted(*args, **kwargs):
        raise RuntimeError('synthetic interruption after durable submission')

    with monkeypatch.context() as old_worker:
        old_worker.setattr(knowledge_text, '_docx', old_docx)
        old_worker.setattr(knowledge_text, '_sop_metadata', lambda archive: None)
        if interruption == 'submission':
            old_worker.setattr(system.store, 'save_item', interrupted)
        elif interruption == 'indexing':
            index.failure = RuntimeError('synthetic index unavailable')
        else:
            finish = system.store.finish
            old_worker.setattr(system.store, 'finish', lambda row, status, code=None:
                                interrupted() if status == 'completed' else finish(row, status, code))
        assert system.service.run_once()

    before = receipt_job(system, receipt)
    assert before['status'] == 'queued'
    with system.store._connection() as connection:
        submission = dict(connection.execute('SELECT * FROM knowledge_submissions').fetchone())
        stored = connection.execute('SELECT record_json FROM knowledge_revisions').fetchone()['record_json']
    old_record = system.catalogue.snapshot(submission['knowledge_id'], submission['revision'])
    assert old_record['content'] != knowledge_text.parse_text_file('方法.docx', document)['text']
    if interruption == 'submission':
        assert before['item_total'] == 0
    calls = len(index.calls)
    index.failure = None
    system.now[0] += 301
    assert system.service.run_once()

    after = receipt_job(system, receipt)
    assert after['status'] == 'completed', after
    assert after['counts']['published'] == 1
    record = system.catalogue.get_published(submission['knowledge_id'])
    assert record['content'] == old_record['content']
    assert record['payload_hash'] == old_record['payload_hash']
    assert 'role_id' not in record and 'files' not in record
    assert system.catalogue.roles() == []
    assert len(index.calls) == calls + (interruption != 'published')
    with system.store._connection() as connection:
        assert dict(connection.execute('SELECT * FROM knowledge_submissions').fetchone()) == submission
        assert connection.execute('SELECT COUNT(*) FROM knowledge_revisions').fetchone()[0] == 1
        assert connection.execute('SELECT record_json FROM knowledge_revisions').fetchone()['record_json'] == stored


def test_plain_chat_is_retained_for_review_when_automatic_publication_enabled(system):
    index = enable(system)
    submit(system)
    system.service.run_once()
    item = job(system)['items'][0]
    assert item['status'] == 'needs_review'
    assert 'structured_delivery_required' in item['reason_codes']
    assert not index.calls and system.catalogue.published_count() == 0


@pytest.mark.parametrize('change', ['withdraw', 'newer_revision'])
def test_resumed_submission_keeps_original_publication_pin(system, change):
    index = enable(system)
    first = intake(system)
    system.service.run_once()
    original = asset_item(receipt_job(system, first))
    index.failure = RuntimeError('synthetic index unavailable')
    pending = intake(system, package(BODY + '\n旧任务的修订。', base=1))
    system.service.run_once()
    assert receipt_job(system, pending)['status'] == 'queued'
    if change == 'withdraw':
        system.catalogue.withdraw(original['knowledge_id'])
    else:
        draft = system.catalogue.import_draft({**system.catalogue.snapshot(original['knowledge_id']),
            'content': BODY + '\n后续显式确认的修订。'})
        system.catalogue.publish(draft['knowledge_id'], draft['revision'],
                                 expected_generation=draft['generation'], confirmed_by='test')
    before = system.catalogue.get_published(original['knowledge_id'])
    calls = len(index.calls)
    index.failure = None
    system.now[0] += 301
    system.service.run_once()
    result = receipt_job(system, pending)
    assert result['status'] == 'needs_review'
    assert asset_item(result)['reason_codes'] == ['publication_version_conflict']
    assert system.catalogue.get_published(original['knowledge_id']) == before
    assert len(index.calls) == calls


@pytest.mark.parametrize('identity', ['candidate_id', 'knowledge_id'])
def test_parser_identity_change_requires_review_before_any_new_import(system, monkeypatch, identity):
    from rag_app import knowledge_processing
    index = enable(system)
    index.failure = RuntimeError('synthetic index unavailable')
    receipt = intake(system)
    system.service.run_once()
    with system.store._connection() as connection:
        original_submission = dict(connection.execute('SELECT * FROM knowledge_submissions').fetchone())
        original_record = connection.execute('SELECT record_json FROM knowledge_revisions').fetchone()[0]
    parse = knowledge_processing.process_batch

    def changed_candidates(*args, **kwargs):
        batch = parse(*args, **kwargs)
        # Models a parser changing derived case boundaries or source membership.
        batch['assets'][0][identity] += '_changed'
        return batch

    monkeypatch.setattr(knowledge_processing, 'process_batch', changed_candidates)
    index.failure = None
    calls = len(index.calls)
    system.now[0] += 301
    system.service.run_once()
    after = receipt_job(system, receipt)
    assert after['status'] == 'needs_review', after
    assert after['error_code'] in {'processing_source_identity_changed', 'processing_submission_mismatch'}
    assert len(index.calls) == calls and system.catalogue.published_count() == 0
    with system.store._connection() as connection:
        assert list(map(dict, connection.execute('SELECT * FROM knowledge_submissions'))) == [original_submission]
        assert [row[0] for row in connection.execute('SELECT record_json FROM knowledge_revisions')] == [original_record]


def test_resume_missing_old_rule_decision_is_review_only(system, monkeypatch):
    from rag_app.knowledge_governance import GovernanceStore
    index = enable(system)
    index.failure = RuntimeError('synthetic index unavailable')
    receipt = intake(system)
    original = GovernanceStore.import_asset

    def legacy_import(self, entry, *args, **kwargs):
        evidence = {key: value for key, value in entry['evidence'].items() if key != 'automatic_rule_reasons'}
        return original(self, {**entry, 'evidence': evidence}, *args, **kwargs)

    with monkeypatch.context() as old:
        old.setattr(GovernanceStore, 'import_asset', legacy_import)
        system.service.run_once()
    assert receipt_job(system, receipt)['status'] == 'queued'
    index.failure = None
    calls = len(index.calls)
    system.now[0] += 301
    system.service.run_once()
    after = receipt_job(system, receipt)
    assert after['status'] == 'needs_review'
    assert asset_item(after)['reason_codes'] == ['processing_decision_unavailable']
    assert len(index.calls) == calls and system.catalogue.published_count() == 0


def test_resume_submission_owner_mismatch_is_reviewed_without_retry_loop(system):
    index = enable(system)
    index.failure = RuntimeError('synthetic index unavailable')
    receipt = intake(system)
    system.service.run_once()
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_submissions SET principal='user:other'")
    index.failure = None
    calls = len(index.calls)
    system.now[0] += 301
    system.service.run_once()
    result = receipt_job(system, receipt)
    assert result['status'] == 'needs_review' and result['error_code'] == 'processing_submission_mismatch'
    assert len(index.calls) == calls and system.catalogue.published_count() == 0
    assert not system.service.run_once()


def test_completed_legacy_receipts_are_not_reenqueued_or_auto_published(system):
    submit(system)
    system.store.enqueue_verified()
    with system.store._connection(write=True) as connection:
        connection.execute('UPDATE knowledge_processing_jobs SET rule_version=?', (LEGACY_RULE_VERSION,))
    enable(system)
    system.service.run_once()
    assert job(system)['counts']['draft'] == 1
    assert not system.service.run_once()
    assert system.catalogue.published_count() == 0


def test_resolution_rejects_stale_version_and_other_owner_receipt(system):
    enable(system)
    first = intake(system, package(**{'共享范围': ''}))
    system.service.run_once()
    result = receipt_job(system, first)
    item = asset_item(result)
    other = intake(system, principal='user:b')
    with pytest.raises(ProcessingError, match='replacement_receipt_not_found'):
        system.store.resolve(result['job_id'], item['item_id'], None, expected_version=item['version'],
            action='replace', reason='补充可共享版本', replacement_receipt_id=other['receipt_id'], actor='reviewer')
    resolved = system.store.resolve(result['job_id'], item['item_id'], None, expected_version=item['version'],
                                   action='archive', reason='保留原件等待后续整理', actor='reviewer')
    assert resolved['status'] == 'archived' and resolved['version'] == item['version'] + 1
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(result['job_id'], item['item_id'], None, expected_version=item['version'],
                             action='archive', reason='重复操作', actor='reviewer')
    result = system.store.get_job(result['job_id'], None)
    assert result['status'] == 'completed'
    assert any(event['actor'] == 'reviewer' and event['event_type'] == 'item_resolved' for event in result['events'])


def test_verified_replacement_is_enqueued_once_and_original_retained(system):
    enable(system)
    first = intake(system, package(**{'共享范围': ''}))
    system.service.run_once()
    result = receipt_job(system, first)
    item = asset_item(result)
    replacement = intake(system, package(base=1))
    resolved = system.store.resolve(result['job_id'], item['item_id'], None, expected_version=item['version'],
        action='replace', reason='补充共享与使用范围', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert resolved['replacement_receipt_id'] == replacement['receipt_id']
    assert system.store.enqueue_verified() == 0
    assert system.service.run_once()
    assert system.catalogue.published_count() == 1
    assert first['object_key'] in system.bucket.data and replacement['object_key'] in system.bucket.data


def replacement_package():
    import zipfile
    original = package(BODY + '\n[必需的核对清单](missing.md)')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files['正文/missing.md'] = '# 核对清单\n记录来源、验收人和附件完整性。\n'
    files['独立待核对.png'] = b'synthetic-image'
    return original, zip_bytes(files)


@pytest.mark.parametrize('already_processed', [False, True])
def test_replacement_links_only_matching_unpublished_source_and_preserves_siblings(system, already_processed):
    enable(system)
    original, corrected = replacement_package()
    receipt = intake(system, original)
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    assert item['revision'] is None and 'unresolved_reference' in item['reason_codes']
    with system.store._connection() as connection:
        identity = connection.execute('SELECT canonical_identity FROM knowledge_item_source_bindings WHERE item_id=?',
                                      (item['item_id'],)).fetchone()['canonical_identity']
    replacement = intake(system, corrected)
    siblings = None
    if already_processed:
        assert system.service.run_once()
        blocked = receipt_job(system, replacement)
        target = asset_item(blocked)
        assert target['reason_codes'] == ['source_relation_review'] and target['revision'] is None
        siblings = [row for row in blocked['items'] if row['item_id'] != target['item_id']]
    resolved = system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
        action='replace', reason='补齐原资料引用的核对清单', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert system.store.get_job(resolved['replacement_job_id'], None)['status'] == 'queued'
    if siblings:
        assert system.store.get_job(resolved['replacement_job_id'], None)['attention_required']
    assert system.service.run_once()
    after = receipt_job(system, replacement)
    repaired = asset_item(after)
    assert repaired['status'] == 'published' and repaired['revision'] == 1
    with system.store._connection() as connection:
        assert connection.execute('SELECT canonical_identity FROM knowledge_item_source_bindings WHERE item_id=?',
                                  (repaired['item_id'],)).fetchone()['canonical_identity'] == identity
    if siblings is not None:
        assert [row for row in after['items'] if row['item_id'] != repaired['item_id']] == siblings
    assert asset_item(receipt_job(system, receipt))['status'] == 'archived'
    assert system.bucket.data[receipt['object_key']] == original
    assert system.bucket.data[replacement['object_key']] == corrected


def test_replacement_rejects_running_target_before_archiving_original(system):
    enable(system)
    original, corrected = replacement_package()
    receipt = intake(system, original)
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    replacement = intake(system, corrected)
    assert system.store.enqueue_verified() == 1
    claimed = system.store.claim()
    assert claimed['receipt_id'] == replacement['receipt_id']
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
            action='replace', reason='等待补件检查完成', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert receipt_job(system, receipt) == before
    assert system.store.get_job(claimed['job_id'], None)['status'] == 'running'


def test_replacement_relation_does_not_guess_a_draft_revision_baseline(system):
    enable(system)
    receipt = intake(system, package(**{'共享范围': ''}))
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    assert item['revision'] == 1
    replacement = intake(system, package(BODY + '\n补齐复用信息。'))
    system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
        action='replace', reason='重新提交补充资料', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert system.service.run_once()
    blocked = asset_item(receipt_job(system, replacement))
    assert blocked['status'] == 'needs_review' and 'source_base_revision_required' in blocked['reason_codes']
    assert system.catalogue.published_count() == 0


@pytest.mark.parametrize('interruption', ['indexing', 'finish'])
def test_replacement_retry_keeps_exact_target_until_terminal_commit(system, monkeypatch, interruption):
    index = enable(system)
    original, corrected = replacement_package()
    receipt = intake(system, original)
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    replacement = intake(system, corrected)
    assert system.service.run_once()
    blocked = receipt_job(system, replacement)
    target = asset_item(blocked)
    siblings = [row for row in blocked['items'] if row['item_id'] != target['item_id']]
    system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
        action='replace', reason='核对补件关系', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    finish = system.store.finish
    failures = []
    def interrupted(row, status, code=None):
        if status == 'needs_review' and not failures:
            failures.append(True)
            raise RuntimeError('synthetic interruption before terminal commit')
        return finish(row, status, code)
    if interruption == 'indexing':
        index.failure = RuntimeError('synthetic index failure')
    else:
        monkeypatch.setattr(system.store, 'finish', interrupted)
    assert system.service.run_once()
    retrying = receipt_job(system, replacement)
    assert retrying['status'] == 'queued' and retrying['attention_required']
    raw = system.store.item_record(blocked['job_id'], target['item_id'], None)
    assert json.loads(raw['repair_json'])['replacement_recheck']
    assert [row for row in retrying['items'] if row['item_id'] != target['item_id']] == siblings
    index.failure = None
    system.now[0] += 31
    assert system.service.run_once()
    after = receipt_job(system, replacement)
    assert asset_item(after)['status'] == 'published' and after['status'] == 'needs_review'
    assert [row for row in after['items'] if row['item_id'] != target['item_id']] == siblings
    raw = system.store.item_record(blocked['job_id'], target['item_id'], None)
    assert 'replacement_recheck' not in json.loads(raw['repair_json'])
    assert len(index.calls) == (2 if interruption == 'indexing' else 1)


def test_replacement_cannot_rebind_target_with_existing_revision(system):
    enable(system)
    receipt = intake(system, package(**{'共享范围': ''}))
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    replacement = intake(system, package(BODY + '\n仍待核验共享范围。', base=1, **{'共享范围': ''}))
    assert system.service.run_once()
    target_before = receipt_job(system, replacement)
    assert asset_item(target_before)['revision'] == 2
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
            action='replace', reason='不得重新绑定已有修订', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert receipt_job(system, receipt) == before
    assert receipt_job(system, replacement) == target_before
    assert system.catalogue.published_count() == 0


@pytest.mark.parametrize('remaining_issue', [False, True])
def test_replacement_does_not_match_a_different_parser_identity(system, remaining_issue):
    import zipfile
    enable(system)
    original, corrected = replacement_package()
    receipt = intake(system, original)
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    with zipfile.ZipFile(io.BytesIO(corrected)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    if not remaining_issue:
        files.pop('独立待核对.png')
    files['交付清单.csv'] = files['交付清单.csv'].replace(b'WORK-001', b'WORK-002')
    replacement = intake(system, zip_bytes(files))
    assert system.service.run_once()
    target_before = receipt_job(system, replacement)
    assert asset_item(target_before)['status'] == 'published'
    assert target_before['status'] == ('needs_review' if remaining_issue else 'completed')
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
            action='replace', reason='同一收件来源不代表同一资料', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert receipt_job(system, receipt) == before
    assert receipt_job(system, replacement) == target_before


@pytest.mark.parametrize('duplicate', [False, True])
def test_completed_matching_replacement_is_linked_without_requeue_or_catalogue_changes(system, duplicate):
    enable(system)
    first = intake(system, package(**{'共享范围': ''}))
    assert system.service.run_once()
    before = receipt_job(system, first)
    item = asset_item(before)
    replacement = intake(system, package(base=1))
    assert system.service.run_once()
    if duplicate:
        replacement = intake(system, package(base=1))
        assert system.service.run_once()
    target_before = receipt_job(system, replacement)
    assert target_before['status'] == 'completed'
    target = asset_item(target_before)
    assert target['status'] == ('duplicate' if duplicate else 'published')
    published = system.catalogue.get_published(target['knowledge_id'])
    resolved = system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
        action='replace', reason='关联已完成的准确补件', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert resolved['replacement_job_id'] == target_before['job_id'] and resolved['status'] == 'archived'
    assert receipt_job(system, replacement) == target_before
    assert system.catalogue.get_published(target['knowledge_id']) == published
    assert not system.service.run_once()


def test_completed_replacement_cannot_link_a_source_explicitly_split_as_new(system):
    enable(system)
    role = system.catalogue.register_role('品牌部', '设计师')
    first = intake(system, package(BODY + '\n[必需的清单](missing.md)'))
    assert system.service.run_once()
    before = receipt_job(system, first)
    item = asset_item(before)
    replacement = intake(system, package())
    assert system.service.run_once()
    target_before = receipt_job(system, replacement)
    target = asset_item(target_before)
    system.store.set_item_uploader_role(target_before['job_id'], target['item_id'], role['role_id'], 0,
        actor='reviewer', source_action='new')
    assert system.service.run_once()
    target_before = receipt_job(system, replacement)
    assert target_before['status'] == 'completed'
    target = asset_item(target_before)
    published = system.catalogue.get_published(target['knowledge_id'])
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
            action='replace', reason='不得关联已经拆分的新资料', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert receipt_job(system, first) == before
    assert receipt_job(system, replacement) == target_before
    assert system.catalogue.get_published(target['knowledge_id']) == published


def test_replacement_rejects_original_with_publication_proof(system):
    enable(system)
    receipt = intake(system, package())
    assert system.service.run_once()
    published = asset_item(receipt_job(system, receipt))
    original = system.catalogue.get_published(published['knowledge_id'])
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_items SET status='needs_review' WHERE item_id=?", (published['item_id'],))
        connection.execute("UPDATE knowledge_processing_jobs SET status='needs_review' WHERE receipt_id=?", (receipt['receipt_id'],))
    before = receipt_job(system, receipt)
    replacement = intake(system, package(BODY + '\n待核实的补充。'))
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(before['job_id'], published['item_id'], None, expected_version=published['version'],
            action='replace', reason='不能以补件重写已发布原条目', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    assert receipt_job(system, receipt) == before
    assert system.catalogue.get_published(published['knowledge_id']) == original


@pytest.mark.parametrize('status', ['needs_review', 'failed'])
@pytest.mark.parametrize('action', ['archive', 'replace'])
def test_last_review_resolution_closes_attention_and_preserves_history(system, status, action):
    enable(system)
    published_receipt = intake(system, source='published:protected')
    assert system.service.run_once()
    published = asset_item(receipt_job(system, published_receipt))
    original = system.catalogue.get_published(published['knowledge_id'])
    receipt = intake(system, package(**{'共享范围': ''}))
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = asset_item(before)
    replacement = intake(system, package(base=1)) if action == 'replace' else None
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_jobs SET status=?,stage=?,error_code='processing_unavailable' WHERE job_id=?",
                           (status, status, before['job_id']))
        protected = {table: [dict(row) for row in connection.execute('SELECT * FROM ' + table)]
                     for table in ('knowledge_assets', 'knowledge_revisions', 'knowledge_submissions')}
    original_receipt = system.receipts.get(receipt['principal'], receipt['receipt_id'])
    original_objects = dict(system.bucket.data)
    resolved = system.store.resolve(before['job_id'], item['item_id'], None,
        expected_version=item['version'], action=action, reason='资料不合格，保留原件与处理记录',
        replacement_receipt_id=replacement['receipt_id'] if replacement else None, actor='reviewer')
    after = receipt_job(system, receipt)
    assert after['status'] == after['stage'] == 'completed' and after['error_code'] is None
    assert not after['attention_required'] and after['blocking_reasons'] == []
    assert system.store.list_jobs(None, status='attention')['total'] == 0
    assert system.store.stats(None)['attention_jobs'] == 0
    assert resolved['status'] == 'archived' and resolved['version'] == item['version'] + 1
    assert system.receipts.get(receipt['principal'], receipt['receipt_id']) == original_receipt
    assert system.bucket.data == original_objects
    assert system.catalogue.get_published(published['knowledge_id']) == original
    with system.store._connection() as connection:
        assert {table: [dict(row) for row in connection.execute('SELECT * FROM ' + table)]
                for table in protected} == protected
    assert any(row['job_id'] == before['job_id'] for row in system.store.list_receipts(None)['items'])
    assert {event['event_id'] for event in before['events']} <= {event['event_id'] for event in after['events']}
    assert any(event['event_type'] == 'item_resolved' for event in after['events'])
    if replacement:
        assert resolved['replacement_receipt_id'] == replacement['receipt_id']
        assert system.store.get_job(resolved['replacement_job_id'], None)['status'] == 'queued'
        assert system.store.enqueue_verified() == 0


@pytest.mark.parametrize('sibling_status', ['needs_review', 'indexing'])
def test_failed_resolution_keeps_unfinished_sibling_in_attention(system, sibling_status):
    receipt = intake(system, zip_bytes({'first.png': b'first', 'second.png': b'second'}))
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item, sibling = before['items']
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_jobs SET status='failed',stage='failed',error_code='processing_unavailable' WHERE job_id=?",
                           (before['job_id'],))
        connection.execute("UPDATE knowledge_processing_items SET status=? WHERE item_id=?", (sibling_status, sibling['item_id']))
    sibling_before = system.store.item_record(before['job_id'], sibling['item_id'], None)
    system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
                         action='archive', reason='保留此原件，其他条目继续处理', actor='reviewer')
    after = receipt_job(system, receipt)
    assert after['status'] == 'failed' and after['error_code'] == 'processing_unavailable'
    assert after['attention_required'] and system.store.list_jobs(None, status='attention')['total'] == 1
    assert system.store.item_record(before['job_id'], sibling['item_id'], None) == sibling_before


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_resolution_cannot_close_active_job(system, status):
    receipt = intake(system, zip_bytes({'image.png': b'synthetic-image'}))
    assert system.service.run_once()
    before = receipt_job(system, receipt)
    item = before['items'][0]
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_jobs SET status=?,stage=? WHERE job_id=?", (status, status, before['job_id']))
    current = receipt_job(system, receipt)
    with pytest.raises(ProcessingError, match='processing_resolution_conflict'):
        system.store.resolve(before['job_id'], item['item_id'], None, expected_version=item['version'],
                             action='archive', reason='不能处理运行中的条目', actor='reviewer')
    assert receipt_job(system, receipt) == current


def test_publication_commit_survives_job_finish_failure_without_republishing(system, monkeypatch):
    index = enable(system)
    receipt = intake(system)
    original = system.store.finish
    failures = [0]

    def finish_once(row, status, code=None):
        if status == 'completed' and failures[0] == 0:
            failures[0] += 1
            raise RuntimeError('simulated process interruption after publish transaction')
        return original(row, status, code)

    monkeypatch.setattr(system.store, 'finish', finish_once)
    system.service.run_once()
    assert receipt_job(system, receipt)['status'] == 'queued'
    assert system.catalogue.published_count() == 1
    assert asset_item(receipt_job(system, receipt))['publication'] is not None
    system.now[0] += 31
    system.service.run_once()
    assert receipt_job(system, receipt)['status'] == 'completed'
    assert len(index.calls) == 1 and system.catalogue.list_assets()[0]['generation'] == 2


def test_preview_distinguishes_publication_receipt_from_current_pointer(system):
    enable(system)
    receipt = intake(system)
    system.service.run_once()
    result = receipt_job(system, receipt)
    item = asset_item(result)
    preview = system.service.get_item(result['job_id'], item['item_id'], 'user:a')
    assert preview['published'] and preview['currently_published']
    system.catalogue.withdraw(item['knowledge_id'])
    preview = system.service.get_item(result['job_id'], item['item_id'], 'user:a')
    assert preview['published'] and not preview['currently_published']


def test_review_manifest_redaction_blocks_automatic_publication(system):
    index = enable(system)
    receipt = intake(system, package(**{'用途': '流程说明 password=synthetic-private-manifest-value'}))
    system.service.run_once()
    item = asset_item(receipt_job(system, receipt))
    assert item['status'] == 'needs_review' and not item['published']
    assert system.catalogue.published_count() == 0 and not index.calls
    assert any('redaction' in reason for reason in item['reason_codes'])


@pytest.mark.parametrize('field,value', [('来源定位', '未取得'), ('用途', '未知'),
    ('证据状态', '未取得'), ('使用对象', '待确认'), ('输入', '未提供'), ('输出', '未知'), ('适用边界', '待补')])
def test_review_explicit_unknown_provenance_does_not_count_as_complete(system, field, value):
    index = enable(system)
    receipt = intake(system, package(**{field: value}))
    system.service.run_once()
    item = asset_item(receipt_job(system, receipt))
    assert item['status'] == 'needs_review' and not item['published']
    assert system.catalogue.published_count() == 0 and not index.calls


def test_review_explicit_no_dependency_is_a_valid_declaration(system):
    enable(system)
    receipt = intake(system, package(**{'依赖与权限': '无'}))
    system.service.run_once()
    assert asset_item(receipt_job(system, receipt))['published']


@pytest.mark.parametrize('kind', ['different_source', 'same_receipt', 'unverified'])
def test_review_replacement_receipt_eligibility(system, kind):
    enable(system)
    receipt = intake(system, package(**{'共享范围': ''}))
    system.service.run_once()
    result = receipt_job(system, receipt)
    item = asset_item(result)
    if kind == 'same_receipt':
        replacement = receipt
    elif kind == 'different_source':
        replacement = intake(system, source='different-work-source')
    else:
        data = package()
        replacement = system.receipts.prepare(receipt['principal'], dict(idempotency_key=uuid.uuid4().hex,
            source_id=receipt['source_id'], filename='assets.zip', byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest()))
    with pytest.raises(ProcessingError, match='replacement_receipt_not_eligible'):
        system.store.resolve(result['job_id'], item['item_id'], None, expected_version=item['version'],
            action='replace', reason='核对补充资料', replacement_receipt_id=replacement['receipt_id'], actor='reviewer')
    current = system.store.item_record(result['job_id'], item['item_id'], None)
    assert current['status'] == 'needs_review' and current['version'] == item['version']


def test_review_manually_archived_file_survives_job_retry(system):
    enable(system)
    data = zip_bytes({'method.md': BODY, 'image.png': b'synthetic-image'})
    receipt = intake(system, data)
    system.service.run_once()
    result = receipt_job(system, receipt)
    item = next(row for row in result['items'] if row['source_path'] == 'image.png')
    # Reproduce a persisted partial outcome followed by exhausted infrastructure retries.
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_jobs SET status='failed',stage='failed' WHERE job_id=?",
                           (result['job_id'],))
    archived = system.store.resolve(result['job_id'], item['item_id'], None, expected_version=item['version'],
                                   action='archive', reason='无需解析此示意图片', actor='reviewer')
    system.store.retry(result['job_id'], None, actor='reviewer')
    system.service.run_once()
    current = system.store.item_record(result['job_id'], item['item_id'], None)
    assert current['status'] == 'archived' and current['version'] == archived['version']
    assert json.loads(current['resolution_json'])['reason'] == '无需解析此示意图片'


def test_review_derived_cases_keep_separate_stable_knowledge_identity(system):
    enable(system)
    data = zip_bytes({'case/SKILL.md': '# 方法\n先核对资料，再参考案例。\n[案例](references/cases.md)\n',
        'case/references/cases.md': '# 工作案例库\n仅供方法参考。\n\n## 01 案例甲\n甲的完整操作步骤。\n\n## 02 案例乙\n乙的完整操作步骤。\n'})
    receipt = intake(system, data)
    system.service.run_once()
    items = receipt_job(system, receipt)['items']
    assert len(items) == 4
    assert len({item['knowledge_id'] for item in items}) == 4
    assert all(item['revision'] == 1 for item in items)
    assert len(system.catalogue.list_assets()) == 4


def test_review_preview_distinguishes_publication_receipt_from_current_pointer(system):
    enable(system)
    original = intake(system)
    system.service.run_once()
    first_job = receipt_job(system, original)
    first_item = asset_item(first_job)
    preview = system.service.get_item(first_job['job_id'], first_item['item_id'], original['principal'])
    assert preview['published'] and preview['currently_published']
    updated = intake(system, package(BODY + '\n新版补充操作。', base=1))
    system.service.run_once()
    second_job = receipt_job(system, updated)
    second_item = asset_item(second_job)
    old = system.service.get_item(first_job['job_id'], first_item['item_id'], original['principal'])
    current = system.service.get_item(second_job['job_id'], second_item['item_id'], updated['principal'])
    assert old['published'] and not old['currently_published']
    assert current['published'] and current['currently_published']
    system.catalogue.withdraw(second_item['knowledge_id'])
    withdrawn = system.service.get_item(second_job['job_id'], second_item['item_id'], updated['principal'])
    assert withdrawn['published'] and not withdrawn['currently_published']
    assert system.catalogue.published_count() == 0
