"""Unauthenticated, read-only access to published company knowledge."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .knowledge_service import KnowledgeService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["company-knowledge"])


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(10, ge=1, le=30, strict=True)
    include_content: bool = Field(True, strict=True)
    department: Optional[str] = Field(None, min_length=1, max_length=200)
    scenario: Optional[str] = Field(None, min_length=1, max_length=200)
    model_config = {"extra": "forbid"}


def get_knowledge_service() -> KnowledgeService:
    return KnowledgeService()


@router.post("/search")
def search_knowledge(request: KnowledgeSearchRequest, service: KnowledgeService = Depends(get_knowledge_service)):
    try:
        results = service.search(
            request.query, request.top_k, include_content=request.include_content,
            department=request.department, scenario=request.scenario,
        )
        return {"ok": True, "query": request.query.strip(), "result_count": len(results), "results": results}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        # SDK exceptions may include URLs, credentials or source content.
        logger.warning("Company knowledge search failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="公司知识检索暂不可用，请检查服务配置和知识索引") from None


@router.get("/{knowledge_id}")
def get_knowledge(
    knowledge_id: str, revision: Optional[int] = Query(None, ge=1, le=2**63 - 1),
    service: KnowledgeService = Depends(get_knowledge_service),
):
    try:
        record = service.get(knowledge_id, revision=revision)
    except (ValueError, KeyError):
        raise HTTPException(status_code=404, detail="知识不存在或尚未发布") from None
    except Exception as exc:
        logger.warning("Company knowledge read failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="公司知识读取暂不可用") from None
    if record is None:
        raise HTTPException(status_code=404, detail="知识不存在或尚未发布")
    return {"ok": True, "knowledge": record}
