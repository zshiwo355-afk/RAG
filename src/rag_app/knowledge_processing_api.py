"""Private processing API, called only by the authenticated MCP portal gateway."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictInt

from .knowledge_processing import ProcessingError, ProcessingService, ProcessingStore
from .knowledge_graph import KnowledgeGraph
from .knowledge_receipts_api import authenticated_principal
from .knowledge_store import KnowledgeStore
from .knowledge_api import KnowledgeSearchRequest, search_knowledge
from .knowledge_service import KnowledgeService


READ_SELF = "company_knowledge.submissions.read"
READ_KNOWLEDGE = "company_knowledge.read"
DASHBOARD = "company_knowledge.dashboard.read"
REVIEW = "company_knowledge.review"
PERMISSIONS = {READ_SELF, READ_KNOWLEDGE, DASHBOARD, REVIEW}
Scope = Literal["self", "company"]
JobStatus = Literal["queued", "running", "completed", "needs_review", "failed"]
ReceiptStatus = Literal["awaiting_upload", "pending_verification", "verified", "rejected", "failed"]


class ProcessingRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                raise HTTPException(status_code=422, detail="invalid_processing_request") from None
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return safe_handler


router = APIRouter(prefix="/api/rag/knowledge-processing", tags=["knowledge-processing"], route_class=ProcessingRoute)
_graph = KnowledgeGraph()


class RetryRequest(BaseModel):
    model_config = {"extra": "forbid"}


class ResolveRequest(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: StrictInt = Field(ge=1, le=2147483647)
    action: Literal["archive", "replace"]
    reason: str = Field(min_length=1, max_length=500)
    replacement_receipt_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def portal_caller(principal: str = Depends(authenticated_principal),
                  x_knowledge_permissions: str = Header(default="")):
    if os.getenv("KNOWLEDGE_PROCESSING_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="processing_disabled")
    # The internal bearer authenticates the gateway before trusting its assertions.
    permissions = x_knowledge_permissions.split(",") if x_knowledge_permissions else []
    if (not permissions or len(permissions) != len(set(permissions))
            or not set(permissions).issubset(PERMISSIONS)):
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return principal, frozenset(permissions)


def get_processing_store():
    try:
        return ProcessingStore()
    except Exception:
        raise HTTPException(status_code=503, detail="processing_unavailable") from None


def get_catalog():
    try:
        return KnowledgeStore()
    except Exception:
        raise HTTPException(status_code=503, detail="knowledge_unavailable") from None


def get_processing_service(store=Depends(get_processing_store)):
    return ProcessingService(store=store)


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except ProcessingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    except Exception:
        raise HTTPException(status_code=503, detail="processing_unavailable") from None


def _principal(caller, scope, *, summary=False):
    principal, permissions = caller
    if scope == "company":
        required = {REVIEW, DASHBOARD} if summary else {REVIEW}
        if not permissions.intersection(required):
            raise HTTPException(status_code=403, detail="processing_permission_required")
        return None
    if not permissions.intersection({READ_SELF, REVIEW}):
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return principal


def _iso(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _public_job(job):
    # Explicit fields keep object keys, lease tokens and owner IDs off the wire.
    result = {key: job.get(key) for key in (
        "job_id", "receipt_id", "source_id", "filename", "rule_version", "status", "stage",
        "attempts", "error_code", "retryable", "counts",
    )}
    result.update(created_at=_iso(job.get("created_at")), updated_at=_iso(job.get("updated_at")), published=job.get("published") is True)
    for key in ("item_total", "event_total", "item_offset", "event_offset", "limit"):
        if key in job:
            result[key] = job[key]
    if "items" in job:
        result["items"] = [{key: item.get(key) for key in (
            "item_id", "source_path", "status", "reason_codes", "knowledge_id", "revision", "title", "has_preview",
            "published", "version", "replacement_receipt_id", "replacement_job_id",
        )} for item in job["items"]]
    if "events" in job:
        result["events"] = [{
            **{key: event.get(key) for key in ("event_id", "event_type", "reason_code")},
            "created_at": _iso(event.get("created_at")),
        } for event in job["events"]]
    return result


CATALOG_KINDS = {
    "sop": "SOP / 流程", "skill": "Skill", "case": "案例", "method": "方法",
    "template": "模板", "prompt": "提示词", "policy": "规则", "reference": "参考", "other": "其他",
}


def _catalog_kind(item):
    value = str(item.get("kind", "")).strip().lower()
    if value == "method" and item.get("source_type") == "Skill方法":
        return "skill"
    value = {"process": "sop", "流程": "sop", "案例": "case", "reference_case": "case",
             "reference_overview": "reference", "方法": "method", "模板": "template",
             "提示词": "prompt", "规则": "policy", "参考": "reference"}.get(value, value)
    return value if value in CATALOG_KINDS else "other"


def _require_catalog(caller):
    if not caller[1].intersection({READ_KNOWLEDGE, REVIEW}):
        raise HTTPException(status_code=403, detail="processing_permission_required")


@router.post("/search")
def semantic_search(body: KnowledgeSearchRequest, request: Request,
                    caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    if request.query_params:
        raise HTTPException(status_code=422, detail="invalid_processing_request")
    return search_knowledge(body, KnowledgeService(store=catalog))


@router.get("/graph")
def knowledge_graph(request: Request, caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    if request.query_params:
        raise HTTPException(status_code=422, detail="invalid_processing_request")
    return _call(_graph.read, catalog, _catalog_kind)


@router.get("/catalog")
def published_catalog(limit: int = Query(default=50, ge=1, le=100),
                      offset: int = Query(default=0, ge=0, le=1_000_000),
                      q: Optional[str] = Query(default=None, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"),
                      kind: Optional[str] = Query(default=None, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"),
                      uploader_position: Optional[str] = Query(default=None, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"),
                      caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    # One metadata snapshot keeps the global aggregates consistent with this page.
    records = _call(catalog.published_catalog)
    items = []
    for record in records:
        category = _catalog_kind(record)
        items.append({**{key: record[key] for key in (
            "knowledge_id", "title", "revision", "published_at", "uploader_position",
            "scenarios", "related_knowledge_ids",
        )}, "kind": category, "kind_label": CATALOG_KINDS[category],
            "source_kind": record["kind"], "summary": None})
    kind_counts = Counter(item["kind"] for item in items)
    position_counts = Counter(item["uploader_position"] for item in items)
    result = {
        "counts": {"published_assets": len(items), "positions": sum(key is not None for key in position_counts),
                   "unknown_position": position_counts[None]},
        "by_kind": [{"key": key, "label": label, "count": kind_counts[key]}
                    for key, label in CATALOG_KINDS.items()],
        "by_uploader_position": [{"key": key or "unknown", "label": key or "待补充岗位", "count": count}
                                 for key, count in position_counts.items()],
        "generated_at": _iso(datetime.now(timezone.utc).timestamp()),
    }
    query = (q or "").strip().casefold()
    filtered = [item for item in items
                if (not query or query in item["title"].casefold())
                and (not kind or item["kind"] == kind)
                and (not uploader_position or (item["uploader_position"] or "unknown") == uploader_position)]
    return {**result, "items": filtered[offset:offset + limit], "total": len(filtered),
            "limit": limit, "offset": offset}


@router.get("/catalog/{knowledge_id}")
def published_detail(knowledge_id: str = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"),
                     caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    record = _call(catalog.get_published, knowledge_id)
    if record is None:
        raise HTTPException(status_code=404, detail="published_knowledge_not_found")
    category = _catalog_kind({**record, "source_type": record.get("evidence", {}).get("source_type")})
    return {**{key: record[key] for key in (
        "knowledge_id", "title", "revision", "published_at", "content",
    )}, "kind": category, "kind_label": CATALOG_KINDS[category],
        "source_kind": record.get("kind", "未知"), "scenarios": record.get("scenarios", []),
        "uploader_position": None, "summary": None}


@router.get("/overview")
def overview(scope: Scope = "self", caller=Depends(portal_caller),
             store=Depends(get_processing_store), catalog=Depends(get_catalog)):
    principal = _principal(caller, scope, summary=True)
    counts = _call(store.stats, principal)
    receipt_counts = counts.pop("receipt_counts")
    counts["published_assets"] = (
        _call(catalog.published_count) if caller[1].intersection({READ_KNOWLEDGE, DASHBOARD, REVIEW}) else None
    )
    return {"scope": scope, "counts": counts, "receipt_counts": receipt_counts,
            "generated_at": _iso(datetime.now(timezone.utc).timestamp())}


@router.get("/receipts")
def receipts(request: Request, scope: Scope = "self", status: Optional[ReceiptStatus] = None,
             limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000),
             caller=Depends(portal_caller), store=Depends(get_processing_store)):
    principal = _principal(caller, scope)
    pairs = request.query_params.multi_items()
    if len(pairs) != len(dict(pairs)) or not set(dict(pairs)) <= {"scope", "status", "limit", "offset"}:
        raise HTTPException(status_code=422, detail="invalid_processing_request")
    result = _call(store.list_receipts, principal, status=status, limit=limit, offset=offset)
    items = [{**{key: row.get(key) for key in (
        "receipt_id", "filename", "byte_length", "status", "error_code", "job_id", "job_status")},
        "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at")),
        "content_verified": row["status"] == "verified",
        "retryable": row["status"] in {"awaiting_upload", "pending_verification", "failed"},
    } for row in result["items"]]
    return {"scope": scope, "items": items, "total": result["total"], "limit": limit,
            "offset": offset, "receipt_counts": result["receipt_counts"]}


@router.get("/jobs")
def jobs(scope: Scope = "self", status: Optional[JobStatus] = None,
         limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000),
         caller=Depends(portal_caller), store=Depends(get_processing_store)):
    principal = _principal(caller, scope)
    result = _call(store.list_jobs, principal, status=status, limit=limit, offset=offset)
    return {"items": [_public_job(job) for job in result["items"]], "total": result["total"],
            "limit": limit, "offset": offset, "scope": scope}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str = Path(pattern=r"^[0-9a-f]{32}$"), scope: Scope = "self",
               limit: int = Query(default=100, ge=1, le=100),
               item_offset: int = Query(default=0, ge=0, le=1_000_000),
               event_offset: int = Query(default=0, ge=0, le=1_000_000),
               caller=Depends(portal_caller), store=Depends(get_processing_store)):
    return _public_job(_call(store.get_job, job_id, _principal(caller, scope),
                            limit=limit, item_offset=item_offset, event_offset=event_offset))


@router.post("/jobs/{job_id}/retry")
def retry_job(request: RetryRequest, job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
              caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _public_job(_call(store.retry, job_id, None, actor=caller[0]))


@router.get("/jobs/{job_id}/items/{item_id}")
def item_preview(job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                 item_id: str = Path(pattern=r"^[0-9a-f]{32}$"), scope: Scope = "self",
                 caller=Depends(portal_caller), service=Depends(get_processing_service)):
    result = _call(service.get_item, job_id, item_id, _principal(caller, scope))
    return {key: result.get(key) for key in ("item_id", "title", "content", "knowledge_id", "revision", "published", "currently_published", "version")}


@router.post("/jobs/{job_id}/items/{item_id}/resolve")
def resolve_item(request: ResolveRequest, job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                 item_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                 caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _call(store.resolve, job_id, item_id, None, actor=caller[0], **request.model_dump())
