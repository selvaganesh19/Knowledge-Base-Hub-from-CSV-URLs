"""Tests for the persisted FAISS index wrapper."""

from __future__ import annotations

import numpy as np
import pytest

from app.services.vector_store import FaissStore, index_ids

DIM = 384


def unit(*values) -> np.ndarray:
    """Build a normalised vector of the index's width from a few leading values."""
    vector = np.zeros(DIM, dtype="float32")
    for position, value in enumerate(values):
        vector[position] = value
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


@pytest.fixture
def store(tmp_path) -> FaissStore:
    return FaissStore(path=tmp_path / "index.faiss", meta_path=tmp_path / "meta.json")


class TestBasics:
    def test_empty_index_returns_no_results(self, store):
        assert store.count == 0
        assert store.search(unit(1.0), k=5) == []

    def test_added_vectors_are_found(self, store):
        store.add([1, 2], np.vstack([unit(1.0), unit(0.0, 1.0)]))

        results = store.search(unit(1.0), k=2)

        assert results[0][0] == 1
        assert results[0][1] == pytest.approx(1.0, abs=1e-5)

    def test_scores_are_cosine_similarity(self, store):
        store.add([1], np.vstack([unit(1.0)]))
        _, score = store.search(unit(1.0), k=1)[0]

        assert -1.0 <= score <= 1.0
        assert score == pytest.approx(1.0, abs=1e-5)

    def test_k_is_clamped_to_the_index_size(self, store):
        store.add([1], np.vstack([unit(1.0)]))

        assert len(store.search(unit(1.0), k=50)) == 1

    def test_removed_ids_stop_matching(self, store):
        store.add([1, 2], np.vstack([unit(1.0), unit(1.0)]))
        store.remove([1])

        assert [chunk_id for chunk_id, _ in store.search(unit(1.0), k=5)] == [2]

    def test_re_adding_an_id_replaces_rather_than_duplicates(self, store):
        """IndexIDMap2 appends duplicates, so add() removes existing ids first."""
        store.add([1], np.vstack([unit(1.0)]))
        store.add([1], np.vstack([unit(0.0, 1.0)]))

        assert store.count == 1
        assert store.search(unit(0.0, 1.0), k=1)[0][0] == 1


class TestPersistence:
    def test_index_survives_a_reload(self, store, tmp_path):
        store.add([7], np.vstack([unit(1.0)]))
        store.persist(model_name="test")

        reopened = FaissStore(path=tmp_path / "index.faiss", meta_path=tmp_path / "meta.json")

        assert reopened.count == 1
        assert reopened.search(unit(1.0), k=1)[0][0] == 7

    def test_persist_writes_a_new_version(self, store):
        store.add([1], np.vstack([unit(1.0)]))
        first = store.persist(model_name="test")

        store.add([2], np.vstack([unit(0.0, 1.0)]))
        second = store.persist(model_name="test")

        assert first != second

    def test_corrupt_index_file_is_rebuilt_rather_than_served(self, store, tmp_path):
        store.add([1], np.vstack([unit(1.0)]))
        store.persist(model_name="test")
        (tmp_path / "index.faiss").write_bytes(b"not a faiss index")

        reopened = FaissStore(path=tmp_path / "index.faiss", meta_path=tmp_path / "meta.json")

        assert reopened.count == 0


class TestDirtyFlagRegression:
    """reset() must not be undone by the next ensure_loaded().

    persist() calls ensure_loaded() to read ntotal. Without the dirty flag that
    call saw the on-disk index as newer than the just-reset in-memory one and
    reloaded it - so a rebuild silently wrote the old vectors back to disk.
    """

    def test_reset_is_not_undone_by_persist(self, store, tmp_path):
        store.add([1, 2, 3], np.vstack([unit(1.0), unit(0.0, 1.0), unit(0.0, 0.0, 1.0)]))
        store.persist(model_name="test")

        store.reset()
        store.add([9], np.vstack([unit(1.0)]))
        store.persist(model_name="test")

        reopened = FaissStore(path=tmp_path / "index.faiss", meta_path=tmp_path / "meta.json")
        assert reopened.count == 1
        assert reopened.search(unit(1.0), k=5)[0][0] == 9

    def test_reset_clears_the_index_immediately(self, store):
        store.add([1], np.vstack([unit(1.0)]))
        store.reset()

        assert store.count == 0

    def test_unpersisted_additions_are_not_discarded(self, store):
        store.add([1], np.vstack([unit(1.0)]))
        store.persist(model_name="test")
        store.add([2], np.vstack([unit(0.0, 1.0)]))

        # A call that would normally consider reloading from disk.
        assert store.count == 2

    def test_store_reloads_when_another_process_replaces_the_file(self, store, tmp_path):
        store.add([1], np.vstack([unit(1.0)]))
        store.persist(model_name="test")

        other = FaissStore(path=tmp_path / "index.faiss", meta_path=tmp_path / "meta.json")
        other.reset()
        other.add([5, 6], np.vstack([unit(1.0), unit(0.0, 1.0)]))
        other.persist(model_name="test")

        assert store.count == 2


class TestIdExtraction:
    def test_index_ids_returns_stored_ids(self, store):
        store.add([4, 8], np.vstack([unit(1.0), unit(0.0, 1.0)]))

        assert sorted(index_ids(store._index)) == [4, 8]
