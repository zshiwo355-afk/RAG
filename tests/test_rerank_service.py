from __future__ import annotations

import pytest

from rag_app import rerank_service
from rag_app.rerank_service import call_rerank_model


pytestmark = pytest.mark.offline


@pytest.mark.parametrize("response,expected", [
    ({"output": {"results": [{"index": 1, "relevance_score": 0.9},
                              {"index": 0, "relevance_score": 0}]}}, [0.0, 0.9]),
    *[({"output": {"results": [{"index": 0, "relevance_score": bad},
                                {"index": 1, "relevance_score": 0.8}]}}, None)
      for bad in (None, "0.9", True, float("nan"), float("inf"), 10**1000)],
    *[({"output": {"results": [{"index": bad, "relevance_score": 0.9},
                                {"index": 1, "relevance_score": 0.8}]}}, None)
      for bad in (False, "0", -1, 2, 1)],
    ({"output": {"results": [{"index": 0, "relevance_score": 0.9}]}}, None),
    ({"output": {"results": [None, {"index": 1, "relevance_score": 0.8}]}}, None),
    ({"output": {"results": "credential-sentinel source-document"}}, None),
    ({"code": "error", "message": "credential-sentinel source-document"}, None),
    (None, None),
])
def test_rerank_requires_one_finite_numeric_score_per_input(monkeypatch, response, expected):
    # Imported function alias remains real when the offline fixture blocks cloud calls.
    monkeypatch.setattr(rerank_service, "ensure_dashscope_api_key", lambda: "offline")
    monkeypatch.setattr(rerank_service, "get_rerank_model", lambda: "gte-rerank-v2")
    monkeypatch.setattr(rerank_service, "post_json", lambda *args, **kwargs: response)
    if expected is not None:
        assert call_rerank_model("synthetic query", ["first", "second"]) == expected
    else:
        with pytest.raises(RuntimeError) as error:
            call_rerank_model("synthetic query", ["first", "second"])
        assert "credential-sentinel" not in str(error.value)
        assert "source-document" not in str(error.value)
