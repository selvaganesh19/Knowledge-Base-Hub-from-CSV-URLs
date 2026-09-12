"""Command-line access to the same service layer the API uses.

    python -m app.cli stats
    python -m app.cli harvest --batch 1
    python -m app.cli index --rebuild
    python -m app.cli search "who is the CFO?" --no-llm
    python -m app.cli import-urls "Leadership URL.xlsx"

The harvest and index commands run the job coroutines directly in the foreground.
That makes the pipeline reproducible without the web server - useful for testing,
and it is the fallback when a background task is interrupted by a restart.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from sqlalchemy import func, select

from app.config import ensure_directories, get_settings
from app.db import SessionLocal, init_db
from app.models import Chunk, HarvestedURL, PersonRecord, UploadBatch
from app.services.reader import (
    ReaderError,
    detect_url_column,
    extract_urls,
    load_rows,
    normalize_url,
)


def main(argv: list[str] | None = None) -> int:
    # Harvested content and model output are frequently non-ASCII (names, smart
    # quotes, the occasional full-width bracket). A cp1252 console would otherwise
    # abort a successful command with a UnicodeEncodeError while printing.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from app.logging_config import configure_logging

    configure_logging()

    parser = argparse.ArgumentParser(prog="app.cli", description="Knowledge Base Hub CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Show dataset counts and index state")

    harvest = sub.add_parser("harvest", help="Re-harvest an existing batch")
    harvest.add_argument("--batch", type=int, required=True)

    index = sub.add_parser("index", help="Chunk and embed harvested pages")
    index.add_argument("--batch", type=int, default=None)
    index.add_argument("--rebuild", action="store_true")

    extract = sub.add_parser("extract", help="Run the person-extraction pass")
    extract.add_argument("--batch", type=int, default=None)
    extract.add_argument("--force", action="store_true")

    search = sub.add_parser("search", help="Run a semantic search")
    search.add_argument("query")
    search.add_argument("--no-llm", action="store_true")
    search.add_argument("--top-k", type=int, default=None)

    reextract = sub.add_parser(
        "reextract",
        help="Re-derive cleaned text from the stored raw HTML, without re-fetching",
    )
    reextract.add_argument("--batch", type=int, default=None)
    reextract.add_argument("--index", action="store_true", help="Reindex afterwards")

    import_urls = sub.add_parser(
        "import-urls", help="Create a batch from a CSV/XLSX without the web UI"
    )
    import_urls.add_argument("file")
    import_urls.add_argument("--column", default=None)
    import_urls.add_argument("--no-harvest", action="store_true")

    args = parser.parse_args(argv)

    ensure_directories()
    init_db()

    if args.command == "stats":
        return cmd_stats()
    if args.command == "harvest":
        return cmd_harvest(args)
    if args.command == "index":
        return cmd_index(args)
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "search":
        return cmd_search(args)
    if args.command == "reextract":
        return cmd_reextract(args)
    if args.command == "import-urls":
        return cmd_import(args)

    parser.print_help()
    return 1


def cmd_stats() -> int:
    from app.services.vector_store import store

    session = SessionLocal()
    try:
        print(f"batches : {session.scalar(select(func.count(UploadBatch.id))) or 0}")
        print(f"urls    : {session.scalar(select(func.count(HarvestedURL.id))) or 0}")
        print(f"chunks  : {session.scalar(select(func.count(Chunk.id))) or 0}")
        print(f"people  : {session.scalar(select(func.count(PersonRecord.id))) or 0}")
        print(f"vectors : {store.count}")
        print(f"index   : {store.path} (version {store.version or 'not persisted'})")
    finally:
        session.close()
    return 0


def cmd_harvest(args) -> int:
    session = SessionLocal()
    try:
        from app.services.jobs import create_job

        job = create_job(session, kind="harvest", batch_id=args.batch)
        job_id = job.id
    finally:
        session.close()

    asyncio.run(_harvest_pipeline(job_id, args.batch))
    return _report(job_id)


async def _harvest_pipeline(harvest_job_id: int, batch_id: int | None) -> None:
    """Run harvest, then index and extract, all in the foreground.

    The web pipeline chains these as background tasks, but a CLI process closes
    its event loop as soon as the entry coroutine returns - a chained task would
    be cancelled before it ran. So the chain is driven explicitly here.
    """
    from app.config import get_settings
    from app.services.extractor import run_extract_job
    from app.services.harvest import run_harvest_job
    from app.services.indexer import run_index_job
    from app.services.jobs import create_job

    settings = get_settings()

    await run_harvest_job(harvest_job_id, chain=False)

    if batch_id is None or not settings.auto_index:
        return

    session = SessionLocal()
    try:
        index_job_id = create_job(session, kind="index", batch_id=batch_id).id
    finally:
        session.close()

    await run_index_job(index_job_id, batch_id=batch_id, chain=False)
    _report(index_job_id)

    if not settings.auto_extract:
        return

    session = SessionLocal()
    try:
        extract_job_id = create_job(session, kind="extract", batch_id=batch_id).id
    finally:
        session.close()

    await run_extract_job(extract_job_id, batch_id=batch_id, force=False)
    _report(extract_job_id)


def cmd_index(args) -> int:
    from app.services.extractor import run_extract_job
    from app.services.indexer import run_index_job
    from app.services.jobs import create_job

    session = SessionLocal()
    try:
        job = create_job(session, kind="index", batch_id=args.batch)
        job_id = job.id
    finally:
        session.close()

    async def _run() -> int | None:
        await run_index_job(job_id, batch_id=args.batch, rebuild=args.rebuild, chain=False)
        settings = get_settings()
        if not settings.auto_extract:
            return None
        session = SessionLocal()
        try:
            extract_id = create_job(session, kind="extract", batch_id=args.batch).id
        finally:
            session.close()
        await run_extract_job(extract_id, batch_id=args.batch, force=False)
        return extract_id

    extract_id = asyncio.run(_run())
    if extract_id is not None:
        _report(extract_id)
    return _report(job_id)


def cmd_extract(args) -> int:
    from app.services.extractor import run_extract_job
    from app.services.jobs import create_job

    session = SessionLocal()
    try:
        job = create_job(session, kind="extract", batch_id=args.batch)
        job_id = job.id
    finally:
        session.close()

    asyncio.run(run_extract_job(job_id, batch_id=args.batch, force=args.force))
    return _report(job_id)


def cmd_reextract(args) -> int:
    """Re-run text extraction over what was stored: raw HTML, or the saved document.

    The point of persisting the raw artifact is that the cleaning step can be
    improved or repeated without hitting the origin server again. This is that path:
    no network access, no rate limits, identical results for identical input. It
    covers harvested documents as well as pages, reading them back from the stored
    copy on disk.
    """
    import hashlib

    from sqlalchemy import or_, select

    from app.services.documents import extract_document_text, resolve_path
    from app.services.scraper import extract_text

    session = SessionLocal()
    try:
        stmt = (
            select(HarvestedURL)
            .where(or_(HarvestedURL.raw_html != "", HarvestedURL.meta_json.is_not(None)))
            .order_by(HarvestedURL.id)
        )
        if args.batch is not None:
            stmt = stmt.where(HarvestedURL.batch_id == args.batch)
        rows = list(session.scalars(stmt).all())

        pages = documents = skipped = changed = 0

        for row in rows:
            meta = dict(row.meta_json or {})
            document_path = meta.get("document_path")
            kind = meta.get("document_kind")

            if row.raw_html:
                text, title, description, new_meta = extract_text(row.raw_html)
                pages += 1
            elif document_path and kind:
                try:
                    payload = resolve_path(document_path).read_bytes()
                    text, new_meta = extract_document_text(kind, payload)
                except (OSError, ValueError) as exc:
                    print(f"  {row.id}: could not re-read stored document ({exc})")
                    skipped += 1
                    continue
                title = meta.get("pdf_title") or row.title
                description = row.meta_description
                documents += 1
            else:
                skipped += 1
                continue

            before = len(row.text_content or "")
            row.text_content = text
            row.title = title or row.title
            row.meta_description = description or row.meta_description
            row.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""

            meta.update(new_meta)
            row.meta_json = meta

            if len(text) != before:
                changed += 1
                print(f"  {row.id}: {before} -> {len(text)} chars (changed)")

        session.commit()
        print(
            f"re-extracted {pages} page(s) and {documents} document(s), "
            f"{changed} changed, {skipped} skipped"
        )
    finally:
        session.close()

    if args.index:
        print("reindexing...")
        return cmd_index(argparse.Namespace(batch=args.batch, rebuild=False))
    return 0


def cmd_search(args) -> int:
    from app.services.search import search

    result = asyncio.run(search(args.query, top_k=args.top_k, use_llm=not args.no_llm))

    if result["note"]:
        print(f"[note] {result['note']}\n")
    if result["answer"]:
        print(result["answer"])

    print(f"\n{len(result['results'])} source(s), {result['latency_ms']} ms")
    for entry in result["results"]:
        print(f"  {entry['score']:.3f}  {entry['url']}")

    if result["people"]:
        print(f"\n{len(result['people'])} person record(s):")
        for person in result["people"]:
            role = " - ".join(part for part in (person["title"], person["company"]) if part)
            print(f"  {person['name']}{' (' + role + ')' if role else ''}")

    return 0


def cmd_import(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    try:
        headers, rows = load_rows(path)
    except ReaderError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 1

    column = args.column if args.column in headers else detect_url_column(headers, rows)
    if column is None:
        print(f"no URL column detected in {path}. Columns: {headers}", file=sys.stderr)
        return 1

    pairs, skipped = extract_urls(rows, column)

    session = SessionLocal()
    try:
        batch = UploadBatch(
            original_filename=path.name,
            stored_path=str(path),
            row_count=len(rows),
            url_count=len(pairs),
            skipped_rows=skipped,
            url_column=column,
            headers=headers,
            status="running",
        )
        session.add(batch)
        session.flush()

        for row, url in pairs:
            session.add(
                HarvestedURL(
                    batch_id=batch.id,
                    url=url,
                    normalized_url=normalize_url(url),
                    meta_json={"row": row},
                )
            )
        session.commit()
        batch_id = batch.id
    finally:
        session.close()

    print(f"batch {batch_id}: {len(pairs)} URL(s) from column '{column}', {skipped} skipped")

    if args.no_harvest:
        return 0

    settings = get_settings()
    if not settings.auto_index:
        print(f"run 'python -m app.cli harvest --batch {batch_id}' next (AUTO_INDEX is off)")
    return cmd_harvest(argparse.Namespace(batch=batch_id))


def _report(job_id: int) -> int:
    from app.models import HarvestJob

    session = SessionLocal()
    try:
        job = session.get(HarvestJob, job_id)
        if job is None:
            print(f"job {job_id} vanished", file=sys.stderr)
            return 1
        print(f"job {job_id} [{job.kind}] {job.status}: {job.message}")
        if job.traceback:
            print(job.traceback, file=sys.stderr)
        return 0 if job.status == "success" else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
