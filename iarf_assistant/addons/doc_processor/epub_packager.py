from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import re
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from flask import current_app
from PIL import Image, ImageCms, ImageOps, ImageSequence, features

from carlina.core.diagram_rendering import project_diagrams_html, validate_svg
from carlina.core.epub_cover import normalize_epub_cover
from carlina.core.epub_export import (
    epub_projection_options_sha256,
    epub_value_sha256,
    normalize_epub_options,
)
from carlina.core.export_notes import normalize_export_note_roles
from carlina.core.semantic_html import (
    SAFE_LANGUAGE_RE as _CONTRACT_LANGUAGE_RE,
)
from carlina.core.semantic_html import (
    SAFE_LINK_SCHEMES as _CONTRACT_LINK_SCHEMES,
)
from carlina.core.semantic_html import (
    SEMANTIC_DOCUMENT_TAGS as SAFE_CONTENT_TAGS,
)
from carlina.core.semantic_html import (
    SEMANTIC_DROP_CONTENT_TAGS as REMOVE_WITH_CONTENTS,
)
from carlina.core.semantic_html import (
    SEMANTIC_GLOBAL_ATTRIBUTES as GLOBAL_ATTRS,
)
from carlina.core.semantic_html import (
    SEMANTIC_TAG_ATTRIBUTES as TAG_ATTRS,
)
from iarf_assistant.core.upload_paths import (
    is_upload_reference,
    resolve_upload_reference,
    uploads_root,
)

from . import semantic_document as _semantic_document
from .epub_front_matter import apply_front_matter
from .epub_notes import note_tokens, place_chapter_notes
from .epub_quality import (
    diagnose_publication,
    make_quality_report,
    project_publication_notes,
    publication_language,
)
from .epub_structure import (
    EpubStructure,
    apply_epub_structure,
    assign_review_anchors,
    epub_label_language,
    epub_labels,
    is_cover_image,
    project_selected_cover,
    publication_heading_text,
)
from .epub_styles import BOOK_CSS
from .metadata_service import (
    get_canonical_document_metadata,
    get_document_display_title,
    get_effective_identifier_entries,
)
from .semantic_document import (
    canonicalize_document_html,
    is_current_semantic_document,
    semantic_result_for_stored_html,
)

# Backwards-compatible exports; implementation lives at the shared semantic
# boundary so DOCX and EPUB cannot drift.
diagnose_semantic_export = _semantic_document.diagnose_semantic_export
is_doc_endnote = _semantic_document.is_doc_endnote
is_doc_noteref = _semantic_document.is_doc_noteref
validate_semantic_export = _semantic_document.validate_semantic_export

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("", XHTML_NS)
ET.register_namespace("epub", EPUB_NS)
ET.register_namespace("dc", DC_NS)

CORE_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/webp",
}
SAFE_INPUT_IMAGE_MEDIA_TYPES = {
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}
MAX_INPUT_IMAGE_BYTES = 25 * 1024 * 1024
REFLOW_CSS = """
html { overflow-wrap: break-word; }
body { margin: 0; padding: 0.6em; line-height: 1.5; }
main { max-inline-size: 100%; }
img, svg { max-width: 100%; max-inline-size: 100%; height: auto; }
figure { margin: 1em 0; max-inline-size: 100%; }
figure.cover { text-align: center; break-after: page; }
figure.cover img { max-height: 95vh; object-fit: contain; }
h1, h2, h3, h4, h5, h6 { line-height: 1.2; break-after: avoid; }
p { orphans: 2; widows: 2; }
table { border-collapse: collapse; max-inline-size: 100%; }
th, td { padding: 0.25em; vertical-align: top; overflow-wrap: anywhere; }
caption { text-align: start; margin-block-end: 0.4em; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
a { overflow-wrap: anywhere; }
.epub-backlink { display: inline-block; padding: 0.2em 0.4em; }
.epub-note-group { margin-block-start: 1.5em; }
nav ol { padding-inline-start: 1.5em; }
""".strip()
ALLOWED_EXTRA_MEDIA_TYPES = CORE_IMAGE_MEDIA_TYPES | {
    "text/css",
    "font/woff",
    "font/woff2",
    "font/otf",
    "font/ttf",
}
MAX_CONTENT_DOCUMENT_BYTES = 250_000
JPEG_QUALITY = 85
WEBP_QUALITY = 84
JPEG_MODE_QUALITY = 90
JPEG_MODE_MAX_DECODED_PIXELS = 40_000_000
JPEG_MODE_MAX_ICC_PROFILE_BYTES = 65_536
EPUB_IMAGE_MODES = {"auto", "jpeg", "webp"}
SAFE_EXTERNAL_LINK_SCHEMES = _CONTRACT_LINK_SCHEMES
SAFE_LANGUAGE_RE = _CONTRACT_LANGUAGE_RE


def _safe_wrapper_attrs(tag: Tag | None) -> dict[str, object]:
    """Keep only inert shared attributes from html/body/main wrappers."""

    if tag is None:
        return {}
    attrs: dict[str, object] = {}
    for raw_name, raw_value in tag.attrs.items():
        name = str(raw_name).lower()
        if name not in GLOBAL_ATTRS or name.startswith("on") or name == "style":
            continue
        value = " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
        if name in {"lang", "xml:lang"} and not SAFE_LANGUAGE_RE.fullmatch(value):
            continue
        if name == "dir" and value.casefold() not in {"ltr", "rtl", "auto"}:
            continue
        attrs[name] = raw_value
    return attrs


# Controlled labels accepted from metadata extraction. Values are current MARC
# relator codes; unknown roles are retained as unscoped text instead of being
# falsely asserted to come from the MARC vocabulary.
MARC_ROLE_CODES = {
    "author": "aut",
    "authors": "aut",
    "editor": "edt",
    "editors": "edt",
    "translator": "trl",
    "translators": "trl",
    "illustrator": "ill",
    "photographer": "pht",
    "compiler": "com",
    "annotator": "ann",
    "narrator": "nrt",
    "adapter": "adp",
    "cover designer": "cov",
    "designer of cover": "cov",
    "book designer": "bkd",
    "designer": "dsr",
    "contributor": "ctb",
    "creator": "cre",
    "artist": "art",
    "cartographer": "ctg",
    "transcriber": "trc",
    "proofreader": "pfr",
    "writer of foreword": "wfw",
    "foreword author": "wfw",
    "writer of introduction": "win",
    "introduction author": "win",
    "writer of preface": "wpr",
    "preface author": "wpr",
    "writer of afterword": "waw",
    "afterword author": "waw",
}
MARC_ROLE_VALUES = set(MARC_ROLE_CODES.values())


def _safe_id(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    if not candidate or not re.match(r"^[A-Za-z_]", candidate):
        candidate = f"{fallback}-{candidate}" if candidate else fallback
    return candidate


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    explicit = {
        ".bmp": "image/bmp",
        ".css": "text/css",
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".otf": "font/otf",
        ".ttf": "font/ttf",
    }
    return (
        explicit.get(suffix)
        or mimetypes.guess_type(path)[0]
        or "application/octet-stream"
    )


def _roman_to_int(value: str) -> int | None:
    roman = str(value or "").upper()
    if not roman or not re.fullmatch(r"[IVXLCDM]+", roman):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for char in reversed(roman):
        current = values[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total if total > 0 else None


def _numeric_group_component(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(
        r"(?i)(?:(?:vol(?:ume)?|issue|no\.?|number)\s*)?([0-9]+(?:\.[0-9]+)*)",
        text,
    )
    if match:
        return ".".join(str(int(part)) for part in match.group(1).split("."))
    roman_match = re.fullmatch(
        r"(?i)(?:(?:vol(?:ume)?|issue|no\.?|number)\s*)?([ivxlcdm]+)", text
    )
    if roman_match:
        number = _roman_to_int(roman_match.group(1))
        return str(number) if number is not None else None
    return None


def _group_position(volume, issue) -> str | None:
    volume_position = _numeric_group_component(volume)
    issue_position = _numeric_group_component(issue)
    if volume_position and issue_position:
        return f"{volume_position}.{issue_position}"
    return volume_position or issue_position


def _onix_identifier_type(scheme: str, value) -> str | None:
    normalized_scheme = str(scheme or "").strip().upper()
    if normalized_scheme == "DOI":
        return "06"
    if normalized_scheme == "ISBN":
        compact = re.sub(r"[^0-9Xx]", "", str(value or ""))
        if len(compact) == 10:
            return "02"
        if len(compact) == 13:
            return "15"
    return None


class EPUBPackager:
    def __init__(
        self,
        document,
        output_path: str,
        *,
        image_mode: str = "auto",
        include_source_toc: bool | None = None,
        allowed_resource_roots: list[Path] | None = None,
        cover: object = None,
        cover_image: bytes | None = None,
        options: object = None,
    ):
        normalized_image_mode = str(image_mode or "auto").strip().lower()
        if normalized_image_mode not in EPUB_IMAGE_MODES:
            raise ValueError(f"Unsupported EPUB image mode: {image_mode}")
        self.document = document
        self.output_path = Path(output_path)
        self.image_mode = normalized_image_mode
        self.cover = normalize_epub_cover(cover)
        self.cover_image = cover_image
        if cover_image is not None and self.cover.get("mode") != "pdf_page":
            raise ValueError("A rendered PDF cover requires the PDF page option.")
        self.include_source_toc = include_source_toc
        self.allowed_resource_roots = [
            Path(root).resolve() for root in (allowed_resource_roots or [])
        ]
        self.metadata = get_canonical_document_metadata(document)
        self.options = normalize_epub_options(options)
        self._structure = EpubStructure(None)
        self._quality_soup: BeautifulSoup | None = None
        self._input_sha256 = ""
        self._content_sha256 = ""
        self._language = "en"
        self._direction = "ltr"
        self._landmarks: dict[str, dict[str, str]] = {}
        self._evaluation: dict | None = None
        self.title = get_document_display_title(document, self.metadata)
        self.uid = str(uuid.uuid4())
        self.resources: list[dict] = []
        self._resource_by_source: dict[str, dict] = {}
        self._resource_by_hash: dict[str, dict] = {}
        self._content_documents: list[dict] = []
        self._toc_entries: list[dict] = []
        self._page_entries: list[dict] = []
        self._has_images = False
        self._has_text = False
        self._all_images_have_alt = True
        self._document_root_attrs: dict[str, object] = {}
        self._document_body_attrs: dict[str, object] = {}
        self._content_root_attrs: dict[str, object] = {}
        self._generated_svg_sources: set[str] = set()
        self._stylesheet_href = ""
        self._source_image_sizes: dict[str, int] = {}
        self._image_sizes: dict[str, int] = {}

    @property
    def _publishing_v2(self) -> bool:
        return self.options.get("profile") == "publishing-v2"

    def _raw_content(self) -> str:
        if self.document.output_html:
            raw_html = str(self.document.output_html)
        else:
            from .format_utils import markdown_to_html_on_the_fly

            raw_html = markdown_to_html_on_the_fly(self.document.output_markdown or "")
        settings = getattr(self.document, "settings", None)
        if is_current_semantic_document(settings):
            return semantic_result_for_stored_html(raw_html, settings).html
        user_id = getattr(self.document, "user_id", None)
        file_path = str(getattr(self.document, "file_path", "") or "")
        if user_id is None or not file_path.startswith(f"/uploads/{user_id}/"):
            # Compatibility for detached export fixtures/adapters.  Real
            # ProcessedDocument rows always have an owner and are canonicalized
            # at completion; without one this package has no authority to
            # reinterpret local image paths.
            return raw_html
        # Legacy data is adapted at the export boundary without mutating the
        # historical row.  Invalid footnote links remain diagnostics and are
        # rejected by ``validate_semantic_export`` below.
        return canonicalize_document_html(
            raw_html,
            user_id=user_id,
            source_file_path=file_path,
            relaxed=True,
        ).html

    def _allowed_file(self, candidate: Path) -> Path | None:
        allowed_roots = list(self.allowed_resource_roots)
        document_path = resolve_upload_reference(
            getattr(self.document, "file_path", None),
            uploads_dir=uploads_root(),
        )
        if document_path is not None:
            allowed_roots.append(
                document_path if document_path.is_dir() else document_path.parent
            )
        try:
            resolved = candidate.resolve()
            file_size = resolved.stat().st_size
        except OSError:
            return None
        if (
            not resolved.is_file()
            or file_size > MAX_INPUT_IMAGE_BYTES
            or _media_type(resolved.name) not in SAFE_INPUT_IMAGE_MEDIA_TYPES
        ):
            return None
        for root in allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return resolved
            except OSError, ValueError:
                continue
        return None

    def _resolve_local_resource(self, source: str) -> Path | None:
        if is_upload_reference(source):
            resolved_upload = resolve_upload_reference(
                source, uploads_dir=uploads_root()
            )
            return (
                self._allowed_file(resolved_upload)
                if resolved_upload is not None
                else None
            )

        parsed = urlparse(source)
        if parsed.scheme == "file":
            decoded_file_path = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded_file_path):
                decoded_file_path = decoded_file_path[1:]
            return self._allowed_file(Path(decoded_file_path))

        source_path = Path(source)
        if source_path.is_absolute():
            return self._allowed_file(source_path)

        if parsed.scheme or source.startswith("//"):
            return None

        decoded = unquote(parsed.path or source)
        document_path = resolve_upload_reference(
            getattr(self.document, "file_path", None),
            uploads_dir=uploads_root(),
        )
        candidates: list[Path] = []
        if is_upload_reference(decoded):
            resolved_upload = resolve_upload_reference(
                decoded, uploads_dir=uploads_root()
            )
            if resolved_upload is not None:
                candidates.append(resolved_upload)
        elif decoded.startswith("/static/"):
            candidates.append(Path(current_app.root_path) / decoded.lstrip("/"))
        elif Path(decoded).is_absolute():
            candidates.append(Path(decoded))
        else:
            if document_path is not None:
                candidates.append(document_path.parent / decoded)
            candidates.append(uploads_root() / decoded.lstrip("/"))

        for candidate in candidates:
            resolved = self._allowed_file(candidate)
            if resolved:
                return resolved
        return None

    def _register_resource(
        self,
        data: bytes,
        filename: str,
        *,
        source_key: str | None = None,
        properties: str = "",
        media_type: str | None = None,
    ) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        if source_key and source_key in self._resource_by_source:
            return self._resource_by_source[source_key]
        if digest in self._resource_by_hash:
            resource = self._resource_by_hash[digest]
            if source_key:
                self._resource_by_source[source_key] = resource
            return resource

        clean_name = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(filename).name).strip("-.")
            or "resource.bin"
        )
        archive_name = f"resources/{digest[:12]}-{clean_name}"
        resource = {
            "id": f"res-{digest[:16]}",
            "href": archive_name,
            "media_type": media_type or _media_type(clean_name),
            "data": data,
            "properties": properties,
        }
        self.resources.append(resource)
        self._resource_by_hash[digest] = resource
        if source_key:
            self._resource_by_source[source_key] = resource
        return resource

    @staticmethod
    def _image_has_transparency(image: Image.Image) -> bool:
        if image.mode in {"RGBA", "LA"}:
            alpha = image.getchannel("A")
            return alpha.getextrema()[0] < 255
        return image.mode == "P" and "transparency" in image.info

    @classmethod
    def _image_is_low_color(cls, image: Image.Image) -> bool:
        mode = "RGBA" if cls._image_has_transparency(image) else "RGB"
        return image.convert(mode).getcolors(maxcolors=257) is not None

    def _encode_webp(self, image: Image.Image, filename: str) -> bytes:
        if not features.check("webp"):
            raise ValueError(
                "WebP image export was requested, but this Pillow build has no WebP support."
            )

        frames = []
        durations = []
        for frame in ImageSequence.Iterator(image):
            converted = frame.convert(
                "RGBA" if self._image_has_transparency(frame) else "RGB"
            )
            frames.append(converted.copy())
            durations.append(
                int(frame.info.get("duration", image.info.get("duration", 0)) or 0)
            )
        if not frames:
            raise ValueError(
                f"Image resource {filename!r} contains no readable frames."
            )

        lossless = self._image_has_transparency(image) or self._image_is_low_color(
            image
        )
        output = io.BytesIO()
        save_options = {
            "format": "WEBP",
            "quality": WEBP_QUALITY,
            "method": 6,
            "lossless": lossless,
        }
        if len(frames) > 1:
            save_options.update(
                {
                    "save_all": True,
                    "append_images": frames[1:],
                    "duration": durations,
                    "loop": int(image.info.get("loop", 0) or 0),
                }
            )
        frames[0].save(output, **save_options)
        return output.getvalue()

    @staticmethod
    def _icc_color_space(profile: object) -> tuple[bytes, str]:
        if (
            not isinstance(profile, bytes)
            or len(profile) > JPEG_MODE_MAX_ICC_PROFILE_BYTES
        ):
            raise ValueError(
                "The image ICC profile is invalid or too large to preserve."
            )
        try:
            opened_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            color_space = (
                str(getattr(opened_profile.profile, "xcolor_space", "")).strip().upper()
            )
        except Exception as exc:
            raise ValueError(
                "The image ICC profile is invalid and cannot be preserved."
            ) from exc
        if color_space not in {"RGB", "GRAY", "CMYK"}:
            raise ValueError(
                f"The image ICC profile color space {color_space!r} is unsupported."
            )
        return profile, color_space

    @staticmethod
    def _icc_matches_image_mode(image: Image.Image, color_space: str) -> bool:
        mode = image.mode
        return (
            (color_space == "RGB" and mode in {"RGB", "RGBA", "P"})
            or (color_space == "GRAY" and mode in {"1", "L", "LA"})
            or (color_space == "CMYK" and mode == "CMYK")
        )

    @staticmethod
    def _jpeg_mode_has_transparency(image: Image.Image) -> bool:
        if image.mode in {"RGBA", "LA"}:
            alpha = image.getchannel("A")
        elif image.info.get("transparency") is not None:
            alpha = image.convert("RGBA").getchannel("A")
        else:
            return False
        return cast(tuple[int, int], alpha.getextrema())[0] < 255

    @staticmethod
    def _encode_profiled_jpeg(
        image: Image.Image, profile: bytes, color_space: str
    ) -> bytes:
        if color_space == "RGB":
            converted = image.convert("RGB")
        elif color_space == "GRAY":
            converted = image.convert("L")
        else:
            converted = image.copy()
        output = io.BytesIO()
        try:
            converted.save(
                output,
                "JPEG",
                quality=JPEG_MODE_QUALITY,
                optimize=True,
                progressive=True,
                icc_profile=profile,
                subsampling=0,
            )
        except Exception as exc:
            raise ValueError(
                "Explicit JPEG mode could not encode the ICC-profiled image with Pillow."
            ) from exc
        return output.getvalue()

    @staticmethod
    def _encode_lossless_png(
        image: Image.Image,
        *,
        has_transparency: bool,
        profile: bytes | None,
        color_space: str | None,
    ) -> bytes:
        if profile is None or color_space == "RGB":
            converted = image.convert("RGBA" if has_transparency else "RGB")
        elif color_space == "GRAY":
            converted = image.convert("LA" if has_transparency else "L")
        else:
            raise ValueError(
                "Explicit JPEG mode cannot preserve this ICC profile in a PNG conversion."
            )
        output = io.BytesIO()
        try:
            converted.save(output, "PNG", optimize=True, icc_profile=profile)
        except Exception as exc:
            raise ValueError(
                "Explicit JPEG mode could not preserve the transparent image as PNG."
            ) from exc
        return output.getvalue()

    @staticmethod
    def _encode_jpegli(image: Image.Image) -> bytes:
        try:
            encoder = cast(
                Callable[..., bytes],
                getattr(import_module("pyjpegli"), "encode", None),
            )
        except (ImportError, OSError) as exc:
            raise ValueError(
                "Explicit JPEG mode requires the pyjpegli==0.3.2 encoder."
            ) from exc
        rgb = image.convert("RGB")
        try:
            encoded = encoder(
                rgb.tobytes(), rgb.width, rgb.height, quality=JPEG_MODE_QUALITY
            )
        except Exception as exc:
            raise ValueError(
                "Explicit JPEG mode failed while encoding with pyjpegli==0.3.2."
            ) from exc
        if not isinstance(encoded, bytes) or not encoded:
            raise ValueError(
                "Explicit JPEG mode received no JPEG data from pyjpegli==0.3.2."
            )
        return encoded

    def _normalize_explicit_jpeg(
        self,
        data: bytes,
        filename: str,
        media_type: str,
        max_edge: int,
    ) -> tuple[bytes, str, str]:
        if media_type == "image/svg+xml":
            return data, filename, media_type
        if max_edge < 1:
            raise ValueError(
                "Explicit JPEG mode requires a positive maximum image edge."
            )
        try:
            with Image.open(io.BytesIO(data)) as opened:
                if getattr(opened, "is_animated", False):
                    return data, filename, media_type
                if media_type == "image/jpeg" and max(opened.size) <= max_edge:
                    return data, filename, media_type
                if opened.width * opened.height > JPEG_MODE_MAX_DECODED_PIXELS:
                    raise ValueError(
                        "Explicit JPEG mode rejects images above the 40-megapixel decode limit."
                    )

                raw_profile = opened.info.get("icc_profile")
                profile: bytes | None = None
                profile_space: str | None = None
                if raw_profile is not None:
                    try:
                        profile, profile_space = self._icc_color_space(raw_profile)
                    except ValueError:
                        if media_type in CORE_IMAGE_MEDIA_TYPES:
                            return data, filename, media_type
                        raise
                    if not self._icc_matches_image_mode(opened, profile_space):
                        if media_type in CORE_IMAGE_MEDIA_TYPES:
                            return data, filename, media_type
                        raise ValueError(
                            "Explicit JPEG mode cannot preserve the image ICC profile with its pixel mode."
                        )

                opened.load()
                image = ImageOps.exif_transpose(opened)
                has_transparency = self._jpeg_mode_has_transparency(image)
                if has_transparency:
                    if media_type in {"image/png", "image/gif"}:
                        return data, filename, media_type
                    png = self._encode_lossless_png(
                        image,
                        has_transparency=True,
                        profile=profile,
                        color_space=profile_space,
                    )
                    return (
                        png,
                        f"{Path(filename).stem or 'image'}.png",
                        "image/png",
                    )

                if image.mode == "CMYK" and profile is None:
                    if media_type in CORE_IMAGE_MEDIA_TYPES:
                        return data, filename, media_type
                    raise ValueError(
                        "Explicit JPEG mode cannot convert an unprofiled CMYK image without losing color data."
                    )

                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                if profile is not None and profile_space is not None:
                    jpeg = self._encode_profiled_jpeg(image, profile, profile_space)
                else:
                    jpeg = self._encode_jpegli(image)
                return (
                    jpeg,
                    f"{Path(filename).stem or 'image'}.jpg",
                    "image/jpeg",
                )
        except ValueError:
            raise
        except Image.DecompressionBombError as exc:
            raise ValueError(
                "Explicit JPEG mode rejects images above the 40-megapixel decode limit."
            ) from exc
        except Exception as exc:
            raise ValueError(
                f"Unsupported EPUB image resource {filename!r} ({media_type}) for explicit JPEG mode."
            ) from exc

    def _normalize_image_payload(
        self,
        data: bytes,
        filename: str,
        media_type: str,
        *,
        photographic: bool = False,
        max_edge: int = 2400,
    ) -> tuple[bytes, str, str]:
        if self.image_mode == "jpeg":
            return self._normalize_explicit_jpeg(data, filename, media_type, max_edge)
        if (
            self._publishing_v2
            and self.image_mode == "auto"
            and self.options.get("image_policy") == "balanced"
            and photographic
            and media_type in {"image/png", "image/jpeg"}
        ):
            return self._optimize_photograph(data, filename, media_type, max_edge)
        if self.image_mode == "auto" and media_type in CORE_IMAGE_MEDIA_TYPES:
            return data, filename, media_type
        if self.image_mode == "webp" and media_type in {"image/svg+xml", "image/webp"}:
            return data, filename, media_type
        try:
            with Image.open(io.BytesIO(data)) as image:
                if not getattr(image, "is_animated", False):
                    image.load()
                if self.image_mode == "webp":
                    return (
                        self._encode_webp(image, filename),
                        f"{Path(filename).stem or 'image'}.webp",
                        "image/webp",
                    )

                image.seek(0)
                use_png = self._image_has_transparency(
                    image
                ) or self._image_is_low_color(image)
                output = io.BytesIO()
                if use_png:
                    converted = image.convert(
                        "RGBA" if self._image_has_transparency(image) else "RGB"
                    )
                    converted.save(output, format="PNG", optimize=True)
                    return (
                        output.getvalue(),
                        f"{Path(filename).stem or 'image'}.png",
                        "image/png",
                    )

                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=JPEG_QUALITY,
                    optimize=True,
                    progressive=True,
                )
                return (
                    output.getvalue(),
                    f"{Path(filename).stem or 'image'}.jpg",
                    "image/jpeg",
                )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                f"Unsupported EPUB image resource {filename!r} ({media_type})."
            ) from exc

    def _optimize_photograph(
        self, data: bytes, filename: str, media_type: str, max_edge: int
    ) -> tuple[bytes, str, str]:
        try:
            with Image.open(io.BytesIO(data)) as opened:
                if opened.width * opened.height > 40_000_000:
                    raise ValueError("EPUB photograph exceeds the decoded pixel limit.")
                if getattr(
                    opened, "is_animated", False
                ) or self._image_has_transparency(opened):
                    return data, filename, media_type
                if media_type == "image/jpeg" and max(opened.size) <= max_edge:
                    return data, filename, media_type
                opened.load()
                if media_type == "image/png" and self._image_is_low_color(opened):
                    return data, filename, media_type
                image = ImageOps.exif_transpose(opened).convert("RGB")
                resized = max(image.size) > max_edge
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                color_profile = opened.info.get("icc_profile")
                profile_options = (
                    {"icc_profile": color_profile}
                    if isinstance(color_profile, bytes) and len(color_profile) <= 65536
                    else {}
                )
                image.save(
                    output,
                    "JPEG",
                    quality=88,
                    subsampling=0,
                    optimize=True,
                    progressive=True,
                    **profile_options,
                )
                optimized = output.getvalue()
                if not resized and len(optimized) >= len(data):
                    return data, filename, media_type
                return optimized, f"{Path(filename).stem or 'image'}.jpg", "image/jpeg"
        except Image.DecompressionBombError as exc:
            raise ValueError(
                "EPUB photograph exceeds the decoded pixel limit."
            ) from exc
        except OSError:
            # Optimization is optional; an encoder failure must not discard a
            # usable core-format resource or prevent an otherwise valid export.
            return data, filename, media_type

    def _package_image(
        self, source: str, *, photographic: bool = False, cover: bool = False
    ) -> str:
        if source.startswith("data:image/"):
            match = re.fullmatch(
                r"data:(image/[A-Za-z0-9.+-]+);base64,(.+)", source, flags=re.DOTALL
            )
            if not match:
                raise ValueError("Unsupported image data URL in document output.")
            media_type, encoded = match.groups()
            data = base64.b64decode(encoded, validate=True)
            generated_svg = (
                media_type == "image/svg+xml" and source in self._generated_svg_sources
            )
            if (
                media_type not in SAFE_INPUT_IMAGE_MEDIA_TYPES and not generated_svg
            ) or len(data) > MAX_INPUT_IMAGE_BYTES:
                raise ValueError("Embedded image exceeds the EPUB safety policy.")
            if generated_svg:
                data = validate_svg(data)
            if len(data) > MAX_INPUT_IMAGE_BYTES:
                raise ValueError("Embedded image exceeds the EPUB safety policy.")
            filename = (
                "diagram.svg"
                if generated_svg
                else f"embedded{mimetypes.guess_extension(media_type) or '.bin'}"
            )
        else:
            parsed = urlparse(source)
            if parsed.scheme in {"http", "https"} or source.startswith("//"):
                raise ValueError(
                    f"Remote image resources are not supported in EPUB export: {source}"
                )
            path = self._resolve_local_resource(source)
            if path is None:
                raise FileNotFoundError(
                    f"Image referenced by the document could not be resolved for EPUB export: {source}"
                )
            data = path.read_bytes()
            filename = path.name
            media_type = _media_type(path.name)
            if (
                len(data) > MAX_INPUT_IMAGE_BYTES
                or media_type not in SAFE_INPUT_IMAGE_MEDIA_TYPES
            ):
                raise ValueError("Image exceeds the EPUB safety policy.")

        self._source_image_sizes[hashlib.sha256(data).hexdigest()] = len(data)
        data, filename, media_type = self._normalize_image_payload(
            data,
            filename,
            media_type,
            photographic=photographic,
            max_edge=1800 if cover else 2400,
        )
        source_key = (
            f"{source}::photo-{cover}"
            if photographic and self._publishing_v2
            else source
        )
        resource = self._register_resource(
            data,
            filename,
            source_key=source_key,
            media_type=media_type,
        )
        self._image_sizes[resource["href"]] = len(data)
        return resource["href"]

    def _apply_cover(self, soup: BeautifulSoup) -> None:
        if not self.cover:
            return
        from .epub_cover import render_pdf_cover, render_title_cover

        if self.cover["mode"] == "generated":
            data = render_title_cover(self.metadata, title=self.title)
        elif self.cover_image is not None:
            data = self.cover_image
        else:
            from .cover_source import owned_pdf_source

            data = render_pdf_cover(
                owned_pdf_source(self.document),
                cast(int, self.cover["page"]),
                crop=cast(list[float] | None, self.cover.get("crop")),
            )

        if self.options:
            image = project_selected_cover(soup, self.metadata, title=self.title)
            image["src"] = "data:image/png;base64," + base64.b64encode(data).decode(
                "ascii"
            )
            self.metadata.pop("cover_image_id", None)
            return

        hint = str(self.metadata.get("cover_image_id") or "").strip()
        covers = []
        for image in soup.find_all("img"):
            source = str(image.get("src") or "")
            marked = any(
                "cover" in (node.get("class") or [])
                or node.get("role") == "doc-cover"
                or "cover" in str(node.get("epub:type") or "").split()
                for node in [image, *image.parents]
                if isinstance(node, Tag)
            )
            if marked or (
                hint
                and hint
                in {
                    str(image.get("id") or ""),
                    source,
                    Path(urlparse(source).path).name,
                    Path(urlparse(source).path).stem,
                }
            ):
                covers.append(image)
        if len(covers) > 1:
            raise ValueError("The document contains more than one designated cover.")
        image = covers[0].extract() if covers else soup.new_tag("img")
        for attribute in ("width", "height", "role", "epub:type"):
            image.attrs.pop(attribute, None)
        image["class"] = "cover"
        image["alt"] = self.title + " — cover"
        image["src"] = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        figure = soup.new_tag("figure", attrs={"class": "cover", "epub:type": "cover"})
        figure.append(image)
        container = soup.find("main") or soup
        container.insert(0, figure)
        self.metadata.pop("cover_image_id", None)

    def _sanitize_content(self) -> BeautifulSoup:
        self._generated_svg_sources.clear()
        raw_content = self._raw_content()
        self._input_sha256 = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        soup = BeautifulSoup(raw_content or "<p></p>", "html.parser")
        if self.options:
            self._language = publication_language(self.metadata, soup)
            source_root = soup.find("html")
            source_direction = str(source_root.get("dir") or "") if source_root else ""
            source_language = publication_language({}, soup)
            edition_language_changed = (
                self._language != "und" and self._language != source_language
            )
            choice = str(self.options.get("direction") or "source")
            self._direction = (
                choice
                if choice in {"ltr", "rtl"}
                else source_direction
                if source_direction in {"ltr", "rtl"} and not edition_language_changed
                else "rtl"
                if self._language.split("-", 1)[0]
                in {"ar", "fa", "he", "ur", "ps", "dv"}
                else "ltr"
            )
            assign_review_anchors(soup)
        self._document_root_attrs = _safe_wrapper_attrs(soup.find("html"))
        self._document_body_attrs = _safe_wrapper_attrs(soup.find("body"))
        self._content_root_attrs = _safe_wrapper_attrs(soup.find("main"))
        body = soup.body
        if body is not None:
            soup = BeautifulSoup(
                "".join(str(child) for child in body.children), "html.parser"
            )

        if self.options:
            # The source html/body wrappers do not survive the reading-section
            # split. Keep their link destinations in the first reading section.
            anchor_container = soup.find("main") or soup
            for attrs in (self._document_root_attrs, self._document_body_attrs):
                if attrs.get("id"):
                    anchor = soup.new_tag(
                        "span",
                        attrs={"id": str(attrs["id"]), "class": "epub-wrapper-anchor"},
                    )
                    anchor_container.insert(0, anchor)

        self._apply_cover(soup)
        active_options = apply_front_matter(soup, self.metadata, self.options)

        source_is_pdf = (
            str(getattr(self.document, "file_path", "") or "").lower().endswith(".pdf")
        )
        # PDF printed TOCs are metadata evidence only. Ignore even a legacy
        # explicit include request; reader navigation is generated from the
        # canonical heading anchors below. Non-PDF callers retain their
        # existing presentation choice.
        include_source_toc = (
            False
            if source_is_pdf
            else self.include_source_toc
            if self.include_source_toc is not None
            else True
        )
        if not include_source_toc:
            from .parsers.pdf_utils import remove_source_toc_blocks

            remove_source_toc_blocks(soup)

        for tag in list(soup.find_all(True)):
            name = tag.name.lower()
            if name in REMOVE_WITH_CONTENTS:
                tag.decompose()
                continue
            if name not in SAFE_CONTENT_TAGS:
                tag.unwrap()
                continue

            allowed_attrs = GLOBAL_ATTRS | TAG_ATTRS.get(name, set())
            for attr_name in list(tag.attrs):
                lowered = str(attr_name).lower()
                if (
                    lowered.startswith("on")
                    or lowered == "style"
                    or lowered not in allowed_attrs
                ):
                    del tag.attrs[attr_name]

            if tag.name == "a":
                href = str(tag.get("href") or "").strip()
                if href.lower().startswith(("javascript:", "vbscript:", "data:")):
                    del tag.attrs["href"]

            if tag.name == "img":
                self._has_images = True
                alt = str(tag.get("alt") or "").strip()
                if not alt:
                    tag["alt"] = ""
                    self._all_images_have_alt = False
                source = str(tag.get("src") or "").strip()
                if not source:
                    raise ValueError("An image in the document has no source.")
                is_cover = is_cover_image(tag, self.metadata)
                photographic = str(tag.get("id") or "") in cast(
                    list[str], self.options.get("photographic_images", [])
                ) or (is_cover and self.cover.get("mode") != "generated")
                tag["src"] = self._package_image(
                    source, photographic=photographic, cover=is_cover
                )
                resource = next(
                    (item for item in self.resources if item["href"] == tag["src"]),
                    None,
                )
                cover_hint = str(self.metadata.get("cover_image_id") or "").strip()
                cover_candidates = {
                    str(tag.get("id") or ""),
                    source,
                    Path(urlparse(source).path).name,
                    Path(urlparse(source).path).stem,
                }
                explicit_cover = (
                    "cover" in (tag.get("class") or [])
                    or str(tag.get("role") or "").lower() == "doc-cover"
                    or "cover" in str(tag.get("epub:type") or "").split()
                )
                has_cover = any(
                    "cover-image" in str(item.get("properties") or "").split()
                    for item in self.resources
                )
                if (
                    resource
                    and not has_cover
                    and (
                        explicit_cover
                        or (cover_hint and cover_hint in cover_candidates)
                    )
                ):
                    resource["properties"] = "cover-image"

        if self.options:
            # XHTML permits a single figure caption only at an edge. Adding
            # extended descriptions can leave a retained caption in the middle.
            for figure in soup.find_all("figure"):
                captions = figure.find_all("figcaption", recursive=False)
                if len(captions) > 1:
                    raise ValueError("EPUB figure has multiple captions.")
                if captions:
                    caption = captions[0]
                    children = [
                        node
                        for node in figure.children
                        if isinstance(node, Tag)
                        or (
                            isinstance(node, NavigableString)
                            and not isinstance(node, Comment)
                            and str(node).strip()
                        )
                    ]
                    if children[0] is not caption and children[-1] is not caption:
                        caption.extract()
                        figure.insert(0, caption)
        normalize_export_note_roles(soup)
        self._has_text = bool(soup.get_text(" ", strip=True))
        validate_semantic_export(soup, export_name="EPUB export")
        if self.options:
            self._structure = apply_epub_structure(soup, self.metadata, active_options)
            if self._publishing_v2:
                for table in soup.find_all("table"):
                    rows = table.find_all("tr")
                    if rows and all(
                        len(row.find_all(["th", "td"], recursive=False)) == 2
                        and not row.select("[colspan], [rowspan]")
                        for row in rows
                    ):
                        table["class"] = " ".join(
                            [
                                *sorted(note_tokens(table, "class")),
                                "epub-two-column-table",
                            ]
                        )
                        cells = [
                            row.find_all(["th", "td"], recursive=False)[-1]
                            for row in rows
                        ]
                        if all(
                            re.fullmatch(r"[\d\s.,%+−–-]+", cell.get_text(strip=True))
                            for cell in cells
                        ):
                            for cell in cells:
                                cell["class"] = " ".join(
                                    [*sorted(note_tokens(cell, "class")), "epub-number"]
                                )
            project_publication_notes(
                soup, self.metadata, self.options, self._structure, self._language
            )
            diagnose_publication(soup, self._structure, self.options, self._language)
        self._normalize_ids(soup)
        if not self.options:
            place_chapter_notes(soup, self.metadata)
        normalize_export_note_roles(soup)
        validate_semantic_export(soup, export_name="EPUB export")
        figure_ids = [
            str(figure["id"]) for figure in soup.select("figure.mermaid-diagram-v1[id]")
        ]
        if figure_ids:
            projected = BeautifulSoup(
                project_diagrams_html(
                    str(soup),
                    default_language=publication_language(self.metadata, soup),
                ),
                "html.parser",
            )
            for figure_id in figure_ids:
                figure = projected.find("figure", id=figure_id)
                if figure is None or "diagram-rendered-v1" not in figure.get(
                    "class", []
                ):
                    raise ValueError("EPUB diagram projection failed")
                image = figure.find("img")
                if image is None:
                    raise ValueError("EPUB diagram projection failed")
                source = str(image.get("src") or "")
                if not source.startswith("data:image/svg+xml;base64,"):
                    raise ValueError("EPUB diagram projection failed")
                self._generated_svg_sources.add(source)
                image["src"] = self._package_image(source)
                self._has_images = True
                if not str(image.get("alt") or "").strip():
                    image["alt"] = ""
                    self._all_images_have_alt = False
            soup = projected
        if self.options:
            self._quality_soup = soup
            # Diagram alternatives come from validated topology and retained
            # wording. Generated alternatives do not imply human review.
            self._all_images_have_alt = all(
                bool(str(image.get("alt") or "").strip())
                or image.get("role") in {"none", "presentation"}
                for image in soup.find_all("img")
            )
        return soup

    def _normalize_ids(self, soup: BeautifulSoup) -> None:
        seen_original: set[str] = set()
        # Reserve stable ASCII ID families before ordinary EPUB normalization.
        # Independently normalizing a Unicode figure ID and its label IDs can
        # strip different suffixes and sever the Mermaid placeholder binding.
        diagram_ids: dict[str, str] = {}
        for figure in soup.select("figure.mermaid-diagram-v1"):
            original = str(figure["id"])
            digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:20]
            normalized = f"diagram-{digest}"
            diagram_ids[original] = normalized
            for label in figure.select("div.mermaid-labels > p"):
                label_id = str(label["id"])
                key = label_id[len(original) + 2 :]
                diagram_ids[label_id] = f"{normalized}--{key}"
        if len(set(diagram_ids.values())) != len(diagram_ids):
            raise ValueError("EPUB diagram identifier collision")
        seen_normalized: set[str] = set(diagram_ids.values())
        id_map: dict[str, str] = {}

        for index, tag in enumerate(soup.find_all(id=True), start=1):
            original = str(tag.get("id") or "").strip()
            if not original:
                del tag.attrs["id"]
                continue
            if original in seen_original:
                raise ValueError(f"Duplicate HTML id in EPUB content: {original}")
            seen_original.add(original)
            if original in diagram_ids:
                normalized = diagram_ids[original]
                tag["id"] = normalized
                id_map[original] = normalized
                continue
            base = _safe_id(original, f"id-{index}")
            normalized = base
            suffix = 2
            while normalized in seen_normalized:
                normalized = f"{base}-{suffix}"
                suffix += 1
            tag["id"] = normalized
            seen_normalized.add(normalized)
            id_map[original] = normalized

        for tag in soup.find_all(True):
            anchor_name = str(tag.get("name") or "")
            if anchor_name in id_map:
                tag["name"] = id_map[anchor_name]
            cite = str(tag.get("cite") or "")
            if cite.startswith("#"):
                target = unquote(cite[1:])
                if target in id_map:
                    tag["cite"] = f"#{id_map[target]}"
            for attribute in ("aria-labelledby", "aria-describedby", "headers"):
                raw_value = tag.get(attribute)
                value = (
                    " ".join(str(token) for token in raw_value)
                    if isinstance(raw_value, list)
                    else str(raw_value or "")
                )
                if value:
                    tag[attribute] = " ".join(
                        id_map.get(token, token) for token in value.split()
                    )

        if self.options:
            for attrs in (self._document_root_attrs, self._document_body_attrs):
                for attribute in ("aria-labelledby", "aria-describedby"):
                    raw = attrs.get(attribute)
                    value = (
                        " ".join(str(token) for token in raw)
                        if isinstance(raw, list)
                        else str(raw or "")
                    )
                    if value:
                        attrs[attribute] = " ".join(
                            id_map.get(token, token) for token in value.split()
                        )

        for index, heading in enumerate(
            soup.find_all(re.compile(r"^h[1-6]$")), start=1
        ):
            if heading.get("id"):
                continue
            base = _safe_id(heading.get_text(" ", strip=True), f"heading-{index}")
            heading_id = base
            suffix = 2
            while heading_id in seen_normalized:
                heading_id = f"{base}-{suffix}"
                suffix += 1
            heading["id"] = heading_id
            seen_normalized.add(heading_id)

        final_ids = {str(tag.get("id")) for tag in soup.find_all(id=True)}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if not href:
                del anchor.attrs["href"]
                continue
            if href.startswith("#"):
                original_target = unquote(href[1:])
                target = id_map.get(original_target, original_target)
                if target not in final_ids:
                    raise ValueError(f"Unresolved internal EPUB link target: {href}")
                anchor["href"] = f"#{target}"
                continue
            parsed = urlparse(href)
            if href.startswith("//"):
                raise ValueError(
                    f"Native EPUB content contains an unsupported external link: {href}"
                )
            if not parsed.scheme:
                raise ValueError(
                    f"Native EPUB content contains an unsupported local link: {href}"
                )
            if parsed.scheme.lower() not in SAFE_EXTERNAL_LINK_SCHEMES:
                raise ValueError(
                    f"Native EPUB content contains an unsafe link scheme: {parsed.scheme}"
                )

    def _content_blocks(self, soup: BeautifulSoup) -> list:
        meaningful = [
            child
            for child in soup.contents
            if not isinstance(child, Comment)
            and (not isinstance(child, NavigableString) or str(child).strip())
        ]
        if (
            len(meaningful) == 1
            and isinstance(meaningful[0], Tag)
            and meaningful[0].name == "main"
        ):
            main = meaningful[0]
            self._content_root_attrs = _safe_wrapper_attrs(main)
            if self.options and main.get("id"):
                main.insert(
                    0,
                    soup.new_tag(
                        "span",
                        attrs={"id": str(main["id"]), "class": "epub-wrapper-anchor"},
                    ),
                )
            return [
                child
                for child in main.contents
                if not isinstance(child, NavigableString) or str(child).strip()
            ]

        self._content_root_attrs = {}
        for main in soup.find_all("main"):
            # The XHTML serializer supplies the document's outer <main>.
            # Retained source wrappers therefore need a neutral element.
            main.name = "div"
        return meaningful

    def _partition_blocks(self, blocks: list) -> list[list]:
        chunks: list[list] = []
        current: list = []
        current_size = 0
        for block in blocks:
            block_size = len(str(block).encode("utf-8"))
            if (
                (self.cover or self.options)
                and isinstance(block, Tag)
                and "cover" in str(block.get("epub:type") or "").split()
            ):
                if self.options and all(
                    isinstance(item, Tag)
                    and "epub-wrapper-anchor" in (item.get("class") or [])
                    for item in current
                ):
                    chunks.append([*current, block])
                else:
                    if current:
                        chunks.append(current)
                    chunks.append([block])
                current = []
                current_size = 0
                continue
            starts_section = isinstance(block, Tag) and (
                block.get("data-epub-role")
                in {
                    "titlepage",
                    "copyright-page",
                    "contributors",
                    "introduction",
                    "part",
                    "chapter",
                    "endnotes",
                    "appendix",
                    "bibliography",
                    "backmatter",
                    "bodymatter",
                }
                | ({"toc"} if self._publishing_v2 else set())
                if self.options
                else block.name in {"h1", "h2"}
            )
            if starts_section and current:
                # A source-page anchor immediately before a heading belongs
                # with that heading, rather than an empty or preceding chapter.
                markers = []
                marker_size = 0
                while current:
                    marker = current[-1]
                    if not (
                        isinstance(marker, Tag)
                        and marker.name == "span"
                        and not marker.contents
                        and (
                            str(marker.get("role") or "").lower() == "doc-pagebreak"
                            or "pagebreak" in str(marker.get("epub:type") or "").split()
                            or (
                                self.options
                                and "epub-wrapper-anchor" in (marker.get("class") or [])
                            )
                        )
                    ):
                        break
                    markers.append(current.pop())
                    marker_size += len(str(marker).encode("utf-8"))
                if current:
                    chunks.append(current)
                current = list(reversed(markers))
                current_size = marker_size
            if current and current_size + block_size > MAX_CONTENT_DOCUMENT_BYTES:
                chunks.append(current)
                current = []
                current_size = 0
            current.append(block)
            current_size += block_size
        if current:
            chunks.append(current)
        return chunks or [[BeautifulSoup("<p></p>", "html.parser").p]]

    def _append_soup_node(self, parent: ET.Element, node) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            text = str(node)
            if len(parent):
                parent[-1].tail = (parent[-1].tail or "") + text
            else:
                parent.text = (parent.text or "") + text
            return
        if not isinstance(node, Tag):
            return

        element = ET.SubElement(parent, f"{{{XHTML_NS}}}{node.name}")
        for raw_name, raw_value in node.attrs.items():
            value = (
                " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            )
            if raw_name == "epub:type":
                attr_name = f"{{{EPUB_NS}}}type"
            elif raw_name == "xml:lang":
                attr_name = f"{{{XML_NS}}}lang"
            else:
                attr_name = str(raw_name)
            element.set(attr_name, value)
        for child in node.contents:
            self._append_soup_node(element, child)

    def _local_wrapper_attrs(
        self, attrs: dict[str, object], soup: BeautifulSoup, document_title: str
    ) -> dict[str, object]:
        if not self.options:
            return attrs
        local_ids = {str(tag["id"]) for tag in soup.find_all(id=True)}
        localized = dict(attrs)
        for name in ("aria-labelledby", "aria-describedby"):
            raw = localized.get(name)
            value = " ".join(raw) if isinstance(raw, list) else str(raw or "")
            if value and any(
                identifier not in local_ids for identifier in value.split()
            ):
                localized.pop(name)
                if name == "aria-labelledby":
                    localized.setdefault("aria-label", document_title or self.title)
                else:
                    self._structure.issue(
                        "wrapper-description-needs-review", severity="warning"
                    )
        return localized

    def _serialize_content_document(
        self, soup: BeautifulSoup, document_title: str
    ) -> str:
        language = (
            self._document_root_attrs.get("lang")
            or self._document_root_attrs.get("xml:lang")
            or self.metadata.get("language")
            or "en"
        )
        language = str(language) if SAFE_LANGUAGE_RE.fullmatch(str(language)) else "en"
        if self.options:
            language = self._language
        root = ET.Element(
            f"{{{XHTML_NS}}}html", {"lang": language, f"{{{XML_NS}}}lang": language}
        )
        for raw_name, raw_value in self._local_wrapper_attrs(
            self._document_root_attrs, soup, document_title
        ).items():
            if raw_name == "id" or (
                self.options
                and raw_name in {"lang", "xml:lang", "dir", "role", "epub:type"}
            ):
                continue
            value = (
                " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            )
            if raw_name == "epub:type":
                attr_name = f"{{{EPUB_NS}}}type"
            elif raw_name == "xml:lang":
                attr_name = f"{{{XML_NS}}}lang"
            else:
                attr_name = raw_name
            root.set(attr_name, value)
        if self.options:
            root.set("dir", self._direction)
        head = ET.SubElement(root, f"{{{XHTML_NS}}}head")
        ET.SubElement(head, f"{{{XHTML_NS}}}title").text = document_title or self.title
        ET.SubElement(head, f"{{{XHTML_NS}}}meta", {"charset": "utf-8"})
        cover_figure = soup.find("figure", class_="cover", attrs={"epub:type": "cover"})
        if self._publishing_v2:
            ET.SubElement(
                head,
                f"{{{XHTML_NS}}}link",
                {
                    "rel": "stylesheet",
                    "type": "text/css",
                    "href": self._stylesheet_href,
                },
            )
        elif self.options:
            ET.SubElement(
                head, f"{{{XHTML_NS}}}style", {"type": "text/css"}
            ).text = REFLOW_CSS
        elif (
            self.cover
            and cover_figure is not None
            and cover_figure.find("img") is not None
        ):
            ET.SubElement(head, f"{{{XHTML_NS}}}style", {"type": "text/css"}).text = (
                "body, main { margin: 0; padding: 0; } "
                "figure.cover { margin: 0; text-align: center; } "
                "figure.cover img { max-width: 100%; height: auto; }"
            )
        body = ET.SubElement(root, f"{{{XHTML_NS}}}body")
        for raw_name, raw_value in self._local_wrapper_attrs(
            self._document_body_attrs, soup, document_title
        ).items():
            if raw_name == "id" or (
                self.options
                and raw_name in {"lang", "xml:lang", "dir", "role", "epub:type"}
            ):
                continue
            value = (
                " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            )
            if raw_name == "epub:type":
                attr_name = f"{{{EPUB_NS}}}type"
            elif raw_name == "xml:lang":
                attr_name = f"{{{XML_NS}}}lang"
            else:
                attr_name = raw_name
            body.set(attr_name, value)
        if self._publishing_v2 and cover_figure is not None:
            body.set(
                "class", " ".join([body.get("class", ""), "epub-cover-page"]).strip()
            )
        main = ET.SubElement(body, f"{{{XHTML_NS}}}main")
        for raw_name, raw_value in self._local_wrapper_attrs(
            self._content_root_attrs, soup, document_title
        ).items():
            if raw_name == "id" or (
                self.options
                and raw_name in {"lang", "xml:lang", "dir", "role", "epub:type"}
            ):
                # The source <main> wrapper is reused for every chunk; an id
                # here would become a duplicate EPUB target.
                continue
            value = (
                " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            )
            if raw_name == "epub:type":
                attr_name = f"{{{EPUB_NS}}}type"
            elif raw_name == "xml:lang":
                attr_name = f"{{{XML_NS}}}lang"
            else:
                attr_name = raw_name
            main.set(attr_name, value)
        content_parent = main
        if self.options:
            role_node = soup.find(
                lambda tag: isinstance(tag, Tag) and tag.has_attr("data-epub-role")
            )
            role = str(role_node.get("data-epub-role") or "") if role_node else ""
            if role and not (
                role == "endnotes"
                and soup.find(
                    lambda tag: (
                        "endnotes" in note_tokens(tag, "epub:type")
                        or "doc-endnotes" in note_tokens(tag, "role")
                    )
                )
                is not None
            ):
                attrs = {f"{{{EPUB_NS}}}type": role}
                if self._publishing_v2:
                    attrs["class"] = f"epub-{role}"
                if role in {
                    "chapter",
                    "introduction",
                    "part",
                    "appendix",
                    "bibliography",
                    "endnotes",
                }:
                    attrs["role"] = f"doc-{role}"
                heading = soup.find(re.compile(r"^h[1-6]$"))
                if heading is not None and heading.get("id"):
                    attrs["aria-labelledby"] = str(heading["id"])
                else:
                    attrs["aria-label"] = document_title or self.title
                content_parent = ET.SubElement(main, f"{{{XHTML_NS}}}section", attrs)
        for child in soup.contents:
            self._append_soup_node(content_parent, child)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

    def _build_content_documents(self, soup: BeautifulSoup) -> None:
        raw_chunks = self._partition_blocks(self._content_blocks(soup))
        chunk_soups: list[BeautifulSoup] = []
        for chunk in raw_chunks:
            chunk_soups.append(
                BeautifulSoup("".join(str(block) for block in chunk), "html.parser")
            )

        id_locations: dict[str, str] = {}
        for index, chunk_soup in enumerate(chunk_soups, start=1):
            href = f"content-{index:03d}.xhtml"
            for node in chunk_soup.find_all(id=True):
                node_id = str(node.get("id"))
                if node_id in id_locations:
                    raise ValueError(
                        f"Duplicate HTML id across EPUB content documents: {node_id}"
                    )
                id_locations[node_id] = href

        current_unit_title = self.title
        for index, chunk_soup in enumerate(chunk_soups, start=1):
            href = f"content-{index:03d}.xhtml"
            for tag in chunk_soup.find_all(True):
                if self.options:
                    for attribute in ("aria-labelledby", "aria-describedby", "headers"):
                        raw = tag.get(attribute)
                        value = (
                            " ".join(raw) if isinstance(raw, list) else str(raw or "")
                        )
                        if any(
                            id_locations.get(identifier) != href
                            for identifier in value.split()
                        ):
                            raise ValueError("epub-relationship-cross-document")
                attributes = ("href", "cite") if tag.name == "a" else ("cite",)
                for attribute in attributes:
                    target = str(tag.get(attribute) or "")
                    if target.startswith("#"):
                        target_id = target[1:]
                        target_href = id_locations.get(target_id)
                        if not target_href:
                            raise ValueError(
                                f"Unresolved internal EPUB link target: {target}"
                            )
                        prefix = (
                            ""
                            if attribute == "cite" and target_href == href
                            else target_href
                        )
                        tag[attribute] = f"{prefix}#{target_id}"

            first_heading = chunk_soup.find(re.compile(r"^h[1-6]$"))
            opener = chunk_soup.find(
                lambda tag: isinstance(tag, Tag) and tag.has_attr("data-epub-role")
            )
            if opener is not None:
                opening_heading = (
                    opener
                    if re.fullmatch(r"h[1-6]", opener.name)
                    else opener.find(re.compile(r"^h[1-6]$"))
                )
                if opening_heading is not None:
                    current_unit_title = (
                        publication_heading_text(opening_heading) or current_unit_title
                    )
            chunk_title = (
                (
                    publication_heading_text(first_heading)
                    if self.options
                    else first_heading.get_text(" ", strip=True)
                )
                if first_heading
                else current_unit_title
                if self.options
                else self.title
            )
            for heading in chunk_soup.find_all(re.compile(r"^h[1-6]$")):
                title = (
                    publication_heading_text(heading)
                    if self.options
                    else heading.get_text(" ", strip=True)
                )
                if title and not (
                    self.options
                    and (
                        heading.get("data-epub-nav-skip") == "true"
                        or any(is_doc_endnote(parent) for parent in heading.parents)
                    )
                ):
                    self._toc_entries.append(
                        {
                            "level": int(
                                str(
                                    heading.get("data-epub-nav-level")
                                    or heading.name[1]
                                )
                            ),
                            "title": title,
                            "href": f"{href}#{heading.get('id')}",
                        }
                    )
            for marker in chunk_soup.find_all(
                lambda tag: (
                    isinstance(tag, Tag)
                    and (
                        str(tag.get("role") or "").lower() == "doc-pagebreak"
                        or "pagebreak" in str(tag.get("epub:type") or "").split()
                    )
                )
            ):
                marker_id = str(marker.get("id") or "")
                if marker_id:
                    label = str(
                        marker.get("title")
                        or marker.get("aria-label")
                        or marker.get_text(" ", strip=True)
                        or marker_id
                    )
                    self._page_entries.append(
                        {
                            "label": label,
                            "href": f"{href}#{marker_id}",
                            "printed": marker.get("data-epub-printed-page") == "true",
                        }
                    )
            if self.options:
                for node in chunk_soup.find_all(id=True):
                    role = str(node.get("data-epub-role") or "")
                    if not role and (
                        "cover" in (node.get("class") or [])
                        or "cover" in str(node.get("epub:type") or "").split()
                    ):
                        role = "cover"
                    if role in {
                        "cover",
                        "titlepage",
                        "copyright-page",
                        "toc",
                        "endnotes",
                        "bibliography",
                        "appendix",
                    }:
                        section_heading = (
                            node
                            if re.fullmatch(r"h[1-6]", node.name)
                            else node.find(re.compile(r"^h[1-6]$"))
                        )
                        section_title = (
                            publication_heading_text(section_heading)
                            if section_heading is not None
                            else str(node.get("aria-label") or "")
                        )
                        self._landmarks.setdefault(
                            role,
                            {
                                "href": f"{href}#{node['id']}",
                                "title": section_title
                                or epub_labels(self._language).get(
                                    "notes" if role == "endnotes" else role, self.title
                                ),
                            },
                        )
                    if role in {"chapter", "introduction", "part", "bodymatter"}:
                        self._landmarks.setdefault(
                            "bodymatter",
                            {
                                "href": f"{href}#{node['id']}",
                                "title": epub_labels(self._language)["start"],
                            },
                        )
                # A content document has its own heading hierarchy. Source
                # anchors and words survive this presentation-only adjustment.
                headings = chunk_soup.find_all(re.compile(r"^h[1-6]$"))
                if headings:
                    base = int(headings[0].name[1])
                    for heading in headings:
                        heading.name = (
                            f"h{min(6, max(1, int(heading.name[1]) - base + 1))}"
                        )
            self._content_documents.append(
                {
                    "id": f"content-{index:03d}",
                    "href": href,
                    "xml": self._serialize_content_document(chunk_soup, chunk_title),
                    "role": str(opener.get("data-epub-role") or "")
                    if opener
                    else "cover"
                    if chunk_soup.find("figure", class_="cover")
                    else "",
                }
            )
            if (
                self.options
                and len(self._content_documents[-1]["xml"].encode("utf-8"))
                > MAX_CONTENT_DOCUMENT_BYTES
            ):
                self._structure.issue(
                    "content-document-oversized",
                    str(first_heading.get("id") or "") if first_heading else "",
                    severity="warning",
                )
        if self.options and "bodymatter" not in self._landmarks:
            self._structure.issue("bodymatter-needs-review")

    def _container_xml(self) -> str:
        return """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    def _toc_tree(self) -> list[dict]:
        roots: list[dict] = []
        stack: list[dict] = []
        for entry in self._toc_entries:
            node = {**entry, "children": []}
            while stack and stack[-1]["level"] >= node["level"]:
                stack.pop()
            if stack:
                stack[-1]["children"].append(node)
            else:
                roots.append(node)
            stack.append(node)
        return roots

    def _append_nav_nodes(self, listing: ET.Element, nodes: list[dict]) -> None:
        for node in nodes:
            item = ET.SubElement(listing, f"{{{XHTML_NS}}}li")
            ET.SubElement(item, f"{{{XHTML_NS}}}a", {"href": node["href"]}).text = node[
                "title"
            ]
            if node["children"]:
                nested = ET.SubElement(item, f"{{{XHTML_NS}}}ol")
                self._append_nav_nodes(nested, node["children"])

    def _nav_xhtml(self) -> str:
        language = (
            self._language if self.options else self.metadata.get("language") or "en"
        )
        labels = epub_labels(language)
        root = ET.Element(
            f"{{{XHTML_NS}}}html", {"lang": language, f"{{{XML_NS}}}lang": language}
        )
        head = ET.SubElement(root, f"{{{XHTML_NS}}}head")
        ET.SubElement(head, f"{{{XHTML_NS}}}title").text = self.title
        if self._publishing_v2:
            root.set("dir", self._direction)
            ET.SubElement(
                head,
                f"{{{XHTML_NS}}}link",
                {
                    "rel": "stylesheet",
                    "type": "text/css",
                    "href": self._stylesheet_href,
                },
            )
        elif self.options:
            root.set("dir", self._direction)
            ET.SubElement(
                head, f"{{{XHTML_NS}}}style", {"type": "text/css"}
            ).text = REFLOW_CSS
        body = ET.SubElement(root, f"{{{XHTML_NS}}}body")

        nav = ET.SubElement(
            body, f"{{{XHTML_NS}}}nav", {f"{{{EPUB_NS}}}type": "toc", "id": "toc"}
        )
        label_attrs = {"lang": epub_label_language(language)} if self.options else {}
        if self.options:
            nav.set("role", "doc-toc")
            nav.set("aria-labelledby", "toc-heading")
        ET.SubElement(
            nav,
            f"{{{XHTML_NS}}}h1",
            {**label_attrs, **({"id": "toc-heading"} if self.options else {})},
        ).text = labels["toc"] if self.options else "Contents"
        listing = ET.SubElement(nav, f"{{{XHTML_NS}}}ol")
        toc_tree = self._toc_tree()
        if toc_tree:
            self._append_nav_nodes(listing, toc_tree)
        else:
            item = ET.SubElement(listing, f"{{{XHTML_NS}}}li")
            ET.SubElement(
                item, f"{{{XHTML_NS}}}a", {"href": self._content_documents[0]["href"]}
            ).text = self.title

        if self._page_entries:
            page_nav = ET.SubElement(
                body,
                f"{{{XHTML_NS}}}nav",
                {f"{{{EPUB_NS}}}type": "page-list", "id": "page-list"},
            )
            page_key = (
                "pages"
                if all(entry.get("printed") for entry in self._page_entries)
                else "pdf_pages"
            )
            if self.options:
                page_nav.set("role", "doc-pagelist")
                page_nav.set("aria-labelledby", "page-list-heading")
            ET.SubElement(
                page_nav,
                f"{{{XHTML_NS}}}h2",
                {
                    **label_attrs,
                    **({"id": "page-list-heading"} if self.options else {}),
                },
            ).text = labels[page_key] if self.options else "Pages"
            page_list = ET.SubElement(page_nav, f"{{{XHTML_NS}}}ol")
            for entry in self._page_entries:
                item = ET.SubElement(page_list, f"{{{XHTML_NS}}}li")
                ET.SubElement(
                    item, f"{{{XHTML_NS}}}a", {"href": entry["href"]}
                ).text = entry["label"]

        landmarks = ET.SubElement(
            body, f"{{{XHTML_NS}}}nav", {f"{{{EPUB_NS}}}type": "landmarks"}
        )
        if self.options:
            landmarks.set("aria-labelledby", "landmarks-heading")
        ET.SubElement(
            landmarks,
            f"{{{XHTML_NS}}}h2",
            {**label_attrs, **({"id": "landmarks-heading"} if self.options else {})},
        ).text = labels["landmarks"] if self.options else "Landmarks"
        landmark_list = ET.SubElement(landmarks, f"{{{XHTML_NS}}}ol")
        entries = (
            self._landmarks
            if self.options
            else {
                "bodymatter": {
                    "href": self._content_documents[0]["href"],
                    "title": "Start of content",
                }
            }
        )
        for role, entry in entries.items():
            landmark_item = ET.SubElement(landmark_list, f"{{{XHTML_NS}}}li")
            ET.SubElement(
                landmark_item,
                f"{{{XHTML_NS}}}a",
                {"href": entry["href"], f"{{{EPUB_NS}}}type": role},
            ).text = entry["title"]
        if self.options and not entries:
            # Landmarks are optional; an empty navigation list is not valid.
            body.remove(landmarks)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

    def _add_dc(
        self, metadata: ET.Element, name: str, value, **attrs
    ) -> ET.Element | None:
        if value in (None, "", []):
            return None
        node = ET.SubElement(
            metadata,
            f"{{{DC_NS}}}{name}",
            {key: str(val) for key, val in attrs.items()},
        )
        node.text = str(value)
        return node

    def _add_meta(
        self,
        metadata: ET.Element,
        prop: str,
        value,
        *,
        refines: str | None = None,
        scheme: str | None = None,
    ) -> ET.Element | None:
        if value in (None, "", []):
            return None
        attrs = {"property": prop}
        if refines:
            attrs["refines"] = refines
        if scheme:
            attrs["scheme"] = scheme
        node = ET.SubElement(metadata, f"{{{OPF_NS}}}meta", attrs)
        node.text = str(value)
        return node

    def _add_role_meta(self, metadata: ET.Element, role, person_id: str) -> None:
        normalized = str(role or "contributor").strip().casefold()
        code = MARC_ROLE_CODES.get(normalized)
        if code:
            self._add_meta(
                metadata, "role", code, refines=f"#{person_id}", scheme="marc:relators"
            )
        elif normalized in MARC_ROLE_VALUES:
            self._add_meta(
                metadata,
                "role",
                normalized,
                refines=f"#{person_id}",
                scheme="marc:relators",
            )
        else:
            self._add_meta(metadata, "role", normalized, refines=f"#{person_id}")

    def _pagination_source(self) -> str | None:
        if not self._page_entries:
            return None
        if self.options and not all(
            entry.get("printed") for entry in self._page_entries
        ):
            return str(getattr(self.document, "filename", "") or "").strip() or None
        for key, prefix in (
            ("source", ""),
            ("isbn", "urn:isbn:"),
            ("issn", "urn:issn:"),
        ):
            value = str(self.metadata.get(key) or "").strip()
            if value:
                return (
                    value
                    if not prefix or value.lower().startswith(prefix)
                    else prefix + value
                )
        return str(getattr(self.document, "filename", "") or "").strip() or None

    def _package_opf(self) -> str:
        language = (
            self._language if self.options else self.metadata.get("language") or "en"
        )
        package = ET.Element(
            f"{{{OPF_NS}}}package",
            {
                "version": "3.0",
                "unique-identifier": "pub-id",
                f"{{{XML_NS}}}lang": language,
            },
        )
        metadata = ET.SubElement(package, f"{{{OPF_NS}}}metadata")

        identifiers = get_effective_identifier_entries(
            self.metadata, fallback_document_id=getattr(self.document, "id", None)
        ) or [{"scheme": "UUID", "value": f"urn:uuid:{self.uid}"}]
        for index, identifier in enumerate(identifiers, start=1):
            identifier_id = "pub-id" if index == 1 else f"identifier-{index}"
            self._add_dc(
                metadata, "identifier", identifier.get("value"), id=identifier_id
            )
            scheme = str(identifier.get("scheme") or "").upper()
            onix_code = _onix_identifier_type(scheme, identifier.get("value"))
            if onix_code:
                self._add_meta(
                    metadata,
                    "identifier-type",
                    onix_code,
                    refines=f"#{identifier_id}",
                    scheme="onix:codelist5",
                )
            elif scheme:
                self._add_meta(
                    metadata, "identifier-type", scheme, refines=f"#{identifier_id}"
                )

        self._add_dc(metadata, "title", self.title, id="title-main")
        self._add_meta(metadata, "title-type", "main", refines="#title-main")
        if self.metadata.get("subtitle"):
            self._add_dc(
                metadata, "title", self.metadata["subtitle"], id="title-subtitle"
            )
            self._add_meta(
                metadata, "title-type", "subtitle", refines="#title-subtitle"
            )
        self._add_dc(metadata, "language", language)

        contributors = (
            self.metadata.get("contributors")
            if isinstance(self.metadata.get("contributors"), dict)
            else {}
        )
        contributor_index = 0
        for role_key, dc_name in (
            ("authors", "creator"),
            ("editors", "contributor"),
            ("translators", "contributor"),
        ):
            for person in contributors.get(role_key, []) or []:
                contributor_index += 1
                person_id = f"contributor-{contributor_index}"
                self._add_dc(metadata, dc_name, person, id=person_id)
                self._add_role_meta(metadata, role_key, person_id)
        for entry in contributors.get("other", []) or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            contributor_index += 1
            person_id = f"contributor-{contributor_index}"
            self._add_dc(metadata, "contributor", entry["name"], id=person_id)
            self._add_role_meta(metadata, entry.get("role"), person_id)

        for key, dc_name in (
            ("publisher", "publisher"),
            ("publication_date", "date"),
            ("rights", "rights"),
            ("description", "description"),
            ("publication_type", "type"),
        ):
            self._add_dc(metadata, dc_name, self.metadata.get(key))
        for subject in self.metadata.get("subjects", []) or []:
            self._add_dc(metadata, "subject", subject)

        pagination_source = self._pagination_source()
        source_value = pagination_source or self.metadata.get("source")
        source_node = self._add_dc(
            metadata,
            "source",
            source_value,
            id="pagination-source" if pagination_source else "source",
        )
        if source_node is not None and pagination_source:
            self._add_meta(
                metadata, "source-of", "pagination", refines="#pagination-source"
            )

        if self.metadata.get("journal_or_series"):
            collection_id = "collection-1"
            collection = self._add_meta(
                metadata, "belongs-to-collection", self.metadata["journal_or_series"]
            )
            if collection is not None:
                collection.set("id", collection_id)
                self._add_meta(
                    metadata, "collection-type", "series", refines=f"#{collection_id}"
                )
                self._add_meta(
                    metadata,
                    "group-position",
                    _group_position(
                        self.metadata.get("volume"), self.metadata.get("issue")
                    ),
                    refines=f"#{collection_id}",
                )

        if self._has_text or not self._has_images:
            self._add_meta(metadata, "schema:accessMode", "textual")
        if self._has_images:
            self._add_meta(metadata, "schema:accessMode", "visual")
        self._add_meta(metadata, "schema:accessibilityFeature", "tableOfContents")
        if self._toc_entries:
            self._add_meta(
                metadata, "schema:accessibilityFeature", "structuralNavigation"
            )
        if self._page_entries:
            self._add_meta(metadata, "schema:accessibilityFeature", "pageNavigation")
        reviewed_alternatives = self.options and not any(
            issue["code"] in {"image-description-missing", "image-description-review"}
            for issue in self._structure.issues
        )
        if (
            self._has_images
            and self._all_images_have_alt
            and (not self.options or reviewed_alternatives)
        ):
            self._add_meta(metadata, "schema:accessibilityFeature", "alternativeText")
        sufficient = (
            "textual"
            if not self._has_images or self._all_images_have_alt
            else "textual,visual"
        )
        if not self.options:
            self._add_meta(metadata, "schema:accessModeSufficient", sufficient)
        hazards = (
            self._evaluation["hazards"]
            if self._evaluation
            else ["unknown" if self.options or self._has_images else "none"]
        )
        for hazard in hazards:
            self._add_meta(metadata, "schema:accessibilityHazard", hazard)
        summary_parts = ["The publication contains a linked table of contents."]
        if self._page_entries:
            summary_parts.append("Page navigation for the source PDF is included.")
        if self._has_images:
            summary_parts.append(
                "All images include alternative text."
                if self._all_images_have_alt
                else "Some images require review for complete alternative text."
            )
        else:
            summary_parts.append(
                "The publication contains no embedded audio, video, or scripted content."
            )
        if self.options:
            summary_parts = [
                "A linked table of contents and reflowable text are provided. Accessibility has not been fully evaluated; no WCAG conformance is asserted."
            ]
            if self._page_entries:
                summary_parts.append(
                    "Source page navigation is included; labels may refer to physical PDF pages."
                )
            if any(
                issue["code"].startswith("image-description")
                for issue in self._structure.issues
            ):
                summary_parts.append("Image descriptions require editorial review.")
            if any(
                issue["code"] == "table-headers-missing"
                for issue in self._structure.issues
            ):
                summary_parts.append("Some tables need accessible headers.")
            if self._evaluation:
                summary_parts = [self._evaluation["summary"]]
                for mode in self._evaluation["sufficient_modes"]:
                    self._add_meta(metadata, "schema:accessModeSufficient", mode)
                if self._evaluation["conforms_to"]:
                    conformance = self._add_meta(
                        metadata, "dcterms:conformsTo", self._evaluation["conforms_to"]
                    )
                    if conformance is not None:
                        conformance.set("id", "accessibility-conformance")
                    evaluator = self._add_meta(
                        metadata,
                        "a11y:certifiedBy",
                        self._evaluation["evaluator"],
                        refines="#accessibility-conformance",
                    )
                    if evaluator is not None:
                        evaluator.set("id", "accessibility-evaluator")
                    self._add_meta(
                        metadata,
                        "a11y:certifierCredential",
                        self._evaluation["credential"],
                        refines="#accessibility-evaluator",
                    )
        self._add_meta(metadata, "schema:accessibilitySummary", " ".join(summary_parts))

        modified = (
            datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self._add_meta(metadata, "dcterms:modified", modified)

        manifest = ET.SubElement(package, f"{{{OPF_NS}}}manifest")
        ET.SubElement(
            manifest,
            f"{{{OPF_NS}}}item",
            {
                "id": "nav",
                "href": "nav.xhtml",
                "media-type": "application/xhtml+xml",
                "properties": "nav",
            },
        )
        for content_document in self._content_documents:
            ET.SubElement(
                manifest,
                f"{{{OPF_NS}}}item",
                {
                    "id": content_document["id"],
                    "href": content_document["href"],
                    "media-type": "application/xhtml+xml",
                },
            )
        for resource in self.resources:
            attrs = {
                "id": resource["id"],
                "href": resource["href"],
                "media-type": resource["media_type"],
            }
            if resource.get("properties"):
                attrs["properties"] = resource["properties"]
            ET.SubElement(manifest, f"{{{OPF_NS}}}item", attrs)

        spine = ET.SubElement(package, f"{{{OPF_NS}}}spine")
        if self.options:
            spine.set("page-progression-direction", self._direction)
        nav_added = False
        for content_document in self._content_documents:
            if (
                self._publishing_v2
                and not nav_added
                and content_document.get("role")
                not in {"cover", "titlepage", "copyright-page"}
            ):
                ET.SubElement(spine, f"{{{OPF_NS}}}itemref", {"idref": "nav"})
                nav_added = True
            ET.SubElement(
                spine, f"{{{OPF_NS}}}itemref", {"idref": content_document["id"]}
            )
        if self._publishing_v2 and not nav_added:
            ET.SubElement(spine, f"{{{OPF_NS}}}itemref", {"idref": "nav"})
        return ET.tostring(package, encoding="utf-8", xml_declaration=True).decode(
            "utf-8"
        )

    def _collect_extra_resources(self) -> None:
        settings = getattr(self.document, "settings", None) or {}
        for raw_path in settings.get("epub_resources") or []:
            path = self._allowed_file(Path(str(raw_path)))
            if path is None:
                raise ValueError(
                    f"EPUB extra resource is outside the allowed document roots: {raw_path}"
                )
            media_type = _media_type(path.name)
            if media_type not in ALLOWED_EXTRA_MEDIA_TYPES:
                raise ValueError(
                    f"Unsupported EPUB extra resource type: {path.name} ({media_type})"
                )
            self._register_resource(
                path.read_bytes(),
                path.name,
                source_key=str(path),
                media_type=media_type,
            )

    def build(self) -> Path:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        soup = self._sanitize_content()
        if self._publishing_v2:
            self._stylesheet_href = self._register_resource(
                BOOK_CSS.encode("utf-8"), "book.css", media_type="text/css"
            )["href"]
        self._build_content_documents(soup)
        self._collect_extra_resources()
        nav_xml = self._nav_xhtml()
        if self.options:
            content_fingerprints = {
                entry["href"]: hashlib.sha256(entry["xml"].encode("utf-8")).hexdigest()
                for entry in self._content_documents
            }
            content_fingerprints["nav.xhtml"] = hashlib.sha256(
                nav_xml.encode("utf-8")
            ).hexdigest()
            for resource in self.resources:
                content_fingerprints[resource["href"]] = hashlib.sha256(
                    resource["data"]
                ).hexdigest()
            self._content_sha256 = epub_value_sha256(content_fingerprints)
            evaluation = self.options.get("accessibility_evaluation")
            if isinstance(evaluation, dict):
                if evaluation["content_sha256"] == self._content_sha256 and evaluation[
                    "options_sha256"
                ] == epub_projection_options_sha256(self.options):
                    self._evaluation = evaluation
                else:
                    self._structure.issue(
                        "accessibility-evaluation-stale", severity="warning"
                    )
        package_xml = self._package_opf()

        with zipfile.ZipFile(self.output_path, "w") as archive:
            archive.writestr(
                "mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED
            )
            archive.writestr(
                "META-INF/container.xml",
                self._container_xml(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            archive.writestr(
                "EPUB/package.opf", package_xml, compress_type=zipfile.ZIP_DEFLATED
            )
            archive.writestr(
                "EPUB/nav.xhtml", nav_xml, compress_type=zipfile.ZIP_DEFLATED
            )
            for content_document in self._content_documents:
                archive.writestr(
                    f"EPUB/{content_document['href']}",
                    content_document["xml"],
                    compress_type=zipfile.ZIP_DEFLATED,
                )
            for resource in self.resources:
                archive.writestr(
                    f"EPUB/{resource['href']}",
                    resource["data"],
                    compress_type=zipfile.ZIP_DEFLATED,
                )
        return self.output_path

    def quality_report(
        self,
        *,
        structure: str = "not_run",
        epubcheck: str = "not_run",
        epubcheck_version: str | None = None,
        ace: str = "not_run",
        ace_version: str | None = None,
    ) -> dict[str, object] | None:
        """Return a byte-bound report only for the explicitly selected profile."""

        if not self.options or self._quality_soup is None:
            return None
        report = make_quality_report(
            self._quality_soup,
            self._structure,
            input_sha256=self._input_sha256,
            options_sha256=epub_value_sha256(self.options),
            artifact_sha256=hashlib.sha256(self.output_path.read_bytes()).hexdigest(),
            content_sha256=self._content_sha256,
            validation=structure,
            epubcheck=epubcheck,
            epubcheck_version=epubcheck_version,
            profile=str(self.options["profile"]),
            ace=ace,
            ace_version=ace_version,
        )
        checks = report.get("checks")
        if self._publishing_v2:
            report["image_bytes"] = {
                "source": sum(self._source_image_sizes.values()),
                "packaged": sum(self._image_sizes.values()),
            }
        if self._evaluation and isinstance(checks, dict):
            checks["manual"] = "reviewed"
        return report
