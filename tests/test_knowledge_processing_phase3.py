"""Receipt-to-publication checks with no ambient credentials or network access."""
import csv
import hashlib
import io
import json
import uuid

import pytest

from rag_app.knowledge_intake import SOURCE_COLUMNS, SOURCE_GOVERNANCE_COLUMNS
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
    writer = csv.DictWriter(output, fieldnames=[*SOURCE_COLUMNS, *SOURCE_GOVERNANCE_COLUMNS])
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
    assert item['status'] == 'needs_review' and 'source_base_revision_required' in item['reason_codes']
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


def test_plain_chat_is_retained_for_review_when_automatic_publication_enabled(system):
    index = enable(system)
    submit(system)
    system.service.run_once()
    item = job(system)['items'][0]
    assert item['status'] == 'needs_review'
    assert 'structured_delivery_required' in item['reason_codes']
    assert not index.calls and system.catalogue.published_count() == 0


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
