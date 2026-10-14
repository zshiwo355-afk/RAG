import json

import pytest

from rag_app import answer_service, knowledge_relevance


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(answer_service, "ensure_dashscope_api_key", lambda: "unit-test-only")
    def install(content, finish="stop"):
        def post(url, headers, payload, timeout):
            assert timeout == 18 and payload["enable_thinking"] is False
            assert payload["messages"][0]["role"] == "system"
            return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}]}
        monkeypatch.setattr(answer_service, "post_json", post)
    return install


def test_supported_evidence_and_empty_decision(model):
    documents = ["标题：合并规则\n\n七天之内允许回档。", "标题：空模板\n\n成功率：待填写"]
    model({"supported": [{"index": 0, "span_index": 0}]})
    assert knowledge_relevance.supported_indices("合并错了能恢复吗", documents) == {0}
    model({"supported": []})
    assert knowledge_relevance.supported_indices("实验成功率是多少", documents) == set()
    model({"supported": [{"index": 0, "span_index": 0}] * 2})
    assert knowledge_relevance.supported_indices("回档", documents) == {0}


@pytest.mark.parametrize("value", [
    {}, {"supported": None}, {"supported": {}, "extra": True},
    {"supported": [{"index": True, "span_index": 0}]},
    {"supported": [{"index": -1, "span_index": 0}]},
    {"supported": [{"index": 1, "span_index": 0}]},
    {"supported": [{"index": 0, "span_index": True}]},
    {"supported": [{"index": 0, "span_index": -1}]},
    {"supported": [{"index": 0, "span_index": 1}]},
    {"supported": [{"index": 0, "span_index": 0, "approved": True}]},
])
def test_invalid_or_unanchored_decision_is_not_silently_empty(model, value):
    model(value)
    with pytest.raises(RuntimeError, match="decision invalid"):
        knowledge_relevance.supported_indices("回档", ["标题：合并规则\n\n七天之内允许回档。"])


def test_truncated_model_response_is_service_error(model):
    model({"supported": []}, finish="length")
    with pytest.raises(RuntimeError, match="decision invalid"):
        knowledge_relevance.supported_indices("查询", ["标题：示例\n\n这是正文内容"])


def test_no_candidates_needs_no_model_call():
    assert knowledge_relevance.supported_indices("查询", []) == set()
