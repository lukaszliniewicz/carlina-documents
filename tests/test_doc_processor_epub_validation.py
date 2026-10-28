from __future__ import annotations

import zipfile
from types import SimpleNamespace

import pytest
from flask import Flask

from iarf_assistant.addons.doc_processor import epub_validation, routes


def _valid_epub_members() -> dict[str, bytes]:
    return {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
        "EPUB/package.opf": b"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Validation fixture</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="pub-id">urn:uuid:test</dc:identifier>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>""",
        "EPUB/nav.xhtml": b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body><nav epub:type="toc"><h1>Contents</h1><ol><li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body>
</html>""",
        "EPUB/chapter.xhtml": b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter</title></head>
  <body><main><p>Text.</p></main></body>
</html>""",
    }


def _write_epub(path, members: dict[str, bytes], *, mimetype_first: bool = True):
    names = list(members)
    if not mimetype_first:
        names.remove("mimetype")
        names.insert(1, "mimetype")
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            compression = (
                zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            )
            archive.writestr(name, members[name], compress_type=compression)
    return path


def _disable_epubcheck(monkeypatch):
    monkeypatch.delenv("EPUBCHECK_EXECUTABLE", raising=False)
    monkeypatch.delenv("EPUBCHECK_JAR", raising=False)
    monkeypatch.delenv("EPUBCHECK_REQUIRED", raising=False)
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: None)


def _publishing_note_members() -> dict[str, bytes]:
    members = _valid_epub_members()
    members["EPUB/chapter.xhtml"] = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Chapter</title></head><body><main>
<h1 id="chapter" data-epub-role="chapter" data-epub-nav-level="1">Chapter<sup><a
 id="ref-391" href="#note-391" epub:type="noteref" role="doc-noteref">391</a></sup></h1>
<p id="other-target">Body prose.</p>
<section id="notes" data-epub-role="endnotes" epub:type="endnotes" role="doc-endnotes"
 aria-labelledby="notes-heading"><h2 id="notes-heading" data-epub-nav-level="1">Notes</h2>
<aside id="note-391" epub:type="endnote" role="doc-endnote">Note prose.
<a href="#ref-391" epub:type="backlink" role="doc-backlink"
 aria-label="Return to reference 391">↩</a></aside></section>
</main></body></html>""".encode()
    members["EPUB/nav.xhtml"] = b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head><body>
<nav epub:type="toc"><h1>Contents</h1><ol>
<li><a href="chapter.xhtml#chapter">Chapter</a></li>
<li><a href="chapter.xhtml#notes-heading">Notes</a></li></ol></nav>
<nav epub:type="landmarks"><h2>Landmarks</h2><ol><li>
<a href="chapter.xhtml#notes" epub:type="endnotes">Notes</a></li></ol></nav>
</body></html>"""
    return members


def test_publishing_labels_and_reciprocal_note_links_pass(tmp_path):
    path = _write_epub(tmp_path / "publishing.epub", _publishing_note_members())
    assert epub_validation.validate_epub_structure(str(path)) == (True, None)


@pytest.mark.parametrize(
    "member,before,after,message",
    [
        (
            "EPUB/nav.xhtml",
            b'chapter.xhtml#chapter">Chapter</a>',
            b'chapter.xhtml#chapter">Chapter 391</a>',
            "contents label does not match",
        ),
        (
            "EPUB/nav.xhtml",
            b'epub:type="endnotes">Notes</a>',
            b'epub:type="endnotes">Note prose. Entire collection.</a>',
            "notes landmark label does not match",
        ),
        (
            "EPUB/chapter.xhtml",
            b"<title>Chapter</title>",
            b"<title>Index</title>",
            "document title does not match",
        ),
        (
            "EPUB/chapter.xhtml",
            b'href="#ref-391" epub:type="backlink"',
            b'href="#other-target" epub:type="backlink"',
            "no reciprocal return link",
        ),
        (
            "EPUB/chapter.xhtml",
            b'<h2 id="notes-heading" data-epub-nav-level="1">Notes</h2>',
            b'<span id="notes-heading">Notes</span>',
            "missing its visible heading",
        ),
    ],
)
def test_publishing_semantics_reject_valid_xml_with_wrong_meaning(
    tmp_path, member, before, after, message
):
    members = _publishing_note_members()
    assert before in members[member]
    members[member] = members[member].replace(before, after)
    path = _write_epub(tmp_path / "corrupted.epub", members)
    passed, diagnostic = epub_validation.validate_epub_structure(str(path))
    assert passed is False
    assert diagnostic is not None and message in diagnostic
    assert "Note prose" not in diagnostic


def test_minimal_epub_passes_structure_and_marks_missing_optional_checker_not_run(
    tmp_path, monkeypatch
):
    _disable_epubcheck(monkeypatch)
    path = _write_epub(tmp_path / "minimal.epub", _valid_epub_members())

    structure = epub_validation.validate_epub_structure(str(path))
    result = epub_validation.validate_epub_file(str(path), config={})

    assert structure == (True, None)
    assert result.passed is True
    assert result.message is None
    assert result.epubcheck == "not_run"
    assert result.version is None
    assert result.as_legacy_tuple() == (True, None)


def test_internal_failures_skip_official_epubcheck(tmp_path, monkeypatch):
    _disable_epubcheck(monkeypatch)
    malformed = tmp_path / "malformed.epub"
    malformed.write_bytes(b"not a ZIP archive")
    monkeypatch.setattr(
        epub_validation,
        "run_official_epubcheck",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("called")),
    )

    result = epub_validation.validate_epub_file(str(malformed), config={})

    assert result.passed is False
    assert result.epubcheck == "not_run"
    assert result.message


def test_structure_rejects_bad_mimetype_navigation_manifest_and_links(tmp_path):
    corruptions = (
        (
            "bad-mimetype.epub",
            lambda members: members.__setitem__("mimetype", b"text/plain"),
            "mimetype is invalid",
        ),
        (
            "bad-navigation.epub",
            lambda members: members.__setitem__(
                "EPUB/nav.xhtml",
                members["EPUB/nav.xhtml"].replace(
                    b'<ol><li><a href="chapter.xhtml">Chapter</a></li></ol>',
                    b"<ol></ol>",
                ),
            ),
            "Navigation ordered lists must contain",
        ),
        (
            "bad-manifest.epub",
            lambda members: members.__setitem__(
                "EPUB/package.opf",
                members["EPUB/package.opf"].replace(
                    b'<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>',
                    b"",
                ),
            ),
            "spine is empty or references an unknown manifest item",
        ),
        (
            "unresolved-target.epub",
            lambda members: members.__setitem__(
                "EPUB/chapter.xhtml",
                members["EPUB/chapter.xhtml"].replace(
                    b"<p>Text.</p>",
                    b'<p><a href="missing.xhtml">Missing</a></p>',
                ),
            ),
            "EPUB hyperlink target is missing or unmanifested",
        ),
    )
    for filename, corrupt, expected in corruptions:
        members = _valid_epub_members()
        corrupt(members)
        path = _write_epub(tmp_path / filename, members)

        passed, message = epub_validation.validate_epub_structure(str(path))

        assert passed is False
        assert expected in str(message)


def test_mimetype_must_be_first(tmp_path):
    path = _write_epub(
        tmp_path / "late-mimetype.epub", _valid_epub_members(), mimetype_first=False
    )

    passed, message = epub_validation.validate_epub_structure(str(path))

    assert passed is False
    assert "mimetype entry is missing or not first" in str(message)


def test_relationship_references_on_root_body_and_main_must_resolve_locally(tmp_path):
    chapter_template = _valid_epub_members()["EPUB/chapter.xhtml"]
    cases = (
        (
            "root",
            b'<html xmlns="http://www.w3.org/1999/xhtml">',
            b'<html xmlns="http://www.w3.org/1999/xhtml" aria-labelledby="missing-root">',
            "missing-root",
        ),
        (
            "body",
            b"<body>",
            b'<body aria-describedby="missing-body">',
            "missing-body",
        ),
        (
            "main",
            b"<main>",
            b'<main aria-labelledby="missing-main">',
            "missing-main",
        ),
    )
    for name, original, replacement, missing_id in cases:
        members = _valid_epub_members()
        members["EPUB/chapter.xhtml"] = chapter_template.replace(
            original, replacement, 1
        )
        path = _write_epub(tmp_path / f"missing-{name}-reference.epub", members)

        passed, message = epub_validation.validate_epub_structure(str(path))

        assert passed is False
        assert message == (
            "XHTML accessibility relationship contains an unresolved local ID reference."
        )
        assert missing_id not in str(message)
        assert "EPUB/chapter.xhtml" not in str(message)


def test_relationship_idrefs_cannot_resolve_across_xhtml_documents(tmp_path):
    members = _valid_epub_members()
    members["EPUB/chapter.xhtml"] = members["EPUB/chapter.xhtml"].replace(
        b"<p>Text.</p>", b'<p id="chapter-label">Text.</p>'
    )
    members["EPUB/nav.xhtml"] = members["EPUB/nav.xhtml"].replace(
        b"<body>", b'<body aria-labelledby="chapter-label">'
    )
    path = _write_epub(tmp_path / "cross-document-reference.epub", members)

    passed, message = epub_validation.validate_epub_structure(str(path))

    assert passed is False
    assert message == (
        "XHTML accessibility relationship contains an unresolved local ID reference."
    )
    assert "chapter-label" not in str(message)
    assert "EPUB/nav.xhtml" not in str(message)


def test_valid_multitoken_relationships_and_table_headers_pass(tmp_path):
    members = _valid_epub_members()
    members["EPUB/chapter.xhtml"] = b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" aria-labelledby="chapter-title description-one">
  <head><title>Chapter</title></head>
  <body aria-describedby="description-one description-two">
    <main aria-labelledby="main-label description-two">
      <h1 id="chapter-title">Chapter</h1>
      <p id="description-one">First description.</p>
      <p id="description-two">Second description.</p>
      <h2 id="main-label">Content</h2>
      <table>
        <tbody>
          <tr><th id="column-one" scope="col">One</th><th id="column-two" scope="col">Two</th></tr>
          <tr><td headers="column-one column-two">Values</td></tr>
        </tbody>
      </table>
    </main>
  </body>
</html>"""
    path = _write_epub(tmp_path / "valid-relationships.epub", members)

    assert epub_validation.validate_epub_structure(str(path)) == (True, None)


def test_headers_references_require_local_ids_and_same_table_headers(tmp_path):
    cases = (
        (
            "missing-header",
            b'<main><table><tr><td headers="missing-header">Value</td></tr></table></main>',
            "XHTML accessibility relationship contains an unresolved local ID reference.",
        ),
        (
            "non-header-target",
            b'<main><p id="not-a-header">Label</p><table><tr><td headers="not-a-header">Value</td></tr></table></main>',
            "XHTML headers relationship must reference a th in the same table.",
        ),
        (
            "different-table",
            b'<main><table><tr><th id="other-table-header">Other</th></tr></table><table><tr><td headers="other-table-header">Value</td></tr></table></main>',
            "XHTML headers relationship must reference a th in the same table.",
        ),
    )
    for name, main_markup, expected_message in cases:
        members = _valid_epub_members()
        members["EPUB/chapter.xhtml"] = members["EPUB/chapter.xhtml"].replace(
            b"<main><p>Text.</p></main>", main_markup
        )
        path = _write_epub(tmp_path / f"{name}.epub", members)

        passed, message = epub_validation.validate_epub_structure(str(path))

        assert passed is False
        assert message == expected_message
        assert "EPUB/chapter.xhtml" not in str(message)


def test_required_checker_and_explicit_missing_paths_fail_without_leaking_paths(
    tmp_path, monkeypatch
):
    _disable_epubcheck(monkeypatch)
    required = epub_validation.run_official_epubcheck(
        "book.epub", config={"EPUBCHECK_REQUIRED": True}
    )
    missing_executable = tmp_path / "private" / "missing-epubcheck"
    missing_jar = tmp_path / "private" / "missing.jar"
    executable_result = epub_validation.run_official_epubcheck(
        "book.epub", config={"EPUBCHECK_EXECUTABLE": str(missing_executable)}
    )
    jar_result = epub_validation.run_official_epubcheck(
        "book.epub", config={"EPUBCHECK_JAR": str(missing_jar)}
    )

    assert required.passed is False
    assert required.epubcheck == "failed"
    assert "Official EPUBCheck validation is required" in str(required.message)
    assert executable_result.message == "Configured EPUBCheck executable was not found."
    assert str(missing_executable) not in str(executable_result.message)
    assert jar_result.message == "Configured EPUBCheck JAR was not found."
    assert str(missing_jar) not in str(jar_result.message)


def test_configured_executable_is_mocked_and_version_is_parsed(tmp_path, monkeypatch):
    executable = tmp_path / "epubcheck"
    executable.write_text("mock", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout="EPUBCheck v5.4.0\nNo errors", stderr=""
        )

    monkeypatch.setattr(
        epub_validation.shutil,
        "which",
        lambda name: str(executable) if name == str(executable) else None,
    )
    monkeypatch.setattr(epub_validation.subprocess, "run", fake_run)

    result = epub_validation.run_official_epubcheck(
        "book.epub", config={"EPUBCHECK_EXECUTABLE": str(executable)}
    )

    assert result.passed is True
    assert result.epubcheck == "passed"
    assert result.version == "5.4.0"
    assert calls == [
        (
            [str(executable), "book.epub"],
            {"capture_output": True, "check": False, "text": True, "timeout": 180},
        )
    ]


def test_missing_normal_version_uses_bounded_version_probe(tmp_path, monkeypatch):
    executable = tmp_path / "epubcheck"
    executable.write_text("mock", encoding="utf-8")
    book_path = str(tmp_path / "book.epub")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return SimpleNamespace(
                returncode=0,
                stdout=f"EPUBCheck v5.4.0 {executable} {book_path}",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="No errors", stderr="")

    monkeypatch.setattr(
        epub_validation.shutil,
        "which",
        lambda name: str(executable) if name == str(executable) else None,
    )
    monkeypatch.setattr(epub_validation.subprocess, "run", fake_run)

    result = epub_validation.run_official_epubcheck(
        book_path, config={"EPUBCHECK_EXECUTABLE": str(executable)}
    )

    assert result.passed is True
    assert result.epubcheck == "passed"
    assert result.version == "5.4.0"
    assert calls == [
        (
            [str(executable), book_path],
            {"capture_output": True, "check": False, "text": True, "timeout": 180},
        ),
        (
            [str(executable), "--version"],
            {"capture_output": True, "check": False, "text": True, "timeout": 10},
        ),
    ]


def test_unavailable_version_probe_does_not_fail_successful_epubcheck(
    tmp_path, monkeypatch
):
    executable = tmp_path / "epubcheck"
    executable.write_text("mock", encoding="utf-8")
    book_path = str(tmp_path / "book.epub")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            raise PermissionError("version probe unavailable")
        return SimpleNamespace(returncode=0, stdout="No errors", stderr="")

    monkeypatch.setattr(
        epub_validation.shutil,
        "which",
        lambda name: str(executable) if name == str(executable) else None,
    )
    monkeypatch.setattr(epub_validation.subprocess, "run", fake_run)

    result = epub_validation.run_official_epubcheck(
        book_path, config={"EPUBCHECK_EXECUTABLE": str(executable)}
    )

    assert result.passed is True
    assert result.epubcheck == "passed"
    assert result.version is None
    assert calls[0][0] == [str(executable), book_path]
    assert calls[0][1]["timeout"] == 180
    assert calls[1][0] == [str(executable), "--version"]
    assert calls[1][1]["timeout"] == 10


def test_configured_jar_uses_existing_argument_array(tmp_path, monkeypatch):
    jar = tmp_path / "epubcheck.jar"
    jar.write_bytes(b"mock")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        epub_validation.shutil,
        "which",
        lambda name: "/usr/bin/java" if name == "java" else None,
    )
    monkeypatch.setattr(epub_validation.subprocess, "run", fake_run)

    result = epub_validation.run_official_epubcheck(
        "book.epub", config={"EPUBCHECK_JAR": str(jar)}
    )

    assert result.epubcheck == "passed"
    assert calls[0][0] == ["/usr/bin/java", "-jar", str(jar), "book.epub"]
    assert calls[0][1]["timeout"] == 180


def test_unusable_configured_command_fails_without_echoing_its_absolute_path(
    tmp_path, monkeypatch
):
    executable = tmp_path / "unusable-epubcheck"
    executable.write_text("mock", encoding="utf-8")
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        epub_validation.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError(13, "Permission denied", str(executable))
        ),
    )

    result = epub_validation.run_official_epubcheck(
        "book.epub", config={"EPUBCHECK_EXECUTABLE": str(executable)}
    )

    assert result.passed is False
    assert result.epubcheck == "failed"
    assert "Unable to run configured EPUBCheck" in str(result.message)
    assert str(executable) not in str(result.message)


def test_failed_checker_diagnostics_are_bounded_and_keep_version(tmp_path, monkeypatch):
    book_path = str(tmp_path / "private" / "book.epub")
    executable = tmp_path / "epubcheck"
    executable.write_text("mock", encoding="utf-8")
    monkeypatch.setattr(
        epub_validation.shutil,
        "which",
        lambda name: str(executable) if name == "epubcheck" else None,
    )
    monkeypatch.setattr(
        epub_validation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout=f"EPUBCheck v5.4.0\n{'x' * 5000}\n{book_path}",
            stderr="",
        ),
    )

    result = epub_validation.run_official_epubcheck(book_path, config={})

    assert result.passed is False
    assert result.epubcheck == "failed"
    assert result.version == "5.4.0"
    assert len(str(result.message)) <= 4000
    assert book_path not in str(result.message)


def test_route_compatibility_wrapper_keeps_official_checker_hook(tmp_path, monkeypatch):
    path = _write_epub(tmp_path / "minimal.epub", _valid_epub_members())
    calls = []

    def fake_checker(output_path: str) -> tuple[bool, str | None]:
        calls.append(output_path)
        return True, None

    monkeypatch.setattr(routes, "_run_official_epubcheck", fake_checker)

    assert routes._validate_epub_file(str(path)) == (True, None)
    assert calls == [str(path)]


def test_route_checker_wrapper_passes_current_app_config(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config["EPUBCHECK_REQUIRED"] = True
    captured = {}

    def fake_runner(output_path, *, config):
        captured["output_path"] = output_path
        captured["config"] = config
        return epub_validation.EpubValidationResult(True, None, "passed", "5.4.0")

    monkeypatch.setattr(epub_validation, "run_official_epubcheck", fake_runner)

    with app.app_context():
        result = routes._run_official_epubcheck("book.epub")

    assert result == (True, None)
    assert captured["output_path"] == "book.epub"
    assert captured["config"] is app.config
