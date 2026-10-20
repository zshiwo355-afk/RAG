"""Explicit, reversible first-revision role repair; no object or vector writes.

Operator-only library entry points. Preview is the default. Application needs an
approved pinned plan and an exclusive local snapshot file; no HTTP endpoint or
automatic migration invokes this module.
"""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path

from .knowledge_governance import fingerprint_record
from .knowledge_store import _json, _knowledge_id, normalize_entry, validate_role_id


ROLE_FIELDS = ('role_id', 'department', 'role_name')


def review_published_role_assignments(store, *, limit=100, offset=0):
    """Read one page of public metadata; findings are review hints, never repairs.

    Pagination counts published revisions, not suspicious findings. This avoids
    scanning the entire catalogue or hydrating object bodies to report a page.
    Only frozen per-item server assignments establish role confirmation.
    """
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or not 0 <= offset <= 2**31 - 1:
        raise ValueError('invalid_published_role_review_page')
    label = lambda value: ' '.join(value.split()) if isinstance(value, str) else ''
    tables = ('knowledge_submissions', 'knowledge_processing_jobs', 'knowledge_processing_items',
              'knowledge_processing_events', 'knowledge_item_role_assignments')
    with store._connection() as connection:
        if connection is None:
            raise ValueError('published_role_review_schema_required')
        ready = {}
        for table in tables:
            found = store._execute(connection,
                'SELECT to_regclass(?) AS name' if store.database_url else
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            ready[table] = bool(found and found['name'])
        total = connection.execute('SELECT COUNT(*) AS total FROM knowledge_assets WHERE published_revision IS NOT NULL').fetchone()['total']
        rows = store._execute(connection, '''SELECT a.knowledge_id,a.published_revision AS revision,r.record_json,r.published_at
            FROM knowledge_assets a JOIN knowledge_revisions r
              ON r.knowledge_id=a.knowledge_id AND r.revision=a.published_revision
            WHERE a.published_revision IS NOT NULL ORDER BY a.knowledge_id LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
        findings = []
        for row in rows:
            record = json.loads(row['record_json'])
            evidence = record.get('evidence', {})
            original = {'department': label(evidence.get('source_uploader_department')) or None,
                        'position': label(evidence.get('source_uploader_position')) or None}
            reasons, confirmations, related_jobs = set(), set(), set()
            role = {field: record.get(field) for field in ROLE_FIELDS}
            origins = []
            if ready['knowledge_submissions']:
                origins = store._execute(connection, '''SELECT item_id,receipt_id,principal,source_id
                    FROM knowledge_submissions WHERE knowledge_id=? AND revision=?
                      AND owned_knowledge_id=knowledge_id AND disposition IN ('new','new_revision')
                    ORDER BY item_id LIMIT 101''', (row['knowledge_id'], row['revision'])).fetchall()
            for origin in origins[:100]:
                if ready['knowledge_item_role_assignments']:
                    selected = store._execute(connection, '''SELECT role_id,confirmation_source
                        FROM knowledge_item_role_assignments WHERE item_id=?''', (origin['item_id'],)).fetchone()
                    if (selected and selected['role_id'] == role['role_id']
                            and selected['confirmation_source'] in {'manual', 'alias', 'legacy'}):
                        confirmations.add(selected['confirmation_source'])
                if ready['knowledge_processing_jobs'] and ready['knowledge_processing_events']:
                    events = store._execute(connection, '''SELECT e.reason_code,e.created_at
                        FROM knowledge_processing_events e JOIN knowledge_processing_jobs j ON j.job_id=e.job_id
                        WHERE j.receipt_id=? AND j.principal=? AND e.event_type='uploader_role_selected'
                        ORDER BY e.created_at DESC,e.event_id LIMIT 101''', (origin['receipt_id'], origin['principal'])).fetchall()
                    if len(events) > 100:
                        reasons.add('selection_history_check_truncated')
                    # A later job action is not evidence for an earlier publication.
                    try:
                        published_at = datetime.fromisoformat(row['published_at'].replace('Z', '+00:00')).timestamp()
                    except (AttributeError, TypeError, ValueError):
                        published_at = None
                    historical = [event for event in events[:100] if published_at is not None and event['created_at'] <= published_at]
                    if historical:
                        latest = {event['reason_code'] for event in historical if event['created_at'] == historical[0]['created_at']}
                        if len(latest) > 1:
                            reasons.add('legacy_selection_ambiguous')
                        elif latest != {role['role_id']}:
                            reasons.add('legacy_selection_differs')
                if ready['knowledge_processing_jobs'] and ready['knowledge_processing_items']:
                    pending = store._execute(connection, '''SELECT DISTINCT j.job_id,i.reason_codes
                        FROM knowledge_processing_jobs j JOIN knowledge_processing_items i ON i.job_id=j.job_id
                        WHERE j.principal=? AND j.source_id=? AND j.receipt_id<>?
                          AND i.status='needs_review' ORDER BY j.job_id LIMIT 101''',
                        (origin['principal'], origin['source_id'], origin['receipt_id'])).fetchall()
                    for item in pending[:100]:
                        if set(json.loads(item['reason_codes'])) & {
                                'source_relation_review', 'source_binding_review_required',
                                'source_base_revision_required', 'uploader_role_conflict'}:
                            related_jobs.add(item['job_id'])
                    if len(pending) > 100:
                        reasons.add('related_jobs_check_truncated')
            if confirmations:
                reasons.discard('legacy_selection_differs')
                reasons.discard('legacy_selection_ambiguous')
            elif original['position'] and (original['position'] != label(role['role_name'])
                    or original['department'] and original['department'] != label(role['department'])):
                reasons.add('declared_role_differs')
            if related_jobs:
                reasons.add('shared_source_requires_review')
            if len(origins) > 100:
                reasons.add('provenance_check_truncated')
            if reasons:
                findings.append({'knowledge_id': row['knowledge_id'], 'revision': row['revision'],
                    'original': original, 'role': role, 'reason_codes': sorted(reasons),
                    'confirmed': bool(confirmations), 'confirmation_sources': sorted(confirmations),
                    'receipt_ids': sorted({origin['receipt_id'] for origin in origins[:100]}),
                    'item_ids': [origin['item_id'] for origin in origins[:100]],
                    'related_job_ids': sorted(related_jobs)[:100]})
        return {'items': findings, 'total': total, 'total_basis': 'published_revisions',
                'limit': limit, 'offset': offset, 'scanned_count': len(rows),
                'review_candidates_this_page': len(findings), 'has_more': offset + len(rows) < total,
                'unavailable_checks': [table for table in tables if not ready[table]]}


def _state(store, connection, knowledge_id, revision):
    asset = dict(store._asset(connection, knowledge_id))
    row = store._execute(connection, 'SELECT * FROM knowledge_revisions WHERE knowledge_id=? AND revision=?',
                         (knowledge_id, revision)).fetchone()
    fingerprint = store._execute(connection,
        'SELECT * FROM knowledge_revision_fingerprints WHERE knowledge_id=? AND revision=?',
        (knowledge_id, revision)).fetchone()
    submissions = [dict(row) for row in store._execute(connection,
        'SELECT * FROM knowledge_submissions WHERE knowledge_id=? AND revision=? ORDER BY item_id',
        (knowledge_id, revision))]
    if row is None or fingerprint is None or not submissions:
        raise ValueError('role_repair_provenance_missing')
    return {'asset': asset, 'revision': dict(row), 'fingerprint': dict(fingerprint), 'submissions': submissions}


def _completed(store, connection, state, *, lock=False):
    rows = store._execute(connection, '''SELECT s.item_id,i.status AS item_status,j.status AS job_status
        FROM knowledge_submissions s JOIN knowledge_processing_items i ON i.item_id=s.item_id
          AND i.knowledge_id=s.knowledge_id AND i.revision=s.revision
        JOIN knowledge_processing_jobs j ON j.job_id=i.job_id AND j.receipt_id=s.receipt_id
          AND j.principal=s.principal AND j.source_id=s.source_id
        WHERE s.knowledge_id=? AND s.revision=?''' + (' FOR UPDATE OF i,j' if lock and store.database_url else ''),
        (state['asset']['knowledge_id'], state['revision']['revision'])).fetchall()
    if len(rows) != len(state['submissions']) or any(row['job_status'] != 'completed' or
            row['item_status'] not in {'published', 'duplicate', 'outdated', 'archived'} for row in rows):
        raise ValueError('role_repair_processing_incomplete')


def _patched(store, before, role):
    """Validate old hashes, then change only the role and its derived hashes."""
    record = json.loads(before['revision']['record_json'])
    if any(record.get(field) for field in ROLE_FIELDS):
        raise ValueError('role_repair_already_assigned')
    hydrated = store._hydrate(record)  # Read/hash-check only; preserve the original content_ref below.
    original = normalize_entry(hydrated)
    if original['payload_hash'] != record.get('payload_hash') or original['payload_hash'] != before['revision']['payload_hash']:
        raise ValueError('role_repair_payload_mismatch')
    old_fingerprint = before['fingerprint']
    expected = fingerprint_record(record, old_fingerprint['attachment_hash'])
    if any(old_fingerprint[key] != value for key, value in expected.items()) or any(
            row['comparison_hash'] != old_fingerprint['comparison_hash'] for row in before['submissions']):
        raise ValueError('role_repair_fingerprint_mismatch')
    normalized = normalize_entry({**hydrated, **role})
    after = deepcopy(before)
    record.update(**role, payload_hash=normalized['payload_hash'])
    after['revision'].update(payload_hash=normalized['payload_hash'], record_json=_json(record))
    after['fingerprint'].update(fingerprint_record(record, old_fingerprint['attachment_hash']))
    for row in after['submissions']:
        row['comparison_hash'] = after['fingerprint']['comparison_hash']
    after['asset']['generation'] += 1
    return after


def _prepare(store, plan):
    try:
        knowledge_id = _knowledge_id(plan['knowledge_id'])
        revision = plan['expected_revision']
        if type(revision) is not int or revision != 1:
            raise ValueError('role_repair_first_revision_only')
        if type(plan['expected_generation']) is not int or plan['expected_generation'] < 1:
            raise ValueError('role_repair_invalid_generation')
        role_id = validate_role_id(plan['proposed_role']['role_id'])
        if plan['original_role'] != dict.fromkeys(ROLE_FIELDS):
            raise ValueError('role_repair_original_role_mismatch')
        with store._connection() as connection:
            before = _state(store, connection, knowledge_id, revision)
            asset = before['asset']
            if (asset['latest_revision'] != revision or asset['published_revision'] != revision
                    or asset['status'] != 'published' or asset['generation'] != plan['expected_generation']):
                raise ValueError('role_repair_version_conflict')
            record = json.loads(before['revision']['record_json'])
            if record['content_hash'] != plan['expected_content_hash'] or record['payload_hash'] != plan['expected_payload_hash']:
                raise ValueError('role_repair_hash_conflict')
            role = store._execute(connection, 'SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?', (role_id,)).fetchone()
            if role is None or plan['proposed_role'] != {'role_id': role['role_id'], 'department': role['department'], 'role_name': role['name']}:
                raise ValueError('role_repair_target_role_mismatch')
            _completed(store, connection, before)
    except (KeyError, TypeError):
        raise ValueError('role_repair_invalid_plan') from None
    return {'format': 'role-assignment-v1', 'plan': deepcopy(plan),
            'before': before, 'after': _patched(store, before, plan['proposed_role'])}


def _write(store, connection, before, after):
    knowledge_id, revision = before['asset']['knowledge_id'], before['revision']['revision']
    statements = [
        ('UPDATE knowledge_assets SET generation=? WHERE knowledge_id=? AND generation=? AND latest_revision=? AND published_revision=?',
         (after['asset']['generation'], knowledge_id, before['asset']['generation'], revision, revision)),
        ('UPDATE knowledge_revisions SET payload_hash=?,record_json=? WHERE knowledge_id=? AND revision=? AND payload_hash=?',
         (after['revision']['payload_hash'], after['revision']['record_json'], knowledge_id, revision, before['revision']['payload_hash'])),
        ('UPDATE knowledge_revision_fingerprints SET comparison_hash=? WHERE knowledge_id=? AND revision=? AND comparison_hash=?',
         (after['fingerprint']['comparison_hash'], knowledge_id, revision, before['fingerprint']['comparison_hash'])),
    ]
    statements.extend(('UPDATE knowledge_submissions SET comparison_hash=? WHERE item_id=? AND request_hash=? AND comparison_hash=?',
        (new['comparison_hash'], old['item_id'], old['request_hash'], old['comparison_hash']))
        for old, new in zip(before['submissions'], after['submissions']))
    for statement, values in statements:
        if store._execute(connection, statement, values).rowcount != 1:
            raise ValueError('role_repair_concurrent_change')


def _locked_state(store, connection, snapshot):
    knowledge_id, revision = snapshot['asset']['knowledge_id'], snapshot['revision']['revision']
    store._lock(connection, 'write:knowledge-governance')
    store._lock(connection, knowledge_id)
    if _state(store, connection, knowledge_id, revision) != snapshot:
        raise ValueError('role_repair_snapshot_changed')
    _completed(store, connection, snapshot, lock=True)


def apply_role_assignment(store, plan, *, backup_path=None, execute=False):
    """Preview by default; apply one approved plan and fsync its rollback data first."""
    if type(execute) is not bool:
        raise ValueError('role_repair_invalid_execute')
    if execute and (plan.get('decision') != 'proposed' or plan.get('approved') is not True):
        raise ValueError('role_repair_not_approved')
    snapshot = _prepare(store, plan)
    if not execute:
        return {'status': 'preview', **snapshot}
    if backup_path is None:
        raise ValueError('role_repair_backup_required')
    with store._connection(write=True) as connection:
        _locked_state(store, connection, snapshot['before'])
        role = store._execute(connection, 'SELECT role_id,department,name FROM knowledge_roles WHERE role_id=?' +
            (' FOR SHARE' if store.database_url else ''), (plan['proposed_role']['role_id'],)).fetchone()
        if role is None or plan['proposed_role'] != {'role_id': role['role_id'], 'department': role['department'], 'role_name': role['name']}:
            raise ValueError('role_repair_target_role_mismatch')
        # A failed/uncertain commit keeps this intent snapshot. Rollback checks
        # the actual database against `after`; a snapshot alone is not success.
        descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(_json(snapshot) + '\n')
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(Path(backup_path).parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _write(store, connection, snapshot['before'], snapshot['after'])
    return {'status': 'applied', 'knowledge_id': plan['knowledge_id'], 'revision': plan['expected_revision'],
            'generation': snapshot['after']['asset']['generation'], 'backup_path': str(backup_path)}


def rollback_role_assignment(store, backup_path, *, execute=False):
    """Restore only an unchanged applied snapshot; generation always increases."""
    if type(execute) is not bool:
        raise ValueError('role_repair_invalid_execute')
    snapshot = json.loads(Path(backup_path).read_text(encoding='utf-8'))
    try:
        before, after = snapshot['before'], snapshot['after']
        role = {key: json.loads(after['revision']['record_json'])[key] for key in ROLE_FIELDS}
        if snapshot['format'] != 'role-assignment-v1' or _patched(store, before, role) != after:
            raise ValueError('role_repair_invalid_backup')
    except (KeyError, TypeError):
        raise ValueError('role_repair_invalid_backup') from None
    restored = deepcopy(before)
    restored['asset']['generation'] = after['asset']['generation'] + 1
    with store._connection(write=execute) as connection:
        if execute:
            _locked_state(store, connection, after)
            _write(store, connection, after, restored)
        else:
            if _state(store, connection, after['asset']['knowledge_id'], after['revision']['revision']) != after:
                raise ValueError('role_repair_snapshot_changed')
            _completed(store, connection, after)
    return {'status': 'rolled_back' if execute else 'preview', 'knowledge_id': after['asset']['knowledge_id'],
            'revision': after['revision']['revision'], 'generation': restored['asset']['generation']}
