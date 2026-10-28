"""Bounded shared inference and identity of the actual loaded artifacts."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path


class InferenceGate:
    """Only one model call at a time; overload fails instead of retaining a book queue."""

    def __init__(self, max_waiting=4):
        self._slots = threading.BoundedSemaphore(max_waiting + 1)
        self._inference = threading.Lock()
        self._state = threading.Lock()
        self.active = 0
        self.waiting = 0
        self.completed = 0

    def status(self):
        with self._state:
            return {"concurrency_limit": 1, "active_requests": self.active,
                    "waiting_requests": self.waiting, "completed_requests": self.completed}

    @contextmanager
    def enter(self):
        if not self._slots.acquire(blocking=False):
            raise RuntimeError("segmentation_capacity_exhausted")
        with self._state:
            self.waiting += 1
        try:
            with self._inference:
                with self._state:
                    self.waiting -= 1
                    self.active = 1
                try:
                    yield
                finally:
                    with self._state:
                        self.active = 0
                        self.completed += 1
        finally:
            self._slots.release()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def loaded_identity(sat, *, model_id, providers, batch_size, threads, server_path, protocol_path=None):
    """No path-based or model-name-only cache identity, and no artifact copies in RAM."""
    model_path = sat.model.ort_session._model_path
    if not model_path:
        raise ValueError("Loaded ONNX artifact cannot be attested.")
    payload = {
        "contract": "carlina.sat-runtime.v1", "model_id": model_id,
        "model_sha256": _file_hash(model_path),
        "tokenizer_sha256": hashlib.sha256(sat.tokenizer.tokenizer.to_str().encode()).hexdigest(),
        "model_config": vars(sat.model.config),
        "runtime": {name: version(name) for name in ("wtpsplit-lite", "onnxruntime-gpu", "tokenizers", "numpy")},
        "providers": list(providers), "batch_size": batch_size, "threads": threads,
        "server_sha256": _file_hash(server_path), "runtime_sha256": _file_hash(__file__),
    }
    if protocol_path is not None:
        payload["protocol_sha256"] = _file_hash(protocol_path)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"fingerprint": fingerprint, "contract": payload["contract"],
            "model_id": model_id, "model_sha256": payload["model_sha256"],
            "tokenizer_sha256": payload["tokenizer_sha256"], "runtime": payload["runtime"],
            "batch_size": batch_size, "threads": threads}


def execute_batch_split(
    sat,
    gate,
    texts,
    *,
    stride,
    block_size,
    weighting="hat",
    treat_newline_as_space=True,
    batch_size=4,
):
    """Run one bounded batch under a single gate acquisition.

    The pinned model natively accepts a list of texts and returns an
    iterator of per-text sentence lists, which is strongly preferred over
    per-text calls. The outer iterator is eagerly materialized while the
    gate is held so no lazy inference escapes the critical section.
    Per-item groups are then strictly validated: each must already be a
    non-empty ``list[str]`` with matching cardinality. A bare string group
    is malformed and is never coerced (coercion would splinter it into
    characters that could pass validation). No per-text fallback exists
    here: model errors propagate fail-closed, and old-server compat is
    handled one layer up by the HTTP 404/405 contract only.
    """
    items = list(texts)
    if not items:
        raise ValueError("Batch texts must not be empty.")
    base = {
        "stride": stride,
        "block_size": block_size,
        "weighting": weighting,
        "treat_newline_as_space": treat_newline_as_space,
        "batch_size": batch_size,
    }
    with gate.enter():
        materialized = list(sat.split(list(items), **base))
    if len(materialized) != len(items):
        raise RuntimeError("Segmenter returned an invalid batch response.")
    for sentences in materialized:
        if (
            not isinstance(sentences, list)
            or not sentences
            or any(
                not isinstance(item, str) or not item.strip()
                for item in sentences
            )
        ):
            raise RuntimeError("Segmenter returned an invalid batch response.")
    return materialized
