import unittest

from bs4 import BeautifulSoup

from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    ListContinuationError,
    concatenate_html_pages,
    has_source_toc_omission_marker,
    normalize_extracted_page_html,
)


class DocProcessorPdfUtilsTests(unittest.TestCase):
    def test_page_output_contract_adds_roots_sanitizes_active_content_and_preserves_literal_fences(
        self,
    ):
        output = normalize_extracted_page_html(
            "```html\n<p onclick='bad()'>Literal ``` marker.</p><script>bad()</script>\n```"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertIsNotNone(soup.find("main_content"))
        self.assertIsNotNone(soup.find("footnotes"))
        self.assertIsNone(soup.find("script"))
        self.assertFalse(soup.find("p").has_attr("onclick"))
        self.assertIn("Literal ``` marker.", output)

    def test_page_output_decodes_one_layer_of_double_escaped_text_entities(self):
        output = normalize_extracted_page_html(
            "<main_content><p>A &amp;ndash; B &amp;sect; 5 "
            "[Allesg&amp;ouml;tterei] &amp;lt;literal&amp;gt;</p></main_content>"
        )

        soup = BeautifulSoup(output, "html.parser")
        self.assertEqual(
            soup.find("p").get_text(),
            "A – B § 5 [Allesgötterei] <literal>",
        )
        self.assertIsNone(soup.find("literal"))

    def test_default_toc_cleanup_preserves_non_toc_content_on_mixed_page(self):
        output = normalize_extracted_page_html(
            "<main_content><p>Publication details.</p><h2 class='source-toc-title'>Contents</h2>"
            "<ol class='toc'><li>Chapter One 1</li><li>Chapter Two 9</li></ol>"
            "<p>Introductory note that must remain.</p></main_content>"
            "<footnotes><div class='footnote'>A retained note.</div></footnotes>"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertIsNone(soup.find(class_="toc"))
        self.assertNotIn("Contents", soup.get_text(" ", strip=True))
        self.assertIn("Publication details.", soup.get_text(" ", strip=True))
        self.assertIn(
            "Introductory note that must remain.", soup.get_text(" ", strip=True)
        )
        self.assertIn("A retained note.", soup.get_text(" ", strip=True))

    def test_toc_only_page_becomes_empty_required_roots(self):
        output = normalize_extracted_page_html(
            "<main_content><h2 class='source-toc-title'>Contents</h2><ol class='toc'><li>Chapter One</li></ol>"
            "</main_content><footnotes></footnotes>"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertEqual(soup.find("main_content").get_text(" ", strip=True), "")
        self.assertIsNotNone(soup.find("footnotes"))

    def test_empty_source_toc_marker_is_explicit_and_removed_before_storage(self):
        marked = (
            '<main_content><nav class="source-toc"></nav></main_content>'
            "<footnotes></footnotes>"
        )
        ordinary_empty = "<main_content></main_content><footnotes></footnotes>"
        transcribed = (
            '<main_content><nav class="source-toc">Chapter One 1</nav></main_content>'
            "<footnotes></footnotes>"
        )
        trailing = marked + "<p>Discarded outside the roots.</p>"
        extra_attribute = (
            '<main_content><nav class="source-toc" data-kind="toc"></nav></main_content>'
            "<footnotes></footnotes>"
        )
        mixed = (
            '<main_content><nav class="source-toc"></nav><p>Real text.</p></main_content>'
            "<footnotes></footnotes>"
        )

        self.assertTrue(has_source_toc_omission_marker(marked))
        self.assertFalse(has_source_toc_omission_marker(ordinary_empty))
        self.assertFalse(has_source_toc_omission_marker(transcribed))
        self.assertFalse(has_source_toc_omission_marker(trailing))
        self.assertFalse(has_source_toc_omission_marker(extra_attribute))
        self.assertFalse(has_source_toc_omission_marker(mixed))
        normalized = BeautifulSoup(
            normalize_extracted_page_html(marked), "html.parser"
        )
        self.assertIsNone(normalized.find("nav"))
        self.assertEqual(
            normalized.find("main_content").get_text(" ", strip=True), ""
        )

    def test_toc_cleanup_does_not_remove_ordinary_lists_or_unattached_headings(self):
        output = normalize_extracted_page_html(
            "<main_content><h2>Contents of the Archive</h2>"
            "<p>This section discusses archival contents.</p>"
            "<ol><li>Boxes</li><li>Letters</li></ol></main_content><footnotes></footnotes>"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertEqual(
            soup.find("h2").get_text(strip=True), "Contents of the Archive"
        )
        self.assertEqual(len(soup.find_all("li")), 2)

    def test_toc_cleanup_never_infers_heading_ownership_from_adjacency(self):
        output = normalize_extracted_page_html(
            "<main_content><h2 id='chapter-one'>Chapter One</h2>"
            "<nav class='source-toc'><div class='toc-entry toc-depth-1'>"
            "<span class='toc-entry-title'>Next chapter</span> "
            "<span class='toc-page-label'>9</span></div></nav>"
            "</main_content><footnotes></footnotes>"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertIsNone(soup.find("nav", class_="source-toc"))
        self.assertEqual(soup.find(id="chapter-one").get_text(strip=True), "Chapter One")

    def test_legacy_toc_cleanup_removes_only_a_generic_adjacent_title(self):
        output = normalize_extracted_page_html(
            "<main_content><h2>Table of Contents</h2>"
            "<ol class='toc'><li>Chapter One ... 1</li></ol>"
            "<h2 id='chapter-one'>Chapter One</h2>"
            "</main_content><footnotes></footnotes>"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertNotIn("Table of Contents", soup.get_text(" ", strip=True))
        self.assertEqual(soup.find(id="chapter-one").get_text(strip=True), "Chapter One")

    def test_legacy_toc_cleanup_keeps_an_arbitrary_adjacent_heading(self):
        output = normalize_extracted_page_html(
            "<main_content><h2 id='chapter-one'>Chapter One</h2>"
            "<ol class='toc'><li>Chapter Two ... 9</li></ol>"
            "</main_content><footnotes></footnotes>"
        )
        soup = BeautifulSoup(output, "html.parser")

        self.assertEqual(soup.find(id="chapter-one").get_text(strip=True), "Chapter One")

    def test_preserve_toc_mode_keeps_and_normalizes_toc(self):
        output = concatenate_html_pages(
            [
                "<main_content><h2>Contents</h2><ul class='toc'><li>Chapter One ... 1</li></ul>"
                "</main_content><footnotes></footnotes>"
            ],
            toc_handling="preserve",
        )
        soup = BeautifulSoup(output, "html.parser")

        toc = soup.find("nav", class_="source-toc")
        self.assertIsNotNone(toc)
        self.assertIsNone(soup.find(["ol", "ul"], class_="toc"))
        self.assertEqual(
            toc.find("span", class_="toc-entry-title").get_text(strip=True),
            "Chapter One",
        )
        self.assertEqual(
            toc.find("span", class_="toc-page-label").get_text(strip=True), "1"
        )
        self.assertIn("Chapter One", soup.get_text(" ", strip=True))

    def test_preserved_toc_keeps_explicit_hierarchy_and_printed_labels_without_list_markers(
        self,
    ):
        output = concatenate_html_pages(
            [
                "<main_content><h2>Contents</h2><ol class='toc'>"
                "<li><span class='toc-entry-title'>Part I</span> "
                "<span class='toc-page-label'>ix</span><ul>"
                "<li><span class='toc-entry-title'>Chapter 1</span> "
                "<span class='toc-page-label'>3</span></li></ul></li></ol>"
                "</main_content><footnotes></footnotes>"
            ],
            toc_handling="preserve",
        )
        soup = BeautifulSoup(output, "html.parser")

        entries = soup.select("nav.source-toc > div.toc-entry")
        self.assertEqual(len(entries), 2)
        self.assertIn("toc-depth-1", entries[0].get("class", []))
        self.assertIn("toc-depth-2", entries[1].get("class", []))
        self.assertEqual(
            [node.get_text(strip=True) for node in soup.select(".toc-page-label")],
            ["ix", "3"],
        )
        self.assertIsNone(soup.find(["ol", "ul"], class_="toc"))

    def test_source_toc_continuations_merge_without_duplicate_titles_or_losing_pages(
        self,
    ):
        output = concatenate_html_pages(
            [
                "<main_content><nav class='source-toc'>"
                "<h2 class='source-toc-title'>Contents</h2>"
                "<div class='toc-entry toc-depth-1'>"
                "<span class='toc-entry-title'>Chapter One</span>"
                "<span class='toc-page-label'>1</span></div></nav>"
                "</main_content><footnotes></footnotes>",
                "<main_content><nav class='source-toc continued'>"
                "<div class='toc-entry toc-depth-1'>"
                "<span class='toc-entry-title'>Chapter Two</span>"
                "<span class='toc-page-label'>9</span></div></nav>"
                "</main_content><footnotes></footnotes>",
                "<main_content><nav class='source-toc continued'>"
                "<h2 class='source-toc-title'>CONTENTS</h2>"
                "<div class='toc-entry toc-depth-1'>"
                "<span class='toc-entry-title'>Chapter Three</span>"
                "<span class='toc-page-label'>17</span></div></nav>"
                "</main_content><footnotes></footnotes>",
                "<main_content><nav class='source-toc continued'>"
                "<h2 class='source-toc-title'>Illustrations</h2>"
                "<div class='toc-entry toc-depth-1'>"
                "<span class='toc-entry-title'>Frontispiece</span>"
                "<span class='toc-page-label'>frontispiece</span></div></nav>"
                "</main_content><footnotes></footnotes>",
            ],
            boundary_flags={
                0: {"main_content": "merge"},
                1: {"main_content": "merge"},
                2: {"main_content": "merge"},
            },
            toc_handling="preserve",
        )
        soup = BeautifulSoup(output, "html.parser")

        toc_blocks = soup.select("nav.source-toc")
        self.assertEqual(len(toc_blocks), 2)
        self.assertEqual(
            [title.get_text(" ", strip=True) for title in soup.select(".source-toc-title")],
            ["Contents", "Illustrations"],
        )
        self.assertEqual(
            [
                entry.get_text(" ", strip=True)
                for entry in toc_blocks[0].select(":scope > .toc-entry")
            ],
            ["Chapter One 1", "Chapter Two 9", "Chapter Three 17"],
        )
        self.assertTrue(
            all(len(block.select(":scope > .source-toc-title")) <= 1 for block in toc_blocks)
        )
        self.assertEqual(
            [marker.get("id") for marker in soup.select("span.pagebreak")],
            ["doc-page-1", "doc-page-2", "doc-page-3", "doc-page-4"],
        )

    def test_unique_same_page_bare_superscript_is_repaired_as_a_footnote_reference(
        self,
    ):
        output = concatenate_html_pages(
            [
                "<main_content><p>Term<sup>1</sup>.</p></main_content>"
                "<footnotes><div class='footnote' id='f1'>The note.</div></footnotes>"
            ]
        )
        soup = BeautifulSoup(output, "html.parser")

        reference = soup.select_one("sup > a.footnote-ref")
        self.assertIsNotNone(reference)
        self.assertEqual(reference.get_text(strip=True), "1")
        self.assertEqual(reference.get("href"), f"#{soup.select_one('.footnote').get('id')}")

    def test_bare_superscript_is_not_linked_to_a_note_from_another_page(self):
        output = concatenate_html_pages(
            [
                "<main_content><p>x<sup>2</sup>.</p></main_content>"
                "<footnotes></footnotes>",
                "<main_content><p>Word<span class='footnote_marker'>2</span>.</p>"
                "</main_content><footnotes>"
                "<div class='footnote' id='f2'>The note.</div></footnotes>",
            ]
        )
        soup = BeautifulSoup(output, "html.parser")

        superscripts = soup.find_all("sup")
        self.assertEqual(len(superscripts), 2)
        self.assertIsNone(superscripts[0].find("a"))
        self.assertEqual(superscripts[0].get_text(strip=True), "2")
        self.assertIsNotNone(superscripts[1].find("a", class_="footnote-ref"))

    def test_complete_adjacent_blockquotes_do_not_auto_merge(self):
        output = concatenate_html_pages(
            [
                "<main_content><blockquote><p>Complete quote.</p></blockquote></main_content><footnotes></footnotes>",
                "<main_content><blockquote><p>Another complete quote.</p></blockquote></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        blockquotes = soup.find_all("blockquote")
        self.assertEqual(len(blockquotes), 2)
        self.assertEqual(blockquotes[0].get_text(" ", strip=True), "Complete quote.")
        self.assertEqual(
            blockquotes[1].get_text(" ", strip=True), "Another complete quote."
        )

    def test_continued_blockquote_container_merges(self):
        output = concatenate_html_pages(
            [
                "<main_content><blockquote><p>First quoted paragraph.</p></blockquote></main_content><footnotes></footnotes>",
                "<main_content><blockquote class='continued'><p>Second quoted paragraph.</p></blockquote></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        blockquotes = soup.find_all("blockquote")
        self.assertEqual(len(blockquotes), 1)
        self.assertEqual(
            [p.get_text(strip=True) for p in blockquotes[0].find_all("p")],
            ["First quoted paragraph.", "Second quoted paragraph."],
        )

    def test_continued_paragraph_is_pulled_into_previous_blockquote(self):
        output = concatenate_html_pages(
            [
                "<main_content><blockquote><p>The quoted sentence was</p></blockquote></main_content><footnotes></footnotes>",
                "<main_content><p class='continued'>continued on the next page.</p></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        blockquotes = soup.find_all("blockquote")
        self.assertEqual(len(blockquotes), 1)
        self.assertEqual(
            blockquotes[0].get_text(" ", strip=True),
            "The quoted sentence was continued on the next page.",
        )
        self.assertIsNone(soup.find("p", class_="continued"))

    def test_boundary_merge_preserves_new_children_inside_continued_containers(self):
        cases = [
            ("blockquote", "p", "First stanza ends.", "second stanza begins."),
            ("ul", "li", "apples", "bananas"),
            ("table", "tr", "<td>first row</td>", "<td>second row</td>"),
        ]
        for parent, child, left, right in cases:
            for decision in ("auto", "merge"):
                with self.subTest(parent=parent, decision=decision):
                    output = concatenate_html_pages(
                        [
                            f"<main_content><{parent}><{child}>{left}</{child}></{parent}></main_content>",
                            f"<main_content><{parent} class='continued'><{child}>{right}</{child}></{parent}></main_content>",
                        ],
                        boundary_flags={0: {"main_content": decision}},
                    )
                    soup = BeautifulSoup(output, "html.parser")
                    self.assertEqual(len(soup.find_all(parent)), 1)
                    self.assertEqual(len(soup.find_all(child)), 2)
                    self.assertEqual(len(soup.select("#doc-page-2")), 1)

    def test_explicit_ordered_list_start_survives_page_merge(self):
        output = concatenate_html_pages(
            [
                "<main_content><ol start='3'><li>Three</li><li>Four</li></ol></main_content>",
                (
                    "<main_content><ol start='7' class='continued'>"
                    "<li>Seven</li><li>Eight</li></ol></main_content>"
                ),
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        ordered_list = soup.find("ol")
        items = ordered_list.find_all("li", recursive=False)
        self.assertEqual(
            [item.get("value") for item in items], [None, None, "7", "8"]
        )
        self.assertIsNotNone(soup.find(id="doc-page-2"))

    def test_explicit_ordered_list_start_preserves_value_resets_and_following_ordinals(
        self,
    ):
        output = concatenate_html_pages(
            [
                "<main_content><ol><li>First</li></ol></main_content>",
                (
                    "<main_content><ol start='7' class='continued'>"
                    "<li>Seven</li><li value='010'>Ten</li><li>Eleven</li>"
                    "</ol></main_content>"
                ),
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        self.assertEqual(
            [item.get("value") for item in ordered_list.find_all("li", recursive=False)],
            [None, "7", "010", "11"],
        )

    def test_continued_ordered_list_item_keeps_the_following_ordinal(self):
        output = concatenate_html_pages(
            [
                "<main_content><ol start='3'><li>Three</li><li>Four</li></ol></main_content>",
                (
                    "<main_content><ol start='4' class='continued'>"
                    "<li class='continued-from-previous'> continued</li>"
                    "<li>Five</li></ol></main_content>"
                ),
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        items = ordered_list.find_all("li", recursive=False)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[1].get_text(" ", strip=True), "Four continued")
        self.assertIsNone(items[1].get("value"))
        self.assertEqual(items[2].get("value"), "5")

    def test_reversed_list_implicit_starts_are_frozen_using_direct_items(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol reversed><li>First<ul>"
                    "<li>Nested one</li><li>Nested two</li></ul></li>"
                    "<li>Second</li></ol></main_content>"
                ),
                (
                    "<main_content><ol reversed class='continued'>"
                    "<li>Third</li><li>Fourth</li></ol></main_content>"
                ),
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        ordered_list = soup.find("ol")
        direct_items = ordered_list.find_all("li", recursive=False)
        self.assertEqual(ordered_list.get("start"), "2")
        self.assertEqual(
            [item.get("value") for item in direct_items],
            [None, None, None, None],
        )
        self.assertEqual(
            [item.get("value") for item in ordered_list.find_all("ul")[0].find_all("li", recursive=False)],
            [None, None],
        )
        self.assertIsNotNone(soup.find(id="doc-page-2"))

    def test_reversed_incoming_list_without_start_continues_from_prior_ordinal(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol reversed start='100'>"
                    "<li>One hundred</li><li>Ninety-nine</li></ol></main_content>"
                ),
                (
                    "<main_content><ol reversed class='continued'>"
                    "<li>Ninety-eight</li><li>Ninety-seven</li></ol></main_content>"
                ),
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        items = ordered_list.find_all("li", recursive=False)
        self.assertEqual(ordered_list.get("start"), "100")
        self.assertEqual([item.get("value") for item in items], [None] * 4)

    def test_reversed_incoming_explicit_start_is_materialized(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol reversed start='100'><li>One hundred</li>"
                    "</ol></main_content>"
                ),
                (
                    "<main_content><ol reversed start='7' class='continued'>"
                    "<li>Seven</li><li>Six</li></ol></main_content>"
                ),
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        self.assertEqual(
            [
                item.get("value")
                for item in ordered_list.find_all("li", recursive=False)
            ],
            [None, "7", "6"],
        )

    def test_reversed_continued_split_inherits_previous_counter(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol reversed start='4'><li>Four</li>"
                    "<li>Three</li></ol></main_content>"
                ),
                (
                    "<main_content><ol reversed class='continued'>"
                    "<li class='continued-from-previous'> continues</li>"
                    "<li>Two</li></ol></main_content>"
                ),
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        items = ordered_list.find_all("li", recursive=False)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[1].get_text(" ", strip=True), "Three continues")
        self.assertIsNone(items[2].get("value"))

    def test_conflicting_explicit_split_ordinal_fails_without_source_details(self):
        with self.assertRaisesRegex(
            ValueError, "^list-continuation-ordinal-conflict$"
        ):
            concatenate_html_pages(
                [
                    "<main_content><ol start='3'><li>Three</li><li>Four</li></ol></main_content>",
                    (
                        "<main_content><ol class='continued'>"
                        "<li value='5' class='continued-from-previous'>continued</li>"
                        "<li>Next</li></ol></main_content>"
                    ),
                ]
            )

    def test_malformed_explicit_ordered_list_start_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError, "^list-continuation-ordinal-conflict$"
        ):
            concatenate_html_pages(
                [
                    "<main_content><ol><li>First</li></ol></main_content>",
                    (
                        "<main_content><ol start='not-a-number' class='continued'>"
                        "<li>Second</li></ol></main_content>"
                    ),
                ]
            )

    def test_malformed_item_value_is_not_rewritten_under_explicit_start(self):
        with self.assertRaisesRegex(
            ValueError, "^list-continuation-ordinal-conflict$"
        ):
            concatenate_html_pages(
                [
                    "<main_content><ol><li>First</li></ol></main_content>",
                    (
                        "<main_content><ol start='7' class='continued'>"
                        "<li>Seven</li><li value='not-a-number'>Unknown</li>"
                        "</ol></main_content>"
                    ),
                ]
            )

    def test_conflicting_ordered_list_styles_fail_closed(self):
        cases = [
            (
                "<ol type='a'><li>First</li></ol>",
                "<ol type='A' class='continued'><li>Second</li></ol>",
            ),
            (
                "<ol reversed><li>First</li></ol>",
                "<ol class='continued'><li>Second</li></ol>",
            ),
        ]
        for first, second in cases:
            with (
                self.subTest(first=first, second=second),
                self.assertRaisesRegex(
                    ValueError, "^list-continuation-style-conflict$"
                ),
            ):
                concatenate_html_pages(
                    [
                        f"<main_content>{first}</main_content>",
                        f"<main_content>{second}</main_content>",
                    ]
                )

    def test_incoming_ordered_list_without_explicit_start_keeps_continuation(self):
        output = concatenate_html_pages(
            [
                "<main_content><ol start='3'><li>Three</li><li>Four</li></ol></main_content>",
                "<main_content><ol class='continued'><li>Five</li></ol></main_content>",
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        items = ordered_list.find_all("li", recursive=False)
        self.assertEqual(ordered_list.get("start"), "3")
        self.assertEqual([item.get("value") for item in items], [None] * 3)

    def test_incoming_ordered_list_without_type_inherits_the_previous_type(self):
        output = concatenate_html_pages(
            [
                "<main_content><ol type='a'><li>First</li></ol></main_content>",
                "<main_content><ol class='continued'><li>Second</li></ol></main_content>",
            ]
        )

        ordered_list = BeautifulSoup(output, "html.parser").find("ol")
        self.assertEqual(ordered_list.get("type"), "a")

    def test_split_decimal_item_merges_explicitly_continued_nested_alpha_item(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol start='4'><li class='continues'>4. parent "
                    "<ol type='a' start='1' class='continues'><li class='continues'>a. first</li>"
                    "</ol></li></ol></main_content><footnotes></footnotes>"
                ),
                (
                    "<main_content><ol start='4' class='continued'>"
                    "<li class='continued-from-previous'>"
                    "<ol type='a' start='1' class='continued'>"
                    "<li class='continued-from-previous'>a. second</li></ol></li>"
                    "</ol></main_content><footnotes></footnotes>"
                ),
            ],
            page_numbers=(12, 13),
        )

        soup = BeautifulSoup(output, "html.parser")
        outer = soup.find("ol", start="4")
        nested = outer.find("ol", type="a")
        self.assertEqual(len(outer.find_all("li", recursive=False)), 1)
        self.assertEqual(len(outer.find("li").find_all("ol", recursive=False)), 1)
        self.assertEqual(len(nested.find_all("li", recursive=False)), 1)
        self.assertEqual(
            nested.find("li").get_text(" ", strip=True), "a. first a. second"
        )
        self.assertEqual(outer.find("li").get_text(" ", strip=True).count("4."), 1)
        self.assertEqual(outer.get("start"), "4")
        self.assertEqual(nested.get("start"), "1")

    def test_outer_item_continuation_does_not_merge_independent_nested_lists(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol><li class='continues'>parent "
                    "<ol type='a'><li value='1'>a. first source item</li></ol>"
                    "</li></ol></main_content><footnotes></footnotes>"
                ),
                (
                    "<main_content><ol class='continued'><li class='continued-from-previous'>"
                    "<ol type='a'><li value='1'>a. another source item</li></ol>"
                    "</li></ol></main_content><footnotes></footnotes>"
                ),
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        outer = soup.find("ol")
        outer_item = outer.find("li", recursive=False)
        nested_lists = outer_item.find_all("ol", recursive=False)
        self.assertEqual(len(outer.find_all("li", recursive=False)), 1)
        self.assertEqual(len(nested_lists), 2)
        self.assertEqual(
            [nested.find("li").get("value") for nested in nested_lists], ["1", "1"]
        )

    def test_nested_list_conflicts_include_physical_boundary_context(self):
        cases = [
            (
                "<ol type='a' class='continues'><li>First</li></ol>",
                "<ol type='A' class='continued'><li>Second</li></ol>",
                "list-continuation-style-conflict",
            ),
            (
                "<ol start='1'><li class='continues'>First</li></ol>",
                "<ol start='2' class='continued'><li class='continued-from-previous'>Second</li></ol>",
                "list-continuation-ordinal-conflict",
            ),
        ]
        for left, right, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(ListContinuationError) as raised:
                    concatenate_html_pages(
                        [
                            (
                                "<main_content><ol><li class='continues'>parent "
                                f"{left}</li></ol></main_content>"
                            ),
                            (
                                "<main_content><ol class='continued'>"
                                "<li class='continued-from-previous'>"
                                f"{right}</li></ol></main_content>"
                            ),
                        ],
                        page_numbers=(44, 45),
                    )

                error = raised.exception
                self.assertEqual(str(error), reason_code)
                self.assertEqual(error.reason_code, reason_code)
                self.assertEqual(error.previous_page, 44)
                self.assertEqual(error.page_number, 45)
                self.assertEqual(error.section, "main_content")

    def test_nonboundary_parent_text_keeps_nested_list_order(self):
        output = concatenate_html_pages(
            [
                (
                    "<main_content><ol><li class='continues'>first parent text "
                    "<ol type='a' class='continues'><li>first nested item</li></ol>"
                    "</li></ol></main_content>"
                ),
                (
                    "<main_content><ol class='continued'>"
                    "<li class='continued-from-previous'>second parent text "
                    "<ol type='a' class='continued'><li>second nested item</li></ol>"
                    "</li></ol></main_content>"
                ),
            ]
        )

        outer_item = BeautifulSoup(output, "html.parser").find("ol").find("li")
        content = outer_item.decode_contents()
        self.assertEqual(len(outer_item.find_all("ol", recursive=False)), 2)
        self.assertLess(
            content.index("first nested item"), content.index("second parent text")
        )
        self.assertLess(
            content.index("second parent text"), content.index("second nested item")
        )

    def test_physical_page_gap_does_not_join_lists_or_renumber_pagebreaks(self):
        output = concatenate_html_pages(
            [
                "<main_content><ol class='continues'><li>First</li></ol></main_content>",
                "<main_content><ol class='continued'><li>Second</li></ol></main_content>",
            ],
            page_numbers=(7, 9),
        )

        soup = BeautifulSoup(output, "html.parser")
        self.assertEqual(len(soup.find_all("ol")), 2)
        self.assertEqual(
            [marker.get("id") for marker in soup.select("span.pagebreak")],
            ["doc-page-7", "doc-page-9"],
        )

    def test_explicit_separate_vetoes_continuation_for_text_and_containers(self):
        cases = [
            ("<p>Library notice.</p>", "<p class='continued'>back flap fragment.</p>"),
            ("<blockquote><p>Quote.</p></blockquote>", "<p class='continued'>New paragraph.</p>"),
            ("<blockquote><p>Quote.</p></blockquote>", "<blockquote class='continued'><p>Other quote.</p></blockquote>"),
            ("<ul><li>First item</li></ul>", "<ul class='continued'><li>Other list</li></ul>"),
        ]
        for left, right in cases:
            with self.subTest(left=left, right=right):
                output = concatenate_html_pages(
                    [f"<main_content>{left}</main_content>", f"<main_content>{right}</main_content>"],
                    boundary_flags={0: {"main_content": "separate"}},
                )
                soup = BeautifulSoup(output, "html.parser")
                blocks = [tag for tag in soup.main.find_all(recursive=False) if tag.name != "span"]
                self.assertEqual(len(blocks), 2)
                self.assertIs(soup.find(id="doc-page-2").parent, soup.main)

    def test_backward_continuation_does_not_authorize_a_future_join(self):
        output = concatenate_html_pages(
            [
                "<main_content><p class='continued'>The old paragraph ends.</p></main_content>",
                "<main_content><p>A new paragraph begins.</p></main_content>",
            ]
        )
        self.assertEqual(len(BeautifulSoup(output, "html.parser").find_all("p")), 2)

    def test_directional_aliases_still_support_an_actual_split(self):
        for left_class, right_class in (("continues-next", ""), ("", "continued-from-previous")):
            with self.subTest(left_class=left_class, right_class=right_class):
                output = concatenate_html_pages(
                    [
                        f"<main_content><p class='{left_class}'>Split</p></main_content>",
                        f"<main_content><p class='{right_class}'>Capitalized ending.</p></main_content>",
                    ]
                )
                self.assertEqual(len(BeautifulSoup(output, "html.parser").find_all("p")), 1)

    def test_nested_marker_does_not_join_two_unmarked_quote_containers(self):
        output = concatenate_html_pages(
            [
                "<main_content><blockquote><p>A complete quote.</p></blockquote></main_content>",
                "<main_content><blockquote><p>New quote.</p><p class='continued'>A later fragment.</p></blockquote></main_content>",
            ]
        )
        self.assertEqual(len(BeautifulSoup(output, "html.parser").find_all("blockquote")), 2)

    def test_forward_marker_is_consumed_at_its_own_boundary(self):
        for opening in (
            "<p class='continues-next'>First sentence.</p>",
            "<blockquote class='continues-next'><p>First sentence.</p></blockquote>",
        ):
            with self.subTest(opening=opening):
                output = concatenate_html_pages(
                    [
                        f"<main_content>{opening}</main_content>",
                        "<main_content><p>Same paragraph.</p></main_content>",
                        "<main_content><p>New paragraph.</p></main_content>",
                    ]
                )
                self.assertEqual(
                    [p.get_text(" ", strip=True) for p in BeautifulSoup(output, "html.parser").find_all("p")],
                    ["First sentence. Same paragraph.", "New paragraph."],
                )

    def test_only_structural_evidence_vetoes_a_marked_continuation(self):
        for decision in ("auto", "merge"):
            for tag, class_name in (("h2", ""), ("p", "author")):
                with self.subTest(decision=decision, tag=tag):
                    output = concatenate_html_pages(
                        [
                            f"<main_content><{tag} class='{class_name}'>First</{tag}></main_content>",
                            f"<main_content><{tag} class='{class_name} continued'>Second</{tag}></main_content>",
                        ],
                        boundary_flags={0: {"main_content": decision}},
                    )
                    self.assertEqual(len(BeautifulSoup(output, "html.parser").find_all(tag)), 2)
        output = concatenate_html_pages(
            [
                "<main_content><p>First sentence.</p></main_content>",
                "<main_content><p class='continued'>Same paragraph, next sentence.</p></main_content>",
            ]
        )
        self.assertEqual(len(BeautifulSoup(output, "html.parser").find_all("p")), 1)

    def test_weak_punctuation_does_not_join_unmarked_capitalized_blocks(self):
        for ending in ("Introduction:", "Some words and", "A trailing comma,"):
            with self.subTest(ending=ending):
                output = concatenate_html_pages(
                    [
                        f"<main_content><p>{ending}</p></main_content>",
                        "<main_content><p>Independent opening.</p></main_content>",
                    ]
                )
                self.assertEqual(len(BeautifulSoup(output, "html.parser").find_all("p")), 2)

    def test_split_paragraph_inside_continued_blockquote_merges_text(self):
        output = concatenate_html_pages(
            [
                "<main_content><blockquote><p>The quoted sentence was</p></blockquote></main_content><footnotes></footnotes>",
                "<main_content><blockquote class='continued'><p class='continued'>continued on the next page.</p></blockquote></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        blockquotes = soup.find_all("blockquote")
        self.assertEqual(len(blockquotes), 1)
        self.assertEqual(len(blockquotes[0].find_all("p")), 1)
        self.assertEqual(
            blockquotes[0].get_text(" ", strip=True),
            "The quoted sentence was continued on the next page.",
        )

    def test_merged_paragraph_keeps_pagebreak_at_physical_boundary(self):
        output = concatenate_html_pages(
            [
                "<main_content><p>The sentence was</p></main_content><footnotes></footnotes>",
                "<main_content><p class='continued'>continued.</p></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        paragraphs = soup.find_all("p")
        pagebreak = soup.find(id="doc-page-2")
        self.assertEqual(len(paragraphs), 1)
        self.assertIsNotNone(pagebreak)
        self.assertIs(pagebreak.parent, paragraphs[0])
        self.assertEqual(
            paragraphs[0].get_text(" ", strip=True),
            "The sentence was continued.",
        )
        self.assertLess(
            paragraphs[0].decode_contents().index("The sentence was"),
            paragraphs[0].decode_contents().index('id="doc-page-2"'),
        )
        self.assertLess(
            paragraphs[0].decode_contents().index('id="doc-page-2"'),
            paragraphs[0].decode_contents().index("continued."),
        )

    def test_fully_merged_page_advances_boundary_for_next_continuation(self):
        output = concatenate_html_pages(
            [
                "<main_content><p>One</p></main_content><footnotes></footnotes>",
                "<main_content><p class='continued'>two</p></main_content><footnotes></footnotes>",
                "<main_content><p class='continued'>three.</p></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        paragraphs = soup.find_all("p")
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].get_text(" ", strip=True), "One two three.")
        self.assertIs(soup.find(id="doc-page-2").parent, paragraphs[0])
        self.assertIs(soup.find(id="doc-page-3").parent, paragraphs[0])

    def test_nonadjacent_selected_pages_never_merge_continuations(self):
        output = concatenate_html_pages(
            [
                "<main_content><p>Selected page one.</p></main_content><footnotes></footnotes>",
                "blank",
                "<main_content><p class='continued'>Selected page three fragment.</p></main_content><footnotes></footnotes>",
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        self.assertEqual(
            [paragraph.get_text(" ", strip=True) for paragraph in soup.find_all("p")],
            ["Selected page one.", "Selected page three fragment."],
        )
        self.assertIsNotNone(soup.find(id="doc-page-1"))
        self.assertIsNotNone(soup.find(id="doc-page-3"))

    def test_restarted_footnote_ids_without_distinct_scope_stay_unlinked(self):
        output = concatenate_html_pages(
            [
                """
                <main_content>
                    <p>Chapter one marker <span class="footnote_marker" id="fm1">1</span>.</p>
                    <p>Chapter one marker <span class="footnote_marker" id="fm2">2</span>.</p>
                    <p>Chapter two marker <span class="footnote_marker" id="fm1">1</span>.</p>
                    <p>Chapter two marker <span class="footnote_marker" id="fm2">2</span>.</p>
                </main_content>
                <footnotes>
                    <div class="footnote" id="f1">First chapter note.</div>
                    <div class="footnote" id="f2">Second chapter note.</div>
                    <div class="footnote" id="f1">Restarted first note.</div>
                    <div class="footnote" id="f2">Restarted second note.</div>
                </footnotes>
                """
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        refs = soup.find_all("a", class_="footnote-ref")
        footnotes = soup.find_all(class_="footnote")
        footnote_ids = [footnote.get("id") for footnote in footnotes]

        # Repeated IDs on the same page do not establish which note is meant.
        # Preserve every body and visible marker instead of guessing by order.
        self.assertEqual(refs, [])
        self.assertEqual(
            [ref.get_text(strip=True) for ref in soup.find_all("sup")],
            ["1", "2", "1", "2"],
        )
        self.assertEqual(len(set(footnote_ids)), 4)
        self.assertEqual(len(footnotes), 4)
        for text in (
            "First chapter note.", "Second chapter note.",
            "Restarted first note.", "Restarted second note.",
        ):
            self.assertIn(text, soup.get_text())
        self.assertNotIn("<h2>Notes</h2>", output)

    def test_marker_id_is_used_when_visible_label_is_ambiguous(self):
        output = concatenate_html_pages(
            [
                """
                <main_content>
                    <p>First marker should target note 2 <span class="footnote_marker" id="fm2">*</span>.</p>
                    <p>Second marker should target note 1 <span class="footnote_marker" id="fm1">*</span>.</p>
                </main_content>
                <footnotes>
                    <div class="footnote" id="f1">First note.</div>
                    <div class="footnote" id="f2">Second note.</div>
                </footnotes>
                """
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        refs = soup.find_all("a", class_="footnote-ref")
        self.assertEqual(len(refs), 2)

        resolved_texts = []
        for ref in refs:
            target_id = (ref.get("href") or "").lstrip("#")
            target_note = soup.find(id=target_id)
            self.assertIsNotNone(target_note)
            resolved_texts.append(target_note.get_text(" ", strip=True))

        self.assertIn("Second note.", resolved_texts[0])
        self.assertIn("First note.", resolved_texts[1])

    def test_repeated_reference_reuses_same_footnote(self):
        output = concatenate_html_pages(
            [
                '<main_content><p>First <span class="footnote_marker" id="fm1">1</span> '
                'again <span class="footnote_marker" id="fm1">1</span> next '
                '<span class="footnote_marker" id="fm2">2</span>.</p></main_content>'
                '<footnotes><div class="footnote" id="f1">Note one.</div>'
                '<div class="footnote" id="f2">Note two.</div></footnotes>'
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        refs = soup.find_all("a", class_="footnote-ref")
        self.assertEqual(refs[0]["href"], refs[1]["href"])
        self.assertNotEqual(refs[1]["href"], refs[2]["href"])

    def test_repeated_reference_does_not_jump_to_next_chapters_restarted_note(self):
        output = concatenate_html_pages(
            [
                "<main_content><h2>First chapter</h2><p>First "
                '<span class="footnote_marker" id="fm1">1</span>, another '
                '<span class="footnote_marker" id="fm2">2</span>, and repeated '
                '<span class="footnote_marker" id="fm1">1</span>.</p></main_content>'
                '<footnotes><div class="footnote" id="f1">First chapter note one.</div>'
                '<div class="footnote" id="f2">First chapter note two.</div></footnotes>',
                "<main_content><h2>Second chapter</h2><p>Restarted "
                '<span class="footnote_marker" id="fm1">1</span>.</p></main_content>'
                '<footnotes><div class="footnote" id="f1">Second chapter note one.</div></footnotes>',
            ]
        )

        soup = BeautifulSoup(output, "html.parser")
        refs = soup.find_all("a", class_="footnote-ref")
        targets = [soup.find(id=ref["href"].removeprefix("#")) for ref in refs]

        self.assertEqual(len(refs), 4)
        self.assertEqual(refs[0]["href"], refs[2]["href"])
        self.assertNotEqual(refs[2]["href"], refs[3]["href"])
        self.assertIn("First chapter note one.", targets[0].get_text(" ", strip=True))
        self.assertIn("First chapter note one.", targets[2].get_text(" ", strip=True))
        self.assertIn("Second chapter note one.", targets[3].get_text(" ", strip=True))
        self.assertNotIn("data-docproc-source-page", output)

    def test_adjacent_complete_lists_and_tables_do_not_merge_without_continuation_signal(
        self,
    ):
        table_output = concatenate_html_pages(
            [
                "<main_content><table><tr><td>A</td></tr></table></main_content><footnotes></footnotes>",
                "<main_content><table><tr><td>B</td></tr></table></main_content><footnotes></footnotes>",
            ]
        )
        list_output = concatenate_html_pages(
            [
                "<main_content><ul><li>First list.</li></ul></main_content><footnotes></footnotes>",
                "<main_content><ul><li>Second list.</li></ul></main_content><footnotes></footnotes>",
            ]
        )

        self.assertEqual(
            len(BeautifulSoup(table_output, "html.parser").find_all("table")), 2
        )
        self.assertEqual(
            len(BeautifulSoup(list_output, "html.parser").find_all("ul")), 2
        )


if __name__ == "__main__":
    unittest.main()
