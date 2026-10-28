"""Live, owner-scoped model terminology, separate from frozen research dossiers.

Reuse encrypted glossary revisions and occurrence receipts. No extra database,
model provider call, fabricated human approval or mutation of prepared packets.
"""
from __future__ import annotations

import copy
import re
import unicodedata
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ...extensions import db
from .analysis_contract import (
    GlossaryPayload,
    private_witness,
    private_witness_candidates,
)
from .block_contract import BlockContractError, canonical_json, sha256_text
from .block_persistence import rehydrate_source_plan, require_active_source_plan
from .block_workspace_api import _load_glossary_source_plan
from .glossary_contract import GlossaryTermInput, GlossaryUsage
from .glossary_persistence import (
    create_glossary_concept,
    find_glossary_revisions_for_term,
    persist_glossary_occurrences,
    persist_glossary_revision,
    rehydrate_glossary_revision,
    verify_glossary_active,
)
from .models import (
    TranslationGlossaryActive,
    TranslationGlossaryConcept,
    TranslationGlossaryRevision,
    TranslationGlossarySuggestion,
    TranslationResearchDossierGlossaryProposal,
    TranslationWindowAttempt,
)
from .window_contract import ContextSlot

GLOSSARY_CONTRACT = "translator-working-glossary-v1"
GLOSSARY_RESOLVER = "working-glossary-v1"
WINDOW_BLOCK_INDEX_CONTRACT = "translator-window-block-index-v1"
MAX_WINDOW_TERMS = 48
MAX_WINDOW_CHARS = 24_000
# Historical name; the unit is UTF-8 bytes measured via _page_bytes.
MAX_READ_CHARS = 128_000


def _sense(payload: GlossaryPayload) -> str:
    return str((payload.usage or {}).get("sense", ""))


def _normalize_match(value: object) -> str:
    """In-memory matching only; stored digests keep unicode-casefold-v1 bytes."""
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _same_sense(left: str, right: str) -> bool:
    return _normalize_match(left) == _normalize_match(right)


# Normalized (NFKC + casefold + whitespace) script ranges where words run
# without spaces. Lookup-only substring fallback applies here; spaced scripts
# and Hangul keep strict word boundaries (no morphology analyzer claimed).
_UNSPACED_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x2E80, 0x2EFF), (0x2F800, 0x2FA1F),
    (0x20000, 0x2A6DF), (0x2A600, 0x2B73F), (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF), (0x2CEB0, 0x2EBE0), (0x30000, 0x3134A),
    (0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF),
    (0x0E00, 0x0E7F), (0x0E80, 0x0EFF),
    (0x1000, 0x109F), (0xAA60, 0xAA7F), (0xA9E0, 0xA9FE),
    (0x1780, 0x17FF), (0x19E0, 0x19FF),
)


def _has_unspaced_script(value: str) -> bool:
    for char in value:
        point = ord(char)
        for low, high in _UNSPACED_SCRIPT_RANGES:
            if low <= point <= high:
                return True
    return False


def _contains_form(haystack_normalized: str, form: object) -> bool:
    """Word-boundary match, plus lookup-only substring for unspaced scripts.

    Short ASCII/spaced-script headwords never fire inside compounds (Rat does
    not match Beratung). Han (including supplementary ideographs),
    Hiragana/Katakana, Thai (likewise Lao/Myanmar/Khmer) also match as a plain
    substring of the normalized window text, because those scripts run without
    spaces and ``\\\\w`` boundaries would otherwise suppress every continuous
    occurrence. Hangul keeps strict boundaries: no morphology analyzer is
    claimed. This is a retrieval advisory for window assembly, not an
    automatic equivalence claim; exact anchored occurrence quotes still resolve
    by exact codepoint against the canonical source projection.
    """
    needle = _normalize_match(form)
    if not needle:
        return False
    if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack_normalized) is not None:
        return True
    return _has_unspaced_script(needle) and needle in haystack_normalized


def _entry(revision: Any, record: Any, *, locked: bool = False) -> dict[str, Any]:
    payload = rehydrate_glossary_revision(revision, record)
    usage = payload.usage or {}
    return {
        "concept_id": revision.concept_id,
        "revision_id": revision.id,
        "revision": revision.revision_number,
        "source_term": payload.source_term,
        "target_term": payload.target_term,
        "notes": payload.notes,
        "sense": usage.get("sense", ""),
        "match_policy": usage.get("match_policy", "preferred"),
        "source_forms": copy.deepcopy(usage.get("source_forms", [payload.source_term])),
        "accepted_target_forms": copy.deepcopy(usage.get("accepted_target_forms", [])),
        "observations": copy.deepcopy(usage.get("observations", [])),
        "provenance": usage.get("provenance", revision.author_type),
        "authority": "human_locked" if locked else "model_advisory",
        "scope": payload.scope,
    }


def _latest_revisions(record: Any) -> list[Any]:
    latest = (
        select(TranslationGlossaryRevision.concept_id.label("concept"),
               func.max(TranslationGlossaryRevision.revision_number).label("number"))
        .where(TranslationGlossaryRevision.source_plan_id == record.id,
               TranslationGlossaryRevision.project_id == record.project_id)
        .group_by(TranslationGlossaryRevision.concept_id)
        .subquery()
    )
    return list(db.session.scalars(
        select(TranslationGlossaryRevision)
        .join(latest, (latest.c.concept == TranslationGlossaryRevision.concept_id)
              & (latest.c.number == TranslationGlossaryRevision.revision_number))
        .order_by(TranslationGlossaryRevision.concept_id)
    ))


def _entries(record: Any) -> list[dict[str, Any]]:
    """Only current proposals and human decisions; never resurrect rejects.

    Pinned dossier terminology remains discoverable for subsequent edits via
    its concept_id: currently-proposed dossier revisions appear as ordinary
    model-advisory entries. A linked revision is hidden only when every
    dossier link to it corresponds to a rejected external suggestion with the
    exact same dossier revision, source, target and notes; unrelated reviewer
    rows, other dossiers, or different targets never hide it, and an identical
    proposal reused by another still-proposed dossier stays visible.
    Superseded revisions are hidden because only the latest per concept lists.
    """
    link_map = _dossier_links_map(record)
    rejected_keys = _rejected_suggestion_keys(record)
    result = []
    for revision in _latest_revisions(record):
        active = db.session.get(TranslationGlossaryActive, revision.concept_id)
        if active is not None:
            result.append(_entry(verify_glossary_active(active), record, locked=True))
        elif revision.revision_state != "proposal" or _is_hidden_link(revision, record, link_map, rejected_keys):
            continue
        else:
            result.append(_entry(revision, record))
    return result


def _dossier_links_map(record: Any) -> dict[str, set[str]]:
    """Glossary revision id to exact linked dossier revision ids."""
    rows = db.session.scalars(
        select(TranslationResearchDossierGlossaryProposal)
        .where(TranslationResearchDossierGlossaryProposal.source_plan_id == record.id,
               TranslationResearchDossierGlossaryProposal.project_id == record.project_id)
    ).all()
    link_map: dict[str, set[str]] = {}
    for link in rows:
        link_map.setdefault(link.glossary_revision_id, set()).add(link.dossier_revision_id)
    return link_map


def _rejected_suggestion_keys(record: Any) -> set[tuple[str, str, str, str | None]]:
    """Exact keys of rejected external dossier suggestions, never approvals."""
    rows = db.session.scalars(
        select(TranslationGlossarySuggestion)
        .where(TranslationGlossarySuggestion.source_plan_id == record.id,
               TranslationGlossarySuggestion.project_id == record.project_id,
               TranslationGlossarySuggestion.state == "rejected",
               TranslationGlossarySuggestion.proposer_kind == "external")
    ).all()
    keys: set[tuple[str, str, str, str | None]] = set()
    for row in rows:
        dossier_id = getattr(row, "dossier_revision_id", None)
        if not dossier_id:
            continue
        source = row.source_term.strip() if isinstance(row.source_term, str) else row.source_term
        target = row.target_term.strip() if isinstance(row.target_term, str) else row.target_term
        notes = row.notes if row.notes else None
        keys.add((dossier_id, source, target, notes))
    return keys


def _is_hidden_link(revision: Any, record: Any, link_map: dict[str, set[str]],
                    rejected_keys: set[tuple[str, str, str, str | None]]) -> bool:
    """Hide only exact rejected links; reviewer rows and other targets stay."""
    dossier_ids = link_map.get(revision.id)
    if not dossier_ids:
        return False
    try:
        payload = rehydrate_glossary_revision(revision, record)
    except BlockContractError:
        return True
    notes = payload.notes if payload.notes else None
    return bool(dossier_ids) and all(
        (dossier_id, payload.source_term, payload.target_term, notes) in rejected_keys
        for dossier_id in dossier_ids
    )


def _current_lemma_map(record: Any) -> dict[tuple[str, str], dict[str, str]]:
    """Verified CURRENT concept map keyed by NFKC lemma and sense.

    Built per proposal batch from the owner- and source-plan-scoped current
    entries (latest revision per concept, exact rejected dossier links hidden,
    human locks included). Legacy ``normalized_term`` digests are casefold-only
    and miss NFKC equivalents such as fullwidth forms, so this map compares the
    CURRENT ``_normalize_match`` (NFKC + casefold + whitespace) lemma AND sense.
    Stored ``normalized_term`` hashes, profiles, and historical receipts are
    untouched. Different senses are distinct keys and remain allowed.
    """
    mapping: dict[tuple[str, str], dict[str, str]] = {}
    for entry in _entries(record):
        key = (_normalize_match(entry.get("source_term")),
               _normalize_match(entry.get("sense", "")))
        if not key[0]:
            continue
        mapping.setdefault(key, {})[str(entry["concept_id"])] = str(entry.get("authority", ""))
    return mapping


def _map_collision_code(mapping: dict[tuple[str, str], dict[str, str]],
                        *, key: tuple[str, str], self_concept_id: str | None) -> str | None:
    others = {cid: auth for cid, auth in mapping.get(key, {}).items()
              if cid != self_concept_id}
    if not others:
        return None
    if any(auth == "human_locked" for auth in others.values()):
        return "glossary-human-locked"
    return "glossary-concept-exists"


def _map_upsert(mapping: dict[tuple[str, str], dict[str, str]], *,
                concept_id: str, lemma: str, sense: str, authority: str = "model_advisory") -> None:
    key = (lemma, sense)
    for existing_key in list(mapping):
        if concept_id in mapping[existing_key]:
            del mapping[existing_key][concept_id]
            if not mapping[existing_key]:
                del mapping[existing_key]
    mapping.setdefault(key, {})[concept_id] = authority


def _page_bytes(page: Any) -> int:
    """Honest UTF-8 response size; len(str) counts codepoints, not bytes."""
    return len(canonical_json(page).encode("utf-8"))


def read_glossary(project: Any, *, query: str = "", offset: int = 0, limit: int = 50,
                  include_observations: bool = True) -> dict[str, Any]:
    """Bounded paginated read with an honest byte budget, never silent loss.

    Concepts are the pagination unit (one latest revision per concept, so the
    per-request scan is over terminology concepts, not book blocks). The
    ``limit`` cap (max 200) bounds entry count; ``MAX_READ_CHARS`` bounds
    actual UTF-8 response bytes: when a full page would exceed the budget, tail
    entries move to the next page via an honest ``next_offset`` instead of
    being silently dropped. A single entry that still exceeds the budget is
    returned once, explicitly marked ``oversized_entry`` in pagination, so the
    caller can retry with ``include_observations=False``; pagination still
    advances and never loops. ``include_observations=False`` returns a bounded
    compact projection (observations replaced by an explicit
    ``observation_count``) for cheap polling; full evidence stays available
    on the next read.
    """
    if (not isinstance(query, str) or len(query) > 1000
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 200
            or not isinstance(include_observations, bool)):
        raise BlockContractError("glossary-read-parameters")
    record, _ = _load_glossary_source_plan(project)
    entries = _entries(record)
    needle = _normalize_match(query)
    if needle:
        normalized_entries = []
        for entry in entries:
            haystacks = [_normalize_match(value) for value in
                         (entry["source_term"], entry["target_term"], entry["sense"], *entry["source_forms"])]
            if any(needle in haystack for haystack in haystacks):
                normalized_entries.append(entry)
        entries = normalized_entries
    total = len(entries)
    page = [copy.deepcopy(item) for item in entries[offset:offset + limit]]
    if not include_observations:
        for item in page:
            item["observation_count"] = len(item.get("observations", []))
            item["observations"] = []
    truncated_by_budget = False
    while len(page) > 1 and _page_bytes(page) > MAX_READ_CHARS:
        page.pop()
        truncated_by_budget = True
    if page:
        next_offset = offset + len(page) if offset + len(page) < total else None
    else:
        next_offset = offset if offset < total else None
    pagination: dict[str, Any] = {"offset": offset, "limit": limit, "total": total,
                       "next_offset": next_offset}
    if truncated_by_budget:
        pagination["truncated_by_budget"] = True
    if len(page) == 1 and _page_bytes(page) > MAX_READ_CHARS:
        pagination["oversized_entry"] = True
    if not include_observations:
        pagination["compact"] = "observations_omitted"
    return {
        "contract": GLOSSARY_CONTRACT,
        "project_id": project.id,
        "source_plan_id": record.id,
        "items": page,
        "pagination": pagination,
    }


def _observations(plan: Any, term: GlossaryTermInput) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve exact quotes to codepoint offsets in the canonical projection.

    Quotes resolve against the canonical ``source_projection`` (Unicode
    codepoints, not bytes, not writable-packet rendering). A phrase spanning
    translatable inline formatting is valid only when the whole span is
    contiguously covered by translatable slots; the occurrence validator
    rejects spans crossing protected, template, or identifier gaps. This is
    deliberately narrow: no fuzzy spanning is attempted. Quote the exact
    visible surface, including spacing and punctuation; use occurrence_index
    when the quote repeats in the block.
    """
    blocks = {block.id: block for block in plan.blocks}
    stored, positions = [], []
    for request in term.occurrences:
        block = blocks.get(request.block_id)
        if block is None:
            raise BlockContractError("glossary-occurrence-owner")
        starts = []
        cursor = 0
        while (found := block.projection.find(request.quote, cursor)) >= 0:
            starts.append(found)
            cursor = found + len(request.quote)
            if len(starts) > 4096:
                raise BlockContractError("glossary-occurrence-limit")
        if not starts:
            raise BlockContractError("glossary-quote-not-found")
        if request.occurrence_index is None and len(starts) != 1:
            raise BlockContractError("glossary-quote-ambiguous")
        index = (request.occurrence_index or 1) - 1
        if index >= len(starts):
            raise BlockContractError("glossary-occurrence-range")
        start = starts[index]
        end = start + len(request.quote)
        anchor_id = request.anchor_id
        if anchor_id is None:
            enclosing = [a for a in block.anchors if a.start <= start and end <= a.end]
            if len(enclosing) == 1:
                anchor_id = enclosing[0].id
        stored.append({"block_id": block.id, "anchor_id": anchor_id,
                       "start_offset": start, "end_offset": end,
                       "surface_form": block.projection[start:end]})
        positions.append({"coordinate_system": "source_projection", "block_id": block.id,
                          "anchor_id": anchor_id, "start_offset": start, "end_offset": end})
    return stored, positions


def propose_glossary(project: Any, *, source_plan_id: str, entries: Sequence[Any],
                     idempotency_key: str, provenance: str = "passive-executor") -> dict[str, Any]:
    """Atomic batch append under the active-plan write fence; caller owns commit."""
    if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key) > 160 or not 1 <= len(entries) <= 32
            or not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 1000):
        raise BlockContractError("glossary-proposal-parameters")
    terms = [GlossaryTermInput.model_validate(entry) for entry in entries]
    record, _ = _load_glossary_source_plan(project)
    if record.id != source_plan_id:
        raise BlockContractError("source-plan-active-conflict")
    # Verify source/resolve quotes before obtaining the SQLite writer lock.
    # The subsequent active-plan fence prevents a source transition mid-edit.
    plan = rehydrate_source_plan(record) if any(term.occurrences for term in terms) else None
    resolved = [_observations(plan, term) if term.occurrences else ([], []) for term in terms]
    require_active_source_plan(project.id, source_plan_id, lock=True)
    saved: list[dict[str, Any]] = []
    request = {"source_plan_id": record.id,
               "entries": [item.model_dump() for item in terms], "provenance": provenance}
    fingerprint = private_witness(request, purpose="translator-working-glossary-request-v1")
    accepted_fingerprints = private_witness_candidates(
        request, purpose="translator-working-glossary-request-v1")
    # Whole-batch savepoint: a later term failure rolls back earlier flushed
    # terms in this batch without touching previously committed revisions.
    # The caller still owns the outer commit. The snapshot below is taken
    # after require_active_source_plan(lock=True) holds the active-plan
    # write/row fence, so concurrent writers serialize on that fence.
    link_map = _dossier_links_map(record)
    rejected_keys = _rejected_suggestion_keys(record)
    # CURRENT NFKC concept map: legacy casefold-only digests miss fullwidth /
    # canonical equivalents, so compare the verified current entries (owner +
    # source-plan scoped, exact rejected links hidden, human locks included)
    # on NFKC + casefold + whitespace lemma AND sense. Stored hashes/profiles
    # and historical receipts are unchanged.
    current_map = _current_lemma_map(record)

    def _blocked(revision: Any) -> bool:
        # Hidden exact rejected links do not block fresh proposals; visible
        # dossier terms and working proposals still enforce duplicates.
        return not _is_hidden_link(revision, record, link_map, rejected_keys)

    with db.session.begin_nested():
        seen_concepts: set[str] = set()
        for index, term in enumerate(terms):
            if term.concept_id is not None:
                if term.concept_id in seen_concepts:
                    raise BlockContractError("glossary-revision-conflict")
                seen_concepts.add(term.concept_id)
            # Receipt identity binds the whole batch, including its length. Exact
            # retries survive key rotation and never append another revision.
            revision_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"carlina:working-glossary:{project.id}:{record.id}:{idempotency_key}:{index}"))
            existing = db.session.get(TranslationGlossaryRevision, revision_id)
            if existing is not None:
                old = rehydrate_glossary_revision(existing, record)
                if (old.usage or {}).get("request_fingerprint") not in accepted_fingerprints:
                    raise BlockContractError("glossary-idempotency-conflict")
                saved.append(_entry(existing, record))
                _map_upsert(current_map, concept_id=existing.concept_id,
                            lemma=_normalize_match(old.source_term),
                            sense=_normalize_match(_sense(old)),
                            authority="human_locked" if db.session.get(
                                TranslationGlossaryActive, existing.concept_id) is not None else "model_advisory")
                continue
            previous = None
            if term.concept_id is not None:
                concept = db.session.get(TranslationGlossaryConcept, term.concept_id)
                if (concept is None or concept.project_id != project.id
                        or concept.source_plan_id != record.id or concept.owner_user_id != project.user_id):
                    raise BlockContractError("glossary-concept-owner")
                active = db.session.get(TranslationGlossaryActive, concept.id)
                if active is not None:
                    verify_glossary_active(active)
                    raise BlockContractError("glossary-human-locked")
                latest = TranslationGlossaryRevision.query.filter_by(concept_id=concept.id).order_by(
                    TranslationGlossaryRevision.revision_number.desc()).first()
                if latest is None or latest.revision_number != term.expected_revision:
                    raise BlockContractError("glossary-revision-conflict")
                previous = rehydrate_glossary_revision(latest, record)
            else:
                if term.expected_revision != 0:
                    raise BlockContractError("glossary-revision-conflict")
                for revision in find_glossary_revisions_for_term(record, term.source_term):
                    if not _blocked(revision):
                        continue
                    payload = rehydrate_glossary_revision(revision, record)
                    if _same_sense(_sense(payload), term.sense):
                        raise BlockContractError("glossary-concept-exists")
                concept = create_glossary_concept(record)
            # CURRENT NFKC duplicate fence (legacy digests miss equivalents).
            # Same lemma + same sense on another concept is blocked; different
            # senses stay allowed. Locked collisions report human-locked.
            _lemma = _normalize_match(term.source_term)
            _sense_key = _normalize_match(term.sense)
            _code = _map_collision_code(current_map, key=(_lemma, _sense_key),
                                        self_concept_id=term.concept_id)
            if _code is not None:
                raise BlockContractError(_code)
            # Do not accidentally merge concepts when editing a lemma/sense.
            for revision in find_glossary_revisions_for_term(record, term.source_term):
                if not _blocked(revision):
                    continue
                if revision.concept_id != concept.id and _same_sense(
                        _sense(rehydrate_glossary_revision(revision, record)), term.sense):
                    raise BlockContractError("glossary-concept-exists")
            forms = term.source_forms
            if forms is None:
                forms = list((previous.usage or {}).get("source_forms", [])) if previous is not None else []
            target_forms = term.accepted_target_forms
            if target_forms is None:
                target_forms = list((previous.usage or {}).get("accepted_target_forms", [])) if previous is not None else []
            observations, positions = resolved[index]
            forms = list(dict.fromkeys([term.source_term, *forms, *[item["surface_form"] for item in observations]]))
            target_forms = list(dict.fromkeys(form for form in target_forms if form != term.target_term))
            usage = GlossaryUsage(sense=term.sense, match_policy=term.match_policy,
                source_forms=forms, accepted_target_forms=target_forms,
                observations=observations, provenance=provenance,
                request_fingerprint=fingerprint)
            payload = GlossaryPayload(source_term=term.source_term, target_term=term.target_term,
                                      notes=term.notes, usage=usage.model_dump())
            try:
                revision = persist_glossary_revision(record, concept, payload=payload,
                    author_type="model", revision_state="proposal", revision_id=revision_id)
            except IntegrityError as exc:
                # Concurrent writer won the same deterministic revision id or
                # the same (concept, revision_number). Fail closed as a write
                # fence conflict; a retry with the same key reuses the winner
                # idempotently via the revision_id lookup above.
                raise BlockContractError("glossary-revision-conflict") from exc
            if positions:
                persist_glossary_occurrences(revision, positions, verified_plan=plan)
            saved.append(_entry(revision, record))
            _map_upsert(current_map, concept_id=concept.id,
                        lemma=_normalize_match(term.source_term),
                        sense=_normalize_match(term.sense))
    return {"contract": GLOSSARY_CONTRACT, "project_id": project.id,
            "source_plan_id": record.id, "items": saved,
            "authority": "model_advisory", "applies_to": "newly_prepared_windows"}


def snapshot_current_glossary_entries(record: Any) -> list[dict[str, Any]]:
    """One verified CURRENT entries snapshot for reuse across packing probes.

    Snapshot once per packing pass (refreshed per repack and per newly
    admitted window) so terminology reads stay O(windows) instead of
    O(probes). Entries are shared read-only; callers must not mutate them.
    """
    return _entries(record)


def build_glossary_slot(record: Any, plan: Any, window: Any, *,
                        entries: list[dict[str, Any]] | None = None) -> ContextSlot | None:
    """Bounded relevant terminology, already-inflected forms kept as evidence."""
    block_ids = set(window.block_ids)
    text = _normalize_match(" ".join(
        block.projection for block in plan.blocks if block.id in block_ids))
    selected = []
    for entry in (entries if entries is not None else _entries(record)):
        if entry["authority"] != "model_advisory":
            continue  # authoritative human locks belong to the frozen baseline
        if any(_contains_form(text, form) for form in entry["source_forms"]):
            selected.append({key: entry[key] for key in (
                "concept_id", "revision_id", "source_term", "target_term", "sense",
                "match_policy", "source_forms", "notes", "authority")})
    if not selected:
        return None
    total = len(selected)
    selected = selected[:MAX_WINDOW_TERMS]
    while selected:
        content = canonical_json({"contract_version": GLOSSARY_CONTRACT, "terms": selected,
            "omitted_terms": total - len(selected),
            "guidance": "Use terms contextually; preferred mappings may inflect. Exact mappings request fixed spelling. Different senses are not interchangeable. Human-locked constraints win. This is a frozen snapshot of live model proposals, not new research."})
        if len(content) <= MAX_WINDOW_CHARS:
            break
        selected.pop()
    if not selected:
        raise BlockContractError("glossary-window-budget")
    return ContextSlot("terminology_guidance", content,
        {"resolver": GLOSSARY_RESOLVER, "source_plan_id": record.id},
        f"working-glossary:{record.id}:{window.order}", "1", sha256_text(content), {}, required=True)


def window_block_index_for_dispatch(record: Any, plan: Any, window: Any) -> dict[str, Any]:
    """Read-only n to canonical block/sentence identity for passive packets.

    Writable source blocks use packet-local ``n`` (1-based, window order).
    Glossary occurrence evidence requires canonical ``block_id`` (plus optional
    sentence ``anchor_id``). This mapping lets a passive executor build exact
    quotes without extra outline fetches. It is dispatch metadata only: window
    packet messages, slots and response schema are unchanged, so frozen and
    replayed attempts are unaffected. Executors must ignore unknown contracts.
    """
    by_id = {block.id: block for block in plan.blocks}
    blocks: list[dict[str, Any]] = []
    for position, block_id in enumerate(tuple(window.block_ids), 1):
        block = by_id.get(block_id)
        if block is None:
            raise BlockContractError("glossary-occurrence-owner")
        sentences = [
            {"n": anchor.order, "anchor_id": anchor.id}
            for anchor in sorted(block.anchors, key=lambda item: item.order)
        ]
        blocks.append({"n": position, "block_id": block.id, "sentences": sentences})
    return {
        "contract_version": WINDOW_BLOCK_INDEX_CONTRACT,
        "source_plan_id": record.id,
        "window_order": window.order,
        "blocks": blocks,
    }


def glossary_slot_for_window(operation: Any, record: Any, plan: Any, window: Any,
                             *, verified_evidence: Any = None) -> ContextSlot | None:
    """Reuse the stored glossary snapshot whenever a packet already exists."""
    attempts = TranslationWindowAttempt.query.filter_by(operation_id=operation.id,
        source_plan_id=record.id, window_order=window.order).all()
    matching = [a for a in attempts if tuple(a.block_ids_json) == tuple(window.block_ids)]
    if matching:
        from .window_persistence import rehydrate_window_attempt
        # All retry descendants use the original lexical snapshot. Do not pick
        # a convenient timestamp or mutate a previously leased request.
        snapshots = []
        for attempt in matching:
            packet = rehydrate_window_attempt(attempt, record, verified_plan=plan,
                                               verified_evidence=verified_evidence)
            slots = [s for s in packet.slots if s.provenance.get("resolver") == GLOSSARY_RESOLVER]
            if len(slots) > 1:
                raise BlockContractError("glossary-window-snapshot")
            snapshots.append(slots[0] if slots else None)
        fingerprints = {slot.artifact_hash if slot else None for slot in snapshots}
        if len(fingerprints) != 1:
            raise BlockContractError("glossary-window-snapshot")
        return snapshots[0]
    return build_glossary_slot(record, plan, window)
