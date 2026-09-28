import json
import socket
import threading

import pytest

import knowledge
from rag_app import knowledge_judge as judge


pytestmark = pytest.mark.offline


def batch_files(tmp_path, contents):
    assets = []
    for index, (title, content) in enumerate(contents):
        path = tmp_path / f"{index}.md"
        path.write_text(content)
        assets.append({"candidate_id": str(index), "body_file": path.name,
                       "content_hash": judge.sha(content), "metadata": {"title": title}})
    (tmp_path / "batch.json").write_text(json.dumps({"assets": assets}))


def valid(text, *_):
    return {"decision": "candidate", "reasons": ["有可追溯方法"],
            "evidence_anchors": [{"quote": text[:20], "location": "段首"}],
            "checks": dict.fromkeys(["reuse", "completeness", "evidence", "sharing"], "待核验")}


def test_call_judge_requests_json_without_thinking(monkeypatch):
    from rag_app import answer_service

    requests = []
    expected = valid("可复用的原文方法。")
    monkeypatch.setattr(answer_service, "ensure_dashscope_api_key", lambda: "offline-test-token")

    def post_json(url, headers, payload, timeout):
        requests.append((payload, timeout))
        return {"choices": [{"message": {"content": json.dumps(expected)}}]}

    monkeypatch.setattr(answer_service, "post_json", post_json)
    assert judge.call_judge("可复用的原文方法。", "方法", "offline-test-model") == expected
    payload, timeout = requests[0]
    assert payload["model"] == "offline-test-model"
    assert payload["enable_thinking"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert timeout == 120


@pytest.mark.parametrize("failure", ["json", "schema", "quote"])
def test_call_judge_corrects_invalid_output_once_without_changing_base_prompt(monkeypatch, failure):
    from rag_app import answer_service

    text = ("执行前检查输入，发现缺项先补齐。\n  再按步骤核验结果！" * 12) + "最后一段🐈"
    expected = valid(text)
    spans = [text[start:start + 120] for start in range(0, len(text), 120)]
    expected["evidence_anchors"] = [{"quote": spans[i], "location": f"source_span:{i}"}
                                    for i in (0, len(spans) - 1)]
    selected = valid(text)
    selected["evidence_anchors"] = [{"span_index": i, "quote": "untrusted-extra-quote",
                                     "location": "untrusted-extra-location"}
                                    for i in (0, len(spans) - 1)]
    invalid = valid(text)
    if failure == "schema":
        invalid["checks"] = []
    elif failure == "quote":
        invalid["evidence_anchors"] = [{"quote": "provider-output-secret-sentinel"}]
    replies = iter(["{" if failure == "json" else json.dumps(invalid), json.dumps(selected)])
    requests = []
    original_prompt, original_version = judge.PROMPT, judge.JUDGE_VERSION
    monkeypatch.setattr(answer_service, "ensure_dashscope_api_key", lambda: "offline-test-token")

    def post_json(url, headers, payload, timeout):
        requests.append((payload, timeout))
        return {"choices": [{"message": {"content": next(replies)}}]}

    monkeypatch.setattr(answer_service, "post_json", post_json)
    assert judge.call_judge(text, "方法", "offline-test-model") == expected
    assert len(requests) == 2
    first, second = (request[0] for request in requests)
    assert first["messages"][0]["content"] == original_prompt
    assert second["messages"][0]["content"].startswith(original_prompt)
    assert second["messages"][0]["content"] != original_prompt
    assert json.loads(first["messages"][1]["content"]) == {"title": "方法", "source_text": text}
    fallback_input = json.loads(second["messages"][1]["content"])
    assert fallback_input["title"] == "方法"
    assert fallback_input["source_spans"] == [{"index": i, "text": value} for i, value in enumerate(spans)]
    assert "".join(span["text"] for span in fallback_input["source_spans"]) == text
    assert "provider-output-secret-sentinel" not in json.dumps(second)
    for payload, timeout in requests:
        assert payload["model"] == "offline-test-model"
        assert payload["max_tokens"] == 1800
        assert payload["enable_thinking"] is False
        assert payload["response_format"] == {"type": "json_object"}
        assert timeout == 120
    assert (judge.PROMPT, judge.JUDGE_VERSION) == (original_prompt, original_version)


@pytest.mark.parametrize("anchors", [
    [{"span_index": -1}], [{"span_index": 999}], [{"span_index": True}],
    [{"span_index": False}], [{"span_index": "0"}],
    [{"quote": "当前正文只有输入核验步骤。"}], [], [{"span_index": 0}] * 3,
])
def test_call_judge_rejects_invalid_span_choice_after_one_correction(monkeypatch, anchors):
    from rag_app import answer_service

    requests = []
    invalid = valid("原文以外的引文")
    selected = valid("当前正文只有输入核验步骤。")
    selected["evidence_anchors"] = anchors
    replies = iter([json.dumps(invalid), json.dumps(selected)])
    monkeypatch.setattr(answer_service, "ensure_dashscope_api_key", lambda: "offline-test-token")

    def post_json(*args, **kwargs):
        requests.append(True)
        return {"choices": [{"message": {"content": next(replies)}}]}

    monkeypatch.setattr(answer_service, "post_json", post_json)
    with pytest.raises(ValueError, match="invalid judgment source span"):
        judge.call_judge("当前正文只有输入核验步骤。", "方法", "offline-test-model")
    assert len(requests) == 2


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_call_judge_does_not_retry_provider_failure(monkeypatch, error_type):
    from rag_app import answer_service

    requests = []
    monkeypatch.setattr(answer_service, "ensure_dashscope_api_key", lambda: "offline-test-token")

    def post_json(*args, **kwargs):
        requests.append(True)
        raise error_type("provider-error-secret-sentinel")

    monkeypatch.setattr(answer_service, "post_json", post_json)
    with pytest.raises(error_type):
        judge.call_judge("当前正文只有输入核验步骤。", "方法", "offline-test-model")
    assert len(requests) == 1


def test_bounded_parallel_calls_preserve_every_window_and_resume(tmp_path):
    contents = [(f"方法{index}", (chr(0x7532 + index) * 7000)) for index in range(3)]
    batch_files(tmp_path, contents)
    barrier, lock = threading.Barrier(3), threading.Lock()
    active = maximum = calls = 0

    def caller(text, title, model):
        nonlocal active, maximum, calls
        with lock:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=5)
            return valid(text)
        finally:
            with lock:
                active -= 1

    result = judge.judge_batch(tmp_path, execute=True, caller=caller, model="offline-test", workers=3)
    assert result["complete"] == 3 and result["windows"] == calls == 6
    assert maximum == result["workers"] == 3
    rows = json.loads((tmp_path / "model_reviews.json").read_text())["assets"]
    for row, (_, content) in zip(rows, contents):
        assert row["coverage_complete"] is True
        assert [(w["start"], w["end"]) for w in row["windows"]] == [(0, 6000), (6000, 7000)]
        assert row["content_hash"] == judge.sha(content)
    judge.judge_batch(tmp_path, execute=True, caller=lambda *_: pytest.fail("cached windows must not call"),
                      model="offline-test", workers=8)
    assert all((tmp_path / f"{i}.md").read_text() == text for i, (_, text) in enumerate(contents))
    assert not list((tmp_path / "judgments").glob("*.tmp"))


def test_outage_stops_new_calls_but_cached_success_and_retry_survive(tmp_path):
    contents = [(f"方法{i}", f"输入{i}：仅保留待核验方法。") for i in range(8)]
    batch_files(tmp_path, contents[:1])
    judge.judge_batch(tmp_path, execute=True, caller=valid, model="offline-test")
    batch_files(tmp_path, contents)
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise RuntimeError("provider-error-secret-sentinel")

    result = judge.judge_batch(tmp_path, execute=True, caller=unavailable, model="offline-test", workers=3)
    assert 1 <= len(calls) <= 3
    assert result["complete"] == 1 and result["failed"] == 7 and result["status"] == "partial"
    text = (tmp_path / "model_reviews.json").read_text()
    rows = json.loads(text)["assets"]
    assert "provider-error-secret-sentinel" not in text
    assert all(row["decision"] == "review_required" and not row["coverage_complete"] for row in rows[1:])
    retried = []

    def recovered(text, *args):
        retried.append(text)
        return valid(text)

    result = judge.judge_batch(tmp_path, execute=True, caller=recovered, model="offline-test", workers=4)
    assert result["complete"] == 8 and len(retried) == 7


@pytest.mark.parametrize("error_type", [socket.timeout, TimeoutError])
def test_isolated_timeout_keeps_scheduling_and_only_failed_window_retries(tmp_path, error_type):
    batch_files(tmp_path, [(str(i), f"待核验方法 {i}。") for i in range(4)])
    scheduled_after_timeout, calls = threading.Event(), []

    def caller(text, title, model):
        calls.append(title)
        if title == "0":
            raise error_type("private-timeout-message")
        if title == "1":
            assert scheduled_after_timeout.wait(timeout=5)
        else:
            scheduled_after_timeout.set()
        return valid(text)

    result = judge.judge_batch(tmp_path, execute=True, caller=caller, model="offline-test", workers=2)
    assert result["complete"] == 3 and result["failed"] == 1 and sorted(calls) == ["0", "1", "2", "3"]
    report = (tmp_path / "model_reviews.json").read_text()
    row = json.loads(report)["assets"][0]
    assert row["decision"] == "review_required" and row["windows"][0]["status"] == "request_timeout"
    assert "private-timeout-message" not in report
    calls.clear()

    def recovered(text, title, model):
        calls.append(title)
        return valid(text)

    assert judge.judge_batch(tmp_path, execute=True, caller=recovered, model="offline-test", workers=2)["complete"] == 4
    assert calls == ["0"]


@pytest.mark.parametrize("error_type", [socket.timeout, TimeoutError])
def test_consecutive_timeouts_at_worker_limit_stop_new_dispatch(tmp_path, error_type):
    batch_files(tmp_path, [(str(i), f"待核验方法 {i}。") for i in range(5)])
    calls = []

    def caller(text, title, model):
        calls.append(title)
        raise error_type("private-timeout-message")

    result = judge.judge_batch(tmp_path, execute=True, caller=caller, model="offline-test", workers=1)
    assert result["complete"] == 0 and result["failed"] == 5 and calls == ["0"]
    rows = json.loads((tmp_path / "model_reviews.json").read_text())["assets"]
    assert rows[0]["windows"][0]["status"] == "request_timeout"
    assert all(row["windows"][0]["status"] == "provider_unavailable" for row in rows[1:])


def test_duplicate_cache_keys_call_once_and_corrupt_cache_can_retry(tmp_path):
    batch_files(tmp_path, [("相同标题", "相同的有效正文。"), ("相同标题", "相同的有效正文。")])
    calls = []

    def caller(text, *args):
        calls.append(text)
        return valid(text)

    result = judge.judge_batch(tmp_path, execute=True, caller=caller, model="offline-test", workers=4)
    assert result["complete"] == 2 and len(calls) == 1
    cached, = (tmp_path / "judgments").glob("*.json")
    cached.write_text("{")
    result = judge.judge_batch(tmp_path, execute=True, caller=caller, model="offline-test", workers=4)
    assert result["complete"] == 2 and len(calls) == 2
    assert json.loads(cached.read_text())["decision"] == "candidate"


@pytest.mark.parametrize("workers", [0, 9, True, 1.5])
def test_workers_rejected_before_reading_inputs(tmp_path, workers):
    with pytest.raises(ValueError, match="workers"):
        judge.judge_batch(tmp_path, workers=workers)


def test_judge_cli_passes_workers_without_service_or_model_call(tmp_path, monkeypatch, capsys):
    received = []
    monkeypatch.setattr(judge, "judge_batch", lambda *args, **kwargs: received.append(kwargs) or {"status": "dry_run"})
    assert knowledge.main(["judge-batch", "--batch", str(tmp_path), "--workers", "4"]) == 0
    assert received == [{"execute": False, "workers": 4}]
    capsys.readouterr()
