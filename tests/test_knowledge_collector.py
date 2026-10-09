from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

import knowledge_collector as collector
from rag_app.knowledge_intake import process_batch
from rag_app.knowledge_processing import ProcessingService
from rag_app.knowledge_receipts import ReceiptObjects, ReceiptService, ReceiptStore
from test_knowledge_receipts import FakeBucket
import upload_knowledge_receipt as uploader


pytestmark = pytest.mark.offline


def event(identifier='message-1', *, session='session-a', agent='hermes', text='核对输入后保存结果；失败时保留记录。', **extra):
    return {'id': identifier, 'source_session_id': session, 'agent': agent, 'role': 'user',
            'timestamp': 101.0, 'text': text, **extra}


def decision(ids):
    return {'event_ids': ids, 'title': '输入核验方法', 'asset_type': '方法', 'sharing': 'company',
            'purpose': '参考输入核验及失败处理方法', 'audience': '执行资料核验的同事',
            'inputs': '待核验资料', 'outputs': '结果及失败记录', 'dependencies': '无',
            'boundaries': '只作历史方法参考，不作为效果证明', 'reason': '保留完整原句及限制'}


@pytest.fixture
def system(tmp_path):
    home, root = tmp_path / 'collector', tmp_path / 'work'
    home.mkdir()
    root.mkdir()
    config = {'allowed_roots': [str(root)], 'sources': [{'agent': 'hermes', 'path': str(tmp_path / 'hermes.db')}],
              'enabled_at': 100.0, 'installation_id': 'a' * 32, 'mode': 'pilot_manual', 'interval_days': 15}
    with collector.database(home) as db:
        with db:
            db.execute("INSERT INTO settings VALUES('config',?)", (collector.dumps(config),))
        yield SimpleNamespace(home=home, root=root, config=config, db=db)


def queue(system, events):
    with system.db:
        collector._commit_snapshot(system.db, 'fixture-source', {'cursor': 1}, events, [], 101.0)


def prepared(system):
    queue(system, [event()])
    result = collector.prepare(system.db, system.home, system.config, lambda events: [decision([e['id'] for e in events])])
    assert result == {'prepared': 1, 'uploaded': False}
    return system.db.execute('SELECT * FROM outbox').fetchone()


def test_collection_checkpoint_and_events_rollback_together(system, monkeypatch):
    db, config = system.db, system.config
    source_key = 'hermes:' + config['sources'][0]['path']
    with db:
        db.execute('INSERT INTO checkpoints VALUES(?,?)', (source_key, '{"cursor":0}'))
    monkeypatch.setattr(collector, 'snapshot', lambda *a, **kw: {
        'checkpoint': {'cursor': 2}, 'events': [event(), event('message-2', role='tool')], 'issues': []})
    with pytest.raises(ValueError, match='collector_event_contract'):
        collector.collect(db, config, now=102)
    assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 0
    assert json.loads(db.execute('SELECT value FROM checkpoints').fetchone()[0]) == {'cursor': 0}
    assert db.execute("SELECT value FROM settings WHERE key='last_scan'").fetchone() is None

    monkeypatch.setattr(collector, 'snapshot', lambda *a, **kw: {
        'checkpoint': {'cursor': 1}, 'events': [event()], 'issues': []})
    assert collector.collect(db, config, now=103) == {'new_events': 1}
    assert collector.collect(db, config, now=104) == {'new_events': 0}
    assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
    assert json.loads(db.execute('SELECT value FROM checkpoints').fetchone()[0]) == {'cursor': 1}


def test_same_message_id_in_two_sessions_is_not_dropped(system):
    queue(system, [event('1', session='first'), event('1', session='second')])
    rows = system.db.execute('SELECT key,value FROM events').fetchall()
    assert len(rows) == 2 and len({row['key'] for row in rows}) == 2
    selected = [collector._safe_event(event('1', session=s))[0] for s in ('first', 'second')]
    assert collector._zip_bytes([selected[0]], decision(['1']))[0] != collector._zip_bytes([selected[1]], decision(['1']))[0]


def test_initialization_failure_does_not_leave_baseline_or_config(tmp_path, monkeypatch):
    home, root = tmp_path / 'home', tmp_path / 'work'
    home.mkdir()
    root.mkdir()
    config = {'allowed_roots': [str(root)], 'sources': [{'agent': 'hermes', 'path': str(tmp_path / 'source.db')}]}
    with collector.database(home) as db:
        db.execute("CREATE TRIGGER reject_config BEFORE INSERT ON settings WHEN NEW.key='config' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        monkeypatch.setattr(collector, 'snapshot', lambda *a, **kw: {'events': [], 'issues': [], 'checkpoint': {'cursor': 12}})
        with pytest.raises(sqlite3.DatabaseError):
            collector.initialize(db, config, now=100)
        assert db.execute('SELECT count(*) FROM checkpoints').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM settings').fetchone()[0] == 0

        db.execute('DROP TRIGGER reject_config')
        def interrupted(*args, **kwargs):
            raise ValueError('snapshot_interrupted')
        monkeypatch.setattr(collector, 'snapshot', interrupted)
        with pytest.raises(ValueError, match='snapshot_interrupted'):
            collector.initialize(db, config, now=100)
        assert db.execute('SELECT count(*) FROM settings').fetchone()[0] == 0


def test_real_hermes_initialization_has_zero_history_then_reads_append(tmp_path):
    home, root, path = tmp_path / 'home', tmp_path / 'work', tmp_path / 'hermes.db'
    home.mkdir()
    root.mkdir()
    source = sqlite3.connect(path)
    source.executescript('''
      CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,started_at REAL,parent_session_id TEXT,cwd TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,role TEXT,content TEXT,
        timestamp REAL,active INTEGER DEFAULT 1,compacted INTEGER DEFAULT 0,observed INTEGER DEFAULT 0,
        tool_calls TEXT,tool_name TEXT,reasoning TEXT);
    ''')
    source.execute("INSERT INTO sessions VALUES('old','desktop',50,NULL,?)", (str(root),))
    source.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('old','user','historical-private-marker',50)")
    source.commit()
    with collector.database(home) as db:
        config = {'allowed_roots': [str(root)], 'sources': [{'agent': 'hermes', 'path': str(path)}]}
        result = collector.initialize(db, config, now=100)
        assert result['new_events'] == 0 and result['scheduled'] is False
        config = collector.get_config(db)
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 0
        checkpoint = db.execute('SELECT value FROM checkpoints').fetchone()[0]
        assert 'historical-private-marker' not in checkpoint
        source.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('old','user','新增工作方法',101)")
        source.commit()
        assert collector.collect(db, config, now=102)['new_events'] == 1
        assert json.loads(db.execute('SELECT value FROM events').fetchone()[0])['text'] == '新增工作方法'
        assert collector.initialize(db, config, now=103)['existing_checkpoint_preserved'] is True
        assert collector.collect(db, config, now=104)['new_events'] == 0
    source.close()


def test_known_secrets_never_enter_durable_queue_or_reviewer(system):
    secrets = ['synthetic-private-value', 'github_pat_' + 'a' * 30, '13900000000', 'private@example.test']
    raw = event(text='api_key=' + secrets[0] + '\n' + '\n'.join(secrets[1:]),
                reasoning='hidden reasoning must never enter queue', cwd='/private/person', tool_calls=['not content'])
    queue(system, [raw])
    row = system.db.execute('SELECT value,state FROM events').fetchone()
    assert row['state'] == 'needs_review'
    assert all(value not in row['value'] for value in secrets)
    assert 'reasoning' not in row['value'] and 'cwd' not in row['value'] and 'tool_calls' not in row['value']
    assert collector.prepare(system.db, system.home, system.config, lambda _: pytest.fail('unsafe text cannot reach model'))['prepared'] == 0
    # Inspect only this synthetic fixture DB and WAL for pre-redaction persistence.
    for file in system.home.glob('collector.sqlite3*'):
        data = file.read_bytes()
        assert all(secret.encode() not in data for secret in secrets)


def test_fixed_zip_bytes_and_source_v2_server_contract(system, tmp_path):
    values = [collector._safe_event(event())[0], collector._safe_event(event('message-2', role='assistant', text='失败时保留原记录，不虚报完成。'))[0]]
    choice = decision([e['id'] for e in values])
    identity, data = collector._zip_bytes(values, choice)
    assert collector._zip_bytes(values, choice) == (identity, data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('交付清单.csv').decode())))
        assert list(rows[0]) == collector.FIELDS and rows[0]['共享范围'] == '测试待确认'
        body = archive.read(rows[0]['正文路径']).decode()
        assert '[user]' in body and '[assistant]' in body and all(e['text'] in body for e in values)
    path = tmp_path / 'source-v2.zip'
    path.write_bytes(data)
    output = tmp_path / 'intake'
    batch = process_batch([{'path': path, 'source_id': 'collector-' + system.config['installation_id'],
                           'package': path.name, 'sha256': collector.digest(data), 'contributor': '未确认'}], output)
    assert len(batch['assets']) == 1
    asset = batch['assets'][0]
    assert asset['state'] == 'draft_ready' and not asset['issues']
    reasons = ProcessingService._automatic_reasons(asset['metadata'], (output / asset['body_file']).read_text())
    assert reasons == {'sharing_scope_unconfirmed'}


@pytest.mark.parametrize('allow,sharing,expected', [
    (False, 'company', '测试待确认'),
    (True, 'company', '全员'),
    (True, 'uncertain', '未确认'),
])
def test_company_sharing_requires_explicit_opt_in_and_model_scope(system, allow, sharing, expected):
    queue(system, [event()])
    choice = {**decision(['message-1']), 'sharing': sharing, 'inputs': '原始资料'}
    assert collector.prepare(system.db, system.home, system.config, lambda _: [choice],
                             allow_company_sharing=allow)['prepared'] == 1
    row = system.db.execute('SELECT * FROM outbox').fetchone()
    frozen = row['frozen_bytes']
    with zipfile.ZipFile(io.BytesIO(frozen)) as archive:
        manifest = next(csv.DictReader(io.StringIO(archive.read('交付清单.csv').decode())))
    assert manifest['共享范围'] == expected
    # Changing the opt-in later cannot rewrite an already frozen asset.
    assert collector.prepare(system.db, system.home, system.config,
                             lambda _: pytest.fail('must not review packaged events'),
                             allow_company_sharing=not allow)['prepared'] == 0
    assert system.db.execute('SELECT frozen_bytes FROM outbox').fetchone()[0] == frozen


def test_company_sharing_opt_in_never_uploads_restricted_content(system):
    queue(system, [event()])
    choice = {**decision(['message-1']), 'sharing': 'restricted'}
    assert collector.prepare(system.db, system.home, system.config, lambda _: [choice],
                             allow_company_sharing=True)['prepared'] == 0
    assert collector.status(system.db)['events'] == {'needs_review': 1}
    assert system.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


def test_company_sharing_opt_in_preserves_missing_metadata_review_gate(system):
    queue(system, [event()])
    choice = {**decision(['message-1']), 'dependencies': '未确认'}
    collector.prepare(system.db, system.home, system.config, lambda _: [choice],
                      allow_company_sharing=True)
    frozen = system.db.execute('SELECT frozen_bytes FROM outbox').fetchone()[0]
    with zipfile.ZipFile(io.BytesIO(frozen)) as archive:
        manifest = next(csv.DictReader(io.StringIO(archive.read('交付清单.csv').decode())))
    assert manifest['共享范围'] == '未确认'


def test_prepare_session_filter_leaves_other_queued_work_and_baselines_untouched(system):
    queue(system, [event(session='other'), event(session='selected')])
    checkpoints = system.db.execute('SELECT * FROM checkpoints').fetchall()
    reviewed = []
    def review(events):
        reviewed.extend(events)
        return [decision([e['id'] for e in events])]
    assert collector.prepare(system.db, system.home, system.config, review,
                             session_id='selected')['prepared'] == 1
    assert {e['source_session_id'] for e in reviewed} == {'selected'}
    states = {json.loads(row['value'])['source_session_id']: row['state']
              for row in system.db.execute('SELECT value,state FROM events')}
    assert states == {'other': 'queued', 'selected': 'packaged'}
    assert system.db.execute('SELECT * FROM checkpoints').fetchall() == checkpoints
    assert collector.prepare(system.db, system.home, system.config,
                             lambda _: pytest.fail('no matching session'), session_id='missing')['prepared'] == 0


def test_upload_package_filter_preserves_other_packages_and_received_state(system, monkeypatch):
    first = prepared(system)
    queue(system, [event('message-2', session='second')])
    collector.prepare(system.db, system.home, system.config,
                      lambda events: [decision([e['id'] for e in events])])
    second = system.db.execute('SELECT * FROM outbox WHERE id!=?', (first['id'],)).fetchone()
    transferred = []
    def transfer(handle, payload, stamp, call, **kwargs):
        transferred.append(handle.read())
        return {'receipt_id': 'f' * 32, 'status': 'verified', 'content_verified': True, 'published': False}
    monkeypatch.setattr(uploader, 'upload', transfer)
    assert collector.upload_pending(system.db, system.home, system.config, lambda *args: None,
                                    now=100, package_id=second['id'])['attempted'] == 1
    assert transferred == [second['frozen_bytes']]
    assert system.db.execute('SELECT state FROM outbox WHERE id=?', (first['id'],)).fetchone()[0] == 'prepared'
    for package in (second['id'], 'missing'):
        assert collector.upload_pending(system.db, system.home, system.config, lambda *args: None,
                                        now=9999, package_id=package)['attempted'] == 0
    assert len(transferred) == 1


@pytest.mark.parametrize('wait_seconds', [0, 5, 30, 60])
def test_upload_verification_wait_is_forwarded(system, monkeypatch, wait_seconds):
    prepared(system)
    waits = []
    def transfer(handle, payload, stamp, call, **kwargs):
        waits.append(kwargs['wait_seconds'])
        return {'receipt_id': 'f' * 32, 'status': 'verified', 'content_verified': True, 'published': False}
    monkeypatch.setattr(uploader, 'upload', transfer)
    result = collector.upload_pending(system.db, system.home, system.config, lambda *args: None,
                                      now=100, wait_seconds=wait_seconds)
    assert result['attempted'] == 1 and waits == [wait_seconds]


@pytest.mark.parametrize('wait_seconds', [-1, 61, True, 0.5])
def test_upload_verification_wait_rejects_invalid_values(system, wait_seconds):
    with pytest.raises(ValueError, match='collector_upload_wait_range'):
        collector.upload_pending(system.db, system.home, system.config,
                                 lambda *args: pytest.fail('invalid wait must not reach MCP'), wait_seconds=wait_seconds)


def test_prepared_received_and_publication_remain_separate_with_retry(system, tmp_path):
    row = prepared(system)
    frozen = (system.home / 'outbox' / row['filename']).read_bytes()
    assert collector.prepare(system.db, system.home, system.config, lambda _: pytest.fail('packaged events cannot be reviewed again'))['prepared'] == 0
    assert system.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1
    service_store = ReceiptStore(tmp_path / 'receipts.db')
    service_store.initialize()
    bucket = FakeBucket()
    service = ReceiptService(service_store, ReceiptObjects(bucket))
    attempts, dropped, ids = [], [False], []

    def call(name, args):
        if name == uploader.PREPARE:
            result = service.prepare('employee:synthetic', args)
            ids.append(result['receipt']['receipt_id'])
            return result
        if name == uploader.COMPLETE:
            if not dropped[0]:
                dropped[0] = True
                raise OSError('lost_after_put')
            service.complete('employee:synthetic', args['receipt_id'])
            assert service.verify_once()
            return service.status('employee:synthetic', args['receipt_id'])
        return service.status('employee:synthetic', args['receipt_id'])

    def put(handle, grant, size):
        remote = service_store.get('employee:synthetic', grant['headers']['x-oss-meta-receipt-id'])
        data = handle.read()
        assert data == frozen and size == len(frozen)
        attempts.append(remote['receipt_id'])
        if remote['object_key'] not in bucket.data:
            bucket.upload(remote, data)

    assert collector.status(system.db)['packages'] == {'prepared': 1}
    assert collector.upload_pending(system.db, system.home, system.config, call, now=100, put=put)['attempted'] == 1
    assert collector.status(system.db)['packages'] == {'retry': 1}
    assert collector.upload_pending(system.db, system.home, system.config, call, now=101, put=put)['attempted'] == 0
    result = collector.upload_pending(system.db, system.home, system.config, call, now=401, put=put)
    assert result['attempted'] == 1 and result['formal_publication_verified'] is False
    assert len(ids) == 2 and len(set(ids)) == 1 and attempts == ids and len(bucket.data) == 1
    stored = system.db.execute('SELECT * FROM outbox').fetchone()
    assert stored['state'] == 'received' and stored['attempts'] == 2
    assert json.loads(stored['receipt'])['published'] is False
    assert collector.status(system.db)['packages'] == {'received': 1}
    assert collector.status(system.db)['formal_publication_verified'] is False
    assert collector.upload_pending(system.db, system.home, system.config, call, now=9999, put=put)['attempted'] == 0
    assert (system.home / 'outbox' / row['filename']).read_bytes() == frozen


def test_tampered_frozen_zip_never_requests_upload(system):
    row = prepared(system)
    (system.home / 'outbox' / row['filename']).write_bytes(b'changed bytes')
    result = collector.upload_pending(system.db, system.home, system.config,
                                      lambda *args: pytest.fail('tampered package must not reach MCP'), now=100)
    assert result['formal_publication_verified'] is False
    row = system.db.execute('SELECT state,last_error,receipt FROM outbox').fetchone()
    assert row['state'] == 'retry' and row['last_error'] == 'collector_upload_failed' and row['receipt'] is None


def test_materialization_crash_recovers_committed_bytes_without_reviewing_again(system, monkeypatch):
    queue(system, [event()])
    calls = []
    def review(events):
        calls.append(True)
        return [decision([e['id'] for e in events])]
    def interrupted(*args):
        raise OSError('synthetic power interruption after durable queue commit')
    with monkeypatch.context() as patch:
        patch.setattr(collector, '_materialize', interrupted)
        with pytest.raises(OSError):
            collector.prepare(system.db, system.home, system.config, review)
    row = system.db.execute('SELECT * FROM outbox').fetchone()
    frozen = row['frozen_bytes']
    assert row['state'] == 'prepared' and collector.digest(frozen) == row['sha256']
    assert not (system.home / 'outbox' / row['filename']).exists()
    assert collector.status(system.db)['events'] == {'packaged': 1}

    transferred = []
    def transfer(handle, payload, stamp, call, **kwargs):
        transferred.append(handle.read())
        assert transferred[-1] == frozen and payload['sha256'] == collector.digest(frozen)
        return {'receipt_id': 'f' * 32, 'status': 'verified', 'content_verified': True, 'published': False}
    monkeypatch.setattr(uploader, 'upload', transfer)
    # A fresh DB connection models runner restart; the original model decision is not needed.
    with collector.database(system.home) as restarted:
        assert collector.prepare(restarted, system.home, system.config, lambda _: pytest.fail('frozen work cannot be regenerated'))['prepared'] == 0
        result = collector.upload_pending(restarted, system.home, system.config, lambda *args: None, now=100)
        assert result['attempted'] == 1 and result['formal_publication_verified'] is False
        assert collector.status(restarted)['packages'] == {'received': 1}
    assert len(calls) == 1 and transferred == [frozen]
    assert (system.home / 'outbox' / row['filename']).read_bytes() == frozen


def test_oversized_session_is_held_without_truncating_or_calling_model(system):
    queue(system, [event(text='x' * (collector.MAX_GROUP_CHARS + 1))])
    assert collector.prepare(system.db, system.home, system.config, lambda _: pytest.fail('must not truncate input'))['prepared'] == 0
    row = system.db.execute('SELECT value,state FROM events').fetchone()
    assert len(json.loads(row['value'])['text']) == collector.MAX_GROUP_CHARS + 1
    assert row['state'] == 'needs_review'


def test_restricted_or_invented_noncontiguous_selection_never_creates_package(system):
    queue(system, [event('1'), event('2'), event('3')])
    with pytest.raises(ValueError, match='collector_reviewer_noncontiguous_span'):
        collector.prepare(system.db, system.home, system.config, lambda _: [decision(['1', '3'])])
    assert system.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    held = {**decision(['1', '2', '3']), 'sharing': 'restricted'}
    assert collector.prepare(system.db, system.home, system.config, lambda _: [held])['prepared'] == 0
    assert collector.status(system.db)['events'] == {'needs_review': 3}


def test_filesystem_root_cannot_be_a_work_scope(system):
    with system.db:
        system.db.execute("DELETE FROM settings WHERE key='config'")
    with pytest.raises(ValueError, match='collector_work_roots_required'):
        collector.initialize(system.db, {**system.config, 'allowed_roots': [system.home.anchor]}, now=100)


def test_upload_cli_never_echoes_hermes_dependency_output(system, monkeypatch, capsys):
    from contextlib import contextmanager
    import sys
    config = {**system.config, 'hermes_root': str(system.home / 'not-a-real-runtime'), 'mcp_server': 'fixture'}
    with system.db:
        system.db.execute("UPDATE settings SET value=? WHERE key='config'", (collector.dumps(config),))
    @contextmanager
    def native_client(*args):
        print('synthetic-token-that-must-not-escape')
        print('synthetic-token-that-must-not-escape', file=sys.stderr)
        yield lambda *args: None
    monkeypatch.setattr(uploader, 'hermes_client', native_client)
    selected = []
    def upload_only(*args, **kwargs):
        selected.append(kwargs)
        return {'attempted': 0, 'formal_publication_verified': False}
    monkeypatch.setattr(collector, 'upload_pending', upload_only)
    assert collector.main(['--home', str(system.home), 'upload', '--execute', '--package-id', 'selected',
                           '--wait-seconds', '30']) == 0
    output = capsys.readouterr()
    assert 'synthetic-token' not in output.out + output.err
    assert json.loads(output.out)['formal_publication_verified'] is False
    assert selected == [{'package_id': 'selected', 'wait_seconds': 30}]


def test_prepare_cli_forwards_explicit_session_and_sharing_flags(system, monkeypatch, capsys):
    selected = []
    def prepare_only(*args, **kwargs):
        selected.append(kwargs)
        return {'prepared': 0, 'uploaded': False}
    monkeypatch.setattr(collector, 'prepare', prepare_only)
    assert collector.main(['--home', str(system.home), 'prepare', '--session-id', 'selected',
                           '--allow-company-sharing']) == 0
    assert selected == [{'session_id': 'selected', 'allow_company_sharing': True}]
    assert json.loads(capsys.readouterr().out)['uploaded'] is False
