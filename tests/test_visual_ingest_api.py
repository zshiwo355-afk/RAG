from __future__ import annotations

import base64
from pathlib import Path

import pytest

from rag_app import visual_ingest_api as api


@pytest.mark.offline
def test_visual_ingest_upload_sanitizes_and_stays_isolated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)

    result = api.upload_file(
        api.UploadRequest(
            filename="../危险.md",
            content_base64=base64.b64encode("hello".encode("utf-8")).decode("ascii"),
            batch_id="batch-a",
        )
    )

    assert result["filename"] == "危险.md"
    assert Path(result["path"]).is_file()
    assert str(Path(result["path"]).resolve()).startswith(str((tmp_path / api.UPLOAD_ROOT).resolve()))
    assert (tmp_path / api.RUN_ROOT / "batch-a" / "upload_result.json").exists()


@pytest.mark.offline
def test_visual_ingest_rejects_unsupported_extension(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)

    with pytest.raises(Exception):
        api.upload_file(api.UploadRequest(filename="bad.exe", content_base64=base64.b64encode(b"x").decode("ascii")))


@pytest.mark.offline
def test_parse_chunk_preflight_and_run_are_dry_run_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    request = api.VisualIngestRequest(
        batch_id="batch-b",
        source_input=api.SourceInput(input_type="text", raw_text="第一段资料。" * 80, source_name="测试资料"),
        config=api.IngestConfig(run_mode="dry_run", target={"index": "text_docs"}),
    )

    parse_result = api.parse_source(request)
    chunk_result = api.preview_chunks(api.VisualIngestRequest(batch_id="batch-b", config=request.config, parse_result=parse_result))
    preflight = api.preflight_ingest(
        api.VisualIngestRequest(batch_id="batch-b", config=request.config, parse_result=parse_result, chunk_result=chunk_result)
    )
    run = api.run_ingest(
        api.VisualIngestRequest(
            batch_id="batch-b",
            config=request.config,
            parse_result=parse_result,
            chunk_result=chunk_result,
            preflight_result=preflight,
        )
    )

    assert parse_result["calls_embedding"] is False
    assert parse_result["writes_vector_db"] is False
    assert chunk_result["calls_embedding"] is False
    assert chunk_result["writes_vector_db"] is False
    assert preflight["status"] == "pass"
    assert preflight["will_write_vector_db"] is False
    assert run["status"] == "success"
    assert run["vector_db_written"] is False
    assert not (tmp_path / "output/ingest_build").exists()
    assert not (tmp_path / "output/ingest_embeddings").exists()


@pytest.mark.offline
def test_real_run_is_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    request = api.VisualIngestRequest(batch_id="batch-c", config=api.IngestConfig(run_mode="real_run"))

    result = api.run_ingest(request)

    assert result["status"] == "blocked"
    assert "暂不开放真实入库" in result["message"]
    assert result["push_executed"] is False
    assert result["delete_executed"] is False


@pytest.mark.offline
def test_preflight_blocked_prevents_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    request = api.VisualIngestRequest(batch_id="batch-d", config=api.IngestConfig(run_mode="dry_run"))

    preflight = api.preflight_ingest(request)
    run = api.run_ingest(api.VisualIngestRequest(batch_id="batch-d", config=request.config, preflight_result=preflight))

    assert preflight["status"] == "blocked"
    assert run["status"] == "blocked"


@pytest.mark.offline
def test_answer_verify_default_is_blocked(monkeypatch) -> None:
    called = {"value": False}

    def fail_if_called(*_args, **_kwargs):
        called["value"] = True
        raise AssertionError("answer should not be called")

    monkeypatch.setitem(__import__("sys").modules, "rag_api", type("FakeRagApi", (), {"rag_answer": fail_if_called})())
    result = api.answer_verify(api.AnswerVerifyRequest(query="hello"))

    assert result["blocked"] is True
    assert result["called_answer"] is False
    assert called["value"] is False


@pytest.mark.offline
def test_search_verify_uses_search_wrapper(monkeypatch) -> None:
    class FakeSearchRequest:
        def __init__(self, query: str, top_k: int) -> None:
            self.query = query
            self.top_k = top_k

    class FakeRagApi:
        SearchRequest = FakeSearchRequest

        @staticmethod
        def rag_search(request: FakeSearchRequest) -> dict[str, object]:
            return {"ok": True, "query": request.query, "product_bundles": [{"product_id": "p"}]}

    monkeypatch.setitem(__import__("sys").modules, "rag_api", FakeRagApi)

    result = api.search_verify(api.SearchVerifyRequest(query="q", top_k=1))

    assert result["ok"] is True
    assert result["called_answer"] is False
    assert result["product_bundles"] == [{"product_id": "p"}]
