from __future__ import annotations

import base64
import stat
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from iarf_assistant.addons.doc_processor.archive_safety import ArchiveSafetyError
from iarf_assistant.addons.doc_processor.parsers import fb2_parser
from iarf_assistant.addons.doc_processor.parsers.fb2_parser import (
    FB2_NAMESPACE,
    Fb2Parser,
    extract_fb2_metadata,
)
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
)

XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


def _rich_fixture(*, namespace: str | None = FB2_NAMESPACE, binary: str | None = None) -> str:
    ns = f' xmlns="{namespace}"' if namespace else ""
    binary = binary or base64.b64encode(_png_bytes()).decode("ascii")
    return f'''<?xml version="1.0" encoding="utf-8"?>
<FictionBook{ns} xmlns:l="{XLINK_NAMESPACE}" id="book/root">
  <description>
    <title-info>
      <genre>fiction</genre><genre>Fiction</genre>
      <book-title>Bound &amp; Safe</book-title>
      <subtitle>Part I</subtitle><lang>en-US</lang>
      <author><first-name>Ada</first-name><middle-name>L.</middle-name><last-name>Lovelace</last-name></author>
      <author><nickname>Second Author</nickname></author>
      <translator><nickname>Тест Переводчик</nickname></translator>
      <annotation><p>Annotation <emphasis>description</emphasis>.</p></annotation>
      <keywords>one, two; three</keywords>
      <sequence name="Series" number="2"/>
    </title-info>
    <document-info><author><nickname>File Author</nickname></author><id>document-id</id></document-info>
    <publish-info><publisher>Example Press</publisher><year value="2025"/><isbn>978-1-2-3</isbn></publish-info>
  </description>
  <body>
    <title id="source-title"><p>Book <emphasis>Title</emphasis></p><p>Second line</p></title>
    <section id="chapter one">
      <title><p>Chapter <strong>One</strong></p></title>
      <p id="paragraph"><emphasis>Nested</emphasis> text <sub>sub</sub><sup>sup</sup><code>code</code><strikethrough>gone</strikethrough> tail.</p>
      <p>Note <a id="note-ref" l:href="#note-1">1</a>; <a l:href="https://example.test/a">external</a>; <a l:href="javascript:alert(1)">unsafe</a>.</p>
      <epigraph><p>Quote <emphasis>here</emphasis>.</p><text-author>Someone</text-author></epigraph>
      <poem><stanza><v>First line</v><v>Second <strong>line</strong></v><text-author>Poet</text-author></stanza></poem>
      <table><tr><th rowspan="2" colspan="2">Head</th></tr><tr><td>Cell <emphasis>one</emphasis></td></tr></table>
      <empty-line/><image l:href="#image-id"/>
      <unknown id="unknown/id">Unknown <emphasis>text</emphasis>.</unknown>
    </section>
  </body>
  <body name="notes"><section id="note-1"><title><p>Note</p></title><p>Note body.</p></section></body>
  <binary id="image-id" content-type="image/png">{binary}</binary>
</FictionBook>'''


def _write_fixture(path: Path, source: str, *, encoding: str = "utf-8", declaration: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if declaration:
        source = source.replace('encoding="utf-8"', f'encoding="{declaration}"', 1)
    path.write_bytes(source.encode(encoding))
    return path


def _write_zip_fixture(path: Path, members: list[tuple[str | zipfile.ZipInfo, bytes | str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return path


def test_fb2_process_renders_rich_semantic_html_and_canonicalizes(tmp_path):
    uploads = tmp_path / "uploads"
    source = _write_fixture(uploads / "7" / "book.fb2", _rich_fixture())
    parser = Fb2Parser(str(source), user_id=7, llm_service=None, settings={"uploads_dir": uploads})

    status, html = parser.process()

    assert status == "COMPLETED"
    assert parser.metrics == {}
    soup = BeautifulSoup(html, "html.parser")
    assert soup.main is not None
    assert soup.main["lang"] == "en-US"
    assert soup.find("h1").get_text(" ", strip=True) == "Book Title Second line"
    assert soup.find("h2").get_text(" ", strip=True) == "Chapter One"
    assert soup.find("h2", class_="subtitle") is None
    assert soup.find("section", role="doc-footnote", attrs={"epub:type": "footnote"}) is not None
    ref = soup.find("a", role="doc-noteref")
    assert ref is not None
    assert ref["epub:type"] == "noteref"
    assert ref["href"].startswith("#fb2-src-note-1-")
    assert soup.find("blockquote").find("cite").get_text(strip=True) == "Someone"
    assert len(soup.select(".poem .stanza > p")) == 3
    cell = soup.find("td")
    assert cell is not None and cell.get_text(" ", strip=True) == "Cell one"
    assert cell.find("em") is not None
    assert soup.find("p").find("br") is not None or soup.find("p") is not None
    assert "<unknown" not in html and "Unknown text." in html
    assert "javascript:" not in html

    semantic = canonicalize_document_html(
        html,
        user_id=7,
        source_file_path=str(source),
        uploads_dir=uploads,
    )
    assert not semantic.errors


def test_fb2_metadata_uses_title_info_authors_and_document_id_only(tmp_path):
    source = _write_fixture(tmp_path / "book.fb2", _rich_fixture())

    metadata = extract_fb2_metadata(source)

    assert metadata["title"] == "Bound & Safe"
    assert metadata["subtitle"] == "Part I"
    assert metadata["language"] == "en-US"
    assert metadata["contributors"] == {
        "authors": ["Ada L. Lovelace", "Second Author"],
        "translators": ["Тест Переводчик"],
        "editors": [],
        "other": [],
    }
    assert metadata["description"] == "Annotation description."
    assert metadata["subjects"] == ["fiction", "one", "two", "three"]
    assert metadata["publisher"] == "Example Press"
    assert metadata["publication_date"] == "2025"
    assert metadata["isbn"] == "978-1-2-3"
    assert metadata["journal_or_series"] == "Series"
    assert metadata["volume"] == "2"
    assert metadata["identifiers"] == [{"value": "book/root", "scheme": "FB2"}, {"value": "document-id", "scheme": "FB2"}]
    assert "File Author" not in str(metadata["contributors"])
    assert "cover_image_id" not in metadata


def test_fb2_zip_wrapper_matches_plain_metadata_and_semantic_html(tmp_path):
    source = _write_fixture(tmp_path / "uploads" / "7" / "book.fb2", _rich_fixture())
    settings = {"uploads_dir": tmp_path / "uploads"}

    plain_metadata = extract_fb2_metadata(source)
    plain_status, plain_html = Fb2Parser(str(source), 7, None, settings=settings).process()

    _write_zip_fixture(
        source,
        [("nested/", b""), ("nested/BOOK.FB2", _rich_fixture().encode("utf-8"))],
    )
    wrapped_metadata = extract_fb2_metadata(source)
    wrapped_status, wrapped_html = Fb2Parser(str(source), 7, None, settings=settings).process()

    assert plain_status == wrapped_status == "COMPLETED"
    assert wrapped_metadata == plain_metadata
    assert wrapped_html == plain_html
    image = BeautifulSoup(wrapped_html, "html.parser").find("img")
    assert image is not None
    extracted = settings["uploads_dir"] / image["src"].removeprefix("/uploads/")
    assert extracted.read_bytes() == _png_bytes()


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([], "exactly one non-directory .fb2 member"),
        (
            [("first.fb2", _rich_fixture()), ("second.FB2", _rich_fixture())],
            "exactly one non-directory .fb2 member",
        ),
        ([('payload.txt', _rich_fixture())], "exactly one non-directory .fb2 member"),
    ],
)
def test_fb2_zip_requires_one_case_insensitive_fb2_payload(tmp_path, members, message):
    source = _write_zip_fixture(tmp_path / "invalid.fb2", members)

    with pytest.raises(ValueError, match=message):
        Fb2Parser(str(source), 1, None).process()


@pytest.mark.parametrize(
    ("label", "source_text", "message"),
    [
        (
            "wrong-root",
            '<Other xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body/></Other>',
            "expected FictionBook",
        ),
        (
            "duplicate-id",
            '<FictionBook><body><section id="same"/><p id="same">x</p></body></FictionBook>',
            "duplicate source ID",
        ),
        (
            "dtd",
            '<!DOCTYPE FictionBook><FictionBook><body><p>x</p></body></FictionBook>',
            "prohibited",
        ),
    ],
)
def test_fb2_zip_reapplies_xml_root_id_and_dtd_protections(tmp_path, label, source_text, message):
    source = _write_zip_fixture(
        tmp_path / f"{label}.fb2",
        [("payload.Fb2", f'<?xml version="1.0" encoding="utf-8"?>{source_text}')],
    )

    with pytest.raises(ValueError, match=message):
        Fb2Parser(str(source), 1, None).process()


def test_fb2_rejects_malformed_pk_header_without_disclosing_source(tmp_path):
    source = tmp_path / "malformed.fb2"
    source.write_bytes(b"PK\x03\x04not a ZIP archive")

    with pytest.raises(ValueError, match=r"^Invalid FB2 ZIP\.$") as error:
        Fb2Parser(str(source), 1, None).process()

    assert str(error.value) == "Invalid FB2 ZIP."


def test_fb2_zip_preserves_archive_traversal_diagnostics(tmp_path):
    source = _write_zip_fixture(tmp_path / "traversal.fb2", [("../payload.fb2", _rich_fixture())])

    with pytest.raises(ArchiveSafetyError, match="unsafe member path"):
        Fb2Parser(str(source), 1, None).process()


def test_fb2_zip_rejects_symlink_member_via_archive_validator(tmp_path):
    symlink = zipfile.ZipInfo("payload.fb2")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    source = _write_zip_fixture(tmp_path / "symlink.fb2", [(symlink, b"payload")])

    with pytest.raises(ArchiveSafetyError, match="link or special file"):
        Fb2Parser(str(source), 1, None).process()


def test_fb2_zip_stream_read_guard_uses_fb2_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(fb2_parser, "MAX_FB2_BYTES", 1024)
    body = "".join(f"{index:04d}" for index in range(260))
    payload = f"<FictionBook><body><p>{body}</p></body></FictionBook>"
    source = _write_zip_fixture(tmp_path / "expanded.fb2", [("payload.fb2", payload)])

    with pytest.raises(ValueError, match="read size"):
        Fb2Parser(str(source), 1, None).process()


@pytest.mark.parametrize(
    ("encoding", "declaration"),
    [("utf-8", "utf-8"), ("utf-16", "utf-16"), ("cp1251", "windows-1251")],
)
def test_fb2_respects_declared_encoding_and_unnamespaced_input(tmp_path, encoding, declaration):
    source = _write_fixture(
        tmp_path / f"book-{encoding}.fb2",
        _rich_fixture(namespace=None).replace("<body>", "<body><p>Привет мир</p>", 1),
        encoding=encoding,
        declaration=declaration,
    )

    status, html = Fb2Parser(str(source), 1, None).process()

    assert status == "COMPLETED"
    assert "Тест Переводчик" not in html  # metadata is intentionally not body text
    assert "Book" in html and "Title" in html
    assert "Привет мир" in html


def test_fb2_preserves_inline_tails_and_maps_every_source_id_deterministically(tmp_path):
    source = _write_fixture(tmp_path / "book.fb2", _rich_fixture())
    first = Fb2Parser(str(source), 1, None).process()[1]
    second = Fb2Parser(str(source), 1, None).process()[1]
    soup = BeautifulSoup(first, "html.parser")

    assert first == second
    assert soup.find(id="fb2-src-paragraph-") is None
    paragraph = soup.find("p", id=lambda value: value and value.startswith("fb2-src-paragraph-"))
    assert paragraph is not None
    assert paragraph.get_text("", strip=False).endswith("tail.")
    assert all((tag.get("id") or "").startswith(("fb2-src-", "fb2-body-")) for tag in soup.find_all(id=True))
    assert len({tag["id"] for tag in soup.find_all(id=True)}) == len(soup.find_all(id=True))


def test_fb2_extracts_verified_image_to_hashed_safe_sibling_path(tmp_path):
    uploads = tmp_path / "uploads"
    source = _write_fixture(uploads / "7" / "book.fb2", _rich_fixture())

    html = Fb2Parser(str(source), 7, None, settings={"uploads_dir": uploads}).process()[1]
    image = BeautifulSoup(html, "html.parser").find("img")

    assert image is not None
    assert image["src"].startswith("/uploads/7/")
    extracted = uploads / image["src"].removeprefix("/uploads/")
    assert extracted.parent == source.parent
    assert extracted.name.startswith("fb2_")
    assert "image-id" not in extracted.name
    assert extracted.read_bytes() == _png_bytes()


def test_fb2_preserves_metadata_cover_without_duplicating_body_image(tmp_path):
    fixture = _rich_fixture().replace('<lang>en-US</lang>', '<lang>en-US</lang><coverpage><image l:href="#image-id"/></coverpage>')
    source = _write_fixture(tmp_path / "book.fb2", fixture)
    assert len(BeautifulSoup(Fb2Parser(str(source), 1, None).process()[1], "html.parser").find_all("img")) == 1
    source.write_text(fixture.replace('<empty-line/><image l:href="#image-id"/>', '<empty-line/>'), encoding="utf-8")
    soup = BeautifulSoup(Fb2Parser(str(source), 1, None).process()[1], "html.parser")
    assert soup.select_one("figure.cover img") is not None


@pytest.mark.parametrize(
    ("label", "source", "message"),
    [
        ("malformed", "<FictionBook><body>", "Invalid FB2 XML"),
        ("no-body", '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"/>', "no body"),
        (
            "wrong-root",
            '<Other xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body/></Other>',
            "expected FictionBook",
        ),
        (
            "wrong-namespace",
            '<FictionBook xmlns="urn:wrong"><body/></FictionBook>',
            "root namespace",
        ),
        (
            "duplicate-id",
            '<FictionBook><body><section id="same"/><p id="same">x</p></body></FictionBook>',
            "duplicate source ID",
        ),
        (
            "entity",
            '<!DOCTYPE FictionBook [<!ENTITY x "boom">]><FictionBook><body><p>&x;</p></body></FictionBook>',
            "prohibited",
        ),
        ("dtd", '<!DOCTYPE FictionBook><FictionBook><body><p>x</p></body></FictionBook>', "prohibited"),
    ],
)
def test_fb2_rejects_invalid_or_unsafe_xml(tmp_path, label, source, message):
    path = _write_fixture(tmp_path / f"{label}.fb2", f'<?xml version="1.0" encoding="utf-8"?>{source}')

    with pytest.raises(ValueError, match=message):
        Fb2Parser(str(path), 1, None).process()


def test_fb2_named_style_quotes_languages_and_title_paragraph_links(tmp_path):
    source = _write_fixture(tmp_path / "book.fb2", '''<FictionBook>
    <description><title-info><lang>en</lang></title-info></description>
    <body xml:lang="pl"><section><title><p id="heading">Heading</p></title>
    <p xml:lang="de">Before <style name="book-specific" xml:lang="fr">Un <emphasis>mot</emphasis></style> after.</p>
    <cite><p>First quotation.</p><p>Second quotation.</p></cite>
    <p><a href="#heading">Return</a></p></section></body></FictionBook>''')
    output = Fb2Parser(str(source), 1, None).process()[1]
    soup = BeautifulSoup(output, "html.parser")
    assert soup.main["lang"] == "pl"
    assert soup.find("p", lang="de").get_text() == "Before Un mot after."
    assert soup.find("span", lang="fr").em.get_text() == "mot"
    assert [node.get_text() for node in soup.blockquote.find_all("p")] == ["First quotation.", "Second quotation."]
    assert soup.find(id=soup.a["href"][1:]).get_text() == "Heading"


def test_fb2_rejects_size_and_tree_depth_before_rendering(tmp_path, monkeypatch):
    source = _write_fixture(tmp_path / "large.fb2", _rich_fixture())
    monkeypatch.setattr(fb2_parser, "MAX_FB2_BYTES", 64)
    with pytest.raises(ValueError, match="64 MiB"):
        Fb2Parser(str(source), 1, None).process()

    nested = "<FictionBook><body><p>" + ("<x>" * 5) + "deep" + ("</x>" * 5) + "</p></body></FictionBook>"
    deep_source = _write_fixture(tmp_path / "deep.fb2", nested)
    monkeypatch.setattr(fb2_parser, "MAX_FB2_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(fb2_parser, "MAX_FB2_DEPTH", 4)
    with pytest.raises(ValueError, match="maximum tree depth"):
        Fb2Parser(str(deep_source), 1, None).process()


def test_fb2_stops_streaming_at_element_limit_before_reading_invalid_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(fb2_parser, "MAX_FB2_ELEMENTS", 8)
    source = _write_fixture(
        tmp_path / "wide.fb2",
        '<FictionBook><body>' + '<p/>' * 20_000 + '<invalid',
    )
    with pytest.raises(ValueError, match="maximum element count"):
        Fb2Parser(str(source), 1, None).process()


def test_fb2_rejects_oversized_raster_before_writing_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(fb2_parser, "MAX_FB2_IMAGE_PIXELS", 3)
    source = _write_fixture(tmp_path / "pixels.fb2", _rich_fixture())
    with pytest.raises(ValueError, match="valid raster"):
        Fb2Parser(str(source), 1, None).process()
    assert not list(tmp_path.glob("fb2_*.png"))


@pytest.mark.parametrize(
    "binary_xml",
    [
        '<binary id="image-id" content-type="image/png">not-base64!</binary>',
        '<binary id="image-id" content-type="image/svg+xml">c3Zn</binary>',
    ],
)
def test_fb2_rejects_missing_or_unsupported_images(tmp_path, binary_xml):
    source_text = _rich_fixture().replace(
        f'<binary id="image-id" content-type="image/png">{base64.b64encode(_png_bytes()).decode("ascii")}</binary>',
        binary_xml,
    )
    if "not-base64" in binary_xml:
        expected = "not valid base64"
    else:
        expected = "Unsupported FB2 image MIME"
    source = _write_fixture(tmp_path / "bad-image.fb2", source_text)

    with pytest.raises(ValueError, match=expected):
        Fb2Parser(str(source), 1, None).process()

    missing = _write_fixture(tmp_path / "missing-image.fb2", _rich_fixture().replace("#image-id", "#missing"))
    with pytest.raises(ValueError, match="missing binary"):
        Fb2Parser(str(missing), 1, None).process()
