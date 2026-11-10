"""Passage-paired exports must retain source and target without corrupting IDs."""

import pytest
from bs4 import BeautifulSoup

from iarf_assistant.addons.translator.bilingual_export import (
    BilingualExportError,
    compose_bilingual_html,
    validate_bilingual_layout,
)

SOURCE = (
    '<html lang="en"><body><main><h1 id="chapter">Hello</h1>'
    '<p id="one">This is <em>original</em> prose. <a href="#chapter">Back</a></p>'
    '<ol><li id="list-one">One item</li></ol>'
    '<table><tbody><tr><td id="cell">Table cell</td></tr></tbody></table>'
    "</main></body></html>"
)
TARGET = (
    '<html lang="pl"><body><main><h1 id="chapter">Witaj</h1>'
    '<p id="one">To jest <em>przetłumaczony</em> tekst. <a href="#chapter">Powrót</a></p>'
    '<ol><li id="list-one">Pierwszy punkt</li></ol>'
    '<table><tbody><tr><td id="cell">Komórka tabeli</td></tr></tbody></table>'
    "</main></body></html>"
)


@pytest.mark.parametrize("layout", ["parallel", "alternating"])
def test_pair_preserves_source_and_translation_ids_lists_and_tables(layout):
    result = compose_bilingual_html(
        SOURCE,
        TARGET,
        layout=layout,
        source_language="en",
        target_language="pl",
    )
    soup = BeautifulSoup(result, "html.parser")
    assert soup.find(string="Hello") is not None
    assert soup.find(string="Witaj") is not None
    assert soup.find("p", class_="carlina-bilingual-source-heading") is not None
    assert len(soup.find_all(id="chapter")) == 1
    assert len(soup.find_all(id="one")) == 1
    assert len(soup.find_all("ol")) == 1
    assert len(soup.find_all("li")) == 1
    assert len(soup.find_all("table")) == (2 if layout == "parallel" else 1)
    assert "This is" in result and "To jest" in result
    assert "Table cell" in soup.get_text() and "Komórka tabeli" in soup.get_text()
    assert 'lang="en"' in result and 'lang="pl"' in result


def test_rejects_unaligned_structure_instead_of_omitting_content():
    with pytest.raises(BilingualExportError, match="bilingual-structure-drift"):
        compose_bilingual_html(
            SOURCE,
            TARGET.replace('<p id="one">', '<div><p id="one">').replace(
                "</p><ol>", "</p></div><ol>"
            ),
            layout="parallel",
            source_language="en",
            target_language="pl",
        )


@pytest.mark.parametrize(
    "format,layout",
    [
        ("epub", "parallel"),
        ("docx", "alternating"),
        ("pdf", "alternating"),
        ("html", "invalid"),
    ],
)
def test_layout_rejects_unsupported_combinations(format, layout):
    with pytest.raises(BilingualExportError):
        validate_bilingual_layout(layout, format)


def test_source_language_rtl_is_marked_without_changing_target():
    result = compose_bilingual_html(
        SOURCE,
        TARGET,
        layout="alternating",
        source_language="ar",
        target_language="pl",
    )
    assert 'dir="rtl"' in result
    assert 'lang="pl"' in result


def _verified_export_request(*, layout: str, fmt: str):
    """Build through the real source-plan identity renderer and target boundary."""
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import patch

    from iarf_assistant.addons.translator.block_contract import (
        AttestedDocumentSnapshot,
        SourceProviderAttestation,
    )
    from iarf_assistant.addons.translator.block_export import (
        build_semantic_export_request,
    )
    from iarf_assistant.addons.translator.block_planner import plan_document_blocks

    sha = hashlib.sha256(SOURCE.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="document-1",
        document_id="document-1",
        output_html=SOURCE,
        content_hash=sha,
        semantic_content_hash=sha,
        metadata={"title": "Original book", "language": "en"},
        provenance={},
    )
    plan = plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_language="en",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="doc-processor",
            provider_capability_id="document.processing",
            owner_id="owner-1",
            resource_id="document-1",
        ),
    )
    record = SimpleNamespace(
        id="source-plan-1",
        plan_hash=plan.plan_hash,
        source_content_hash=sha,
        source_document_id="document-1",
        project=SimpleNamespace(target_lang="pl", source_lang="en"),
        project_id="project-1",
    )
    with (
        patch(
            "iarf_assistant.addons.translator.block_export.rehydrate_source_plan",
            return_value=plan,
        ),
        patch(
            "iarf_assistant.addons.translator.block_export.render_persisted_target_document",
            return_value=TARGET,
        ),
    ):
        return build_semantic_export_request(
            record, export_format=fmt, bilingual_layout=layout
        )


@pytest.mark.parametrize("layout,fmt", [("parallel", "html"), ("alternating", "epub")])
def test_real_export_boundary_verifies_both_languages_and_builds_file(layout, fmt):
    from types import SimpleNamespace

    from iarf_assistant.addons.doc_processor.semantic_export import (
        export_semantic_document,
    )

    request = _verified_export_request(layout=layout, fmt=fmt)
    assert request.metadata["bilingual-layout"] == layout
    assert request.metadata["source-language"] == "en"
    assert request.metadata["language"] == "pl"
    assert "Hello" in request.semantic_html and "Witaj" in request.semantic_html
    artifact = export_semantic_document(
        request.to_dict(), resolver=SimpleNamespace(resolve=lambda _: None)
    )
    assert artifact.receipt.format == fmt
    if fmt == "html":
        assert b"carlina-bilingual-pair" in artifact.data
        assert b"<style>" in artifact.data
    else:
        import io
        from zipfile import ZipFile

        with ZipFile(io.BytesIO(artifact.data)) as archive:
            chapters = b" ".join(
                archive.read(item)
                for item in archive.namelist()
                if item.endswith(".xhtml")
            )
        assert b"carlina-bilingual" in chapters
        assert b"Hello" in chapters and b"Witaj" in chapters
        from xml.etree import ElementTree as ET

        with ZipFile(io.BytesIO(artifact.data)) as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
        languages = [
            element.text
            for element in package.findall(
                ".//{http://purl.org/dc/elements/1.1/}language"
            )
        ]
        assert languages == ["pl", "en"]


def test_export_boundary_rejects_mismatched_layout_before_source_work():
    from iarf_assistant.addons.translator.block_export import BlockExportError

    with pytest.raises(BlockExportError, match="bilingual-layout-format"):
        _verified_export_request(layout="parallel", fmt="epub")


def test_existing_placeholder_class_does_not_steal_translation():
    source = SOURCE.replace(
        '<h1 id="chapter">',
        '<div class="carlina-bilingual-placeholder">Keep</div><h1 id="chapter">',
    )
    target = TARGET.replace(
        '<h1 id="chapter">',
        '<div class="carlina-bilingual-placeholder">Keep</div><h1 id="chapter">',
    )
    result = compose_bilingual_html(
        source, target, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(result, "html.parser")
    assert soup.get_text().count("Witaj") == 1
    assert soup.get_text().count("To jest") == 1
    assert soup.get_text().count("Keep") == 2


def test_source_noteref_is_inert_but_target_noteref_remains_active():
    original = (
        '<html lang="en"><body><main><h1 id="chapter">Chapter</h1>'
        '<p>Original<a class="footnote-ref" role="doc-noteref" epub:type="noteref" id="sref" href="#fn">[1]</a>.</p>'
        '<section role="doc-endnotes"><p id="fn">The note.</p></section>'
        "</main></body></html>"
    )
    translated = (
        '<html lang="pl"><body><main><h1 id="chapter">Rozdział</h1>'
        '<p>Przekład<a class="footnote-ref" role="doc-noteref" epub:type="noteref" id="sref" href="#fn">[1]</a>.</p>'
        '<section role="doc-endnotes"><p id="fn">Przypis.</p></section>'
        "</main></body></html>"
    )
    result = compose_bilingual_html(
        original,
        translated,
        layout="alternating",
        source_language="en",
        target_language="pl",
    )
    soup = BeautifulSoup(result, "html.parser")
    assert len(soup.select('[role="doc-noteref"]')) == 1
    assert len(soup.select(".footnote-ref")) == 1
    assert len(soup.select('[role="doc-endnotes"]')) == 1
    assert len(soup.find_all(id="fn")) == 1
    assert "The note." in soup.get_text() and "Przypis." in soup.get_text()
    assert len(soup.find_all("a", href="#fn")) == 1


@pytest.mark.parametrize("publishing_options", [False, True])
def test_three_chapters_split_to_separate_epub_content_files(publishing_options):
    import hashlib
    import io
    from types import SimpleNamespace
    from zipfile import ZipFile

    from carlina.core.semantic_export import SemanticExportRequest
    from iarf_assistant.addons.doc_processor.semantic_export import (
        export_semantic_document,
    )

    original = (
        '<html lang="en"><body><main>'
        + "".join(
            f'<h1 id="chapter-{n}">Original {n}</h1><p>Original body {n}</p>'
            for n in range(1, 4)
        )
        + "</main></body></html>"
    )
    translated = (
        '<html lang="pl"><body><main>'
        + "".join(
            f'<h1 id="chapter-{n}">Przekład {n}</h1><p>Przekład treści {n}</p>'
            for n in range(1, 4)
        )
        + "</main></body></html>"
    )
    content = compose_bilingual_html(
        original,
        translated,
        layout="alternating",
        source_language="en",
        target_language="pl",
    )
    from carlina.core.epub_export import normalize_epub_options

    request = SemanticExportRequest(
        semantic_html=content,
        semantic_html_sha256=hashlib.sha256(content.encode()).hexdigest(),
        metadata={"title": "Bilingual chapters", "language": "pl"},
        resources=(),
        format="epub",
        include_source_toc=False,
        profile_id="semantic-default",
        profile_version="1",
        epub_options=normalize_epub_options({"chapter_level": 1})
        if publishing_options
        else None,
    )
    if publishing_options:
        # Separately test the real EPUB packager's section partitioning.
        # The installed EPUBCheck 5.4 has a known navigation-label false
        # positive even on target-only books; never override production
        # validation or pretend this test validates conformance.
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "bilingual.epub"
            document = SimpleNamespace(
                output_html=content,
                output_markdown="",
                file_path=None,
                user_id=None,
                metadata_record=None,
                settings={"document_metadata": request.to_dict()["metadata"]},
            )
            EPUBPackager(
                document,
                str(path),
                include_source_toc=False,
                options=request.epub_options,
            ).build()
            epub = path.read_bytes()
    else:
        epub = export_semantic_document(
            request, resolver=SimpleNamespace(resolve=lambda _: None)
        ).data
    with ZipFile(io.BytesIO(epub)) as archive:
        content_files = [
            n
            for n in archive.namelist()
            if n.startswith("EPUB/content-") and n.endswith(".xhtml")
        ]
        assert len(content_files) == 3, content_files
        for number, name in enumerate(sorted(content_files), 1):
            text = BeautifulSoup(archive.read(name), "html.parser").get_text(
                " ", strip=True
            )
            assert f"Original {number}" in text and f"Przekład {number}" in text


def test_unpaired_empty_source_text_fails_closed():
    with pytest.raises(BilingualExportError, match="bilingual-unpaired-text"):
        compose_bilingual_html(
            "<main><span></span></main>",
            "<main><span>Hello</span></main>",
            layout="parallel",
            source_language="en",
            target_language="pl",
        )


def test_docx_parallel_table_contains_both_languages_without_nested_tables():
    import hashlib
    import io
    import subprocess
    import sys
    import zipfile
    from pathlib import Path
    from shutil import which
    from types import SimpleNamespace

    from carlina.core.semantic_export import SemanticExportRequest
    from iarf_assistant.addons.doc_processor.semantic_export import (
        export_semantic_document,
    )

    output = compose_bilingual_html(
        SOURCE, TARGET, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(output, "html.parser")
    original_table = soup.find("td", id="cell")
    assert original_table is not None
    assert original_table.find("table") is None
    request = SemanticExportRequest(
        semantic_html=output,
        semantic_html_sha256=hashlib.sha256(output.encode()).hexdigest(),
        metadata={"title": "Bilingual table", "language": "pl"},
        resources=(),
        format="docx",
        include_source_toc=False,
        profile_id="semantic-default",
        profile_version="1",
    )

    pandoc = which("pandoc") or str(Path(sys.executable).parent / "pandoc")
    if not Path(pandoc).is_file():
        pytest.skip("Pandoc is unavailable in this development environment")

    def runner(args, *, runtime_unit, input_text=None, timeout_seconds, check=False):
        assert runtime_unit == "document_processing"
        args = [pandoc if args[0] == "pandoc" else args[0], *args[1:]]
        return subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=check,
        )

    document = export_semantic_document(
        request,
        resolver=SimpleNamespace(resolve=lambda _: None),
        run_managed_process=runner,
    )
    with zipfile.ZipFile(io.BytesIO(document.data)) as archive:
        word = archive.read("word/document.xml").decode()
    assert "Table cell" in word
    assert "Komórka tabeli" in word
    assert "This is" in word
    assert "To jest" in word


def test_figure_keeps_single_accessible_figcaption_child():
    # A caption must stay a direct child of <figure> in XHTML/EPUB.
    # Exporting it as a nested table moves it out of its semantic position.
    source = (
        '<main><figure><img alt="A flower" '
        'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="/>'
        '<figcaption id="caption">Original photo caption</figcaption></figure></main>'
    )
    target = source.replace("Original photo caption", "Polski podpis")
    for layout in ("parallel", "alternating"):
        result = compose_bilingual_html(
            source, target, layout=layout, source_language="en", target_language="pl"
        )
        soup = BeautifulSoup(result, "html.parser")
        caption = soup.select_one("figure > figcaption#caption")
        assert caption is not None
        assert len(soup.find_all("figcaption")) == 1
        assert "Original photo caption" in caption.get_text()
        assert "Polski podpis" in caption.get_text()
        assert caption.select_one('[lang="en"]') is not None
        assert caption.select_one('[lang="pl"]') is not None


@pytest.mark.parametrize(
    "source,target",
    [
        (
            "<section><h1>Original heading</h1><p>Original paragraph</p></section>",
            "<html><body><section><h1>Polski tytuł</h1><p>Polski akapit</p></section></body></html>",
        ),
        (
            "<h1>Original heading</h1><p>Original paragraph</p>",
            "<html><body><h1>Polski tytuł</h1><p>Polski akapit</p></body></html>",
        ),
    ],
)
def test_valid_non_main_fragments_pair_against_persisted_html_shell(source, target):
    html = compose_bilingual_html(
        source, target, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert "Original paragraph" in soup.get_text()
    assert "Polski akapit" in soup.get_text()
    assert len(soup.select(".carlina-bilingual-source")) == 2


@pytest.mark.parametrize("which", ["source", "target"])
def test_semantic_safety_failures_remain_coded_errors(which):
    unsafe = SOURCE.replace("This is", "This is<!-- a hidden comment -->")
    source = unsafe if which == "source" else SOURCE
    target = unsafe.replace("Hello", "Witaj") if which == "target" else TARGET
    with pytest.raises(BilingualExportError, match=f"bilingual-{which}-invalid"):
        compose_bilingual_html(
            source,
            target,
            layout="parallel",
            source_language="en",
            target_language="pl",
        )


def test_inline_shared_images_are_not_duplicated_and_media_drift_is_rejected():
    image = '<img alt="A flower" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="/>'
    source = f"<main><p>Original {image} caption.</p></main>"
    target = f"<main><p>Przekład {image} podpis.</p></main>"
    html = compose_bilingual_html(
        source, target, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.find_all("img")) == 1
    assert "Original" in soup.get_text() and "Przekład" in soup.get_text()
    with pytest.raises(BilingualExportError, match="bilingual-media-drift"):
        compose_bilingual_html(
            source,
            target.replace("R0lGODlh", "R0lGODla"),
            layout="parallel",
            source_language="en",
            target_language="pl",
        )


def test_original_backlink_is_inert_but_translated_link_remains_live():
    source = '<main><p id="ref">Original</p><p>Note: <a role="doc-backlink" class="footnote-back" href="#ref">↩</a></p></main>'
    target = source.replace("Original", "Tłumaczenie")
    html = compose_bilingual_html(
        source, target, layout="alternating", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.select('a[href="#ref"]')) == 1
    assert len(soup.select('[role="doc-backlink"]')) == 1
    assert len(soup.select(".footnote-back")) == 1
    assert len(soup.find_all("span", string="↩")) == 1


def test_caption_with_paragraphs_does_not_create_invalid_span_wrappers():
    source = "<main><table><caption><p>Original long caption</p></caption><tbody><tr><td>One cell</td></tr></tbody></table></main>"
    target = source.replace("Original long caption", "Długi podpis").replace(
        "One cell", "Jedna komórka"
    )
    html = compose_bilingual_html(
        source, target, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.find_all("caption")) == 1
    assert len(soup.select("caption p")) == 2
    assert not soup.select("caption span p")


def test_parallel_pdf_wraps_visible_long_tokens_without_changing_references():
    from iarf_assistant.addons.doc_processor.print_export import (
        _soft_wrap_parallel_pdf_tokens,
    )

    token = "https://example.org/" + "verylongunbrokenpassage" * 10
    soup = BeautifulSoup(
        f'<main><p><a href="{token}">{token}</a></p><p title="{token}">Other</p></main>',
        "html.parser",
    )
    before = soup.get_text()
    _soft_wrap_parallel_pdf_tokens(soup)
    assert "\u200b" in soup.get_text()
    assert soup.get_text().replace("\u200b", "") == before
    assert soup.a["href"] == token
    assert soup.p.find_next_sibling("p")["title"] == token


def test_pdf_parallel_does_not_clip_long_unbroken_text():
    import hashlib
    import subprocess
    import sys
    from types import SimpleNamespace

    import pymupdf

    from carlina.core.semantic_export import SemanticExportRequest
    from iarf_assistant.addons.doc_processor.semantic_export import (
        export_semantic_document,
    )

    long_url = "https://example.org/" + "verylongunbrokenpassage" * 10
    source = f'<main><h1 id="heading">Original</h1><p>Left before {long_url} after.</p></main>'
    target = (
        f'<main><h1 id="heading">Próba</h1><p>Right before {long_url} after.</p></main>'
    )
    combined = compose_bilingual_html(
        source, target, layout="parallel", source_language="en", target_language="pl"
    )
    request = SemanticExportRequest(
        semantic_html=combined,
        semantic_html_sha256=hashlib.sha256(combined.encode()).hexdigest(),
        metadata={
            "title": "Próba",
            "language": "pl",
            "bilingual-layout": "parallel",
            "source-language": "en",
        },
        resources=(),
        format="pdf",
        include_source_toc=False,
        profile_id="semantic-default",
        profile_version="1",
    )

    def runner(args, *, runtime_unit, input_text=None, timeout_seconds, check=False):
        assert runtime_unit == "document_processing"
        assert args[0] == "python"
        return subprocess.run(
            [sys.executable, *args[1:]],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=check,
        )

    artifact = export_semantic_document(
        request,
        resolver=SimpleNamespace(resolve=lambda _: None),
        run_managed_process=runner,
    )
    with pymupdf.open(stream=artifact.data, filetype="pdf") as document:
        extracted = "".join(page.get_text() for page in document)
        assert "".join(extracted.split()).count(long_url) == 2
        assert all(
            word[2] <= page.rect.width + 0.5
            for page in document
            for word in page.get_text("words")
        )


def test_epub_preflight_hash_is_specific_to_bilingual_edition():
    from iarf_assistant.addons.translator.block_export import (
        BlockExportError,
        semantic_export_review_hash,
    )
    from iarf_assistant.addons.translator.worker import _verify_epub_review_hash

    monolingual = _verified_export_request(layout="none", fmt="epub").to_dict()
    bilingual = _verified_export_request(layout="alternating", fmt="epub").to_dict()
    monolingual_hash = semantic_export_review_hash(monolingual)
    bilingual_hash = semantic_export_review_hash(bilingual)
    assert monolingual_hash != bilingual_hash
    _verify_epub_review_hash(bilingual, bilingual_hash)
    with pytest.raises(BlockExportError, match="epub-review-stale"):
        _verify_epub_review_hash(bilingual, monolingual_hash)
