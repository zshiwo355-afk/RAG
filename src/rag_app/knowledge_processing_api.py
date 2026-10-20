"""Private processing API, called only by the authenticated MCP portal gateway."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictBool, StrictInt

from .knowledge_processing import ProcessingError, ProcessingService, ProcessingStore, UPLOAD_ROLE_METADATA
from .knowledge_graph import KnowledgeGraph
from .knowledge_receipts_api import authenticated_principal
from .knowledge_store import KnowledgeStore, knowledge_category
from .knowledge_api import KnowledgeSearchRequest, search_knowledge
from .knowledge_service import KnowledgeService


READ_SELF = "company_knowledge.submissions.read"
READ_KNOWLEDGE = "company_knowledge.read"
DASHBOARD = "company_knowledge.dashboard.read"
REVIEW = "company_knowledge.review"
PERMISSIONS = {READ_SELF, READ_KNOWLEDGE, DASHBOARD, REVIEW}
Scope = Literal["self", "company"]
JobStatus = Literal["queued", "running", "completed", "needs_review", "failed"]
JobFilter = Literal[JobStatus, "attention", "pending_publication"]
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


class UploaderRoleRequest(BaseModel):
    model_config = {"extra": "forbid"}
    role_id: str = Field(pattern=r"^role_[0-9a-f]{24}$")
    expected_version: StrictInt = Field(ge=0, le=2147483647)


class ItemUploaderRoleRequest(BaseModel):
    model_config = {"extra": "forbid"}
    role_id: str = Field(pattern=r"^role_[0-9a-f]{24}$")
    expected_version: StrictInt = Field(ge=0, le=2147483647)
    remember_rule: StrictBool = False
    source_action: Literal["auto", "new"] = "auto"


class CreateRoleRequest(BaseModel):
    model_config = {"extra": "forbid"}
    department: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    name: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")


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
        store = ProcessingStore()
        store.display_metadata = UPLOAD_ROLE_METADATA
        return store
    except Exception:
        raise HTTPException(status_code=503, detail="processing_unavailable") from None


def get_catalog():
    try:
        return KnowledgeStore()
    except Exception:
        raise HTTPException(status_code=503, detail="knowledge_unavailable") from None


def get_knowledge_files():
    from .knowledge_files import KnowledgeFiles
    try:
        return KnowledgeFiles()
    except Exception:
        raise HTTPException(status_code=503, detail="knowledge_files_unavailable") from None


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


def _public_job(job, *, uploader=False):
    # Explicit fields keep object keys, lease tokens and owner IDs off the wire.
    result = {key: job.get(key) for key in (
        "job_id", "receipt_id", "source_id", "filename", "rule_version", "status", "stage",
        "attempts", "error_code", "retryable", "counts",
    )}
    result.update(created_at=_iso(job.get("created_at")), updated_at=_iso(job.get("updated_at")), published=job.get("published") is True,
                  display_title=job.get("display_title"), material_count=job.get("material_count", 0),
                  attention_required=bool(job.get("attention_required", job.get("status") in {"needs_review", "failed"})),
                  blocking_reasons=job.get("blocking_reasons", []),
                  blocking_reasons_truncated=job.get("blocking_reasons_truncated", False))
    if uploader:
        result.update(uploader_positions=job.get("uploader_positions", []),
                      uploader_departments=job.get("uploader_departments", []),
                      role_assignments=job.get("role_assignments", []),
                      role_assignments_total=job.get("role_assignments_total", 0),
                      role_assignments_updated_at=job.get("role_assignments_updated_at"))
    for key in ("item_total", "event_total", "item_offset", "event_offset", "limit"):
        if key in job:
            result[key] = job[key]
    if "items" in job:
        result["items"] = [{key: item.get(key) for key in (
            "item_id", "source_path", "status", "reason_codes", "knowledge_id", "revision", "title", "has_preview",
            "published", "version", "replacement_receipt_id", "replacement_job_id", "role_assignment", "current_reason_codes", "rechecking", "missing_references",
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


def _require_catalog(caller=Depends(portal_caller)):
    if not caller[1].intersection({READ_KNOWLEDGE, REVIEW}):
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return caller


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
                      role_id: Optional[str] = Query(default=None, pattern=r"^(role_[0-9a-f]{24}|unknown)$"),
                      category: Optional[Literal["sop", "skill", "other"]] = None,
                      caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    # One metadata snapshot keeps the global aggregates consistent with this page.
    records = _call(catalog.published_catalog)
    items = []
    for record in records:
        kind_key = _catalog_kind(record)
        items.append({**{key: record[key] for key in (
            "knowledge_id", "title", "revision", "published_at", "uploader_position",
            "scenarios", "related_knowledge_ids",
        )}, "kind": kind_key, "kind_label": CATALOG_KINDS[kind_key],
            "role_id": record.get("role_id"), "department": record.get("department"),
            "role_name": record.get("role_name"), "category": knowledge_category(record),
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
                and (role_id is None or (item["role_id"] or "unknown") == role_id)
                and (category is None or item["category"] == category)
                and (not uploader_position or (item["uploader_position"] or "unknown") == uploader_position)]
    return {**result, "items": filtered[offset:offset + limit], "total": len(filtered),
            "limit": limit, "offset": offset}


@router.get("/roles")
def published_roles(caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    return {"items": _call(catalog.roles)}


@router.post("/roles")
def create_role(request: CreateRoleRequest, caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    try:
        role = catalog.register_role(request.department, request.name, existing_department=True)
    except ValueError as error:
        if str(error) == "role_department_not_found":
            raise HTTPException(status_code=404, detail="role_department_not_found") from None
        raise HTTPException(status_code=422, detail="invalid_processing_request") from None
    except Exception:
        raise HTTPException(status_code=503, detail="processing_unavailable") from None
    return next(row for row in _call(catalog.roles) if row["role_id"] == role["role_id"])


@router.get("/catalog/{knowledge_id}")
def published_detail(knowledge_id: str = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"),
                     caller=Depends(portal_caller), catalog=Depends(get_catalog)):
    _require_catalog(caller)
    record = _call(catalog.get_published, knowledge_id)
    if record is None:
        raise HTTPException(status_code=404, detail="published_knowledge_not_found")
    category = _catalog_kind({**record, "source_type": record.get("evidence", {}).get("source_type")})
    from .knowledge_files import public_file_descriptor
    return {**{key: record[key] for key in (
        "knowledge_id", "title", "revision", "published_at", "content",
    )}, "kind": category, "kind_label": CATALOG_KINDS[category],
        "source_kind": record.get("kind", "未知"), "scenarios": record.get("scenarios", []),
        "role_id": record.get("role_id"), "department": record.get("department"),
        "role_name": record.get("role_name"), "category": knowledge_category(record),
        "files": [public_file_descriptor(item) for item in record.get("files", [])],
        "uploader_position": _call(catalog.published_uploader_position, record), "summary": None}


@router.get("/catalog/{knowledge_id}/files/{file_id}")
def published_file(knowledge_id: str = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"),
                   file_id: str = Path(pattern=r"^[A-Za-z0-9_-]{1,80}$"),
                   disposition: Literal["inline", "attachment"] = "attachment",
                   caller=Depends(_require_catalog), catalog=Depends(get_catalog), files=Depends(get_knowledge_files)):
    record = _call(catalog.get_published, knowledge_id)
    descriptor = next((item for item in (record or {}).get("files", []) if item["file_id"] == file_id), None)
    if descriptor is None:
        raise HTTPException(status_code=404, detail="published_file_not_found")
    if disposition == "inline" and (descriptor["purpose"] != "preview" or descriptor["media_type"] != "text/html"):
        raise HTTPException(status_code=422, detail="file_inline_preview_unavailable")
    if disposition == "inline":
        # OSS default domains may force HTML downloads; return verified bytes as
        # JSON for the portal's sandboxed srcdoc instead of navigating to OSS.
        data = _call(files.get, descriptor)
        try:
            result = {"html": data.decode("utf-8"), "expires_in": 300}
        except UnicodeError:
            raise HTTPException(status_code=503, detail="file_preview_unavailable") from None
    else:
        result = _call(files.signed_url, descriptor, disposition=disposition)
    if {"knowledge_id": knowledge_id, "revision": record["revision"]} not in _call(catalog.published_revisions):
        raise HTTPException(status_code=404, detail="published_file_not_found")
    return {**result, "filename": descriptor["filename"], "media_type": descriptor["media_type"]}


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


def list_filters(request: Request,
                 q: Optional[str] = Query(default=None, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"),
                 department: Optional[str] = Query(default=None, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"),
                 role_id: Optional[str] = Query(default=None, pattern=r"^role_[0-9a-f]{24}$"),
                 processing_status: Optional[Literal[JobStatus, "none"]] = None,
                 created_from: Optional[str] = Query(default=None, max_length=40),
                 created_to: Optional[str] = Query(default=None, max_length=40)):
    pairs = request.query_params.multi_items()
    names = {"q", "department", "role_id", "processing_status", "created_from", "created_to"}
    if len(pairs) != len(dict(pairs)) or not set(dict(pairs)) <= names | {"scope", "status", "limit", "offset"}:
        raise HTTPException(status_code=422, detail="invalid_processing_request")
    dates = []
    for value in (created_from, created_to):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None
            if parsed is not None and parsed.utcoffset() is None:
                raise ValueError("timezone required")
            dates.append(parsed)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_processing_request") from None
    if dates[0] is not None and dates[1] is not None and dates[0] >= dates[1]:
        raise HTTPException(status_code=422, detail="invalid_processing_request")
    return {key: value for key, value in (("q", q), ("department", department), ("role_id", role_id),
        ("processing_status", processing_status), ("created_from", created_from), ("created_to", created_to)) if value is not None}


@router.get("/receipts")
def receipts(request: Request, scope: Scope = "self", status: Optional[ReceiptStatus] = None,
             limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000),
             caller=Depends(portal_caller), store=Depends(get_processing_store), filters=Depends(list_filters)):
    principal = _principal(caller, scope)
    result = _call(store.list_receipts, principal, status=status, limit=limit, offset=offset, **filters)
    items = [{**{key: row.get(key) for key in (
        "receipt_id", "filename", "byte_length", "status", "error_code", "job_id", "job_status", "job_stage")},
        "display_title": row.get("display_title"), "material_count": row.get("material_count", 0),
        "uploader_positions": row.get("uploader_positions", []), "uploader_departments": row.get("uploader_departments", []),
        "role_assignments": row.get("role_assignments", []), "role_assignments_total": row.get("role_assignments_total", 0),
        "role_assignments_updated_at": row.get("role_assignments_updated_at"),
        "counts": row.get("counts", {}), "attention_required": bool(row.get("attention_required", False)),
        "blocking_reasons": row.get("blocking_reasons", []),
        "blocking_reasons_truncated": row.get("blocking_reasons_truncated", False),
        "job_updated_at": _iso(row.get("job_updated_at")),
        "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at")),
        "content_verified": row["status"] == "verified",
        "retryable": row["status"] in {"awaiting_upload", "pending_verification", "failed"},
    } for row in result["items"]]
    return {"scope": scope, "items": items, "total": result["total"], "limit": limit,
            "offset": offset, "receipt_counts": result["receipt_counts"]}


@router.get("/jobs")
def jobs(scope: Scope = "self", status: Optional[JobFilter] = None,
         limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000),
         caller=Depends(portal_caller), store=Depends(get_processing_store), filters=Depends(list_filters)):
    principal = _principal(caller, scope)
    if filters.get("processing_status") == "none":
        raise HTTPException(status_code=422, detail="invalid_processing_request")
    result = _call(store.list_jobs, principal, status=status, limit=limit, offset=offset, **filters)
    return {"items": [_public_job(job, uploader=True) for job in result["items"]], "total": result["total"],
            "limit": limit, "offset": offset, "scope": scope}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str = Path(pattern=r"^[0-9a-f]{32}$"), scope: Scope = "self",
               limit: int = Query(default=100, ge=1, le=100),
               item_offset: int = Query(default=0, ge=0, le=1_000_000),
               event_offset: int = Query(default=0, ge=0, le=1_000_000),
               caller=Depends(portal_caller), store=Depends(get_processing_store)):
    return _public_job(_call(store.get_job, job_id, _principal(caller, scope),
                            limit=limit, item_offset=item_offset, event_offset=event_offset), uploader=True)


@router.post("/jobs/{job_id}/retry")
def retry_job(request: RetryRequest, job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
              caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _public_job(_call(store.retry, job_id, None, actor=caller[0]))


@router.get("/jobs/{job_id}/uploader-role")
def get_uploader_role(job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                      caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _call(store.get_uploader_role, job_id)


@router.post("/jobs/{job_id}/uploader-role")
def set_uploader_role(request: UploaderRoleRequest, job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                      caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _call(store.set_uploader_role, job_id, **request.model_dump(), actor=caller[0])


@router.get("/jobs/{job_id}/items/{item_id}")
def item_preview(job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                 item_id: str = Path(pattern=r"^[0-9a-f]{32}$"), scope: Scope = "self",
                 caller=Depends(portal_caller), service=Depends(get_processing_service)):
    result = _call(service.get_item, job_id, item_id, _principal(caller, scope))
    return {key: result.get(key) for key in ("item_id", "title", "content", "knowledge_id", "revision", "published", "currently_published", "version")}


@router.get("/jobs/{job_id}/items/{item_id}/uploader-role")
def get_item_uploader_role(job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                           item_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                           caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _call(store.get_item_uploader_role, job_id, item_id)


@router.post("/jobs/{job_id}/items/{item_id}/uploader-role")
def set_item_uploader_role(request: ItemUploaderRoleRequest,
                           job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                           item_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                           caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _call(store.set_item_uploader_role, job_id, item_id, **request.model_dump(), actor=caller[0])


@router.post("/jobs/{job_id}/items/{item_id}/resolve")
def resolve_item(request: ResolveRequest, job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                 item_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                 caller=Depends(portal_caller), store=Depends(get_processing_store)):
    if REVIEW not in caller[1]:
        raise HTTPException(status_code=403, detail="processing_permission_required")
    return _call(store.resolve, job_id, item_id, None, actor=caller[0], **request.model_dump())
