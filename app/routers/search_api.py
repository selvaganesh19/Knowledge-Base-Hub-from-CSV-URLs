"""Semantic search endpoint, available as both POST and GET.

GET exists so a query can be run from a browser address bar or a plain curl
command during a demo, and so a search is a shareable URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import SearchRequest, SearchResponse
from app.services.search import search as run_search

router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search/", response_model=SearchResponse)
async def search_post(payload: SearchRequest, session: Session = Depends(get_db)) -> SearchResponse:
    """Semantic search over the harvested knowledge base.

    Returns a synthesized answer with citations when an LLM is configured, plus
    the ranked source chunks and extracted person records either way, so the
    endpoint stays useful without a working API key.
    """
    result = await run_search(
        payload.query,
        session=session,
        top_k=payload.top_k,
        use_llm=payload.include_llm,
    )
    return SearchResponse(**result)


@router.get("/search/", response_model=SearchResponse)
async def search_get(
    q: str = Query(..., description="Natural-language query"),
    top_k: int | None = Query(None, ge=1, le=50),
    include_llm: bool = Query(True),
    session: Session = Depends(get_db),
) -> SearchResponse:
    result = await run_search(q, session=session, top_k=top_k, use_llm=include_llm)
    return SearchResponse(**result)
