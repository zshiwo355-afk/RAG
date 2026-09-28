#!/usr/bin/env python3
"""Isolated real-data trial: prepare -> load -> serve. Never uses the formal DB.

Intake and value judgment remain scripts/knowledge.py subcommands. All cloud
writes are recorded before execution for cleanup_knowledge_trial.py.
"""
from __future__ import annotations

import argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
from scripts.embed_documents import embed_batch
from scripts.cleanup_knowledge_trial import read_manifest
from rag_app.config import load_env
from rag_app.knowledge_index import KnowledgeIndex, _check_body, _vector, chunk_document
from rag_app.knowledge_intake import import_batch, write_json
from rag_app.knowledge_objects import KnowledgeObjects
from rag_app.knowledge_service import KnowledgeService
from rag_app.knowledge_store import KnowledgeStore, normalize_entry
from rag_app.retrieval_service import extract_result_items


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def private_write(path, value):
    # No world-readable interval, including on first creation.
    fd = os.open(str(path.with_suffix('.tmp')), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    path.with_suffix('.tmp').replace(path)
    path.chmod(0o600)


def prepare(root, extend=False):
    existing = validate_plan(root) if (root / 'trial.json').exists() else None
    if existing is not None and not extend:
        return {'status': 'already_prepared', 'assets': len(existing['assets'])}
    batch = read(root / 'batch' / 'batch.json')
    captured = []

    class Capture:
        def import_document(self, entry, execute=False):
            captured.append(normalize_entry(entry))
            return {}

    result = import_batch(root / 'batch', Capture())
    selected = [row for row in result['results'] if row['status'] == 'dry_run']
    if len(selected) != len(captured):
        raise ValueError('selection validation failed')
    by_id = {asset['candidate_id']: asset for asset in batch['assets']}
    run_id = existing['trial_id'] if existing is not None else uuid4().hex
    prefix = f'company-knowledge/trials/{run_id}/'
    manifest = existing if existing is not None else {'trial_id': run_id, 'prefix': prefix, 'assets': [],
                'created_object_versions': {}, 'formal_published': False, 'status': 'prepared'}
    previous = {a['candidate_id']: a for a in manifest['assets']}
    if not set(previous) <= {row['candidate_id'] for row in selected}:
        raise ValueError('previously accepted selection changed; start a new trial')
    manifest['selection'] = result
    new_entries = []
    for row, original in zip(selected, captured):
        candidate = by_id[row['candidate_id']]
        knowledge_id = 'trial_' + run_id[:8] + '_' + digest(row['candidate_id'])[:24]
        entry = dict(original, knowledge_id=knowledge_id)
        entry['evidence'] = {**entry['evidence'], 'trial': {'trial_id': run_id,
            'source_knowledge_id': original['knowledge_id'], 'formal_published': False}}
        entry = normalize_entry(entry)
        if row['candidate_id'] in previous:
            if previous[row['candidate_id']]['payload_hash'] != entry['payload_hash']:
                raise ValueError('previously prepared entry changed')
            continue
        chunks = chunk_document(dict(entry, revision=1))
        relative = 'entries/' + knowledge_id + '.json'
        new_entries.append((root / relative, entry))
        manifest['assets'].append({'candidate_id': row['candidate_id'], 'knowledge_id': knowledge_id,
            'revision': 1, 'title': entry['title'], 'kind': entry['kind'], 'chars': len(entry['content']),
            'content_hash': entry['content_hash'], 'payload_hash': entry['payload_hash'],
            'entry_file': relative, 'content_file': 'batch/' + candidate['body_file'],
            'chunk_ids': [chunk['id'] for chunk in chunks], 'revision_key': chunks[0]['revision_key'],
            'object_key': prefix + entry['content_hash'] + '.txt', 'state': 'prepared',
            'preflight_absent': False})
    (root / 'entries').mkdir(exist_ok=True)
    for path, entry in new_entries:
        write_json(path, entry)
    if len(manifest['assets']) > len(previous):
        manifest['status'] = 'prepared'
    write_json(root / 'trial.json', manifest)
    return {'status': 'prepared', 'assets': len(selected),
            'chunks': sum(len(asset['chunk_ids']) for asset in manifest['assets']),
            'skipped': len(result['skipped']), 'selection_failed': len(result['results']) - len(selected)}


def docker(*args, env=None):
    result = subprocess.run(['docker', *args], env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('trial Docker operation failed')
    return result.stdout.strip()


def ensure_database(root, manifest):
    runtime_file = root / 'runtime.json'
    if runtime_file.exists():
        runtime = read(runtime_file)
        expected_name = 'rag-trial-' + manifest['trial_id'][:12]
        if runtime['container_name'] != expected_name or runtime['volume_name'] != expected_name:
            raise ValueError('runtime resource mismatch')
        info = json.loads(docker('inspect', runtime['container_name']))[0]
        url = urlsplit(runtime['database_url'])
        ports = info['HostConfig']['PortBindings'].get('5432/tcp', [])
        if (url.scheme != 'postgresql' or url.hostname != '127.0.0.1' or url.username != 'trial'
                or url.path != '/knowledge_trial' or url.query or url.fragment
                or not any(p['HostIp'] == '127.0.0.1' and p['HostPort'] == str(url.port) for p in ports)):
            raise ValueError('runtime database does not match owned container')
        if info['Config'].get('Labels', {}).get('com.rag.knowledge-trial') != manifest['trial_id']:
            raise ValueError('container ownership mismatch')
        if not info['State']['Running']:
            docker('start', runtime['container_name'])
        return runtime
    name = 'rag-trial-' + manifest['trial_id'][:12]
    password = secrets.token_urlsafe(32)
    # Choose a loopback port before creation; Docker failure never selects a formal DB.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    runtime = {'database_url': f'postgresql://trial:{password}@127.0.0.1:{port}/knowledge_trial',
               'container_name': name, 'volume_name': name, 'server_pid': None, 'server_port': None}
    private_write(runtime_file, runtime)
    label = 'com.rag.knowledge-trial=' + manifest['trial_id']
    docker('volume', 'create', '--label', label, name)
    env = {**os.environ, 'POSTGRES_PASSWORD': password}
    docker('run', '-d', '--name', name, '--label', label, '--memory', '512m', '--cpus', '1',
           '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_USER=trial', '-e', 'POSTGRES_DB=knowledge_trial',
           '-p', f'127.0.0.1:{port}:5432', '-v', name + ':/var/lib/postgresql/data', 'postgres:16', env=env)
    for _ in range(30):
        check = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'trial', '-d', 'knowledge_trial'], capture_output=True)
        if check.returncode == 0:
            return runtime
        time.sleep(1)
    raise RuntimeError('trial PostgreSQL startup failed')


class CachedIndex(KnowledgeIndex):
    def __init__(self, cache):
        super().__init__()
        self.cache = cache
        cache.mkdir(exist_ok=True)

    def warm(self, entry):
        texts = list(dict.fromkeys('\n'.join([chunk['title'], *chunk.get('section_path', []), chunk['source_text']])
            for chunk in chunk_document(dict(entry, revision=1))))
        missing = [text for text in texts if not self.cache_path(text).exists()]
        for start in range(0, len(missing), 10):
            batch = missing[start:start + 10]
            vectors = embed_batch([{'page_content': text} for text in batch], timeout=120)
            for text, vector in zip(batch, vectors):
                self.save_vector(text, _vector(vector))

    def cache_path(self, text):
        return self.cache / (digest('text-embedding-v4:1024:' + text) + '.json')

    def save_vector(self, text, vector):
        path = self.cache_path(text)
        temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(vector), encoding='utf-8')
        temporary.replace(path)

    def _embed(self, text):
        # Current company index has a fixed v4/1024 embedding contract.
        path = self.cache_path(text)
        if path.exists():
            return _vector(read(path))
        vector = super()._embed(text)
        self.save_vector(text, vector)
        return vector


def service_for(root, manifest, runtime, objects=None):
    return KnowledgeService(KnowledgeStore(database_url=runtime['database_url'],
        objects=objects or KnowledgeObjects(prefix=manifest['prefix'])), CachedIndex(root / 'embedding_cache'))



def validate_plan(root):
    manifest, _ = read_manifest(root)
    if manifest['status'] in {'cleaned', 'cleanup_failed', 'cleanup_in_progress'}:
        raise ValueError('trial cleanup started')
    for asset in manifest['assets']:
        expected_id = 'trial_' + manifest['trial_id'][:8] + '_' + digest(asset['candidate_id'])[:24]
        file = (root / asset['entry_file']).resolve()
        if root not in file.parents or asset['knowledge_id'] != expected_id:
            raise ValueError('unsafe prepared entry')
        entry = normalize_entry(read(file))
        body_file = (root / asset['content_file']).resolve()
        if root not in body_file.parents or digest(body_file.read_text(encoding='utf-8')) != asset['content_hash']:
            raise ValueError('prepared standard body changed')
        if any(entry[key] != asset[key] for key in ('knowledge_id', 'content_hash', 'payload_hash')):
            raise ValueError('prepared entry changed')
        expected = chunk_document(dict(entry, revision=1))
        if ([chunk['id'] for chunk in expected] != asset['chunk_ids']
                or expected[0]['revision_key'] != asset['revision_key']):
            raise ValueError('prepared chunk plan changed')
    return manifest


def load(root, workers):
    manifest = validate_plan(root)
    if not manifest['assets']:
        return {'status': 'no_eligible_assets'}
    runtime = ensure_database(root, manifest)
    service_for(root, manifest, runtime).store.initialize()
    lock = threading.Lock()

    def save():
        write_json(root / 'trial.json', manifest)

    def one(asset):
        if asset['state'] == 'ready':
            return
        stage = 'entry_validation'
        try:
            file = (root / asset['entry_file']).resolve()
            if root not in file.parents:
                raise ValueError('unsafe entry path')
            entry = normalize_entry(read(file))
            if any(entry[key] != asset[key] for key in ('knowledge_id', 'content_hash', 'payload_hash')):
                raise ValueError('prepared entry changed')
            stage = 'preflight'
            index = CachedIndex(root / 'embedding_cache')
            index._connect()
            objects = KnowledgeObjects(prefix=manifest['prefix'])
            if not asset['preflight_absent']:
                for start in range(0, len(asset['chunk_ids']), 10):
                    response = index.client.fetch(index.models.FetchRequest(table_name=index.table,
                        ids=asset['chunk_ids'][start:start + 10], include_vector=False, output_fields=['id']))
                    _check_body(response)
                    if extract_result_items(response):
                        raise ValueError('trial IDs already exist')
                with lock:
                    if (asset['object_key'] not in manifest['created_object_versions']
                            and objects.bucket.object_exists(asset['object_key'])):
                        raise ValueError('trial body already exists without ownership record')
                    asset['preflight_absent'] = True
                    save()
            original_put = objects.bucket.put_object

            def tracked_put(key, *args, **kwargs):
                result = original_put(key, *args, **kwargs)
                with lock:
                    manifest['created_object_versions'][key] = result.headers.get('x-oss-version-id')
                    save()
                return result

            objects.bucket.put_object = tracked_put
            service = KnowledgeService(KnowledgeStore(database_url=runtime['database_url'], objects=objects), index)
            with lock:
                asset['state'] = 'loading'
                manifest['status'] = 'loading'
                save()
            stage = 'embedding'
            index.warm(entry)
            stage = 'pg_oss_import'
            record = service.import_document(entry, execute=True)
            if record['revision'] != 1:
                raise ValueError('trial entry unexpectedly changed revision')
            # Only this isolated catalogue is made visible. This is not formal approval.
            stage = 'index_publish_verify'
            result = service.publish(entry['knowledge_id'], revision=1,
                confirmed_by='隔离试用，非正式审核', execute=True)
            if result['verification']['verified_chunk_ids'] != asset['chunk_ids']:
                raise ValueError('trial chunk plan mismatch')
            with lock:
                asset['state'] = 'ready'
                asset.pop('error_type', None)
                asset.pop('error_stage', None)
                save()
        except Exception as error:
            with lock:
                asset.update(state='error', error_type=type(error).__name__, error_stage=stage)
                save()
        with lock:
            print(json.dumps({'ready': sum(row['state'] == 'ready' for row in manifest['assets']),
                'errors': sum(row['state'] == 'error' for row in manifest['assets']),
                'total': len(manifest['assets'])}), flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, manifest['assets']))
    manifest['status'] = 'ready' if all(asset['state'] == 'ready' for asset in manifest['assets']) else 'partial'
    save()
    return {'status': manifest['status'], 'ready': sum(a['state'] == 'ready' for a in manifest['assets']),
            'total': len(manifest['assets'])}


def serve(root, port):
    import uvicorn
    from rag_app.knowledge_trial_ui import create_app
    manifest = validate_plan(root)
    runtime = ensure_database(root, manifest)
    ready = [asset for asset in manifest['assets'] if asset['state'] == 'ready']
    if not ready:
        raise ValueError('no ready assets')
    catalog = {'trial_id': manifest['trial_id'], 'asset_count': len(ready),
        'chunk_count': sum(len(asset['chunk_ids']) for asset in ready), 'assets': ready,
        'sample_queries': read(root / 'sample_queries.json') if (root / 'sample_queries.json').exists()
                          else [asset['title'] for asset in ready[:6]]}
    runtime.update(server_pid=os.getpid(), server_port=port)
    private_write(root / 'runtime.json', runtime)
    uvicorn.run(create_app(service_for(root, manifest, runtime), catalog), host='127.0.0.1', port=port, access_log=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'load', 'serve', 'status'])
    parser.add_argument('--trial', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='Required for cloud writes by load')
    parser.add_argument('--extend', action='store_true', help='prepare: append newly eligible judgments to this trial; never replace existing entries')
    parser.add_argument('--workers', type=int, choices=range(1, 9), default=4)
    parser.add_argument('--port', type=int, default=8788)
    args = parser.parse_args()
    root = args.trial.resolve()
    if args.command == 'serve' or args.command == 'load' and args.execute:
        load_env()
    if args.command == 'prepare':
        with (root / 'operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = prepare(root, extend=args.extend)
    elif args.command == 'load':
        if args.execute:
            with (root / 'operation.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = load(root, args.workers)
        else:
            result = {'status': 'dry_run', 'will_write': False}
    elif args.command == 'serve':
        if not 1024 <= args.port <= 65535:
            raise ValueError('invalid loopback port')
        return serve(root, args.port)
    else:
        manifest = read(root / 'trial.json')
        result = {'status': manifest['status'], 'ready': sum(a['state'] == 'ready' for a in manifest['assets']),
                  'total': len(manifest['assets'])}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") in {"partial", "failed", "no_eligible_assets"} else 0


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
