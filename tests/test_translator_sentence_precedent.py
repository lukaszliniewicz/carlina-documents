"""Focused tests for bounded sentence-precedent evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import update

from tests.test_translator_block_translation import (
    _candidate,
    _seed_target_for_slot,
    _state,
    _window,
)
from iarf_assistant.addons.translator import alignment_persistence as _alignment_persistence
from iarf_assistant.addons.translator import target_persistence as _target_persistence
from iarf_assistant.addons.translator.models import TranslationTargetBlockActive
from iarf_assistant.addons.translator.target_contract import structural_fingerprint
from iarf_assistant.extensions import db
from iarf_assistant.addons.translator.sentence_precedent import (
    SENTENCE_PRECEDENT_CONTRACT_VERSION,
    dice_similarity,
    normalize_sentence_match_text,
    sentence_precedent_slot,
)
from iarf_assistant.addons.translator.window_contract import TranslationWindow
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'sentence-precedent.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="sentence-precedent-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


def test_exact_sentence_repeated_inside_different_paragraphs(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>The same sentence appears in different paragraphs today.</p>"
        "<p>There is unrelated intervening context here.</p>"
        "<p>The same sentence appears in different paragraphs today.</p>"
        "</body></html>"
    )
    first, _middle, last = plan.blocks
    _seed_target_for_slot(
        _project,
        plan,
        record,
        first,
        "To samo zdanie pojawia się dziś w różnych akapitach.",
    )

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=2))

    assert slot is not None
    assert slot.required is False
    assert slot.trust_class == "untrusted_reference"
    payload = json.loads(slot.content)
    assert payload["contract_version"] == SENTENCE_PRECEDENT_CONTRACT_VERSION
    assert payload["queries"][0]["n"] == 1
    assert payload["queries"][0]["source_sentence_order"] == 1
    match = payload["queries"][0]["matches"][0]
    assert match["match_kind"] == "exact"
    assert match["similarity"] == 1.0
    assert match["source_order"] == first.order
    assert match["source_sentence_order"] == 1
    assert match["target_text"] == "To samo zdanie pojawia się dziś w różnych akapitach."
    assert slot.dependency["source_plan_id"] == record.id
    assert len(slot.dependency["active_targets"]) == 1
    assert slot.dependency["active_targets"][0]["block_id"] == first.id
    assert last.id not in {
        item["block_id"] for item in slot.dependency["active_targets"]
    }


def test_query_identity_keeps_multiple_sentences_in_one_block_separate(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>The first sentence has enough words for matching. The second sentence has enough words too.</p>"
        "<p>Unrelated intervening context has enough words for matching.</p>"
        "<p>The first sentence has enough words for matching. The second sentence has enough words too.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(_project, plan, record, source, "Pierwsze zdanie. Drugie zdanie.")

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=2))

    assert slot is not None
    rows = json.loads(slot.content)["queries"]
    assert [(row["n"], row["source_sentence_order"]) for row in rows] == [(1, 1), (1, 2)]


def test_matching_normalization_and_unique_trigram_dice_are_bounded():
    assert normalize_sentence_match_text("  Héllo, WORLD! 42 ") == "héllo world 42"
    assert dice_similarity("a long repeated sentence for matching", "a long repeated sentence for matching") == 1.0
    assert dice_similarity("xy", "xy") == 0.0


def test_short_marker_and_short_sentence_are_rejected(app):
    _project, plan, record, _operation = _state(
        "<html><body><p>*</p><p>Too short.</p><p>Long enough source sentence for matching.</p></body></html>"
    )
    source = plan.blocks[2]
    _seed_target_for_slot(_project, plan, record, source, "Wystarczająco długie tłumaczenie.")

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=0, end=2))

    assert slot is None


def test_approximate_match_is_labeled_and_negation_stays_advisory(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>The ship crosses the northern sea before dawn.</p>"
        "<p>The ship crosses the northern sea before dark.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(_project, plan, record, source, "Statek przepływa morze przed świtem.")

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1))

    assert slot is not None
    match = json.loads(slot.content)["queries"][0]["matches"][0]
    assert match["match_kind"] == "approximate"
    assert 0.85 <= match["similarity"] < 1
    assert "negation" in json.loads(slot.content)["guidance"]
    assert slot.required is False


def test_punctuation_difference_is_never_an_exact_match(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>The re-sign procedure is clearly documented for reference.</p>"
        "<p>The resign procedure is clearly documented for reference.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(_project, plan, record, source, "Procedura jest opisana.")

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1))

    assert slot is not None
    match = json.loads(slot.content)["queries"][0]["matches"][0]
    assert match["match_kind"] == "approximate"
    assert match["similarity"] == 1.0


def test_linked_note_suffix_is_accepted_for_sentence_queries(app):
    _project, plan, record, _operation = _state(
        '<html><body>'
        "<p>A repeated sentence has enough words to match reliably.</p>"
        '<p>A repeated sentence has enough words to match reliably. '
        '<a role="doc-noteref" href="#note">1</a></p>'
        "<p>Unrelated context has enough words for matching.</p>"
        '<aside id="note" role="doc-footnote">'
        "<p>The note body has enough words to be visible here.</p>"
        "</aside></body></html>"
    )
    source, body, _other, note = plan.blocks
    _seed_target_for_slot(
        _project,
        plan,
        record,
        source,
        "Powtórzone zdanie ma wiarygodne tłumaczenie.",
    )
    window = TranslationWindow(
        2,
        (body.id, note.id),
        (structural_fingerprint(plan, body), structural_fingerprint(plan, note)),
    )

    slot = sentence_precedent_slot(record, plan=plan, window=window)

    assert slot is not None
    match = json.loads(slot.content)["queries"][0]["matches"][0]
    assert match["match_kind"] == "exact"
    assert match["source_order"] == source.order


def test_random_nonadjacent_window_is_rejected(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>First source sentence has enough words for matching.</p>"
        "<p>Middle source sentence has enough words for matching.</p>"
        "<p>Last source sentence has enough words for matching.</p>"
        "</body></html>"
    )
    first, _middle, last = plan.blocks
    window = TranslationWindow(
        1,
        (first.id, last.id),
        (structural_fingerprint(plan, first), structural_fingerprint(plan, last)),
    )

    with pytest.raises(ValueError, match="sentence-precedent-window-adjacency"):
        sentence_precedent_slot(record, plan=plan, window=window)


def test_active_target_beyond_untranslated_repeats_survives_verification_cap(app):
    sentence = "A very long repeated sentence appears in every paragraph for matching."
    html = "<html><body>" + "".join(f"<p>{sentence}</p>" for _ in range(67)) + "</body></html>"
    _project, plan, record, _operation = _state(html)
    target_block = plan.blocks[-1]
    _seed_target_for_slot(
        _project,
        plan,
        record,
        target_block,
        "To jest aktualne tłumaczenie odległego powtórzenia.",
    )

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, end=1))

    assert slot is not None
    match = json.loads(slot.content)["queries"][0]["matches"][0]
    assert match["source_order"] == target_block.order


def test_one_to_many_alignment_joins_verified_target_spans(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>The source sentence is repeated with enough words for matching.</p>"
        "<p>The source sentence is repeated with enough words for matching.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(
        _project,
        plan,
        record,
        source,
        "Pierwsza część tłumaczenia. Druga część tłumaczenia.",
    )

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1))

    assert slot is not None
    assert (
        json.loads(slot.content)["queries"][0]["matches"][0]["target_text"]
        == "Pierwsza część tłumaczenia. Druga część tłumaczenia."
    )


def test_many_source_alignment_group_is_skipped(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>The first source sentence has enough words to match. The second source sentence also has enough words.</p>"
        "<p>The first source sentence has enough words to match.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(_project, plan, record, source, "Jedno tłumaczenie całego akapitu.")

    assert sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1)) is None


def test_same_window_occurrences_are_excluded_and_missing_target_returns_none(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>A sufficiently long sentence repeats in this window.</p>"
        "<p>A sufficiently long sentence repeats in this window.</p>"
        "</body></html>"
    )
    first, second = plan.blocks
    _seed_target_for_slot(_project, plan, record, first, "Pierwsze tłumaczenie zdania.")

    assert sentence_precedent_slot(record, plan=plan, window=_window(plan)) is None
    assert sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1)) is not None


def test_current_active_target_is_reloaded_after_manual_version_update(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>A sufficiently long sentence repeats in this document.</p>"
        "<p>A sufficiently long sentence repeats in this document.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(_project, plan, record, source, "Stara wersja tłumaczenia zdania.")
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": source.id},
    )
    assert pointer is not None
    previous = db.session.get(_target_persistence.TranslationTargetBlockRevision, pointer.revision_id)
    assert previous is not None
    candidate = _candidate(plan, record, source, "Nowa aktualna wersja tłumaczenia zdania.")
    revision = _target_persistence.persist_target_revision(
        record,
        candidate,
        target_language=_project.target_lang,
        author_type="human",
        author_user_id=_project.user_id,
        parent_revision_id=previous.id,
        verified_plan=plan,
    )
    span_set = _alignment_persistence.persist_target_span_set(record, revision, verified_plan=plan)
    alignment = _alignment_persistence.persist_alignment_revision(record, span_set, verified_plan=plan)
    _target_persistence.activate_target_revision(
        record,
        block_id=source.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=pointer.version,
        verified_plan=plan,
    )
    db.session.commit()

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1))

    assert slot is not None
    payload = json.loads(slot.content)
    assert payload["queries"][0]["matches"][0]["target_text"] == "Nowa aktualna wersja tłumaczenia zdania."
    assert slot.dependency["active_targets"][0]["revision_id"] == revision.id


def test_corrupt_active_pair_is_rejected_before_emitting_evidence(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        "<p>A sufficiently long sentence repeats in this document.</p>"
        "<p>A sufficiently long sentence repeats in this document.</p>"
        "</body></html>"
    )
    source = plan.blocks[0]
    _seed_target_for_slot(_project, plan, record, source, "Aktualne tłumaczenie zdania.")
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": source.id},
    )
    assert pointer is not None
    db.session.execute(
        update(TranslationTargetBlockActive)
        .where(
            TranslationTargetBlockActive.source_plan_id == record.id,
            TranslationTargetBlockActive.block_id == source.id,
        )
        .values(version=pointer.version + 1),
        execution_options={"synchronize_session": False},
    )
    with pytest.raises(ValueError, match="sentence-precedent-active-pair|target-active-pair"):
        sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1))
    db.session.rollback()


def test_output_is_bounded_to_six_matches_and_eight_thousand_bytes(app):
    sentence = "A sufficiently long sentence repeats in this document."
    html = "<html><body>" + "".join(f"<p>{sentence}</p>" for _ in range(8)) + "</body></html>"
    _project, plan, record, _operation = _state(html)
    for block in plan.blocks[:7]:
        _seed_target_for_slot(_project, plan, record, block, "Bardzo długie tłumaczenie tego powtarzanego zdania.")

    slot = sentence_precedent_slot(record, plan=plan, window=_window(plan, start=7))

    assert slot is not None
    payload = json.loads(slot.content)
    assert len(slot.content.encode("utf-8")) <= 8_000
    assert sum(len(row["matches"]) for row in payload["queries"]) <= 6
    assert all(len(row["matches"]) <= 2 for row in payload["queries"])
    assert len(
        {
            match["source_order"]
            for row in payload["queries"]
            for match in row["matches"]
        }
    ) >= 2


def test_plan_owner_or_hash_mismatch_fails_closed(app):
    _project, plan, record, _operation = _state(
        "<html><body><p>A sufficiently long sentence repeats in this document.</p>"
        "<p>A sufficiently long sentence repeats in this document.</p></body></html>"
    )
    with pytest.raises(ValueError, match="sentence-precedent-source-plan"):
        original = record.plan_hash
        record.plan_hash = "f" * 64
        try:
            sentence_precedent_slot(record, plan=plan, window=_window(plan, start=1))
        finally:
            record.plan_hash = original
