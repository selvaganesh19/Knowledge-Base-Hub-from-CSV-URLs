"""Sentence-embedding model wrapper.

The model is loaded once per process and reused. Loading takes a few seconds and
the first run also downloads ~90 MB, so it happens lazily inside the indexing job
where the UI can report "loading model" - never inside a request handler where it
would look like a hang.

Every call goes through asyncio.to_thread. Torch holds the GIL for long stretches,
so encoding on the event loop would freeze every other request for the duration of
a batch.
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np

from app.config import MODEL_CACHE_DIR, get_settings

settings = get_settings()

_model = None
_model_lock = threading.Lock()
_load_failed: str = ""


def get_model():
    """Load the sentence-transformer model, once per process.

    The cached copy on disk is preferred over a Hub round-trip. Loading from the
    cache is faster, works without a network, and avoids the Hub's
    "unauthenticated requests" notice on every start - which is advice for model
    publishers and has no bearing on whether this app works. Only the very first
    run, before anything is cached, reaches out for the download.
    """
    global _model, _load_failed

    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        from sentence_transformers import SentenceTransformer
        from transformers.utils import logging as transformers_logging

        # transformers renders a "Loading weights" tqdm bar directly to stderr,
        # below the logging layer, so quieting its logger does not stop it.
        transformers_logging.disable_progress_bar()

        try:
            _model = _load_model(SentenceTransformer, local_files_only=True)
        except Exception:  # noqa: BLE001 - nothing cached yet, or the cache is partial
            try:
                _model = _load_model(SentenceTransformer, local_files_only=False)
            except Exception as exc:  # noqa: BLE001 - surfaced to the job row
                _load_failed = f"{type(exc).__name__}: {exc}"
                raise

        _load_failed = ""
        return _model


def _load_model(model_class, local_files_only: bool):
    return model_class(
        settings.embedding_model,
        cache_folder=str(MODEL_CACHE_DIR),
        local_files_only=local_files_only,
    )


def model_name() -> str:
    return settings.embedding_model


def embedding_dim() -> int:
    return int(get_model().get_sentence_embedding_dimension())


def load_error() -> str:
    return _load_failed


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed documents into normalized float32 vectors.

    Vectors are normalized at write time because the FAISS index uses inner
    product - on unit vectors, inner product is cosine similarity, which is what
    makes the returned scores directly interpretable.
    """
    if not texts:
        return np.zeros((0, 0), dtype="float32")

    model = get_model()
    vectors = model.encode(
        texts,
        batch_size=settings.embedding_batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return np.ascontiguousarray(vectors, dtype="float32")


def embed_query(text: str) -> np.ndarray:
    """Embed one query string with the same normalization as documents."""
    return embed_texts([text])[0]


async def embed_texts_async(texts: list[str]) -> np.ndarray:
    return await asyncio.to_thread(embed_texts, texts)


async def embed_query_async(text: str) -> np.ndarray:
    return await asyncio.to_thread(embed_query, text)
