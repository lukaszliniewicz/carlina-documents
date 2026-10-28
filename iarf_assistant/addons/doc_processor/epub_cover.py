"""Bounded PDF and metadata-backed EPUB cover image rendering."""

from __future__ import annotations

import hashlib
import html
import importlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, Protocol, Self, cast


class _Rect(Protocol):
    x0: float
    y0: float
    width: float
    height: float


class _Pixmap(Protocol):
    width: int
    height: int

    def tobytes(self, output: str) -> bytes: ...


class _Page(Protocol):
    rect: _Rect

    def get_pixmap(
        self,
        *,
        matrix: object,  # noqa: V107 - checked third-party keyword signature
        colorspace: object,  # noqa: V107
        alpha: bool,
        annots: bool,  # noqa: V107
        clip: _Rect | None = None,
    ) -> _Pixmap: ...

    def insert_htmlbox(
        self,
        rect: _Rect,
        text: str,
        *,
        css: str,
        scale_low: int,  # noqa: V107
        overlay: bool,
    ) -> tuple[float, float]: ...

    def draw_rect(
        self,
        rect: _Rect,
        *,
        color: None,  # noqa: V107
        fill: tuple[float, float, float],  # noqa: V107
        overlay: bool,
    ) -> object: ...

    def draw_line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        color: tuple[float, float, float],  # noqa: V107
        width: float,
    ) -> object: ...


class _PDFDocument(Protocol):
    is_encrypted: bool
    needs_pass: bool
    page_count: int

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...

    def load_page(self, page: int) -> _Page: ...

    def new_page(self, *, width: int, height: int) -> _Page: ...

    def tobytes(self) -> bytes: ...

    def close(self) -> None: ...


class _MuPDF(Protocol):
    csRGB: object

    def open(
        self,
        *,
        stream: bytes | None = None,
        filetype: str | None = None,  # noqa: V107
    ) -> _PDFDocument: ...

    def Matrix(self, width: float, height: float) -> object: ...

    def Rect(self, left: float, top: float, right: float, bottom: float) -> _Rect: ...


def _pdf_api() -> _MuPDF:
    # Snapshot hashing does not need the optional rendering runtime.
    # PyMuPDF has no complete stubs; describe only the API used here.
    return cast(_MuPDF, importlib.import_module("fitz"))


MAX_COVER_PDF_BYTES = 512 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_COVER_MAX_EDGE = 1800
_TITLE_PAGE_WIDTH = 600
_TITLE_PAGE_HEIGHT = 900
_SOURCE_UNAVAILABLE = "PDF source is unavailable"
_SOURCE_TOO_LARGE = "PDF source exceeds the size limit"
_SOURCE_HASH_MISMATCH = "PDF source hash mismatch"
_SOURCE_NOT_PDF = "PDF source is not a readable PDF"
_SOURCE_ENCRYPTED = "PDF source is password-protected"
_PAGE_INVALID = "page must be a positive integer"
_PAGE_OUT_OF_RANGE = "PDF page is out of range"
_TITLE_INVALID = "cover title must be nonempty text"
_TITLE_METADATA_INVALID = "cover metadata must be a mapping"
_TITLE_LAYOUT_FAILED = "cover text could not be fitted"

_CREAM = (0.965, 0.949, 0.902)
_NAVY = "#182A46"
_ACCENT = (0.72, 0.60, 0.39)


def _open_bounded_source(path: object) -> BinaryIO:
    if not isinstance(path, Path):
        raise ValueError(_SOURCE_UNAVAILABLE)

    try:
        if not path.is_file():
            raise ValueError(_SOURCE_UNAVAILABLE)
        if path.stat().st_size > MAX_COVER_PDF_BYTES:
            raise ValueError(_SOURCE_TOO_LARGE)
        return path.open("rb")
    except ValueError as error:
        if str(error) in {_SOURCE_UNAVAILABLE, _SOURCE_TOO_LARGE}:
            raise
        raise ValueError(_SOURCE_UNAVAILABLE) from None
    except OSError:
        raise ValueError(_SOURCE_UNAVAILABLE) from None


def _source_sha256(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        remaining = MAX_COVER_PDF_BYTES - total
        chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_COVER_PDF_BYTES:
            raise ValueError(_SOURCE_TOO_LARGE)
        digest.update(chunk)
    return digest.hexdigest()


def pdf_source_sha256(path: Path) -> str:
    """Hash one regular PDF source in bounded chunks without retaining its bytes."""

    try:
        with _open_bounded_source(path) as source:
            return _source_sha256(source)
    except ValueError:
        raise
    except OSError:
        raise ValueError(_SOURCE_UNAVAILABLE) from None


def _read_bounded_source(path: Path) -> bytes:
    try:
        with _open_bounded_source(path) as source:
            data = source.read(MAX_COVER_PDF_BYTES + 1)
    except ValueError:
        raise
    except OSError:
        raise ValueError(_SOURCE_UNAVAILABLE) from None
    if len(data) > MAX_COVER_PDF_BYTES:
        raise ValueError(_SOURCE_TOO_LARGE)
    return data


def _render_page_png(page: _Page, crop: Sequence[float] | None = None) -> bytes:
    fitz = _pdf_api()
    try:
        rect = page.rect
        clip = None
        if crop is not None:
            left, top, right, bottom = crop
            clip = fitz.Rect(
                rect.x0 + left * rect.width,
                rect.y0 + top * rect.height,
                rect.x0 + right * rect.width,
                rect.y0 + bottom * rect.height,
            )
    except RuntimeError, ValueError:
        raise ValueError(_SOURCE_NOT_PDF) from None
    longest_edge = (
        max(clip.width, clip.height)
        if clip is not None
        else max(rect.width, rect.height)
    )
    if longest_edge <= 0:
        raise ValueError(_SOURCE_NOT_PDF)

    scale = (_COVER_MAX_EDGE - 0.001) / longest_edge

    def render_at(scale: float) -> _Pixmap:
        if clip is None:
            return page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=False,
                annots=False,
            )
        return page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
            clip=clip,
        )

    try:
        pixmap = render_at(scale)
        if max(pixmap.width, pixmap.height) > _COVER_MAX_EDGE:
            scale *= (_COVER_MAX_EDGE - 1) / max(pixmap.width, pixmap.height)
            pixmap = render_at(scale)
        if max(pixmap.width, pixmap.height) > _COVER_MAX_EDGE:
            raise ValueError(_SOURCE_NOT_PDF)
        return pixmap.tobytes("png")
    except OSError, RuntimeError, TypeError, ValueError:
        raise ValueError(_SOURCE_NOT_PDF) from None


def render_pdf_cover(
    path: Path,
    page: int,
    *,
    expected_sha256: object = None,
    crop: Sequence[float] | None = None,
) -> bytes:
    """Render one 1-based physical PDF page as a bounded RGB PNG."""

    if type(page) is not int or page <= 0:
        raise ValueError(_PAGE_INVALID)

    source = _read_bounded_source(path)
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or hashlib.sha256(source).hexdigest() != expected_sha256.casefold()
    ):
        raise ValueError(_SOURCE_HASH_MISMATCH)

    fitz = _pdf_api()
    try:
        document = fitz.open(stream=source, filetype="pdf")
    except OSError, RuntimeError, TypeError, ValueError:
        raise ValueError(_SOURCE_NOT_PDF) from None
    with document:
        try:
            encrypted = document.is_encrypted or document.needs_pass
            page_count = document.page_count
        except RuntimeError, ValueError:
            raise ValueError(_SOURCE_NOT_PDF) from None
        if encrypted:
            raise ValueError(_SOURCE_ENCRYPTED)
        if page > page_count:
            raise ValueError(_PAGE_OUT_OF_RANGE)
        try:
            selected_page = document.load_page(page - 1)
        except RuntimeError, ValueError:
            raise ValueError(_SOURCE_NOT_PDF) from None
        return _render_page_png(selected_page, crop)


def _text_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _metadata_names(metadata: Mapping[str, object], role: str) -> list[str]:
    contributors = metadata.get("contributors")
    if not isinstance(contributors, Mapping):
        return []
    names = cast(Mapping[str, object], contributors).get(role)
    if not isinstance(names, (list, tuple)):
        return []
    return [
        name.strip()
        for name in cast(list[object] | tuple[object, ...], names)
        if isinstance(name, str) and name.strip()
    ]


def _cover_text_html(text: str, *, css_class: str) -> str:
    return f'<div class="{css_class}">{html.escape(text, quote=True)}</div>'


def _insert_cover_text(page: _Page, rect: _Rect, markup: str, css: str) -> None:
    spare_height, scale = page.insert_htmlbox(
        rect,
        markup,
        css=css,
        scale_low=0,
        overlay=True,
    )
    if spare_height < 0 or scale <= 0:
        raise ValueError(_TITLE_LAYOUT_FAILED)


def _build_title_cover_pdf(metadata: object, *, title: object) -> bytes:
    if not isinstance(metadata, Mapping):
        raise TypeError(_TITLE_METADATA_INVALID)
    if not isinstance(title, str):
        raise TypeError("cover title must be text")
    if not title.strip():
        raise ValueError(_TITLE_INVALID)

    metadata = cast(Mapping[str, object], metadata)
    subtitle = _text_value(metadata.get("subtitle"))
    authors = _metadata_names(metadata, "authors")
    if not authors:
        author = _text_value(metadata.get("author"))
        authors = [author] if author else []
    translators = _metadata_names(metadata, "translators")

    fitz = _pdf_api()
    document = fitz.open()
    try:
        page = document.new_page(
            width=_TITLE_PAGE_WIDTH,
            height=_TITLE_PAGE_HEIGHT,
        )
        page.draw_rect(page.rect, color=None, fill=_CREAM, overlay=False)
        page.draw_line((72, 174), (160, 174), color=_ACCENT, width=2)

        _insert_cover_text(
            page,
            fitz.Rect(72, 215, 528, 515),
            _cover_text_html(title.strip(), css_class="title"),
            f"""
            .title {{
                color: {_NAVY};
                font-family: sans-serif;
                font-size: 42pt;
                font-weight: bold;
                line-height: 1.15;
                white-space: pre-wrap;
            }}
            """,
        )
        if subtitle:
            _insert_cover_text(
                page,
                fitz.Rect(74, 538, 526, 625),
                _cover_text_html(subtitle, css_class="subtitle"),
                """
                .subtitle {
                    color: #59677D;
                    font-family: sans-serif;
                    font-size: 19pt;
                    line-height: 1.2;
                    white-space: pre-wrap;
                }
                """,
            )
        if authors:
            _insert_cover_text(
                page,
                fitz.Rect(74, 674, 526, 750),
                _cover_text_html(", ".join(authors), css_class="authors"),
                """
                .authors {
                    color: #263B58;
                    font-family: sans-serif;
                    font-size: 16pt;
                    line-height: 1.3;
                    white-space: pre-wrap;
                }
                """,
            )
        if translators:
            page.draw_line((74, 786), (526, 786), color=(0.82, 0.79, 0.72), width=0.8)
            translator_markup = (
                '<div class="translator-label">Translated by</div>'
                f'<div class="translators">{html.escape(", ".join(translators), quote=True)}</div>'
            )
            _insert_cover_text(
                page,
                fitz.Rect(74, 800, 526, 878),
                translator_markup,
                """
                .translator-label {
                    color: #697386;
                    font-family: sans-serif;
                    font-size: 9pt;
                    font-weight: bold;
                    line-height: 1.2;
                }
                .translators {
                    color: #263B58;
                    font-family: sans-serif;
                    font-size: 12pt;
                    line-height: 1.25;
                    white-space: pre-wrap;
                }
                """,
            )
        return document.tobytes()
    except OSError, RuntimeError, TypeError:
        raise ValueError(_TITLE_LAYOUT_FAILED) from None
    finally:
        document.close()


def render_title_cover(metadata: Mapping[str, object], *, title: str) -> bytes:
    """Create a self-contained designed cover PNG from bounded metadata text."""

    pdf_bytes = _build_title_cover_pdf(metadata, title=title)
    fitz = _pdf_api()
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except OSError, RuntimeError, TypeError, ValueError:
        raise ValueError(_TITLE_LAYOUT_FAILED) from None
    with document:
        try:
            page = document.load_page(0)
        except RuntimeError, ValueError:
            raise ValueError(_TITLE_LAYOUT_FAILED) from None
        return _render_page_png(page)
