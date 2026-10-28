import hashlib
import subprocess
import sys
from pathlib import Path

import fitz
import pytest

from carlina.core.epub_cover import normalize_epub_cover
from iarf_assistant.addons.doc_processor import epub_cover
from iarf_assistant.addons.doc_processor.epub_cover import (
    MAX_COVER_PDF_BYTES,
    _build_title_cover_pdf,
    pdf_source_sha256,
    render_pdf_cover,
    render_title_cover,
)


def _write_pdf(
    path: Path, page_specs: list[tuple[tuple[float, float, float], str]]
) -> None:
    document = fitz.open()
    try:
        for color, label in page_specs:
            page = document.new_page(width=600, height=900)
            page.draw_rect(page.rect, color=None, fill=color, overlay=False)
            page.insert_text((60, 100), label, fontsize=30, color=(1, 1, 1))
        document.save(path)
    finally:
        document.close()


def _pixmap(png: bytes) -> fitz.Pixmap:
    return fitz.Pixmap(png)


def _write_halfspread_pdf(path: Path) -> None:
    document = fitz.open()
    try:
        page = document.new_page(width=1200, height=800)
        page.draw_rect(fitz.Rect(0, 0, 600, 800), color=None, fill=(1, 0, 0))
        page.draw_rect(fitz.Rect(600, 0, 1200, 800), color=None, fill=(0, 0, 1))
        document.save(path)
    finally:
        document.close()


def _write_rotated_quadrants_pdf(path: Path) -> None:
    document = fitz.open()
    try:
        page = document.new_page(width=400, height=200)
        page.draw_rect(fitz.Rect(0, 0, 200, 100), color=None, fill=(1, 0, 0))
        page.draw_rect(fitz.Rect(200, 0, 400, 100), color=None, fill=(0, 1, 0))
        page.draw_rect(fitz.Rect(0, 100, 200, 200), color=None, fill=(0, 0, 1))
        page.draw_rect(fitz.Rect(200, 100, 400, 200), color=None, fill=(1, 1, 0))
        page.set_rotation(90)
        document.save(path)
    finally:
        document.close()


def _write_inherited_cropbox_pdf(path: Path) -> None:
    document = fitz.open()
    try:
        page = document.new_page(width=800, height=600)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 0))
        page.draw_rect(fitz.Rect(200, 100, 600, 500), color=None, fill=(1, 0, 0))
        _, parent_reference = document.xref_get_key(page.xref, "Parent")
        parent_xref = int(parent_reference.split()[0])
        document.xref_set_key(parent_xref, "CropBox", "[200 100 600 500]")
        document.save(path)
    finally:
        document.close()


def test_normalize_epub_cover_crop_preserves_legacy_defaults_and_rounds():
    assert normalize_epub_cover() == {}
    assert normalize_epub_cover({"mode": "existing"}) == {}
    assert normalize_epub_cover({"mode": "generated"}) == {"mode": "generated"}
    assert normalize_epub_cover({"mode": "pdf_page", "page": 7}) == {
        "mode": "pdf_page",
        "page": 7,
    }
    assert normalize_epub_cover(
        {"mode": "pdf_page", "page": 7, "crop": (0.1, 0.2, 0.11, 0.21)}
    ) == {
        "mode": "pdf_page",
        "page": 7,
        "crop": [0.1, 0.2, 0.11, 0.21],
    }
    assert normalize_epub_cover(
        {"mode": "pdf_page", "page": 7, "crop": [0, 0.12345678, 0.98765432, 1]}
    ) == {
        "mode": "pdf_page",
        "page": 7,
        "crop": [0.0, 0.123457, 0.987654, 1.0],
    }
    assert normalize_epub_cover(
        {"mode": "pdf_page", "page": 7, "crop": [-0.0, -0.0, 1, 1]}
    )["crop"] == [0.0, 0.0, 1.0, 1.0]


def test_normalize_epub_cover_rejects_invalid_crop_values_and_modes():
    invalid_crops = (
        None,
        "0,0,1,1",
        [0, 0, 1],
        [0, 0, 1, 1, 1],
        [False, 0, 1, 1],
        [0, float("nan"), 1, 1],
        [0, 0, float("inf"), 1],
        [-0.001, 0, 1, 1],
        [0, 0, 1.001, 1],
        [0.5, 0, 0.5, 1],
        [0.75, 0, 0.25, 1],
        [0, 0, 0.009999, 1],
        [0, 0, 1, 0.009999],
    )
    for crop in invalid_crops:
        with pytest.raises(ValueError, match=r"^Invalid EPUB cover crop\.$"):
            normalize_epub_cover({"mode": "pdf_page", "page": 1, "crop": crop})

    for options in (
        {"mode": "existing", "crop": None},
        {"mode": "generated", "crop": None},
        {"crop": None},
        {"mode": "pdf_page", "page": 1, "crop": [0, 0, 1, 1], "extra": True},
    ):
        with pytest.raises(ValueError):
            normalize_epub_cover(options)


def test_pdf_cover_renders_the_selected_one_based_physical_page_as_rgb_png(tmp_path):
    source = tmp_path / "two-pages.pdf"
    _write_pdf(
        source,
        [
            ((0.85, 0.12, 0.12), "physical page one"),
            ((0.12, 0.72, 0.24), "physical page two"),
        ],
    )

    first = render_pdf_cover(source, 1)
    second = render_pdf_cover(
        source,
        2,
        expected_sha256=pdf_source_sha256(source).upper(),
    )
    first_pixmap = _pixmap(first)
    second_pixmap = _pixmap(second)

    first_pixel = first_pixmap.pixel(first_pixmap.width // 2, first_pixmap.height // 2)
    second_pixel = second_pixmap.pixel(
        second_pixmap.width // 2, second_pixmap.height // 2
    )
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert second.startswith(b"\x89PNG\r\n\x1a\n")
    assert first_pixel[0] > first_pixel[1]
    assert second_pixel[1] > second_pixel[0]
    assert first != second
    assert first_pixmap.n == 3 and not first_pixmap.alpha
    assert second_pixmap.n == 3 and not second_pixmap.alpha
    assert max(second_pixmap.width, second_pixmap.height) <= 1800


def test_pdf_cover_without_crop_matches_the_existing_full_page_raster(tmp_path):
    source = tmp_path / "legacy-raster.pdf"
    _write_pdf(source, [((0.2, 0.4, 0.6), "full page")])

    with fitz.open(source) as document:
        page = document[0]
        scale = (1800 - 0.001) / max(page.rect.width, page.rect.height)
        expected = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        ).tobytes("png")

    assert render_pdf_cover(source, 1) == expected


def test_pdf_cover_crop_selects_halfspread_and_keeps_source_hash_guard(tmp_path):
    source = tmp_path / "synthetic-halfspread.pdf"
    _write_halfspread_pdf(source)

    pixmap = _pixmap(
        render_pdf_cover(
            source,
            1,
            expected_sha256=pdf_source_sha256(source),
            crop=[0.5, 0, 1, 1],
        )
    )
    center = pixmap.pixel(pixmap.width // 2, pixmap.height // 2)
    assert max(pixmap.width, pixmap.height) <= 1800
    assert pixmap.width / pixmap.height == pytest.approx(0.75, abs=0.002)
    assert center[2] > center[0]

    with pytest.raises(ValueError, match=r"^PDF source hash mismatch$"):
        render_pdf_cover(source, 1, expected_sha256="0" * 64, crop=[0.5, 0, 1, 1])


def test_pdf_cover_crop_uses_visible_rotated_page_rectangle(tmp_path):
    source = tmp_path / "synthetic-rotated.pdf"
    _write_rotated_quadrants_pdf(source)

    pixmap = _pixmap(render_pdf_cover(source, 1, crop=[0, 0, 0.5, 0.5]))
    center = pixmap.pixel(pixmap.width // 2, pixmap.height // 2)
    assert (pixmap.width, pixmap.height) == (900, 1800)
    assert center[2] > center[0]


def test_pdf_cover_without_explicit_crop_honors_inherited_cropbox(tmp_path):
    source = tmp_path / "synthetic-inherited-cropbox.pdf"
    _write_inherited_cropbox_pdf(source)
    with fitz.open(source) as document:
        page = document[0]
        assert document.xref_get_key(page.xref, "CropBox") == ("null", "null")
        assert tuple(page.cropbox) == (200.0, 100.0, 600.0, 500.0)

    pixmap = _pixmap(render_pdf_cover(source, 1))
    assert (pixmap.width, pixmap.height) == (1800, 1800)
    assert pixmap.pixel(20, 20)[0] > pixmap.pixel(20, 20)[2]


def test_pdf_cover_rejects_invalid_page_numbers_before_opening_source(tmp_path):
    source = tmp_path / "missing.pdf"
    for page in (0, -1, True, 1.0, "1"):
        with pytest.raises(ValueError, match=r"^page must be a positive integer$"):
            render_pdf_cover(source, page)


def test_pdf_cover_rejects_out_of_range_page_with_stable_message(tmp_path):
    source = tmp_path / "one-page.pdf"
    _write_pdf(source, [((0.2, 0.3, 0.4), "only page")])

    with pytest.raises(ValueError, match=r"^PDF page is out of range$"):
        render_pdf_cover(source, 2)


def test_pdf_cover_reports_missing_non_file_and_invalid_pdf_without_source_paths(
    tmp_path,
):
    missing = tmp_path / "missing-private-name.pdf"
    directory = tmp_path / "private-directory"
    directory.mkdir()
    invalid = tmp_path / "invalid-private-name.pdf"
    invalid.write_bytes(b"not a PDF")

    for source in (missing, directory):
        with pytest.raises(ValueError, match=r"^PDF source is unavailable$") as error:
            pdf_source_sha256(source)
        assert str(source) not in str(error.value)
        with pytest.raises(ValueError, match=r"^PDF source is unavailable$") as error:
            render_pdf_cover(source, 1)
        assert str(source) not in str(error.value)

    with pytest.raises(
        ValueError, match=r"^PDF source is not a readable PDF$"
    ) as error:
        render_pdf_cover(invalid, 1)
    assert str(invalid) not in str(error.value)


def test_pdf_cover_rejects_encrypted_sources_without_password_prompt(tmp_path):
    source = tmp_path / "password-protected.pdf"
    document = fitz.open()
    document.new_page(width=600, height=900)
    document.save(
        source,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="synthetic-owner-password",
        user_pw="synthetic-user-password",
    )
    document.close()

    with pytest.raises(
        ValueError, match=r"^PDF source is password-protected$"
    ) as error:
        render_pdf_cover(source, 1)
    assert str(source) not in str(error.value)


def test_pdf_cover_rejects_expected_hash_mismatch(tmp_path):
    source = tmp_path / "source.pdf"
    _write_pdf(source, [((0.2, 0.3, 0.4), "synthetic page")])

    with pytest.raises(ValueError, match=r"^PDF source hash mismatch$") as error:
        render_pdf_cover(source, 1, expected_sha256="0" * 64)
    assert str(source) not in str(error.value)


def test_pdf_hash_and_renderer_reject_growth_past_a_small_test_limit(
    tmp_path, monkeypatch
):
    source = tmp_path / "bounded-source.pdf"
    source.write_bytes(b"0123456789abcdefg")
    monkeypatch.setattr(epub_cover, "MAX_COVER_PDF_BYTES", 16)

    for operation in (
        lambda: pdf_source_sha256(source),
        lambda: render_pdf_cover(source, 1),
    ):
        with pytest.raises(ValueError, match=r"^PDF source exceeds the size limit$"):
            operation()

    assert MAX_COVER_PDF_BYTES == 512 * 1024 * 1024


def test_pdf_source_hash_uses_sha256_of_exact_source_bytes(tmp_path, monkeypatch):
    source = tmp_path / "hash-source.pdf"
    source_bytes = b"small synthetic input" * 100
    source.write_bytes(source_bytes)
    monkeypatch.setattr(epub_cover, "_HASH_CHUNK_BYTES", 13)

    assert pdf_source_sha256(source) == hashlib.sha256(source_bytes).hexdigest()


def test_snapshot_hash_does_not_import_pdf_rendering_runtime(tmp_path):
    source = tmp_path / "hash-only.pdf"
    source.write_bytes(b"%PDF synthetic hash-only source")
    script = """
import hashlib
import importlib
import sys
from pathlib import Path

class NoRenderer:
    def find_spec(self, fullname, path, target=None):
        if fullname in {"fitz", "pymupdf"}:
            raise AssertionError("hashing must not load the PDF renderer")

sys.meta_path.insert(0, NoRenderer())
module = importlib.import_module(sys.argv[1])
source = Path(sys.argv[2])
assert module.pdf_source_sha256(source) == hashlib.sha256(source.read_bytes()).hexdigest()
"""
    subprocess.run(
        [sys.executable, "-c", script, epub_cover.__name__, str(source)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_pdf_cover_uses_white_background_and_skips_annotations(tmp_path):
    source = tmp_path / "blank-with-annotation.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=900)
    annotation = page.add_rect_annot(fitz.Rect(80, 100, 300, 400))
    annotation.set_colors(stroke=(1, 0, 0), fill=(1, 0, 0))
    annotation.update()
    document.save(source)
    document.close()

    pixmap = _pixmap(render_pdf_cover(source, 1))
    assert pixmap.pixel(20, 20) == (255, 255, 255)
    assert pixmap.pixel(200, 200) == (255, 255, 255)


def test_pdf_cover_scales_large_pages_to_at_most_1800_pixels(tmp_path):
    source = tmp_path / "large-page.pdf"
    document = fitz.open()
    page = document.new_page(width=2400, height=3600)
    page.draw_rect(page.rect, color=None, fill=(0.1, 0.2, 0.3), overlay=False)
    document.save(source)
    document.close()

    pixmap = _pixmap(render_pdf_cover(source, 1))
    assert max(pixmap.width, pixmap.height) <= 1800


def test_pdf_cover_raster_is_deterministic_for_the_same_page(tmp_path):
    source = tmp_path / "deterministic.pdf"
    _write_pdf(source, [((0.3, 0.4, 0.5), "repeatable page")])

    assert render_pdf_cover(source, 1) == render_pdf_cover(source, 1)


def test_title_cover_embeds_escaped_bounded_metadata_before_rasterization():
    title = "Życie λόγος 中文 الحياة"
    metadata = {
        "subtitle": "Subtitle <b>literal</b> & safe",
        "contributors": {
            "authors": ("A. Author", "B. Writer"),
            "translators": ["T. Translator"],
        },
    }
    intermediate = _build_title_cover_pdf(metadata, title=title)
    with fitz.open(stream=intermediate, filetype="pdf") as document:
        text = document[0].get_text()

    assert "Życie" in text
    assert "λόγος" in text
    assert "中文" in text
    assert any(0xFE80 <= ord(character) <= 0xFEFF for character in text)
    assert "Subtitle <b>literal</b> & safe" in text
    assert "A. Author" in text and "B. Writer" in text
    assert "Translated by" in text and "T. Translator" in text


def test_title_cover_uses_author_fallback_and_keeps_long_text(tmp_path):
    title = "TitleWithoutSpaces" * 20
    metadata = {
        "subtitle": "Subtitle " * 20,
        "author": "Fallback author",
        "contributors": {
            "authors": [],
            "translators": ("Translator one", "Translator two"),
        },
    }

    intermediate = _build_title_cover_pdf(metadata, title=title)
    with fitz.open(stream=intermediate, filetype="pdf") as document:
        text = document[0].get_text()
    compact_text = "".join(text.split())

    assert title in compact_text
    assert "Subtitle" in text
    assert "Fallback author" in text
    assert "Translator one" in text and "Translator two" in text


def test_title_cover_png_has_designed_portrait_dimensions_and_is_deterministic():
    metadata = {
        "subtitle": "A subtitle",
        "contributors": {"authors": ["A. Author"]},
    }
    first = render_title_cover(metadata, title="A designed title")
    second = render_title_cover(metadata, title="A designed title")
    pixmap = _pixmap(first)

    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert (pixmap.width, pixmap.height) == (1200, 1800)
    assert pixmap.n == 3 and not pixmap.alpha
    assert pixmap.pixel(20, 20) == (246, 241, 230)
    assert first == second


def test_title_cover_rejects_empty_titles():
    for title in ("", " \n "):
        with pytest.raises(ValueError, match=r"^cover title must be nonempty text$"):
            render_title_cover({}, title=title)


def test_title_cover_rejects_nontext_title():
    with pytest.raises(TypeError, match=r"^cover title must be text$"):
        render_title_cover({}, title=None)
