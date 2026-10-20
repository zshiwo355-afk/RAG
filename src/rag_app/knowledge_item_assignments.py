"""Reviewed item roles and durable source bindings; never changes receipt bytes."""
from __future__ import annotations

import hashlib
import json
import uuid

from .knowledge_receipts import ReceiptError, _utc, validate_principal
from .knowledge_store import validate_role_id

TABLES = ('knowledge_item_source_bindings', 'knowledge_item_role_assignments', 'knowledge_role_aliases')
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS knowledge_item_source_bindings (
       item_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, principal TEXT NOT NULL,
       external_source_id TEXT NOT NULL, parser_identity TEXT NOT NULL,
       department TEXT NOT NULL, position TEXT NOT NULL, source_locator TEXT NOT NULL DEFAULT '', material_hash TEXT NOT NULL,
       canonical_identity TEXT, created_at DOUBLE PRECISION NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS knowledge_item_role_assignments (
       item_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, version BIGINT NOT NULL,
       actor TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL, confirmation_source TEXT NOT NULL,
       remember_rule INTEGER NOT NULL, rule_status TEXT NOT NULL, source_action TEXT NOT NULL,
       requeued INTEGER NOT NULL, resume_pending INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS knowledge_role_aliases (
       declaration_key TEXT PRIMARY KEY, department TEXT NOT NULL, position TEXT NOT NULL,
       role_id TEXT NOT NULL, actor TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS knowledge_item_sources_external ON knowledge_item_source_bindings(principal,external_source_id,parser_identity)',
)
PLACEHOLDERS = {'未知', '未确认', '待补充', '未取得', '未提供', '待填写', '未填写', '无', '暂无', '-', 'N/A'}


def label(value):
    result = ' '.join(value.split()) if isinstance(value, str) else ''
    return '' if result in PLACEHOLDERS else result


def error(code, status=409):
    from .knowledge_processing import ProcessingError
    return ProcessingError(code, status)


def ready(store, connection):
    if store.database_url:
        return (all(connection.execute("SELECT to_regclass('%s') IS NOT NULL AS ready" % name).fetchone()['ready'] for name in TABLES)
            and bool(connection.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='knowledge_item_source_bindings' AND column_name='source_locator'").fetchone()))
    return (all(connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() for name in TABLES)
            and any(row['name'] == 'source_locator' for row in connection.execute('PRAGMA table_info(knowledge_item_source_bindings)')))


def initialize(store, *, apply=False):
    with store._connection(write=apply) as connection:
        if apply:
            store._lock(connection, 'schema:knowledge-processing')
            for statement in SCHEMA:
                connection.execute(statement)
            columns = ({row['column_name'] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='knowledge_item_source_bindings'")}
                if store.database_url else {row['name'] for row in connection.execute('PRAGMA table_info(knowledge_item_source_bindings)')})
            if 'source_locator' not in columns:
                connection.execute("ALTER TABLE knowledge_item_source_bindings ADD COLUMN source_locator TEXT NOT NULL DEFAULT ''")
        return {'ready': bool(ready(store, connection)), 'applied': apply, 'tables': list(TABLES)}


def declaration_key(department, position):
    return hashlib.sha256(json.dumps([department, position], ensure_ascii=False).encode()).hexdigest()


def context(store, row, item, entry, content, asset):
    evidence = entry.get('evidence', {})
    department, position = label(evidence.get('source_uploader_department')), label(evidence.get('source_uploader_position'))
    # Hash is only an exact-replay discriminator. It never becomes a source identity.
    source = {k: v for k, v in evidence.items() if k.startswith('source_') and k not in {'source_base_revision', 'source_revision_label'}}
    from .knowledge_governance import _attachment_hash
    material = {'attachments': _attachment_hash(asset), 'source_path': asset['source_path'], 'content': content, 'title': entry['title'], 'kind': entry.get('kind'),
                'source': source, 'role': [entry.get(k) for k in ('role_id', 'department', 'role_name')]}
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with store._connection(write=True) as connection:
        store.fence(connection, row)
        if not ready(store, connection):
            raise error('uploader_role_schema_required', 503)
        store._execute(connection, '''INSERT INTO knowledge_item_source_bindings
            (item_id,job_id,principal,external_source_id,parser_identity,department,position,source_locator,material_hash,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO NOTHING''',
            (item['item_id'], row['job_id'], row['principal'], row['source_id'], entry['knowledge_id'], department, position, label(evidence.get('source_locator')), digest, store.clock()))


def effective_identity(store, connection, item_id, fallback):
    if not ready(store, connection):
        return fallback
    row = store._execute(connection, 'SELECT canonical_identity FROM knowledge_item_source_bindings WHERE item_id=?', (item_id,)).fetchone()
    if row:
        original = store._execute(connection, 'SELECT parser_identity FROM knowledge_item_source_bindings WHERE item_id=?', (item_id,)).fetchone()
        if original['parser_identity'] != fallback:
            raise error('processing_submission_mismatch', 422)
    return row['canonical_identity'] if row and row['canonical_identity'] else fallback


def _replacement_sources(store, connection, row, current):
    prior = store._execute(connection, '''SELECT b.canonical_identity,b.department,b.position,
        i.item_id,i.revision,i.resolution_json,a.role_id,a.actor,a.confirmation_source
        FROM knowledge_item_source_bindings b JOIN knowledge_processing_items i ON i.item_id=b.item_id AND i.job_id=b.job_id
        JOIN knowledge_processing_jobs j ON j.job_id=i.job_id
        LEFT JOIN knowledge_item_role_assignments a ON a.item_id=i.item_id
        WHERE b.principal=? AND b.external_source_id=? AND b.parser_identity=? AND b.job_id<>?
        AND j.principal=b.principal AND j.source_id=b.external_source_id
        AND i.status='archived' AND i.publication_json IS NULL AND b.canonical_identity IS NOT NULL
        AND i.resolution_json IS NOT NULL''',
        (row['principal'], row['source_id'], current['parser_identity'], row['job_id'])).fetchall()
    sources = []
    for previous in prior:
        resolution = json.loads(previous['resolution_json'])
        if (resolution.get('action') == 'replace' and resolution.get('replacement_job_id') == row['job_id']
                and resolution.get('replacement_receipt_id') == row['receipt_id']):
            sources.append(dict(previous))
    return sources


def replacement_identity(store, connection, row, current):
    identities = {source['canonical_identity'] for source in _replacement_sources(store, connection, row, current)}
    if len(identities) > 1:
        raise error('source_relation_review', 422)
    return next(iter(identities), None)


def resolve_source(store, row, item, entry):
    with store._connection(write=True) as connection:
        store.fence(connection, row)
        store._lock(connection, 'source-binding:' + row['principal'] + ':' + row['source_id'])
        current = store._execute(connection, 'SELECT * FROM knowledge_item_source_bindings WHERE item_id=?', (item['item_id'],)).fetchone()
        if current['canonical_identity']:
            return current['canonical_identity']
        assignment = store._execute(connection, 'SELECT * FROM knowledge_item_role_assignments WHERE item_id=?', (item['item_id'],)).fetchone()
        replacement = replacement_identity(store, connection, row, current)
        peers = store._execute(connection, '''SELECT DISTINCT b.canonical_identity,b.material_hash,b.department,b.position,b.source_locator FROM knowledge_item_source_bindings b
            WHERE b.principal=? AND b.external_source_id=? AND b.parser_identity=? AND b.item_id<>?
            AND b.canonical_identity IS NOT NULL''', (row['principal'], row['source_id'], current['parser_identity'], item['item_id'])).fetchall()
        # Legacy submissions have no binding row and retain their original identity forever.
        legacy = store._execute(connection, '''SELECT DISTINCT s.source_identity,r.sha256 FROM knowledge_submissions s
            JOIN knowledge_upload_receipts r ON r.receipt_id=s.receipt_id
            LEFT JOIN knowledge_item_source_bindings b ON b.item_id=s.item_id
            WHERE s.principal=? AND s.source_id=? AND s.source_identity=? AND b.item_id IS NULL''',
            (row['principal'], row['source_id'], current['parser_identity'])).fetchall()
        identities = {p['canonical_identity'] for p in peers} | {p['source_identity'] for p in legacy}
        exact = {p['canonical_identity'] for p in peers if p['material_hash'] == current['material_hash']}
        receipt_hash = store._execute(connection, 'SELECT sha256 FROM knowledge_upload_receipts WHERE receipt_id=?', (row['receipt_id'],)).fetchone()['sha256']
        exact.update(p['source_identity'] for p in legacy if p['sha256'] == receipt_hash)
        base = entry.get('evidence', {}).get('source_base_revision')
        related = set(identities)
        if base and len(related) > 1 and all(current[key] for key in ('department', 'position', 'source_locator')):
            related = {p['canonical_identity'] for p in peers
                       if all(p[key] == current[key] for key in ('department', 'position', 'source_locator'))}
            # Legacy ownership is retained; use its recorded original declarations only as relation hints.
            for prior in legacy:
                record = store._execute(connection, '''SELECT r.record_json FROM knowledge_submissions s
                    JOIN knowledge_revisions r ON r.knowledge_id=s.knowledge_id AND r.revision=s.revision
                    WHERE s.principal=? AND s.source_id=? AND s.source_identity=? ORDER BY s.created_at DESC LIMIT 1''',
                    (row['principal'], row['source_id'], prior['source_identity'])).fetchone()
                evidence = json.loads(record['record_json']).get('evidence', {}) if record else {}
                if (label(evidence.get('source_uploader_department')) == current['department']
                        and label(evidence.get('source_uploader_position')) == current['position']
                        and label(evidence.get('source_locator')) == current['source_locator']):
                    related.add(prior['source_identity'])
        if assignment and assignment['source_action'] == 'new':
            identity = 'kg_' + uuid.uuid4().hex
        elif replacement:
            # A reviewed item relation chooses the source, never its revision baseline.
            identity = replacement
        elif len(exact) == 1:
            identity = next(iter(exact))
        elif base and len(related) == 1:
            # Governance retains the source's established role and checks revision conflicts.
            identity = next(iter(related))
        elif not identities:
            # Preserve existing intra-package case/reference IDs on first submission.
            identity = current['parser_identity']
        else:
            raise error('source_relation_review', 422)
        store._execute(connection, 'UPDATE knowledge_item_source_bindings SET canonical_identity=? WHERE item_id=?', (identity, item['item_id']))
        return identity


def bind_references(store, row, item, entry, identity):
    evidence = dict(entry.get('evidence', {}))
    references = {value['knowledge_id'] for value in evidence.get('related_assets', []) if value.get('knowledge_id')}
    if evidence.get('parent_reference_id'):
        references.add(evidence['parent_reference_id'])
    if not references:
        return entry
    with store._connection() as connection:
        mappings = {r['parser_identity']: r['canonical_identity'] for r in store._execute(connection,
            'SELECT parser_identity,canonical_identity FROM knowledge_item_source_bindings WHERE job_id=?', (row['job_id'],))}
        # Durable siblings from before this migration retain their established target.
        mappings.update({r['parser_identity']: r['knowledge_id'] for r in store._execute(connection,
            '''SELECT COALESCE(b.parser_identity,s.source_identity) AS parser_identity,s.knowledge_id
               FROM knowledge_submissions s LEFT JOIN knowledge_item_source_bindings b ON b.item_id=s.item_id
               WHERE s.receipt_id=?''', (row['receipt_id'],))})
    if any(not mappings.get(ref) for ref in references):
        # A confirmed split cannot keep references pointing at somebody else's old asset.
        raise error('source_relation_review', 422)
    evidence['related_assets'] = [{**value, 'knowledge_id': mappings.get(value['knowledge_id']) or value['knowledge_id']}
        for value in evidence.get('related_assets', [])]
    if evidence.get('parent_reference_id'):
        evidence['parent_reference_id'] = mappings.get(evidence['parent_reference_id']) or evidence['parent_reference_id']
    return {**entry, 'evidence': evidence}


def apply_role(store, item_id, entry):
    with store._connection(write=True) as connection:
        store._lock(connection, 'item-role:' + item_id)
        current = store._execute(connection, 'SELECT * FROM knowledge_item_role_assignments WHERE item_id=?', (item_id,)).fetchone()
        binding = store._execute(connection, 'SELECT * FROM knowledge_item_source_bindings WHERE item_id=?', (item_id,)).fetchone()
        if current is None:
            job = store._job(connection, binding['job_id'], None)
            sources = [source for source in _replacement_sources(store, connection, job, binding)
                       if source['revision'] is None and source['confirmation_source'] in {'manual', 'legacy'} and source['role_id']]
            if len({source['role_id'] for source in sources}) > 1:
                raise ValueError('uploader_role_ambiguous')
            if sources:
                previous = sources[0]
                role = store._execute(connection, 'SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?', (previous['role_id'],)).fetchone()
                if role is None:
                    raise ValueError('uploader_role_not_found')
                unchanged = any((binding['department'], binding['position']) == (source['department'], source['position']) for source in sources)
                if ((not unchanged and any(binding[key] and binding[key] != role[target] for key, target in [('department', 'department'), ('position', 'name')]))
                        or any(entry.get(key) and entry[key] != role[target] for key, target in [('role_id', 'role_id'), ('department', 'department'), ('role_name', 'name')])):
                    raise ValueError('uploader_role_conflict')
                store._execute(connection, '''INSERT INTO knowledge_item_role_assignments
                    (item_id,role_id,version,actor,updated_at,confirmation_source,remember_rule,rule_status,source_action,requeued,resume_pending)
                    VALUES (?,?,1,?,?,'manual',0,?,'auto',0,0) ON CONFLICT(item_id) DO NOTHING''',
                    (item_id, role['role_id'], previous['actor'], store.clock(),
                     'none' if binding['department'] and binding['position'] else 'incomplete'))
                store._event(connection, job['job_id'], 'item_uploader_role_inherited',
                    json.dumps({'item_id': item_id, 'source_item_ids': sorted(source['item_id'] for source in sources), 'role_id': role['role_id']}, sort_keys=True),
                    previous['actor'])
                current = store._execute(connection, 'SELECT * FROM knowledge_item_role_assignments WHERE item_id=?', (item_id,)).fetchone()
        if current is None and binding['department'] and binding['position']:
            current = store._execute(connection, 'SELECT * FROM knowledge_role_aliases WHERE declaration_key=?',
                (declaration_key(binding['department'], binding['position']),)).fetchone()
            if current is not None:
                store._execute(connection, '''INSERT INTO knowledge_item_role_assignments
                    (item_id,role_id,version,actor,updated_at,confirmation_source,remember_rule,rule_status,source_action,requeued,resume_pending)
                    VALUES (?,?,1,?,?,'alias',0,'matched','auto',0,0) ON CONFLICT(item_id) DO NOTHING''',
                    (item_id, current['role_id'], current['actor'], store.clock()))
        if current is None:
            return entry
        role = store._execute(connection, 'SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?', (current['role_id'],)).fetchone()
        if role is None:
            raise error('uploader_role_not_found', 404)
        selected = dict(current)
        if selected.get('confirmation_source', 'alias') == 'alias' and (entry.get('role_id') or entry.get('role_name')):
            if (entry.get('role_id'), entry.get('department'), entry.get('role_name')) != (role['role_id'], role['department'], role['name']):
                raise ValueError('uploader_role_conflict')
        return {**entry, 'role_id': role['role_id'], 'department': role['department'], 'role_name': role['name']}


def _item(store, connection, job_id, item_id):
    item = store._execute(connection, 'SELECT * FROM knowledge_processing_items WHERE job_id=? AND item_id=?', (job_id, item_id)).fetchone()
    if item is None:
        raise error('processing_item_not_found', 404)
    return dict(item)


def _original(store, connection, job, item):
    binding = store._execute(connection, 'SELECT * FROM knowledge_item_source_bindings WHERE item_id=?', (item['item_id'],)).fetchone() if ready(store, connection) else None
    if binding:
        return {'department': binding['department'] or None, 'position': binding['position'] or None}
    if store.display_metadata is not None:
        values = store.display_metadata.project([{**item, 'receipt_id': job['receipt_id']}], str(store.path or store.database_url))
        position, department = values.get(item['item_id'], (None, None))
        return {'department': label(department) or None, 'position': label(position) or None}
    return {'department': None, 'position': None}


def _eligible(store, connection, job, item):
    return bool(item['status'] == 'needs_review' and item['revision'] is None and item['content_ref']
        and not item['publication_json'] and not item['resolution_json']
        and not store._execute(connection, 'SELECT 1 FROM knowledge_submissions WHERE item_id=?', (item['item_id'],)).fetchone()
        and job['rule_version'] == 'receipt-rules-v2')


def _dto(store, connection, job, item, *, requeued=False):
    original = _original(store, connection, job, item)
    complete = bool(original['department'] and original['position'])
    assignment = store._execute(connection, 'SELECT * FROM knowledge_item_role_assignments WHERE item_id=?', (item['item_id'],)).fetchone() if ready(store, connection) else None
    eligible = _eligible(store, connection, job, item)
    alias = store._execute(connection, 'SELECT * FROM knowledge_role_aliases WHERE declaration_key=?',
        (declaration_key(original['department'], original['position']),)).fetchone() if complete and eligible and ready(store, connection) else None
    selected = assignment or alias
    role = store._execute(connection, 'SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?', (selected['role_id'],)).fetchone() if selected else None
    if item['revision'] is not None and item['knowledge_id']:
        record = store._execute(connection, 'SELECT record_json FROM knowledge_revisions WHERE knowledge_id=? AND revision=?', (item['knowledge_id'], item['revision'])).fetchone()
        actual_id = json.loads(record['record_json']).get('role_id') if record else None
        role = store._execute(connection, 'SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?', (actual_id,)).fetchone() if actual_id else None
    from .knowledge_processing import UPLOADER_ROLE_REASONS
    remaining = json.loads(item['reason_codes'])
    if assignment and assignment['resume_pending']:
        remaining = [r for r in remaining if r not in UPLOADER_ROLE_REASONS and not (r == 'source_relation_review' and assignment['source_action'] == 'new')]
    return {'original': original, 'role': dict(role) if role else None, 'version': assignment['version'] if assignment else 0,
            'updated_at': _utc(selected['updated_at']) if selected else None, 'requeued': requeued,
            'confirmation_source': assignment['confirmation_source'] if assignment else 'alias' if alias else None,
            'can_remember': complete, 'remembered': bool(alias and role and alias['role_id'] == role['role_id']),
            'rule_status': assignment['rule_status'] if assignment else 'matched' if alias else 'none' if complete else 'incomplete',
            'remaining_reasons': remaining, 'eligible': eligible}


def get_role(store, job_id, item_id):
    with store._connection() as connection:
        job = store._job(connection, job_id, None)
        return _dto(store, connection, job, _item(store, connection, job_id, item_id))


def _preserves_source_role(store, connection, job, item, role_id):
    binding = store._execute(connection, 'SELECT canonical_identity FROM knowledge_item_source_bindings WHERE item_id=?',
        (item['item_id'],)).fetchone() if ready(store, connection) else None
    established = store._execute(connection, '''SELECT r.record_json FROM knowledge_submissions s
        JOIN knowledge_assets a ON a.knowledge_id=s.owned_knowledge_id
        JOIN knowledge_revisions r ON r.knowledge_id=a.knowledge_id AND r.revision=a.latest_revision
        WHERE s.principal=? AND s.source_id=? AND s.source_identity=? LIMIT 1''',
        (job['principal'], job['source_id'], binding['canonical_identity'])).fetchone() if binding and binding['canonical_identity'] else None
    return not established or json.loads(established['record_json']).get('role_id') == role_id


def _will_requeue(store, connection, job, item, role_id, source_action='auto', *, legacy=False):
    from .knowledge_processing import UPLOADER_ROLE_REASONS
    reasons = set(json.loads(item['reason_codes']))
    if legacy and store._execute(connection, 'SELECT 1 FROM knowledge_submissions WHERE principal=? AND source_id=? LIMIT 1', (job['principal'], job['source_id'])).fetchone():
        # Deployment migration may remember the old explicit choice, but must
        # not advance an existing publication when the new worker resumes.
        return False
    preserves_role = _preserves_source_role(store, connection, job, item, role_id)
    allowed = UPLOADER_ROLE_REASONS | ({'source_relation_review'} if source_action == 'new' else set())
    return bool(preserves_role and reasons and reasons <= allowed and job['status'] == 'needs_review'
        and store._execute(connection, "SELECT 1 FROM knowledge_upload_receipts WHERE receipt_id=? AND status='verified'",
            (job['receipt_id'],)).fetchone())


def set_role(store, job_id, item_id, role_id, expected_version, *, actor, remember_rule=False, source_action='auto', confirmation_source='manual'):
    from .knowledge_processing import UPLOADER_ROLE_REASONS
    try:
        validate_role_id(role_id)
        validate_principal(actor)
    except (ValueError, ReceiptError):
        raise error('invalid_uploader_role_selection', 400) from None
    if (type(expected_version) is not int or not 0 <= expected_version <= 2147483647
            or type(remember_rule) is not bool or source_action not in {'auto', 'new'}):
        raise error('invalid_uploader_role_selection', 400)
    with store._connection(write=True) as connection:
        job = store._job(connection, job_id, None, lock=True)
        if not ready(store, connection):
            raise error('uploader_role_schema_required', 503)
        item = _item(store, connection, job_id, item_id)
        current = store._execute(connection, 'SELECT * FROM knowledge_item_role_assignments WHERE item_id=?', (item_id,)).fetchone()
        version = current['version'] if current else 0
        if (current and expected_version == version - 1 and current['role_id'] == role_id
                and bool(current['remember_rule']) == remember_rule and current['source_action'] == source_action):
            return _dto(store, connection, job, item, requeued=bool(current['requeued']))
        if expected_version != version or version == 2147483647:
            raise error('uploader_role_version_conflict')
        if job['status'] in {'queued', 'running'} or job['lease_token'] or job['lease_until']:
            raise error('uploader_role_job_busy')
        if not store._execute(connection, 'SELECT 1 FROM knowledge_roles WHERE role_id=?', (role_id,)).fetchone():
            raise error('uploader_role_not_found', 404)
        eligible = _eligible(store, connection, job, item)
        if not eligible:
            raise error('uploader_role_assignment_locked')
        if confirmation_source == 'legacy' and store._execute(connection, "SELECT 1 FROM knowledge_processing_jobs WHERE status='running' AND lease_until>? LIMIT 1", (store.clock(),)).fetchone():
            raise error('uploader_role_job_busy')
        reasons = set(json.loads(item['reason_codes']))
        if source_action == 'new' and 'source_relation_review' not in reasons:
            raise error('source_action_not_allowed')
        original = _original(store, connection, job, item)
        complete = bool(original['department'] and original['position'])
        rule_status = 'none' if complete else 'incomplete'
        now = store.clock()
        if remember_rule and complete:
            key = declaration_key(original['department'], original['position'])
            store._lock(connection, 'role-alias:' + key)
            alias = store._execute(connection, 'SELECT * FROM knowledge_role_aliases WHERE declaration_key=?', (key,)).fetchone()
            if alias and alias['role_id'] != role_id:
                rule_status = 'conflict'
            else:
                store._execute(connection, '''INSERT INTO knowledge_role_aliases
                    (declaration_key,department,position,role_id,actor,updated_at) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(declaration_key) DO NOTHING''', (key, original['department'], original['position'], role_id, actor, now))
                rule_status = 'saved'
        requeued = _will_requeue(store, connection, job, item, role_id, source_action, legacy=confirmation_source == 'legacy')
        store._execute(connection, '''INSERT INTO knowledge_item_role_assignments
            (item_id,role_id,version,actor,updated_at,confirmation_source,remember_rule,rule_status,source_action,requeued,resume_pending)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET role_id=excluded.role_id,
            version=excluded.version,actor=excluded.actor,updated_at=excluded.updated_at,
            confirmation_source=excluded.confirmation_source,remember_rule=excluded.remember_rule,
            rule_status=excluded.rule_status,source_action=excluded.source_action,requeued=excluded.requeued,resume_pending=excluded.resume_pending''',
            (item_id, role_id, version + 1, actor, now, confirmation_source, int(remember_rule), rule_status, source_action, int(requeued), int(requeued)))
        store._event(connection, job_id, 'item_uploader_role_selected', json.dumps({'item_id': item_id, 'role_id': role_id, 'version': version + 1, 'remember_rule': remember_rule, 'rule_status': rule_status, 'source_action': source_action}, sort_keys=True), actor)
        if confirmation_source != 'legacy' and not requeued:
            remaining = [reason for reason in json.loads(item['reason_codes']) if reason not in UPLOADER_ROLE_REASONS]
            if not _preserves_source_role(store, connection, job, item, role_id):
                remaining.append('uploader_role_conflict')
            if remaining != json.loads(item['reason_codes']):
                item['reason_codes'] = json.dumps(remaining)
                store._execute(connection, '''UPDATE knowledge_processing_items
                    SET reason_codes=?,version=version+1,updated_at=? WHERE item_id=?''', (item['reason_codes'], now, item_id))
                store._execute(connection, 'UPDATE knowledge_processing_jobs SET updated_at=? WHERE job_id=?', (now, job_id))
        if requeued:
            store._execute(connection, "UPDATE knowledge_processing_jobs SET status='queued',stage='queued',attempts=0,available_at=0,error_code=NULL,updated_at=? WHERE job_id=?", (now, job_id))
            store._event(connection, job_id, 'retried', 'item_uploader_role_selected', actor)
        return _dto(store, connection, job, item, requeued=requeued)


def single_item(store, job_id):
    with store._connection() as connection:
        job = store._job(connection, job_id, None)
        items = store._execute(connection, "SELECT item_id FROM knowledge_processing_items WHERE job_id=? AND (content_ref IS NOT NULL OR knowledge_id IS NOT NULL OR (status<>'archived' AND metadata_ref IS NOT NULL))", (job_id,)).fetchall()
        if not items and job['status'] in {'queued', 'running'}:
            raise error('uploader_role_job_busy')
        if len(items) != 1:
            raise error('uploader_role_item_required')
        return items[0]['item_id']


def migration(store, *, apply=False):
    with store._connection() as connection:
        schema_ready = ready(store, connection)
        events = store._execute(connection, "SELECT * FROM knowledge_processing_events WHERE event_type='uploader_role_selected' ORDER BY created_at,event_id").fetchall()
        latest = {event['job_id']: dict(event) for event in events}
        tied = {job_id for job_id, event in latest.items() if len({e['reason_code'] for e in events
                if e['job_id'] == job_id and e['created_at'] == event['created_at']}) > 1}
        active = store._execute(connection, "SELECT 1 FROM knowledge_processing_jobs WHERE status='running' AND lease_until>? LIMIT 1", (store.clock(),)).fetchone()
        rows = []
        for job_id, event in latest.items():
            job = store._job(connection, job_id, None)
            items = store._execute(connection, "SELECT * FROM knowledge_processing_items WHERE job_id=? AND (content_ref IS NOT NULL OR knowledge_id IS NOT NULL OR (status<>'archived' AND metadata_ref IS NOT NULL))", (job_id,)).fetchall()
            status, reason, item = 'ambiguous', 'uploader_role_item_required', items[0] if len(items) == 1 else None
            if item and job_id not in tied:
                existing = store._execute(connection, 'SELECT 1 FROM knowledge_item_role_assignments WHERE item_id=?', (item['item_id'],)).fetchone() if schema_ready else None
                if existing:
                    status, reason = 'already_migrated', None
                elif active or job['status'] in {'queued', 'running'} or job['lease_token'] or job['lease_until']:
                    status, reason = 'blocked', 'uploader_role_job_busy'
                elif not store._execute(connection, 'SELECT 1 FROM knowledge_roles WHERE role_id=?', (event['reason_code'],)).fetchone():
                    status, reason = 'blocked', 'uploader_role_not_found'
                elif not _eligible(store, connection, job, item):
                    status, reason = 'blocked', 'uploader_role_assignment_locked'
                else:
                    status, reason = 'eligible', None
            rows.append({'job_id': job_id, 'item_id': item['item_id'] if item else None, 'role_id': event['reason_code'], 'status': status, 'reason': reason,
                         'will_requeue': bool(status == 'eligible' and _will_requeue(store, connection, job, item, event['reason_code'], legacy=True))})
    if apply:
        if not schema_ready:
            raise error('uploader_role_schema_required', 503)
        for result in rows:
            if result['status'] == 'eligible':
                event = latest[result['job_id']]
                from .knowledge_processing import ProcessingError
                try:
                    saved = set_role(store, result['job_id'], result['item_id'], result['role_id'], 0,
                             actor=event['actor'] or 'legacy-role-migration', confirmation_source='legacy')
                    result.update(applied=True, requeued=saved['requeued'])
                except ProcessingError as exc:
                    result.update(status='blocked', reason=exc.code, applied=False, requeued=False)
    return {'applied': apply, 'items': rows, 'counts': {state: sum(r['status'] == state for r in rows)
            for state in ('eligible', 'blocked', 'ambiguous', 'already_migrated')}}
