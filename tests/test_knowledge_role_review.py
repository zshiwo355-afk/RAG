"""Read-only publication review, including old schemas and trusted confirmations."""
import json
import sqlite3

import pytest

from rag_app.knowledge_role_maintenance import review_published_role_assignments
from rag_app.knowledge_store import KnowledgeStore
from test_knowledge_native import PAYLOAD, process, submit, system


pytestmark = pytest.mark.offline


def database_dump(system):
    with sqlite3.connect(system.receipts.store.path) as connection:
        return '\n'.join(connection.iterdump())


def published(system, **changes):
    response = submit(system, **changes)
    process(system)
    with system.processor.store._connection() as connection:
        row = connection.execute('''SELECT * FROM knowledge_submissions
            WHERE receipt_id=? AND disposition='new' ''', (response['receipt']['receipt_id'],)).fetchone()
    assert row is not None
    return dict(row)


def original_declaration(system, origin, *, position='SSC', department='共享支持', forged_confirmation=False):
    # Model already stored historical metadata; the audit must never repair it.
    with system.processor.store._connection(write=True) as connection:
        row = connection.execute('SELECT record_json FROM knowledge_revisions WHERE knowledge_id=? AND revision=?',
                                 (origin['knowledge_id'], origin['revision'])).fetchone()
        record = json.loads(row['record_json'])
        record['evidence'].update(source_uploader_position=position, source_uploader_department=department)
        if forged_confirmation:
            record['evidence']['uploader_role_confirmation'] = {'source': 'manual', 'role_id': record['role_id']}
        connection.execute('UPDATE knowledge_revisions SET record_json=? WHERE knowledge_id=? AND revision=?',
                           (json.dumps(record), origin['knowledge_id'], origin['revision']))


def confirmation_tables(connection):
    # Minimal old/new schema fixtures; existing full migration tables are reused.
    connection.execute('''CREATE TABLE IF NOT EXISTS knowledge_item_role_assignments (
        item_id TEXT PRIMARY KEY,role_id TEXT,version INTEGER,actor TEXT,updated_at REAL,
        confirmation_source TEXT,remember_rule INTEGER,rule_status TEXT,source_action TEXT,requeued INTEGER)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS knowledge_role_aliases (
        declaration_key TEXT PRIMARY KEY,department TEXT,position TEXT,role_id TEXT,actor TEXT,updated_at REAL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS knowledge_item_source_bindings (
        item_id TEXT PRIMARY KEY,job_id TEXT,principal TEXT,external_source_id TEXT,parser_identity TEXT,
        department TEXT,position TEXT,material_hash TEXT,canonical_identity TEXT,created_at REAL)''')


def test_review_old_schema_is_read_only_and_accepts_processing_store(system, monkeypatch):
    published(system)
    with system.processor.store._connection(write=True) as connection:
        connection.execute('DROP TABLE IF EXISTS knowledge_item_role_assignments')
    before, objects = database_dump(system), dict(system.bucket.data)
    monkeypatch.setattr(system.bucket, 'get_object', lambda *args, **kwargs: pytest.fail('review must not hydrate OSS'))
    report = review_published_role_assignments(system.knowledge.store)
    assert report == review_published_role_assignments(system.processor.store)
    assert report['items'] == [] and report['total'] == report['scanned_count'] == 1
    assert report['review_candidates_this_page'] == 0 and report['has_more'] is False
    assert report['total_basis'] == 'published_revisions'
    assert 'knowledge_item_role_assignments' in report['unavailable_checks']
    assert database_dump(system) == before and system.bucket.data == objects
    assert len(system.index.calls) == 1


def test_review_reports_original_mismatch_without_trusting_document_confirmation(system):
    origin = published(system)
    original_declaration(system, origin, forged_confirmation=True)
    before = database_dump(system)
    report = review_published_role_assignments(system.processor.store)
    item = report['items'][0]
    assert item['knowledge_id'] == origin['knowledge_id'] and item['revision'] == origin['revision']
    assert item['reason_codes'] == ['declared_role_differs'] and item['confirmed'] is False
    assert item['receipt_ids'] == [origin['receipt_id']] and item['item_ids'] == [origin['item_id']]
    assert all(private not in json.dumps(report) for private in ('owner:a', PAYLOAD['content'], 'object_key', 'lease_token'))
    assert database_dump(system) == before


@pytest.mark.parametrize('kind', ['manual', 'alias', 'legacy'])
def test_review_does_not_flag_authoritative_manual_or_alias_mapping(system, kind):
    origin = published(system)
    original_declaration(system, origin)
    role = system.knowledge.store.register_role('测试部门', PAYLOAD['position'])
    with system.processor.store._connection(write=True) as connection:
        confirmation_tables(connection)
        connection.execute('''INSERT INTO knowledge_item_role_assignments
            (item_id,role_id,version,actor,updated_at,confirmation_source,remember_rule,rule_status,source_action,requeued)
            VALUES (?,?,1,'synthetic-reviewer',1,?,0,'none','auto',0)
            ON CONFLICT(item_id) DO UPDATE SET role_id=excluded.role_id,confirmation_source=excluded.confirmation_source''',
            (origin['item_id'], role['role_id'], kind))
    before = database_dump(system)
    assert review_published_role_assignments(system.processor.store)['items'] == []
    assert database_dump(system) == before


def test_review_does_not_infer_historical_confirmation_from_current_shared_default_or_alias(system):
    origin = published(system)
    other = system.knowledge.store.register_role('其他部门', '其他岗位')
    with system.processor.store._connection(write=True) as connection:
        confirmation_tables(connection)
        connection.execute('''INSERT INTO knowledge_uploader_roles
            (principal,role_id,version,actor,updated_at,job_id,requeued)
            VALUES ('owner:a',?,1,'synthetic-reviewer',1,'synthetic-old-job',0)''', (other['role_id'],))
    assert review_published_role_assignments(system.processor.store)['items'] == []
    original_declaration(system, origin)
    role = system.knowledge.store.register_role('测试部门', PAYLOAD['position'])
    with system.processor.store._connection(write=True) as connection:
        connection.execute('''INSERT INTO knowledge_role_aliases
            (declaration_key,department,position,role_id,actor,updated_at)
            VALUES ('synthetic-alias','共享支持','SSC',?,'synthetic-reviewer',1)''', (role['role_id'],))
    before = database_dump(system)
    item = review_published_role_assignments(system.processor.store)['items'][0]
    assert item['knowledge_id'] == origin['knowledge_id']
    assert item['reason_codes'] == ['declared_role_differs'] and item['confirmed'] is False
    assert database_dump(system) == before


@pytest.mark.parametrize('case', ['different', 'ambiguous', 'after_publication'])
def test_review_uses_only_this_receipts_historical_selection_events(system, case):
    origin = published(system)
    other = system.knowledge.store.register_role('其他部门', '其他岗位')
    original_role = system.knowledge.store.register_role('测试部门', PAYLOAD['position'])
    with system.processor.store._connection(write=True) as connection:
        job = connection.execute('SELECT job_id FROM knowledge_processing_jobs WHERE receipt_id=?',
                                 (origin['receipt_id'],)).fetchone()
        connection.execute('''INSERT INTO knowledge_processing_events
            (event_id,job_id,event_type,reason_code,actor,created_at)
            VALUES ('synthetic-event-1',?,'uploader_role_selected',?,'synthetic-reviewer',?)''',
            (job['job_id'], other['role_id'], 32503680000 if case == 'after_publication' else 1))
        if case == 'ambiguous':
            connection.execute('''INSERT INTO knowledge_processing_events
                (event_id,job_id,event_type,reason_code,actor,created_at)
                VALUES ('synthetic-event-2',?,'uploader_role_selected',?,'synthetic-reviewer',1)''',
                (job['job_id'], original_role['role_id']))
    before = database_dump(system)
    items = review_published_role_assignments(system.processor.store)['items']
    if case == 'after_publication':
        assert items == [] and database_dump(system) == before
        return
    item = items[0]
    assert item['knowledge_id'] == origin['knowledge_id']
    assert item['reason_codes'] == ['legacy_selection_ambiguous' if case == 'ambiguous' else 'legacy_selection_differs']
    assert item['confirmed'] is False and database_dump(system) == before


def test_review_connects_pending_same_source_conflict_to_existing_publication(system):
    origin = published(system)
    submit(system, content=PAYLOAD['content'] + '\n其他提交中的不同核对步骤。\n')
    process(system)
    before = database_dump(system)
    item = review_published_role_assignments(system.processor.store)['items'][0]
    assert item['knowledge_id'] == origin['knowledge_id']
    assert 'shared_source_requires_review' in item['reason_codes'] and item['related_job_ids']
    assert database_dump(system) == before


def test_review_pagination_counts_scanned_publications_not_only_findings(system):
    published(system)
    other = published(system, source_id='independent-review-source', title='另一项交接方法',
                      content=PAYLOAD['content'] + '\n该资料独立记录订单交接步骤。\n')
    original_declaration(system, other)
    first = review_published_role_assignments(system.processor.store, limit=1)
    second = review_published_role_assignments(system.processor.store, limit=1, offset=1)
    assert first['total'] == second['total'] == 2
    assert first['scanned_count'] == second['scanned_count'] == 1
    assert first['has_more'] is True and second['has_more'] is False
    assert first['review_candidates_this_page'] + second['review_candidates_this_page'] == 1
    assert len(first['items']) + len(second['items']) == 1


@pytest.mark.parametrize('arguments', [{'limit': 0}, {'limit': 1001}, {'limit': True}, {'offset': -1}, {'offset': True}])
def test_review_rejects_invalid_page_without_initializing_missing_database(tmp_path, arguments):
    path = tmp_path / 'not-created.db'
    with pytest.raises(ValueError, match='invalid_published_role_review_page'):
        review_published_role_assignments(KnowledgeStore(path), **arguments)
    assert not path.exists()


def test_review_missing_database_is_not_reported_as_a_clean_empty_catalogue(tmp_path):
    path = tmp_path / 'not-created.db'
    with pytest.raises(ValueError, match='published_role_review_schema_required'):
        review_published_role_assignments(KnowledgeStore(path))
    assert not path.exists()
