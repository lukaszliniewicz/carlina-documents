from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant.addons.translator import (
    block_contract as translator_block_contract,
    block_planner as translator_block_planner,
    block_rendering as translator_block_rendering,
    source_plan_corrections as translator_source_plan_corrections,
)
# isort: on

LEGACY_BLOCK_PLANNER_VERSION = translator_block_contract.LEGACY_BLOCK_PLANNER_VERSION
AttestedDocumentSnapshot = translator_block_contract.AttestedDocumentSnapshot
BlockContractError = translator_block_contract.BlockContractError
SourceProviderAttestation = translator_block_contract.SourceProviderAttestation
_plan_document_blocks = translator_block_planner.plan_document_blocks
validate_canonical_plan = translator_block_planner.validate_canonical_plan
render_source_identity = translator_block_rendering.render_source_identity
build_successor_plan = translator_source_plan_corrections.build_successor_plan

FIXTURE = Path(__file__).parent / "fixtures" / "translator_block" / "v1"


def _snapshot(html: str) -> AttestedDocumentSnapshot:
    digest = hashlib.sha256(html.encode()).hexdigest()
    return AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )


def _attestation(snapshot: AttestedDocumentSnapshot) -> SourceProviderAttestation:
    return SourceProviderAttestation(
        provider_addon_id="doc-processor",
        provider_capability_id="document.processing",
        owner_id="owner-1",
        resource_id=snapshot.document_id,
    )


def plan_document_blocks(snapshot: AttestedDocumentSnapshot, **kwargs):
    return _plan_document_blocks(
        snapshot,
        source_attestation=kwargs.pop("source_attestation", _attestation(snapshot)),
        **kwargs,
    )


def test_fixture_taxonomy_coverage_and_retry_stability() -> None:
    html = (FIXTURE / "semantic-kitchen-sink.html").read_text()
    manifest = json.loads((FIXTURE / "expected-manifest.json").read_text())
    first = plan_document_blocks(
        _snapshot(html), project_id="project-1", source_language="en"
    )
    second = plan_document_blocks(
        _snapshot(html), project_id="project-1", source_language="en"
    )
    legacy = plan_document_blocks(
        _snapshot(html),
        project_id="project-1",
        source_language="en",
        planner_version=LEGACY_BLOCK_PLANNER_VERSION,
    )
    assert first.to_dict() == second.to_dict()
    assert first.plan_hash == second.plan_hash
    previous = plan_document_blocks(
        _snapshot(html), project_id="project-1", source_language="en",
        planner_version=translator_block_contract.BLOCK_PLANNER_V2,
    )
    assert previous.plan_hash == manifest["expected_plan_hash_v2"]
    validate_canonical_plan(previous)
    assert legacy.plan_hash == manifest["expected_plan_hash_v1"]
    assert set(manifest["required_roles"]).issubset(
        {block.role for block in first.blocks}
    )
    assert set(manifest["required_policies"]).issubset(
        {entry.policy_code for entry in first.coverage.entries}
        | {slot.policy_code for block in first.blocks for slot in block.slots}
    )
    assert all(
        entry.disposition != "translatable" or entry.block_id
        for entry in first.coverage.entries
    )
    assert all(
        [(anchor.start, anchor.end) for anchor in block.anchors]
        == [(0, len(block.projection))]
        or (
            block.anchors[0].start == 0
            and block.anchors[-1].end == len(block.projection)
        )
        for block in first.blocks
    )


@pytest.mark.parametrize("planner_version", ["document-block-planner-v0", []])
def test_planner_version_must_be_a_supported_string(planner_version) -> None:
    snapshot = _snapshot("<html><body><p>Source.</p></body></html>")
    with pytest.raises(BlockContractError, match="plan-planner-version"):
        plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_language="en",
            planner_version=planner_version,
        )


def test_changed_source_has_distinct_plan_and_direct_prose_runs() -> None:
    source = "<html><body>A <em>run</em><p>Paragraph.</p>B run.</body></html>"
    plan = plan_document_blocks(
        _snapshot(source), project_id="project-1", source_language="en"
    )
    changed = plan_document_blocks(
        _snapshot(source.replace("B run", "Changed run")),
        project_id="project-1",
        source_language="en",
    )
    assert plan.plan_hash != changed.plan_hash
    assert [
        block.projection for block in plan.blocks if block.role == "direct_prose"
    ] == ["A run", "B run."]


def test_project_scope_volatile_snapshot_and_probe_regressions() -> None:
    source = '<html lang="en"><body><p><strong>Hello</strong> <em>world</em>{{token}}</p><p><span lang="fr">Bonjour</span> normal.</p><li role="doc-endnote">Note.</li><script>x()</script></body></html>'
    # The active tag is forbidden before planning; the remaining probe document
    # verifies whitespace, template, foreign-language, and note-role policies.
    with pytest.raises(BlockContractError, match="html-tag-unknown"):
        plan_document_blocks(
            _snapshot(source), project_id="project-1", source_language="en"
        )
    source = source.replace("<script>x()</script>", "")
    first_snapshot = _snapshot(source)
    second_snapshot = AttestedDocumentSnapshot(
        **{
            **first_snapshot.to_dict(),
            "captured_at": "2026-01-01T00:00:00Z",
            "metadata": {"volatile": "ignored for plan identity"},
            "provenance": {"reader": "new"},
        }
    )
    first = plan_document_blocks(
        first_snapshot, project_id="project-1", source_language="en"
    )
    same = plan_document_blocks(
        second_snapshot, project_id="project-1", source_language="en"
    )
    other = plan_document_blocks(
        first_snapshot, project_id="project-2", source_language="en"
    )
    assert first.plan_hash == same.plan_hash
    assert first.plan_hash != other.plan_hash
    assert first.blocks[0].projection == "Hello world{{token}}"
    policies = {slot.policy_code for block in first.blocks for slot in block.slots}
    assert {"template-token", "foreign-language"}.issubset(policies)
    assert any(block.role == "note_body" for block in first.blocks)


def test_nested_ownership_boolean_attributes_and_pair_wire() -> None:
    source = "<html><body><blockquote>Before <em>one</em><p>Child <strong>two</strong>.</p>After.</blockquote><ol><li>Lead<ul><li>Nested.</li></ul>Tail.</li></ol><p hidden>gone</p></body></html>"
    plan = plan_document_blocks(
        _snapshot(source), project_id="project-1", source_language="en"
    )
    assert [
        (block.role, block.projection)
        for block in plan.blocks
        if block.role in {"blockquote", "list_item"}
    ] == [
        ("blockquote", "Before one"),
        ("blockquote", "Child two."),
        ("blockquote", "After."),
        ("list_item", "Lead"),
        ("list_item", "Nested."),
        ("list_item", "Tail."),
    ]
    token_ids = [token.id for block in plan.blocks for token in block.protected_inline]
    assert len(token_ids) == len(set(token_ids))
    pairs = [
        token
        for block in plan.blocks
        for token in block.protected_inline
        if token.kind in {"open", "close"}
    ]
    assert {token.element_id for token in pairs}
    assert any(
        entry.policy_code == "empty-or-boolean-attribute"
        for entry in plan.coverage.entries
    )


def test_versioned_focused_fixture_summaries() -> None:
    for stem in ("inline-template", "nested-notes", "structure"):
        html = (FIXTURE / f"{stem}.html").read_text()
        summary = json.loads((FIXTURE / f"{stem}.summary.json").read_text())
        plan = plan_document_blocks(
            _snapshot(html), project_id="project-1", source_language="en"
        )
        roles = {block.role for block in plan.blocks}
        assert set(summary["roles"]).issubset(roles)
        policies = {entry.policy_code for entry in plan.coverage.entries} | {
            slot.policy_code for block in plan.blocks for slot in block.slots
        }
        if "policies" in summary:
            assert set(summary["policies"]).issubset(policies)
        if "projection" in summary:
            assert summary["projection"] in {block.projection for block in plan.blocks}
        if "direct_projections" in summary:
            assert summary["direct_projections"] == [
                block.projection
                for block in plan.blocks
                if block.role == "direct_prose"
            ]
        if "role_projections" in summary:
            assert summary["role_projections"] == {
                role: [block.projection for block in plan.blocks if block.role == role]
                for role in summary["role_projections"]
            }
        if "literal_policy" in summary:
            assert summary["literal_policy"] in policies


def test_strict_parser_rejects_duplicate_unknown_and_malformed_syntax() -> None:
    for source, code in (
        (
            '<html><body><p class="a" class="b">x</p></body></html>',
            "html-attribute-duplicate",
        ),
        ('<html><body><p onclick="x">x</p></body></html>', "html-attribute-unknown"),
        ("<html><body><p>x</div></body></html>", "html-stack-mismatch"),
    ):
        with pytest.raises(BlockContractError, match=f"^{code}$"):
            plan_document_blocks(
                _snapshot(source), project_id="project-1", source_language="en"
            )


def test_attribute_witnesses_note_children_and_zero_writable_plan() -> None:
    source = '<html><body><p class="" title="" hidden>gone</p><a href=https://example.com>Link</a><aside role="doc-footnote"><p>Footnote paragraph.</p></aside><li role="doc-endnote"><p>Endnote paragraph.</p></li></body></html>'
    plan = plan_document_blocks(
        _snapshot(source), project_id="project-1", source_language="en"
    )
    empty = [
        entry
        for entry in plan.coverage.entries
        if entry.policy_code == "empty-or-boolean-attribute"
    ]
    assert empty and all(entry.raw_end > entry.raw_start for entry in empty)
    assert any(
        entry.source_hash
        for entry in plan.coverage.entries
        if entry.locator.endswith("/@href")
    )
    assert [block.role for block in plan.blocks].count("note_body") >= 2
    code_only = plan_document_blocks(
        _snapshot("<html><body><pre>code</pre></body></html>"),
        project_id="project-1",
        source_language="en",
    )
    assert not code_only.blocks
    assert (
        render_source_identity(code_only) == "<html><body><pre>code</pre></body></html>"
    )


def test_planner_rejects_bad_language_and_settings_types() -> None:
    snapshot = _snapshot("<html><body><p>Text.</p></body></html>")
    for kwargs in (
        {"source_language": 1},
        {"source_language": "en", "settings": []},
        {"source_language": "en", "project_id": 1},
    ):
        with pytest.raises(BlockContractError):
            plan_document_blocks(
                snapshot,
                project_id=kwargs.get("project_id", "project-1"),
                source_language=kwargs["source_language"],
                settings=kwargs.get("settings"),
            )


def test_anchor_abbreviation_and_whitespace_partition() -> None:
    plan = plan_document_blocks(
        _snapshot("<html><body><p>Dr. Smith stayed.  Then left.</p></body></html>"),
        project_id="project-1",
        source_language="en",
    )
    block = plan.blocks[0]
    assert (
        "".join(block.projection[anchor.start : anchor.end] for anchor in block.anchors)
        == block.projection
    )
    assert block.anchors[0].splitter_profile in {"pysbd-char-span", "fallback"}
    assert all(anchor.splitter_version for anchor in block.anchors)


def test_callback_anchors_preserve_exact_text_and_replay_without_provider() -> None:
    source = (
        "<html><body><p>אבג.  דהו.</p>"
        "<p>Dr. Smith stayed. Then left.</p>"
        "<div>Repeated.  Repeated.</div>"
        "<p><span dir=\"rtl\">טקסט <em>מדויק</em>.</span></p>"
        "<p title=\"תיאור נגיש.\">Accessibility.</p></body></html>"
    )
    snapshot = _snapshot(source)
    calls: list[str] = []
    segments = {
        "אבג.  דהו.": ("אבג.", "דהו."),
        "Dr. Smith stayed. Then left.": ("Dr. Smith stayed.", "Then left."),
        "Repeated.  Repeated.": ("Repeated.", "Repeated."),
        "טקסט מדויק.": ("טקסט מדויק.",),
        "Accessibility.": ("Accessibility.",),
        "תיאור נגיש.": ("תיאור נגיש.",),
    }

    def segment(projection: str) -> tuple[str, ...]:
        calls.append(projection)
        return segments[projection]

    plan = plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_language="he",
        sentence_segmenter=segment,
    )
    assert {block.role for block in plan.blocks} >= {
        "paragraph",
        "direct_prose",
        "accessibility_text",
    }
    assert all(
        anchor.splitter_profile == "sentence-segmenter"
        and anchor.splitter_version == "text.segmentation-v1"
        for block in plan.blocks
        for anchor in block.anchors
    )
    assert all(
        "".join(block.projection[anchor.start : anchor.end] for anchor in block.anchors)
        == block.projection
        for block in plan.blocks
    )
    assert render_source_identity(plan) == source
    assert len(calls) == len(set(calls))
    validate_canonical_plan(plan)


def test_aligned_sentence_planning_splits_oversized_plain_paragraphs_losslessly() -> None:
    sentences = tuple(
        f"Sentence {index:02d} " + ("content " * 18) + "."
        for index in range(1, 13)
    )
    projection = "  ".join(sentences)
    source = f"<html><body><p>{projection}</p></body></html>"
    snapshot = _snapshot(source)

    def segment(text: str) -> tuple[str, ...]:
        assert text == projection
        return sentences

    plan = plan_document_blocks(
        snapshot,
        project_id="project-split",
        source_language="en",
        settings={"translation_max_block_chars": 500},
        sentence_segmenter=segment,
    )
    paragraphs = [block for block in plan.blocks if block.role == "paragraph"]
    assert len(paragraphs) > 1
    assert max(len(block.projection) for block in paragraphs) <= 500
    assert "".join(block.projection for block in paragraphs) == projection
    assert paragraphs[0].source_start < paragraphs[0].source_end
    assert paragraphs[-1].source_start < paragraphs[-1].source_end
    assert all(
        left.source_end == right.source_start
        for left, right in zip(paragraphs, paragraphs[1:])
    )
    assert all(
        anchor.splitter_profile == "sentence-segmenter"
        for block in paragraphs
        for anchor in block.anchors
    )
    assert render_source_identity(plan) == source
    validate_canonical_plan(plan)

    block_mode = plan_document_blocks(
        snapshot,
        project_id="project-block",
        source_language="en",
    )
    block_paragraphs = [block for block in block_mode.blocks if block.role == "paragraph"]
    assert len(block_paragraphs) == 1
    assert block_paragraphs[0].projection == projection


def test_aligned_sentence_split_never_bisects_protected_inline_scope() -> None:
    sentences = tuple(
        f"Sentence {index:02d} " + ("content " * 18) + "."
        for index in range(1, 8)
    )
    projection = "  ".join(sentences)
    source = f"<html><body><p><em>{projection}</em></p></body></html>"
    snapshot = _snapshot(source)

    def segment(text: str) -> tuple[str, ...]:
        assert text == projection
        return sentences

    plan = plan_document_blocks(
        snapshot,
        project_id="project-inline",
        source_language="en",
        settings={"translation_max_block_chars": 500},
        sentence_segmenter=segment,
    )
    paragraphs = [block for block in plan.blocks if block.role == "paragraph"]
    assert len(paragraphs) == 1
    assert len(paragraphs[0].projection) > 500
    assert {token.kind for token in paragraphs[0].protected_inline} == {"open", "close"}
    assert render_source_identity(plan) == source
    validate_canonical_plan(plan)


def test_callback_invalid_coverage_and_provider_failure_fail_closed() -> None:
    snapshot = _snapshot("<html><body><p>First. Second.</p></body></html>")
    for result in ([], ["Altered.", "Second."], ["First.", "Missing."]):
        with pytest.raises(BlockContractError, match="source-sentence-segmentation-"):
            plan_document_blocks(
                snapshot,
                project_id="project-1",
                source_language="en",
                sentence_segmenter=lambda _projection, result=result: result,
            )

    def provider_failure(_projection: str) -> list[str]:
        raise RuntimeError("private source must not appear")

    with pytest.raises(BlockContractError, match=r"^source-sentence-segmentation-failed$") as exc:
        plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_language="en",
            sentence_segmenter=provider_failure,
        )
    assert str(exc.value) == "source-sentence-segmentation-failed"


def test_manifest_bounds_reject_missing_and_extra_projection_entries() -> None:
    snapshot = _snapshot("<html><body><p>First. Second.</p></body></html>")
    callback_plan = plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_language="en",
        sentence_segmenter=lambda projection: [projection],
    )
    settings = callback_plan.to_dict()["planner_settings"]
    projection_hash = next(iter(settings["source_sentence_manifest"]["projections"]))
    missing = {
        **settings,
        "source_sentence_manifest": {
            **settings["source_sentence_manifest"],
            "projections": {},
        },
    }
    with pytest.raises(BlockContractError, match="source-sentence-segmentation-"):
        plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_language="en",
            settings=missing,
        )
    extra = {
        **settings,
        "source_sentence_manifest": {
            **settings["source_sentence_manifest"],
            "projections": {
                **settings["source_sentence_manifest"]["projections"],
                "0" * 64: [1],
            },
        },
    }
    with pytest.raises(BlockContractError, match="source-sentence-segmentation-"):
        plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_language="en",
            settings=extra,
        )
    conflict = {"source_sentence_manifest": {}}
    with pytest.raises(BlockContractError, match="source-sentence-segmentation-manifest-conflict"):
        plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_language="en",
            settings=conflict,
            sentence_segmenter=lambda projection: [projection],
        )
    assert projection_hash


def test_manifest_replay_supports_more_than_generic_settings_item_limit() -> None:
    source = "<html><body>" + "".join(
        f"<p>Unique sentence {index}.</p>" for index in range(130)
    ) + "</body></html>"
    plan = plan_document_blocks(
        _snapshot(source),
        project_id="project-1",
        source_language="en",
        sentence_segmenter=lambda projection: [projection],
    )
    assert len(plan.blocks) == 130
    assert len(plan.planner_settings["source_sentence_manifest"]["projections"]) == 130
    validate_canonical_plan(plan)


@pytest.mark.parametrize("planner_version", [
    translator_block_contract.BLOCK_PLANNER_V2,
    translator_block_contract.BLOCK_PLANNER_VERSION,
])
def test_corrected_plan_replays_callback_manifest_without_provider(planner_version) -> None:
    source = "<html><body><p>First. Second.</p><p>Third. Fourth.</p></body></html>"

    def segment(projection: str) -> list[str]:
        boundary = projection.index(". ") + 1
        return [projection[:boundary], projection[boundary + 1 :]]

    plan = plan_document_blocks(
        _snapshot(source),
        project_id="project-1",
        source_language="en",
        sentence_segmenter=segment,
        planner_version=planner_version,
    )
    corrected = build_successor_plan(
        plan,
        block_id=plan.blocks[0].id,
        action="merge_next",
    ).plan
    validate_canonical_plan(corrected)
    assert (
        corrected.planner_settings["base_planner_settings"][
            "source_sentence_manifest"
        ]
        == plan.planner_settings["source_sentence_manifest"]
    )


def test_manifest_projection_cap_fails_before_provider_calls(monkeypatch) -> None:
    monkeypatch.setattr(translator_block_planner, "MAX_SOURCE_SENTENCE_PROJECTIONS", 1)
    snapshot = _snapshot(
        "<html><body><p>First.</p><p>Second.</p></body></html>"
    )
    calls: list[str] = []
    with pytest.raises(BlockContractError, match="source-sentence-segmentation-manifest"):
        plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_language="en",
            sentence_segmenter=lambda projection: calls.append(projection) or [projection],
        )
    assert calls == []


def test_attestation_and_settings_are_pinned_and_round_trip() -> None:
    snapshot = _snapshot("<html><body><p>Text.</p></body></html>")
    settings = {"profile": {"strict": True}}
    plan = plan_document_blocks(
        snapshot, project_id="project-1", source_language="en", settings=settings
    )
    assert plan.planner_settings == settings
    assert type(plan).from_dict(plan.to_dict()).to_dict() == plan.to_dict()
    bad = plan.to_dict()
    bad["settings_fingerprint"] = "0" * 64
    with pytest.raises(BlockContractError, match="plan-settings-fingerprint"):
        type(plan).from_dict(bad)
    with pytest.raises(BlockContractError, match="source-attestation-resource"):
        _plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_attestation={
                **_attestation(snapshot).to_dict(),
                "resource_id": "other-document",
            },
            source_language="en",
        )
    with pytest.raises(BlockContractError):
        _plan_document_blocks(
            snapshot,
            project_id="project-1",
            source_attestation={"version": 1},
            source_language="en",
        )


def test_nested_paragraphs_and_direct_runs_keep_structural_roles() -> None:
    source = (
        "<html><body>"
        "<ul><li>Lead<p>Item paragraph.</p>Tail.</li></ul>"
        "<blockquote>Before<p>Quoted paragraph.</p>After.</blockquote>"
        "<dl><dd><p>Definition paragraph.</p></dd></dl>"
        "<table><tr><td><p>Cell paragraph.</p></td></tr></table>"
        "<figure><figcaption><p>Caption paragraph.</p></figcaption></figure>"
        "</body></html>"
    )
    plan = plan_document_blocks(
        _snapshot(source), project_id="project-1", source_language="en"
    )

    assert [(block.role, block.projection) for block in plan.blocks] == [
        ("list_item", "Lead"),
        ("list_item", "Item paragraph."),
        ("list_item", "Tail."),
        ("blockquote", "Before"),
        ("blockquote", "Quoted paragraph."),
        ("blockquote", "After."),
        ("definition_body", "Definition paragraph."),
        ("table_cell", "Cell paragraph."),
        ("figure_caption", "Caption paragraph."),
    ]


def test_v3_preserves_whitespace_entities_without_creating_blank_translation_blocks():
    source = '<html><body><p>&nbsp;</p><p>&#160;</p><p>Hello&nbsp;world.</p></body></html>'
    snapshot = _snapshot(source)
    plan = plan_document_blocks(snapshot, project_id="whitespace", source_language="en")
    assert [block.projection for block in plan.blocks] == ["Hello\u00a0world."]
    assert render_source_identity(plan) == source
    validate_canonical_plan(plan)
    previous = plan_document_blocks(
        snapshot, project_id="whitespace", source_language="en",
        planner_version=translator_block_contract.BLOCK_PLANNER_V2,
    )
    assert len(previous.blocks) == 3
    validate_canonical_plan(previous)
    assert render_source_identity(previous) == source


def test_direct_prose_keeps_line_break_inside_one_block() -> None:
    source = "<html><body><div>Hello<br>World.</div></body></html>"
    plan = plan_document_blocks(
        _snapshot(source), project_id="project-1", source_language="en"
    )

    assert [(block.role, block.projection) for block in plan.blocks] == [
        ("direct_prose", "Hello\nWorld.")
    ]
    assert [(token.kind, token.tag) for token in plan.blocks[0].protected_inline] == [
        ("void", "br")
    ]
    assert render_source_identity(plan) == source


@pytest.mark.parametrize("tag", ["p", "div"])
def test_v3_line_break_attributes_have_disjoint_coverage(tag):
    source = (
        f'<html><body><{tag}>Hello<br class="line" title="Pause">'
        f'World.</{tag}></body></html>'
    )
    plan = plan_document_blocks(
        _snapshot(source), project_id="break-attributes", source_language="en"
    )
    assert {block.projection for block in plan.blocks} == {"Hello\nWorld.", "Pause"}
    validate_canonical_plan(plan)
    assert render_source_identity(plan) == source
    spans = sorted((entry.raw_start, entry.raw_end) for entry in plan.coverage.entries)
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:]))


def test_doc_processor_running_marginalia_is_not_a_translation_block() -> None:
    source = (
        '<html><body><div class="page_header">Running title</div>'
        '<p>Chapter text.</p><section class="page_footer">42</section>'
        '<div class="running_header">Repeated chapter</div>'
        '<footer class="running_footer">Copyright notice</footer></body></html>'
    )
    plan = plan_document_blocks(
        _snapshot(source), project_id="project-1", source_language="en"
    )

    assert [(block.role, block.projection) for block in plan.blocks] == [
        ("paragraph", "Chapter text.")
    ]
    assert {
        entry.policy_code
        for entry in plan.coverage.entries
        if entry.disposition == "excluded-with-reason"
    } >= {"running-marginalia"}
    assert render_source_identity(plan) == source
