"""Role directory and real receipt paths, with only temporary SQLite and fake OSS."""
import csv
import hashlib
import io
import json

import pytest

from rag_app.knowledge_intake import SOURCE_COLUMNS, SOURCE_GOVERNANCE_COLUMNS, SOURCE_UPLOADER_COLUMN, SOURCE_ROLE_COLUMNS
from rag_app.knowledge_processing_api import get_knowledge_files
from rag_app.knowledge_service import KnowledgeService
from rag_app.knowledge_store import KnowledgeStore
from test_knowledge_api import FakeIndex, entry
from test_knowledge_processing import system, submit, job, zip_bytes, BODY
from test_knowledge_processing_api import api, headers, BASE, READ, SELF
from test_knowledge_processing_phase3 import enable, intake, receipt_job
from test_knowledge_text import docx, paragraph, table

pytestmark = pytest.mark.offline


def sop_bytes(department="总经办", name="AI架构师"):
    return docx(paragraph("岗位标准作业程序", "Title") +
                table([["所属部门", department, "岗位名称", name]]) +
                paragraph("交付流程", "Heading1") + paragraph("先核对输入，再记录验证依据。"))


def role_package(role, *, asset="CASE-1", kind="案例", skill=None, body=BODY, **changes):
    row = dict(zip(SOURCE_COLUMNS, [asset, "交接实践", kind, "交付", "新人工作交接", "正文/知识.md",
                                    "项目交接记录", "流程已核对", "以当前项目条件为准", ""]))
    row.update({"基线修订": "", "共享范围": "全员", "使用对象": "岗位接替人", "输入": "项目需求",
                "输出": "验证记录", "依赖与权限": "授权项目访问", "适用边界": "内部交接使用",
                SOURCE_UPLOADER_COLUMN: "实际上传人的声明岗位", "role_id": role["role_id"],
                "department": role["department"], "role_name": role["name"]})
    row.update(changes)
    fields = [*SOURCE_COLUMNS, *SOURCE_GOVERNANCE_COLUMNS, SOURCE_UPLOADER_COLUMN, *SOURCE_ROLE_COLUMNS]
    files = {row["正文路径"]: body}
    if skill is not None:
        row["skill_package_path"] = "安装包/工作方法.zip"
        fields.append("skill_package_path")
        files[row["skill_package_path"]] = skill
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerow(row)
    return zip_bytes({"交付清单.csv": buffer.getvalue(), **files})


def publish(store, data):
    draft = store.import_draft(data)
    return store.publish(draft["knowledge_id"], draft["revision"], expected_generation=draft["generation"], confirmed_by="offline-test")


def test_sop_registers_stable_department_role_without_rewriting_legacy(tmp_path):
    store = KnowledgeStore(tmp_path / "roles.db")
    legacy = publish(store, entry())
    role = store.register_role("总经办", "AI架构师")
    assert store.register_role("总经办", "AI架构师") == role
    other = store.register_role("研发部", "AI架构师")
    assert role["role_id"] != other["role_id"]
    assert all(item["total"] == 0 and item["counts"] == {"sop": 0, "skill": 0, "other": 0} for item in store.roles())
    sop = publish(store, {**entry(), "knowledge_id": "sop", "kind": "process", "department": "总经办", "role_name": "AI架构师"})
    assert sop["role_id"] == role["role_id"]
    assert store.get_published(legacy["knowledge_id"])["payload_hash"] == legacy["payload_hash"]
    assert store.published_revisions(role_id="unknown") == [{"knowledge_id": legacy["knowledge_id"], "revision": 1}]
    with pytest.raises(ValueError, match="unknown_role_id"):
        store.import_draft({**entry(), "role_id": "role_" + "0" * 24, "department": "总经办", "role_name": "AI架构师"})
    with pytest.raises(ValueError, match="role_metadata_mismatch"):
        store.import_draft({**entry(), "role_id": role["role_id"], "department": "研发部", "role_name": "AI架构师"})


def test_search_filters_role_and_category_before_index_topk(tmp_path):
    store, index = KnowledgeStore(tmp_path / "roles.db"), FakeIndex()
    service = KnowledgeService(store, index)
    first = store.register_role("总经办", "AI架构师")
    second = store.register_role("研发部", "AI架构师")
    for key, role, kind in (("role-sop", first, "process"), ("role-case", first, "case"), ("other-sop", second, "process")):
        service.import_document({**entry(), "knowledge_id": key, "kind": kind, "role_id": role["role_id"],
                                 "department": role["department"], "role_name": role["name"]}, execute=True)
        service.publish(key, confirmed_by="offline-test", execute=True)
    results = service.search("工作", role_id=first["role_id"], category="sop", top_k=1)
    assert index.search_calls[-1][1] == [{"knowledge_id": "role-sop", "revision": 1}]
    assert [item["knowledge_id"] for item in results] == ["role-sop"]
    assert results[0]["role_id"] == first["role_id"] and results[0]["category"] == "sop"


def test_bare_docx_receipt_builds_role_publishes_and_binds_original_preview(system):
    index = enable(system)
    original = sop_bytes()
    receipt = submit(system, original, filename="岗位SOP.docx")
    assert system.service.run_once()
    result = job(system)
    assert result["status"] == "completed", result
    item = next(item for item in result["items"] if item["published"])
    record = system.catalogue.get_published(item["knowledge_id"])
    assert record["kind"] == "process" and record["role_name"] == "AI架构师"
    assert record["title"] == "总经办 AI架构师 岗位SOP"
    assert record["department"] == "总经办" and len(index.calls) == 1
    assert system.catalogue.roles()[0]["counts"] == {"sop": 1, "skill": 0, "other": 0}
    assert record["evidence"]["governance_receipt_id"] == receipt["receipt_id"]
    assert system.catalogue.published_uploader_position(record) is None
    assert {ref["purpose"] for ref in record["files"]} == {"original", "preview"}
    original_ref = next(ref for ref in record["files"] if ref["purpose"] == "original")
    assert original_ref["sha256"] == hashlib.sha256(original).hexdigest()
    assert system.bucket.data[original_ref["object_ref"]["object_key"]] == original
    public = KnowledgeService(system.catalogue, index).get(item["knowledge_id"])
    assert "object_ref" not in json.dumps(public["files"])


def test_sop_title_prefers_document_metadata_and_never_first_numbered_section():
    from rag_app.knowledge_intake import _sop_title
    content = "AI架构师岗位标准作业程序\n# 1 文档基本信息\n## 2 工作流程"
    metadata = {"department": "总经办", "role_name": "AI架构师"}
    assert _sop_title(metadata, content, "source") == "总经办 AI架构师 岗位SOP"
    assert _sop_title({**metadata, "document_name": "AI系统交付标准作业程序"}, content, "source") == "AI系统交付标准作业程序"
    assert _sop_title({}, content, "source") == "AI架构师岗位标准作业程序"
    assert _sop_title({}, "# 1 文档基本信息", "岗位说明") == "岗位说明"


def test_source_manifest_binds_asset_role_without_replacing_uploader_and_unknown_is_pending(system):
    enable(system)
    role = system.catalogue.register_role("总经办", "AI架构师")
    receipt = intake(system, role_package(role))
    system.service.run_once()
    item = next(item for item in receipt_job(system, receipt)["items"] if item["published"])
    record = system.catalogue.get_published(item["knowledge_id"])
    assert record["role_id"] == role["role_id"]
    assert system.catalogue.published_uploader_position(record) == "实际上传人的声明岗位"
    missing = intake(system, role_package({**role, "role_id": "role_" + "0" * 24}, asset="CASE-2"))
    system.service.run_once()
    result = receipt_job(system, missing)
    assert result["counts"]["published"] == 0
    assert any("unknown_role_id" in item["reason_codes"] for item in result["items"])


def test_identical_material_for_different_roles_is_not_silently_deduplicated(system):
    enable(system)
    for department in ("总经办", "研发部"):
        role = system.catalogue.register_role(department, "AI架构师")
        receipt = intake(system, role_package(role), source="agent:" + role["role_id"])
        system.service.run_once()
        assert receipt_job(system, receipt)["counts"]["published"] == 1
    assert system.catalogue.published_count() == 2
    assert {item["department"] for item in system.catalogue.published_catalog()} == {"总经办", "研发部"}


def test_sop_department_in_document_must_match_explicit_manifest(system):
    enable(system)
    role = system.catalogue.register_role("总经办", "AI架构师")
    receipt = intake(system, role_package(role, kind="流程", body=sop_bytes("研发部"), **{"正文路径": "正文/SOP.docx"}))
    system.service.run_once()
    result = receipt_job(system, receipt)
    assert result["counts"]["published"] == 0
    assert any("role_metadata_mismatch" in item["reason_codes"] for item in result["items"])


def test_preview_storage_failure_never_publishes_partial_file_revision(system):
    from rag_app.knowledge_files import KnowledgeFiles
    enable(system)
    real = KnowledgeFiles(system.bucket)
    class Files:
        def put(self, data, filename, **options):
            if options["purpose"] == "preview":
                raise RuntimeError("offline simulated storage failure")
            return real.put(data, filename, **options)
    system.service.files = Files()
    submit(system, sop_bytes(), filename="岗位SOP.docx")
    system.service.run_once()
    assert system.catalogue.published_count() == 0
    assert system.catalogue.list_assets() == []
    assert job(system)["status"] != "completed"


def test_complete_skill_package_is_downloadable_but_execution_not_claimed(system):
    enable(system)
    role = system.catalogue.register_role("总经办", "AI架构师")
    skill = zip_bytes({"SKILL.md": "# 方法\n参见 [参考](references/help.md)。", "references/help.md": "核对输入。"})
    receipt = intake(system, role_package(role, kind="Skill方法", skill=skill))
    system.service.run_once()
    result = receipt_job(system, receipt)
    assert result["status"] == "completed", result
    record = system.catalogue.get_published(next(item["knowledge_id"] for item in result["items"] if item["published"]))
    assert any(item["purpose"] == "skill" and item["sha256"] == hashlib.sha256(skill).hexdigest() for item in record["files"])
    assert record["evidence"]["skill_package_validation"]["execution_verified"] is False


def test_roles_api_counts_filters_and_file_signing_require_current_public_version(api):
    client, _, catalog = api
    role = catalog.register_role("总经办", "AI架构师")
    assert client.get(BASE + "/roles", headers=headers(SELF)).status_code == 403
    assert client.get(BASE + "/roles", headers=headers(READ)).json()["items"][0]["total"] == 0
    from rag_app.knowledge_files import KnowledgeFiles
    from test_knowledge_processing import Bucket
    files = KnowledgeFiles(Bucket())
    ref = files.put(b"original", "sop.docx")
    record = publish(catalog, {**entry(), "kind": "process", "role_id": role["role_id"],
                              "department": role["department"], "role_name": role["name"], "files": [ref]})
    detail = client.get(BASE + "/catalog/decision-review", headers=headers(READ)).json()
    assert detail["role_id"] == role["role_id"] and "object_ref" not in json.dumps(detail)
    assert client.get(BASE + "/catalog", params={"role_id": role["role_id"], "category": "other"}, headers=headers(READ)).json()["total"] == 0
    class Signer:
        calls = []
        def signed_url(self, descriptor, **options):
            self.calls.append(descriptor)
            return {"url": "https://example.test/signed", "expires_in": 300}
    signer = Signer()
    client.app.dependency_overrides[get_knowledge_files] = lambda: signer
    path = BASE + "/catalog/decision-review/files/" + ref["file_id"]
    assert client.get(path, headers=headers(SELF)).status_code == 403
    assert client.get(path, params={"disposition": "inline"}, headers=headers(READ)).status_code == 422
    assert not signer.calls
    assert client.get(path, headers=headers(READ)).json()["filename"] == "sop.docx"
    catalog.withdraw(record["knowledge_id"])
    assert client.get(path, headers=headers(READ)).status_code == 404
    assert len(signer.calls) == 1


def test_reviewers_add_roles_only_inside_saved_departments_and_reuse_duplicates(api):
    from test_knowledge_processing_api import REVIEW, DASH
    client, _, catalog = api
    catalog.register_role('总经办', 'AI架构师')
    body = {'department': ' 总经办 ', 'name': ' 流程专员 '}
    assert client.post(BASE + '/roles', json=body).status_code == 401
    for permission in (SELF, READ, DASH):
        assert client.post(BASE + '/roles', headers=headers(permission), json=body).status_code == 403
    response = client.post(BASE + '/roles', headers=headers(REVIEW), json=body)
    assert response.status_code == 200
    role = response.json()
    assert set(role) == {'role_id', 'department', 'name', 'counts', 'total'}
    assert role['department'] == '总经办' and role['name'] == '流程专员'
    assert role['counts'] == {'sop': 0, 'skill': 0, 'other': 0} and role['total'] == 0
    publish(catalog, {**entry(), 'role_id': role['role_id'], 'department': role['department'],
                      'role_name': role['name'], 'kind': 'prompt'})
    repeated = client.post(BASE + '/roles', headers=headers(REVIEW), json=body).json()
    assert repeated['role_id'] == role['role_id'] and repeated['counts']['other'] == repeated['total'] == 1
    assert repeated in client.get(BASE + '/roles', headers=headers(READ)).json()['items']
    assert len(catalog.roles()) == 2
    missing = client.post(BASE + '/roles', headers=headers(REVIEW), json={**body, 'department': '不存在的部门'})
    assert missing.status_code == 404 and missing.json()['detail'] == 'role_department_not_found'
    assert len(catalog.roles()) == 2


@pytest.mark.parametrize('change', [
    {'department': ''}, {'department': '  '}, {'name': '  '}, {'name': '\n新岗位'},
    {'name': '新\t岗位'}, {'department': '总经办\x00'}, {'name': '\x7f'},
    {'name': '岗' * 201}, {'department': '部' * 201}, {'name': 123},
    {'role_id': 'role_' + '0' * 24}, {'principal': 'spoof'}, {'actor': 'spoof'},
])
def test_role_creation_rejects_invalid_fields_without_mutating_directory(api, change):
    from test_knowledge_processing_api import REVIEW
    client, _, catalog = api
    catalog.register_role('总经办', 'AI架构师')
    response = client.post(BASE + '/roles', headers=headers(REVIEW),
                           json={**{'department': '总经办', 'name': '流程专员'}, **change})
    assert response.status_code == 422 and response.json()['detail'] == 'invalid_processing_request'
    assert len(catalog.roles()) == 1


def test_published_preview_returns_verified_private_html_without_oss_navigation(api, monkeypatch):
    from rag_app.knowledge_files import KnowledgeFiles
    from test_knowledge_files import FileBucket
    client, _, catalog = api
    bucket = FileBucket()
    files = KnowledgeFiles(bucket)
    content = "<!doctype html><meta charset='utf-8'><body><table><tr><td>岗位流程</td></tr></table></body>"
    ref = files.put(content.encode(), "岗位SOP.html", purpose="preview")
    record = publish(catalog, {**entry(), "files": [ref]})
    client.app.dependency_overrides[get_knowledge_files] = lambda: files
    path = BASE + "/catalog/decision-review/files/" + ref["file_id"] + "?disposition=inline"
    assert client.get(path, headers=headers(SELF)).status_code == 403
    result = client.get(path, headers=headers(READ))
    assert result.status_code == 200
    assert result.json() == {"html": content, "expires_in": 300, "filename": ref["filename"], "media_type": "text/html"}
    assert result.headers["cache-control"] == "no-store"
    assert not bucket.signatures and len(bucket.writes) == 1
    key = ref["object_ref"]["object_key"]
    bucket.objects[key] = b"tampered preview"
    assert client.get(path, headers=headers(READ)).status_code == 503
    bucket.objects[key] = content.encode()
    bucket.acls[key] = "public-read"
    assert client.get(path, headers=headers(READ)).status_code == 503
    bucket.acls[key] = "private"
    get = files.get
    def withdraw_during_read(descriptor):
        data = get(descriptor)
        catalog.withdraw(record["knowledge_id"])
        return data
    monkeypatch.setattr(files, "get", withdraw_during_read)
    assert client.get(path, headers=headers(READ)).status_code == 404
    assert client.get(path, headers=headers(READ)).status_code == 404
    assert not bucket.signatures and len(bucket.writes) == 1
