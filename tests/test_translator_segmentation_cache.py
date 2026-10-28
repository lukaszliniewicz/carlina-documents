from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from flask import Flask

from iarf_assistant.addons.translator import segmentation_cache as cache_module
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore


@pytest.fixture
def app():
    application = Flask(__name__)
    application.extensions["carlina_encryption"] = EncryptionService(InstallationKeyStore.ephemeral())
    with application.app_context():
        yield application


def test_cache_survives_reopen_without_source_text_and_separates_owner_profile(app, tmp_path):
    calls = []
    def split(text):
        calls.append(text)
        return ["One.", "Two."]
    text = " One.  Two. "
    profile = {"runtime": "one", "language": "en"}
    cache = cache_module.SegmentationCache(tmp_path, 1)
    with cache.planning(lambda: pytest.fail("unexpected contention")):
        assert "".join(cache.split(text, split, profile)) == text
        assert "".join(cache.split(text, split, profile)) == text
        assert (cache.hits, cache.misses) == (1, 1)
    assert len(calls) == 1
    cache = cache_module.SegmentationCache(tmp_path, 1)
    with cache.planning(lambda: pytest.fail("unexpected contention")):
        assert "".join(cache.split(text, split, profile)) == text
        assert cache.hits == 1 and len(calls) == 1
        cache.split(text, split, {**profile, "runtime": "new"})
    other = cache_module.SegmentationCache(tmp_path, 2)
    with other.planning(lambda: pytest.fail("unexpected contention")):
        other.split(text, split, profile)
    assert len(calls) == 3
    for path in tmp_path.iterdir():
        assert b"One." not in path.read_bytes() and b"Two." not in path.read_bytes()


def test_corrupt_offsets_are_not_authority_and_invalid_inference_is_not_cached(app, tmp_path):
    cache = cache_module.SegmentationCache(tmp_path, 1)
    with cache.planning(lambda: None):
        cache.split("One. Two.", lambda _: ["One.", "Two."], {})
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE offsets SET ends=?", (json.dumps([9]),))
    cache = cache_module.SegmentationCache(tmp_path, 1)
    with cache.planning(lambda: None):
        assert cache.split("One. Two.", lambda _: ["One.", "Two."], {}) == ["One. ", "Two."]
        assert cache.misses == 1 and cache.hits == 0
        with pytest.raises(cache_module.BlockContractError):
            cache.split("Missing text.", lambda _: ["Missing"], {})
    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT COUNT(*) FROM offsets").fetchone()[0] == 1


def test_expired_gate_can_be_reclaimed_and_stale_writer_cannot_release_or_publish(app, tmp_path):
    now = [1000.0]
    first = cache_module.SegmentationCache(tmp_path, 1, clock=lambda: now[0])
    second = cache_module.SegmentationCache(tmp_path, 2, clock=lambda: now[0])
    assert first.acquire()
    assert not second.acquire()
    now[0] += cache_module.LEASE_SECONDS + 1
    assert second.acquire()
    with pytest.raises(cache_module.BlockContractError, match="lease-lost"):
        first.heartbeat(force=True)
    first.close()
    second.heartbeat(force=True)
    second.close()


def test_independent_contexts_coalesce_same_projection_without_holding_sqlite_writer(app, tmp_path):
    started = Event()
    release = Event()
    waiting = Event()
    calls = []
    def run(first):
        with app.app_context():
            cache = cache_module.SegmentationCache(tmp_path, 1)
            def splitter(text):
                calls.append(text)
                started.set()
                assert release.wait(10)
                return [text]
            with cache.planning(waiting.set):
                result = cache.split("Synthetic long source.", splitter, {"runtime": "same"})
                # A separate SQLite writer can write even while this planner owns the logical gate.
                with sqlite3.connect(cache.path, timeout=1) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS independent_write(n INTEGER)")
                    db.execute("INSERT INTO independent_write VALUES(1)")
                return result, cache.hits
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, True)
        assert started.wait(10)
        second = pool.submit(run, False)
        assert waiting.wait(10)
        release.set()
        assert first.result(timeout=10)[1] == 0
        assert second.result(timeout=10)[1] == 1
    assert calls == ["Synthetic long source."]


def test_completed_offset_batches_survive_interrupted_plan(app, tmp_path):
    cache = cache_module.SegmentationCache(tmp_path, 1)
    with pytest.raises(RuntimeError, match="interrupted"):
        with cache.planning(lambda: None):
            for index in range(cache_module.COMMIT_BATCH):
                cache.split(f"Synthetic sentence {index}.", lambda text: [text], {})
            raise RuntimeError("interrupted")
    recovered = cache_module.SegmentationCache(tmp_path, 1)
    with recovered.planning(lambda: None):
        recovered.split("Synthetic sentence 0.", lambda _: pytest.fail("reran inference"), {})
        assert recovered.hits == 1
