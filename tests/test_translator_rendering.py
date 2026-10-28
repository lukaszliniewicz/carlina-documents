from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from iarf_assistant.addons.translator.rendering import render_project_html


def _project(skeleton: str, segments: list[SimpleNamespace], **overrides):
    values = {
        "id": "project-1",
        "user_id": 7,
        "name": "A <title> & report",
        "target_lang": "pl",
        "skeleton_html": skeleton,
        "segments": segments,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _segment(
    segment_id: str,
    *,
    translated: str | None = None,
    original: str = "",
    merged_into_id: str | None = None,
    outer_tag: str = "p",
    version: int = 1,
):
    return SimpleNamespace(
        id=segment_id,
        translated_text=translated,
        original_text=original,
        merged_into_id=merged_into_id,
        outer_tag=outer_tag,
        version=version,
    )


def test_render_preserves_document_structure_but_removes_active_markup():
    project = _project(
        """
        <article onmouseover="alert(1)">
          <h1>{{heading}}</h1>
          <script>alert('source')</script>
          <style>body { background: url(javascript:bad) }</style>
          <iframe src="https://evil.example/collect">frame</iframe>
          <form><button>Submit secret</button></form>
          <p>{{body}}</p>
          <table><tr><th colspan="2">Term</th></tr><tr><td>A</td><td>B</td></tr></table>
          <figure><figcaption>Caption</figcaption></figure>
          <a href="javascript:alert(1)" onclick="bad()">bad link</a>
          <a href="/safe/chapter#part-1">safe relative link</a>
          <img src="https://remote.example/track.gif" onerror="bad()" style="x">
        </article>
        """,
        [
            _segment("heading", translated="<strong>Translated</strong>"),
            _segment(
                "body",
                translated='<b>Safe</b><script>model_secret()</script><div onload="bad">text</div>',
            ),
        ],
    )

    rendered = render_project_html(project)
    lowered = rendered.lower()

    assert "<strong>Translated</strong>" in rendered
    assert "<b>Safe</b>" in rendered
    assert "<table>" in rendered and '<th colspan="2">Term</th>' in rendered
    assert "<figcaption>Caption</figcaption>" in rendered
    assert "/safe/chapter#part-1" in rendered
    assert "script" not in lowered
    assert "style" not in lowered
    assert "iframe" not in lowered
    assert "submit secret" not in lowered
    assert "javascript:" not in lowered
    assert "onmouseover" not in lowered
    assert "onload" not in lowered
    assert "onerror" not in lowered
    assert "remote.example" not in rendered
    assert "data:image/gif;base64," in rendered
    assert "<title>A &lt;title&gt; &amp; report</title>" in rendered


def test_malicious_original_fallback_is_sanitized_and_merged_children_are_omitted():
    project = _project(
        "<section><p>{{parent}}</p><p>{{child}}</p></section>",
        [
            _segment(
                "parent",
                original='<em>Original</em><script>steal()</script><a href="file:///secret">link</a>',
            ),
            _segment(
                "child",
                translated="Should not be rendered",
                merged_into_id="parent",
            ),
        ],
    )

    rendered = render_project_html(project)
    lowered = rendered.lower()

    assert "<em>Original</em>" in rendered
    assert "Should not be rendered" not in rendered
    assert "steal()" not in rendered
    assert "file:///secret" not in rendered
    assert "<script" not in lowered


def test_preview_keeps_only_owner_uploads_and_safe_inline_images(tmp_path):
    owner_file = tmp_path / "7" / "images" / "owned.png"
    owner_file.parent.mkdir(parents=True)
    owner_file.write_bytes(b"not-used-by-preview")
    (tmp_path / "8").mkdir()

    project = _project(
        """
        <p>{{text}}</p>
        <img src="/uploads/7/images/owned.png">
        <img src="/uploads/8/images/foreign.png">
        <img src="/uploads/7/%2e%2e/8/images/foreign.png">
        <img src="//remote.example/image.png">
        <img src="file:///etc/passwd">
        <img src="data:image/png;base64,AAAA">
        """,
        [_segment("text", translated="Text")],
    )

    rendered = render_project_html(project, uploads_dir=tmp_path)

    assert "/uploads/7/images/owned.png" in rendered
    assert "/uploads/8/images/foreign.png" not in rendered
    assert "%2e%2e" not in rendered
    assert "//remote.example" not in rendered
    assert "file:///etc/passwd" not in rendered
    assert "data:image/png;base64,AAAA" in rendered


def test_export_resolves_existing_owner_upload_and_never_leaves_remote_paths(tmp_path):
    owner_file = tmp_path / "7" / "images" / "owned.png"
    owner_file.parent.mkdir(parents=True)
    owner_file.write_bytes(b"owned")
    foreign_file = tmp_path / "8" / "images" / "foreign.png"
    foreign_file.parent.mkdir(parents=True)
    foreign_file.write_bytes(b"foreign")

    project = _project(
        """
        <figure>
          <img src="/uploads/7/images/owned.png">
          <img src="/uploads/8/images/foreign.png">
          <img src="https://remote.example/image.png">
          <img src="/tmp/outside.png">
        </figure>
        """,
        [],
    )

    rendered = render_project_html(project, for_export=True, uploads_dir=tmp_path)

    assert str(owner_file.resolve()) in rendered
    assert str(foreign_file.resolve()) not in rendered
    assert "remote.example" not in rendered
    assert "/tmp/outside.png" not in rendered
    assert rendered.count("data:image/gif;base64,") == 3


def test_inline_image_limit_is_checked_without_the_generic_attribute_cap():
    prefix = "data:image/png;base64,"
    valid_image = prefix + ("A" * 5_000)
    oversized_image = prefix + ("A" * (1_048_576 - len(prefix) + 1))
    project = _project(
        f'<p><img src="{valid_image}"><img src="{oversized_image}"></p>',
        [],
    )

    rendered = render_project_html(project)
    sources = [
        str(image.get("src") or "")
        for image in BeautifulSoup(rendered, "html.parser").find_all("img")
    ]

    assert len(sources[0]) == len(valid_image)
    assert sources[0].startswith(prefix)
    assert sources[1].startswith("data:image/gif;base64,")
    assert len(sources[1]) < len(oversized_image)


def test_existing_html_frame_uses_the_validated_target_language():
    project = _project(
        '<!doctype html><html lang="en"><head><title>Source</title></head>'
        "<body><p>{{text}}</p></body></html>",
        [_segment("text", translated="Treść")],
        target_lang="de",
    )

    rendered = render_project_html(project)
    root = BeautifulSoup(rendered, "html.parser").find("html")

    assert root is not None
    assert root.get("lang") == "de"
    assert "Treść" in rendered


def test_multilingual_and_hidden_epub_semantics_survive_sanitization():
    project = _project(
        '<p><span xml:lang="fr" hidden>Bonjour</span> '
        '<span xml:lang="not valid" hidden="false">Invalid metadata</span></p>',
        [],
    )

    rendered = BeautifulSoup(render_project_html(project), "html.parser")
    spans = rendered.find_all("span")

    assert spans[0].get("xml:lang") == "fr"
    assert spans[0].has_attr("hidden")
    assert not spans[1].has_attr("xml:lang")
    assert not spans[1].has_attr("hidden")


def _note(
    note_id: str,
    anchor: str,
    body: str,
    *,
    order_index: int = 0,
    segment_version: int = 1,
    state: str = "active",
):
    return SimpleNamespace(
        id=note_id,
        anchor_text=anchor,
        body=body,
        order_index=order_index,
        segment_version=segment_version,
        state=state,
    )


def test_enabled_translator_notes_are_numbered_and_semantic_in_anchor_order():
    first = _segment("first", translated="Hanshi Festival and Lantern Festival.")
    first.translator_notes = [
        _note(
            "note-lantern",
            "Lantern Festival",
            "A conventional festival term.",
            order_index=1,
        ),
        _note(
            "note-hanshi",
            "Hanshi Festival",
            "Cultural & historical context.",
            order_index=0,
        ),
    ]
    second = _segment("second", translated="Dao shapes the practice.")
    second.translator_notes = [
        _note(
            "note-dao",
            "Dao",
            "A philosophical sense not carried by the surrounding prose.",
        ),
    ]
    project = _project(
        "<article><p>{{first}}</p><p>{{second}}</p></article>",
        [first, second],
        settings={"enable_translator_notes": True},
    )

    rendered = render_project_html(project, for_export=True)
    soup = BeautifulSoup(rendered, "html.parser")
    references = soup.select('[role="doc-noteref"]')
    asides = soup.select('[role="doc-endnotes"] [role="list"] > [role="listitem"]')
    assert len(asides) == 3
    assert not soup.select('[role="doc-endnotes"] [role="doc-footnote"]')

    assert [reference.get_text(strip=True) for reference in references] == [
        "[T1]",
        "[T2]",
        "[T3]",
    ]
    assert [aside.get("id") for aside in asides] == [
        "translator-note-note-hanshi",
        "translator-note-note-lantern",
        "translator-note-note-dao",
    ]
    assert references[0].get("href") == "#translator-note-note-hanshi"
    assert references[1].get("href") == "#translator-note-note-lantern"
    assert references[2].get("href") == "#translator-note-note-dao"
    assert references[0].get("id") == "translator-note-ref-note-hanshi"
    assert asides[0].find("a", role="doc-backlink").get("href") == (
        "#translator-note-ref-note-hanshi"
    )
    assert "Cultural &amp; historical context." in rendered
    assert soup.select_one("#first") is None
    first_paragraph = soup.find_all("p")[0]
    assert first_paragraph.get_text("", strip=True).startswith("Hanshi Festival[T1]")
    assert all("Translator’s note:" in aside.get_text() for aside in asides)
    assert [
        aside.select_one(".footnote-marker").get_text(strip=True) for aside in asides
    ] == ["T1.", "T2.", "T3."]


def test_translator_notes_require_the_current_segment_version_and_unique_container():
    segment = _segment("text", translated="Dao and li.", version=2)
    segment.translator_notes = [
        _note("stale", "Dao", "Stale note.", segment_version=1),
        _note("current", "li", "Current note.", segment_version=2),
    ]
    project = _project(
        '<article><section id="translator-notes-project-1"></section><p>{{text}}</p></article>',
        [segment],
        settings={"enable_translator_notes": True},
    )

    soup = BeautifulSoup(render_project_html(project), "html.parser")
    assert soup.select_one("#translator-note-stale") is None
    assert soup.select_one("#translator-note-current") is not None
    assert soup.select_one("#translator-notes-project-1-2") is not None


def test_disabled_translator_notes_are_omitted_without_changing_rendering():
    segment = _segment("text", translated="Hanshi Festival.")
    segment.translator_notes = [_note("note-1", "Hanshi Festival", "No export.")]
    with_notes = _project(
        "<article><p>{{text}}</p></article>",
        [segment],
        settings={"enable_translator_notes": False},
    )
    without_notes = _project(
        "<article><p>{{text}}</p></article>",
        [_segment("text", translated="Hanshi Festival.")],
        settings={},
    )

    assert render_project_html(with_notes) == render_project_html(without_notes)
    assert "translator-note" not in render_project_html(with_notes)
    assert "doc-noteref" not in render_project_html(with_notes)


def test_invalid_legacy_translator_note_is_omitted_with_content_free_warning():
    segment = _segment("text", translated="A different target.")
    segment.translator_notes = [
        _note("note-1", "Missing anchor", "Private note body that must not be logged.")
    ]
    project = _project(
        "<article><p>{{text}}</p></article>",
        [segment],
        settings={"enable_translator_notes": True},
    )

    logger = render_project_html.__globals__["logger"]
    with patch.object(logger, "warning") as warning:
        rendered = render_project_html(project)

    assert "translator-note" not in rendered
    warning.assert_called_once_with(
        "Translator note omitted during rendering for project %s.",
        project.id,
    )
    assert "Private note body" not in repr(warning.call_args)
