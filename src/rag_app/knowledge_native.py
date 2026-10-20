"""Text intake using the existing receipt, catalogue, worker and search pipeline."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from typing import Literal, Optional
import zipfile

from pydantic import BaseModel, Field, StrictBool, field_validator

from .knowledge_intake import SOURCE_COLUMNS, SOURCE_GOVERNANCE_COLUMNS, SOURCE_UPLOADER_COLUMN, SOURCE_UPLOADER_DEPARTMENT_COLUMN
from .knowledge_processing import ProcessingStore
from .knowledge_receipts import ReceiptError, validate_principal


class TextSubmission(BaseModel):
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=50_000)
    position: str = Field(min_length=1, max_length=200)
    department: Optional[str] = Field(default=None, min_length=1, max_length=200)
    kind: Literal["方法", "流程", "模板", "提示词", "Skill方法", "案例", "规则", "参考"]
    source_locator: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=2000)
    evidence_status: str = Field(min_length=1, max_length=2000)
    audience: str = Field(min_length=1, max_length=1000)
    inputs: str = Field(min_length=1, max_length=2000)
    outputs: str = Field(min_length=1, max_length=2000)
    dependencies: str = Field(min_length=1, max_length=2000)
    boundaries: str = Field(min_length=1, max_length=2000)
    sharing_confirmed: StrictBool
    base_revision: Optional[int] = Field(default=None, ge=1, le=999_999_999, strict=True)
    model_config = {"extra": "forbid"}

    @field_validator("*")
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str):
            if not value.strip() or any(ord(c) < 32 and c not in "\t\r\n" for c in value):
                raise ValueError("invalid_submission_text")
            value.encode("utf-8")
        return value

    @field_validator("position")
    @classmethod
    def declared_position(cls, value):
        if value.strip() in {"未知", "待确认", "岗位", "填写自己的岗位"} or re.search(r"[【\[].*填写.*[】\]]", value):
            raise ValueError("position_required")
        return value.strip()

    @field_validator("department")
    @classmethod
    def declared_department(cls, value):
        if value is not None:
            if not value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
                raise ValueError("invalid_department")
            return value.strip()
        return value

    @field_validator("sharing_confirmed")
    @classmethod
    def shared(cls, value):
        if value is not True:
            raise ValueError("sharing_confirmation_required")
        return value



def source_package(request: TextSubmission) -> bytes:
    asset_id = "KA-" + hashlib.sha256(request.source_id.encode()).hexdigest()[:24]
    path = "知识正文/" + asset_id + ".md"
    row = dict(zip(SOURCE_COLUMNS, [asset_id, request.title, request.kind, "交付", request.purpose,
                                   path, request.source_locator, request.evidence_status,
                                   request.boundaries, ""]))
    row.update({"基线修订": str(request.base_revision or ""), "共享范围": "全员",
                "使用对象": request.audience, "输入": request.inputs, "输出": request.outputs,
                "依赖与权限": request.dependencies, "适用边界": request.boundaries,
                SOURCE_UPLOADER_COLUMN: request.position})
    manifest = io.StringIO(newline="")
    columns = [*SOURCE_COLUMNS, *SOURCE_GOVERNANCE_COLUMNS, SOURCE_UPLOADER_COLUMN]
    # An omitted optional field must preserve old package hashes and retry receipts.
    if request.department is not None:
        columns.append(SOURCE_UPLOADER_DEPARTMENT_COLUMN)
        row[SOURCE_UPLOADER_DEPARTMENT_COLUMN] = request.department
    writer = csv.DictWriter(manifest, fieldnames=columns)
    writer.writeheader()
    writer.writerow(row)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (("交付清单.csv", manifest.getvalue()), (path, request.content)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))
    return output.getvalue()



def submission_status(service, principal, receipt_id):
    result = service.status(principal, receipt_id)
    store = ProcessingStore(service.store.path, database_url=service.store.database_url)
    with store._connection() as connection:
        # Receipt-only installations retain their existing status behaviour.
        if store.database_url:
            available = connection.execute("SELECT to_regclass('knowledge_processing_jobs') AS name").fetchone()['name']
        else:
            available = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_processing_jobs'").fetchone()
        row = store._execute(connection, "SELECT job_id FROM knowledge_processing_jobs WHERE receipt_id=? AND principal=? ORDER BY created_at DESC,job_id DESC LIMIT 1",
                             (receipt_id, principal)).fetchone() if available else None
    result['processing'] = None
    if row is not None:
        job = store.get_job(row['job_id'], principal)
        processing = {key: job[key] for key in ('job_id', 'status', 'stage', 'error_code', 'counts')}
        processing['items'] = [{key: item.get(key) for key in
            ('item_id', 'status', 'knowledge_id', 'revision', 'published', 'reason_codes')}
            for item in job['items'] if item.get('knowledge_id') or item.get('reason_codes')]
        # A parser candidate can collide with another source's ID. Only an
        # imported revision is a knowledge reference for this receipt.
        confirmed_roles = {item['item_id']: item['role_assignment']['role']
            for item in job['items'] if item.get('role_assignment')}
        for item in processing['items']:
            if item['revision'] is None:
                item['knowledge_id'] = None
                role = confirmed_roles.get(item['item_id'])
                if role:
                    item.update(role_id=role['role_id'], department=role['department'], role_name=role['name'])
        # Only inspect revisions belonging to this authorized receipt. No body/OSS reads.
        with store._connection() as connection:
            for item in processing['items']:
                if item['knowledge_id'] and item['revision']:
                    record = store._execute(connection,
                        'SELECT record_json FROM knowledge_revisions WHERE knowledge_id=? AND revision=?',
                        (item['knowledge_id'], item['revision'])).fetchone()
                    if record is not None:
                        metadata = json.loads(record['record_json'])
                        item.update({key: metadata.get(key) for key in ('role_id', 'department', 'role_name')})
        result['processing'] = processing
        # Return current blockers from the whole job, independent of the first
        # processing page. Reading status never opens source documents or OSS.
        with store._connection() as connection:
            rows = store._execute(connection, "SELECT i.*,a.resume_pending,a.source_action,COUNT(*) OVER() AS repair_total,"
                + store._replacement_pending(connection) + " AS replacement_recheck FROM knowledge_processing_items i LEFT JOIN "
                + store._assignment_table(connection) + " a ON a.item_id=i.item_id WHERE i.job_id=? AND i.status='needs_review' "
                "ORDER BY COALESCE(a.resume_pending,0),replacement_recheck,i.source_path,i.item_id LIMIT 20", (row['job_id'],)).fetchall()
        items = []
        for pending in rows:
            public = store._public_item(pending)
            items.append({**{key: public[key] for key in ('item_id', 'version', 'status', 'missing_references')},
                'title': public['title'][:240], 'reason_codes': store._current_reason_codes(pending)})
        total = rows[0]['repair_total'] if rows else 0
        result['repair'] = {'items': items, 'items_total': total, 'truncated': total > len(items)}
        # The MCP intake transport has a 64 KB response ceiling. Keep totals and
        # signal truncation rather than losing the entire status on large packages.
        def oversized():
            return len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 60_000
        while len(items) > 1 and oversized():
            items.pop()
            result['repair']['truncated'] = True
        while items and items[0]['missing_references'] and oversized():
            items[0]['missing_references'].pop()
            result['repair']['truncated'] = True
        while processing['items'] and oversized():
            processing['items'].pop()
            result['repair']['truncated'] = True
    return result


def submit_text(service, principal, request: TextSubmission):
    validate_principal(principal)
    data = source_package(request)
    if len(data) > service.max_bytes:
        raise ReceiptError('upload_too_large', 413)
    digest = hashlib.sha256(data).hexdigest()
    row = service.store.prepare(principal, {
        'idempotency_key': 'native-' + digest, 'source_id': request.source_id,
        'filename': 'native-text.zip', 'byte_length': len(data), 'sha256': digest,
    }, ingestion_source='agent_collector')
    if row['status'] in {'awaiting_upload', 'failed'}:
        objects = service._objects()
        objects.assert_ready()
        try:
            objects.bucket.put_object(row['object_key'], data, headers={
                'Content-Type': 'application/octet-stream', 'x-oss-object-acl': 'private',
                'x-oss-forbid-overwrite': 'true', 'x-oss-meta-receipt-id': row['receipt_id'],
                'x-oss-meta-sha256': digest,
            })
        except Exception as exc:
            if getattr(exc, 'status', None) != 409 or getattr(exc, 'code', None) != 'FileAlreadyExists':
                raise ReceiptError('original_storage_unavailable') from None
        service.complete(principal, row['receipt_id'])
    return submission_status(service, principal, row['receipt_id'])
