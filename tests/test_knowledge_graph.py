from collections import Counter
import json

import pytest

from rag_app.knowledge_graph import KnowledgeGraph, _features, _related
from rag_app.knowledge_processing_api import _catalog_kind
from rag_app.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.offline


class Queued:
    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args):
        self.tasks.append((fn, args))

    def finish(self):
        tasks, self.tasks = self.tasks, []
        for fn, args in tasks:
            fn(*args)


def publish(store, key, content, **fields):
    draft = store.import_draft({"knowledge_id": key, "title": key, "content": content, "kind": "method",
                                "sources": [{"name": "package.zip", "locator": key + ".md"}], **fields})
    return store.publish(key, draft["revision"], expected_generation=draft["generation"], confirmed_by="test")


@pytest.fixture
def system(tmp_path, monkeypatch):
    catalog = KnowledgeStore(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    queue = Queued()
    now = [0]
    graph = KnowledgeGraph(executor=queue, clock=lambda: now[0])
    reads = Counter()
    original = KnowledgeStore.get_published

    def counted(store, kid):
        reads[(str(store.path), kid)] += 1
        return original(store, kid)

    monkeypatch.setattr(KnowledgeStore, "get_published", counted)
    return catalog, queue, graph, reads, now


def ready(catalog, queue, graph):
    first = graph.read(catalog, _catalog_kind)
    queue.finish()
    result = graph.read(catalog, _catalog_kind)
    assert result["status"] == "ready"
    return first, result


def test_graph_is_incremental_and_excerpts_are_original_text(system):
    catalog, queue, graph, reads, _ = system
    left = publish(catalog, "left", "招聘岗位画像。\n候选人面试能力与招聘验证。")
    right = publish(catalog, "right", "岗位画像需要招聘验证。\n候选人面试能力决定岗位画像。")
    first, result = ready(catalog, queue, graph)
    assert first["status"] == "building" and first["counts"]["indexed_assets"] == 0
    assert result["counts"]["indexed_assets"] == 2
    explicit = [edge for edge in result["edges"] if edge["relation"] == "reference"]
    related = [edge for edge in result["edges"] if edge["relation"] == "related"]
    assert not explicit
    assert len(related) == 1 and related[0]["directed"] is False and len(related[0]["terms"]) == 2
    originals = {"left": left, "right": right}
    for edge in result["edges"]:
        for evidence in edge["evidence"]:
            assert evidence["excerpt"] in originals[evidence["knowledge_id"]]["content"]
            assert evidence["revision"] == originals[evidence["knowledge_id"]]["revision"]
    for _ in range(4):
        assert graph.read(catalog, _catalog_kind)["edges"] == result["edges"]
    assert sum(reads.values()) == 2 and not queue.tasks
    publish(catalog, "third", "完全独立的餐饮配送物流路线。")
    assert graph.read(catalog, _catalog_kind)["status"] == "building"
    assert len(queue.tasks) == 1
    queue.finish()
    assert graph.read(catalog, _catalog_kind)["counts"]["indexed_assets"] == 3
    assert sum(reads.values()) == 3


def test_edges_are_cached_until_publications_or_completed_features_change(system, monkeypatch):
    catalog, queue, graph, _, _ = system
    from rag_app import knowledge_graph
    original = knowledge_graph._related
    calls = []
    def counted(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(knowledge_graph, "_related", counted)
    publish(catalog, "left", "招聘岗位画像及候选人面试。")
    publish(catalog, "right", "候选人面试及招聘岗位画像。")
    graph.read(catalog, _catalog_kind)
    for _ in range(3):
        graph.read(catalog, _catalog_kind)
    assert len(calls) == 1
    queue.finish()
    result = graph.read(catalog, _catalog_kind)
    for _ in range(3):
        assert graph.read(catalog, _catalog_kind)["edges"] == result["edges"]
    assert len(calls) == 2
    catalog.withdraw("right")
    assert not graph.read(catalog, _catalog_kind)["edges"]
    assert len(calls) == 3
    current = catalog.snapshot("right", 1)
    catalog.publish("right", 1, expected_generation=current["generation"], confirmed_by="test")
    assert graph.read(catalog, _catalog_kind)["counts"]["indexed_assets"] == 1
    assert len(calls) == 4
    queue.finish()
    assert graph.read(catalog, _catalog_kind)["counts"]["related_edges"] == 1
    assert len(calls) == 5


def test_edges_cache_remains_bounded_and_reports_truncation(system, monkeypatch):
    catalog, queue, graph, _, _ = system
    monkeypatch.setattr("rag_app.knowledge_graph.MAX_EDGES", 1)
    publish(catalog, "origin", "[[target]] [[other]]")
    publish(catalog, "target", "目标正文。")
    publish(catalog, "other", "另一正文。")
    graph.read(catalog, _catalog_kind)
    queue.finish()
    result = graph.read(catalog, _catalog_kind)
    assert result["truncated"] and result["status"] == "partial" and len(result["edges"]) == 1
    assert len(next(iter(graph.states.values()))["edges"]) == 1
    assert graph.read(catalog, _catalog_kind)["edges"] == result["edges"]


def test_reference_resolution_ignores_code_ambiguity_and_external_links(system):
    catalog, queue, graph, _, _ = system
    publish(catalog, "origin", "[[目标标题#详细说明|阅读]] [别名](target.md) [外站](https://invalid.test/target)\n"
            "[[重复标题]]\n```\n[[other]]\n```\n`[[other]]`\n![图像](other.md)")
    publish(catalog, "target", "唯一目标正文。", title="目标标题")
    publish(catalog, "other", "独立正文。", title="重复标题")
    publish(catalog, "another", "第二份正文。", title="重复标题")
    _, result = ready(catalog, queue, graph)
    edges = [edge for edge in result["edges"] if edge["relation"] == "reference"]
    assert [(edge["source"], edge["target"]) for edge in edges] == [("origin", "target")]
    assert "source_aliases" not in json.dumps(result) and "package.zip" not in json.dumps(result)


def test_real_source_section_references_remain_while_boilerplate_terms_are_excluded(system):
    catalog, queue, graph, _, _ = system
    body = "# 招聘\n岗位画像及面试。\n## 来源\n统一机器人通用声明。\n[依据](knowledge://target#细节)"
    publish(catalog, "origin", body)
    publish(catalog, "target", "目标独立正文。")
    _, result = ready(catalog, queue, graph)
    assert [(edge["source"], edge["target"]) for edge in result["edges"]] == [("origin", "target")]
    assert result["edges"][0]["evidence"][0]["excerpt"] in body
    assert "机器人" not in _features({"title": "招聘"}, body)["terms"]


def test_explicit_references_suppress_overlapping_recommendation_in_both_directions(system):
    catalog, queue, graph, _, _ = system
    left = publish(catalog, "left", "招聘岗位画像与候选人面试。\n[[right]]")
    right = publish(catalog, "right", "候选人面试与招聘岗位画像。\n[[left]]")
    _, result = ready(catalog, queue, graph)
    assert result["counts"]["explicit_edges"] == 2 and result["counts"]["related_edges"] == 0
    assert {(edge["source"], edge["target"]) for edge in result["edges"]} == {("left", "right"), ("right", "left")}
    original = {"left": left["content"], "right": right["content"]}
    assert all(edge["directed"] is True and edge["label"] == "正文引用" for edge in result["edges"])
    assert all(evidence["excerpt"] in original[evidence["knowledge_id"]] for edge in result["edges"] for evidence in edge["evidence"])


def test_source_aliases_require_unique_matching_package_and_do_not_leak(system):
    catalog, queue, graph, _, _ = system
    evidence = {"governance_receipt_id": "private-receipt"}
    publish(catalog, "origin", "参考[资料](refs/target.md)。", sources=[{"name": "private-package", "locator": "notes/start.md"}], evidence=evidence)
    publish(catalog, "target", "目标正文。", sources=[{"name": "private-package", "locator": "notes/refs/target.md"}], evidence=evidence)
    publish(catalog, "other", "另一来源。", sources=[{"name": "other-package", "locator": "notes/refs/target.md"}], evidence=evidence)
    _, result = ready(catalog, queue, graph)
    assert [(edge["source"], edge["target"]) for edge in result["edges"]] == [("origin", "target")]
    assert "private-package" not in json.dumps(result) and "private-receipt" not in json.dumps(result)
    publish(catalog, "ambiguous", "歧义来源。", sources=[{"name": "private-package", "locator": "notes/refs/target.md"}], evidence=evidence)
    _, result = ready(catalog, queue, graph)
    assert not result["edges"]


def test_relative_file_reference_does_not_fall_back_to_package_root(system):
    catalog, queue, graph, _, _ = system
    evidence = {"governance_receipt_id": "fixture-receipt"}
    publish(catalog, "origin", "[下一篇](target.md)", sources=[{"name": "same.zip", "locator": "notes/start.md"}], evidence=evidence)
    publish(catalog, "root-target", "根目录资料", sources=[{"name": "same.zip", "locator": "target.md"}], evidence=evidence)
    _, result = ready(catalog, queue, graph)
    assert not result["edges"]
    publish(catalog, "correct-target", "相对目录资料", sources=[{"name": "same.zip", "locator": "notes/target.md"}], evidence=evidence)
    _, result = ready(catalog, queue, graph)
    assert [(edge["source"], edge["target"]) for edge in result["edges"]] == [("origin", "correct-target")]


def test_relative_files_need_a_trusted_matching_receipt_namespace(system):
    catalog, queue, graph, _, _ = system
    publish(catalog, "origin", "[下一篇](target.md)", sources=[{"name": "same.zip", "locator": "start.md"}])
    publish(catalog, "target", "同名包不代表同一来源", sources=[{"name": "same.zip", "locator": "target.md"}])
    _, result = ready(catalog, queue, graph)
    assert not result["edges"]
    publish(catalog, "origin", "[下一篇](target.md)", sources=[{"name": "same.zip", "locator": "start.md"}],
            evidence={"governance_receipt_id": "receipt-a"})
    publish(catalog, "target", "同名包不代表同一来源", sources=[{"name": "same.zip", "locator": "target.md"}],
            evidence={"governance_receipt_id": "receipt-b"})
    _, result = ready(catalog, queue, graph)
    assert not result["edges"]


def test_metadata_declarations_are_not_called_body_references(system):
    catalog, queue, graph, _, _ = system
    publish(catalog, "left", "左边正文", evidence={"related_assets": [{"knowledge_id": "right", "source_locator": "private-path"}, {"knowledge_id": "private-draft"}]})
    publish(catalog, "right", "右边正文")
    catalog.import_draft({"knowledge_id": "private-draft", "title": "private-title", "content": "私有", "sources": [{"name": "secret"}]})
    _, result = ready(catalog, queue, graph)
    assert len(result["edges"]) == 1
    assert result["edges"][0]["label"] == "已记录关联" and result["edges"][0]["evidence"] == []
    assert "private" not in json.dumps(result)


def test_new_drafts_do_not_reindex_and_replacement_removes_old_links(system):
    catalog, queue, graph, reads, _ = system
    publish(catalog, "left", "引用 [[right]]")
    publish(catalog, "right", "右边内容")
    ready(catalog, queue, graph)
    changed = catalog.import_draft({"knowledge_id": "left", "title": "新版", "content": "新版没有引用", "sources": [{"name": "new.md"}]})
    assert graph.read(catalog, _catalog_kind)["counts"]["explicit_edges"] == 1
    assert not queue.tasks and sum(reads.values()) == 2
    catalog.publish("left", 2, expected_generation=changed["generation"], confirmed_by="test")
    pending = graph.read(catalog, _catalog_kind)
    assert pending["counts"]["indexed_assets"] == 1 and pending["counts"]["explicit_edges"] == 0
    queue.finish()
    current = graph.read(catalog, _catalog_kind)
    assert current["counts"]["indexed_assets"] == 2 and current["counts"]["explicit_edges"] == 0
    assert sum(reads.values()) == 3


def test_final_snapshot_drops_withdrawn_node_edges_and_excerpts(system, monkeypatch):
    catalog, queue, graph, _, _ = system
    publish(catalog, "left", "引用 [[right]]")
    publish(catalog, "right", "右边内容")
    ready(catalog, queue, graph)
    original = catalog.graph_internal_snapshot
    calls = [0]
    def change_during_graph():
        calls[0] += 1
        if calls[0] == 2:
            catalog.withdraw("right")
        return original()
    monkeypatch.setattr(catalog, "graph_internal_snapshot", change_during_graph)
    result = graph.read(catalog, _catalog_kind)
    assert {node["knowledge_id"] for node in result["nodes"]} == {"left"}
    assert result["counts"]["published_assets"] == 1 and not result["edges"]


def test_final_snapshot_replacement_drops_cached_old_revision_evidence(system, monkeypatch):
    catalog, queue, graph, _, _ = system
    publish(catalog, "left", "引用 [[right]]")
    publish(catalog, "right", "右边内容")
    ready(catalog, queue, graph)
    original = catalog.graph_internal_snapshot
    calls = [0]
    def change_during_graph():
        calls[0] += 1
        if calls[0] == 2:
            publish(catalog, "left", "新正文没有引用", title="新标题")
        return original()
    monkeypatch.setattr(catalog, "graph_internal_snapshot", change_during_graph)
    result = graph.read(catalog, _catalog_kind)
    current = next(node for node in result["nodes"] if node["knowledge_id"] == "left")
    assert current["revision"] == 2 and current["title"] == "新标题"
    assert result["status"] == "building" and result["counts"]["indexed_assets"] == 1
    assert not result["edges"]


def test_stale_queued_work_skips_source_and_same_revision_republish_reindexes(system):
    catalog, queue, graph, reads, _ = system
    publish(catalog, "left", "引用 [[right]]")
    publish(catalog, "right", "右边内容")
    graph.read(catalog, _catalog_kind)
    catalog.withdraw("right")
    graph.read(catalog, _catalog_kind)
    queue.finish()
    assert reads[(str(catalog.path), "right")] == 0
    current = catalog.snapshot("right", 1)
    catalog.publish("right", 1, expected_generation=current["generation"], confirmed_by="test")
    _, result = ready(catalog, queue, graph)
    assert result["counts"]["indexed_assets"] == 2
    assert sum(reads.values()) == 2
    catalog.withdraw("left")
    current = catalog.snapshot("left", 1)
    catalog.publish("left", 1, expected_generation=current["generation"], confirmed_by="test")
    assert graph.read(catalog, _catalog_kind)["counts"]["indexed_assets"] == 1
    queue.finish()
    assert graph.read(catalog, _catalog_kind)["counts"]["indexed_assets"] == 2
    assert reads[(str(catalog.path), "left")] == 2


def test_failures_retry_after_backoff_and_cache_isolated_by_store(system, tmp_path, monkeypatch):
    catalog, queue, graph, _, now = system
    publish(catalog, "same", "当前正文")
    original = KnowledgeStore.get_published
    attempts = [0]
    def broken(store, kid):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("private-storage-error")
        return original(store, kid)
    monkeypatch.setattr(KnowledgeStore, "get_published", broken)
    graph.read(catalog, _catalog_kind); queue.finish()
    result = graph.read(catalog, _catalog_kind)
    assert result["status"] == "partial" and result["counts"]["failed_assets"] == 1
    assert not queue.tasks and "private" not in json.dumps(result)
    now[0] = 59
    graph.read(catalog, _catalog_kind)
    assert not queue.tasks
    now[0] = 60
    graph.read(catalog, _catalog_kind); queue.finish()
    assert graph.read(catalog, _catalog_kind)["status"] == "ready"
    second = KnowledgeStore(tmp_path / "other.sqlite3")
    publish(second, "same", "第二家知识正文")
    assert graph.read(second, _catalog_kind)["counts"]["indexed_assets"] == 0
    queue.finish()
    assert graph.read(second, _catalog_kind)["status"] == "ready" and attempts[0] == 3


def test_queue_and_document_cap_remain_bounded_during_changing_publications(system, monkeypatch):
    catalog, queue, graph, _, _ = system
    monkeypatch.setattr("rag_app.knowledge_graph.MAX_DOCUMENTS", 3)
    graph.max_documents = 2
    for index in range(4):
        publish(catalog, "item-" + str(index), "普通正文")
    initial = graph.read(catalog, _catalog_kind)
    assert initial["truncated"] is True and len(initial["nodes"]) == 2 and len(queue.tasks) == 2
    for index in range(4, 10):
        publish(catalog, "item-" + str(index), "新增正文")
        graph.read(catalog, _catalog_kind)
    assert len(queue.tasks) == graph.pending_total == 3
    queue.finish()
    graph.read(catalog, _catalog_kind); queue.finish()
    current = graph.read(catalog, _catalog_kind)
    assert current["status"] == "partial" and current["counts"]["indexed_assets"] == 2


def test_boilerplate_is_removed_and_features_never_keep_full_body():
    content = "# 工作方法\n招聘面试岗位画像。\n## 来源\n机器人来源token通用来源模板。\n```\n[[private-id]]\n```"
    feature = _features({"title": "岗位画像"}, content)
    assert not feature["mentions"] and "机器人" not in feature["terms"] and "token" not in feature["terms"]
    assert set(feature) == {"terms", "mentions", "snippets"} and "content" not in feature
    assert all(snippet in content for snippet in feature["snippets"].values())


def test_fixed_common_word_cap_limits_candidate_expansion():
    records = {str(index): {"revision": 1} for index in range(200)}
    features = {kid: {"terms": {"commonone": 3, "commontwo": 2} if int(kid) < 65 else {"unique" + kid: 1},
                      "snippets": {"commonone": "原文", "commontwo": "原文", "unique" + kid: "原文"}}
                for kid in records}
    assert not _related(records, features)
