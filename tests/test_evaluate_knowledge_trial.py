import hashlib
import io
import json

import pytest

import evaluate_knowledge_trial as evaluate


pytestmark = pytest.mark.offline


def write_manifest(tmp_path):
    body = "# 活动方案\n\n先确认目标，再明确预算和分工。\n"
    (tmp_path / "body.md").write_text(body, encoding="utf-8")
    asset = {"knowledge_id": "trial_a", "revision": 1, "title": "活动方案",
             "content_file": "body.md", "content_sha256": hashlib.sha256(body.encode()).hexdigest()}
    query = {"query_id": "title-a", "category": "locator", "source": "title",
             "query": "活动方案", "expected_ids": ["trial_a"], "top_k": 5}
    payload = {"schema_version": 1, "trial_id": "trial", "base_url": "http://127.0.0.1:9999",
               "assets": [asset], "queries": [query]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload, body


def test_default_dry_run_validates_standard_bodies_without_http_or_output(tmp_path, monkeypatch, capsys):
    path, _, _ = write_manifest(tmp_path)
    monkeypatch.setattr(evaluate.urllib_request.OpenerDirector, "open", lambda *_a, **_k: pytest.fail("no HTTP"))
    output = tmp_path / "evaluation"
    assert evaluate.main(["--manifest", str(path), "--out", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "dry_run"
    assert result["asset_count"] == result["query_count"] == 1
    assert not output.exists()


def mock_api(monkeypatch, documents, rows=None):
    calls = []

    def open_request(_opener, request, timeout):
        calls.append((request.get_method(), request.full_url))
        if request.get_method() == "POST":
            payload = json.loads(request.data)
            assert payload["include_content"] is True
            results = rows if rows is not None else list(documents.values())
            data = {"ok": True, "results": results, "result_count": len(results)}
        else:
            key = request.full_url.rsplit("/", 1)[1].split("?")[0]
            data = {"ok": True, "knowledge": documents[key]}
        response = io.BytesIO(json.dumps(data).encode())
        response.status = 200
        return response

    monkeypatch.setattr(evaluate.urllib_request.OpenerDirector, "open", open_request)
    return calls


def api_document(asset, body):
    return {"knowledge_id": asset["knowledge_id"], "revision": asset["revision"], "status": "published",
            "content": body, "content_hash": asset["content_sha256"], "score": 0.9,
            "snippet": "先确认目标", "chunk_id": asset["knowledge_id"] + "__r1__c1",
            "read_url": f"/api/knowledge/{asset['knowledge_id']}?revision={asset['revision']}"}


def test_execution_separates_query_groups_checks_each_full_body_and_accepts_multiple_targets(tmp_path, monkeypatch):
    path, payload, body = write_manifest(tmp_path)
    second = {**payload["assets"][0], "knowledge_id": "trial_b", "title": "另一方法"}
    payload["assets"].append(second)
    payload["queries"] += [
        {"query_id": "business", "category": "business", "source": "manual_business",
         "query": "办活动前如何分工？", "expected_ids": ["trial_a", "trial_b"]},
        {"query_id": "negative", "category": "negative", "source": "negative_fixture",
         "query": "一个暂未判定相关性的题目", "expected_ids": [], "forbidden_ids": ["old_trial"]},
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    docs = {a["knowledge_id"]: api_document(a, body) for a in reversed(payload["assets"])}
    calls = mock_api(monkeypatch, docs)
    output = tmp_path / "evaluation"
    assert evaluate.main(["--manifest", str(path), "--out", str(output), "--execute"]) == 0
    results = [json.loads(line) for line in (output / "queries.jsonl").read_text().splitlines()]
    assert results[0]["expected_ranks"] == {"trial_a": 2}
    assert results[1]["expected_ranks"] == {"trial_a": 2, "trial_b": 1}
    assert results[1]["expected_coverage"] == 1.0
    assert results[2]["any_hit"] is None and results[2]["checks_ok"] is True
    assert all(row["body_matches"] and row["read_body_matches"] for result in results for row in result["results"])
    assert all(row["content_sha256"] == row["read_content_sha256"] == row["expected_content_sha256"]
               for result in results for row in result["results"])
    assert len(calls) == 9
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "completed"
    assert summary["groups"]["locator"]["top1_hits"] == 0
    assert summary["groups"]["business"]["top1_hits"] == 1
    assert summary["groups"]["negative"]["expected_queries"] == 0
    assert "不代表真实用户 Recall" in (output / "report.md").read_text()
    assert all(body not in p.read_text() for p in output.iterdir())


@pytest.mark.parametrize("change", [
    {"source": ["title"]}, {"source": "manual_business"}, {"expected_ids": ["outside_trial"]},
    {"top_k": True}, {"must_be_empty": True},
])
def test_bad_manifest_stops_before_http(tmp_path, monkeypatch, change):
    path, payload, _ = write_manifest(tmp_path)
    payload["queries"][0].update(change)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(evaluate.urllib_request.OpenerDirector, "open", lambda *_a, **_k: pytest.fail("no HTTP"))
    assert evaluate.main(["--manifest", str(path), "--out", str(tmp_path / "out"), "--execute"]) == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("mutation,expected_error", [
    ("body", "search_body_or_version_mismatch"), ("revision", "search_body_or_version_mismatch"),
    ("url", "read_url_mismatch"), ("outside", "asset_outside_trial"),
    ("duplicate", "duplicate_asset"), ("untrusted_revision", "search_body_or_version_mismatch"),
])
def test_bad_returned_assets_never_earn_a_hit_or_expose_untrusted_fields(tmp_path, monkeypatch, mutation, expected_error):
    path, payload, body = write_manifest(tmp_path)
    doc = api_document(payload["assets"][0], body)
    row = dict(doc)
    rows = [row]
    if mutation == "body":
        row["content"] = "缺少正文"
    elif mutation == "revision":
        row["revision"] = 2
    elif mutation == "url":
        row["read_url"] = "https://private.example/do-not-store"
    elif mutation == "outside":
        row["knowledge_id"] = "not_in_this_trial"
    elif mutation == "duplicate":
        rows.append(dict(row))
    else:
        row["revision"] = {"secret": "do-not-store"}
    calls = mock_api(monkeypatch, {"trial_a": doc}, rows)
    output = tmp_path / "out"
    assert evaluate.main(["--manifest", str(path), "--out", str(output), "--execute"]) == 1
    result = json.loads((output / "queries.jsonl").read_text())
    assert expected_error in result["errors"]
    assert json.loads((output / "summary.json").read_text())["groups"]["locator"]["any_hits"] == 0
    if mutation in {"url", "outside"}:
        assert len(calls) == 1
    assert all("do-not-store" not in p.read_text() for p in output.iterdir())


def test_http_failure_is_safe_persisted_and_does_not_skip_later_questions(tmp_path, monkeypatch, capsys):
    path, payload, body = write_manifest(tmp_path)
    payload["queries"].append({**payload["queries"][0], "query_id": "second"})
    path.write_text(json.dumps(payload), encoding="utf-8")
    mock_api(monkeypatch, {"trial_a": api_document(payload["assets"][0], body)})
    good_open = evaluate.urllib_request.OpenerDirector.open
    first = True

    def sometimes_fails(opener, request, timeout):
        nonlocal first
        if first:
            first = False
            raise evaluate.urllib_error.HTTPError(request.full_url, 503, "do-not-store", {}, io.BytesIO(b"do-not-store"))
        return good_open(opener, request, timeout)

    monkeypatch.setattr(evaluate.urllib_request.OpenerDirector, "open", sometimes_fails)
    output = tmp_path / "out"
    args = ["--manifest", str(path), "--out", str(output), "--execute"]
    assert evaluate.main(args) == 1
    rows = [json.loads(s) for s in (output / "queries.jsonl").read_text().splitlines()]
    assert rows[0]["http_status"] == 503 and rows[0]["errors"] == ["search_HTTPError"]
    assert rows[1]["checks_ok"] and rows[1]["any_hit"]
    before = (output / "queries.jsonl").read_bytes()
    assert evaluate.main(args) == 2  # A repeated run cannot overwrite evidence.
    assert (output / "queries.jsonl").read_bytes() == before
    assert "do-not-store" not in capsys.readouterr().out
    assert all("do-not-store" not in p.read_text() for p in output.iterdir())


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1.evil", "http://user:secret@127.0.0.1", "http://127.0.0.1/path"])
def test_nonlocal_or_credentialed_origin_is_rejected(tmp_path, monkeypatch, url):
    path, _, _ = write_manifest(tmp_path)
    monkeypatch.setattr(evaluate.urllib_request.OpenerDirector, "open", lambda *_a, **_k: pytest.fail("no HTTP"))
    assert evaluate.main(["--manifest", str(path), "--out", str(tmp_path / "out"), "--base-url", url, "--execute"]) == 2


def test_changed_standard_body_is_rejected_and_explicit_negative_empty_is_enforced(tmp_path, monkeypatch):
    path, payload, body = write_manifest(tmp_path)
    (tmp_path / "body.md").write_text(body + "changed", encoding="utf-8")
    assert evaluate.main(["--manifest", str(path), "--out", str(tmp_path / "unused")]) == 2
    (tmp_path / "body.md").write_text(body, encoding="utf-8")
    payload["queries"] = [{"query_id": "empty", "category": "negative", "source": "negative_fixture",
                           "query": "明确隔离负例", "must_be_empty": True}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    mock_api(monkeypatch, {"trial_a": api_document(payload["assets"][0], body)})
    output = tmp_path / "out"
    assert evaluate.main(["--manifest", str(path), "--out", str(output), "--execute"]) == 1
    assert "expected_empty_result" in json.loads((output / "queries.jsonl").read_text())["errors"]
