"""Pure v7 provider-manifest sentence alignment checks."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_translator_block_translation",)

from iarf_assistant.addons.translator.alignment_contract import (
    AlignmentGroup,
    TargetSpan,
)
from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.sentence_alignment import (
    SentenceAlignmentGroup,
    apply_sentence_edit,
    normalize_sentence_alignment,
    rebase_sentence_alignment,
)
from iarf_assistant.addons.translator.target_contract import (
    TargetBlockCandidate,
    TargetTextPart,
)


@pytest.mark.parametrize("segmented", [False, True])
def test_v7_packet_is_local_and_profile_controls_note_cap(app, segmented):
    from test_translator_block_translation import (
        _config,
        _profile,
        _state,
        _window,
        _workflow_config,
    )

    from iarf_assistant.addons.translator.block_translation import build_window_material
    from iarf_assistant.addons.translator.window_contract import (
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    )

    _project, plan, record, _operation = _state(
        "<html><body><p>One. Two.</p></body></html>",
        sentence_segmenter=(lambda _projection: ["One.", "Two."])
        if segmented
        else None,
    )
    config = _config(plan, record, _profile())
    config["writable_source_contract_version"] = (
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
    )
    config["annotation_profile"] = "study"
    material = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=_window(plan),
        frozen_config=config,
        workflow_config=_workflow_config(),
        execution_profile=_profile(),
        source_plan_record=record,
        include_continuity=False,
        token_counter=lambda **_: 10,
    )
    writable = next(
        slot for slot in material.packet.slots if slot.slot_type == "writable_source"
    )
    payload = __import__("json").loads(writable.content)
    assert set(payload) == {"contract_version", "annotation_profile", "blocks"}
    assert payload["annotation_profile"] == "study"
    assert set(payload["blocks"][0]) == {"n", "role", "content", "fields", "sentences"}
    # Replay must use the exact fixed anchors, not assume where the installed
    # fallback splitter assigns inter-sentence whitespace.
    source_sentences = [item["text"] for item in payload["blocks"][0]["sentences"]]
    assert source_sentences == [
        plan.blocks[0].projection[anchor.start : anchor.end]
        for anchor in plan.blocks[0].anchors
    ]
    assert [sentence.strip() for sentence in source_sentences] == ["One.", "Two."]
    if segmented:
        assert all(
            anchor.splitter_profile == "sentence-segmenter"
            for anchor in plan.blocks[0].anchors
        )
    assert "id" not in __import__("json").dumps(payload)
    result = material.packet.response_format["json_schema"]["schema"]["properties"][
        "results"
    ]["items"]
    assert result["properties"]["notes"]["maxItems"] == 6
    assert set(result["properties"]["alignment"]["properties"]) == {
        "target_sentences",
        "groups",
    }


@pytest.mark.parametrize("segmented", [False, True])
def test_v7_response_normalizes_candidate_before_manifest(app, segmented):
    from test_translator_block_translation import (
        _config,
        _profile,
        _state,
        _window,
        _workflow_config,
    )

    from iarf_assistant.addons.translator.block_translation import build_window_material
    from iarf_assistant.addons.translator.window_contract import (
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    )
    from iarf_assistant.addons.translator.window_execution import (
        normalize_window_response,
    )

    _project, plan, record, _operation = _state(
        "<html><body><p>One. Two.</p></body></html>",
        sentence_segmenter=(lambda _projection: ["One.", "Two."])
        if segmented
        else None,
    )
    config = _config(plan, record, _profile())
    config["writable_source_contract_version"] = (
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
    )
    config["annotation_profile"] = "none"
    material = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=_window(plan),
        frozen_config=config,
        workflow_config=_workflow_config(),
        execution_profile=_profile(),
        source_plan_record=record,
        include_continuity=False,
        token_counter=lambda **_: 10,
    )
    source = __import__("json").loads(
        next(
            slot.content
            for slot in material.packet.slots
            if slot.slot_type == "writable_source"
        )
    )
    row = source["blocks"][0]
    response = {
        "results": [
            {
                "n": 1,
                "content": "Jeden. Dwa.",
                "fields": row["fields"],
                "notes": [],
                "alignment": {
                    "target_sentences": ["Jeden.", "Dwa."],
                    "groups": [
                        {"source": [1], "target": [1]},
                        {"source": [2], "target": [2]},
                    ],
                },
            }
        ]
    }
    candidates, notes, manifests = normalize_window_response(
        response,
        plan=plan,
        plan_id=record.id,
        window=_window(plan),
        writable_source_contract_version=SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        annotation_profile="none",
        target_language="pl",
    )
    assert len(candidates) == 1 and not notes
    assert [(span.start, span.end) for span in manifests[0].target_spans] == [
        (0, 7),
        (7, 11),
    ]


@pytest.mark.parametrize("single_copy", [False, True])
def test_v7_attempt_recovers_provider_groups_and_binds_staged_candidates(
    app, monkeypatch, single_copy
):
    from test_translator_block_translation import (
        _config,
        _profile,
        _state,
        _window,
        _workflow_config,
    )

    from iarf_assistant.addons.translator import block_window_runtime
    from iarf_assistant.addons.translator.block_translation import build_window_material
    from iarf_assistant.addons.translator.block_workspace_api import (
        build_workspace_payload,
    )
    from iarf_assistant.addons.translator.models import (
        TranslationAlignmentRevision,
        TranslationTargetSpanSet,
        TranslationWindowAttempt,
    )
    from iarf_assistant.addons.translator.target_contract import TargetTextPart
    from iarf_assistant.addons.translator.window_contract import (
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
    )
    from iarf_assistant.addons.translator.window_execution import (
        execute_window_attempt,
    )
    from iarf_assistant.addons.translator.window_persistence import (
        persist_window_attempt,
    )
    from iarf_assistant.extensions import db

    version = SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION if single_copy else SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
    project, plan, record, operation = _state()
    project.settings = {
        **(project.settings or {}),
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    profile = _profile()
    config = _config(plan, record, profile)
    config["writable_source_contract_version"] = (
        version
    )
    config["annotation_profile"] = "none"
    window = _window(plan)
    material = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=window,
        frozen_config=config,
        workflow_config=_workflow_config(),
        execution_profile=profile,
        source_plan_record=record,
        include_continuity=False,
        token_counter=lambda **_: 10,
    )
    response = {
        "results": [
            {
                "n": 1,
                "content": "Pierwsze. Dalej.",
                "fields": [],
                "notes": [],
                "alignment": {
                    "target_sentences": ["Pierwsze.", "Dalej."],
                    "groups": [{"source": [1], "target": [1, 2]}],
                },
            },
            {
                "n": 2,
                "content": "Drugie.",
                "fields": [],
                "notes": [],
                "alignment": {
                    "target_sentences": ["Drugie."],
                    "groups": [{"source": [1], "target": [1]}],
                },
            },
        ]
    }
    if single_copy:
        for row in response["results"]:
            row["content"] = "<sb/> ".join(row["alignment"].pop("target_sentences")) + "<sb/>"
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=material.packet,
        settings=material.settings,
        model_profile=material.model_profile,
        admission=material.admission,
    )
    db.session.commit()
    execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: response,
    )
    attempt_id = attempt.id
    db.session.expire_all()
    staged = db.session.get(TranslationWindowAttempt, attempt_id)
    db.session.rollback()
    assert staged is not None

    normalized = block_window_runtime.normalize_window_response(
        response,
        plan=plan,
        plan_id=record.id,
        window=window,
        writable_source_contract_version=(
            version
        ),
        annotation_profile="none",
        target_language="pl",
    )
    candidates, notes, manifests = normalized
    drifted = replace(
        candidates[0], parts=(TargetTextPart(kind="text", text="Podmienione."),)
    )
    db.session.rollback()
    monkeypatch.setattr(
        block_window_runtime,
        "normalize_window_response",
        lambda *_args, **_kwargs: ((drifted, *candidates[1:]), notes, manifests),
    )
    with pytest.raises(BlockContractError, match="window-finalize-v7-candidate"):
        block_window_runtime.finalize_staged_window_attempt(
            staged,
            operation=operation,
            source_plan=record,
            window=window,
        )

    monkeypatch.undo()
    receipt = block_window_runtime.finalize_staged_window_attempt(
        staged,
        operation=operation,
        source_plan=record,
        window=window,
    )
    db.session.commit()
    alignments = [
        db.session.get(TranslationAlignmentRevision, item)
        for item in receipt.alignment_revision_ids
    ]
    assert all(item is not None for item in alignments)
    assert [item.method for item in alignments] == [
        "provider-manifest-v1",
        "provider-manifest-v1",
    ]
    assert [len(item.groups[0].source_anchor_ids) for item in alignments] == [1, 1]
    assert [len(item.groups[0].target_span_ids) for item in alignments] == [2, 1]
    span_sets = [
        db.session.get(TranslationTargetSpanSet, item.span_set_id)
        for item in alignments
    ]
    assert [item.profile_id for item in span_sets] == [
        "provider-manifest-v1",
        "provider-manifest-v1",
    ]
    workspace = build_workspace_payload(project, offset=0, limit=2)
    projected = workspace["blocks"][0]["target"]["alignment"]
    assert workspace["contract"] == "translator-block-workspace-v6"
    assert projected["origin"] == "model_reported"
    assert projected["groups"][0]["kind"] == "split"
    assert len(projected["groups"][0]["target_sentences"]) == 2

    assert "<sb" not in __import__("json").dumps(workspace["blocks"][0]["target"])


def test_provider_manifest_span_set_preserves_reported_boundaries(app):
    from test_translator_block_translation import _candidate, _state

    from iarf_assistant.addons.translator.alignment_persistence import (
        persist_target_span_set,
        rehydrate_target_span_set,
    )
    from iarf_assistant.addons.translator.sentence_alignment import (
        SentenceAlignmentGroup,
        SentenceAlignmentManifest,
        TargetSentenceSpan,
    )
    from iarf_assistant.addons.translator.target_persistence import (
        persist_target_revision,
    )

    _project, plan, record, _operation = _state(
        "<html><body><p>One. Two.</p></body></html>"
    )
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, "Jeden.  Dwa.")
    revision = persist_target_revision(
        record,
        candidate,
        target_language="pl",
        author_type="model",
    )
    manifest = SentenceAlignmentManifest(
        block_id=block.id,
        target_sentences=("Jeden.", "Dwa."),
        target_spans=(
            TargetSentenceSpan(1, 0, 8, "Jeden.  "),
            TargetSentenceSpan(2, 8, 12, "Dwa."),
        ),
        groups=(
            SentenceAlignmentGroup((1,), (1,)),
            SentenceAlignmentGroup((2,), (2,)),
        ),
    )
    invalid = replace(
        manifest,
        target_spans=(
            replace(manifest.target_spans[0], text="Jeden."),
            manifest.target_spans[1],
        ),
    )
    with pytest.raises(BlockContractError, match="alignment-provider-manifest-spans"):
        persist_target_span_set(record, revision, manifest=invalid)
    span_set = persist_target_span_set(record, revision, manifest=manifest)
    assert span_set.profile_id == "provider-manifest-v1"
    assert [(row.start_offset, row.end_offset) for row in span_set.spans] == [
        (0, 8),
        (8, 12),
    ]
    assert len(rehydrate_target_span_set(span_set, record)) == 2


def _case(source: str, target: str):
    cursor = 0
    anchors = []
    for order, text in enumerate(source.split("|"), 1):
        end = cursor + len(text)
        anchors.append(SimpleNamespace(order=order, start=cursor, end=end))
        cursor = end
    block = SimpleNamespace(
        id="block-1",
        projection=source.replace("|", ""),
        anchors=tuple(anchors),
        slots=(
            SimpleNamespace(
                id="text",
                kind="text",
                disposition="translatable",
                source_value=source.replace("|", ""),
            ),
        ),
        wire=(SimpleNamespace(kind="slot", ref_id="text"),),
    )
    candidate = TargetBlockCandidate(
        source_plan_id="plan-1",
        source_plan_hash="a" * 64,
        block_id="block-1",
        structural_fingerprint="b" * 64,
        parts=(TargetTextPart(kind="text", text=target),),
        lexical_attributes=(),
    )
    return block, candidate


@pytest.mark.parametrize(
    ("source", "target_sentences", "groups"),
    (
        (
            "One.|Two.",
            ["Jeden.", "Dwa."],
            [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
        ),
        ("One.", ["Jeden.", "Dalej."], [{"source": [1], "target": [1, 2]}]),
        ("One.|Two.", ["Raz."], [{"source": [1, 2], "target": [1]}]),
        (
            "One.|Two.",
            ["Raz.", "Dalej."],
            [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
        ),
    ),
)
def test_manifest_normalizes_monotonic_partitions(source, target_sentences, groups):
    block, candidate = _case(source, "".join(target_sentences))
    manifest = normalize_sentence_alignment(
        block=block,
        candidate=candidate,
        value={"target_sentences": target_sentences, "groups": groups},
        target_language="pl",
    )
    assert [span.text for span in manifest.target_spans] == target_sentences
    assert manifest.groups[0].source_ordinals == tuple(groups[0]["source"])


def test_manifest_assigns_boundary_whitespace_deterministically():
    block, candidate = _case("One.", "  Jeden.  ")
    manifest = normalize_sentence_alignment(
        block=block,
        candidate=candidate,
        value={
            "target_sentences": ["Jeden."],
            "groups": [{"source": [1], "target": [1]}],
        },
        target_language="pl",
    )
    assert manifest.target_spans[0].start == 0
    assert manifest.target_spans[0].end == len("  Jeden.  ")


@pytest.mark.parametrize(
    "groups",
    (
        [{"source": [1], "target": [2]}, {"source": [2], "target": [1]}],
        [{"source": [1, 3], "target": [1]}],
        [{"source": [1], "target": [1]}, {"source": [1], "target": [2]}],
        [{"source": [1], "target": [3]}],
    ),
)
def test_manifest_rejects_crossing_duplicate_and_out_of_range_groups(groups):
    block, candidate = _case("One.|Two.", "Raz.Dwa.")
    with pytest.raises(BlockContractError):
        normalize_sentence_alignment(
            block=block,
            candidate=candidate,
            value={
                "target_sentences": ["Raz.", "Dwa."],
                "groups": groups,
            },
            target_language="pl",
        )


def test_manifest_rejects_target_material_mismatch():
    block, candidate = _case("One.", "Jeden.")
    with pytest.raises(BlockContractError, match="sentence-alignment-target"):
        normalize_sentence_alignment(
            block=block,
            candidate=candidate,
            value={
                "target_sentences": ["Inny."],
                "groups": [{"source": [1], "target": [1]}],
            },
            target_language="pl",
        )


def test_manifest_preserves_explicit_units_without_punctuation_veto():
    block, candidate = _case("One.", "Jeden. Dwa.")
    manifest = normalize_sentence_alignment(
        block=block, candidate=candidate,
        value={"target_sentences": ["Jeden. Dwa."],
               "groups": [{"source": [1], "target": [1]}]},
        target_language="pl",
    )
    assert manifest.target_spans[0].text == "Jeden. Dwa."


@pytest.mark.parametrize(
    ("target", "target_language"),
    [
        (
            "The angels ask, “What has God wrought?”—what new teaching there is on high.",
            "en",
        ),
        ("“Stop!” said Mary.", "en"),
        ("“Really?”, he asked.", "en"),
    ],
)
def test_manifest_accepts_clear_quoted_continuations(target, target_language):
    block, candidate = _case("One.", target)
    manifest = normalize_sentence_alignment(
        block=block,
        candidate=candidate,
        value={
            "target_sentences": [target],
            "groups": [{"source": [1], "target": [1]}],
        },
        target_language=target_language,
    )
    assert len(manifest.target_spans) == 1
    assert manifest.target_spans[0].text == target


@pytest.mark.parametrize("target", [
    "“Done.” Next.", "Tel.: 123 456", "www.example.test", "mail@example.test",
    "1. [Editor, 1904]", "A. N. Example", "Value 3.14, section 2.1.",
    "A work made by human hands! - for public viewing.", "Final words! -",
    "Dr. Nowak, ul. Długa 4.", "Это пример, т. е. пояснение.",
])
def test_manifest_uses_supplied_boundaries_for_language_and_fragment_units(target):
    block, candidate = _case("One.", target)
    manifest = normalize_sentence_alignment(
        block=block, candidate=candidate,
        value={"target_sentences": [target],
               "groups": [{"source": [1], "target": [1]}]},
        target_language="en",
    )
    assert manifest.target_spans[0].text == target


def _rebase_case(target: str, *, anchor_count: int = 1):
    from iarf_assistant.addons.translator.block_contract import sha256_text

    block = SimpleNamespace(
        id="block-1",
        anchors=tuple(
            SimpleNamespace(id=f"anchor-{index}", order=index)
            for index in range(1, anchor_count + 1)
        ),
        slots=(
            SimpleNamespace(
                id="text",
                kind="text",
                disposition="translatable",
                source_value="",
            ),
        ),
        wire=(SimpleNamespace(kind="slot", ref_id="text"),),
    )
    candidate = TargetBlockCandidate(
        source_plan_id="plan-1",
        source_plan_hash="a" * 64,
        block_id="block-1",
        structural_fingerprint="b" * 64,
        parts=(TargetTextPart(kind="text", text=target),),
        lexical_attributes=(),
    )

    def make_span(order: int, start: int, end: int, text: str) -> TargetSpan:
        return TargetSpan(
            id=f"span-{order}",
            order=order,
            start=start,
            end=end,
            text_hash=sha256_text(text),
        )

    return block, candidate, make_span


def test_rebase_preserves_one_to_two_groups_through_word_edits():
    block, candidate, make_span = _rebase_case("Pierwszy.  Drugi.")
    previous = "Jeden.  Dwa."
    previous_spans = (
        make_span(1, 0, 8, previous[:8]),
        make_span(2, 8, len(previous), previous[8:]),
    )
    result = rebase_sentence_alignment(
        block,
        candidate,
        previous_projection=previous,
        previous_spans=previous_spans,
        previous_groups=(
            AlignmentGroup(
                ("anchor-1",),
                ("span-1", "span-2"),
                "saved",
                0.9,
                "approved",
                {},
            ),
        ),
    )
    assert result is not None
    assert result.target_sentences == ("Pierwszy.", "Drugi.")
    assert result.target_spans[0].text == "Pierwszy.  "
    assert len(result.groups) == 1
    assert result.groups[0].source_ordinals == (1,)
    assert result.groups[0].target_ordinals == (1, 2)


def test_rebase_bounds_fragmented_unique_character_edits(monkeypatch):
    from difflib import SequenceMatcher

    previous = "".join(chr(0x10000 + index) for index in range(32_768))
    updated = "".join(
        chr(0x20000 + index) if index % 8 == 7 else char
        for index, char in enumerate(previous)
    )
    block, candidate, make_span = _rebase_case(updated)
    spans = tuple(
        make_span(
            index + 1, index * 8, (index + 1) * 8, previous[index * 8 : (index + 1) * 8]
        )
        for index in range(4096)
    )
    calls = 0
    original = SequenceMatcher.find_longest_match

    def counted_match(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        assert calls < 100, "Fragmented input exceeded the bounded search allowance"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SequenceMatcher, "find_longest_match", counted_match)
    assert (
        rebase_sentence_alignment(
            block,
            candidate,
            previous_projection=previous,
            previous_spans=spans,
            previous_groups=(
                AlignmentGroup(
                    ("anchor-1",),
                    tuple(span.id for span in spans),
                    "saved",
                    0.5,
                    "needs_review",
                    {},
                ),
            ),
        )
        is None
    )
    assert calls > 0


def test_rebase_identity_returns_the_saved_partition():
    block, candidate, make_span = _rebase_case("Original.")
    result = rebase_sentence_alignment(
        block,
        candidate,
        previous_projection="Original.",
        previous_spans=(make_span(1, 0, 9, "Original."),),
        previous_groups=(
            AlignmentGroup(
                ("anchor-1",),
                ("span-1",),
                "saved",
                0.9,
                "approved",
                {},
            ),
        ),
    )
    assert result is not None
    assert result.target_sentences == ("Original.",)
    assert result.target_spans[0].text == "Original."
    assert result.groups == (SentenceAlignmentGroup((1,), (1,)),)


def test_rebase_preserves_many_to_one_groups_and_outer_boundary_edits():
    block, candidate, make_span = _rebase_case(
        "Intro First changed suffix.", anchor_count=2
    )
    previous = "First sentence."
    previous_spans = (make_span(1, 0, len(previous), previous),)
    result = rebase_sentence_alignment(
        block,
        candidate,
        previous_projection=previous,
        previous_spans=previous_spans,
        previous_groups=(
            AlignmentGroup(
                ("anchor-1", "anchor-2"),
                ("span-1",),
                "saved",
                0.9,
                "approved",
                {},
            ),
        ),
    )
    assert result is not None
    assert result.target_sentences == ("Intro First changed suffix.",)
    assert result.groups[0].source_ordinals == (1, 2)


def test_rebase_keeps_saved_split_when_new_wording_has_different_model_boundaries():
    block, candidate, make_span = _rebase_case("A longer first clause; second.")
    previous = "First clause; second."
    result = rebase_sentence_alignment(
        block,
        candidate,
        previous_projection=previous,
        previous_spans=(
            make_span(1, 0, 14, previous[:14]),
            make_span(2, 14, len(previous), previous[14:]),
        ),
        previous_groups=(
            AlignmentGroup(
                ("anchor-1",),
                ("span-1", "span-2"),
                "saved",
                0.9,
                "approved",
                {},
            ),
        ),
    )
    assert result is not None
    assert result.target_sentences == ("A longer first clause;", "second.")
    assert result.target_spans[0].text == "A longer first clause; "


@pytest.mark.parametrize("updated", ["Original!", "Original.\nFurther."])
def test_rebase_rejects_sentence_delimiter_changes(updated):
    block, candidate, make_span = _rebase_case(updated)
    assert (
        rebase_sentence_alignment(
            block,
            candidate,
            previous_projection="Original.",
            previous_spans=(make_span(1, 0, 9, "Original."),),
            previous_groups=(
                AlignmentGroup(
                    ("anchor-1",), ("span-1",), "saved", 0.5, "needs_review", {}
                ),
            ),
        )
        is None
    )


@pytest.mark.parametrize(
    "size,identity", [(40_000, False), (3_000, False), (40_000, True)]
)
def test_rebase_bounds_repetitive_comparisons_before_diff(monkeypatch, size, identity):
    previous = "a" * size + "."
    updated = previous if identity else "b" + previous[1:]
    block, candidate, make_span = _rebase_case(updated)

    def unexpected_diff(*args, **kwargs):
        pytest.fail("An oversized or repetitive comparison reached SequenceMatcher")

    monkeypatch.setitem(
        rebase_sentence_alignment.__globals__, "SequenceMatcher", unexpected_diff
    )
    result = rebase_sentence_alignment(
        block,
        candidate,
        previous_projection=previous,
        previous_spans=(make_span(1, 0, len(previous), previous),),
        previous_groups=(
            AlignmentGroup(
                ("anchor-1",), ("span-1",), "saved", 0.5, "needs_review", {}
            ),
        ),
    )
    assert (result is not None) is identity


def test_rebase_rejects_boundary_crossing_punctuation_and_sentence_deletion():
    block, _candidate, make_span = _rebase_case("One. Extra Two.")
    previous = "One. Two."
    previous_spans = (
        make_span(1, 0, 5, previous[:5]),
        make_span(2, 5, len(previous), previous[5:]),
    )
    block.anchors = (
        SimpleNamespace(id="anchor-1", order=1),
        SimpleNamespace(id="anchor-2", order=2),
    )
    groups = (
        AlignmentGroup(("anchor-1",), ("span-1",), "saved", 0.9, "approved", {}),
        AlignmentGroup(("anchor-2",), ("span-2",), "saved", 0.9, "approved", {}),
    )
    candidate = TargetBlockCandidate(
        source_plan_id="plan-1",
        source_plan_hash="a" * 64,
        block_id="block-1",
        structural_fingerprint="b" * 64,
        parts=(TargetTextPart(kind="text", text="One. Extra Two."),),
        lexical_attributes=(),
    )
    assert (
        rebase_sentence_alignment(
            block,
            candidate,
            previous_projection=previous,
            previous_spans=previous_spans,
            previous_groups=groups,
        )
        is None
    )

    delete_candidate = TargetBlockCandidate(
        source_plan_id="plan-1",
        source_plan_hash="a" * 64,
        block_id="block-1",
        structural_fingerprint="b" * 64,
        parts=(TargetTextPart(kind="text", text="One. "),),
        lexical_attributes=(),
    )
    assert (
        rebase_sentence_alignment(
            block,
            delete_candidate,
            previous_projection=previous,
            previous_spans=previous_spans,
            previous_groups=groups,
        )
        is None
    )


def test_rebase_rejects_malformed_saved_partition_and_unknown_group_id():
    block, candidate, make_span = _rebase_case("Changed.")
    previous = "Original."
    valid_span = make_span(1, 0, len(previous), previous)
    groups = (
        AlignmentGroup(("anchor-1",), ("span-unknown",), "saved", 0.9, "approved", {}),
    )
    with pytest.raises(BlockContractError):
        rebase_sentence_alignment(
            block,
            candidate,
            previous_projection=previous,
            previous_spans=(valid_span,),
            previous_groups=groups,
        )
    with pytest.raises(BlockContractError):
        rebase_sentence_alignment(
            block,
            candidate,
            previous_projection=previous,
            previous_spans=(
                TargetSpan(
                    "span-1",
                    1,
                    1,
                    len(previous),
                    valid_span.text_hash,
                ),
            ),
            previous_groups=(
                AlignmentGroup(
                    ("anchor-1",),
                    ("span-1",),
                    "saved",
                    0.9,
                    "approved",
                    {},
                ),
            ),
        )


def test_explicit_sentence_replace_keeps_saved_partition_and_protected_parts():
    from iarf_assistant.addons.translator.block_contract import sha256_text

    block = SimpleNamespace(
        id="block-1",
        anchors=(SimpleNamespace(id="anchor-1", order=1),),
        slots=(
            SimpleNamespace(
                id="text",
                kind="text",
                disposition="translatable",
                source_value="source",
            ),
            SimpleNamespace(
                id="literal",
                kind="text",
                disposition="literal-preserved",
                source_value="[literal]",
            ),
        ),
        wire=(
            SimpleNamespace(kind="slot", ref_id="text"),
            SimpleNamespace(kind="slot", ref_id="literal"),
        ),
    )
    candidate = TargetBlockCandidate(
        "plan-1",
        "a" * 64,
        "block-1",
        "b" * 64,
        (
            TargetTextPart(kind="text", text="Old "),
            TargetTextPart(kind="literal", literal_slot_id="literal"),
            TargetTextPart(kind="text", text=" text."),
        ),
        (),
    )
    previous_projection = "Old [literal] text."
    span = TargetSpan(
        "span-1", 1, 0, len(previous_projection), sha256_text(previous_projection)
    )
    group = AlignmentGroup(
        ("anchor-1",), ("span-1",), "saved", 0.9, "approved", {}
    )

    edited, manifest = apply_sentence_edit(
        block,
        candidate,
        (span,),
        (group,),
        {"action": "replace", "sentence_order": 1, "text": "New [literal] text!"},
    )

    assert edited.parts[1] is candidate.parts[1]
    assert edited.parts[0].text == "New "
    assert edited.parts[2].text == " text!"
    assert manifest.target_sentences == ("New [literal] text!",)
    with pytest.raises(BlockContractError, match="sentence-edit-protected-text"):
        apply_sentence_edit(
            block,
            candidate,
            (span,),
            (group,),
            {"action": "replace", "sentence_order": 1, "text": "New text!"},
        )


def test_explicit_sentence_split_merge_transforms_cross_group_membership():
    from iarf_assistant.addons.translator.block_contract import sha256_text

    block, candidate, make_span = _rebase_case("One. Two. Three.", anchor_count=3)
    previous = "One. Two. Three."
    spans = (
        make_span(1, 0, 5, previous[:5]),
        make_span(2, 5, 10, previous[5:10]),
        make_span(3, 10, len(previous), previous[10:]),
    )
    groups = (
        AlignmentGroup(("anchor-1",), ("span-1",), "saved", 0.9, "approved", {}),
        AlignmentGroup(("anchor-2",), ("span-2",), "saved", 0.9, "approved", {}),
        AlignmentGroup(("anchor-3",), ("span-3",), "saved", 0.9, "approved", {}),
    )

    split_candidate, split_manifest = apply_sentence_edit(
        block,
        candidate,
        spans,
        groups,
        {"action": "split", "sentence_order": 1, "text": "One. First.", "offset": 5},
    )
    assert split_manifest.target_sentences == ("One.", "First.", "Two.", "Three.")
    assert [group.source_ordinals for group in split_manifest.groups] == [
        (1,),
        (2,),
        (3,),
    ]
    assert split_manifest.groups[0].target_ordinals == (1, 2)

    merged_candidate, merged_manifest = apply_sentence_edit(
        block,
        split_candidate,
        tuple(
            TargetSpan(
                f"split-{index}",
                index,
                span.start,
                span.end,
                sha256_text(split_manifest.target_spans[index - 1].text),
            )
            for index, span in enumerate(split_manifest.target_spans, start=1)
        ),
        tuple(
            AlignmentGroup(
                tuple(
                    f"anchor-{ordinal}"
                    for ordinal in group.source_ordinals
                ),
                tuple(f"split-{ordinal}" for ordinal in group.target_ordinals),
                "saved",
                0.9,
                "approved",
                {},
            )
            for group in split_manifest.groups
        ),
        {"action": "merge_next", "sentence_order": 1},
    )
    assert merged_candidate.parts[0].text == "One. First. Two. Three."
    assert merged_manifest.target_sentences == ("One. First.", "Two.", "Three.")
    assert merged_manifest.groups[0].source_ordinals == (1,)
    assert merged_manifest.groups[0].target_ordinals == (1,)


@pytest.mark.parametrize("action", ["replace", "split", "merge_next"])
def test_sentence_edit_preserves_other_sentences_in_attribute(action):
    from iarf_assistant.addons.translator.target_contract import TargetLexicalAttribute

    previous = "One. Two. Three."
    block, candidate, make_span = _rebase_case(previous, anchor_count=3)
    block.slots = (SimpleNamespace(id="alt", kind="attribute", disposition="translatable", source_value=previous),)
    block.wire = ()
    candidate = replace(candidate, parts=(), lexical_attributes=(TargetLexicalAttribute(slot_id="alt", value=previous),))
    spans = tuple(make_span(order, start, end, previous[start:end]) for order, start, end in [(1, 0, 5), (2, 5, 10), (3, 10, len(previous))])
    groups = tuple(AlignmentGroup((f"anchor-{order}",), (f"span-{order}",), "saved", 0.9, "approved", {}) for order in range(1, 4))
    edit = {"action": action, "sentence_order": 2}
    if action != "merge_next":
        edit["text"] = "New second."
    if action == "split":
        edit["offset"] = 4
    edited, manifest = apply_sentence_edit(block, candidate, spans, groups, edit)
    expected = previous if action == "merge_next" else "One. New second. Three."
    assert edited.lexical_attributes[0].value == expected
    assert "".join(span.text for span in manifest.target_spans) == expected
