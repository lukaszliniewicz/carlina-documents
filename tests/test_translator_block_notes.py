from __future__ import annotations

import hashlib
import importlib

import bs4
import pytest
import sqlalchemy as sa

from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_notes import (
    ANNOTATION_PROFILE_NONE,
    ANNOTATION_PROFILE_RESTRAINED,
    ANNOTATION_PROFILE_STUDY,
    MAX_BLOCK_NOTE_ANCHOR_CHARS,
    MAX_BLOCK_NOTE_BODY_CHARS,
    MAX_BLOCK_NOTES_PER_WINDOW,
    MAX_STUDY_NOTES_PER_WINDOW,
    BlockNoteCandidate,
    normalize_annotation_profile,
    note_anchor_span,
    note_from_provider,
    render_note_body,
    validate_note_candidates,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.compact_markup import (
    compact_result_schema,
    compact_source_block,
    normalize_compact_result,
)
from iarf_assistant.addons.translator.target_rendering import render_target_document


@pytest.mark.parametrize(("body", "expected"), [
    ("**Bold _term_** and `code`.", "<strong>Bold <em>term</em></strong> and <code>code</code>."),
    ("<img src=x onerror=alert(1)> &amp; [link](https://example.test)", "&lt;img src=x onerror=alert(1)&gt; &amp;amp; [link](https://example.test)"),
    (r"\*literal\* word_part עברית", "*literal* word_part עברית"),
    ("`<script>`\nNext", "<code>&lt;script&gt;</code><br>Next"),
    ("# Heading\n- item ~~strike~~", "# Heading<br>- item ~~strike~~"),
])
def test_note_body_renders_only_limited_inline_markdown(body, expected):
    assert render_note_body(body) == expected


def test_project_cleanup_is_child_first_and_preserves_external_sources(
    tmp_path,
) -> None:
    migration = importlib.import_module(
        "iarf_assistant.addons.translator.migrations.versions.022_block_translator_notes"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql(
            "CREATE TABLE processed_document (id TEXT PRIMARY KEY, title TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE subtitle_project (id TEXT PRIMARY KEY, title TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE translation_project (id TEXT PRIMARY KEY, document_id TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE translation_parent ("
            "id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES translation_project(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE translation_child ("
            "id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES translation_parent(id) ON DELETE RESTRICT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO processed_document VALUES ('document-1', 'Book')"
        )
        connection.exec_driver_sql(
            "INSERT INTO subtitle_project VALUES ('subtitle-1', 'Episode')"
        )
        connection.exec_driver_sql(
            "INSERT INTO translation_project VALUES ('project-1', 'document-1')"
        )
        connection.exec_driver_sql(
            "INSERT INTO translation_parent VALUES ('parent-1', 'project-1')"
        )
        connection.exec_driver_sql(
            "INSERT INTO translation_child VALUES ('child-1', 'parent-1')"
        )

        migration._delete_translator_rows(connection)
        connection.execute(sa.text('DELETE FROM "translation_project"'))

        assert connection.scalar(sa.text("SELECT count(*) FROM translation_child")) == 0
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM translation_parent")) == 0
        )
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM translation_project")) == 0
        )
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM processed_document")) == 1
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM subtitle_project")) == 1


def _plan(
    source: str = "<html><body><p>Hello <em>dao</em>.</p></body></html>",
):
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="document-1",
        document_id="document-1",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
    )
    return plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="document-provider",
            provider_capability_id="document.processing",
            owner_id="owner-1",
            resource_id="document-1",
        ),
        source_language="en",
    )


def test_compact_note_schema_is_exactly_opt_in() -> None:
    disabled = compact_result_schema()
    disabled_result = disabled["properties"]["results"]["items"]
    assert disabled_result["required"] == ["n", "content", "fields"]
    assert set(disabled_result["properties"]) == {"n", "content", "fields"}

    enabled = compact_result_schema(notes_enabled=True)
    enabled_result = enabled["properties"]["results"]["items"]
    assert enabled_result["required"] == ["n", "content", "fields", "notes"]
    assert set(enabled_result["properties"]) == {"n", "content", "fields", "notes"}
    assert enabled_result["properties"]["notes"]["maxItems"] == 1


def test_note_bounds_plain_text_and_anchor_topology_are_fail_closed() -> None:
    valid = BlockNoteCandidate(
        block_id="block-1",
        anchor_text="a" * MAX_BLOCK_NOTE_ANCHOR_CHARS,
        body="b" * MAX_BLOCK_NOTE_BODY_CHARS,
    )
    assert valid.to_provider_dict() == {
        "anchor": "a" * MAX_BLOCK_NOTE_ANCHOR_CHARS,
        "body": "b" * MAX_BLOCK_NOTE_BODY_CHARS,
    }
    for anchor, body in (
        ("a" * (MAX_BLOCK_NOTE_ANCHOR_CHARS + 1), "body"),
        ("anchor", "b" * (MAX_BLOCK_NOTE_BODY_CHARS + 1)),
        ("<anchor>", "body"),
        ("anchor", "[text](https://example.test)"),
        ("anchor\n", "body"),
        ("anchor", "body\x00"),
    ):
        with pytest.raises(BlockContractError):
            note_from_provider("block-1", {"anchor": anchor, "body": body})

    with pytest.raises(BlockContractError):
        note_anchor_span("<p>foo foo</p>", "foo")
    with pytest.raises(BlockContractError):
        note_anchor_span("<p>foo <em>bar</em></p>", "foo bar")
    with pytest.raises(BlockContractError):
        note_anchor_span("<p>aaa</p>", "aa")


def test_note_window_cap_and_unique_block_anchors() -> None:
    notes = tuple(
        BlockNoteCandidate(f"block-{index}", f"term-{index}", "Meaning.")
        for index in range(MAX_BLOCK_NOTES_PER_WINDOW + 1)
    )
    with pytest.raises(BlockContractError, match="block-note-window-cap"):
        validate_note_candidates(
            notes,
            {note.block_id: f"<p>{note.anchor_text}</p>" for note in notes},
        )

    duplicate = (
        BlockNoteCandidate("block-1", "term", "Meaning."),
        BlockNoteCandidate("block-1", "other", "Meaning."),
    )
    with pytest.raises(BlockContractError, match="block-note-block-duplicate"):
        validate_note_candidates(duplicate, {"block-1": "<p>term other</p>"})


def test_annotation_profile_normalization_preserves_explicit_precedence() -> None:
    assert normalize_annotation_profile(False) == ANNOTATION_PROFILE_NONE
    assert normalize_annotation_profile(True) == ANNOTATION_PROFILE_RESTRAINED
    assert normalize_annotation_profile(None, False) == ANNOTATION_PROFILE_NONE
    assert normalize_annotation_profile(None, True) == ANNOTATION_PROFILE_RESTRAINED
    assert normalize_annotation_profile(ANNOTATION_PROFILE_NONE, True) == (
        ANNOTATION_PROFILE_NONE
    )
    assert normalize_annotation_profile(ANNOTATION_PROFILE_STUDY, False) == (
        ANNOTATION_PROFILE_STUDY
    )
    with pytest.raises(BlockContractError, match="block-note-profile"):
        normalize_annotation_profile("unsupported", True)


def test_none_profile_rejects_candidates_and_restrained_keeps_one_per_block() -> None:
    note = BlockNoteCandidate("block-1", "term", "Meaning.")
    with pytest.raises(BlockContractError, match="block-note-disabled"):
        validate_note_candidates(
            (note,), {"block-1": "<p>term</p>"}, annotation_profile="none"
        )
    with pytest.raises(BlockContractError, match="block-note-block-duplicate"):
        validate_note_candidates(
            (
                note,
                BlockNoteCandidate("block-1", "other", "Meaning."),
            ),
            {"block-1": "<p>term other</p>"},
            annotation_profile="restrained",
        )


def test_study_profile_allows_multiple_notes_but_enforces_caps_and_duplicates() -> None:
    anchors = tuple("abcdef")
    notes = tuple(
        BlockNoteCandidate("block-1", anchor, "Meaning.") for anchor in anchors
    )
    assert (
        validate_note_candidates(
            notes,
            {"block-1": "<p>a b c d e f</p>"},
            annotation_profile="study",
        )
        == notes
    )
    with pytest.raises(BlockContractError, match="block-note-block-cap"):
        validate_note_candidates(
            (*notes, BlockNoteCandidate("block-1", "g", "Meaning.")),
            {"block-1": "<p>a b c d e f g</p>"},
            annotation_profile="study",
        )
    duplicate = (
        BlockNoteCandidate("block-1", "a", "First."),
        BlockNoteCandidate("block-1", "a", "Second."),
    )
    with pytest.raises(BlockContractError, match="block-note-anchor-duplicate"):
        validate_note_candidates(
            duplicate, {"block-1": "<p>a</p>"}, annotation_profile="study"
        )
    window_notes = tuple(
        BlockNoteCandidate(f"block-{index}", "term", "Meaning.")
        for index in range(MAX_STUDY_NOTES_PER_WINDOW + 1)
    )
    with pytest.raises(BlockContractError, match="block-note-window-cap"):
        validate_note_candidates(
            window_notes,
            {note.block_id: "<p>term</p>" for note in window_notes},
            annotation_profile=ANNOTATION_PROFILE_STUDY,
        )


def test_enabled_normalization_returns_candidate_and_verified_notes() -> None:
    plan = _plan()
    block = plan.blocks[0]
    source = compact_source_block(block, number=1)
    normalized = normalize_compact_result(
        plan=plan,
        plan_id="plan-1",
        block=block,
        expected_number=1,
        value={
            "n": source["n"],
            "content": source["content"],
            "fields": source["fields"],
            "notes": [{"anchor": "dao", "body": "A philosophical concept."}],
        },
        notes_enabled=True,
    )
    candidate, notes = normalized
    assert candidate.block_id == block.id
    assert notes == (BlockNoteCandidate(block.id, "dao", "A philosophical concept."),)

    with pytest.raises(BlockContractError, match="compact-result-shape"):
        normalize_compact_result(
            plan=plan,
            plan_id="plan-1",
            block=block,
            expected_number=1,
            value={
                "n": source["n"],
                "content": source["content"],
                "fields": source["fields"],
            },
            notes_enabled=True,
        )


def test_compact_study_notes_preserve_order_through_document_rendering() -> None:
    plan = _plan("<html><body><p>dao alpha beta gamma delta epsilon.</p></body></html>")
    block = plan.blocks[0]
    normalized = normalize_compact_result(
        plan=plan,
        plan_id="plan-1",
        block=block,
        expected_number=1,
        value={
            "n": 1,
            "content": "dao alpha beta gamma delta epsilon.",
            "fields": [],
            "notes": [
                {"anchor": anchor, "body": f"Study note for {anchor}."}
                for anchor in ("dao", "alpha", "beta", "gamma", "delta", "epsilon")
            ],
        },
        annotation_profile=ANNOTATION_PROFILE_STUDY,
    )

    candidate, notes = normalized
    assert [note.order_index for note in notes] == list(range(6))
    rendered = render_target_document(
        plan,
        "plan-1",
        {block.id: candidate},
        target_language="en",
        notes={block.id: notes},
    )
    assert rendered.count('role="doc-noteref"') == 6
    soup = bs4.BeautifulSoup(rendered, "html.parser")
    assert len(soup.select('[role="doc-endnotes"] [role="list"] > [role="listitem"]')) == 6
    assert not soup.select('[role="doc-endnotes"] [role="doc-footnote"]')
    assert rendered.count('role="doc-backlink"') == 6
    positions = [rendered.index(note.body) for note in notes]
    assert positions == sorted(positions)


def test_source_and_translator_notes_keep_distinct_markers_and_visible_origins() -> (
    None
):
    plan = _plan(
        '<html><body><p>Source wording<a id="source-ref" role="doc-noteref" '
        'epub:type="noteref" href="#source-note">1</a>.</p>'
        '<aside id="source-note" role="doc-footnote" epub:type="footnote">'
        "<p>2024 is the publication year. "
        '<a role="doc-backlink" href="#source-ref">↩</a></p></aside></body></html>'
    )
    candidates = {}
    for number, block in enumerate(plan.blocks, start=1):
        source = compact_source_block(block, number=number)
        candidates[block.id] = normalize_compact_result(
            plan=plan,
            plan_id="plan-1",
            block=block,
            expected_number=number,
            value={key: source[key] for key in ("n", "content", "fields")},
        )
    rendered = render_target_document(
        plan,
        "plan-1",
        candidates,
        target_language="en",
        notes={
            plan.blocks[0].id: [
                {"anchor_text": "Source wording", "body": "An added explanation."}
            ]
        },
    )
    soup = bs4.BeautifulSoup(rendered, "html.parser")
    assert soup.find(id="source-ref").get_text() == "1"
    original = soup.find(id="source-note")
    assert original.get_text().startswith("2024 is the publication year.")
    assert "Translator’s note" not in original.get_text()
    added = soup.select_one(".translator-note")
    assert added.get_text().startswith("T1. Translator’s note: An added explanation.")
    refs = soup.select('[role="doc-noteref"]')
    assert {ref.get_text() for ref in refs} == {"1", "[T1]"}
    assert len({ref["href"] for ref in refs}) == 2


def test_rendered_notes_are_numbered_escaped_and_backlinked() -> None:
    plan = _plan()
    block = plan.blocks[0]
    source = compact_source_block(block, number=1)
    candidate = normalize_compact_result(
        plan=plan,
        plan_id="plan-1",
        block=block,
        expected_number=1,
        value={key: source[key] for key in ("n", "content", "fields")},
    )
    rendered = render_target_document(
        plan,
        "plan-1",
        {block.id: candidate},
        target_language="pl",
        notes={
            block.id: [
                {
                    "anchor_text": "dao",
                    "body": "Meaning & **use** for *readers* with `dao`.",
                }
            ]
        },
    )
    assert 'role="doc-noteref"' in rendered
    assert 'role="doc-endnotes"' in rendered
    soup = bs4.BeautifulSoup(rendered, "html.parser")
    assert len(soup.select('[role="doc-endnotes"] [role="list"] > [role="listitem"]')) == 1
    assert not soup.select('[role="doc-endnotes"] [role="doc-footnote"]')
    assert 'role="doc-backlink"' in rendered
    assert "Meaning &amp; <strong>use</strong> for <em>readers</em> with <code>dao</code>." in rendered
    assert 'aria-label="Translator note T1">[T1]</a>' in rendered
    assert '<span class="footnote-marker">T1. </span>' in rendered
    assert "Translator’s note:" in rendered
    assert rendered.index("dao") < rendered.index('role="doc-noteref"')

    with pytest.raises(BlockContractError, match="target-document-note-owner"):
        render_target_document(
            plan,
            "plan-1",
            {block.id: candidate},
            target_language="pl",
            notes={"missing-attribute-block": []},
        )
