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
        f'<main><h1>{token}</h1><table class="carlina-bilingual-pair" role="presentation">'
        f'<tr><td><p><a href="{token}">{token}</a></p></td></tr></table>'
        f'<p title="{token}">Other</p></main>',
        "html.parser",
    )
    before = soup.get_text()
    heading_before = soup.h1.get_text()
    _soft_wrap_parallel_pdf_tokens(soup)
    assert "\u200b" in soup.select_one("table.carlina-bilingual-pair").get_text()
    assert soup.get_text().replace("\u200b", "") == before
    assert soup.h1.get_text() == heading_before
    assert soup.a["href"] == token
    assert soup.select_one("main > p")["title"] == token


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


def test_publishing_epub_preserves_cross_chapter_notes_images_and_bidirectional_links():
    """Real packager, not a mocked emitter: check navigation after XHTML splitting."""
    import base64
    import io
    import posixpath
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from urllib.parse import unquote, urlsplit
    from xml.etree import ElementTree as ET
    from zipfile import ZipFile

    from PIL import Image

    from carlina.core.epub_export import normalize_epub_options
    from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
    from iarf_assistant.addons.doc_processor.epub_validation import (
        validate_epub_structure,
    )

    binary = io.BytesIO()
    Image.new("RGB", (8, 8), (80, 110, 140)).save(binary, format="PNG")
    image = base64.b64encode(binary.getvalue()).decode("ascii")
    source = (
        '<html lang="en"><body><main>'
        '<h1 id="first">ORIGINAL_first_chapter</h1>'
        '<p>ORIGINAL_opening <a id="ref1" role="doc-noteref" epub:type="noteref" href="#note1">1</a> ORIGINAL_tail.</p>'
        f'<figure><img src="data:image/png;base64,{image}" alt="ORIGINAL_image"/>'
        "<figcaption>ORIGINAL_caption</figcaption></figure>"
        '<table><caption>ORIGINAL_table</caption><thead><tr><th id="column">ORIGINAL_header</th></tr></thead>'
        '<tbody><tr><td headers="column">ORIGINAL_cell</td></tr></tbody></table>'
        '<h1 id="second">ORIGINAL_second_chapter</h1>'
        '<p>ORIGINAL_middle <a id="ref2" role="doc-noteref" epub:type="noteref" href="#note2">2</a> ORIGINAL_end.</p>'
        '</main><section id="notes" class="footnotes" role="doc-endnotes"><ol>'
        '<li id="note1" role="doc-footnote"><p>ORIGINAL_note_one '
        '<a role="doc-backlink" href="#ref1">↩</a></p></li>'
        '<li id="note2" role="doc-footnote"><p>ORIGINAL_note_two '
        '<a role="doc-backlink" href="#ref2">↩</a></p></li>'
        "</ol></section></body></html>"
    )
    target = source.replace("ORIGINAL_", "TRANSLATED_").replace(
        'lang="en"', 'lang="pl"'
    )
    bilingual = compose_bilingual_html(
        source, target, layout="alternating", source_language="en", target_language="pl"
    )
    metadata = {
        "title": "Bilingual notes and figures",
        "language": "pl",
        "source-language": "en",
        "bilingual-layout": "alternating",
    }
    with TemporaryDirectory() as directory:
        path = Path(directory) / "test.epub"
        document = SimpleNamespace(
            output_html=bilingual,
            output_markdown="",
            file_path=None,
            user_id=None,
            metadata_record=None,
            settings={"document_metadata": metadata},
        )
        EPUBPackager(
            document,
            str(path),
            include_source_toc=False,
            options=normalize_epub_options({"chapter_level": 1}),
        ).build()
        valid, diagnostic = validate_epub_structure(str(path))
        assert valid, diagnostic
        with ZipFile(path) as archive:
            xml_documents = {
                name: BeautifulSoup(archive.read(name), "xml")
                for name in archive.namelist()
                if name.startswith("EPUB/") and name.endswith(".xhtml")
            }
            package = ET.fromstring(archive.read("EPUB/package.opf"))
        languages = [
            item.text
            for item in package.findall(".//{http://purl.org/dc/elements/1.1/}language")
        ]
        assert languages == ["pl", "en"]
        content = {
            path: soup for path, soup in xml_documents.items() if "/content-" in path
        }
        assert len(content) >= 2
        text = " ".join(soup.get_text(" ", strip=True) for soup in content.values())
        for witness in (
            "ORIGINAL_first_chapter",
            "TRANSLATED_first_chapter",
            "ORIGINAL_second_chapter",
            "TRANSLATED_second_chapter",
            "ORIGINAL_caption",
            "TRANSLATED_caption",
            "ORIGINAL_table",
            "TRANSLATED_table",
            "ORIGINAL_note_one",
            "TRANSLATED_note_one",
            "ORIGINAL_note_two",
            "TRANSLATED_note_two",
        ):
            assert witness in text, witness
        assert sum(len(soup.find_all("img")) for soup in content.values()) == 1
        assert (
            sum(len(soup.select('[role="doc-noteref"]')) for soup in content.values())
            == 2
        )
        assert (
            sum(len(soup.select('[role="doc-backlink"]')) for soup in content.values())
            == 2
        )
        identities = {
            name: {tag.get("id") for tag in soup.find_all(id=True)}
            for name, soup in xml_documents.items()
        }
        for name, soup in xml_documents.items():
            for link in soup.find_all("a", href=True):
                url = urlsplit(str(link["href"]))
                if url.scheme or url.netloc or not url.fragment:
                    continue
                other = (
                    posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), unquote(url.path))
                    )
                    if url.path
                    else name
                )
                assert other in identities, (name, link["href"])
                assert unquote(url.fragment) in identities[other], (name, link["href"])


def test_actual_preflight_reviews_paired_content_and_rejects_monolingual_witness():
    from carlina.core.epub_export import normalize_epub_options
    from iarf_assistant.addons.doc_processor.epub_quality import preflight_epub
    from iarf_assistant.addons.translator.block_export import (
        semantic_export_review_hash,
        validate_epub_preflight_result,
    )

    options = normalize_epub_options({"chapter_level": 1})
    mono = _verified_export_request(layout="none", fmt="epub")
    bilingual = _verified_export_request(layout="alternating", fmt="epub")
    result = preflight_epub(bilingual.semantic_html, bilingual.metadata, options)
    review_hash = semantic_export_review_hash(bilingual.to_dict())
    assert review_hash != semantic_export_review_hash(mono.to_dict())
    result["review_hash"] = review_hash
    checked = validate_epub_preflight_result(
        result,
        semantic_html_sha256=bilingual.semantic_html_sha256,
        epub_options=options,
        expected_review_hash=review_hash,
    )
    assert checked["review_hash"] == review_hash
    assert checked["quality"]["input_sha256"] == bilingual.semantic_html_sha256
    assert checked["outline"]["input_sha256"] == bilingual.semantic_html_sha256


def test_parallel_docx_marks_arabic_translation_as_bidi():
    """Pandoc must preserve paragraph and run direction, not just visible Arabic."""
    import hashlib
    import io
    import subprocess
    import sys
    from pathlib import Path
    from shutil import which
    from types import SimpleNamespace
    from xml.etree import ElementTree as ET
    from zipfile import ZipFile

    from carlina.core.semantic_export import SemanticExportRequest
    from iarf_assistant.addons.doc_processor.semantic_export import (
        export_semantic_document,
    )

    pandoc = which("pandoc") or str(Path(sys.executable).parent / "pandoc")
    if not Path(pandoc).is_file():
        pytest.skip("Pandoc is unavailable in this development environment")
    original = "<main><h1>Original chapter</h1><p>Original prose in English.</p></main>"
    translated = (
        "<main><h1>عنوان الفصل</h1>"
        "<p>هذه جملة عربية لاختبار اتجاه الفقرات عند التصدير إلى مستند وورد.</p></main>"
    )
    output = compose_bilingual_html(
        original,
        translated,
        layout="parallel",
        source_language="en",
        target_language="ar",
    )
    request = SemanticExportRequest(
        semantic_html=output,
        semantic_html_sha256=hashlib.sha256(output.encode()).hexdigest(),
        metadata={
            "title": "Arabic bilingual edition",
            "language": "ar",
            "bilingual-layout": "parallel",
            "source-language": "en",
        },
        resources=(),
        format="docx",
        include_source_toc=False,
        profile_id="semantic-default",
        profile_version="1",
    )

    def runner(args, *, runtime_unit, input_text=None, timeout_seconds, check=False):
        assert runtime_unit == "document_processing"
        return subprocess.run(
            [pandoc if args[0] == "pandoc" else args[0], *args[1:]],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=check,
        )

    artifact = export_semantic_document(
        request,
        resolver=SimpleNamespace(resolve=lambda _: None),
        run_managed_process=runner,
    )
    with ZipFile(io.BytesIO(artifact.data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    matches = [
        p
        for p in root.findall(f".//{w}p")
        if "هذه جملة" in "".join(t.text or "" for t in p.findall(f".//{w}t"))
    ]
    assert len(matches) == 1
    paragraph = matches[0]
    assert paragraph.find(f"{w}pPr/{w}bidi") is not None
    assert paragraph.find(f".//{w}rtl") is not None
    assert any(t.get(f"{w}val") == "ar" for t in paragraph.findall(f".//{w}lang"))
    headings = [
        p
        for p in root.findall(f".//{w}p")
        if "عنوان الفصل" in "".join(t.text or "" for t in p.findall(f".//{w}t"))
    ]
    assert len(headings) == 1
    heading = headings[0]
    assert heading.find(f"{w}pPr/{w}bidi") is not None
    assert heading.find(f".//{w}rtl") is not None
    assert any(t.get(f"{w}val") == "ar" for t in heading.findall(f".//{w}lang"))


def test_parallel_html_layout_tables_are_presentational():
    html = compose_bilingual_html(
        SOURCE, TARGET, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(html, "html.parser")
    pairs = soup.select("table.carlina-bilingual-pair")
    assert pairs
    assert all(pair.get("role") == "presentation" for pair in pairs)
    assert all(len(pair.select(":scope > tbody > tr > td")) == 2 for pair in pairs)


def test_source_image_only_link_is_removed_and_target_remains_functional():
    image = '<img alt="Photo" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="/>'
    source = f'<main><p id="dest">Source</p><p><a href="#dest">{image}</a> caption</p></main>'
    target = source.replace("Source", "Target").replace("caption", "podpis")
    html = compose_bilingual_html(
        source, target, layout="parallel", source_language="en", target_language="pl"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.select('a[href="#dest"]')) == 1
    assert len(soup.find_all("img")) == 1
    assert not soup.select("a:empty")


@pytest.mark.parametrize("layout", ["parallel", "alternating"])
def test_real_target_renderer_adds_translator_notes_without_bilingual_drift(layout):
    """Exercise plan, compact target reconstruction, inserted notes, and pairing."""
    import hashlib

    from iarf_assistant.addons.translator.block_contract import (
        AttestedDocumentSnapshot,
        SourceProviderAttestation,
    )
    from iarf_assistant.addons.translator.block_planner import plan_document_blocks
    from iarf_assistant.addons.translator.compact_markup import (
        compact_source_block,
        normalize_compact_result,
    )
    from iarf_assistant.addons.translator.target_rendering import (
        render_target_document,
        validate_target_semantic_document,
    )

    original = (
        '<html lang="en"><body><p>Source wording'
        '<a id="source-ref" role="doc-noteref" epub:type="noteref" href="#source-note">1</a>.</p>'
        '<aside id="source-note" role="doc-footnote" epub:type="footnote">'
        '<p>Source endnote body. <a role="doc-backlink" href="#source-ref">↩</a></p>'
        "</aside></body></html>"
    )
    digest = hashlib.sha256(original.encode()).hexdigest()
    plan = plan_document_blocks(
        AttestedDocumentSnapshot(
            id="document-1",
            document_id="document-1",
            output_html=original,
            content_hash=digest,
            semantic_content_hash=digest,
            metadata={"language": "en"},
            provenance={},
        ),
        project_id="project-1",
        source_language="en",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="doc-processor",
            provider_capability_id="document.processing",
            owner_id="owner-1",
            resource_id="document-1",
        ),
    )
    candidates = {}
    for index, block in enumerate(plan.blocks, 1):
        wire = compact_source_block(block, number=index)
        candidates[block.id] = normalize_compact_result(
            plan=plan,
            plan_id="plan-1",
            block=block,
            expected_number=index,
            value={key: wire[key] for key in ("n", "content", "fields")},
        )
    rendered_target = render_target_document(
        plan,
        "plan-1",
        candidates,
        target_language="pl",
        notes={
            plan.blocks[0].id: [
                {
                    "anchor_text": "Source wording",
                    "body": "Translator explanatory note.",
                }
            ]
        },
    )
    assert validate_target_semantic_document(rendered_target).valid
    result = compose_bilingual_html(
        original,
        rendered_target,
        layout=layout,
        source_language="en",
        target_language="pl",
    )
    soup = BeautifulSoup(result, "html.parser")
    assert soup.get_text().count("Source endnote body.") == 2
    assert soup.get_text().count("Translator explanatory note.") == 1
    assert len(soup.select('[role="doc-noteref"]')) == 2
    assert len(soup.select('[role="doc-backlink"]')) == 2
    assert len(soup.select('[role="doc-endnotes"]')) == 1
    identifiers = [tag["id"] for tag in soup.find_all(id=True)]
    assert len(identifiers) == len(set(identifiers))
