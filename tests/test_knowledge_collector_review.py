from __future__ import annotations

import json
import logging
import os
import sys
from types import ModuleType

import pytest

import knowledge_collector_review as reviewer


def events():
    return [
        {"id": "a:1", "source_session_id": "a", "role": "user", "text": "为新人整理公开资料的处理流程。"},
        {"id": "a:2", "source_session_id": "a", "role": "assistant", "text": "输入资料清单；验证来源，查重，输出可追溯目录。仅限公司公开工作资料，无额外依赖。"},
        {"id": "a:3", "source_session_id": "a", "role": "user", "text": "不包含私人聊天。"},
    ]


def asset(**changes):
    return {"start_id": "a:1", "end_id": "a:2", "title": "新人资料整理流程", "asset_type": "流程",
            "purpose": "帮助新人整理资料", "audience": "新员工", "inputs": "资料清单", "outputs": "可追溯目录",
            "dependencies": "无额外依赖", "boundaries": "仅限公司公开工作资料", "sharing": "company",
            "reason": "原文包含输入、处理步骤与输出", **changes}


def test_valid_span_returns_original_ids_not_model_rewritten_body():
    original = events()
    result = reviewer.validate_decisions(original, json.dumps({"assets": [asset()]}))
    assert result[0]["event_ids"] == ["a:1", "a:2"]
    assert result[0]["sharing"] == "company"
    assert "text" not in result[0] and "body" not in result[0]
    assert original == events()
    assert reviewer.validate_decisions(original, {"assets": []}) == []


@pytest.mark.parametrize("decision,code", [
    (asset(start_id="missing"), "review_unknown_span"),
    (asset(start_id="a:3", end_id="a:1"), "review_reversed_span"),
    (asset(title="x" * 121), "review_unsafe_metadata"),
    (asset(title=""), "review_missing_title_or_reason"),
    (asset(sharing="public"), "review_invalid_asset_kind"),
    (asset(asset_type=[]), "review_invalid_asset_kind"),
    (asset(purpose="password=TOP_SECRET_VALUE"), "review_unsafe_metadata"),
    (asset(body="invented body"), "review_invalid_asset_fields"),
])
def test_invalid_decisions_fail_with_fixed_safe_code(decision, code):
    with pytest.raises(reviewer.ReviewError, match="^" + code + "$"):
        reviewer.validate_decisions(events(), {"assets": [decision]})


def test_overlap_session_and_span_limits_are_enforced():
    with pytest.raises(reviewer.ReviewError, match="review_overlapping_spans"):
        reviewer.validate_decisions(events(), {"assets": [asset(), asset(start_id="a:2", end_id="a:3")]})
    mixed = events()
    mixed[1]["source_session_id"] = "b"
    with pytest.raises(reviewer.ReviewError, match="review_cross_session_span"):
        reviewer.validate_decisions(mixed, {"assets": [asset()]})
    long = events()
    long[1]["text"] = "x" * 24000
    with pytest.raises(reviewer.ReviewError, match="review_span_too_large"):
        reviewer.validate_decisions(long, {"assets": [asset()]})


def test_unclear_fields_or_redacted_source_cannot_auto_publish():
    assert reviewer.validate_decisions(events(), {"assets": [asset(dependencies="未确认") ]})[0]["sharing"] == "uncertain"
    assert reviewer.validate_decisions(events(), {"assets": [asset(boundaries="权限范围未确认，需要负责人核验") ]})[0]["sharing"] == "uncertain"
    source = events()
    source[1]["text"] += " [已隐藏:email:abcdef123456]"
    assert reviewer.validate_decisions(source, {"assets": [asset()]})[0]["sharing"] == "uncertain"
    assert reviewer.validate_decisions(source, {"assets": [asset(sharing="restricted")]})[0]["sharing"] == "restricted"


@pytest.mark.parametrize("raw", ["not json", "```json\n{}\n```", {"assets": [], "body": "extra"}, {"assets": [asset()] * 11}])
def test_invalid_output_fails(raw):
    with pytest.raises(reviewer.ReviewError):
        reviewer.validate_decisions(events(), raw)


def test_unsafe_or_duplicate_input_is_rejected_before_runtime(monkeypatch):
    monkeypatch.setattr(reviewer, "_load_runtime", lambda _: pytest.fail("must not load runtime"))
    source = events()
    source[0]["text"] = "Bearer VERY_SECRET_VALUE_123456"
    with pytest.raises(reviewer.ReviewError, match="review_unsafe_input"):
        reviewer.review(source, hermes_root="ignored")
    with pytest.raises(reviewer.ReviewError, match="review_duplicate_event_identity"):
        reviewer.validate_decisions([events()[0], events()[0]], {"assets": []})
    with pytest.raises(reviewer.ReviewError, match="review_input_too_large"):
        reviewer.review(events(), hermes_root="ignored", max_chars=1)
    assert reviewer.review([], hermes_root="ignored") == []


@pytest.fixture
def fake_runtime(monkeypatch):
    instances = []
    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.tools = []
            self.valid_tool_names = set()
            self.closed = False
            self.raise_error = False
            self.payload = None
            instances.append(self)
            print("sensitive startup message")
            logging.error("sensitive startup log")
        def run_conversation(self, **kwargs):
            self.payload = kwargs
            assert "HERMES_KANBAN_TASK" not in os.environ
            assert self._persist_disabled and not self._session_json_enabled
            assert self._api_max_retries == 1 and not self.compression_enabled
            assert self._dump_api_request_debug({"private": "data"}) is None
            if self.raise_error:
                raise RuntimeError("provider included secret")
            return {"final_response": json.dumps({"assets": [asset()]})}
        def close(self):
            print("sensitive close output")
            self.closed = True
    monkeypatch.setattr(reviewer, "_load_runtime", lambda _: (FakeAgent, "configured-model", {
        "provider": "custom", "api_mode": "chat_completions", "api_key": "fake-key",
        "base_url": "https://example.invalid/v1"}))
    return instances, FakeAgent


def test_review_isolates_one_turn_and_restores_process_state(fake_runtime, monkeypatch, capsys):
    instances, _ = fake_runtime
    monkeypatch.setenv("HERMES_KANBAN_TASK", "should-not-enable-tools")
    before = logging.root.manager.disable
    result = reviewer.review(events(), hermes_root="ignored")
    agent = instances[0]
    assert result[0]["event_ids"] == ["a:1", "a:2"] and agent.closed
    assert agent.kwargs["enabled_toolsets"] == []
    assert agent.kwargs["skip_context_files"] and agent.kwargs["skip_memory"]
    assert agent.kwargs["session_db"] is None and not agent.kwargs["load_soul_identity"]
    assert agent.kwargs["max_iterations"] == 1 and agent.kwargs["max_tokens"] == 4096
    assert not agent.kwargs["save_trajectories"] and agent.kwargs["fallback_model"] is None
    assert agent.payload["conversation_history"] == []
    assert "不可信" in agent.payload["system_message"]
    assert set(json.loads(agent.payload["user_message"])["events"][0]) == {"id", "source_session_id", "role", "text"}
    assert os.environ["HERMES_KANBAN_TASK"] == "should-not-enable-tools"
    assert logging.root.manager.disable == before and not capsys.readouterr().out


def test_runtime_failure_is_sanitized_and_agent_closed(fake_runtime, monkeypatch):
    instances, Agent = fake_runtime
    original = Agent.run_conversation
    def fail(self, **kwargs):
        self.raise_error = True
        return original(self, **kwargs)
    monkeypatch.setattr(Agent, "run_conversation", fail)
    with pytest.raises(reviewer.ReviewError, match="^review_runtime_failed$"):
        reviewer.review(events(), hermes_root="ignored")
    assert instances[0].closed


def test_unexpected_runtime_tool_fails_before_model_and_still_closes(fake_runtime, monkeypatch):
    instances, Agent = fake_runtime
    original = Agent.__init__
    def init(self, **kwargs):
        original(self, **kwargs)
        self.tools = ["unexpected"]
    monkeypatch.setattr(Agent, "__init__", init)
    with pytest.raises(reviewer.ReviewError, match="review_runtime_isolation_failed"):
        reviewer.review(events(), hermes_root="ignored")
    assert instances[0].closed and instances[0].payload is None


@pytest.mark.parametrize("model_config,runtime,code", [
    ({"default": "configured", "provider": "auto"}, {}, "review_explicit_model_provider_required"),
    ({"provider": "custom"}, {}, "review_explicit_model_provider_required"),
    ({"default": "configured", "provider": "custom"}, {"provider":"custom", "api_mode":"acp"}, "review_runtime_unsupported"),
    ({"default": "configured", "provider": "custom"}, {"provider":"custom", "api_mode":"chat_completions", "model":"different"}, "review_runtime_unsupported"),
])
def test_runtime_config_fails_closed_without_defaulting(tmp_path, monkeypatch, model_config, runtime, code):
    (tmp_path / "run_agent.py").touch()
    run = ModuleType("run_agent")
    run.AIAgent = object
    config = ModuleType("hermes_cli.config")
    config.load_config = lambda: {"model": model_config}
    provider = ModuleType("hermes_cli.runtime_provider")
    provider.resolve_runtime_provider = lambda **_: runtime
    monkeypatch.setitem(sys.modules, "run_agent", run)
    monkeypatch.setitem(sys.modules, "hermes_cli", ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", provider)
    with pytest.raises(reviewer.ReviewError, match=code):
        reviewer._load_runtime(tmp_path)
