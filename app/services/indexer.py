"""The indexing job: chunk harvested text, embed it, and update the FAISS index.

Re-indexing is incremental. A page is only re-processed when its content hash
differs from the hash its current chunks were built from, so running a rebuild
twice is a no-op the second time rather than a source of duplicate vectors.

When a page is re-chunked its old chunk rows are deleted, which invalidates their
FAISS ids - so they are removed from the index in the same step. Skipping that
would leave orphan vectors that still match queries but resolve to nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Chunk, HarvestedURL, HarvestJob, IndexMeta
from app.services import embedder
from app.services import jobs as job_registry
from app.services.chunking import chunk_text
from app.services.vector_store import store

logger = logging.getLogger(__name__)

settings = get_settings()


def start_index_job(batch_id: int | None = None, rebuild: bool = False) -> int:
    session = SessionLocal()
    try:
        job = job_registry.create_job(session, kind="index", batch_id=batch_id)
        job_registry.spawn(job.id, run_index_job(job.id, batch_id=batch_id, rebuild=rebuild))
        return job.id
    finally:
        session.close()


async def run_index_job(
    job_id: int,
    batch_id: int | None = None,
    rebuild: bool = False,
    chain: bool = True,
) -> None:
    session = SessionLocal()
    try:
        job = session.get(HarvestJob, job_id)
        if job is None:
            return

        job.status = "running"
        job.started_at = dt.datetime.now(dt.UTC)
        job.message = "selecting pages to index"
        session.commit()

        if rebuild:
            store.reset()
            session.execute(
                delete(Chunk).where(
                    Chunk.url_id.in_(select(HarvestedURL.id).where(_scope(batch_id)))
                )
            )
            session.commit()

        targets = _select_targets(session, batch_id, rebuild)
        job.total = len(targets)
        session.commit()

        if not targets:
            job.status = "success"
            job.message = "nothing to index - all pages are already up to date"
            job.finished_at = dt.datetime.now(dt.UTC)
            session.commit()
            return

        # Loading the model is the slowest single step on a cold start, so the
        # job says so rather than appearing to hang.
        job.message = f"loading embedding model ({embedder.model_name()})"
        session.commit()
        await _warm_model()

        job.message = f"embedding {len(targets)} page(s)"
        session.commit()

        chunk_ids: list[int] = []
        for row in targets:
            try:
                new_ids = await _index_page(session, row)
                chunk_ids.extend(new_ids)
                job.succeeded += 1
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
                logger.exception("indexing failed for url %s", row.id)
                job.failed += 1
                job.message = f"error on {row.url}: {type(exc).__name__}: {exc}"

            job.processed += 1
            session.commit()

        version = store.persist(model_name=embedder.model_name())
        _record_index_meta(session, version)

        job.status = "success"
        job.message = f"indexed {job.succeeded} page(s), {len(chunk_ids)} chunk(s)"
        job.finished_at = dt.datetime.now(dt.UTC)
        session.commit()

    finally:
        session.close()

    if settings.auto_extract and chain:
        from app.services.extractor import start_extract_job

        start_extract_job(batch_id=batch_id)


def _scope(batch_id: int | None):
    """Which rows a pass may touch: pages with text, plus pages that once had text.

    The second half matters. A page that has since started returning a bot-block
    page - or a 403, or a scan with no text layer - now has empty text, and a filter
    on non-empty text alone would leave its previously indexed chunks in place. The
    search results would keep serving content from a URL the UI reports as failing.
    Selecting them here routes them through the same path, where the empty text
    deletes their chunks and removes their vectors.
    """
    clause = (HarvestedURL.text_content != "") | (HarvestedURL.chunk_count > 0)
    return clause if batch_id is None else (clause & (HarvestedURL.batch_id == batch_id))


def _select_targets(session: Session, batch_id: int | None, rebuild: bool) -> list[HarvestedURL]:
    rows = list(
        session.scalars(
            select(HarvestedURL).where(_scope(batch_id)).order_by(HarvestedURL.id)
        ).all()
    )
    if rebuild:
        return rows

    def needs_work(row: HarvestedURL) -> bool:
        if not row.text_content:
            # Text is gone but chunks remain - clear them out.
            return bool(row.chunk_count)
        return not row.chunk_count or row.indexed_hash != row.content_hash

    return [row for row in rows if needs_work(row)]


async def _warm_model() -> None:
    """Load the model off the event loop so page requests keep being served."""
    await asyncio.to_thread(embedder.get_model)


async def _index_page(session: Session, row: HarvestedURL) -> list[int]:
    """Re-chunk and re-embed one page, returning the new chunk ids."""
    pieces = chunk_text(
        row.text_content,
        max_chars=settings.chunk_size,
        overlap=settings.chunk_overlap,
    )

    old_ids = list(session.scalars(select(Chunk.id).where(Chunk.url_id == row.id)).all())

    # Remove the old vectors before deleting the rows they point at, otherwise
    # FAISS keeps ids that no longer resolve to a chunk.
    if old_ids:
        store.remove(old_ids)
        session.execute(delete(Chunk).where(Chunk.url_id == row.id))
        session.flush()

    if not pieces:
        row.chunk_count = 0
        row.indexed_hash = row.content_hash
        return []

    rows = [
        Chunk(
            url_id=row.id,
            ordinal=piece["ordinal"],
            text=piece["text"],
            char_start=piece["char_start"],
            char_end=piece["char_end"],
            heading=piece["heading"][:500],
            embedding_model=embedder.model_name(),
        )
        for piece in pieces
    ]
    session.add_all(rows)
    session.flush()  # assigns ids for the FAISS mapping

    vectors = await embedder.embed_texts_async([r.text for r in rows])
    ids = [r.id for r in rows]

    if vectors.size:
        store.add(ids, vectors)

    for chunk in rows:
        chunk.indexed_at = dt.datetime.now(dt.UTC)

    row.chunk_count = len(rows)
    row.indexed_hash = row.content_hash
    return ids


def _record_index_meta(session: Session, version: str) -> None:
    meta = session.scalar(select(IndexMeta).order_by(IndexMeta.id))
    if meta is None:
        meta = IndexMeta()
        session.add(meta)

    meta.index_version = version
    meta.dim = 384
    meta.model_name = embedder.model_name()
    meta.vector_count = store.count
    meta.built_at = dt.datetime.now(dt.UTC)
    meta.path = str(store.path)
    session.commit()


def index_stats(session: Session) -> dict:
    return {
        "urls": session.scalar(select(func.count(HarvestedURL.id))) or 0,
        "chunks": session.scalar(select(func.count(Chunk.id))) or 0,
        "vectors": store.count,
        "version": store.version or None,
    }
