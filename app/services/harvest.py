"""The harvest job: fetch every URL in a batch and store what came back.

The shape of this coroutine is the reason the async design is worth it. All
fetches are launched at once under a semaphore, then results are consumed with
`asyncio.as_completed` and written as they arrive. Two consequences:

* Progress is truthful mid-run, because the job row's counters are committed per
  completed URL rather than at the end.
* Only this one coroutine ever holds a writing session, so SQLite never has two
  concurrent writers. The usual "database is locked" problem under concurrent
  scraping does not arise - it is designed out rather than retried around.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import HarvestedURL, HarvestJob, UploadBatch
from app.services.crawl_status import CrawlStatus
from app.services.reader import normalize_url
from app.services.scraper import (
    FetchResult,
    HostLimiter,
    RobotsCache,
    build_client,
    fetch_url,
)
from app.services.url_guard import domain_of

logger = logging.getLogger(__name__)

settings = get_settings()


def start_harvest_job(batch_id: int) -> int:
    """Create the job row and schedule it. Returns the job id for progress polling."""
    from app.services import jobs

    session = SessionLocal()
    try:
        job = jobs.create_job(session, kind="harvest", batch_id=batch_id)
        jobs.spawn(job.id, run_harvest_job(job.id))
        return job.id
    finally:
        session.close()


async def run_harvest_job(job_id: int, chain: bool = True) -> None:
    session = SessionLocal()
    batch_id: int | None = None
    try:
        job = session.get(HarvestJob, job_id)
        if job is None:
            return
        batch_id = job.batch_id

        rows = list(
            session.scalars(
                select(HarvestedURL)
                .where(HarvestedURL.batch_id == job.batch_id)
                .order_by(HarvestedURL.id)
            ).all()
        )

        job.status = "running"
        job.started_at = dt.datetime.now(dt.UTC)
        job.total = len(rows)
        job.message = f"fetching {len(rows)} URL(s) with {settings.scrape_workers} workers"
        session.commit()

        if not rows:
            job.status = "success"
            job.message = "no URLs to harvest"
            job.finished_at = dt.datetime.now(dt.UTC)
            session.commit()
            return

        async with build_client(settings) as client:
            robots = RobotsCache(client, settings.user_agent, settings.respect_robots)
            limiter = HostLimiter(settings.crawl_delay)
            semaphore = asyncio.Semaphore(max(1, settings.scrape_workers))

            async def fetch_one(row: HarvestedURL):
                async with semaphore:
                    if settings.crawl_engine == "crawl4ai":
                        # Opt-in alternative engine. Falls back to the built-in HTTP
                        # fetch for any URL it cannot handle, so enabling it cannot
                        # lose a page - see app/services/crawl4ai_engine.py.
                        from app.services.crawl4ai_engine import fetch_or_fallback

                        result = await fetch_or_fallback(
                            row.url,
                            settings,
                            lambda: fetch_url(client, row.url, settings, robots, limiter),
                        )
                    else:
                        result = await fetch_url(client, row.url, settings, robots, limiter)
                return row, result

            tasks = [asyncio.create_task(fetch_one(row)) for row in rows]

            for future in asyncio.as_completed(tasks):
                try:
                    row, result = await future
                except Exception as exc:  # noqa: BLE001 - keep the batch going
                    logger.exception("harvest task raised")
                    job.failed += 1
                    job.processed += 1
                    job.message = f"task error: {type(exc).__name__}: {exc}"
                    session.commit()
                    continue

                _store_result(row, result)
                job.processed += 1
                if result.ok:
                    job.succeeded += 1
                else:
                    job.failed += 1
                # The status is in the message as well as the counters, because
                # "BLOCKED" and "FAILED" need different responses from the reader
                # and the two share one counter.
                job.message = f"{job.processed}/{job.total} · {row.crawl_status} · {row.url}"
                session.commit()

            if settings.discover_linked_pages:
                await _harvest_discovered(
                    session, job, client, robots, limiter, semaphore, settings
                )

        succeeded = job.succeeded
        failed = job.failed
        job.status = "success"
        job.message = f"harvested {succeeded} page(s), {failed} with errors"
        job.finished_at = dt.datetime.now(dt.UTC)

        batch = session.get(UploadBatch, job.batch_id)
        if batch is not None:
            batch.status = "done"

        session.commit()

    finally:
        session.close()

    # Chained outside the session: indexing is a separate job with its own row.
    # Callers that drive the pipeline themselves (the CLI, which closes the event
    # loop as soon as this coroutine returns) pass chain=False.
    if settings.auto_index and chain and batch_id is not None:
        from app.services.indexer import start_index_job

        start_index_job(batch_id=batch_id)


async def _harvest_discovered(
    session,
    job: HarvestJob,
    client,
    robots: RobotsCache,
    limiter: HostLimiter,
    semaphore: asyncio.Semaphore,
    settings,
) -> None:
    """Follow priority internal links, one depth level at a time.

    Runs after the CSV's own URLs so a discovery failure can never cost the user the
    pages they explicitly asked for. Each round fetches the pages queued by the
    previous one, so depth grows a level at a time and the budget is shared across
    the whole run rather than granted per seed.

    Discovery is best-effort throughout: a page that yields no links, or links that
    all fail validation, simply ends that branch.
    """
    from app.services.discovery import dedup_key, select_new_links

    # Every URL seen this run, so a page reachable from several seeds is crawled
    # once and depth-first loops cannot re-queue each other.
    seen: set[str] = {
        dedup_key(row.normalized_url or row.url)
        for row in session.scalars(
            select(HarvestedURL).where(HarvestedURL.batch_id == job.batch_id)
        ).all()
    }

    #: Rounds run depth-first per level. Depth 0 rows are the CSV's own URLs.
    frontier = list(
        session.scalars(
            select(HarvestedURL)
            .where(
                HarvestedURL.batch_id == job.batch_id,
                HarvestedURL.crawl_depth == 0,
                HarvestedURL.raw_html != "",
            )
            .order_by(HarvestedURL.id)
        ).all()
    )

    for depth in range(1, settings.max_crawl_depth + 1):
        if not frontier:
            return

        queued: list[HarvestedURL] = []
        for parent in frontier:
            for link in select_new_links(parent.raw_html, parent.url, settings, seen):
                seen.add(dedup_key(link.url))
                row = HarvestedURL(
                    batch_id=parent.batch_id,
                    url=link.url,
                    normalized_url=normalize_url(link.url),
                    domain=domain_of(link.url),
                    crawl_status=str(CrawlStatus.PENDING),
                    crawl_depth=depth,
                    parent_url=parent.url,
                    meta_json={
                        "discovered": {
                            "from": parent.url,
                            "score": link.score,
                            "matched": link.matched,
                        }
                    },
                )
                session.add(row)
                queued.append(row)

        if not queued:
            return

        session.commit()
        job.total += len(queued)
        job.message = f"discovered {len(queued)} link(s) at depth {depth}"
        session.commit()
        logger.info("link discovery queued %d page(s) at depth %d", len(queued), depth)

        async def fetch_one(row: HarvestedURL):
            async with semaphore:
                result = await fetch_url(client, row.url, settings, robots, limiter)
            return row, result

        tasks = [asyncio.create_task(fetch_one(row)) for row in queued]

        for future in asyncio.as_completed(tasks):
            try:
                row, result = await future
            except Exception:  # noqa: BLE001 - discovery must not fail the batch
                logger.exception("discovered-page fetch raised")
                job.processed += 1
                job.failed += 1
                session.commit()
                continue

            _store_result(row, result)
            job.processed += 1
            if result.ok:
                job.succeeded += 1
            else:
                job.failed += 1
            job.message = f"{job.processed}/{job.total} · {row.crawl_status} · {row.url}"
            session.commit()

        # Only pages that actually returned HTML can be mined for more links.
        frontier = [row for row in queued if row.raw_html]


def _store_result(row: HarvestedURL, result: FetchResult) -> None:
    """Copy a FetchResult onto the ORM row."""
    text = result.text or ""
    row.http_status = result.status_code
    row.final_url = result.final_url or row.url
    row.content_type = result.content_type or ""
    row.raw_html = result.html or ""
    row.text_content = text
    row.title = result.title or ""
    row.meta_description = result.description or ""
    row.fetched_at = dt.datetime.now(dt.UTC)
    row.updated_at = row.fetched_at
    row.fetch_ms = result.elapsed_ms
    row.error = result.error or ""
    row.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""

    # Crawl classification and provenance, so a row answers "what happened to this
    # URL and which client produced its text" without re-deriving either.
    row.crawl_status = str(result.crawl_status)
    row.scraping_method = result.scraping_method or ""
    row.char_count = result.char_count or len(text)
    row.word_count = result.word_count or len(text.split())
    row.domain = domain_of(row.final_url or row.url)

    # Merge rather than replace: the upload step stored the original spreadsheet
    # row under meta_json["row"], and overwriting here would throw away the input
    # data the file was uploaded for.
    meta = dict(row.meta_json or {})
    meta.update(result.meta or {})
    meta["normalized_url"] = normalize_url(row.url)
    row.meta_json = meta

    # A page that declares its own canonical URL is saying "this address and that
    # one are the same document", which is the strongest de-duplication signal
    # available and worth storing rather than leaving buried in the metadata blob.
    row.canonical_url = str(meta.get("canonical") or "")
