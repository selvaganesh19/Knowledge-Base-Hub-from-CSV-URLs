"""End-to-end semantic search over the harvested knowledge base.

The query flow is: embed the query, over-fetch from FAISS, group the hits by
source page, attach the person records already extracted from those pages, and
only then ask the LLM to write an answer over that context.

Grouping by page rather than returning a flat list of fragments is what makes the
results readable - a user asking "who is the CFO?" wants the page, not four
disconnected paragraphs from it.

Every LLM failure is non-fatal. The retrieval half of the response is computed
before the model is called, so a missing key or a rate limit degrades to
"here are the matching passages and people, without a narrative".
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Chunk, HarvestedURL, PersonRecord, SearchQuery
from app.services.embedder import embed_query_async
from app.services.llm import LLMUnavailable, get_provider
from app.services.vector_store import store

logger = logging.getLogger(__name__)

settings = get_settings()

# How many extra chunks from the same page to include beyond the best match.
CONTEXT_CHUNKS_PER_PAGE = 2
# How many chunks are handed to the LLM for synthesis.
SYNTHESIS_CONTEXT_CHUNKS = 6
# Models do not emit citations in one fixed shape. Seen in practice: [1], the
# full-width 【1】, and Groq's 【1†L9-L11】 where a line reference is appended to the
# source number. All three mean the same thing, so the pattern takes the leading
# number and tolerates whatever follows it.
CITATION_RE = re.compile(r"[\[【](\d+)[^\]】]*[\]】]")

SYNTHESIS_SYSTEM_PROMPT = """You are a knowledge-base assistant answering questions \
about people and organisations from harvested web pages.

Rules:
- Answer using ONLY the CONTEXT provided. Do not use outside knowledge.
- Cite sources inline as [1], [2] matching the numbered context entries. Use plain
  ASCII square brackets exactly like that - not full-width brackets, and with no
  line or page reference appended.
- If the context does not contain the answer, say so plainly. Do not speculate or \
fill gaps with assumptions.
- Be concise and structured: short paragraphs or bullet lists, with names and job \
titles in bold.
- When the question is about a person, lead with their name, title and company."""


async def search(
    query: str,
    session: Session | None = None,
    top_k: int | None = None,
    use_llm: bool = True,
) -> dict:
    """Run a semantic search. Returns an API-shaped dict."""
    started = time.perf_counter()
    query = (query or "").strip()
    top_k = top_k or settings.search_top_k

    if not query:
        return _empty_response(query, "empty query")

    owns_session = session is None
    session = session or SessionLocal()

    try:
        vector = await embed_query_async(query)
        hits = await asyncio.to_thread(store.search, vector, top_k * 4)

        if not hits:
            return _empty_response(
                query,
                "the index is empty - upload and index some URLs first",
                latency_ms=_elapsed(started),
            )

        low_confidence = False
        filtered = [(cid, score) for cid, score in hits if score >= settings.search_min_score]
        if not filtered:
            # Nothing clears the bar. Returning the best available matches with a
            # flag beats returning nothing at all.
            low_confidence = True
            filtered = hits[:top_k]

        chunks = _load_chunks(session, [chunk_id for chunk_id, _ in filtered])
        grouped = _group_by_page(chunks, dict(filtered), limit=top_k)

        people_by_url = _load_people(session, [entry["url_id"] for entry in grouped.values()])
        relevant_url_ids = _relevant_page_ids(grouped.values())
        for entry in grouped.values():
            # A weakly-matching page still shows its passage - it may be the only
            # thing the index has on the topic - but its leadership team is not
            # presented as an answer to this question.
            entry["people"] = (
                people_by_url.get(entry["url_id"], [])
                if entry["url_id"] in relevant_url_ids
                else []
            )

        results = list(grouped.values())[:top_k]

        answer = ""
        llm_used = False
        provider_name = "none"
        note = ""

        if use_llm:
            provider = get_provider()
            provider_name = provider.name
            if provider.available():
                context = _build_context(results)
                try:
                    answer = await provider.complete(
                        SYNTHESIS_SYSTEM_PROMPT,
                        f"QUESTION: {query}\n\nCONTEXT:\n{context}",
                    )
                    llm_used = True
                except LLMUnavailable as exc:
                    note = f"LLM unavailable: {exc}"
                    logger.warning("synthesis failed: %s", exc)
            else:
                note = "No LLM API key configured - showing retrieval results only."
        else:
            note = "LLM synthesis disabled for this request."

        sources = _extract_sources(answer, results)
        people = _rank_people(results, query, answer)

        latency_ms = _elapsed(started)
        _log_query(session, query, top_k, len(results), latency_ms, llm_used, provider_name)

        return {
            "query": query,
            "answer": answer,
            "llm_used": llm_used,
            "provider": provider_name,
            "low_confidence": low_confidence,
            "note": note,
            "latency_ms": latency_ms,
            "sources": sources,
            "results": results,
            "people": people,
        }

    finally:
        if owns_session:
            session.close()


def _load_chunks(session: Session, chunk_ids: list[int]) -> list[Chunk]:
    if not chunk_ids:
        return []
    return list(session.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))).all())


def _group_by_page(chunks: list[Chunk], scores: dict[int, float], limit: int) -> dict[str, dict]:
    """Group scored chunks by source page, best chunk first within each page.

    Grouping is keyed on the normalized URL rather than the row id: the same page
    harvested in two different uploads is two rows, and keying on the row id would
    return the same page twice in one result set.
    """
    by_page: dict[str, list[tuple[float, Chunk]]] = {}

    for chunk in chunks:
        score = scores.get(chunk.id)
        if score is None:
            continue
        key = page_key(chunk.url)
        by_page.setdefault(key, []).append((score, chunk))

    pages: dict[str, dict] = {}
    for key, entries in by_page.items():
        entries.sort(key=lambda item: item[0], reverse=True)
        best_score, best_chunk = entries[0]
        url = best_chunk.url

        pages[key] = {
            "url_id": url.id,
            "url": url.url,
            "final_url": url.final_url or url.url,
            "title": url.title or url.url,
            "http_status": url.http_status,
            "score": round(best_score, 4),
            "chunk_id": best_chunk.id,
            "heading": best_chunk.heading,
            "text": best_chunk.text,
            "matched_chunks": [
                {
                    "chunk_id": chunk.id,
                    "score": round(score, 4),
                    "heading": chunk.heading,
                    "text": chunk.text,
                }
                for score, chunk in entries[: 1 + CONTEXT_CHUNKS_PER_PAGE]
            ],
            "people": [],
        }

    ordered = sorted(pages.items(), key=lambda item: item[1]["score"], reverse=True)
    return dict(ordered[:limit])


def page_key(url: HarvestedURL) -> str:
    """Identity of a page for de-duplication: the normalized URL."""
    return url.normalized_url or url.url


def _relevant_page_ids(entries: Iterable[dict]) -> set[int]:
    """Which pages matched the query well enough to answer with their people.

    The floor is relative to the best hit, not absolute. An absolute floor cannot
    work here: on a dataset of company home pages everything scores in a narrow
    band around 0.3, so any fixed cut-off either admits every page or rejects every
    page. What matters is which page the query is actually about, and that is the
    one the embedding ranked first.

    A page scoring under three quarters of the best hit is a different topic that
    happened to clear the floor - a query about IBM's Watson returning Accenture's
    executives is exactly this failure.
    """
    pages = list(entries)
    if not pages:
        return set()

    best = max(entry["score"] for entry in pages)
    threshold = max(best * settings.person_relevance_ratio, settings.search_min_score)
    return {entry["url_id"] for entry in pages if entry["score"] >= threshold}


def _load_people(session: Session, url_ids: list[int]) -> dict[int, list[dict]]:
    if not url_ids:
        return {}

    records = session.scalars(select(PersonRecord).where(PersonRecord.url_id.in_(url_ids))).all()

    grouped: dict[int, list[dict]] = {}
    for record in records:
        grouped.setdefault(record.url_id, []).append(_person_payload(record))
    return grouped


def _person_payload(record: PersonRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "title": record.title,
        "company": record.company,
        "location": record.location,
        "bio": record.bio,
        "email": record.email,
        "phone": record.phone,
        "linkedin_url": record.linkedin_url,
        "confidence": record.confidence,
        "source_url": record.url.url if record.url else "",
        "url_id": record.url_id,
    }


def _rank_people(results: list[dict], query: str, answer: str = "") -> list[dict]:
    """Flatten person cards in relevance order.

    Ordering matters here: a query about Oracle returns the Oracle page first, but
    flattening every page's people in database order puts whichever page happens to
    have the lowest id on top, so the cards shown would be from the wrong company.
    So results are walked in score order.

    Anyone named in the query or in the synthesized answer is pulled to the very
    front. That is what turns "who is the CFO of Oracle?" into a card for the CFO
    rather than the alphabetical first twelve of twenty-five executives - the model
    already worked out who matters, and this reuses that judgement.
    """
    haystack = _squash_spaces(f"{query}\n{answer}")
    named: list[dict] = []
    rest: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for result in results:
        people = sorted(
            result["people"], key=lambda person: person.get("confidence") or 0.0, reverse=True
        )
        for person in people:
            key = ((person.get("name") or "").lower(), (person.get("title") or "").lower())
            if key in seen:
                continue
            seen.add(key)

            name = _squash_spaces(person.get("name") or "").lower()
            # Full names only: a surname alone would match half a leadership team.
            if name and len(name.split()) >= 2 and name in haystack:
                named.append(person)
            else:
                rest.append(person)

    return (named + rest)[:12]


def _squash_spaces(text: str) -> str:
    """Collapse every Unicode space to a plain one, lowercased.

    Model output routinely contains narrow no-break spaces (U+202F) and
    non-breaking spaces inside people's names. Comparing raw strings against them
    fails on characters that are invisible in the rendered answer, so both sides of
    every comparison go through this first.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def _build_context(results: list[dict]) -> str:
    """Number the chunks handed to the model, matching the citation format."""
    entries = []
    count = 0

    for result in results:
        for chunk in result["matched_chunks"]:
            count += 1
            if count > SYNTHESIS_CONTEXT_CHUNKS:
                return "\n\n".join(entries)
            entries.append(f"[{count}] {result['title']}\nURL: {result['url']}\n{chunk['text']}")
    return "\n\n".join(entries)


def _extract_sources(answer: str, results: list[dict]) -> list[dict]:
    """Map [n] citations back to the URLs they refer to."""
    if not answer:
        return []

    # One entry per context slot. _build_context numbers chunks in this same order,
    # so a position in this list must line up with the citation index it maps to.
    ordered_chunks = [result for result in results for _chunk in result["matched_chunks"]]

    seen_indexes: set[int] = set()
    seen_urls: set[str] = set()
    sources = []
    for match in CITATION_RE.finditer(answer):
        index = int(match.group(1))
        if index in seen_indexes or not (1 <= index <= len(ordered_chunks)):
            continue
        seen_indexes.add(index)
        result = ordered_chunks[index - 1]
        # Two citations often land on two chunks of the same page; listing that URL
        # twice suggests two sources where there is one.
        if result["url"] in seen_urls:
            continue
        seen_urls.add(result["url"])
        sources.append({"index": index, "url": result["url"], "title": result["title"]})
    return sources


def _log_query(
    session: Session,
    query: str,
    top_k: int,
    result_count: int,
    latency_ms: int,
    llm_used: bool,
    provider: str,
) -> None:
    try:
        session.add(
            SearchQuery(
                query=query[:2000],
                top_k=top_k,
                result_count=result_count,
                latency_ms=latency_ms,
                llm_used=llm_used,
                provider=provider,
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 - logging must never break a search
        session.rollback()


def _empty_response(query: str, note: str, latency_ms: int = 0) -> dict:
    return {
        "query": query,
        "answer": "",
        "llm_used": False,
        "provider": "none",
        "low_confidence": False,
        "note": note,
        "latency_ms": latency_ms,
        "sources": [],
        "results": [],
        "people": [],
    }


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
