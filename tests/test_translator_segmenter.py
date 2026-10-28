from unittest.mock import patch

from bs4 import BeautifulSoup

from iarf_assistant.addons.translator.segmenter import DomSegmenter


class _FailingSentenceSegmenter:
    def split_text(self, text, *, raise_on_failure=False):
        assert raise_on_failure is True
        assert "<" not in text  # providers receive plain prose, not DOM fragments
        raise RuntimeError("docker unavailable")


class _OmittingSentenceSegmenter:
    def split_text(self, text, *, raise_on_failure=False):
        return ["Second sentence.", "First sentence."]


def test_sentence_segmenter_failure_uses_multilingual_fallback():
    paragraph = "Pierwsze zdanie pozostaje całe. Drugie zdanie także. Trzecie zdanie kończy akapit."
    with patch("iarf_assistant.addons.translator.segmenter.pysbd", None):
        assert DomSegmenter._split_into_sentences(
            paragraph,
            sentence_segmenter=_FailingSentenceSegmenter(),
            source_language="pl",
        ) == [
            "Pierwsze zdanie pozostaje całe.",
            "Drugie zdanie także.",
            "Trzecie zdanie kończy akapit.",
        ]
        assert DomSegmenter._split_into_sentences(
            "أولاً؟ ثانياً؟", source_language="ar"
        ) == ["أولاً؟", "ثانياً؟"]
        assert DomSegmenter._split_into_sentences(
            "第一文。第二文！", source_language="ja"
        ) == ["第一文。", "第二文！"]


def test_sentence_provider_omission_or_reordering_falls_back_without_loss():
    source = "First sentence. Second sentence."
    assert DomSegmenter._split_into_sentences(
        source,
        sentence_segmenter=_OmittingSentenceSegmenter(),
        source_language="en",
    ) == ["First sentence.", "Second sentence."]


def test_semantic_segmentation_preserves_comments_and_covers_plain_divs():
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        "<!-- keep me --><div>Translate me.</div><script>leave()</script>",
        "project-1",
        source_language="en",
    )
    assert len(segments) == 1
    assert segments[0].original_text == "Translate me."
    assert "<!-- keep me -->" in skeleton
    assert "<script>leave()</script>" in skeleton
    assert f"{{{{{segments[0].id}}}}}" in skeleton


def test_excluded_nodes_untouched_and_inline_markup_is_one_balanced_segment():
    source = "<p>Hello <b>bold. Still bold.</b> Bye.</p><pre>do not translate</pre>"
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        source, "project-1", source_language="en"
    )
    assert len(segments) == 1
    assert segments[0].original_text == "Hello <b>bold. Still bold.</b> Bye."
    assert "<pre>do not translate</pre>" in skeleton
    assert f"{{{{{segments[0].id}}}}}" in skeleton
    assert (
        BeautifulSoup(segments[0].original_text, "html.parser").find("b").get_text()
        == "bold. Still bold."
    )


def test_placeholder_segment_bijection_avoids_nested_duplicates():
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        "<section><p>One.</p><p>Two.</p></section>", "project-1", source_language="en"
    )
    assert len(segments) == 2
    assert sorted(segment.order_index for segment in segments) == [0, 1]
    assert sum(skeleton.count(f"{{{{{segment.id}}}}}") for segment in segments) == len(
        segments
    )


def test_multi_sentence_placeholders_are_not_resegmented_as_source_text():
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        "<p>Hello. World.</p>", "project-1", source_language="en"
    )

    assert [segment.original_text for segment in segments] == ["Hello.", "World."]
    assert all("{{" not in segment.original_text for segment in segments)
    assert all(skeleton.count(f"{{{{{segment.id}}}}}") == 1 for segment in segments)


def test_sentence_placeholders_preserve_exact_source_separators_on_reassembly():
    source = "<p>First.Second.  Third.\nFourth?</p>"
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        source, "project-1", source_language=None
    )

    rebuilt = skeleton
    for segment in segments:
        rebuilt = rebuilt.replace(f"{{{{{segment.id}}}}}", segment.original_text, 1)

    assert [segment.original_text for segment in segments] == [
        "First.",
        "Second.",
        "Third.",
        "Fourth?",
    ]
    assert rebuilt == source


def test_inline_breaks_and_semantic_inline_tags_survive_segmentation():
    source = (
        "<p>Hello<br/>World <kbd>Ctrl</kbd><wbr/>"
        "<ins>now</ins> <del>later</del> <samp>ok</samp>.</p>"
    )
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        source, "project-1", source_language="en"
    )

    assert len(segments) == 1
    parsed = BeautifulSoup(segments[0].original_text, "html.parser")
    assert [tag.name for tag in parsed.find_all(True)] == [
        "br",
        "kbd",
        "wbr",
        "ins",
        "del",
        "samp",
    ]
    assert parsed.get_text(" ", strip=True).replace(" .", ".") == (
        "Hello World Ctrl now later ok."
    )
    assert skeleton.count(f"{{{{{segments[0].id}}}}}") == 1


def test_nested_containers_and_excluded_inline_nodes_keep_structure_and_prose():
    source = "<div>Before <div>Inner text.</div> After<!-- note --><code>secret()</code> End.</div>"
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        source, "project-1", source_language="en"
    )
    originals = [segment.original_text for segment in segments]
    assert any("Inner text." in value for value in originals)
    assert any("Before" in value for value in originals)
    assert any("After" in value or "End." in value for value in originals)
    assert not any("secret()" in value for value in originals)
    assert "<!-- note -->" in skeleton
    assert "<code>secret()</code>" in skeleton
    assert skeleton.count("<div>") == 2


def test_custom_rules_preserve_comments_and_code_while_segmenting_surrounding_prose():
    skeleton, segments = DomSegmenter.apply_rules_and_segment(
        "<section><p>Hello<!--note--><code>x</code>world</p></section>",
        {"custom_paragraph": "section, p"},
        "project-1",
    )
    assert [segment.original_text for segment in segments] == ["Hello", "world"]
    assert all(segment.html_tag == "custom_paragraph" for segment in segments)
    assert all(segment.outer_tag == "p" for segment in segments)
    assert "<!--note-->" in skeleton
    assert "<code>x</code>" in skeleton
    assert not any("x" == segment.original_text for segment in segments)


def test_nested_custom_selection_keeps_parent_direct_prose_in_document_order():
    skeleton, segments = DomSegmenter.apply_rules_and_segment(
        "<section>Before <p>Nested.</p> After</section>",
        {"custom": "section, p"},
        "project-1",
    )
    assert [segment.original_text for segment in segments] == [
        "Before",
        "Nested.",
        "After",
    ]
    assert all(segment.html_tag == "custom" for segment in segments)
    assert BeautifulSoup(skeleton, "html.parser").find("section").find("p")


def test_nested_semantic_nodes_keep_their_outer_structure_and_direct_prose():
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        "<blockquote>Before <p>Nested prose.</p> After</blockquote>",
        "project-1",
        source_language="en",
    )
    assert any(segment.original_text == "Nested prose." for segment in segments)
    assert any(segment.original_text == "Before" for segment in segments)
    assert any(segment.original_text == "After" for segment in segments)
    soup = BeautifulSoup(skeleton, "html.parser")
    assert soup.find("blockquote").find("p") is not None


def test_direct_prose_placeholder_replacement_preserves_adjacent_whitespace():
    skeleton, _segments = DomSegmenter.apply_semantic_segmentation(
        "<p>Hello <b>world</b> after<!--note--><code>x</code> done.</p>",
        "project-1",
        source_language="en",
    )
    # The exact placeholders vary, but the whitespace that separated source
    # tokens must survive around inline placeholders and preserved nodes.
    assert "}} {{" in skeleton
    assert "<!--note--><code>x</code> " in skeleton


def test_body_and_document_level_prose_are_not_silently_ignored():
    body_skeleton, body_segments = DomSegmenter.apply_semantic_segmentation(
        "<body>Body prose.</body>", "project-1", source_language="en"
    )
    document_skeleton, document_segments = DomSegmenter.apply_semantic_segmentation(
        "Loose document prose.", "project-1", source_language="en"
    )
    assert [segment.original_text for segment in body_segments] == ["Body prose."]
    assert [segment.original_text for segment in document_segments] == [
        "Loose document prose."
    ]
    assert "{{" in body_skeleton and "{{" in document_skeleton
