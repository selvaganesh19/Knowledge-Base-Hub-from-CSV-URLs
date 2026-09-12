"""Shared test fixtures.

Two rules shape this file:

* **Nothing touches real state.** The suite runs against a scratch directory set
  through ``KBHUB_DATA_DIR``, so the developer's own database, index and uploads
  are never opened, and every test starts from an empty database and index.
* **Nothing touches the network.** The embedding model is replaced with a
  deterministic bag-of-words vectoriser, and the LLM provider is disabled. Tests
  exercise the real FAISS index and the real SQLite schema without downloading a
  90 MB model or spending API quota on every run.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
import zlib
from pathlib import Path

import numpy as np
import pytest

# Assigned before any application module is imported: app.config resolves its data
# root at import time, so this has to be in place first.
TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="kbhub-tests-"))
os.environ["KBHUB_DATA_DIR"] = str(TEST_DATA_ROOT)
os.environ["LLM_PROVIDER"] = "none"
os.environ["AUTO_INDEX"] = "0"
os.environ["AUTO_EXTRACT"] = "0"
os.environ["LOG_TO_FILE"] = "0"
os.environ["LOG_LEVEL"] = "WARNING"
# The developer's .env may enable private hosts so the demo documents on
# 127.0.0.1 can be harvested. Tests must not inherit that: the SSRF guard is
# behaviour under test, and it has to be the secure default here.
os.environ["ALLOW_PRIVATE_HOSTS"] = "0"
# The crawler's per-host politeness delay is real seconds. Tests construct their own
# limiter, but the harvest job reads this setting directly.
os.environ["CRAWL_DELAY"] = "0"

# Backstop for runs that end without pytest's session hook (an interrupt, or a
# closed output pipe killing the process), which would otherwise leave scratch
# directories behind in the temp folder.
atexit.register(shutil.rmtree, TEST_DATA_ROOT, ignore_errors=True)

EMBEDDING_DIM = 384


def deterministic_vector(text: str, dim: int = EMBEDDING_DIM) -> np.ndarray:
    """Turn text into a unit vector by hashing its tokens.

    This is a real retrieval signal - texts sharing vocabulary end up close - so
    ranking assertions test the pipeline's logic rather than a stub's constant
    output. crc32 rather than hash() because hash() is salted per process.
    """
    vector = np.zeros(dim, dtype="float32")
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(token.encode()) % dim] += 1.0

    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch):
    """Replace the sentence-transformer with the deterministic vectoriser."""
    from app.services import embedder

    def fake_embed_texts(texts):
        if not texts:
            return np.zeros((0, 0), dtype="float32")
        return np.asarray([deterministic_vector(text) for text in texts], dtype="float32")

    def fake_embed_query(text):
        return deterministic_vector(text)

    async def fake_embed_texts_async(texts):
        return fake_embed_texts(texts)

    async def fake_embed_query_async(text):
        return fake_embed_query(text)

    monkeypatch.setattr(embedder, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(embedder, "embed_query", fake_embed_query)
    monkeypatch.setattr(embedder, "embed_texts_async", fake_embed_texts_async)
    monkeypatch.setattr(embedder, "embed_query_async", fake_embed_query_async)
    monkeypatch.setattr(embedder, "get_model", lambda: None)
    monkeypatch.setattr(embedder, "model_name", lambda: "test-vectoriser")


@pytest.fixture(autouse=True)
def clean_state():
    """Give every test an empty database and an empty vector index."""
    from app.config import FAISS_INDEX_PATH, FAISS_META_PATH
    from app.db import Base, engine, init_db
    from app.services.vector_store import store

    init_db()
    yield
    Base.metadata.drop_all(bind=engine)

    store.reset()
    store._version = ""
    store._dirty = False
    for path in (FAISS_INDEX_PATH, FAISS_META_PATH):
        if path.exists():
            path.unlink()


@pytest.fixture
def session():
    """A database session for direct model manipulation."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def make_url(session):
    """Insert a harvested URL row and return it."""
    import hashlib

    from app.models import HarvestedURL, UploadBatch
    from app.services.reader import normalize_url

    counter = {"n": 0}

    def _make(url: str, text: str = "", status: int = 200, batch: UploadBatch | None = None):
        if batch is None:
            counter["n"] += 1
            batch = UploadBatch(
                original_filename=f"test-{counter['n']}.csv",
                row_count=1,
                url_count=1,
                url_column="URL",
            )
            session.add(batch)
            session.flush()

        row = HarvestedURL(
            batch_id=batch.id,
            url=url,
            normalized_url=normalize_url(url),
            http_status=status,
            final_url=url,
            content_type="text/html",
            raw_html="<html><body>page</body></html>",
            text_content=text,
            title=url.rsplit("/", 2)[-2] if url.count("/") > 2 else url,
            content_hash=hashlib.sha256(text.encode()).hexdigest() if text else "",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    return _make


@pytest.fixture
def vector_for():
    """Expose the deterministic vectoriser so tests can build real index entries."""
    return deterministic_vector


@pytest.fixture
def index_page(session):
    """Populate the database and FAISS index with one harvested page.

    Returns the created URL row. Chunks are produced by the real chunker and
    embedded with the deterministic vectoriser, so search tests exercise the real
    grouping, scoring and ranking paths.
    """
    from app.models import Chunk
    from app.services.chunking import chunk_text
    from app.services.vector_store import store

    def _index(row, text: str):
        pieces = chunk_text(text)
        chunks = [
            Chunk(
                url_id=row.id,
                ordinal=piece["ordinal"],
                text=piece["text"],
                char_start=piece["char_start"],
                char_end=piece["char_end"],
                heading=piece["heading"][:500],
            )
            for piece in pieces
        ]
        session.add_all(chunks)
        session.flush()

        if chunks:
            store.add(
                [chunk.id for chunk in chunks],
                np.asarray([deterministic_vector(chunk.text) for chunk in chunks], dtype="float32"),
            )

        row.chunk_count = len(chunks)
        row.indexed_hash = row.content_hash
        session.commit()
        return row

    return _index


@pytest.fixture
def client():
    """A TestClient bound to the application."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest hook signature
    """Remove the scratch data directory once the run is over."""
    shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)
