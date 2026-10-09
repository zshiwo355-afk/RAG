"""Small, explicit employee collector pilot. No scheduler or upload is enabled by init.

Source checkpoints and sanitized events commit together. Prepared ZIPs are immutable;
retrying an upload always sends the same bytes and receipt idempotency key.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rag_app.knowledge_text import clean_text
from knowledge_collector_hermes import snapshot
from knowledge_collector_sources import baseline_jsonl, scan_jsonl

FIELDS = ['资产编号', '名称', '类型', '状态', '用途', '正文路径', '来源定位', '证据状态',
          '限制与待办', '合并目标', '基线修订', '共享范围', '使用对象', '输入', '输出', '依赖与权限', '适用边界']
MAX_GROUP_CHARS = 24000
EXTRA_SECRET = re.compile(r'\b(?:github_pat_[A-Za-z0-9_]{20,}|LTAI[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{16})\b')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def event_key(item):
    return digest(dumps([item['agent'], item['source_session_id'], item['id']]))


@contextmanager
def locked(home):
    home = Path(home).expanduser()
    if home.is_symlink():
        raise ValueError('collector_home_symlink')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        home.chmod(0o700)
    path = home / 'collector.lock'
    if path.is_symlink():
        raise ValueError('collector_lock_symlink')
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('collector_already_running') from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('collector_already_running') from None
        try:
            yield home
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def database(home):
    path = Path(home) / 'collector.sqlite3'
    if path.is_symlink():
        raise ValueError('collector_database_symlink')
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    if os.name != 'nt':
        path.chmod(0o600)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS checkpoints (source TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            key TEXT PRIMARY KEY, session TEXT NOT NULL, value TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS issues (
            key TEXT PRIMARY KEY, code TEXT NOT NULL, count INTEGER NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            receipt TEXT, last_error TEXT, next_retry REAL NOT NULL DEFAULT 0,
            frozen_bytes BLOB);
        ''')
        if 'frozen_bytes' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}:
            db.execute('ALTER TABLE outbox ADD COLUMN frozen_bytes BLOB')
            db.commit()
        yield db
    finally:
        db.close()


def issue(db, code, context='', now=None):
    now = time.time() if now is None else now
    # Do not keep source exception messages, titles or raw paths in diagnostics.
    key = digest(code + context)
    db.execute('INSERT INTO issues VALUES(?,?,1,?,?) ON CONFLICT(key) DO UPDATE SET '
               'count=count+1,last_seen=excluded.last_seen', (key, code, now, now))


def get_config(db):
    row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    if row is None:
        raise ValueError('collector_not_initialized')
    return json.loads(row[0])


def _files(source):
    root = Path(source['path']).expanduser()
    pattern = 'rollout-*.jsonl' if source['agent'] == 'codex' else '*.jsonl'
    if not root.is_dir() or root.is_symlink():
        raise ValueError('collector_source_directory_invalid')
    result = []
    for path in root.rglob(pattern):
        if path.is_file() and not path.is_symlink() and not any(p.is_symlink() for p in path.parents if p != root.parent):
            result.append(path)
            if len(result) > 500:
                raise ValueError('collector_source_file_limit')
    return sorted(result)


def _safe_event(event):
    # Only this allowlist crosses into local durable storage and model input.
    text, counts = clean_text(event['text'])
    text, extra = EXTRA_SECRET.subn('[已隐藏:额外凭据]', text)
    state = 'needs_review' if counts or extra or '[已隐藏:' in text else 'queued'
    item = {k: event[k] for k in ('id', 'source_session_id', 'agent', 'role', 'timestamp')}
    if item['role'] not in {'user', 'assistant'} or item['agent'] not in {'hermes', 'codex', 'workbuddy'}:
        raise ValueError('collector_event_contract')
    item['text'] = text
    # Personal directory names never enter the model or cloud package.
    item['locator'] = item['agent'] + ':session:' + digest(item['source_session_id'])[:20] + ':message:' + digest(item['id'])[:20]
    return item, state


def _commit_snapshot(db, source_key, checkpoint, events, issues, now):
    for event in events:
        item, state = _safe_event(event)
        key = event_key(item)
        session = digest(item['agent'] + ':' + item['source_session_id'])
        db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',
                   (key, session, dumps(item), state, now))
        if state == 'needs_review':
            issue(db, 'local_redaction_review', key, now)
    for item in issues:
        issue(db, item['code'], source_key + item.get('session_sha256', ''), now)
    if checkpoint is not None:
        db.execute('INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value',
                   (source_key, dumps(checkpoint)))


def collect(db, config, *, initial=False, now=None):
    """One atomic transaction: interruption cannot advance cursors past queued data."""
    now = time.time() if now is None else now
    before = db.execute('SELECT count(*) FROM events').fetchone()[0]
    with db:
        for source in config['sources']:
            agent = source['agent']
            scope = digest(dumps([sorted(config['allowed_roots']), sorted(config.get('excluded_sessions', [])), config['enabled_at']]))
            identities = {}
            if agent != 'hermes':
                for stored in db.execute('SELECT source,value FROM checkpoints WHERE source LIKE ?', (agent + ':%',)):
                    cp = json.loads(stored['value'])
                    if cp.get('dev') is not None and cp.get('ino') is not None:
                        identities[(cp['dev'], cp['ino'])] = (stored['source'], cp)
            paths = [Path(source['path']).expanduser()] if agent == 'hermes' else _files(source)
            for path in paths:
                source_key = agent + ':' + str(path.resolve())
                row = db.execute('SELECT value FROM checkpoints WHERE source=?', (source_key,)).fetchone()
                previous = json.loads(row[0]) if row else None
                if agent == 'hermes':
                    result = snapshot(path, previous, allowed_roots=config['allowed_roots'],
                                      enabled_at=config['enabled_at'], max_messages=500, max_chars=120000,
                                      excluded_sessions=config.get('excluded_sessions', []))
                    checkpoint = result['checkpoint']
                else:
                    baseline_issues = []
                    if previous is None:
                        stamp = path.stat()
                        moved = identities.get((stamp.st_dev, stamp.st_ino))
                        if moved:
                            old_key, previous = moved
                            db.execute('DELETE FROM checkpoints WHERE source=?', (old_key,))
                    changed_scope = previous is not None and previous.get('_scope') != scope
                    if changed_scope:
                        previous = None
                        baseline_issues.append({'code': 'collector_scope_changed_rebaseline'})
                    if previous is None:
                        base = baseline_jsonl(path, agent, config['enabled_at'], new=not initial and not changed_scope)
                        previous = base['checkpoint']
                        baseline_issues.extend(base['issues'])
                    if previous is None or initial or changed_scope:
                        result = {'events': [], 'issues': baseline_issues}
                        checkpoint = previous
                    else:
                        result = scan_jsonl(path, previous, config['allowed_roots'], config['enabled_at'],
                                            excluded_sessions=config.get('excluded_sessions', []))
                        result['issues'].extend(baseline_issues)
                        checkpoint = result['next_checkpoint']
                    if checkpoint is not None:
                        checkpoint['_scope'] = scope
                _commit_snapshot(db, source_key, checkpoint, result['events'], result['issues'], now)
        db.execute("INSERT INTO settings VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    return {'new_events': db.execute('SELECT count(*) FROM events').fetchone()[0] - before}


def initialize(db, config, *, now=None):
    existing = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    if existing:
        return {'initialized': False, 'existing_checkpoint_preserved': True}
    config = dict(config)
    config.update(version=1, installation_id=uuid.uuid4().hex, enabled_at=time.time() if now is None else now,
                  mode='pilot_manual', interval_days=15)
    roots = [str(Path(root).expanduser().resolve(strict=True)) for root in config['allowed_roots']]
    if not roots or any(not Path(root).is_dir() or str(Path(root)) == Path(root).anchor for root in roots):
        raise ValueError('collector_work_roots_required')
    config['allowed_roots'] = roots
    if not config['sources'] or any(s['agent'] not in {'hermes', 'codex', 'workbuddy'} for s in config['sources']):
        raise ValueError('collector_source_configuration')
    # collect commits this config and all baselines in the same transaction.
    db.execute("INSERT INTO settings VALUES('config',?)", (dumps(config),))
    collect(db, config, initial=True, now=config['enabled_at'])
    return {'initialized': True, 'enabled_at': config['enabled_at'], 'new_events': 0, 'scheduled': False}


def _zip_bytes(events, decision, *, pilot=True):
    selected = [e for e in events if e['id'] in decision['event_ids']]
    identity = digest(dumps([(e['agent'], e['source_session_id'], e['id']) for e in selected]))[:24]
    asset_id = 'KA-' + identity
    locator = '; '.join(e['locator'] for e in selected)
    body = '# ' + decision['title'] + '\n\n'
    body += ('来源：Agent 新增对话原文；提交人不代表作者。\n'
             '下文保留 user / assistant 角色；执行、采纳和效果未独立核实。\n\n')
    for event in selected:
        body += f"## [{event['role']}] {event['locator']}\n\n{event['text']}\n\n"
    row = dict(zip(FIELDS, [''] * len(FIELDS)))
    row.update({'资产编号': asset_id, '名称': decision['title'], '类型': decision['asset_type'], '状态': '交付',
                '用途': decision['purpose'], '正文路径': f'知识正文/{asset_id}.md', '来源定位': locator,
                '证据状态': '原文角色及定位保留；执行、采纳和效果未独立核实',
                '限制与待办': '本轮小范围采集测试，须人工确认后使用。' if pilot else decision['reason'],
                # Pilot is always held by the server's existing governance gate.
                '共享范围': '测试待确认' if pilot else ('全员' if decision['sharing'] == 'company' else '未确认'),
                '使用对象': decision['audience'], '输入': decision['inputs'], '输出': decision['outputs'],
                '依赖与权限': decision['dependencies'], '适用边界': decision['boundaries']})
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerow(row)
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in [('交付清单.csv', stream.getvalue()), (row['正文路径'], body)]:
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, text.encode('utf-8'))
    return identity, result.getvalue()


def _materialize(home, row):
    """The committed queue owns the bytes; a missing file is recoverable cache."""
    outdir = Path(home) / 'outbox'
    if outdir.is_symlink():
        raise ValueError('collector_outbox_symlink')
    outdir.mkdir(mode=0o700, exist_ok=True)
    target = outdir / row['filename']
    if target.exists() or target.is_symlink():
        if target.is_symlink() or digest(target.read_bytes()) != row['sha256']:
            raise ValueError('collector_frozen_package_changed')
        return target
    data = row['frozen_bytes']
    if data is None or digest(data) != row['sha256']:
        raise ValueError('collector_frozen_package_missing')
    temporary = outdir / (uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            if os.name != 'nt':
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def prepare(db, home, config, reviewer, *, max_groups=1, session_id=None, allow_company_sharing=False):
    """Only bounded, sanitized new source text reaches the tool-free reviewer."""
    query = "SELECT DISTINCT session FROM events WHERE state='queued'"
    parameters = []
    if session_id is not None:
        query += ' AND session IN (?,?,?)'
        parameters.extend(digest(agent + ':' + session_id) for agent in ('hermes', 'codex', 'workbuddy'))
    sessions = db.execute(query + ' ORDER BY created LIMIT ?', (*parameters, max_groups)).fetchall()
    prepared = 0
    for session in sessions:
        rows = db.execute("SELECT key,value FROM events WHERE session=? AND state IN ('queued','parked') ORDER BY created,rowid", (session[0],)).fetchall()
        events = [json.loads(row['value']) for row in rows]
        if sum(len(e['text']) for e in events) > MAX_GROUP_CHARS:
            with db:
                issue(db, 'local_context_limit_review', session[0])
                db.execute("UPDATE events SET state='needs_review' WHERE session=? AND state IN ('queued','parked')", (session[0],))
            continue
        from knowledge_collector_review import validate_decisions
        reviewed = reviewer(events)
        raw = []
        for item in reviewed:
            ids = item.get('event_ids')
            if not isinstance(ids, list) or not ids:
                raise ValueError('collector_reviewer_contract')
            raw.append({**{k: v for k, v in item.items() if k != 'event_ids'},
                        'start_id': ids[0], 'end_id': ids[-1]})
        decisions = validate_decisions(events, {'assets': raw})
        if any(a['event_ids'] != b['event_ids'] for a, b in zip(decisions, reviewed)):
            raise ValueError('collector_reviewer_noncontiguous_span')
        if any(d['sharing'] == 'restricted' for d in decisions):
            with db:
                issue(db, 'local_sharing_review', session[0])
                db.execute("UPDATE events SET state='needs_review' WHERE session=? AND state IN ('queued','parked')", (session[0],))
            continue
        pending = []
        for decision in decisions:
            identity, data = _zip_bytes(events, decision, pilot=not allow_company_sharing)
            filename = 'intake-' + identity + '.zip'
            sha = digest(data)
            pending.append((identity, filename, sha, decision['event_ids'], data))
        with db:
            for identity, filename, sha, ids, data in pending:
                db.execute("INSERT INTO outbox(id,filename,sha256,state,frozen_bytes) VALUES(?,?,?,'prepared',?)", (identity, filename, sha, data))
                for event_id in ids:
                    key = next(row['key'] for row, event in zip(rows, events) if event['id'] == event_id)
                    db.execute("UPDATE events SET state='packaged' WHERE key=?", (key,))
            db.execute("UPDATE events SET state='parked' WHERE session=? AND state='queued'", (session[0],))
            if not pending:
                issue(db, 'no_complete_asset_yet', session[0])
        for identity, *_ in pending:
            _materialize(home, db.execute('SELECT * FROM outbox WHERE id=?', (identity,)).fetchone())
        prepared += len(pending)
    return {'prepared': prepared, 'uploaded': False}


def upload_pending(db, home, config, call, *, now=None, max_packages=1, put=None, package_id=None, wait_seconds=5):
    from upload_knowledge_receipt import describe_file, upload, UploadError
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise ValueError('collector_upload_wait_range')
    now = time.time() if now is None else now
    query = "SELECT * FROM outbox WHERE state IN ('prepared','retry') AND next_retry<=?"
    parameters = [now]
    if package_id is not None:
        query += ' AND id=?'
        parameters.append(package_id)
    rows = db.execute(query + ' ORDER BY rowid LIMIT ?', (*parameters, max_packages)).fetchall()
    for row in rows:
        error = None
        receipt = None
        try:
            path = _materialize(home, row)
            if path.is_symlink():
                raise ValueError('collector_frozen_package_changed')
            with path.open('rb') as handle:
                payload, stamp = describe_file(handle, row['filename'], 'collector-' + config['installation_id'])
                if payload['sha256'] != row['sha256']:
                    raise ValueError('collector_frozen_package_changed')
                receipt = upload(handle, payload, stamp, call, wait_seconds=wait_seconds, **({'put': put} if put else {}))
            state = 'received' if receipt['content_verified'] else ('rejected' if receipt['status'] == 'rejected' else 'retry')
        except (OSError, ValueError, UploadError):
            error, state = 'collector_upload_failed', 'retry'
        with db:
            db.execute('UPDATE outbox SET state=?,attempts=attempts+1,receipt=?,last_error=?,next_retry=? WHERE id=?',
                       (state, dumps(receipt) if receipt else row['receipt'], error,
                        now + min(86400, 300 * 2 ** min(row['attempts'], 8)), row['id']))
    return {'attempted': len(rows), 'formal_publication_verified': False}


def status(db):
    config = get_config(db)
    return {'mode': config['mode'], 'enabled_at': config['enabled_at'], 'interval_days': config['interval_days'],
            'scheduled': False,
            'events': dict(db.execute('SELECT state,count(*) FROM events GROUP BY state').fetchall()),
            'packages': dict(db.execute('SELECT state,count(*) FROM outbox GROUP BY state').fetchall()),
            'issues': [{'code': r[0], 'occurrences': r[1]} for r in db.execute('SELECT code,sum(count) FROM issues GROUP BY code')],
            'formal_publication_verified': False}


def model_review(home, config, events):
    # A child process contains Hermes imports, credential handling and model logs.
    folder = Path(home) / 'review-work'
    folder.mkdir(mode=0o700, exist_ok=True)
    request, response = folder / (uuid.uuid4().hex + '.json'), folder / (uuid.uuid4().hex + '.json')
    try:
        with request.open('x', encoding='utf-8') as handle:
            if os.name != 'nt':
                os.fchmod(handle.fileno(), 0o600)
            handle.write(dumps({'events': events, 'hermes_root': config['hermes_root']}))
        interpreter = Path(config['hermes_root']) / ('venv/Scripts/python.exe' if os.name == 'nt' else 'venv/bin/python')
        result = subprocess.run([str(interpreter), str(Path(__file__).resolve()), '--home', str(home),
                                 'review-worker', '--request', str(request), '--response', str(response)],
                                timeout=150, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0 or not response.exists() or response.stat().st_size > 64000:
            raise ValueError('collector_reviewer_failed')
        return json.loads(response.read_text(encoding='utf-8'))
    finally:
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--root', action='append', required=True)
    init.add_argument('--hermes-root', type=Path, default=Path.home() / '.hermes/hermes-agent')
    init.add_argument('--hermes-db', type=Path, default=Path.home() / '.hermes/state.db')
    for name in ('collect', 'status'):
        commands.add_parser(name)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('--session-id', help='Only review queued events from this source session')
    prepare_parser.add_argument('--allow-company-sharing', action='store_true',
                                help='Allow company-scoped model decisions to enter normal server publication checks')
    upload_parser = commands.add_parser('upload')
    upload_parser.add_argument('--execute', action='store_true')
    upload_parser.add_argument('--package-id', help='Only upload this frozen package, when due')
    upload_parser.add_argument('--wait-seconds', type=int, choices=range(61), default=5,
                               metavar='0..60', help='Wait for receipt verification (default: 5 seconds)')
    worker = commands.add_parser('review-worker')
    worker.add_argument('--request', type=Path, required=True)
    worker.add_argument('--response', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'review-worker':
            logging.disable(logging.CRITICAL)
            from knowledge_collector_review import review
            value = json.loads(args.request.read_text(encoding='utf-8'))
            result = review(value['events'], hermes_root=value['hermes_root'])
            with args.response.open('x', encoding='utf-8') as handle:
                if os.name != 'nt':
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(dumps(result))
            return 0
        with locked(args.home) as home, database(home) as db:
            if args.command == 'init':
                output = initialize(db, {'allowed_roots': args.root, 'sources': [{'agent': 'hermes', 'path': str(args.hermes_db)}],
                                         'hermes_root': str(args.hermes_root), 'mcp_server': 'company-mcp'})
            else:
                config = get_config(db)
                if args.command == 'collect':
                    output = collect(db, config)
                elif args.command == 'prepare':
                    output = prepare(db, home, config, lambda events: model_review(home, config, events),
                                     session_id=args.session_id, allow_company_sharing=args.allow_company_sharing)
                elif args.command == 'upload' and args.execute:
                    from upload_knowledge_receipt import hermes_client
                    logging.disable(logging.CRITICAL)
                    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                        with hermes_client(Path(config['hermes_root']), config['mcp_server']) as call:
                            output = upload_pending(db, home, config, call, package_id=args.package_id,
                                                    wait_seconds=args.wait_seconds)
                else:
                    output = status(db)
            print(dumps({'ok': True, **output}))
        return 0
    except Exception:
        # Provider/transport error bodies may include credentials or private text.
        print(dumps({'ok': False, 'error': 'collector_operation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
