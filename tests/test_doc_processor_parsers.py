import builtins
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from PIL import Image

from iarf_assistant.addons.doc_processor.parsers import (
    get_parser,
    process_file_headlessly,
)
from iarf_assistant.addons.doc_processor.parsers.html_epub_parser import HtmlEpubParser
from iarf_assistant.addons.doc_processor.parsers.txt_parser import TxtParser


def _import_blocking_fitz(name, globals=None, locals=None, fromlist=(), level=0):
    if name in {"fitz", "pymupdf"}:
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)
    return _import_blocking_fitz.original(name, globals, locals, fromlist, level)


_import_blocking_fitz.original = builtins.__import__


def test_process_file_headlessly_epub_does_not_require_fitz():
    app = Flask(__name__)
    app.secret_key = "test"

    with app.app_context():
        with patch.object(
            HtmlEpubParser, "process", return_value=("COMPLETED", "<p>Hello EPUB</p>")
        ):
            with patch("builtins.__import__", side_effect=_import_blocking_fitz):
                result = process_file_headlessly(
                    "book.epub", user_id=1, llm_service=None
                )

    assert not result.startswith("Error:")
    assert "Hello EPUB" in result


def test_get_parser_pdf_without_fitz_raises_clear_runtime_error():
    import sys

    # Clear cached modules to force import attempts to go through __import__ patch
    pdf_parser_key = "iarf_assistant.addons.doc_processor.parsers.pdf_parser"
    old_pdf_parser = sys.modules.pop(pdf_parser_key, None)
    old_fitz = sys.modules.pop("fitz", None)
    try:
        with patch("builtins.__import__", side_effect=_import_blocking_fitz):
            with pytest.raises(RuntimeError, match="PyMuPDF"):
                get_parser("book.pdf", user_id=1, llm_service=None)
    finally:
        if old_pdf_parser is not None:
            sys.modules[pdf_parser_key] = old_pdf_parser
        if old_fitz is not None:
            sys.modules["fitz"] = old_fitz


def test_process_file_headlessly_uses_shared_html_to_markdown_converter():
    app = Flask(__name__)
    app.secret_key = "test"

    with app.app_context():
        with patch.object(
            HtmlEpubParser, "process", return_value=("COMPLETED", "<h1>Hi</h1>")
        ):
            with patch(
                "iarf_assistant.addons.doc_processor.format_utils.html_to_markdown_on_the_fly",
                return_value="# Hi",
            ) as converter:
                result = process_file_headlessly(
                    "book.epub", user_id=1, llm_service=None
                )

    converter.assert_called_once_with("<h1>Hi</h1>")
    assert result == "# Hi"


def test_get_parser_supports_rtf_extension():
    parser = get_parser("document.rtf", user_id=1, llm_service=None)
    assert isinstance(parser, HtmlEpubParser)


def test_get_parser_resolves_stored_upload_web_path(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path))

    with app.app_context():
        parser = get_parser("/uploads/1/document.txt", user_id=1, llm_service=None)

    assert (
        Path(parser.file_path)
        == (tmp_path / "uploads" / "1" / "document.txt").resolve()
    )


def test_txt_parser_records_actual_structuring_model(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("A heading\nA paragraph", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="<h1>A heading</h1><p>A paragraph</p>")
            )
        ],
    )
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"parsing_model": "lmstudio/example-model"},
    )

    with patch.object(parser, "_get_setting", return_value="{numbered_lines}"):
        status, _html = parser.process()

    assert status == "COMPLETED"
    assert parser.settings["models_used"] == {"structuring": ["lmstudio/example-model"]}


def test_txt_parser_passes_immutable_reasoning_snapshot(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("A heading\nA paragraph", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="<h1>A heading</h1><p>A paragraph</p>"),
                finish_reason="stop",
            )
        ],
    )
    snapshot = {
        "version": 1,
        "slots": {
            slot: {
                "model_id": "provider/model",
                "provider": "provider",
                "level": "high",
                "supported_levels": ["high"],
                "source": "book_quality_default",
            }
            for slot in ("parsing", "metadata")
        },
    }
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={
            "parsing_model": "provider/model",
            "reasoning_snapshot": snapshot,
        },
    )

    with patch.object(parser, "_get_setting", return_value="{numbered_lines}"):
        status, _html = parser.process()

    assert status == "COMPLETED"
    assert llm_service.completion.call_args.kwargs["thinking_config"] == {
        "level": "high"
    }


def _txt_response(content, *, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
    )


def test_txt_parser_chunks_on_paragraph_and_line_boundaries_with_global_numbers(
    tmp_path,
):
    source = tmp_path / "book.txt"
    source.write_text(
        "Title\nFirst paragraph\n\nChapter two\nSecond paragraph\n", encoding="utf-8"
    )
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        _txt_response("<h1>Title</h1><p>First paragraph</p>"),
        _txt_response("<h2>Chapter two</h2><p>Second paragraph</p>"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"parsing_model": "test/model", "txt_max_chunk_lines": 3},
    )

    with patch.object(parser, "_get_setting", return_value="{numbered_lines}"):
        status, html = parser.process()

    assert status == "COMPLETED"
    assert html.index("Title") < html.index("Chapter two")
    assert parser.metrics["chunk_count"] == 2
    prompts = [
        call.kwargs["messages"][0]["content"]
        for call in llm_service.completion.call_args_list
    ]
    assert "[1] Title" in prompts[0]
    assert "[4] Chapter two" in prompts[1]
    assert "[1] Title" not in prompts[1]


def test_txt_parser_prior_context_is_bounded_structure_only_and_not_repeated(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("Alpha\n\nBeta\n\nGamma", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        _txt_response("<h1>Alpha</h1>"),
        _txt_response("<p>Beta</p>"),
        _txt_response("<p>Gamma</p>"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"parsing_model": "test/model", "txt_max_chunk_chars": 5},
    )

    with patch.object(parser, "_get_setting", return_value="{numbered_lines}"):
        parser.process()

    prompts = [
        call.kwargs["messages"][0]["content"]
        for call in llm_service.completion.call_args_list
    ]
    assert "[PRIOR_STRUCTURE_CONTEXT_BEGIN]" in prompts[1]
    assert "Context only. Do not repeat" in prompts[1]
    assert "Alpha" not in prompts[1]
    assert "<h1> x1" in prompts[1]
    assert "Beta" not in prompts[2]


def test_txt_parser_marks_context_when_custom_template_uses_context_placeholder(
    tmp_path,
):
    source = tmp_path / "book.txt"
    source.write_text("Alpha\n\nBeta", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        _txt_response("<h1>Alpha</h1>"),
        _txt_response("<p>Beta</p>"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={
            "txt_max_chunk_lines": 2,
            "prompt_txt_structuring": "Prior={prior_structure_context}\n{numbered_lines}",
        },
    )

    parser.process()

    second_prompt = llm_service.completion.call_args_list[1].kwargs["messages"][0][
        "content"
    ]
    assert "[PRIOR_STRUCTURE_CONTEXT_BEGIN]" in second_prompt
    assert "Context only. Do not repeat" in second_prompt


def test_txt_parser_preserves_literal_braces_in_custom_template(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("One\nTwo", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = _txt_response("<p>One</p><p>Two</p>")
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={
            "parsing_model": "test/model",
            "prompt_txt_structuring": 'JSON {"literal": true}\n{numbered_lines}',
        },
    )

    parser.process()

    prompt = llm_service.completion.call_args.kwargs["messages"][0]["content"]
    assert '{"literal": true}' in prompt
    assert "[1] One" in prompt


@pytest.mark.parametrize(
    "content",
    ["", "<script>alert(1)</script>", '<a href="javascript:alert(1)">bad</a>'],
)
def test_txt_parser_rejects_empty_or_unsafe_chunk_without_partial_result(
    tmp_path, content
):
    source = tmp_path / "notes.txt"
    source.write_text("One\nTwo", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = _txt_response(content)
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_retry_backoff_seconds": 0},
    )

    with pytest.raises(RuntimeError, match="no partial document"):
        parser.process()

    assert parser.metrics["chunks_completed"] == 0


def test_txt_parser_mid_run_failure_does_not_return_partial_book(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("One\n\nTwo", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        _txt_response("<p>One</p>"),
        RuntimeError("permanent failure"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_retries": 1, "txt_max_chunk_lines": 2},
    )

    with pytest.raises(RuntimeError, match="no partial document"):
        parser.process()

    assert parser.metrics["chunks_completed"] == 1
    assert parser.metrics["failed_chunk"] == 2


def test_txt_parser_rejects_huge_input_before_any_model_call(tmp_path):
    source = tmp_path / "huge.txt"
    source.write_text("a\n" * 20, encoding="utf-8")
    llm_service = MagicMock()
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_input_chars": 10},
    )

    with pytest.raises(RuntimeError, match="No model call was made"):
        parser.process()

    llm_service.completion.assert_not_called()


def test_txt_parser_rejects_oversized_file_from_stat_before_reading(tmp_path):
    source = tmp_path / "sparse.txt"
    source.write_bytes(b"x" * 32)
    llm_service = MagicMock()
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_input_bytes": 1},
    )

    with patch.object(
        parser, "_read_text", side_effect=AssertionError("must not read")
    ):
        with pytest.raises(RuntimeError, match="file was not read"):
            parser.process()

    llm_service.completion.assert_not_called()


def test_txt_parser_prefers_paragraph_boundary_when_next_unit_would_overflow(tmp_path):
    source = tmp_path / "paragraphs.txt"
    source.write_text("AAAA\n\nBBBB", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        _txt_response("<p>AAAA</p>"),
        _txt_response("<p>BBBB</p>"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_chunk_chars": 9},
    )

    parser.process()

    prompts = [
        call.kwargs["messages"][0]["content"]
        for call in llm_service.completion.call_args_list
    ]
    assert len(prompts) == 2
    assert "[1] AAAA" in prompts[0]
    assert "[3] BBBB" in prompts[1]
    assert "[3] BBBB" not in prompts[0]


def test_txt_parser_rejects_too_many_chunks_before_any_model_call(tmp_path):
    source = tmp_path / "many.txt"
    source.write_text("One\n\nTwo\n\nThree", encoding="utf-8")
    llm_service = MagicMock()
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_chunk_lines": 2, "txt_max_chunks": 1},
    )

    with pytest.raises(RuntimeError, match="No model call was made"):
        parser.process()

    llm_service.completion.assert_not_called()


def test_txt_parser_retries_transient_model_error_and_records_attempts(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("One", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        TimeoutError("temporary timeout"),
        _txt_response("<p>One</p>"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_retry_backoff_seconds": 0},
    )

    status, html = parser.process()

    assert status == "COMPLETED"
    assert html == "<p>One</p>"
    assert parser.metrics["model_calls"] == 2
    assert parser.metrics["chunk_retries"] == 1


@pytest.mark.parametrize(
    "content",
    [
        "<svg><text>bad</text></svg>",
        "<math><mi>x</mi></math>",
        '<link rel="stylesheet" href="https://example.invalid/x.css">bad</link>',
        '<meta name="x" content="bad">bad',
        '<base href="https://example.invalid/">bad',
        "<audio>bad</audio>",
        "<video>bad</video>",
        '<source src="https://example.invalid/x">bad',
        "<template>bad</template>",
        "<textarea>bad</textarea>",
        "<select><option>bad</option></select>",
        "<canvas>bad</canvas>",
        '<map name="bad">bad</map>',
        "<noscript>bad</noscript>",
        "<form>bad</form>",
        '<embed src="https://example.invalid/x">bad</embed>',
        '<img src="data:image/png;base64,AAAA" alt="bad">',
    ],
)
def test_txt_parser_rejects_non_inert_or_hallucinated_asset_html(tmp_path, content):
    source = tmp_path / "notes.txt"
    source.write_text("One", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = _txt_response(content)
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_retry_backoff_seconds": 0},
    )

    with pytest.raises(RuntimeError, match="reason_category=unsafe_model_output"):
        parser.process()


def test_txt_parser_accepts_rich_inert_semantic_html_and_template_text(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("Rich", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = _txt_response(
        '<h2 xml:lang="en" hidden>Heading</h2>'
        '<blockquote cite="#source">Quote</blockquote>'
        "<ol><li>Use <code>{{ customer_name }}</code></li></ol>"
        '<p><time datetime="2026-08-24">Today</time></p>'
        "<table><thead><tr><th>Term</th></tr></thead>"
        "<tbody><tr><td>Value</td></tr></tbody></table>"
    )
    parser = TxtParser(str(source), user_id=1, llm_service=llm_service)

    status, html = parser.process()

    assert status == "COMPLETED"
    assert '<blockquote cite="#source">Quote</blockquote>' in html
    assert 'xml:lang="en"' in html
    assert "<ol><li>Use <code>{{ customer_name }}</code></li></ol>" in html
    assert "<table>" in html and "<th>Term</th>" in html


@pytest.mark.parametrize(
    "content",
    [
        '<p title="' + ("x" * 4_097) + '">bad</p>',
        '<p title="bad\x01value">bad</p>',
        '<a href="file:///secret">bad</a>',
        '<a href="javascript:alert(1)">bad</a>',
    ],
)
def test_txt_parser_rejects_unsafe_or_unbounded_attributes(tmp_path, content):
    source = tmp_path / "notes.txt"
    source.write_text("One", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = _txt_response(content)
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_retry_backoff_seconds": 0},
    )

    with pytest.raises(RuntimeError, match="reason_category=unsafe_model_output"):
        parser.process()


@pytest.mark.parametrize(
    "finish_reason",
    [
        "LENGTH",
        "MAX_TOKENS",
        "RECITATION",
        "SAFETY",
        "PROHIBITED_CONTENT",
        "SPII",
        "BLOCKLIST",
        "content_filter",
    ],
)
def test_txt_parser_rejects_terminal_provider_finish_reasons_without_retry(
    tmp_path, finish_reason
):
    source = tmp_path / "notes.txt"
    source.write_text("One", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.return_value = _txt_response(
        "<p>One</p>", finish_reason=finish_reason
    )
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_retry_backoff_seconds": 0},
    )

    with pytest.raises(RuntimeError, match="reason_category=terminal_finish_reason"):
        parser.process()

    llm_service.completion.assert_called_once()


def test_txt_parser_keeps_permanent_error_category_without_provider_detail(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("One", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = RuntimeError("secret source document content")
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_retries": 1},
    )

    with pytest.raises(RuntimeError) as error:
        parser.process()

    rendered = str(error.value)
    assert "reason_category=permanent_provider_failure" in rendered
    assert "secret source document content" not in rendered


def test_txt_parser_merges_fenced_and_wrapped_fragments_without_wrapper_roots(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("One\n\nTwo", encoding="utf-8")
    llm_service = MagicMock()
    llm_service.completion.side_effect = [
        _txt_response("```html\n<main_content><p>One</p></main_content>\n```"),
        _txt_response("<body><p>Two</p></body>"),
    ]
    parser = TxtParser(
        str(source),
        user_id=1,
        llm_service=llm_service,
        settings={"txt_max_chunk_lines": 2},
    )

    status, html = parser.process()

    assert status == "COMPLETED"
    assert html == "<p>One</p>\n<p>Two</p>"
    assert "<main_content>" not in html
    assert "<body>" not in html


def test_txt_parser_no_llm_fallback_has_one_document_heading(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("Title\nChapter one\nChapter two", encoding="utf-8")
    parser = TxtParser(str(source), user_id=1, llm_service=None)

    status, html = parser.process()

    assert status == "COMPLETED"
    assert html.count("<h1>") == 1
    assert html.count("<p>") == 2


def test_epub_parser_follows_spine_excludes_nav_and_preserves_relative_images_and_links(
    tmp_path,
):
    epub_path = tmp_path / "book.epub"
    image_bytes = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(image_bytes, format="PNG")
    container = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    package = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:test</dc:identifier><dc:title>T</dc:title><dc:language>en</dc:language></metadata>
      <manifest>
        <item id="one" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
        <item id="two" href="text/chapter2.xhtml" media-type="application/xhtml+xml"/>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        <item id="img" href="images/pic.png" media-type="image/png"/>
      </manifest><spine><itemref idref="two"/><itemref idref="one"/></spine>
    </package>"""
    with zipfile.ZipFile(epub_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        archive.writestr(
            "EPUB/nav.xhtml", "<html><body><p>NAVIGATION DUPLICATE</p></body></html>"
        )
        archive.writestr(
            "EPUB/text/chapter1.xhtml",
            '<html><body><h1 id="start">One</h1><img src="../images/pic.png" alt="Blue"/></body></html>',
        )
        archive.writestr(
            "EPUB/text/chapter2.xhtml",
            '<html><body><h1>Two</h1><a href="chapter1.xhtml#start">Next</a></body></html>',
        )
        archive.writestr("EPUB/images/pic.png", image_bytes.getvalue())

    app = Flask(__name__)
    with app.app_context():
        status, output = HtmlEpubParser(str(epub_path), 1, None).process()

    assert status == "COMPLETED"
    assert output.index("Two") < output.index("One")
    assert "NAVIGATION DUPLICATE" not in output
    assert 'href="#epub-2-start"' in output
    soup = BeautifulSoup(output, "html.parser")
    extracted_src = soup.find("img")["src"]
    assert extracted_src.startswith("epub_")
    assert (tmp_path / extracted_src).is_file()
