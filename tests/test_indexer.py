"""Tests for the indexing pass.

The interesting cases here are all about chunks that should stop existing: a page
whose text is gone must not keep serving the content it had last time, and a pass
with nothing to do must be a genuine no-op rather than a re-embed of every page.
"""

from __future__ import annotations

import asyncio

from app.services.indexer import run_index_job
from app.services.vector_store import store

TEXT = "# Leadership\n\nJane Doe is the chief executive officer of Example Corp."


def run(coro):
    return asyncio.run(coro)


def index(session, batch_id: int | None = None, rebuild: bool = False):
    """Run one indexing pass in the foreground and return its job row.

    The registry is skipped on purpose - the job body is what is under test, and a
    background task would make the assertions race it.
    """
    from app.models import HarvestJob

    job = HarvestJob(kind="index", batch_id=batch_id, status="queued")
    session.add(job)
    session.commit()

    run(run_index_job(job.id, batch_id=batch_id, rebuild=rebuild, chain=False))

    session.expire_all()
    return session.get(HarvestJob, job.id)


class TestIndexingPass:
    def test_text_is_chunked_and_embedded(self, session, make_url):
        row = make_url("https://example.test/leadership", TEXT)

        index(session)

        session.refresh(row)
        assert row.chunk_count > 0
        assert store.count == row.chunk_count

    def test_the_vector_ids_are_the_chunk_ids(self, session, make_url):
        """The whole reason a search hit resolves to text without a mapping table."""
        from app.models import Chunk

        row = make_url("https://example.test/leadership", TEXT)
        index(session)

        chunk_ids = {chunk.id for chunk in session.query(Chunk).filter_by(url_id=row.id)}
        assert chunk_ids
        assert set(store.ids()) == chunk_ids


class TestStaleChunks:
    def test_a_page_that_lost_its_text_has_its_chunks_removed(self, session, make_url):
        """A page that later starts blocking must stop being searchable.

        Harvest stores the failure and empties the text, but the chunks from the
        previous successful pass are still in the database. Selecting only pages
        with text would leave them there, so search would keep returning content
        from a URL the UI reports as failing.
        """
        from app.models import Chunk

        row = make_url("https://example.test/leadership", TEXT)
        index(session)
        assert store.count > 0

        # What a re-harvest that came back blocked leaves behind.
        row.text_content = ""
        row.content_hash = ""
        row.error = "blocked by site bot protection"
        session.commit()

        index(session)

        session.refresh(row)
        assert session.query(Chunk).filter_by(url_id=row.id).count() == 0
        assert row.chunk_count == 0
        assert store.count == 0
        assert store.ids() == []

    def test_a_page_that_never_had_text_is_not_re_indexed_forever(self, session, make_url):
        """A failed row must not make every pass do work it cannot complete."""
        make_url("https://dead.test/", "", status=403)
        index(session)

        job = index(session)

        assert job.message == "nothing to index - all pages are already up to date"


class TestIdempotency:
    def test_a_second_pass_over_unchanged_pages_does_nothing(self, session, make_url):
        from app.models import Chunk

        make_url("https://example.test/leadership", TEXT)
        index(session)
        first = session.query(Chunk).count()

        job = index(session)

        assert session.query(Chunk).count() == first
        assert store.count == first
        assert "nothing to index" in job.message

    def test_rebuild_replaces_chunks_rather_than_duplicating_them(self, session, make_url):
        """Rebuild resets the index, so the chunk count must come back the same."""
        from app.models import Chunk

        make_url("https://example.test/leadership", TEXT)
        index(session)
        first = session.query(Chunk).count()

        index(session, rebuild=True)

        assert session.query(Chunk).count() == first
        assert store.count == first
