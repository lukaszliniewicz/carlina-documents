"""A deterministic, risk-weighted sample of blocks for review.

Full bilingual review of a long book is rarely affordable; an unweighted
sample spends attention on the safest prose. This draws the sample from
translated blocks not yet completely covered for the requested review kind,
weighting each block by evidence already on record: open QA findings by
severity, open flags, headings, numbers and length. The draw is weighted
sampling without replacement (Efraimidis and Spirakis) with each block's
random key derived from a hash of the seed, block and target version, so the
same seed over the same targets always yields the same sample. The sample is
not recorded by itself: the reviewer records coverage for the blocks actually
read and quotes ``coverage_comment``, which names the sample.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, cast

from .block_contract import canonical_json
from .block_workspace_api import load_block_source_plan
from .qa_diagnostics import MAX_QA_LIMIT, qa_diagnostics
from .review_coverage import review_coverage_manifest
from .review_records import open_flags_by_block
from .scholarly_review import ReviewActor, ScholarlyReviewError, actor_can_read

SAMPLE_CONTRACT = "translator-review-sample-v1"
SAMPLE_KINDS = ("bilingual", "target_reading")
MAX_SAMPLE_SIZE = 200
MAX_SAMPLE_SEED = 2**31 - 1
LONG_BLOCK_CHARS = 1200
_QA_PAGES = 50
_SEVERITY_WEIGHT = {"integrity": 3.0, "warning": 2.0, "advisory": 0.5}
_DIGIT_RE = re.compile(r"\d")


def _qa_by_block(project: Any) -> dict[str, Counter[str]]:
    by_block: dict[str, Counter[str]] = {}
    cursor: str | None = None
    for _page in range(_QA_PAGES):
        result = qa_diagnostics(
            project,
            limit=MAX_QA_LIMIT,
            include_spelling=False,
            include_excerpts=False,
            cursor=cursor,
        )
        for item in cast(Sequence[Mapping[str, Any]], result.get("items") or []):
            counts = by_block.setdefault(str(item.get("block_id")), Counter())
            counts[str(item.get("severity"))] += 1
        pagination = cast(Mapping[str, Any], result.get("pagination") or {})
        cursor = cast(str | None, pagination.get("next_cursor"))
        if not cursor:
            break
    return by_block


def _key(seed: int, block_id: str, version: int, weight: float) -> float:
    digest = hashlib.sha256(
        canonical_json([SAMPLE_CONTRACT, seed, block_id, version]).encode("utf-8")
    ).digest()
    # A uniform value in (0, 1), never exactly 0 so the logarithm is defined.
    uniform = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)
    return math.log(uniform) / weight


def review_sample(
    project: Any,
    *,
    actor: ReviewActor,
    size: object,
    seed: object = 0,
    kind: object = "bilingual",
) -> dict[str, Any]:
    """Return a reproducible risk-weighted sample of blocks to review."""

    if not actor_can_read(actor, project=project):
        raise ScholarlyReviewError("review-read-forbidden")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ScholarlyReviewError("review-sample-size")
    if not 1 <= size <= MAX_SAMPLE_SIZE:
        raise ScholarlyReviewError("review-sample-size")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ScholarlyReviewError("review-sample-seed")
    if not 0 <= seed <= MAX_SAMPLE_SEED:
        raise ScholarlyReviewError("review-sample-seed")
    if not isinstance(kind, str) or kind not in SAMPLE_KINDS:
        raise ScholarlyReviewError("review-sample-kind")
    workspace = load_block_source_plan(project)
    blocks = list(cast(Any, workspace).plan.blocks)
    coverage = review_coverage_manifest(project, _workspace_plan=workspace)
    manifest = {
        str(item["block_id"]): item
        for item in cast(Sequence[Mapping[str, Any]], coverage["blocks"])
    }
    flags = open_flags_by_block(project, str(cast(Any, workspace).record.id), blocks)
    qa = _qa_by_block(project)
    population: list[tuple[float, dict[str, Any]]] = []
    translated = 0
    complete = 0
    for block in blocks:
        block_id = str(block.id)
        entry = manifest.get(block_id)
        version = entry.get("target_version") if entry is not None else None
        if entry is None or not isinstance(version, int):
            continue
        translated += 1
        kinds = cast(Mapping[str, Mapping[str, Any]], entry["kinds"])
        if kinds[kind]["complete"]:
            complete += 1
            continue
        reasons: list[str] = []
        weight = 1.0
        severities = qa.get(block_id, Counter())
        for severity, count in sorted(severities.items()):
            weight += _SEVERITY_WEIGHT.get(severity, 0.5) * count
            reasons.append(
                f"{count} open {severity} QA finding{'s' if count > 1 else ''}"
            )
        open_flags = len(flags.get(block_id, []))
        if open_flags:
            weight += 2.0 * open_flags
            reasons.append(f"{open_flags} open flag{'s' if open_flags > 1 else ''}")
        if getattr(block, "role", "") == "heading":
            weight += 1.0
            reasons.append("heading")
        source = str(getattr(block, "projection", ""))
        if _DIGIT_RE.search(source):
            weight += 1.0
            reasons.append("numbers in source")
        if len(source) > LONG_BLOCK_CHARS:
            weight += 1.0
            reasons.append("long block")
        population.append(
            (
                _key(seed, block_id, version, weight),
                {
                    "block_id": block_id,
                    "order": int(block.order),
                    "target_version": version,
                    "weight": weight,
                    "reasons": reasons,
                },
            )
        )
    chosen = sorted(population, key=lambda pair: (-pair[0], pair[1]["block_id"]))[:size]
    items = sorted(
        (item for _key_value, item in chosen), key=lambda item: item["order"]
    )
    sample_id = hashlib.sha256(
        canonical_json(
            [
                SAMPLE_CONTRACT,
                str(project.id),
                coverage["source_plan_hash"],
                kind,
                seed,
                size,
                [[item["block_id"], item["target_version"]] for item in items],
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "contract": SAMPLE_CONTRACT,
        "sample_id": sample_id,
        "kind": kind,
        "seed": seed,
        "size": size,
        "blocks_translated": translated,
        "already_complete": complete,
        "population": len(population),
        "items": items,
        "coverage_comment": (
            f"Risk-weighted {kind} sample {sample_id} (seed {seed}): "
            f"{len(items)} of {len(population)} uncovered translated blocks."
        ),
        "method": (
            "Weighted sampling without replacement over translated blocks not yet "
            "completely covered for this kind; weight 1 plus open QA findings "
            "(integrity 3, warning 2, advisory 0.5 each), 2 per open flag, and 1 each "
            "for a heading, numbers in the source and a long block. Spelling findings "
            "are not counted."
        ),
    }


__all__ = ["SAMPLE_CONTRACT", "SAMPLE_KINDS", "review_sample"]
