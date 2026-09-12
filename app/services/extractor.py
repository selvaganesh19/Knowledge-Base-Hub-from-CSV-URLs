"""Extracting structured person records from harvested pages.

This runs at index time, not at query time, and that is a deliberate trade. It
costs one LLM call per page up front, but it means search results keep showing
person cards even if the API key is gone, the quota is exhausted, or the provider
is down when someone is actually searching. The expensive, failure-prone work
happens where a failure is survivable and visible, rather than on the critical
path of every query.

A single page failing to extract never fails the job - it increments a counter,
records the reason, and moves on.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Chunk, HarvestedURL, HarvestJob, PersonRecord
from app.services import jobs as job_registry
from app.services.llm import LLMUnavailable, get_provider, parse_json_response
from app.services.people import extract_people

logger = logging.getLogger(__name__)

# Leadership pages front-load names and back-load contact details, so a plain
# head truncation would drop exactly the fields being extracted. Head-and-tail
# keeps both ends.
HEAD_CHARS = 6000
TAIL_CHARS = 2000
MAX_TEXT_CHARS = HEAD_CHARS + TAIL_CHARS

PERSON_SYSTEM_PROMPT = """You extract structured records about individual people \
from the text of a web page.

Return STRICT JSON only - no prose, no explanation, no code fences.

Schema:
{"people": [{"name": str, "title": str, "company": str, "bio": str, "email": str, \
"phone": str, "linkedin_url": str, "location": str, "confidence": float}]}

Rules:
- Only include people the text actually describes as individuals.
- Use "" for any field the text does not state.
- Never invent contact details, job titles, or biographies.
- confidence is your own 0-1 estimate that this record is a real person with an \
accurate name.
- If the page contains no individual people, return {"people": []}"""


def start_extract_job(batch_id: int | None = None, force: bool = False) -> int:
    session = SessionLocal()
    try:
        job = job_registry.create_job(session, kind="extract", batch_id=batch_id)
        job_registry.spawn(job.id, run_extract_job(job.id, batch_id=batch_id, force=force))
        return job.id
    finally:
        session.close()


async def run_extract_job(job_id: int, batch_id: int | None = None, force: bool = False) -> None:
    session = SessionLocal()
    try:
        job = session.get(HarvestJob, job_id)
        if job is None:
            return

        job.status = "running"
        job.started_at = dt.datetime.now(dt.UTC)
        session.commit()

        provider = get_provider()
        llm_available = provider.available()
        # The pattern matcher needs no key and no network, so person extraction is
        # never skipped outright. Without a key the job still produces records - it
        # just produces fewer, and says so.
        job.message = (
            f"extracting people with {provider.name}"
            if llm_available
            else "extracting people with the pattern matcher (no LLM key configured)"
        )
        session.commit()

        rows = list(
            session.scalars(
                select(HarvestedURL)
                .where(
                    HarvestedURL.text_content != "",
                    *([HarvestedURL.batch_id == batch_id] if batch_id else []),
                )
                .order_by(HarvestedURL.id)
            ).all()
        )
        job.total = len(rows)
        session.commit()

        for row in rows:
            already_done = (row.meta_json or {}).get("people_extracted")
            if not force and row.person_count and already_done:
                job.processed += 1
                job.message = "skipped already-extracted page"
                session.commit()
                continue

            try:
                count = await extract_people_for_url(
                    session, row, provider if llm_available else None
                )
                row.person_count = count
                row.meta_json = {
                    **(row.meta_json or {}),
                    "people_extracted": True,
                    "people_extraction": "llm+pattern" if llm_available else "pattern",
                }
                job.succeeded += 1
                job.message = f"{row.url} - {count} person record(s)"
            except LLMUnavailable as exc:
                job.failed += 1
                job.message = f"LLM error on {row.url}: {exc}"
                logger.warning("person extraction failed for %s: %s", row.url, exc)
            except Exception as exc:  # noqa: BLE001 - keep going through the batch
                job.failed += 1
                job.message = f"error on {row.url}: {type(exc).__name__}: {exc}"
                logger.exception("person extraction crashed for %s", row.url)

            job.processed += 1
            session.commit()

        job.status = "success"
        job.message = f"extracted from {job.succeeded} page(s), {job.failed} failed"
        job.finished_at = dt.datetime.now(dt.UTC)
        session.commit()

    finally:
        session.close()


async def extract_people_for_url(session: Session, row: HarvestedURL, provider=None) -> int:
    """Extract people from one page, by pattern and optionally by model.

    Both readers run because they fail in opposite directions. The pattern matcher
    is exact and literal - it needs the name and the title to be adjacent - so it
    misses prose like "Jane has led the company since 2019 as its chief executive".
    The model reads that prose but is unavailable without a key and can return
    nothing on a page whose layout it does not follow. Storing the union means a
    page is only empty of people when neither reader found anyone.

    A model failure degrades to the pattern matcher rather than losing the page.
    """
    deterministic = extract_people(row.text_content, row.url or row.final_url)

    records: list[dict] = []
    if provider is not None:
        try:
            records = await _llm_records(row, provider)
        except LLMUnavailable as exc:
            logger.warning("LLM extraction failed for %s, using pattern matches: %s", row.url, exc)

    merged = merge_records(records, deterministic)
    return _store_records(session, row, merged)


async def _llm_records(row: HarvestedURL, provider) -> list:
    """Ask the model for structured records. Raises LLMUnavailable on any failure."""
    text = truncate_for_prompt(row.text_content)
    if not text.strip():
        return []

    user_prompt = f"URL: {row.url}\nPage title: {row.title or '(none)'}\n\nPAGE TEXT:\n{text}"

    raw = await provider.complete(PERSON_SYSTEM_PROMPT, user_prompt, json_mode=True)
    payload = parse_json_response(raw)

    records = payload.get("people")
    if not isinstance(records, list):
        raise LLMUnavailable("model response had no 'people' list")
    return records


def merge_records(model_records: list, pattern_people: list) -> list[dict]:
    """Combine both readers into one record per person.

    The model's fields win where both described the same person, because it reads
    the biography and contact details that the pattern matcher cannot see. The
    pattern matcher fills a field the model left empty - most often the job title,
    which it reads off a card the model summarised past.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []

    for record in model_records:
        coerced = _coerce(record)
        key = coerced["name"].lower()
        if not key:
            continue
        merged[key] = coerced
        order.append(key)

    for person in pattern_people:
        coerced = _coerce(person.as_record())
        key = coerced["name"].lower()
        if not key:
            continue

        existing = merged.get(key)
        if existing is None:
            merged[key] = coerced
            order.append(key)
            continue

        for field_name, value in coerced.items():
            if value and not existing.get(field_name):
                existing[field_name] = value
        # Pattern evidence is literal text from the page; it makes the source chunk
        # findable even when the model wrote a paraphrase.
        existing["pattern_evidence"] = person.evidence

    return [merged[key] for key in order]


def truncate_for_prompt(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return f"{text[:HEAD_CHARS]}\n\n[...middle of page omitted...]\n\n{text[-TAIL_CHARS:]}"


def _store_records(session: Session, row: HarvestedURL, records: list) -> int:
    """Upsert extracted records, replacing any previous extraction for this page."""
    clean = [_coerce(record) for record in records]
    clean = [record for record in clean if record["name"]]

    # Re-extraction replaces the old set rather than accumulating duplicates.
    session.execute(delete(PersonRecord).where(PersonRecord.url_id == row.id))
    session.flush()

    chunk_ids = list(session.scalars(select(Chunk.id).where(Chunk.url_id == row.id)).all())
    chunk_texts: dict[int, str] = {}
    if chunk_ids:
        for chunk_id, text in session.execute(
            select(Chunk.id, Chunk.text).where(Chunk.id.in_(chunk_ids))
        ).all():
            chunk_texts[chunk_id] = text.lower()

    created = 0
    for record in clean:
        name_key = record["name"].lower()
        source_ids = [
            chunk_id
            for chunk_id, haystack in chunk_texts.items()
            if name_key and name_key in haystack
        ]

        session.add(
            PersonRecord(
                url_id=row.id,
                batch_id=row.batch_id,
                name=record["name"][:500],
                title=record["title"][:500],
                company=record["company"][:500],
                location=record["location"][:500],
                email=record["email"][:500],
                phone=record["phone"][:120],
                linkedin_url=record["linkedin_url"][:1000],
                bio=record["bio"],
                source_chunk_ids=source_ids,
                confidence=record["confidence"],
                raw_json=record,
            )
        )
        created += 1

    session.flush()
    return created


def _coerce(record: dict) -> dict:
    """Force a model response into the expected shape, dropping malformed values."""
    if not isinstance(record, dict):
        return _empty_record()

    result = {
        key: _as_text(record.get(key))
        for key in ("name", "title", "company", "location", "email", "phone", "linkedin_url", "bio")
    }

    confidence = record.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    result["confidence"] = confidence

    return result


def _empty_record() -> dict:
    return {
        "name": "",
        "title": "",
        "company": "",
        "location": "",
        "email": "",
        "phone": "",
        "linkedin_url": "",
        "bio": "",
        "confidence": None,
    }


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
