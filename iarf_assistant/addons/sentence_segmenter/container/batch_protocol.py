"""Bounded batch contract for sentence segmentation.

Stdlib-only. The container image ships this file (see Dockerfile COPY) and
imports it as a top-level module; backend code imports the same file under
the segmenter add-on's fully qualified container path.
One canonical file, two import styles, no duplication.

Size bounds compare UTF-8 bytes, not characters: the model tokenizer works on
bytes and multilingual projections vary widely in bytes-per-character. Names
say BYTES wherever bytes are compared.
"""
from __future__ import annotations

MAX_BATCH_TEXTS = 32
MAX_BATCH_TEXT_CHARS = 1_000_000
MAX_BATCH_TOTAL_BYTES = 1_000_000


class SplitBatchUnsupported(RuntimeError):
    """The segmenter server predates /split_batch (HTTP 404/405 only)."""

    carlina_batch_unsupported = True


def validate_batch_texts(texts: object) -> list[str]:
    """Return a validated list copy or raise ValueError (caller bug, no fallback)."""
    if not isinstance(texts, (list, tuple)):
        raise ValueError("Batch texts must be a list of strings.")
    items = list(texts)
    if not 1 <= len(items) <= MAX_BATCH_TEXTS:
        raise ValueError(
            f"Batch must hold 1-{MAX_BATCH_TEXTS} texts, got {len(items)}."
        )
    total = 0
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError(f"Batch item {index} must be a string.")
        if not item.strip():
            raise ValueError(f"Batch item {index} must not be blank.")
        if len(item) > MAX_BATCH_TEXT_CHARS:
            raise ValueError(
                f"Batch item {index} exceeds {MAX_BATCH_TEXT_CHARS} characters."
            )
        total += len(item.encode("utf-8"))
        if total > MAX_BATCH_TOTAL_BYTES:
            raise ValueError(
                f"Batch total exceeds {MAX_BATCH_TOTAL_BYTES} bytes."
            )
    return items


def exceeds_batch_bytes(text: str) -> bool:
    """True when one text alone exceeds the batch byte total.

    Such a single projection stays valid for the legacy per-text endpoint
    (which bounds characters, not bytes) and must be routed there by an
    explicit preflight check. It must never be sent to /split_batch and
    never be rescued from a batch error.
    """
    return len(text.encode("utf-8")) > MAX_BATCH_TOTAL_BYTES


def check_batch_response(payload: object, expected_count: int) -> list[list[str]]:
    """Strictly validate a /split_batch body; malformed never falls back."""
    if not isinstance(payload, dict):
        raise RuntimeError("Segmenter returned an invalid batch response.")
    batch = payload.get("sentences_batch")
    if not isinstance(batch, list) or len(batch) != expected_count:
        raise RuntimeError("Segmenter returned an invalid batch response.")
    checked: list[list[str]] = []
    for sentences in batch:
        if (
            not isinstance(sentences, list)
            or not sentences
            or any(
                not isinstance(item, str) or not item.strip()
                for item in sentences
            )
        ):
            raise RuntimeError("Segmenter returned an invalid batch response.")
        checked.append([str(item) for item in sentences])
    return checked


def chunk_texts(
    texts: list[str],
    *,
    max_items: int = MAX_BATCH_TEXTS,
    max_bytes: int = MAX_BATCH_TOTAL_BYTES,
) -> list[list[str]]:
    """Split ordered texts into bounded chunks; one oversized item stays whole."""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for text in texts:
        size = len(text.encode("utf-8"))
        if current and (
            len(current) >= max_items or current_bytes + size > max_bytes
        ):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(text)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


__all__ = [
    "MAX_BATCH_TEXTS",
    "MAX_BATCH_TEXT_CHARS",
    "MAX_BATCH_TOTAL_BYTES",
    "SplitBatchUnsupported",
    "check_batch_response",
    "chunk_texts",
    "exceeds_batch_bytes",
    "validate_batch_texts",
]
