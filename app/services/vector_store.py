"""Persisted FAISS index.

Design notes:

* The index is `IndexIDMap2(IndexFlatIP(384))` with the FAISS id set equal to
  `Chunk.id`. The index therefore stores only (id, vector), and a search result
  resolves back to text, offsets and a source URL with one SQL query.
* Inner product over L2-normalized vectors *is* cosine similarity, so scores come
  back in [-1, 1] and mean exactly what they appear to mean. IndexFlatL2 would
  need a `1 - d^2/2` conversion and would still lose the sign.
* IndexFlatIP is a flat, exact search. It is the right choice at this scale by a
  wide margin - the dataset is dozens of pages, not millions of vectors. Swapping
  to IndexHNSWFlat later changes the constructor and nothing else.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import numpy as np

from app.config import FAISS_DIR, FAISS_INDEX_PATH, FAISS_META_PATH, get_settings

settings = get_settings()

INDEX_DIM = 384


class FaissStore:
    """Thread-safe wrapper around a persisted FAISS index."""

    def __init__(
        self,
        path: Path = FAISS_INDEX_PATH,
        meta_path: Path = FAISS_META_PATH,
    ) -> None:
        self.path = Path(path)
        self.meta_path = Path(meta_path)
        self._index = None
        self._dim = INDEX_DIM
        self._version = ""
        # True while the in-memory index holds changes that are not on disk yet.
        # Without this, a reset() or an add() would be discarded on the next
        # call: the disk copy would look "newer" than the in-memory one and be
        # reloaded over the top of it.
        self._dirty = False
        self._lock = threading.RLock()

    # -- loading ------------------------------------------------------------

    def _new_index(self, dim: int):
        import faiss

        return faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

    def ensure_loaded(self) -> None:
        """Load the index from disk on first use, and reload if another process
        replaced it. Uncommitted in-memory changes always win."""
        with self._lock:
            if self._index is not None and (self._dirty or not self._disk_is_newer()):
                return
            self._load()

    def _disk_is_newer(self) -> bool:
        if self._dirty:
            return False
        disk = self._read_meta()
        return bool(disk.get("version")) and disk["version"] != self._version

    def _load(self) -> None:
        import faiss

        FAISS_DIR.mkdir(parents=True, exist_ok=True)
        meta = self._read_meta()

        if self.path.exists() and self.path.stat().st_size > 0:
            try:
                self._index = faiss.read_index(str(self.path))
                self._dim = int(meta.get("dim") or INDEX_DIM)
                self._version = str(meta.get("version") or "")
                self._dirty = False
                return
            except (RuntimeError, OSError):
                # A corrupt or unreadable index is rebuilt rather than served.
                pass

        self._index = self._new_index(INDEX_DIM)
        self._dim = INDEX_DIM
        self._version = ""
        self._dirty = False
        self._write_meta(version="", model_name="")

    def _read_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, version: str, model_name: str, count: int | None = None) -> None:
        FAISS_DIR.mkdir(parents=True, exist_ok=True)
        # Read ntotal directly rather than via the `count` property, which would
        # re-enter ensure_loaded() while the load itself is still in progress.
        current = int(self._index.ntotal) if self._index is not None else 0
        payload = {
            "version": version,
            "dim": self._dim,
            "model_name": model_name,
            "count": current if count is None else count,
        }
        temp_path = self.meta_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp_path, self.meta_path)

    # -- mutation -----------------------------------------------------------

    def add(self, ids: list[int], vectors: np.ndarray) -> None:
        """Add vectors under explicit ids. Re-adding an id replaces its vector."""
        if len(ids) == 0:
            return

        with self._lock:
            self.ensure_loaded()
            matrix = np.ascontiguousarray(vectors, dtype="float32")
            id_array = np.asarray(ids, dtype="int64")

            existing = self._existing_ids(set(ids))
            if existing:
                self._index.remove_ids(np.asarray(sorted(existing), dtype="int64"))

            self._index.add_with_ids(matrix, id_array)
            self._dirty = True

    def remove(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self.ensure_loaded()
            self._index.remove_ids(np.asarray(ids, dtype="int64"))
            self._dirty = True

    def reset(self) -> None:
        with self._lock:
            self._index = self._new_index(self._dim)
            self._version = ""
            self._dirty = True

    def _existing_ids(self, candidate_ids: set[int]) -> set[int]:
        """Which of these ids are already in the index.

        IndexIDMap2.add_with_ids does not replace on a duplicate id - it appends,
        which would leave two vectors under one id. So any id being re-added is
        removed first.
        """
        if self._index is None or self._index.ntotal == 0:
            return set()
        stored = np.asarray(index_ids(self._index), dtype="int64")
        return set(stored.tolist()) & candidate_ids

    # -- introspection ------------------------------------------------------

    def describe(self) -> dict:
        """Report what the index actually is, for stats output and diagnostics.

        Exposed rather than reaching into the private fields from outside: "which
        index and which metric is this really" is the first question when retrieval
        quality looks wrong.
        """
        with self._lock:
            self.ensure_loaded()
            return {
                "index_type": type(self._index).__name__ if self._index is not None else "none",
                "metric": "inner product (cosine on normalized vectors)",
                "dim": self._dim,
                "vectors": int(self._index.ntotal) if self._index is not None else 0,
                "version": self._version or None,
                "path": str(self.path),
                "persisted": self.path.exists(),
            }

    def ids(self) -> list[int]:
        """Every vector id currently in the index."""
        with self._lock:
            self.ensure_loaded()
            if self._index is None:
                return []
            return sorted(index_ids(self._index))

    # -- query --------------------------------------------------------------

    def search(self, vector: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
        """Return [(chunk_id, cosine_similarity)] ordered best first."""
        with self._lock:
            self.ensure_loaded()
            if self._index is None or self._index.ntotal == 0:
                return []

            k = max(1, min(k, int(self._index.ntotal)))
            query = np.ascontiguousarray(vector.reshape(1, -1), dtype="float32")
            scores, ids = self._index.search(query, k)

        results = []
        for score, chunk_id in zip(scores[0], ids[0], strict=False):
            if chunk_id == -1:
                continue
            results.append((int(chunk_id), float(score)))
        return results

    @property
    def count(self) -> int:
        with self._lock:
            self.ensure_loaded()
            return int(self._index.ntotal) if self._index is not None else 0

    # -- persistence --------------------------------------------------------

    def persist(self, model_name: str, version: str | None = None) -> str:
        """Write the index and its metadata sidecar atomically.

        A new version is minted whenever there are uncommitted changes, because the
        version is how another process holding an older copy of this index knows it
        has been replaced. Reusing the version across writes would leave that
        process serving stale vectors indefinitely.
        """
        import faiss

        with self._lock:
            self.ensure_loaded()

            if version is None:
                version = uuid.uuid4().hex if (self._dirty or not self._version) else self._version

            FAISS_DIR.mkdir(parents=True, exist_ok=True)

            temp_path = self.path.with_suffix(".faiss.tmp")
            faiss.write_index(self._index, str(temp_path))
            os.replace(temp_path, self.path)

            self._version = version
            self._dirty = False
            self._write_meta(version=version, model_name=model_name)
            return version

    @property
    def version(self) -> str:
        with self._lock:
            return self._version


def index_ids(index) -> list[int]:
    """Extract the id list from an IndexIDMap2 without scanning vectors."""
    import faiss

    return [int(value) for value in faiss.vector_to_array(index.id_map)]


store = FaissStore()
