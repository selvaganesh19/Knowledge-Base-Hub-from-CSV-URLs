"""SQLAlchemy models.

Two ideas shape this schema:

* Harvested pages are stored whole (raw HTML and cleaned text) so the data can be
  inspected and re-processed without re-scraping. Everything downstream - chunks,
  embeddings, person records - is derived and can be rebuilt from this table.
* Long-running work reports progress through a row, not through the request. A
  harvest_jobs row is what a browser polls, which is the only way an in-process
  background task can surface progress at all.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class UploadBatch(Base):
    """One uploaded CSV/XLSX file, and the scope for everything harvested from it."""

    __tablename__ = "upload_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024), default="")
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    url_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0)
    url_column: Mapped[str] = mapped_column(String(255), default="")
    headers: Mapped[list] = mapped_column(JSON, default=list)
    #: reason -> count for rows the URL validator refused, so an upload can explain
    #: what it dropped long after the response that reported it has been closed.
    rejected_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    notes: Mapped[str] = mapped_column(Text, default="")

    urls: Mapped[list[HarvestedURL]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[HarvestJob]] = relationship(back_populates="batch")


class HarvestJob(Base):
    """Progress and outcome of a long-running unit of work.

    kind is one of harvest/index/extract. batch_id is nullable because indexing
    and extraction can be run across every batch at once.
    """

    __tablename__ = "harvest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    traceback: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped[UploadBatch | None] = relationship(back_populates="jobs")

    @property
    def percent(self) -> int:
        if not self.total:
            return 0 if self.status != "success" else 100
        return min(100, int(self.processed / self.total * 100))


class HarvestedURL(Base):
    """A single URL from a batch, plus everything fetched from it.

    This table is the source of truth behind GET /api/urls/ - it holds the HTTP
    status code, the raw HTML, and the extracted metadata the assignment asks for.
    """

    __tablename__ = "harvested_urls"
    __table_args__ = (
        UniqueConstraint("batch_id", "normalized_url", name="uq_batch_normalized_url"),
        Index("ix_harvested_urls_http_status", "http_status"),
        Index("ix_harvested_urls_crawl_status", "crawl_status"),
        Index("ix_harvested_urls_domain", "domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("upload_batches.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, index=True)
    #: The page's own <link rel="canonical">, when it declares one. Used as a second
    #: de-duplication key: two URLs that both name the same canonical are one page.
    canonical_url: Mapped[str] = mapped_column(Text, default="")
    #: Hostname without a leading www. Indexed because "show me everything from
    #: this company" is the most common filter once a batch holds several sites.
    domain: Mapped[str] = mapped_column(String(255), default="")

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(255), default="")

    #: One of app.services.crawl_status.CrawlStatus. Denormalised onto the row so
    #: the list API and the UI badges filter and render without recomputing it.
    crawl_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    #: Which extractor actually produced the text: http | trafilatura | dom |
    #: playwright | document. Answers "why did this page come back thin?".
    scraping_method: Mapped[str] = mapped_column(String(32), default="")
    #: How many links deep this page was found, 0 for a URL straight from the CSV.
    crawl_depth: Mapped[int] = mapped_column(Integer, default=0)
    #: The page this URL was discovered on, for pages found by link discovery.
    parent_url: Mapped[str] = mapped_column(Text, default="")

    raw_html: Mapped[str] = mapped_column(Text, default="")
    text_content: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(1024), default="")
    meta_description: Mapped[str] = mapped_column(Text, default="")
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Denormalised text size. Counting characters over a TEXT column on every list
    # request would read megabytes off disk to render a table column.
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set on every harvest, successful or not, so "when did we last look at this"
    #: is answerable for a failing row too.
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetch_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")

    # sha256 of text_content - lets reindexing skip pages whose text has not changed.
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    # The content_hash that the currently stored chunks were built from. A
    # mismatch means this page needs re-chunking and re-embedding.
    indexed_hash: Mapped[str] = mapped_column(String(64), default="")

    # Denormalized so list views and the list API avoid an N+1 count per row.
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    person_count: Mapped[int] = mapped_column(Integer, default=0)

    batch: Mapped[UploadBatch] = relationship(back_populates="urls")
    chunks: Mapped[list[Chunk]] = relationship(back_populates="url", cascade="all, delete-orphan")
    people: Mapped[list[PersonRecord]] = relationship(
        back_populates="url", cascade="all, delete-orphan"
    )

    @property
    def failure_reason(self) -> str:
        """The reason this row did not yield usable content.

        Stored in the `error` column, exposed under the name the API documents.
        A property rather than a second column: two fields holding the same string
        is a way to have them disagree.
        """
        return self.error or ""

    @property
    def is_usable(self) -> bool:
        """Whether this row has text worth indexing."""
        from app.services.crawl_status import USABLE_STATUSES

        return self.crawl_status in USABLE_STATUSES and bool(self.text_content)


class Chunk(Base):
    """A retrievable slice of a page's cleaned text.

    One row per embedding. The FAISS index stores (Chunk.id, vector), so this
    table is where a vector search result is resolved back into text and a source
    URL.
    """

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("url_id", "ordinal", name="uq_chunk_url_ordinal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    url_id: Mapped[int] = mapped_column(
        ForeignKey("harvested_urls.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    heading: Mapped[str] = mapped_column(String(512), default="")

    embedding_model: Mapped[str] = mapped_column(String(255), default="")
    indexed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_version: Mapped[str] = mapped_column(String(64), default="")

    url: Mapped[HarvestedURL] = relationship(back_populates="chunks")


class PersonRecord(Base):
    """A structured person extracted from a page by the LLM pass.

    Extracted at index time rather than query time, so search results keep showing
    person cards even when no LLM key is available at query time.
    """

    __tablename__ = "person_records"
    __table_args__ = (
        UniqueConstraint("url_id", "name", "title", name="uq_person_url_name_title"),
        Index("ix_person_records_name", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    url_id: Mapped[int] = mapped_column(
        ForeignKey("harvested_urls.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("upload_batches.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(512), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    company: Mapped[str] = mapped_column(String(512), default="")
    location: Mapped[str] = mapped_column(String(512), default="")
    email: Mapped[str] = mapped_column(String(512), default="")
    phone: Mapped[str] = mapped_column(String(128), default="")
    linkedin_url: Mapped[str] = mapped_column(String(1024), default="")
    bio: Mapped[str] = mapped_column(Text, default="")

    source_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    extracted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    url: Mapped[HarvestedURL] = relationship(back_populates="people")


class IndexMeta(Base):
    """Single-row bookkeeping for the persisted FAISS index.

    The store compares this against meta.json on disk; a mismatch means the index
    on disk is not the one this database describes, and a rebuild is required
    rather than serving stale vectors silently.
    """

    __tablename__ = "index_meta"

    id: Mapped[int] = mapped_column(primary_key=True)
    index_version: Mapped[str] = mapped_column(String(64), default="")
    dim: Mapped[int] = mapped_column(Integer, default=0)
    model_name: Mapped[str] = mapped_column(String(255), default="")
    vector_count: Mapped[int] = mapped_column(Integer, default=0)
    built_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    path: Mapped[str] = mapped_column(String(1024), default="")


class SearchQuery(Base):
    """Append-only log of searches. Feeds the dashboard and keeps latency honest."""

    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    top_k: Mapped[int] = mapped_column(Integer, default=0)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_used: Mapped[bool] = mapped_column(default=False)
    provider: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
