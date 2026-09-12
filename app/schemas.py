"""Pydantic request and response models.

Declaring the response shapes is what makes /docs accurate without extra work, so
these are the single source of truth for the API contract rather than something
assembled ad hoc in the views.
"""

from __future__ import annotations

import datetime as dt
from http import HTTPStatus

from pydantic import BaseModel, Field

from app.models import HarvestedURL, HarvestJob, PersonRecord, UploadBatch


def status_text(status: int | None) -> str:
    if status is None:
        return ""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


#: Plain-language labels for the crawl badges, so the UI does not have to own the
#: mapping and the API stays self-describing for anyone reading JSON by hand.
_CRAWL_LABELS = {
    "SUCCESS": "Crawled",
    "PARTIAL": "Partial content",
    "BLOCKED": "Blocked by site",
    "FAILED": "Failed",
    "SKIPPED": "Skipped",
    "PENDING": "Queued",
    "PROCESSING": "Crawling",
}


def crawl_status_label(status: str) -> str:
    return _CRAWL_LABELS.get((status or "").upper(), status or "")


# --- harvested URLs --------------------------------------------------------


class HarvestedURLOut(BaseModel):
    """A harvested URL.

    raw_html and text_content are omitted by default and their lengths reported
    instead: a single leadership page can be 200 KB of markup, and returning that
    for every row would make the list response unusable. Pass include_html=1 /
    include_content=1, or fetch the detail endpoint, which always includes them.
    """

    id: int
    url: str
    final_url: str
    canonical_url: str
    domain: str
    http_status: int | None
    http_status_text: str
    #: SUCCESS | PARTIAL | BLOCKED | FAILED | SKIPPED | PENDING | PROCESSING.
    #: A 200 is not enough to call a page a success - see app.services.content_quality.
    crawl_status: str
    crawl_status_label: str
    #: Which extractor produced the text: trafilatura | dom | document:pdf |
    #: playwright+trafilatura.
    scraping_method: str
    #: The reason this row is not a clean success, empty when it is one.
    failure_reason: str
    crawl_depth: int
    parent_url: str
    content_type: str
    title: str
    meta_description: str
    metadata: dict = Field(default_factory=dict)
    fetched_at: dt.datetime | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    fetch_ms: int | None
    error: str
    batch_id: int
    raw_html_length: int
    text_length: int
    word_count: int
    chunk_count: int
    person_count: int
    raw_html: str | None = None
    text_content: str | None = None


class HarvestedURLPage(BaseModel):
    count: int
    page: int
    page_size: int
    pages: int
    results: list[HarvestedURLOut]


def to_url_out(
    row: HarvestedURL, include_html: bool = False, include_content: bool = False
) -> HarvestedURLOut:
    return HarvestedURLOut(
        id=row.id,
        url=row.url,
        final_url=row.final_url or row.url,
        canonical_url=row.canonical_url or "",
        domain=row.domain or "",
        http_status=row.http_status,
        http_status_text=status_text(row.http_status),
        crawl_status=row.crawl_status or "PENDING",
        crawl_status_label=crawl_status_label(row.crawl_status),
        scraping_method=row.scraping_method or "",
        failure_reason=row.failure_reason,
        crawl_depth=row.crawl_depth or 0,
        parent_url=row.parent_url or "",
        content_type=row.content_type or "",
        title=row.title or "",
        meta_description=row.meta_description or "",
        metadata=row.meta_json or {},
        fetched_at=row.fetched_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        fetch_ms=row.fetch_ms,
        error=row.error or "",
        batch_id=row.batch_id,
        raw_html_length=len(row.raw_html or ""),
        text_length=len(row.text_content or ""),
        word_count=row.word_count or 0,
        chunk_count=row.chunk_count,
        person_count=row.person_count,
        raw_html=row.raw_html if include_html else None,
        text_content=row.text_content if include_content else None,
    )


# --- batches and jobs ------------------------------------------------------


class BatchOut(BaseModel):
    id: int
    original_filename: str
    uploaded_at: dt.datetime
    row_count: int
    url_count: int
    skipped_rows: int
    url_column: str
    headers: list
    status: str
    notes: str
    rejections: dict = Field(default_factory=dict)


def to_batch_out(batch: UploadBatch) -> BatchOut:
    return BatchOut(
        id=batch.id,
        original_filename=batch.original_filename,
        uploaded_at=batch.uploaded_at,
        row_count=batch.row_count,
        url_count=batch.url_count,
        skipped_rows=batch.skipped_rows,
        url_column=batch.url_column or "",
        headers=batch.headers or [],
        status=batch.status,
        notes=batch.notes or "",
        rejections=batch.rejected_json or {},
    )


class JobOut(BaseModel):
    id: int
    batch_id: int | None
    kind: str
    status: str
    total: int
    processed: int
    succeeded: int
    failed: int
    percent: int
    message: str
    traceback: str
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


def to_job_out(job: HarvestJob) -> JobOut:
    return JobOut(
        id=job.id,
        batch_id=job.batch_id,
        kind=job.kind,
        status=job.status,
        total=job.total,
        processed=job.processed,
        succeeded=job.succeeded,
        failed=job.failed,
        percent=job.percent,
        message=job.message or "",
        traceback=job.traceback or "",
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


# --- people ----------------------------------------------------------------


class PersonOut(BaseModel):
    id: int
    name: str
    title: str
    company: str
    location: str
    bio: str
    email: str
    phone: str
    linkedin_url: str
    confidence: float | None
    source_url: str
    url_id: int


def to_person_out(record: PersonRecord, source_url: str = "") -> PersonOut:
    return PersonOut(
        id=record.id,
        name=record.name or "",
        title=record.title or "",
        company=record.company or "",
        location=record.location or "",
        bio=record.bio or "",
        email=record.email or "",
        phone=record.phone or "",
        linkedin_url=record.linkedin_url or "",
        confidence=record.confidence,
        source_url=source_url or (record.url.url if record.url else ""),
        url_id=record.url_id,
    )


# --- upload ----------------------------------------------------------------


class UploadResult(BaseModel):
    batch_id: int
    url_count: int
    row_count: int
    skipped_rows: int
    url_column: str
    headers: list[str]
    harvest_job_id: int
    # The validation breakdown, so the UI can report what happened to every row
    # instead of only how many survived.
    valid_urls: int = 0
    invalid_urls: int = 0
    duplicate_urls: int = 0
    empty_rows: int = 0
    #: reason -> count, e.g. {"'localhost' is a local or private address": 2}
    rejections: dict[str, int] = Field(default_factory=dict)
    summary: str = ""


class UploadError(BaseModel):
    error: str
    headers: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)


# --- search ----------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None
    include_llm: bool = True


class MatchedChunk(BaseModel):
    chunk_id: int
    score: float
    heading: str
    text: str


class SearchResultOut(BaseModel):
    url_id: int
    url: str
    final_url: str
    title: str
    http_status: int | None
    score: float
    chunk_id: int
    heading: str
    text: str
    matched_chunks: list[MatchedChunk]
    people: list[PersonOut]


class SourceOut(BaseModel):
    index: int
    url: str
    title: str


class SearchResponse(BaseModel):
    query: str
    answer: str
    llm_used: bool
    provider: str
    low_confidence: bool
    note: str
    latency_ms: int
    sources: list[SourceOut]
    results: list[SearchResultOut]
    people: list[PersonOut]


# --- misc ------------------------------------------------------------------


class StatsOut(BaseModel):
    batches: int
    urls: int
    chunks: int
    people: int
    vectors: int
    index_version: str | None
    # What the vector index actually is, so "is this really FAISS, and is it
    # cosine?" is answerable from the API rather than from the source.
    index_type: str
    index_metric: str
    index_dim: int
    embedding_model: str
    llm_provider: str
