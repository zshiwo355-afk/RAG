"""Internal visual ingest console API.

The v1 API is intentionally non-destructive. It stores uploaded files and
preview artifacts under uploads/visual_ingest* only. It never calls embedding,
push, delete, or OpenSearch write paths.
"""

from __future__ import annotations

import base64
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Optional

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, Field
except Exception:
    class HTTPException(Exception):
        def __init__(self, status_code: int = 500, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def post(self, *_args: Any, **_kwargs: Any) -> Any:
            return lambda func: func

        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            return lambda func: func

    class BaseModel:
        def __init__(self, **kwargs: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for name in annotations:
                if name in kwargs:
                    setattr(self, name, kwargs[name])
                    continue
                default = getattr(self.__class__, name, None)
                if isinstance(default, _DefaultFactory):
                    setattr(self, name, default.factory())
                else:
                    setattr(self, name, default)

    class _DefaultFactory:
        def __init__(self, factory: Any) -> None:
            self.factory = factory

    def Field(default: Any = None, **kwargs: Any) -> Any:
        if "default_factory" in kwargs:
            return _DefaultFactory(kwargs["default_factory"])
        return default

from .config import PROJECT_ROOT, safe_text, truncate


UPLOAD_ROOT = Path("uploads/visual_ingest")
RUN_ROOT = Path("uploads/visual_ingest_runs")
ALLOWED_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".xlsx", ".xls", ".pdf"}
DEFAULT_CHUNK_SIZE = 900
DEFAULT_OVERLAP = 120
TARGET_DEFAULT = {"index": "text_docs", "collection": "text_docs", "namespace": "default"}

router = APIRouter(prefix="/api/rag/visual-ingest", tags=["visual-ingest"])


class UploadRequest(BaseModel):
    filename: str
    content_base64: str = Field(..., description="Base64 encoded file content")
    batch_id: Optional[str] = None


class SourceInput(BaseModel):
    input_type: str = Field("text", description="text/json/file")
    raw_text: Optional[str] = None
    raw_json: Optional[Any] = None
    file_path: Optional[str] = None
    source_name: Optional[str] = None
    source_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestConfig(BaseModel):
    knowledge_action: str = "supplement_existing"
    import_mode: str = "supplement"
    run_mode: str = "dry_run"
    target: dict[str, Any] = Field(default_factory=lambda: dict(TARGET_DEFAULT))
    chunk_size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_OVERLAP
    overwrite: bool = False
    answer_verify: bool = False
    new_knowledge_name: Optional[str] = None
    new_knowledge_description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    version: Optional[str] = None


class VisualIngestRequest(BaseModel):
    batch_id: Optional[str] = None
    source_input: SourceInput = Field(default_factory=SourceInput)
    config: IngestConfig = Field(default_factory=IngestConfig)
    parse_result: Optional[dict[str, Any]] = None
    chunk_result: Optional[dict[str, Any]] = None
    preflight_result: Optional[dict[str, Any]] = None


class SearchVerifyRequest(BaseModel):
    query: str
    top_k: int = Field(5, ge=1, le=30)


class AnswerVerifyRequest(BaseModel):
    query: str
    top_k: int = Field(5, ge=1, le=10)
    answer_verify: bool = False
    confirmation: Optional[str] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_batch_id() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S_%f")


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "").name.replace("\x00", "")
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name).strip("._")
    if not name:
        raise ValueError("filename is empty after sanitization")
    return name


def validate_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported file extension: {suffix or '<none>'}")
    return suffix


def resolve_under(base: Path, path: Path) -> Path:
    base_resolved = base.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes allowed root: {path}") from exc
    return path_resolved


def run_dir(batch_id: str) -> Path:
    base = (PROJECT_ROOT / RUN_ROOT).resolve()
    path = resolve_under(base, base / batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def upload_dir(batch_id: str) -> Path:
    base = (PROJECT_ROOT / UPLOAD_ROOT).resolve()
    path = resolve_under(base, base / batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def write_artifact(batch_id: str, name: str, payload: dict[str, Any]) -> None:
    path = run_dir(batch_id) / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_artifact(batch_id: str, name: str) -> dict[str, Any] | None:
    path = run_dir(batch_id) / name
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def source_id_for(source: SourceInput, text: str, filename: str = "") -> str:
    explicit = safe_text(source.source_id).strip()
    if explicit:
        return explicit
    prefix = safe_text(source.source_name).strip() or Path(filename).stem or source.input_type
    prefix = re.sub(r"\s+", "_", prefix.strip())[:64] or "visual_source"
    return f"{prefix}__{sha1_text(text)[:12]}"


def parse_json_documents(value: Any, source: SourceInput) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else [value]
    documents: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if isinstance(item, dict):
            title = safe_text(item.get("title") or item.get("name") or item.get("product_name") or f"JSON 文档 {index + 1}")
            content = safe_text(item.get("content") or item.get("text") or item.get("body") or json.dumps(item, ensure_ascii=False))
            metadata = {k: v for k, v in item.items() if k not in {"content", "text", "body"}}
        else:
            title = f"JSON 文档 {index + 1}"
            content = safe_text(item)
            metadata = {}
        documents.append(
            {
                "document_id": f"doc_{index + 1:04d}",
                "title": title,
                "source_id": source_id_for(source, content),
                "text": content,
                "text_length": len(content),
                "metadata": {**source.metadata, **metadata},
            }
        )
    return documents


def parse_csv(path: Path, source: SourceInput) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as file_obj:
        reader = csv.DictReader(file_obj)
        rows = list(reader)
    documents = []
    for index, row in enumerate(rows):
        title = safe_text(row.get("title") or row.get("name") or row.get("产品名称") or f"CSV 行 {index + 1}")
        content = "\n".join(f"{key}: {value}" for key, value in row.items() if safe_text(value).strip())
        documents.append(
            {
                "document_id": f"doc_{index + 1:04d}",
                "title": title,
                "source_id": source_id_for(source, content, path.name),
                "text": content,
                "text_length": len(content),
                "metadata": {**source.metadata, "row_index": index + 1, "headers": reader.fieldnames or []},
            }
        )
    if not rows:
        warnings.append("CSV 没有数据行。")
    return documents, warnings


def parse_excel(path: Path, source: SourceInput) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if path.suffix.lower() == ".xls":
        return [], ["当前环境不保证支持 .xls 解析；请转为 .xlsx 后重试。"]
    try:
        import openpyxl  # type: ignore
    except Exception as exc:
        return [], [f"openpyxl 不可用，无法解析 Excel：{exc}"]
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    documents: list[dict[str, Any]] = []
    doc_index = 0
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        headers = [safe_text(value).strip() or f"col_{idx + 1}" for idx, value in enumerate(rows[0])]
        for row_number, row in enumerate(rows[1:], start=2):
            values = {headers[idx]: row[idx] for idx in range(min(len(headers), len(row))) if safe_text(row[idx]).strip()}
            if not values:
                continue
            doc_index += 1
            title = safe_text(values.get("产品名称") or values.get("名称") or values.get("title") or f"{sheet.title} 第 {row_number} 行")
            content = "\n".join(f"{key}: {value}" for key, value in values.items())
            documents.append(
                {
                    "document_id": f"doc_{doc_index:04d}",
                    "title": title,
                    "source_id": source_id_for(source, content, path.name),
                    "text": content,
                    "text_length": len(content),
                    "metadata": {**source.metadata, "sheet": sheet.title, "row_number": row_number, "headers": headers},
                }
            )
    if not documents:
        warnings.append("Excel 未解析出可预览文档。")
    return documents, warnings


def parse_pdf(path: Path, source: SourceInput) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:
        return [], [f"pypdf 不可用，无法解析 PDF：{exc}"]
    warnings: list[str] = []
    reader = PdfReader(str(path))
    documents: list[dict[str, Any]] = []
    for page_index, page in enumerate(reader.pages):
        text = safe_text(page.extract_text()).strip()
        if not text:
            continue
        documents.append(
            {
                "document_id": f"doc_{page_index + 1:04d}",
                "title": f"{path.stem} 第 {page_index + 1} 页",
                "source_id": source_id_for(source, text, path.name),
                "text": text,
                "text_length": len(text),
                "metadata": {**source.metadata, "page": page_index + 1},
            }
        )
    if not documents:
        warnings.append("PDF 未抽取到文本。")
    return documents, warnings


def parse_source_documents(source: SourceInput) -> tuple[list[dict[str, Any]], list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    raw_summary: dict[str, Any] = {"input_type": source.input_type}
    if source.input_type == "json":
        try:
            payload = source.raw_json if source.raw_json is not None else json.loads(source.raw_text or "")
        except Exception as exc:
            return [], warnings, [f"JSON 解析失败：{exc}"], raw_summary
        raw_summary["json_type"] = type(payload).__name__
        return parse_json_documents(payload, source), warnings, errors, raw_summary
    if source.input_type == "text":
        text = safe_text(source.raw_text).strip()
        if not text:
            return [], warnings, ["文本输入为空。"], raw_summary
        return [
            {
                "document_id": "doc_0001",
                "title": safe_text(source.source_name).strip() or "文本输入",
                "source_id": source_id_for(source, text),
                "text": text,
                "text_length": len(text),
                "metadata": source.metadata,
            }
        ], warnings, errors, {"input_type": "text", "text_preview": truncate(text, 240), "text_length": len(text)}
    if source.input_type == "file":
        if not source.file_path:
            return [], warnings, ["缺少 file_path。"], raw_summary
        path = resolve_under((PROJECT_ROOT / UPLOAD_ROOT).resolve(), Path(source.file_path))
        suffix = validate_extension(path.name)
        raw_summary.update({"filename": path.name, "size": path.stat().st_size, "sha1": sha1_bytes(path.read_bytes())})
        if suffix in {".txt", ".md"}:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                return [], warnings, ["文件文本为空。"], raw_summary
            return [
                {
                    "document_id": "doc_0001",
                    "title": safe_text(source.source_name).strip() or path.name,
                    "source_id": source_id_for(source, text, path.name),
                    "text": text,
                    "text_length": len(text),
                    "metadata": {**source.metadata, "filename": path.name},
                }
            ], warnings, errors, raw_summary
        if suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                return [], warnings, [f"JSON 文件解析失败：{exc}"], raw_summary
            return parse_json_documents(payload, source), warnings, errors, raw_summary
        if suffix == ".csv":
            documents, parser_warnings = parse_csv(path, source)
            return documents, warnings + parser_warnings, errors, raw_summary
        if suffix in {".xlsx", ".xls"}:
            documents, parser_warnings = parse_excel(path, source)
            return documents, warnings + parser_warnings, errors, raw_summary
        if suffix == ".pdf":
            documents, parser_warnings = parse_pdf(path, source)
            return documents, warnings + parser_warnings, errors, raw_summary
    return [], warnings, [f"不支持的 input_type：{source.input_type}"], raw_summary


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = safe_text(text).strip()
    if not text:
        return []
    size = max(100, int(chunk_size or DEFAULT_CHUNK_SIZE))
    step = max(1, size - max(0, min(overlap, size - 1)))
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += step
    return chunks


def build_chunks(parse_result: dict[str, Any], config: IngestConfig) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    duplicate_hashes: set[str] = set()
    empty_count = 0
    too_short = 0
    too_long = 0
    for document in parse_result.get("documents") or []:
        for index, text in enumerate(chunk_text(document.get("text", ""), config.chunk_size, config.overlap)):
            digest = sha1_text(text)
            if digest in seen_hashes:
                duplicate_hashes.add(digest)
            seen_hashes.add(digest)
            if not text.strip():
                empty_count += 1
            if len(text) < 20:
                too_short += 1
            if len(text) > max(config.chunk_size, DEFAULT_CHUNK_SIZE) * 1.2:
                too_long += 1
            chunks.append(
                {
                    "chunk_id": f"{document.get('document_id')}__chunk_{index + 1:04d}",
                    "document_id": document.get("document_id"),
                    "source_id": document.get("source_id"),
                    "text": text,
                    "char_count": len(text),
                    "hash": digest,
                    "metadata": document.get("metadata") or {},
                    "flags": {
                        "empty": not text.strip(),
                        "too_short": len(text) < 20,
                        "too_long": len(text) > max(config.chunk_size, DEFAULT_CHUNK_SIZE) * 1.2,
                        "duplicate": digest in duplicate_hashes,
                    },
                }
            )
    return {
        "chunk_count": len(chunks),
        "chunks": chunks,
        "stats": {
            "empty_text": empty_count,
            "too_short": too_short,
            "too_long": too_long,
            "duplicate_hashes": len(duplicate_hashes),
        },
    }


def preflight(parse_result: dict[str, Any], chunk_result: dict[str, Any], config: IngestConfig, batch_id: str | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    add("batch_id", "pass" if batch_id else "blocked", "batch_id 已生成" if batch_id else "缺少 batch_id")
    target = config.target or {}
    target_known = bool(target.get("index") or target.get("collection") or target.get("namespace"))
    add("target", "pass" if target_known else "blocked", "目标知识库已设置" if target_known else "目标知识库未知")
    add("run_mode", "pass" if config.run_mode == "dry_run" else "blocked", "v1 仅允许 dry-run" if config.run_mode != "dry_run" else "当前为 dry-run")
    add("overwrite", "warning" if config.overwrite else "pass", "v1 不执行覆盖，仅提示风险" if config.overwrite else "未开启覆盖")
    documents = parse_result.get("documents") or []
    chunks = chunk_result.get("chunks") or []
    add("documents", "pass" if documents else "blocked", f"解析文档数：{len(documents)}")
    add("chunks", "pass" if chunks else "blocked", f"chunk 数：{len(chunks)}")
    missing_source = sum(1 for chunk in chunks if not safe_text(chunk.get("source_id")).strip())
    add("source_id", "pass" if missing_source == 0 else "blocked", f"缺少 source_id 的 chunk：{missing_source}")
    empty_text = int((chunk_result.get("stats") or {}).get("empty_text") or 0)
    add("empty_text", "pass" if empty_text == 0 else "blocked", f"空文本 chunk：{empty_text}")
    duplicate_hashes = int((chunk_result.get("stats") or {}).get("duplicate_hashes") or 0)
    add("duplicate_hash", "warning" if duplicate_hashes else "pass", f"重复 chunk hash：{duplicate_hashes}")
    blocked = [item for item in checks if item["status"] == "blocked"]
    warnings = [item for item in checks if item["status"] == "warning"]
    status = "blocked" if blocked else ("warning" if warnings else "pass")
    return {
        "status": status,
        "checks": checks,
        "will_call_embedding": False,
        "will_write_vector_db": False,
        "will_push": False,
        "will_delete": False,
        "will_overwrite": False,
        "target": config.target,
    }


def run_report(batch_id: str, request: VisualIngestRequest) -> dict[str, Any]:
    parse_result = request.parse_result or read_artifact(batch_id, "parse_result.json") or {}
    chunk_result = request.chunk_result or read_artifact(batch_id, "chunk_result.json") or {}
    preflight_result = request.preflight_result or read_artifact(batch_id, "preflight_result.json") or {}
    if request.config.run_mode != "dry_run":
        status = "blocked"
        message = "v1 页面暂不开放真实入库，请走后续确认版本"
    elif preflight_result.get("status") == "blocked":
        status = "blocked"
        message = "preflight blocked，禁止执行入库"
    else:
        status = "success"
        message = "dry-run 完成，未写入向量库"
    return {
        "batch_id": batch_id,
        "status": status,
        "message": message,
        "run_mode": request.config.run_mode,
        "import_mode": request.config.import_mode,
        "knowledge_action": request.config.knowledge_action,
        "target": request.config.target,
        "parse_summary": {
            "document_count": len(parse_result.get("documents") or []),
            "errors": parse_result.get("errors") or [],
            "warnings": parse_result.get("warnings") or [],
        },
        "chunk_summary": {
            "chunk_count": chunk_result.get("chunk_count") or 0,
            "stats": chunk_result.get("stats") or {},
        },
        "preflight_status": preflight_result.get("status") or "unknown",
        "answer_called": False,
        "embedding_called": False,
        "vector_db_written": False,
        "push_executed": False,
        "delete_executed": False,
        "overwrite_executed": False,
    }


@router.post("/upload")
def upload_file(request: UploadRequest) -> dict[str, Any]:
    try:
        filename = sanitize_filename(request.filename)
        validate_extension(filename)
        data = base64.b64decode(request.content_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    batch_id = request.batch_id or make_batch_id()
    target_dir = upload_dir(batch_id)
    target = resolve_under((PROJECT_ROOT / UPLOAD_ROOT).resolve(), target_dir / filename)
    if target.exists():
        raise HTTPException(status_code=409, detail="uploaded file already exists for this batch")
    target.write_bytes(data)
    payload = {
        "batch_id": batch_id,
        "filename": filename,
        "path": str(target),
        "size": target.stat().st_size,
        "sha1": sha1_bytes(data),
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
    }
    write_artifact(batch_id, "upload_result.json", payload)
    return payload


@router.post("/parse")
def parse_source(request: VisualIngestRequest) -> dict[str, Any]:
    batch_id = request.batch_id or make_batch_id()
    documents, warnings, errors, raw_summary = parse_source_documents(request.source_input)
    payload = {
        "batch_id": batch_id,
        "created_at": now_iso(),
        "raw_summary": raw_summary,
        "document_count": len(documents),
        "documents": documents,
        "warnings": warnings,
        "errors": errors,
        "writes_vector_db": False,
        "calls_embedding": False,
    }
    write_artifact(batch_id, "parse_result.json", payload)
    return payload


@router.post("/chunks/preview")
def preview_chunks(request: VisualIngestRequest) -> dict[str, Any]:
    batch_id = request.batch_id or make_batch_id()
    parse_result = request.parse_result or read_artifact(batch_id, "parse_result.json")
    if not parse_result:
        raise HTTPException(status_code=400, detail="missing parse_result")
    payload = build_chunks(parse_result, request.config)
    payload.update({"batch_id": batch_id, "created_at": now_iso(), "writes_vector_db": False, "calls_embedding": False})
    write_artifact(batch_id, "chunk_result.json", payload)
    return payload


@router.post("/preflight")
def preflight_ingest(request: VisualIngestRequest) -> dict[str, Any]:
    batch_id = request.batch_id or make_batch_id()
    parse_result = request.parse_result or read_artifact(batch_id, "parse_result.json") or {}
    chunk_result = request.chunk_result or read_artifact(batch_id, "chunk_result.json") or {}
    payload = preflight(parse_result, chunk_result, request.config, batch_id)
    payload.update({"batch_id": batch_id, "created_at": now_iso()})
    write_artifact(batch_id, "preflight_result.json", payload)
    return payload


@router.post("/run")
def run_ingest(request: VisualIngestRequest) -> dict[str, Any]:
    batch_id = request.batch_id or make_batch_id()
    payload = run_report(batch_id, request)
    payload["created_at"] = now_iso()
    write_artifact(batch_id, "run_result.json", payload)
    return payload


@router.post("/search-verify")
def search_verify(request: SearchVerifyRequest) -> dict[str, Any]:
    from rag_api import SearchRequest, rag_search  # Lazy import avoids circular import at app startup.

    payload = rag_search(SearchRequest(query=request.query, top_k=request.top_k))
    if isinstance(payload, dict):
        payload.setdefault("called_answer", False)
        payload.setdefault("called_llm", False)
    return payload


@router.post("/answer-verify")
def answer_verify(request: AnswerVerifyRequest) -> dict[str, Any]:
    if not request.answer_verify or request.confirmation != "YES_ANSWER":
        return {
            "ok": False,
            "blocked": True,
            "message": "answer 验证默认关闭；如需调用 LLM，请显式开启并输入 YES_ANSWER。",
            "called_answer": False,
            "called_llm": False,
        }
    from rag_api import AnswerRequest, rag_answer  # Lazy import avoids circular import at app startup.

    payload = rag_answer(AnswerRequest(query=request.query, top_k=request.top_k, debug=False))
    if isinstance(payload, dict):
        payload["called_answer"] = True
        payload["called_llm"] = True
    return payload


@router.get("/runs/{batch_id}")
def get_run(batch_id: str) -> dict[str, Any]:
    path = run_dir(batch_id)
    artifacts = {}
    for artifact in [
        "upload_result.json",
        "parse_result.json",
        "chunk_result.json",
        "preflight_result.json",
        "run_result.json",
    ]:
        artifacts[artifact] = read_artifact(batch_id, artifact)
    return {"batch_id": batch_id, "run_dir": str(path), "artifacts": artifacts}
