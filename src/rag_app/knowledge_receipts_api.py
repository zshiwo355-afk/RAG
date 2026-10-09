"""Authenticated MCP-to-RAG original upload control plane."""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator

from .knowledge_receipts import IDENTIFIER, MAX_BYTES, ReceiptError, ReceiptService, validate_payload, validate_principal


class ReceiptRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_validation(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default error includes caller input; receipts return fixed codes only.
                raise HTTPException(status_code=422, detail="invalid_upload_request") from None

        return safe_validation


router = APIRouter(prefix="/api/knowledge-intake/uploads", tags=["knowledge-intake"], route_class=ReceiptRoute)


class UploadRequest(BaseModel):
    idempotency_key: str = Field(..., pattern="^" + IDENTIFIER + "$")
    source_id: str = Field(..., pattern="^" + IDENTIFIER + "$")
    filename: str = Field(..., min_length=1, max_length=200)
    byte_length: int = Field(..., ge=1, le=MAX_BYTES, strict=True)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    model_config = {"extra": "forbid"}

    @field_validator("filename")
    @classmethod
    def basename_only(cls, value):
        try:
            validate_payload({"idempotency_key": "validate", "source_id": "validate", "filename": value,
                              "byte_length": 1, "sha256": "0" * 64})
        except ReceiptError:
            raise ValueError("invalid_filename") from None
        return value


class CompleteRequest(BaseModel):
    model_config = {"extra": "forbid"}


def authenticated_principal(authorization: str = Header(default=""), x_knowledge_principal: str = Header(default="")):
    if os.getenv("KNOWLEDGE_RECEIPTS_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="receipts_disabled")
    token = os.getenv("KNOWLEDGE_RECEIPTS_TOKEN", "")
    if len(token.encode()) < 32 or token != token.strip():
        raise HTTPException(status_code=503, detail="receipt_auth_not_configured")
    if not secrets.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
        raise HTTPException(status_code=401, detail="receipt_auth_required")
    try:
        validate_principal(x_knowledge_principal)
    except ReceiptError:
        raise HTTPException(status_code=400, detail="invalid_principal") from None
    return x_knowledge_principal


def get_receipt_service():
    try:
        return ReceiptService()
    except Exception:
        raise HTTPException(status_code=503, detail="receipts_unavailable") from None


def _call(method, *args):
    try:
        return method(*args)
    except ReceiptError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    except Exception:
        # Neither SDK errors nor signed URLs belong in API errors or logs.
        raise HTTPException(status_code=503, detail="receipts_unavailable") from None


@router.post("")
def prepare_upload(request: UploadRequest, principal: str = Depends(authenticated_principal),
                   service: ReceiptService = Depends(get_receipt_service)):
    return _call(service.prepare, principal, request.model_dump())


@router.post("/{receipt_id}/complete")
def complete_upload(receipt_id: str, request: CompleteRequest, principal: str = Depends(authenticated_principal),
                    service: ReceiptService = Depends(get_receipt_service)):
    return _call(service.complete, principal, receipt_id)


@router.get("/{receipt_id}")
def upload_status(receipt_id: str, principal: str = Depends(authenticated_principal),
                  service: ReceiptService = Depends(get_receipt_service)):
    return _call(service.status, principal, receipt_id)
