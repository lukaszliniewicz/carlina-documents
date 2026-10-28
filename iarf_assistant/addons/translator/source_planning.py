"""Snapshot-first planning for asynchronous document creation.

The encrypted project owns HTML. The encrypted operation checkpoint owns its
small attestation envelope and frozen planner settings. Queue messages contain
only identities. A worker never re-reads the mutable Document Processor source.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .block_contract import AttestedDocumentSnapshot, DOCUMENT_BLOCK_ENGINE_CONTRACT, BlockContractError
from .block_persistence import plan_and_persist_source_snapshot
from .segmentation_cache import (
    SegmentationCache,
    _chunk_for_inference,
    _exceeds_batch_bytes,
    _narrowed_profile,
    is_batch_unsupported,
    segmentation_planner_settings,
)

ASYNC_SOURCE_PLANNING = "async-source-v1"

# Re-exported for callers/tests resolving the helper via this module. The
# canonical definition lives in segmentation_cache (single source of truth).
SEGMENTATION_IRRELEVANT_PLANNER_KEYS = frozenset({"translation_max_block_chars"})
_narrowed_profile_for_compare = _narrowed_profile


class SourcePlanningNotReady(RuntimeError):
    code = "source_planning_not_ready"
    detail = "The captured source is not ready for translation. Poll the ingestion execution until planning completes; retry source planning on the existing project if it failed."

    def __init__(self):
        super().__init__(self.detail)


def automatic_creation_key(snapshot: AttestedDocumentSnapshot, request_payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"source_content_hash": snapshot.content_hash, "request": dict(request_payload)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "source-" + hashlib.sha256(encoded).hexdigest()


def planning_payload(source_hash: str) -> dict[str, Any]:
    return {
        "planning_contract": ASYNC_SOURCE_PLANNING,
        "source_content_hash": source_hash,
        "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
    }


def freeze_planning_checkpoint(project, snapshot, *, planner_settings: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = snapshot if isinstance(snapshot, AttestedDocumentSnapshot) else AttestedDocumentSnapshot.from_mapping(snapshot)
    if snapshot.document_id != project.document_id:
        raise ValueError("The source snapshot does not belong to this project.")
    project.source_snapshot = snapshot.output_html
    project.source_content_hash = snapshot.content_hash
    if snapshot.captured_at:
        captured = datetime.fromisoformat(snapshot.captured_at)
        project.source_captured_at = captured.astimezone(UTC).replace(tzinfo=None) if captured.tzinfo else captured
    envelope = snapshot.to_dict()
    envelope.pop("output_html")
    return {
        **planning_payload(snapshot.content_hash), "phase": "queued",
        "snapshot_envelope": envelope, "planner_settings": dict(planner_settings),
        "source_language": project.source_lang,
        "translation_unit_mode": (project.settings or {}).get("translation_unit_mode", "semantic_blocks"),
        "source_projections_segmented": 0, "source_sentences_discovered": 0,
    }


def is_async_source_operation(operation) -> bool:
    checkpoint = getattr(operation, "checkpoint_json", None)
    return (
        operation.kind == "plan_document_blocks" and isinstance(checkpoint, dict)
        and checkpoint.get("planning_contract") == ASYNC_SOURCE_PLANNING
    )


def snapshot_for_operation(project, operation) -> AttestedDocumentSnapshot:
    if not is_async_source_operation(operation) or (
        operation.project_id != project.id or operation.owner_user_id != project.user_id
        or operation.source_content_hash != project.source_content_hash
    ):
        raise ValueError("The pending source operation has foreign snapshot pins.")
    checkpoint = operation.checkpoint_json
    envelope = checkpoint.get("snapshot_envelope")
    if not isinstance(envelope, dict):
        raise ValueError("The pending source attestation is missing.")
    snapshot = AttestedDocumentSnapshot.from_mapping({**envelope, "output_html": project.source_snapshot})
    if snapshot.document_id != project.document_id or snapshot.content_hash != operation.source_content_hash:
        raise ValueError("The pending source snapshot changed before planning.")
    return snapshot


def build_pending_source_plan(
    project, operation, *, splitter_factory: Callable[[], Callable[[str], Sequence[str]]],
    report_progress: Callable[[dict[str, Any]], None], cache_dir: Path | None = None,
):
    """Cache exact validated projections without copying another project's plan."""
    snapshot = snapshot_for_operation(project, operation)
    frozen = dict(operation.checkpoint_json)
    progress = {"phase": "preparing_source", "source_projections_segmented": 0,
                "source_projections_total": 0,
                "source_sentences_discovered": 0, "segmentation_cache_hits": 0,
                "segmentation_cache_misses": 0}
    report_progress(dict(progress))
    aligned = frozen["translation_unit_mode"] == "aligned_sentences"
    cache = SegmentationCache(cache_dir, project.user_id) if aligned and cache_dir is not None else None

    def waiting():
        report_progress({**progress, "phase": "waiting_for_segmenter_capacity"})

    with cache.planning(waiting) if cache is not None else nullcontext():
        splitter = None
        batch_segmenter = None
        if aligned:
            progress["phase"] = "segmenter_starting"
            report_progress(dict(progress))
            provider_splitter = splitter_factory()
            identity = getattr(provider_splitter, "segmentation_profile", None)

            def strict_splitter(projection: str):
                # Normalize provider failures, not progress/cancellation callbacks.
                # The batch path bypasses segment_projection's single-call guard.
                try:
                    return provider_splitter(projection)
                except BlockContractError:
                    raise
                except Exception as exc:
                    raise BlockContractError("source-sentence-segmentation-failed") from exc
            profile = None
            legacy_profile = None
            if isinstance(identity, dict):
                profile = {"splitter": identity, "source_language": frozen["source_language"],
                           "planner_settings": segmentation_planner_settings(frozen["planner_settings"]),
                           "adapter": "strict-offsets-v1"}
                # Exact known pre-narrowing shape (full planner settings) for
                # one-run promotion of rows written before the grouping key was
                # excluded. Only this exact shape qualifies; the cache refuses
                # any legacy profile that narrows differently.
                full = {"splitter": identity, "source_language": frozen["source_language"],
                        "planner_settings": dict(frozen["planner_settings"]),
                        "adapter": "strict-offsets-v1"}
                legacy_profile = full if full != profile else None
                previous = frozen.get("segmentation_profile")
                if previous is not None and _narrowed_profile_for_compare(previous) != profile:
                    raise BlockContractError("source-segmenter-identity-changed")
                progress["segmentation_profile"] = profile
                report_progress(dict(progress))
            last_report = time.monotonic()

            def note_done(done: int, total: int, sentences: int = 0) -> None:
                """Record monotonic batch progress without ever reporting 100%."""
                nonlocal last_report
                progress["phase"] = "segmenting_source"
                progress["source_projections_total"] = max(
                    progress["source_projections_total"], int(total),
                )
                progress["source_projections_segmented"] = max(
                    progress["source_projections_segmented"], min(int(done), int(total)),
                )
                progress["source_sentences_discovered"] += int(sentences)
                if cache is not None:
                    cache.heartbeat()
                    progress["segmentation_cache_hits"] = cache.hits
                    progress["segmentation_cache_misses"] = cache.misses
                now = time.monotonic()
                if (
                    progress["source_projections_segmented"] >= progress["source_projections_total"] > 0
                    or now - last_report >= 1.0
                ):
                    report_progress(dict(progress))
                    last_report = now

            def note_single(sentences: int = 0) -> None:
                """Record one legacy per-projection result without a total.

                The batch paths report real totals; the single path has no
                denominator, so it must not equate done==total mid-run
                (that would read as 80% from the first projection).
                Without a total the queue percent stays in the 1-10 band.
                """
                nonlocal last_report
                progress["phase"] = "segmenting_source"
                progress["source_projections_segmented"] += 1
                progress["source_sentences_discovered"] += int(sentences)
                if cache is not None:
                    cache.heartbeat()
                    progress["segmentation_cache_hits"] = cache.hits
                    progress["segmentation_cache_misses"] = cache.misses
                now = time.monotonic()
                if progress["source_projections_segmented"] == 1 or now - last_report >= 1.0:
                    report_progress(dict(progress))
                    last_report = now

            def split(projection: str):
                result = (cache.split(projection, strict_splitter, profile, legacy_profile=legacy_profile)
                          if cache is not None and profile is not None else strict_splitter(projection))
                sentences = (
                    len(result)
                    if isinstance(result, Sequence) and not isinstance(result, (str, bytes))
                    else 0
                )
                note_single(sentences)
                return result

            provider_batch = getattr(provider_splitter, "split_batch", None)
            batch_capable = None
            if callable(provider_batch):
                def batch_capable(texts):
                    try:
                        return provider_batch(texts)
                    except BlockContractError:
                        raise
                    except Exception as exc:
                        if is_batch_unsupported(exc):
                            raise
                        raise BlockContractError("source-sentence-segmentation-failed") from exc

            def batch_split(projection_list) -> list:
                """Segment unique projections via the cache with bounded batches."""
                items = list(projection_list)

                def count_sentences(groups) -> int:
                    total_sentences = 0
                    for part in groups:
                        if isinstance(part, Sequence) and not isinstance(part, (str, bytes)):
                            total_sentences += len(part)
                    return total_sentences

                if cache is not None and profile is not None:
                    grouped = cache.split_many(
                        items, strict_splitter, profile,
                        batch_splitter=batch_capable,
                        on_progress=lambda done, total: note_done(done, total),
                        legacy_profile=legacy_profile,
                    )
                    note_done(len(items), len(items), count_sentences(grouped))
                    return grouped
                if batch_capable is not None:
                    outputs = []
                    chunks = _chunk_for_inference(items)
                    for piece in chunks:
                        if len(piece) == 1 and _exceeds_batch_bytes(piece[0]):
                            # A single valid projection can exceed the batch
                            # byte total while staying within the legacy
                            # per-text bound. Segment it per-text
                            # explicitly; never batch it and never rescue a
                            # batch error for it.
                            grouped = [strict_splitter(piece[0])]
                        else:
                            try:
                                grouped = batch_capable(piece)
                            except Exception as exc:
                                if not is_batch_unsupported(exc):
                                    raise
                                grouped = [strict_splitter(text) for text in piece]
                        if not isinstance(grouped, (list, tuple)) or len(grouped) != len(piece):
                            raise BlockContractError("source-segmentation-cache-batch")
                        outputs.extend(grouped)
                        note_done(len(outputs), len(items),
                                  sum(len(part) for part in grouped
                                      if isinstance(part, Sequence) and not isinstance(part, (str, bytes))))
                    note_done(len(items), len(items))
                    return outputs
                outputs = [strict_splitter(text) for text in items]
                note_done(len(items), len(items),
                          sum(len(part) for part in outputs
                              if isinstance(part, Sequence) and not isinstance(part, (str, bytes))))
                return outputs

            splitter = split
            batch_segmenter = batch_split if aligned else None
        # Planner does DOM work once; cache persists small offset batches during inference.
        # Final canonical plan persistence still belongs to the existing atomic plan path.
        record, _ = plan_and_persist_source_snapshot(
            project, snapshot, source_language=frozen["source_language"], language_profile=None,
            settings=frozen["planner_settings"], sentence_segmenter=splitter,
            sentence_batch_segmenter=batch_segmenter if aligned else None,
        )
    return record, dict(progress)
