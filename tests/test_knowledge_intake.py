import csv
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

import knowledge
from rag_app import knowledge_intake as intake
from rag_app.knowledge_judge import WINDOW_CHARS, judge_batch
from rag_app.knowledge_service import KnowledgeService
from rag_app.knowledge_store import KnowledgeStore


pytestmark = pytest.mark.offline


def archive(tmp_path, label="first", body=None):
    path = tmp_path / (label + ".zip")
    text = body or "# 复盘方法\n\n## 输入\n保留证据。\n\n## 步骤\n先检查失败原因，不要宣称已经成功。\n"
    with zipfile.ZipFile(path, "w") as output:
        output.writestr(label + "/知识正文/KA-001.md", text)
        output.writestr(label + "/附件/one/SKILL.md", "# 一个技能\n\n执行前检查输入。\n参考 [资料](references/check.md)。\n")
        output.writestr(label + "/附件/one/references/check.md", "# 核验\n电话 13900000000，失败就停止。\n")
        output.writestr(label + "/附件/other/SKILL.md", "# 另一个技能\n用途不同，需要独立保留。\n")
        output.writestr(label + "/附件/run.py", "raise RuntimeError('DO_NOT_EXECUTE')")
        output.writestr(label + "/.env", "SECRET_MUST_NOT_READ")
        output.writestr(label + "/附件/history.txt", "历史附件需要登记归属。")
    return {"path": path, "source_id": "source-a", "package": path.name,
            "sha256": intake.file_hash(path), "contributor": "未确认"}


def judgment(text, title, model):
    return {"decision": "candidate", "reasons": ["有可复用检查点"],
            "evidence_anchors": [{"quote": text[:min(30, len(text))], "location": "本段"}],
            "checks": dict.fromkeys(["reuse", "completeness", "evidence", "sharing"], "待核验")}


def test_intake_covers_skills_references_full_bodies_and_resume(tmp_path, monkeypatch):
    source = archive(tmp_path)
    original = source["path"].read_bytes()
    out = tmp_path / "result"
    batch = intake.process_batch([source], out)
    assert batch["summary"]["asset_count"] == 3
    assert batch["summary"]["unassigned_files"] == 1
    assert batch["summary"]["published"] is False
    skill = next(asset for asset in batch["assets"] if "/one/" in asset["source_path"])
    body = (out / skill["body_file"]).read_text()
    assert "失败就停止" in body and "13900000000" not in body
    assert len(skill["members"]) == 2
    chunks = [json.loads(line) for line in (out / skill["chunk_file"]).read_text().splitlines()]
    covered = set()
    for chunk in chunks:
        assert chunk["source_text"] == chunk["prefix"] + body[chunk["start_offset"]:chunk["end_offset"]]
        covered.update(range(chunk["start_offset"], chunk["end_offset"]))
    offset = 0
    for line in body.splitlines(keepends=True):
        if line.strip() and not re.fullmatch(r"[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*", line.strip()):
            assert set(range(offset, offset + len(line.rstrip()))) <= covered
        offset += len(line)
    assert skill["metadata"]["chunking_version"] == "structure-v2"
    assert all(chunk["chunking_version"] == "structure-v2" for chunk in chunks)
    all_generated = "".join(file.read_text() for file in out.rglob("*") if file.is_file())
    assert "SECRET_MUST_NOT_READ" not in all_generated and "DO_NOT_EXECUTE" not in all_generated
    assert source["path"].read_bytes() == original
    monkeypatch.setattr(intake, "parse_text_file", lambda *_: pytest.fail("resume should reuse successful parsing"))
    resumed = intake.process_batch([source], out, resume=True)
    assert resumed == batch
    with pytest.raises(FileExistsError):
        intake.process_batch([source], out)
    with pytest.raises(ValueError, match="identical"):
        intake.process_batch([{**source, "source_id": "changed"}], out, resume=True)


def test_duplicate_source_context_and_different_versions_are_not_overwritten(tmp_path):
    a, b, c = archive(tmp_path, "a"), archive(tmp_path, "b"), archive(tmp_path, "c", "# 修订\n改了条件。")
    result = intake.process_batch([a, b, c], tmp_path / "result")
    bodies = [asset for asset in result["assets"] if "知识正文" in asset["source_path"]]
    assert len(bodies) == 2
    assert len({asset["knowledge_id"] for asset in bodies}) == 1
    assert bodies[0]["duplicate_sources"][0]["package"] == "b.zip"
    assert all(any(issue["code"] == "same_asset_different_version" for issue in asset["issues"]) for asset in bodies)


def test_invalid_archive_is_isolated_and_review_requires_real_hash_and_quote(tmp_path):
    source = archive(tmp_path)
    bad = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bad, "w") as output:
        output.writestr("../outside.md", "cannot escape")
    result = intake.process_batch([source, {**source, "path": bad, "sha256": intake.file_hash(bad), "package": bad.name}], tmp_path / "result",
                                  reviews=[{"source_package": source["package"], "source_path": "first/知识正文/KA-001.md",
                                            "source_sha256": "wrong", "decision": "candidate", "reasons": ["AI声称正确"],
                                            "evidence_anchors": [{"quote": "编造原文"}]}])
    assert result["summary"]["asset_count"] == 3
    assert result["packages"][1]["issues"][0]["code"] == "archive_failed"
    assert not (tmp_path / "outside.md").exists()
    asset = next(asset for asset in result["assets"] if "知识正文" in asset["source_path"])
    assert asset["decision"] == "review_required"
    assert {issue["code"] for issue in asset["issues"]} >= {"review_evidence_mismatch"}


def test_full_model_coverage_draft_bridge_and_retry_cache(tmp_path):
    source = archive(tmp_path, body="# 长案例\n" + "失败原因和限制。" * 2000 + "最后纠正：并未验证。")
    out = tmp_path / "result"
    intake.process_batch([source], out)
    calls = []
    def caller(text, title, model):
        calls.append(text)
        return judgment(text, title, model)
    result = judge_batch(out, execute=True, caller=caller, model="offline-test")
    assert result["complete"] == 3
    assert any("最后纠正：并未验证。" in call for call in calls)
    assert all(len(call) <= WINDOW_CHARS for call in calls)
    judge_batch(out, execute=True, caller=lambda *_: pytest.fail("cached judgments must be reused"), model="offline-test")
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    service = KnowledgeService(store=store)
    preview = intake.import_batch(out, service)
    assert len(preview["results"]) == 3 and not store.path.exists()
    imported = intake.import_batch(out, service, execute=True)
    assert len(imported["results"]) == 3 and not store.published_revisions()
    for asset in store.list_assets():
        assert store.snapshot(asset["knowledge_id"])["chunking_version"] == "structure-v2"
    reviews = json.loads((out / "model_reviews.json").read_text())
    reviews["assets"][0]["windows"] = reviews["assets"][0]["windows"][:1]
    intake.write_json(out / "model_reviews.json", reviews)
    again = intake.import_batch(out, service)
    assert again["results"][0]["status"] == "failed"


def test_model_failure_or_invented_quote_never_becomes_useless_or_published(tmp_path):
    out = tmp_path / "result"
    intake.process_batch([archive(tmp_path)], out)
    calls = []
    def broken(*args):
        calls.append(args)
        raise RuntimeError("HTTP 401 credential-sentinel")
    result = judge_batch(out, execute=True, caller=broken, model="offline-test")
    assert result["failed"] == 3 and len(calls) == 1
    text = (out / "model_reviews.json").read_text()
    assert "credential-sentinel" not in text
    assert all(asset["decision"] == "review_required" for asset in json.loads(text)["assets"])
    result = judge_batch(out, execute=True, caller=judgment, model="offline-test")
    assert result["complete"] == 3


def test_cli_intake_and_judge_preview_do_not_construct_storage(tmp_path, monkeypatch, capsys):
    source = archive(tmp_path)
    manifest = tmp_path / "input.json"
    manifest.write_text(json.dumps({"sources": [{"path": str(source["path"]), "source_id": "source-a"}]}))
    import rag_app.knowledge_service as service_module
    monkeypatch.setattr(service_module, "KnowledgeService", lambda: pytest.fail("local intake must not initialize the service"))
    out = tmp_path / "result"
    assert knowledge.main(["intake", "--manifest", str(manifest), "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["asset_count"] == 3
    assert knowledge.main(["judge-batch", "--batch", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["will_call_model"] is False


def test_package_root_references_and_explicit_legacy_encoding(tmp_path):
    path = tmp_path / "legacy.zip"
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("交付/知识正文/方法.md", "# 方法\n附件见 `附件/KA-1/旧文本.txt`，工具为 `附件/KA-1/工具.py`。")
        output.writestr("交付/附件/KA-1/旧文本.txt", "先核对失败原因。".encode("gb18030"))
        output.writestr("交付/附件/KA-1/工具.py", "NEVER_RUN")
    source = {"path": path, "package": path.name, "source_id": "source-a", "sha256": intake.file_hash(path), "contributor": "未确认",
              "text_encodings": {"交付/附件/KA-1/旧文本.txt": "gb18030"}}
    out = tmp_path / "out"
    batch = intake.process_batch([source], out)
    asset = batch["assets"][0]
    assert "先核对失败原因。" in (out / asset["body_file"]).read_text()
    codes = {issue["code"] for issue in asset["issues"]}
    assert "explicit_encoding" in codes and "script_dependency_not_provided" in codes
    assert "unresolved_reference" not in codes
    assert asset["metadata"]["evidence"]["delivery_scope"] == "method_reference_only"


def test_problematic_reference_prevents_main_document_candidate_auto_import(tmp_path):
    source = archive(tmp_path)
    main = "first/附件/one/SKILL.md"
    reference = "first/附件/one/references/check.md"
    reviews = []
    with zipfile.ZipFile(source["path"]) as package:
        for name, decision, quote in [(main, "candidate", "执行前检查输入。"), (reference, "repair", "失败就停止。")]:
            reviews.append({"source_package": source["package"], "source_path": name, "source_sha256": intake.sha(package.read(name)),
                            "decision": decision, "reasons": ["示例检查"], "evidence_anchors": [{"quote": quote}]})
    result = intake.process_batch([source], tmp_path / "out", reviews=reviews)
    asset = next(asset for asset in result["assets"] if asset["source_path"] == main)
    assert asset["decision"] == "repair"
    assert asset["review"]["assessed_members"] == asset["review"]["total_members"] == 2
    assert any(issue["code"] == "dependency_review_requires_curation" for issue in asset["issues"])


def simple_archive(tmp_path, members):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as output:
        for name, text in members.items():
            output.writestr(name, text)
    return {"path": path, "source_id": "source-a", "package": path.name,
            "sha256": intake.file_hash(path), "contributor": "未确认"}


@pytest.mark.parametrize("second_body", ["# 方法\n相同正文。", "# 方法\n不同正文。"])
def test_meaningful_directories_are_distinct_assets_not_delivery_wrappers(tmp_path, second_body):
    source = simple_archive(tmp_path, {"DeptA/method.md": "# 方法\n相同正文。", "DeptB/method.md": second_body})
    batch = intake.process_batch([source], tmp_path / "result")
    assert batch["summary"]["file_count"] == batch["summary"]["asset_count"] == 2
    assert len({asset["knowledge_id"] for asset in batch["assets"]}) == 2
    assert all("duplicate_sources" not in asset for asset in batch["assets"])
    assert not any(issue["code"] == "same_asset_different_version"
                   for asset in batch["assets"] for issue in asset["issues"])


def test_review_quote_that_cleans_to_empty_cannot_select_candidate(tmp_path):
    text = "# 方法\n正常正文。"
    source = simple_archive(tmp_path, {"asset.md": text})
    review = {"source_package": source["package"], "source_path": "asset.md",
              "source_sha256": intake.sha(text), "decision": "candidate", "reasons": ["已核验"],
              "evidence_anchors": [{"quote": "\ufeff"}]}
    batch = intake.process_batch([source], tmp_path / "result", reviews=[review])
    asset = batch["assets"][0]
    assert asset["decision"] == "review_required" and "review" not in asset
    assert "review_evidence_mismatch" in {issue["code"] for issue in asset["issues"]}


def test_truncated_source_remains_repair_required_even_with_candidate_review(tmp_path):
    text = "完整正文" + "甲" * 500010
    source = simple_archive(tmp_path, {"asset.md": text})
    review = {"source_package": source["package"], "source_path": "asset.md",
              "source_sha256": intake.sha(text), "decision": "candidate", "reasons": ["有复用价值"],
              "evidence_anchors": [{"quote": "完整正文"}]}
    out = tmp_path / "result"
    batch = intake.process_batch([source], out, reviews=[review])
    asset = batch["assets"][0]
    assert asset["decision"] == "candidate" and asset["state"] == "repair_required"
    assert len((out / asset["body_file"]).read_text()) == 500000 < len(text)
    metadata = json.loads((out / asset["metadata_file"]).read_text())
    assert metadata["evidence"]["source_complete"] is False
    assert "text_character_limit" in metadata["evidence"]["processing_issues"]
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    result = intake.import_batch(out, KnowledgeService(store=store), execute=True)
    assert result["results"] == [] and len(result["skipped"]) == 1
    assert not store.path.exists()


def test_model_call_redacts_filename_title_and_reuses_clean_title_cache(tmp_path):
    address = "privacy-test@example.invalid"
    source = simple_archive(tmp_path, {address + ".txt": "正文没有标题。"})
    out = tmp_path / "result"
    intake.process_batch([source], out)
    sent_titles = []
    def caller(text, title, model):
        sent_titles.append(title)
        return judgment(text, title, model)
    result = judge_batch(out, execute=True, caller=caller, model="offline-test")
    assert result["complete"] == 1 and len(sent_titles) == 1
    assert address not in sent_titles[0] and "[已隐藏:email:" in sent_titles[0]
    judge_batch(out, execute=True, caller=lambda *_: pytest.fail("clean title cache must be reused"), model="offline-test")


def test_rejected_archive_still_inventories_every_member(tmp_path):
    members = {"../outside.md": "拒收正文。", "good.md": "正常正文。"}
    source = simple_archive(tmp_path, members)
    original = source["path"].read_bytes()
    batch = intake.process_batch([source], tmp_path / "result")
    report = batch["packages"][0]
    assert batch["summary"]["file_count"] == report["declared_files"] == 2
    assert {file["name"] for file in report["files"]} == set(members)
    assert all(file["state"] == "archive_rejected" for file in report["files"])
    assert batch["assets"] == [] and not (tmp_path / "outside.md").exists()
    assert source["path"].read_bytes() == original


def test_oversized_composed_asset_preserves_full_body_and_blocks_draft_import(tmp_path):
    root = "delivery/附件/example/"
    text = "# 技能\n" + "甲" * 260000 + "\n\n参考 [资料](references/details.md)。\n"
    reference = "# 参考\n" + "乙" * 260000
    source = simple_archive(tmp_path, {root + "SKILL.md": text, root + "references/details.md": reference})
    original = source["path"].read_bytes()
    review = {"source_package": source["package"], "source_path": root + "SKILL.md",
              "source_sha256": intake.sha(text), "decision": "candidate", "reasons": ["有复用价值"],
              "evidence_anchors": [{"quote": "# 技能"}]}
    out = tmp_path / "result"
    batch = intake.process_batch([source], out, reviews=[review])
    assert batch["summary"]["asset_count"] == 1
    asset = batch["assets"][0]
    body = (out / asset["body_file"]).read_text()
    assert len(body) > 500000 and text in body and reference in body
    assert len(asset["members"]) == 2 and asset["decision"] == "candidate"
    assert asset["state"] == "repair_required" and asset["chunk_count"] == 0
    assert {issue["code"] for issue in asset["issues"]} >= {"asset_too_large", "draft_validation_failed"}
    metadata = json.loads((out / asset["metadata_file"]).read_text())
    assert metadata["evidence"]["source_complete"] is False
    assert "asset_too_large" in metadata["evidence"]["processing_issues"]
    assert (out / asset["chunk_file"]).read_text() == ""
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    result = intake.import_batch(out, KnowledgeService(store=store), execute=True)
    assert result["results"] == [] and len(result["skipped"]) == 1
    assert not store.path.exists() and source["path"].read_bytes() == original


def test_skill_method_cases_and_commentary_have_lossless_review_boundaries(tmp_path):
    root = "delivery/附件/example/"
    skill = "# 方法\n先确认输入。\n[案例](references/cases.md) [流程](references/method.md)\n`tools/missing.py`\n"
    method = "# 行业数据与榜单源\n来源未核验。\n"
    library = ("# 视频文案案例库\n材料只供方法参考。\n\n## 阅读说明\n结构还原不是原片台词。\n\n"
               "## 全库索引\n|编号|索引|数值|\n|---|---|---|\n|A01|甲|123|\n|A02|乙独有索引|777777|\n\n"
               "## 2025 类别\n年度概况保留。\n\n#### A01 案例甲\n甲的正文。\n```markdown\n"
               "#### A99 代码内的伪案例\n```\n##### 子步骤\n保留完整步骤。\n\n"
               "### 年度分析\n这段不能吞进案例甲。\n\n#### A02 案例乙\n乙独有正文及数值888888。\n\n"
               "## 总结\n归因仍待验证。\n")
    source = simple_archive(tmp_path, {root + "SKILL.md": skill, root + "references/method.md": method,
                                       root + "references/cases.md": library})
    original = source["path"].read_bytes()
    reviews = [{"source_package": source["package"], "source_path": root + name,
                "source_sha256": intake.sha(text), "decision": "candidate", "reasons": ["旧文件级建议"],
                "evidence_anchors": [{"quote": quote}]} for name, text, quote in
               [("SKILL.md", skill, "先确认输入。"), ("references/method.md", method, "来源未核验。"),
                ("references/cases.md", library, "甲的正文。")]]
    out = tmp_path / "out"
    batch = intake.process_batch([source], out, reviews=reviews)
    assert batch["summary"]["primary_asset_count"] == 1
    assert batch["summary"]["reference_overview_count"] == 1
    assert batch["summary"]["derived_case_count"] == 2
    parent = next(a for a in batch["assets"] if a["source_path"].endswith("SKILL.md"))
    overview = next(a for a in batch["assets"] if a.get("asset_role") == "reference_overview")
    cases = [a for a in batch["assets"] if a.get("asset_role") == "reference_case"]
    assert parent["decision"] == "candidate" and parent["state"] == "draft_ready"
    assert len(parent["members"]) == 2
    assert method in (out / parent["body_file"]).read_text()
    assert "甲的正文" not in (out / parent["body_file"]).read_text()
    assert library in (out / parent["full_bundle_file"]).read_text()
    assert (out / overview["full_bundle_file"]).read_text() == library
    assert all(a["decision"] == "review_required" and "review" not in a for a in [overview, *cases])
    assert {a["source_fragment"]["case_number"] for a in cases} == {"A01", "A02"}
    assert len({a["knowledge_id"] for a in batch["assets"]}) == 4
    case_one = next(a for a in cases if a["source_fragment"]["case_number"] == "A01")
    body = (out / case_one["body_file"]).read_text()
    assert "结构还原不是原片台词" in body and "2025 类别" in body
    assert "代码内的伪案例" in body and "保留完整步骤" in body
    assert not any(other in body for other in ["乙独有", "777777", "888888", "这段不能吞进案例甲"])
    commentary = (out / overview["body_file"]).read_text()
    assert all(fragment in commentary for fragment in ["全库索引", "年度概况保留", "年度分析", "归因仍待验证"])
    assert "甲的正文" not in commentary and "乙独有正文" not in commentary
    covered = set()
    for asset in [overview, *cases]:
        body = (out / asset["body_file"]).read_text()
        for member in asset["members"]:
            start, end = member["source_start_offset"], member["source_end_offset"]
            assert member["offset_basis"] == "cleaned_source_text"
            assert body[member["start_offset"]:member["end_offset"]] == library[start:end]
            covered.update(range(start, end))
    assert covered == set(range(len(library)))
    normalized = intake.normalize_entry({"knowledge_id": parent["knowledge_id"],
                                         "content": (out / parent["body_file"]).read_text(), **parent["metadata"]})
    relation = normalized["evidence"]["related_assets"][0]
    assert relation["knowledge_id"] == overview["knowledge_id"]
    assert relation["source_locator"].endswith("references/cases.md") and relation["published"] is False
    assert {a["knowledge_id"] for a in cases} <= {r["knowledge_id"] for r in overview["metadata"]["evidence"]["related_assets"]}
    assert not any(issue["code"] == "unresolved_reference" for issue in parent["issues"])
    assert intake.process_batch([source], out, reviews=reviews, resume=True) == batch
    assert source["path"].read_bytes() == original


@pytest.mark.parametrize("headings", ["#### 01 甲\n甲。\n#### 01 乙\n乙。\n", "## 无编号案例\n边界不明。\n"])
def test_ambiguous_case_library_stays_whole_and_unreviewed(tmp_path, headings):
    root = "delivery/附件/example/"
    library = "# 案例库\n说明。\n" + headings
    source = simple_archive(tmp_path, {root + "SKILL.md": "# 方法\n[库](references/cases.md)",
                                       root + "references/cases.md": library})
    out = tmp_path / "out"
    batch = intake.process_batch([source], out)
    overview = next(a for a in batch["assets"] if a.get("asset_role") == "reference_overview")
    assert batch["summary"]["derived_case_count"] == 0
    assert (out / overview["body_file"]).read_text() == library
    assert overview["decision"] == "review_required"
    assert "case_boundary_requires_review" in {issue["code"] for issue in overview["issues"]}


def test_case_size_guard_is_recomputed_after_explicit_boundaries():
    root = "delivery/附件/example/"
    texts = {root + "SKILL.md": "# 方法\n参考案例库。\n",
             root + "references/cases.md": "# 案例库\n\n## 01 案例甲\n" + "甲" * 260000 + "\n## 02 案例乙\n" + "乙" * 260000}
    documents = {name: {"text": text, "sha256": intake.sha(text), "redactions": {}, "issues": [], "locations": [],
                        "item": {"name": name, "state": "parsed", "disposition": "supporting_material"}}
                 for name, text in texts.items()}
    report = {"files": [doc["item"] for doc in documents.values()]}
    assets = intake._prepare_assets({"source_id": "source-a", "package": "sample.zip", "contributor": "未确认"}, report, documents)
    assert len(assets) == 4
    assert all(len(a["content"]) < 500000 for a in assets)
    assert all(a["metadata"]["evidence"]["source_complete"] for a in assets)
    assert not any(issue["code"] == "asset_too_large" for a in assets for issue in a["issues"])
    assert max(len(a.get("full_bundle_content", "")) for a in assets) > 500000


def source_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(intake.SOURCE_COLUMNS)
    writer.writerows(rows)
    return "\ufeff" + stream.getvalue()


def source_row(key="KA-001", kind="Skill方法", state="交付", path="知识正文/KA-001.md", target=""):
    return [key, "可复用模板", kind, state, '多人复核，保留"异常"\n第二行', path,
            "WorkBuddy：C:\\Users\\example\\private.md", "执行未验证", "仅分享方法，未批准发布", target]


def test_source_v2_routes_only_deliveries_preserves_body_and_department(tmp_path):
    rows, members = [], {}
    for number, kind in enumerate(intake.SOURCE_TYPES, 1):
        key = f"KA-{number:03d}"
        path = f"知识正文/{key}.md"
        rows.append(source_row(key, kind, path=path.replace("/", "\\")))
        members["包/" + path] = "# 原有固定头\n来源限制：未验证。\n\n## 原文正文\n保留全部步骤与例外。\n"
    rows.extend([source_row("KA-101", state="待补", path=""),
                 source_row("KA-102", state="排除", path=""),
                 source_row("KA-103", state="合并", path="", target="KA-001")])
    members["包/交付清单.csv"] = source_csv(rows)
    members["包/知识正文/KA-999.md"] = "# 未登记\n不能暗入库。"
    members["包/说明.md"] = "# 过程说明\n不能当知识正文。"
    source = simple_archive(tmp_path, members)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [{"path": str(source["path"]), "source_id": "employee-a",
                                                "department": "人事"}]}))
    sources = intake.read_sources(manifest)
    out = tmp_path / "result"
    batch = intake.process_batch(sources, out)
    assert len(batch["assets"]) == len(intake.SOURCE_TYPES)
    report = batch["packages"][0]
    assert len(report["source_records"]) == len(rows)
    assert "source_manifest_unregistered_body" in {issue["code"] for issue in report["issues"]}
    for asset in batch["assets"]:
        meta = asset["metadata"]
        assert meta["department"] == "人事" and meta["title"] == "可复用模板"
        assert meta["kind"] == intake.SOURCE_TYPES[meta["evidence"]["source_type"]]
        assert asset["decision"] == "review_required" and asset["published"] is False
        assert meta["evidence"]["source_approval"] is False
        assert meta["evidence"]["source_purpose"] == rows[0][4]
        assert meta["evidence"]["source_evidence_status"] == "执行未验证"
        assert "C:\\Users" not in json.dumps(meta, ensure_ascii=False)
        assert (out / asset["body_file"]).read_text() == members[asset["source_path"]]
    skill = next(a for a in batch["assets"] if a["metadata"]["evidence"]["source_type"] == "Skill方法")
    assert skill["metadata"]["kind"] == "method"
    assert skill["metadata"]["evidence"]["delivery_scope"] == "method_reference_only"
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["sources"][0]["source_protocol"] == intake.SOURCE_PROTOCOL
    assert intake.process_batch(sources, out, resume=True) == batch
    before = (out / "batch.json").read_bytes()
    with pytest.raises(FileExistsError):
        intake.process_batch(sources, out)
    assert (out / "batch.json").read_bytes() == before
    receipt["sources"][0].pop("source_protocol")
    intake.write_json(out / "receipt.json", receipt)
    with pytest.raises(ValueError, match="identical"):
        intake.process_batch(sources, out, resume=True)
    assert (out / "batch.json").read_bytes() == before


@pytest.mark.parametrize("rows,code", [
    ([source_row(path="../知识正文/KA-001.md")], "source_manifest_unsafe_path"),
    ([source_row(path="C:\\private.md")], "source_manifest_unsafe_path"),
    ([source_row(path="知识正文/missing.md")], "source_manifest_body_unavailable"),
    ([source_row(kind="不认识的类型")], "source_manifest_invalid_row"),
    ([source_row(), source_row()], "source_manifest_invalid_row"),
    ([source_row(), source_row("KA-002")], "source_manifest_duplicate_body"),
    ([source_row(state="待补")], "source_manifest_nondelivery_body"),
    ([source_row(state="合并", path="", target="missing")], "source_manifest_invalid_merge"),
    ([source_row(state="合并", path="", target="KA-001")], "source_manifest_invalid_merge"),
    ([source_row(state="合并", path="", target="KA-002"),
      source_row("KA-002", state="合并", path="", target="KA-001")], "source_manifest_invalid_merge"),
])
def test_source_v2_invalid_manifest_fails_closed_without_generic_fallback(tmp_path, rows, code):
    source = simple_archive(tmp_path, {"交付清单.csv": source_csv(rows),
                                      "知识正文/KA-001.md": "# 完整材料\n不能因清单失败而暗入库。"})
    batch = intake.process_batch([source], tmp_path / "result")
    assert batch["assets"] == []
    assert code in {issue["code"] for issue in batch["packages"][0]["issues"]}


@pytest.mark.parametrize("content", ["名称,状态\n甲,交付\n", source_csv([source_row()[:-1]]),
                                     source_csv([]) + '"unterminated'])
def test_source_v2_strict_csv_and_bad_header_never_fall_back(tmp_path, content):
    source = simple_archive(tmp_path, {"交付清单.csv": content, "知识正文/KA-001.md": "# 正文"})
    batch = intake.process_batch([source], tmp_path / "result")
    assert batch["assets"] == []
    assert "source_manifest_invalid" in {issue["code"] for issue in batch["packages"][0]["issues"]}


def test_source_v2_unregistered_link_is_not_appended_and_identity_survives_rename(tmp_path):
    body = "# 头部与原文都保留\n参考[未登记正文](extra.md)。"
    source = simple_archive(tmp_path, {"交付清单.csv": source_csv([source_row()]),
                                      "知识正文/KA-001.md": body, "知识正文/extra.md": "不能自动合并。"})
    first = intake.process_batch([source], tmp_path / "first")["assets"][0]
    assert (tmp_path / "first" / first["body_file"]).read_text() == body
    assert "unresolved_reference" in {issue["code"] for issue in first["issues"]}
    renamed = simple_archive(tmp_path, {"交付清单.csv": source_csv([source_row(path="知识正文/renamed.md")]),
                                       "知识正文/renamed.md": body})
    second = intake.process_batch([renamed], tmp_path / "second")["assets"][0]
    assert first["knowledge_id"] == second["knowledge_id"]


@pytest.mark.parametrize("extra,body,code", [
    ({}, b"\xffnot-utf8", "source_manifest_body_unavailable"),
    ({"other/交付清单.csv": source_csv([])}, "# 正文", "source_manifest_invalid"),
])
def test_source_v2_unparsed_body_and_ambiguous_manifest_are_not_deliveries(tmp_path, extra, body, code):
    source = simple_archive(tmp_path, {"交付清单.csv": source_csv([source_row()]),
                                      "知识正文/KA-001.md": body, **extra})
    batch = intake.process_batch([source], tmp_path / "result")
    assert batch["assets"] == []
    assert code in {issue["code"] for issue in batch["packages"][0]["issues"]}


@pytest.mark.parametrize("department", ["", " " * 2, 12, "字" * 201])
def test_source_department_is_explicit_and_validated(tmp_path, department):
    source = archive(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [{"path": str(source["path"]), "source_id": "a", "department": department}]}))
    with pytest.raises(ValueError, match="department"):
        intake.read_sources(manifest)


def test_reference_scanning_ignores_fenced_examples_but_retains_narrative(tmp_path):
    body = ("# 方法\n先确认输入。\n[真实资料](references/real.md) 和 `templates/real.md`。\n"
            "```powershell\nWrite-Output '[例子](x/y)'\n`references/fake.md`\n```\n"
            "   ~~~~markdown\n[例子](a/b)\n~~~\n[仍在代码内](c/d)\n~~~~~\n"
            "正文最后的[附件](references/last.md)。\n")
    assert intake._references(body) == ["references/last.md", "references/real.md", "templates/real.md"]
    source = simple_archive(tmp_path, {"交付清单.csv": source_csv([source_row()]), "知识正文/KA-001.md": body})
    out = tmp_path / "result"
    asset = intake.process_batch([source], out)["assets"][0]
    assert {issue["reference"] for issue in asset["issues"] if issue["code"] == "unresolved_reference"} == set(intake._references(body))
    assert (out / asset["body_file"]).read_text() == body


def test_inline_code_is_not_a_markdown_link_but_explicit_reference_paths_are_kept():
    text = ("坐标 `item.transform[4](x)/[5](y)`；"
            "再用 `[regex]::Replace($j,'u([0-9a-fA-F]{4})',{param($m)[char][int]('0x'+$m.Groups[1].Value)})`。\n"
            "双反引号 ``示例含 `反引号` 及 [示例](fake.md)``；"
            "真实[x](ref.md)，参考 `references/x.md` 和 ``templates/check.md``。")
    assert intake._references(text) == ["ref.md", "references/x.md", "templates/check.md"]


def reviewed_missing_method(tmp_path, *, review_changes=None, kind="Skill方法", legacy=False, extra=None):
    body = "# 方法\n先确认输入，缺少附件时仅参考正文方法。\n[框架](references/framework.md) 和 `templates/check.md`。\n"
    path = "SKILL.md" if legacy else "知识正文/KA-001.md"
    members = {path: body, **(extra or {})}
    if not legacy:
        members["交付清单.csv"] = source_csv([source_row(kind=kind)])
    source = simple_archive(tmp_path, members)
    review = {"source_package": source["package"], "source_path": path, "source_sha256": intake.sha(body),
              "decision": "candidate", "reasons": ["正文可独立作方法参考，不是完整 Skill"],
              "evidence_anchors": [{"quote": "缺少附件时仅参考正文方法"}],
              "accepted_missing_references": ["references/framework.md", "templates/check.md"]}
    review.update(review_changes or {})
    if review.get("accepted_missing_references") == "omit":
        review.pop("accepted_missing_references")
    out = tmp_path / "result"
    batch = intake.process_batch([source], out, reviews=[review])
    return out, batch


def test_explicit_review_can_import_incomplete_source_v2_method_as_reference_only(tmp_path):
    out, batch = reviewed_missing_method(tmp_path)
    asset = batch["assets"][0]
    expected = ["references/framework.md", "templates/check.md"]
    evidence = json.loads((out / asset["metadata_file"]).read_text())["evidence"]
    assert evidence["accepted_missing_references"] == expected and evidence["source_complete"] is False
    assert evidence["delivery_scope"] == "method_reference_only" and evidence["source_approval"] is False
    assert {issue["reference"] for issue in asset["issues"] if issue["code"] == "unresolved_reference"} == set(expected)
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    result = intake.import_batch(out, KnowledgeService(store=store), execute=True)
    assert result["skipped"] == [] and result["results"][0]["status"] == "draft"
    saved = store.snapshot(asset["knowledge_id"])
    assert saved["kind"] == "method" and saved["evidence"]["accepted_missing_references"] == expected
    assert saved["evidence"]["source_complete"] is False and not store.published_revisions()


@pytest.mark.parametrize("changes,kind,legacy", [
    ({"accepted_missing_references": "omit"}, "Skill方法", False),
    ({"accepted_missing_references": []}, "Skill方法", False),
    ({"accepted_missing_references": ["references/framework.md"]}, "Skill方法", False),
    ({"accepted_missing_references": ["references/framework.md", "templates/check.md", "extra.md"]}, "Skill方法", False),
    ({"accepted_missing_references": ["references/framework.md", "templates/check.md", "templates/check.md"]}, "Skill方法", False),
    ({"accepted_missing_references": ["references/framework.md", {"path": "templates/check.md"}]}, "Skill方法", False),
    ({"accepted_missing_references": ["references/framework.md", "/templates/check.md"]}, "Skill方法", False),
    ({"source_sha256": "wrong"}, "Skill方法", False),
    ({"evidence_anchors": [{"quote": "这句原文不存在"}]}, "Skill方法", False),
    ({"decision": "repair"}, "Skill方法", False),
    ({}, "方法", False),
    ({}, "模板", False),
    ({}, "Skill方法", True),
])
def test_missing_reference_exception_requires_exact_scope_and_valid_review(tmp_path, changes, kind, legacy):
    out, batch = reviewed_missing_method(tmp_path, review_changes=changes, kind=kind, legacy=legacy)
    assert "accepted_missing_references" not in batch["assets"][0]["metadata"]["evidence"]
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    result = intake.import_batch(out, KnowledgeService(store=store), execute=True)
    assert result["results"] == [] and len(result["skipped"]) == 1 and not store.path.exists()


@pytest.mark.parametrize("blocker", ["same_asset_different_version", "draft_validation_failed", "referenced_material_unparsed"])
def test_accepted_missing_references_do_not_bypass_other_blockers(tmp_path, blocker):
    out, batch = reviewed_missing_method(tmp_path)
    asset = batch["assets"][0]
    asset["issues"].append({"code": blocker})
    if blocker == "referenced_material_unparsed":
        asset["state"] = "repair_required"
    intake.write_json(out / "batch.json", batch)
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    result = intake.import_batch(out, KnowledgeService(store=store), execute=True)
    assert result["results"] == [] and len(result["skipped"]) == 1 and not store.path.exists()


@pytest.mark.parametrize("changed", ["source_complete", "accepted_missing_references", "source_type", "review_hash"])
def test_import_rechecks_missing_reference_scope_and_persisted_metadata(tmp_path, changed):
    out, batch = reviewed_missing_method(tmp_path)
    asset = batch["assets"][0]
    path = out / asset["metadata_file"]
    metadata = json.loads(path.read_text())
    if changed == "review_hash":
        asset["review"]["materials"][0]["source_sha256"] = "wrong"
    else:
        metadata["evidence"][changed] = {"source_complete": True, "accepted_missing_references": [], "source_type": "方法"}[changed]
        intake.write_json(path, metadata)
        asset["metadata_hash"] = intake.file_hash(path)
    intake.write_json(out / "batch.json", batch)
    store = KnowledgeStore(tmp_path / "db.sqlite3")
    result = intake.import_batch(out, KnowledgeService(store=store), execute=True)
    assert not any(row["status"] == "draft" for row in result["results"]) and not store.path.exists()
