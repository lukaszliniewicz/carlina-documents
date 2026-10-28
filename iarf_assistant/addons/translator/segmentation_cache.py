"""Rebuildable owner-scoped sentence offsets and a cross-process planning gate.

No source or target text is stored here. Keys are private keyed witnesses;
validated offsets carry an authenticated receipt. This independent cache cannot
activate targets or modify historical plans. SQLite transactions never span
inference or waiting, and the single planning lease expires after process death.

Batch planning resolves many unique projections in bounded inference chunks
(see ``split_many``). Reads accept every retained installation key so a key
rotation does not orphan stored offsets; writes always use the active key.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import BlockContractError
from .integrity_receipts import make_receipt, verify_receipt
from .source_segmentation import manifest_spans, segment_projection

LEASE_SECONDS = 900
MAX_CACHE_ROWS = 100_000
MAX_OFFSET_BYTES = 256_000
COMMIT_BATCH = 16
PURPOSE = "translator-segmentation-cache-v1"
# Translator-local inference-chunk contract. These numbers mirror the segmenter
# batch protocol exactly but are deliberately local: Translator must not import
# the sentence_segmenter add-on (add-on boundary). A protocol test asserts the
# two contracts stay equal.
SENTENCE_BATCH_MAX_ITEMS = 32
SENTENCE_BATCH_MAX_BYTES = 1_000_000
# Fallback signal raised by a batch-capable splitter when its server predates
# /split_batch (HTTP 404/405 only). Detected by an explicit opt-in marker
# first (``carlina_batch_unsupported`` duck-typed attribute, so this module
# stays import-clean of the sentence_segmenter add-on), with the legacy
# class-name match retained for splitters written against the older contract.
# Any other error propagates fail-closed; malformed bodies and 409 identity
# drift never fall back.
BATCH_UNSUPPORTED_NAME = "SplitBatchUnsupported"
BATCH_UNSUPPORTED_MARKER = "carlina_batch_unsupported"
_LOOKUP_CHUNK_KEYS = 400

# Downstream block grouping only. Sentence boundaries do not depend on these
# keys, so they are excluded from the segmentation cache identity: changing
# grouping must hit warm sentence offsets. Every other planner setting, the
# splitter runtime fingerprint, stride/block_size/weighting, source language
# and owner stay in the key. Mirrors
# block_planner.TRANSLATION_MAX_BLOCK_CHARS_SETTING without importing the
# planner here.
SEGMENTATION_IRRELEVANT_PLANNER_KEYS = frozenset({"translation_max_block_chars"})


def segmentation_planner_settings(planner_settings: Mapping | None) -> dict:
    """Return the segmentation-relevant subset of frozen planner settings."""
    source = dict(planner_settings) if isinstance(planner_settings, Mapping) else {}
    return {key: value for key, value in source.items() if key not in SEGMENTATION_IRRELEVANT_PLANNER_KEYS}


def _narrowed_profile(stored_profile: object) -> object:
    """Strip grouping-only keys from a persisted profile for comparison."""
    if not isinstance(stored_profile, dict):
        return stored_profile
    narrowed = dict(stored_profile)
    settings = narrowed.get("planner_settings")
    if isinstance(settings, Mapping):
        narrowed["planner_settings"] = segmentation_planner_settings(settings)
    return narrowed


def is_batch_unsupported(exc: BaseException) -> bool:
    """True only for the legacy-server fallback signal, never for failures."""
    if getattr(exc, BATCH_UNSUPPORTED_MARKER, False) is True:
        return True
    return type(exc).__name__ == BATCH_UNSUPPORTED_NAME


def _exceeds_batch_bytes(text: str) -> bool:
    """True when one text alone exceeds the batch byte total.

    Mirrors the segmenter batch protocol exactly (see SENTENCE_BATCH_*
    above): such a single projection stays valid for legacy per-text
    segmentation and must be routed there explicitly.
    """
    return len(text.encode("utf-8")) > SENTENCE_BATCH_MAX_BYTES


def _chunk_for_inference(texts: list[str]) -> list[list[str]]:
    """Split ordered texts into bounded chunks; one oversized item stays whole."""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for text in texts:
        size = len(text.encode("utf-8"))
        if current and (
            len(current) >= SENTENCE_BATCH_MAX_ITEMS
            or current_bytes + size > SENTENCE_BATCH_MAX_BYTES
        ):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(text)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


class SegmentationCache:
    """One connection per planner; short, independent cache transactions only."""

    def __init__(self, data_dir: Path, owner_id: int, *, clock=time.time):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "source-segmentation-v1.sqlite3"
        self.owner_id = int(owner_id)
        self.clock = clock
        self.token = None
        self.last_heartbeat = 0.0
        self.pending = {}
        self.hits = 0
        self.misses = 0
        self.connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS offsets (
                cache_key TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
                ends TEXT NOT NULL, receipt TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS offsets_created ON offsets(created_at);
            CREATE TABLE IF NOT EXISTS planning_gate (
                slot INTEGER PRIMARY KEY CHECK(slot=1), token TEXT NOT NULL,
                deadline REAL NOT NULL
            );
        """)
        self.path.chmod(0o600)

    def acquire(self) -> bool:
        token = self.token or uuid.uuid4().hex
        now = self.clock()
        changed = self.connection.execute("""
            INSERT INTO planning_gate(slot, token, deadline) VALUES(1, ?, ?)
            ON CONFLICT(slot) DO UPDATE SET token=excluded.token, deadline=excluded.deadline
            WHERE planning_gate.deadline <= ? OR planning_gate.token = ?
        """, (token, now + LEASE_SECONDS, now, token)).rowcount
        if changed:
            self.token = token
        return bool(changed)

    def heartbeat(self, *, force=False):
        now = self.clock()
        if not force and now - self.last_heartbeat < 5:
            return
        changed = self.connection.execute(
            "UPDATE planning_gate SET deadline=? WHERE slot=1 AND token=? AND deadline>?",
            (now + LEASE_SECONDS, self.token, now),
        ).rowcount
        if changed != 1:
            raise BlockContractError("source-planning-lease-lost")
        self.last_heartbeat = now

    def flush(self):
        if not self.pending:
            return
        self.heartbeat(force=True)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.executemany(
                "INSERT OR REPLACE INTO offsets VALUES(?, ?, ?, ?, ?)",
                [(key, self.owner_id, json.dumps(ends), receipt, self.clock())
                 for key, (ends, receipt) in self.pending.items()],
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        self.pending.clear()

    def _cache_value(self, projection, profile):
        return {"owner": self.owner_id, "profile": profile, "projection": projection}

    def _lookup_key_rows(self, keys):
        """Fetch {cache_key: (ends_json, receipt)} for candidate keys, owner-scoped."""
        rows = {}
        for index in range(0, len(keys), _LOOKUP_CHUNK_KEYS):
            chunk = keys[index:index + _LOOKUP_CHUNK_KEYS]
            placeholders = ",".join("?" for _ in chunk)
            for row_key, ends, receipt in self.connection.execute(
                f"SELECT cache_key, ends, receipt FROM offsets WHERE cache_key IN ({placeholders}) AND owner_id=?",
                (*chunk, self.owner_id),
            ).fetchall():
                rows[row_key] = (ends, receipt)
        return rows

    def _legacy_value(self, projection, profile, legacy_profile):
        """Return the exact known legacy cache value, or None when inapplicable.

        Only the pre-narrowing full planner-settings shape qualifies: the
        legacy profile must narrow to exactly the active narrowed profile, so
        the splitter runtime/stride/settings, source language, adapter and
        owner semantics are unchanged and only grouping-only keys differ. Any
        other shape (unknown grouping values, changed runtime, non-dict) is
        not inferred and stays a cold miss.
        """
        if legacy_profile is None or not isinstance(legacy_profile, dict):
            return None
        if not isinstance(profile, dict):
            return None
        if legacy_profile == profile or _narrowed_profile(legacy_profile) != profile:
            return None
        return {"owner": self.owner_id, "profile": legacy_profile, "projection": projection}

    def _resolve_legacy(self, projection, legacy_value):
        """Validate one exact legacy row and return (ends, segments) or Nones."""
        legacy_key = private_witness(legacy_value, purpose=PURPOSE)
        if self.pending.get(legacy_key) is not None:
            # Staged under the legacy key in this process (seeded pre-upgrade
            # rows are never in pending, but guard explicitly).
            return None, None
        candidates = private_witness_candidates(legacy_value, purpose=PURPOSE)
        keys = [legacy_key, *(candidate for candidate in candidates if candidate != legacy_key)]
        rows = self._lookup_key_rows(keys)
        matched = next((candidate for candidate in keys if candidate in rows), None)
        if matched is None:
            return None, None
        stored = rows[matched]
        try:
            if len(stored[0]) > MAX_OFFSET_BYTES:
                raise ValueError("oversized cache offsets")
            ends, receipt = json.loads(stored[0]), stored[1]
            verify_receipt(
                stored=receipt, purpose=PURPOSE,
                values={"key": matched, "owner": self.owner_id, "ends": ends},
                code="source-segmentation-cache-receipt",
            )
            spans = manifest_spans(projection, ends)
        except (BlockContractError, TypeError, ValueError):
            # A damaged rebuildable entry is a miss, never source authority.
            self.connection.execute("DELETE FROM offsets WHERE cache_key=?", (matched,))
            return None, None
        return ends, [projection[start:end] for start, end in spans]

    def _resolve_one(self, projection, profile, legacy_profile=None):
        """Return (active_key, ends, segments) with Nones on miss.

        Receipts are verified against the matched witness key, so rows written
        under a retained installation key still verify after rotation. A hit
        under a stale key lazily migrates: the offsets are restaged under the
        active key by the caller-facing path.
        """
        value = self._cache_value(projection, profile)
        key = private_witness(value, purpose=PURPOSE)
        stored = self.pending.get(key)
        matched = key if stored is not None else None
        if stored is None:
            candidates = private_witness_candidates(value, purpose=PURPOSE)
            keys = [key, *(candidate for candidate in candidates if candidate != key)]
            rows = self._lookup_key_rows(keys)
            for candidate in keys:
                if candidate in rows:
                    stored = rows[candidate]
                    matched = candidate
                    break
            if stored is not None:
                try:
                    if len(stored[0]) > MAX_OFFSET_BYTES:
                        raise ValueError("oversized cache offsets")
                    stored = (json.loads(stored[0]), stored[1])
                except (TypeError, ValueError):
                    stored = None
                    matched = None
        if stored is not None and matched is not None:
            ends, receipt = stored
            try:
                verify_receipt(
                    stored=receipt, purpose=PURPOSE,
                    values={"key": matched, "owner": self.owner_id, "ends": ends},
                    code="source-segmentation-cache-receipt",
                )
                spans = manifest_spans(projection, ends)
            except (BlockContractError, TypeError, ValueError):
                # A damaged rebuildable entry is a miss, never source authority.
                self.connection.execute("DELETE FROM offsets WHERE cache_key=?", (matched,))
            else:
                migrate = matched != key
                return key, ends, [projection[start:end] for start, end in spans], migrate
        legacy_value = self._legacy_value(projection, profile, legacy_profile)
        if legacy_value is not None:
            legacy_ends, legacy_segments = self._resolve_legacy(projection, legacy_value)
            if legacy_segments is not None:
                # Promote: restage validated legacy offsets under the narrowed
                # active key by the caller-facing path.
                return key, legacy_ends, legacy_segments, True
        return key, None, None, False

    def _store(self, key, ends):
        self.heartbeat()
        receipt = make_receipt(
            purpose=PURPOSE, values={"key": key, "owner": self.owner_id, "ends": ends},
        )
        if len(json.dumps(ends)) <= MAX_OFFSET_BYTES:
            self.pending[key] = (ends, receipt)
            if len(self.pending) >= COMMIT_BATCH:
                self.flush()

    def split(self, projection, splitter, profile, *, legacy_profile=None):
        segments = self.split_many([projection], splitter, profile, legacy_profile=legacy_profile)
        return segments[0]

    def split_many(self, projections, splitter, profile, *, batch_splitter=None, on_progress=None,
                   legacy_profile=None):
        """Resolve many projections in input order with bounded batched misses.

        Hits (including retained-key rows, promoted pre-narrowing rows and
        in-memory staged offsets) never reach inference. Misses are
        deduplicated and segmented in bounded chunks of at most 32 texts / 1M
        bytes through ``batch_splitter`` when given, else one ``splitter``
        call per unique miss. ``legacy_profile`` is the exact known
        pre-narrowing profile (full planner settings); a validated legacy row
        promotes to the narrowed key. Strict per-projection offset validation
        is identical to :func:`split`. Counters stay per-input-item,
        duplicates included.
        """
        items = list(projections)
        total = len(items)
        if total == 0:
            return []
        for projection in items:
            if not isinstance(projection, str):
                raise BlockContractError("source-segmentation-cache-projection")
        self.heartbeat()
        results: list[list[str] | None] = [None] * total
        miss_order: list[str] = []
        miss_index: dict[str, list[int]] = {}
        miss_projection: dict[str, str] = {}
        for index, projection in enumerate(items):
            key, ends, segments, migrate = self._resolve_one(projection, profile, legacy_profile)
            if segments is not None:
                self.hits += 1
                results[index] = segments
                if migrate and ends is not None:
                    self._store(key, list(ends))
            else:
                self.misses += 1
                if key not in miss_index:
                    miss_index[key] = []
                    miss_order.append(key)
                    miss_projection[key] = projection
                miss_index[key].append(index)
        resolved = sum(1 for item in results if item is not None)
        if on_progress is not None:
            on_progress(resolved, total)
        if miss_order:
            worker = batch_splitter
            ordered_texts = [miss_projection[key] for key in miss_order]
            position = 0
            for chunk in _chunk_for_inference(ordered_texts):
                # Lease/cancel fence at every batch boundary: heartbeat raises
                # source-planning-lease-lost when the planning slot is gone, and
                # an on_progress cancellation aborts before the next chunk.
                self.heartbeat()
                take = miss_order[position:position + len(chunk)]
                position += len(chunk)
                take_texts = [miss_projection[key] for key in take]
                if len(take) == 1 and _exceeds_batch_bytes(take_texts[0]):
                    # A single valid projection can exceed the batch byte
                    # total while staying within the legacy per-text bound.
                    # Segment it per-text explicitly: never send it to the
                    # batch worker and never rescue a batch error for it.
                    grouped = [splitter(take_texts[0])]
                elif worker is not None:
                    try:
                        grouped = worker(take_texts)
                    except Exception as exc:
                        if not is_batch_unsupported(exc):
                            raise
                        worker = None
                        grouped = [splitter(text) for text in take_texts]
                else:
                    grouped = [splitter(text) for text in take_texts]
                if (
                    not isinstance(grouped, (list, tuple))
                    or len(grouped) != len(take)
                ):
                    raise BlockContractError("source-segmentation-cache-batch")
                for key, text, sentences in zip(take, take_texts, grouped):
                    if not isinstance(sentences, (list, tuple)) or isinstance(sentences, (str, bytes, bytearray)):
                        raise BlockContractError("source-segmentation-cache-batch")
                    fixed = tuple(sentences)
                    ends = list(segment_projection(text, lambda _projection, _fixed=fixed: _fixed))
                    self._store(key, ends)
                    spans = manifest_spans(text, ends)
                    segments = [text[start:end] for start, end in spans]
                    for index in miss_index[key]:
                        results[index] = list(segments)
                resolved = sum(1 for item in results if item is not None)
                if on_progress is not None:
                    on_progress(resolved, total)
        return [segments for segments in results if segments is not None]

    def close(self):
        try:
            if self.token is not None:
                self.connection.execute("DELETE FROM planning_gate WHERE slot=1 AND token=?", (self.token,))
        finally:
            self.connection.close()

    @contextmanager
    def planning(self, wait_callback):
        try:
            while not self.acquire():
                wait_callback()
                time.sleep(2)
            # Bounded retention. Eviction only changes future cache hit rates.
            self.connection.execute("""
                DELETE FROM offsets WHERE cache_key IN (
                    SELECT cache_key FROM offsets ORDER BY created_at DESC LIMIT -1 OFFSET ?
                )
            """, (MAX_CACHE_ROWS,))
            yield self
            self.flush()
            self.heartbeat(force=True)
        finally:
            self.close()
