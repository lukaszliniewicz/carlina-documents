"""The translator's report: what a finished run still owes its reader.

One bounded read assembles the evidence a translator hands back with a book:
blocks still untranslated, open flags, glossary renderings nobody verified,
review coverage by kind, the QA picture including dismissed findings, and the
glossary terms rendered inconsistently. It reads and never writes; every count
comes from the same verified records the individual tools return, and each
list states when it was cut short.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from .qa_diagnostics import MAX_QA_LIMIT, qa_diagnostics
from .review_coverage import review_coverage_manifest
from .review_records import list_flags
from .scholarly_review import ReviewActor, ScholarlyReviewError, actor_can_read
from .working_glossary import read_glossary

REPORT_CONTRACT = "translator-report-v1"
MAX_REPORT_ITEMS = 100
MAX_CONSISTENCY_BLOCKS = 20
# Terminology findings are paged; this bounds the scan, not the counts QA
# reports in its summary.
MAX_CONSISTENCY_PAGES = 20


def _glossary_rows(project: Any, verification: str) -> dict[str, Any]:
    result = read_glossary(
        project,
        verification=verification,
        limit=MAX_REPORT_ITEMS,
        include_observations=False,
    )
    entries = cast(Sequence[Mapping[str, Any]], result.get("items") or [])
    pagination = cast(Mapping[str, Any], result.get("pagination") or {})
    return {
        "entries": [
            {
                "concept_id": entry.get("concept_id"),
                "source_term": entry.get("source_term"),
                "target_term": entry.get("target_term"),
                "kind": entry.get("kind", "term"),
            }
            for entry in entries
        ],
        "truncated": pagination.get("next_offset") is not None,
        "by_verification": cast(Mapping[str, Any], result.get("summary") or {}).get(
            "by_verification", {}
        ),
    }


def _consistency(project: Any) -> dict[str, Any]:
    """Group open terminology findings by glossary concept, worst first."""

    groups: dict[str, dict[str, Any]] = {}
    blocks: dict[str, list[str]] = defaultdict(list)
    cursor: str | None = None
    complete = False
    for _page in range(MAX_CONSISTENCY_PAGES):
        result = qa_diagnostics(
            project,
            categories=("terminology",),
            limit=MAX_QA_LIMIT,
            include_spelling=False,
            include_excerpts=False,
            cursor=cursor,
        )
        for item in cast(Sequence[Mapping[str, Any]], result.get("items") or []):
            evidence = cast(Mapping[str, Any], item.get("evidence") or {})
            key = str(evidence.get("concept_id") or evidence.get("source_term"))
            group = groups.setdefault(
                key,
                {
                    "concept_id": evidence.get("concept_id"),
                    "source_term": evidence.get("source_term"),
                    "preferred_target": evidence.get("preferred_target"),
                    "match_policy": evidence.get("match_policy"),
                    "divergent_blocks": 0,
                },
            )
            block_id = str(item.get("block_id"))
            if block_id not in blocks[key]:
                blocks[key].append(block_id)
                group["divergent_blocks"] += 1
        pagination = cast(Mapping[str, Any], result.get("pagination") or {})
        cursor = cast(str | None, pagination.get("next_cursor"))
        if not cursor:
            complete = True
            break
    ordered = sorted(
        groups.items(),
        key=lambda pair: (
            -int(pair[1]["divergent_blocks"]),
            str(pair[1]["source_term"]),
        ),
    )
    terms = [
        {**group, "block_ids": blocks[key][:MAX_CONSISTENCY_BLOCKS]}
        for key, group in ordered[:MAX_REPORT_ITEMS]
    ]
    return {
        "terms": terms,
        "truncated": not complete or len(ordered) > MAX_REPORT_ITEMS,
        "scope": "glossary terms with a target rendering; names outside the glossary are not compared",
    }


def translator_report(project: Any, *, actor: ReviewActor) -> dict[str, Any]:
    """Assemble the hand-back report for one document-block project."""

    if not actor_can_read(actor, project=project):
        raise ScholarlyReviewError("review-read-forbidden")
    coverage = review_coverage_manifest(project)
    manifest_blocks = cast(Sequence[Mapping[str, Any]], coverage["blocks"])
    untranslated = [
        str(block["block_id"])
        for block in manifest_blocks
        if block.get("target_revision_id") is None
    ]
    flags = list_flags(project, actor=actor, states=("open",), limit=MAX_REPORT_ITEMS)
    flag_summary = cast(Mapping[str, Any], flags["summary"])
    flag_pagination = cast(Mapping[str, Any], flags["pagination"])
    unverified = _glossary_rows(project, "unverified")
    coined = _glossary_rows(project, "coined")
    by_verification = unverified.pop("by_verification")
    coined.pop("by_verification")
    qa = qa_diagnostics(project, limit=1, include_excerpts=False)
    qa_summary = cast(Mapping[str, Any], qa["summary"])
    return {
        "contract": REPORT_CONTRACT,
        "project_id": str(project.id),
        "source_plan_id": coverage["source_plan_id"],
        "translation": {
            "blocks_total": len(manifest_blocks),
            "blocks_translated": len(manifest_blocks) - len(untranslated),
            "untranslated_block_ids": untranslated[:MAX_REPORT_ITEMS],
            "untranslated_truncated": len(untranslated) > MAX_REPORT_ITEMS,
        },
        "flags": {
            "open_total": flag_summary.get("open", 0),
            "open_by_kind": flag_summary.get("open_by_kind", {}),
            "open": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "block_id",
                        "order",
                        "kind",
                        "origin",
                        "body",
                        "excerpt",
                    )
                }
                for item in cast(Sequence[Mapping[str, Any]], flags["items"])
            ],
            "truncated": bool(flag_pagination.get("has_more")),
        },
        "glossary": {
            "by_verification": by_verification,
            "unverified": unverified,
            "coined": coined,
        },
        "coverage": {"by_kind": coverage["summary"]},
        "qa": {
            "ruleset": qa.get("ruleset"),
            "open_total": qa_summary.get("total"),
            "dismissed_total": qa_summary.get("dismissed_total"),
            "by_category": qa_summary.get("by_category"),
            "by_severity": qa_summary.get("by_severity"),
            "alignment_coverage": qa_summary.get("alignment_coverage"),
            "truncated": qa_summary.get("truncated"),
        },
        "consistency": _consistency(project),
        "disclaimer": (
            "Counts come from deterministic records. Open QA findings and flags are "
            "evidence for review, coverage records what someone declared they read, "
            "and nothing here verifies meaning."
        ),
    }


__all__ = ["REPORT_CONTRACT", "translator_report"]
