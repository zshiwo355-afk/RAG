"""Readable titles are projected from existing, owner-scoped processing records."""
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import threading
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_app.knowledge_processing_api import get_processing_store, router
from rag_app.knowledge_native import TextSubmission, source_package
from rag_app.knowledge_processing import ARTIFACT_PREFIX, UploadRoleMetadata
from rag_app.knowledge_intake import SOURCE_PROTOCOL
from test_knowledge_native import PAYLOAD
from test_knowledge_processing import BODY, submit, system, zip_bytes
from test_knowledge_processing_phase3 import enable, intake, receipt_job
from test_knowledge_receipt_history import BASE, TOKEN, headers

pytestmark = pytest.mark.offline


@pytest.fixture
def display_api(system, monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_PROCESSING_ENABLED', '1')
    monkeypatch.setenv('KNOWLEDGE_RECEIPTS_TOKEN', TOKEN)
    app = FastAPI()
    app.include_router(router)
    metadata = UploadRoleMetadata(read=system.service.artifacts.get)
    system.store.display_metadata = metadata
    app.dependency_overrides[get_processing_store] = lambda: system.store
    try:
        with TestClient(app) as client:
            yield client
    finally:
        metadata.executor.shutdown(wait=True)


def test_receipt_and_job_show_real_material_title_without_renaming_original(system, display_api):
    enable(system)
    receipt = intake(system, principal='alice')
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    for path in ('/receipts', '/jobs', '/jobs/' + job['job_id']):
        response = display_api.get(BASE + path, headers=headers())
        assert response.status_code == 200
        row = response.json() if path.startswith('/jobs/') else response.json()['items'][0]
        assert row['display_title'] == '新人工作资料核对方法'
        assert row['material_count'] == 1  # The delivery manifest is not a second material.
        assert row['filename'] == 'assets.zip'
        assert row['receipt_id'] == receipt['receipt_id']


def test_multiple_materials_ignore_noncontent_and_detail_pagination(system, display_api):
    receipt = submit(system, zip_bytes({'01.md': BODY, '02.md': BODY.replace('工作方法', '交接核对'),
                                      'tools/run.sh': 'echo never-execute', 'empty.txt': ''}),
                     filename='bundle.zip', principal='alice')
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    for path in ('/receipts', '/jobs', '/jobs/' + job['job_id'] + '?limit=1&item_offset=3'):
        response = display_api.get(BASE + path, headers=headers())
        assert response.status_code == 200
        row = response.json() if path.startswith('/jobs/') else response.json()['items'][0]
        assert row['display_title'] == '工作方法'
        assert row['material_count'] == 2
        assert row['filename'] == 'bundle.zip'


def test_unprocessed_and_empty_page_have_no_invented_title(system, display_api):
    row = system.receipts.prepare('alice', dict(idempotency_key='waiting', source_id='source:waiting',
        filename='native-text.zip', byte_length=10, sha256='0' * 64))
    value = display_api.get(BASE + '/receipts', headers=headers()).json()['items'][0]
    assert value['receipt_id'] == row['receipt_id']
    assert value['display_title'] is None and value['material_count'] == 0
    assert display_api.get(BASE + '/receipts?offset=1', headers=headers()).json()['items'] == []
    assert system.store.list_jobs('alice')['items'] == []


def test_manifest_and_blank_titles_are_not_materials_even_with_legacy_references(system, display_api):
    receipt = intake(system, principal='alice')
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_items SET content_ref='{}' WHERE job_id=?", (job['job_id'],))
        connection.execute("UPDATE knowledge_processing_items SET title='  ' WHERE job_id=? AND source_path='正文/方法.md'", (job['job_id'],))
    for path in ('/receipts', '/jobs', '/jobs/' + job['job_id']):
        response = display_api.get(BASE + path, headers=headers())
        assert response.status_code == 200
        value = response.json() if path.startswith('/jobs/') else response.json()['items'][0]
        assert value['display_title'] is None and value['material_count'] == 0


def test_a_page_reads_material_titles_in_one_query_without_loading_bodies(system, monkeypatch):
    for number in range(3):
        submit(system, BODY.replace('工作方法', '工作方法' + str(number)), principal='alice',
               source_id='notes:' + str(number))
        assert system.service.run_once()
    statements = []
    original = system.store._connection

    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    def no_body_read(*args, **kwargs):
        raise AssertionError('Lists must not fetch original files or bodies')

    monkeypatch.setattr(system.store, '_connection', traced)
    monkeypatch.setattr(system.bucket, 'get_object', no_body_read)
    for read in (system.store.list_receipts, system.store.list_jobs):
        statements.clear()
        result = read('alice', limit=3)
        assert len(result['items']) == 3
        assert all(row['material_count'] == 1 for row in result['items'])
        title_queries = [query for query in statements if 'SELECT i.job_id,i.title,i.source_path,i.reason_codes' in query]
        assert len(title_queries) == 1
        assert not any(query.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for query in statements)


def test_blocking_summary_reads_all_current_items_and_ignores_resolved_history(system, display_api):
    receipt = submit(system, zip_bytes({
        '01-reference.md': BODY + '\n[附件](missing.md)',
        '02-script.md': BODY + '\n运行 `scripts/missing.py`',
        '03-sensitive.md': BODY + '\npassword=test-not-real-secret\n',
        '04-draft.md': BODY,
        'tools/archive.sh': 'echo never-execute',
    }), filename='bundle.zip', principal='alice')
    assert system.service.run_once()
    job = receipt_job(system, receipt)
    resolved = next(item for item in job['items'] if item['source_path'] == '03-sensitive.md')
    system.store.resolve(job['job_id'], resolved['item_id'], None, actor='reviewer',
                         action='archive', reason='保留原件', expected_version=resolved['version'])
    with system.store._connection(write=True) as connection:
        system.store._event(connection, job['job_id'], 'item_needs_review', 'uploader_role_not_found', 'reviewer')
        connection.execute("UPDATE knowledge_processing_jobs SET error_code='processing_unavailable' WHERE job_id=?", (job['job_id'],))
    expected = ['processing_unavailable', 'script_dependency_not_provided', 'unresolved_reference']
    listing = display_api.get(BASE + '/jobs', headers=headers()).json()['items'][0]
    detail = display_api.get(BASE + '/jobs/' + job['job_id'] + '?limit=1&item_offset=4', headers=headers()).json()
    assert len(detail['items']) == 1
    for result in (listing, detail):
        assert result['blocking_reasons'] == expected
        assert result['blocking_reasons_truncated'] is False
        assert 'redaction_review' not in result['blocking_reasons']
        assert 'reviewer_archived' not in result['blocking_reasons']
        assert 'uploader_role_not_found' not in result['blocking_reasons']


def test_blocking_summary_is_bounded_private_and_batch_queried(system, monkeypatch):
    from rag_app.knowledge_processing import PUBLIC_BLOCKING_REASONS
    jobs = []
    for principal in ('alice', 'alice', 'bob'):
        receipt = submit(system, BODY + f'\n材料 {len(jobs)}\n[附件](missing.md)', principal=principal, source_id='source-' + str(len(jobs)))
        assert system.service.run_once()
        jobs.append(receipt_job(system, receipt))
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_items SET reason_codes=? WHERE job_id=? AND status='needs_review'",
                           (json.dumps(sorted(PUBLIC_BLOCKING_REASONS)), jobs[0]['job_id']))
        connection.execute("UPDATE knowledge_processing_items SET reason_codes=? WHERE job_id=? AND status='needs_review'",
                           (json.dumps(['private_source_text', 'https://private.invalid/secret', {'secret': 'never-display'}]), jobs[1]['job_id']))
        connection.execute("UPDATE knowledge_processing_jobs SET status='failed',error_code='raw private traceback' WHERE job_id=?", (jobs[1]['job_id'],))
        connection.execute("UPDATE knowledge_processing_items SET reason_codes='[\"uploader_role_not_found\"]' WHERE job_id=?", (jobs[2]['job_id'],))
    statements = []
    original = system.store._connection

    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(system.store, '_connection', traced)
    result = system.store.list_jobs('alice', status='attention')
    assert len(result['items']) == 2
    bounded = next(row for row in result['items'] if row['job_id'] == jobs[0]['job_id'])
    unknown = next(row for row in result['items'] if row['job_id'] == jobs[1]['job_id'])
    assert len(bounded['blocking_reasons']) == 32 and bounded['blocking_reasons_truncated']
    assert unknown['blocking_reasons'] == ['manual_review_required'] and not unknown['blocking_reasons_truncated']
    assert len([statement for statement in statements if 'SELECT DISTINCT job_id,reason_codes' in statement]) == 1
    assert not any(statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for statement in statements)
    with system.store._connection(write=True) as connection:
        connection.execute("UPDATE knowledge_processing_jobs SET status='queued' WHERE principal='alice'")
    assert all(row['blocking_reasons'] == [] for row in system.store.list_jobs('alice')['items'])


def test_upload_declaration_belongs_to_its_receipt_and_historical_revision(system, display_api):
    enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    data = source_package(TextSubmission(**{**PAYLOAD, 'position': 'AI架构师', 'department': '总经办'}))
    first = intake(system, data, principal='alice')
    assert system.service.run_once()
    first_job = receipt_job(system, first)
    item = next(item for item in first_job['items'] if item['knowledge_id'])
    repeated = intake(system, data, principal='alice')
    assert system.service.run_once()
    duplicate = receipt_job(system, repeated)
    assert duplicate['uploader_positions'] == ['AI架构师']  # From its own intake metadata, not the previous revision.
    update = source_package(TextSubmission(**{**PAYLOAD, 'position': 'AI架构师', 'department': '总经办',
        'content': PAYLOAD['content'] + '\n补充：交接时核对修订日期。', 'base_revision': 1}))
    latest = intake(system, update, principal='alice')
    assert system.service.run_once()
    for path in ('/receipts', '/jobs', '/jobs/' + first_job['job_id']):
        result = display_api.get(BASE + path, headers=headers()).json()
        row = result if path.startswith('/jobs/') else next(row for row in result['items'] if row['receipt_id'] == first['receipt_id'])
        assert 'uploader_principal_id' not in row
        assert row['uploader_positions'] == ['AI架构师'] and row['uploader_departments'] == ['总经办']
    assert receipt_job(system, latest)['uploader_positions'] == ['AI架构师']
    # A copied attribution from another receipt must not be displayed as this upload's position.
    with system.store._connection(write=True) as db:
        stored = db.execute('SELECT record_json FROM knowledge_revisions WHERE knowledge_id=? AND revision=?',
                            (item['knowledge_id'], item['revision'])).fetchone()
        record = json.loads(stored['record_json'])
        record['evidence']['uploader_attribution']['receipt_id'] = latest['receipt_id']
        record['contributor'] = '伪造上传人'
        db.execute('UPDATE knowledge_revisions SET record_json=? WHERE knowledge_id=? AND revision=?',
                   (json.dumps(record), item['knowledge_id'], item['revision']))
    row = display_api.get(BASE + '/jobs/' + first_job['job_id'], headers=headers()).json()
    assert row['uploader_positions'] == ['AI架构师'] and row['uploader_departments'] == ['总经办']
    assert 'uploader_principal_id' not in row and '伪造上传人' not in json.dumps(row, ensure_ascii=False)
    assert display_api.get(BASE + '/jobs/' + first_job['job_id'], headers=headers(principal='bob')).status_code == 404


def test_multiple_declared_positions_are_not_collapsed_to_one(system, display_api):
    enable(system)
    files, rows = {}, []
    for number, (department, position) in enumerate((('总经办', 'AI架构师'), ('组织部', '人事专员'))):
        system.catalogue.register_role(department, position)
        data = source_package(TextSubmission(**{**PAYLOAD, 'source_id': 'multi:' + str(number),
            'position': position, 'department': department, 'title': position + '交接方法',
            'content': PAYLOAD['content'] + '\n岗位检查项：' + position}))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            rows.extend(csv.DictReader(io.StringIO(archive.read('交付清单.csv').decode())))
            files.update({name: archive.read(name) for name in archive.namelist() if name != '交付清单.csv'})
    manifest = io.StringIO(newline='')
    writer = csv.DictWriter(manifest, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files['交付清单.csv'] = manifest.getvalue()
    receipt = intake(system, zip_bytes(files), principal='alice')
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    row = display_api.get(BASE + '/jobs/' + result['job_id'] + '?limit=1', headers=headers()).json()
    assert set(row['uploader_positions']) == {'AI架构师', '人事专员'}
    assert set(row['uploader_departments']) == {'总经办', '组织部'}


@pytest.mark.parametrize('position,content,reason', [
    ('怀仁集团技术部', PAYLOAD['content'], 'uploader_role_not_found'),
    ('AI架构师', PAYLOAD['content'] + '\n运行 `scripts/knowledge.py` 执行导入。', 'script_dependency_not_provided'),
])
def test_unpublished_review_items_show_their_own_uploaded_declaration(system, display_api, position, content, reason):
    enable(system)
    system.catalogue.register_role('总经办', 'AI架构师')
    data = source_package(TextSubmission(**{**PAYLOAD, 'position': position, 'content': content}))
    receipt = intake(system, data, principal='alice')
    assert system.service.run_once()
    result = receipt_job(system, receipt)
    item = next(item for item in result['items'] if reason in item['reason_codes'])
    assert item['revision'] is None and result['status'] == 'needs_review'
    for path in ('/receipts', '/jobs', '/jobs/' + result['job_id']):
        response = display_api.get(BASE + path, headers=headers())
        assert response.status_code == 200
        row = response.json() if path.startswith('/jobs/') else response.json()['items'][0]
        assert row['uploader_positions'] == [position] and row['uploader_departments'] == []
        assert 'metadata_ref' not in response.text and 'object_key' not in response.text
        assert 'principal' not in row and 'uploader_principal_id' not in row


def test_metadata_projection_retries_failure_checks_identity_and_keeps_cache_scoped(system):
    receipt, item = '1' * 32, '2' * 32
    body = json.dumps({'item': {'item_id': item}, 'entry': {'evidence': {
        'receipt_id': receipt, 'source_protocol': SOURCE_PROTOCOL, 'source_uploader_position': 'AI架构师'}}})
    ref = system.service.artifacts.put(body)
    row = {'receipt_id': receipt, 'item_id': item, 'metadata_ref': json.dumps(ref)}
    reads = []
    def read(value):
        reads.append(value['sha256'])
        return system.service.artifacts.get(value)
    projection = UploadRoleMetadata(read=read)
    try:
        original = system.bucket.data[ref['object_key']]
        system.bucket.data[ref['object_key']] = b'corrupted metadata'
        assert projection.project([row], 'first-database') == {}
        assert not projection.cache
        system.bucket.data[ref['object_key']] = original
        assert projection.project([row], 'first-database') == {item: ('AI架构师', None)}
        assert projection.project([row], 'first-database') == {item: ('AI架构师', None)}
        assert len(reads) == 2
        assert projection.project([row], 'second-database') == {item: ('AI架构师', None)}
        assert len(reads) == 3
        for changed in ({'receipt_id': '3' * 32}, {'item_id': '4' * 32}):
            assert projection.project([{**row, **changed}], 'first-database') == {}
        assert all(value == ('AI架构师', None) for value in projection.cache.values())
        calls = len(reads)
        for invalid in ({**ref, 'byte_length': 128 * 1024 + 1}, {**ref, 'object_key': 'private/source'}):
            assert projection.project([{**row, 'metadata_ref': json.dumps(invalid)}], 'first-database') == {}
        assert len(reads) == calls
    finally:
        projection.executor.shutdown(wait=True)


def test_metadata_reads_are_bounded_and_do_not_block_the_request_until_finished():
    gate, entered, rows = threading.Event(), [], []
    for number in range(30):
        digest = hashlib.sha256(str(number).encode()).hexdigest()
        ref = {'object_key': ARTIFACT_PREFIX + '/' + digest + '.txt', 'sha256': digest, 'byte_length': 128}
        rows.append({'receipt_id': str(number), 'item_id': str(number), 'metadata_ref': json.dumps(ref)})
    def read(ref):
        entered.append(ref['sha256'])
        assert gate.wait(2)
        raise RuntimeError('temporary unavailable')
    projection = UploadRoleMetadata(read=read)
    try:
        started = time.monotonic()
        assert projection.project(rows, 'bounded', timeout=.02) == {}
        assert time.monotonic() - started < .5
        assert 0 < len(entered) <= 4 and len(projection.pending) == 16
        assert projection.project(rows, 'bounded', timeout=0) == {}
        assert len(projection.pending) == 16
    finally:
        gate.set()
        projection.executor.shutdown(wait=True)
    assert not projection.pending and not projection.cache
