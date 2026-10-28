"""Batched segmentation cache: order, duplicates, rotation, bounds, scale.

Uses synthetic splitters only; sqlite dir under tmp_path; no live data.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from flask import Flask

from importlib import import_module
_ROOT = "iarf" + "_assistant"
cache_module = import_module(f"{_ROOT}.addons.translator.segmentation_cache")
_analysis = import_module(f"{_ROOT}.addons.translator.analysis_contract")
_contract = import_module(f"{_ROOT}.addons.translator.block_contract")
_encryption = import_module(f"{_ROOT}.core.encryption")

PROFILE = {"runtime": "batch-test", "language": "en"}


@pytest.fixture
def app():
    application = Flask(__name__)
    application.extensions["carlina_encryption"] = _encryption.EncryptionService(_encryption.InstallationKeyStore.ephemeral())
    with application.app_context():
        yield application


@pytest.fixture
def cache(app, tmp_path):
    holder = cache_module.SegmentationCache(tmp_path, 7)
    with holder.planning(lambda: pytest.fail("unexpected contention")):
        yield holder


def _splitter_factory(calls):
    def fake_split(text):
        calls.append(text)
        return [text]
    return fake_split


def _batch_factory(calls):
    def fake_batch(texts):
        calls.append(list(texts))
        return [[text] for text in texts]
    return fake_batch


def test_split_many_preserves_order_dedups_misses_and_counts(cache):
    one_calls, batch_calls = [], []
    texts = ["Alpha. ", "Beta. ", "Alpha. ", "Gamma. "]
    first = cache.split_many(texts, _splitter_factory(one_calls), PROFILE,
                             batch_splitter=_batch_factory(batch_calls))
    assert first == [["Alpha. "], ["Beta. "], ["Alpha. "], ["Gamma. "]]
    assert batch_calls == [["Alpha. ", "Beta. ", "Gamma. "]]
    assert one_calls == []
    assert (cache.hits, cache.misses) == (0, 4)
    second = cache.split_many(texts, _splitter_factory(one_calls), PROFILE,
                              batch_splitter=_batch_factory(batch_calls))
    assert second == first
    assert len(batch_calls) == 1 and one_calls == []
    assert (cache.hits, cache.misses) == (4, 4)


def test_split_many_duplicate_results_are_independent_lists(cache):
    out = cache.split_many(["Same. ", "Same. "], _splitter_factory([]), PROFILE,
                           batch_splitter=_batch_factory([]))
    assert out[0] == out[1] == ["Same. "]
    out[0].append("mutation")
    assert out[1] == ["Same. "]


def test_split_many_mixed_hit_and_miss_batches_only_misses(cache):
    one_calls, batch_calls = [], []
    cache.split("Cached. ", _splitter_factory(one_calls), PROFILE)
    assert one_calls == ["Cached. "]
    out = cache.split_many(["Cached. ", "Fresh. "], _splitter_factory(one_calls),
                           PROFILE, batch_splitter=_batch_factory(batch_calls))
    assert out == [["Cached. "], ["Fresh. "]]
    assert batch_calls == [["Fresh. "]]
    assert one_calls == ["Cached. "]
    assert (cache.hits, cache.misses) == (1, 2)


def test_split_many_rejects_batch_shape_violations(cache):
    with pytest.raises(cache_module.BlockContractError):
        cache.split_many(["A. ", "B. "], _splitter_factory([]), PROFILE,
                         batch_splitter=lambda texts: [["only-one"]])
    with pytest.raises(cache_module.BlockContractError):
        cache.split_many(["A. "], _splitter_factory([]), PROFILE,
                         batch_splitter=lambda texts: ["coerced-string-group"])
    with pytest.raises(ValueError, match="boom"):
        def bad_worker(texts):
            raise ValueError("boom")
        cache.split_many(["A. "], _splitter_factory([]), PROFILE,
                         batch_splitter=bad_worker)


def test_split_many_falls_back_only_for_unsupported_batch_name(cache):
    class SplitBatchUnsupported(Exception):
        """Same name, unrelated class: fallback must match by name only."""

    seen = []

    def legacy_only(texts):
        raise SplitBatchUnsupported("old server")

    out = cache.split_many(["A. ", "B. "], _splitter_factory(seen), PROFILE,
                           batch_splitter=legacy_only)
    assert out == [["A. "], ["B. "]]
    assert seen == ["A. ", "B. "]


def test_split_many_key_rotation_hits_and_migrates(tmp_path):
    # Isolated file-backed store: ephemeral() shares cwd state and its
    # rotate() would load unrelated keys. initialize()+rotate() here is
    # fully contained in tmp_path.
    isolated = Flask(__name__)
    store = _encryption.InstallationKeyStore(tmp_path / "iso-keys.json")
    store.initialize()
    isolated.extensions["carlina_encryption"] = _encryption.EncryptionService(store)
    text = "Rotated. "
    with isolated.app_context():
        first = cache_module.SegmentationCache(tmp_path, 7)
        with first.planning(lambda: None):
            assert first.split(text, lambda t: [t], PROFILE) == ["Rotated. "]
            first.flush()
        assert len(store.key_ids()) == 1
        store.rotate()
        assert len(store.key_ids()) == 2
        calls = []
        second = cache_module.SegmentationCache(tmp_path, 7)
        with second.planning(lambda: None):
            out = second.split_many([text], _splitter_factory(calls), PROFILE,
                                    batch_splitter=_batch_factory([]))
        assert out == [["Rotated. "]]
        assert calls == []
        assert (second.hits, second.misses) == (1, 0)
        active = _analysis.private_witness(
            {"owner": 7, "profile": PROFILE, "projection": text},
            purpose=cache_module.PURPOSE,
        )
        with sqlite3.connect(second.path) as db:
            keys = {row[0] for row in db.execute("SELECT cache_key FROM offsets")}
        assert active in keys and len(keys) == 2


def test_split_many_oversize_single_routes_to_per_text_splitter(cache):
    heavy = "é" * 600_000
    assert len(heavy.encode("utf-8")) > cache_module.SENTENCE_BATCH_MAX_BYTES
    assert len(heavy) <= 1_000_000  # still valid for legacy /split
    one_calls, batch_calls = [], []
    out = cache.split_many([heavy], _splitter_factory(one_calls), PROFILE,
                           batch_splitter=_batch_factory(batch_calls))
    assert out == [[heavy]]
    assert one_calls == [heavy] and batch_calls == []
    normal = ["Plain. "]
    out = cache.split_many([normal[0], heavy], _splitter_factory(one_calls),
                           PROFILE, batch_splitter=_batch_factory(batch_calls))
    assert out == [[normal[0]], [heavy]]
    assert batch_calls == [[normal[0]]]
    # heavy hit the cache from the first call: no second inference
    assert one_calls == [heavy]


def test_split_many_damaged_row_is_remiss_not_authority(app, tmp_path):
    holder = cache_module.SegmentationCache(tmp_path, 7)
    with holder.planning(lambda: None):
        holder.split("One. Two.", lambda _: ["One.", "Two."], PROFILE)
        holder.flush()
    with sqlite3.connect(holder.path) as db:
        db.execute("UPDATE offsets SET ends='[999999]'")
    batch_calls = []
    later = cache_module.SegmentationCache(tmp_path, 7)
    with later.planning(lambda: None):
        out = later.split_many(["One. Two."], lambda _: ["One.", "Two."],
                               PROFILE, batch_splitter=_batch_factory(batch_calls))
    assert out == [["One. Two."]]
    assert batch_calls == [["One. Two."]]
    assert (later.hits, later.misses) == (0, 1)


def test_3964_projection_call_count_benchmark(app, tmp_path):
    texts = [f"Quelle sentence {index:04d} with ordinary filler words. " for index in range(3964)]
    assert len(texts) == 3964
    batched = cache_module.SegmentationCache(tmp_path / "batched", 7)
    batch_calls, one_calls = [], []
    started = time.monotonic()
    with batched.planning(lambda: None):
        out = batched.split_many(texts, _splitter_factory(one_calls), PROFILE,
                                 batch_splitter=_batch_factory(batch_calls))
    batched_seconds = time.monotonic() - started
    assert out == [[text] for text in texts]
    assert one_calls == []
    assert len(batch_calls) == 124, len(batch_calls)  # ceil(3964/32)
    assert all(len(chunk) <= 32 for chunk in batch_calls)
    assert sum(len(chunk) for chunk in batch_calls) == 3964
    legacy = cache_module.SegmentationCache(tmp_path / "legacy", 7)
    legacy_calls = []
    with legacy.planning(lambda: None):
        legacy_out = legacy.split_many(texts, _splitter_factory(legacy_calls), PROFILE)
    assert legacy_out == out
    assert len(legacy_calls) == 3964
    print(f"\n3964 projections: batch={len(batch_calls)} calls in {batched_seconds:.1f}s "
          f"vs legacy={len(legacy_calls)} calls")
