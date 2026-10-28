"""Owned image-crop helpers for PDF extraction and completed-document review."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import stat
import uuid
from collections.abc import Callable, MutableMapping
from contextlib import suppress
from io import BufferedReader
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from PIL import Image

logger = logging.getLogger(__name__)

_BBOX_RE = re.compile(
    r"\[?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]?"
)
_SOURCE_IMAGE_NAME_RE = re.compile(r"image_source_[0-9a-f]{64}\.png\Z")
_COPY_CHUNK_SIZE = 1024 * 1024
_MAX_REVIEW_IMAGES_PER_GROUP = 500

ImageRecord = dict[str, Any]


class _Pixmap(Protocol):
    width: int
    height: int

    def save(self, filename: str | Path) -> None: ...


class _Page(Protocol):
    rect: Any

    def get_pixmap(self, *, matrix: Any, alpha: bool = False) -> _Pixmap:
        del matrix, alpha
        raise NotImplementedError


class _PdfDocument(Protocol):
    def __len__(self) -> int: ...

    def __getitem__(self, page_index: int) -> _Page: ...

    def close(self) -> None: ...


class _FitzApi(Protocol):
    Matrix: Any

    def open(self, path: str | Path) -> _PdfDocument: ...


class ImageCropError(ValueError):
    """A safe, source-free image crop request failure."""


def normalize_bbox(
    value: object, *, legacy_unit_interval: bool = False
) -> list[float] | None:
    """Validate a y-min, x-min, y-max, x-max crop box."""

    if not isinstance(value, (list, tuple)):
        return None
    coordinates_value = cast(list[object] | tuple[object, ...], value)
    if len(coordinates_value) != 4:
        return None
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in coordinates_value
    ):
        return None
    coordinates = [float(cast(int | float, item)) for item in coordinates_value]
    if legacy_unit_interval and all(item <= 1.0 for item in coordinates):
        coordinates = [item * 1000 for item in coordinates]
    if (
        not all(math.isfinite(item) and 0 <= item <= 1000 for item in coordinates)
        or coordinates[0] >= coordinates[2]
        or coordinates[1] >= coordinates[3]
    ):
        return None
    return coordinates


def _bbox_from_src(src: str) -> list[float] | None:
    match = _BBOX_RE.search(src)
    if not match:
        return None
    try:
        coordinates = [float(item) for item in match.groups()]
    except ValueError:
        return None
    if all(item <= 1.0 for item in coordinates):
        coordinates = [item * 1000 for item in coordinates]
    # Historical extraction sorted reversed values before cropping. Preserve that
    # behavior while rejecting non-finite and out-of-contract model coordinates.
    ymin, ymax = sorted((coordinates[0], coordinates[2]))
    xmin, xmax = sorted((coordinates[1], coordinates[3]))
    return normalize_bbox([ymin, xmin, ymax, xmax])


def _pixel_box(bbox: list[float], width: int, height: int) -> tuple[int, int, int, int]:
    ymin, xmin, ymax, xmax = bbox
    left = max(0, int(xmin * width / 1000))
    top = max(0, int(ymin * height / 1000))
    right = min(width, int(xmax * width / 1000))
    bottom = min(height, int(ymax * height / 1000))
    if right <= left or bottom <= top:
        raise ImageCropError("Image crop has no area.")
    return left, top, right, bottom


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _hash_open_file(file_obj: BufferedReader) -> str:
    digest = hashlib.sha256()
    while chunk := file_obj.read(_COPY_CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def retain_source_png(
    png_path: str | Path,
    output_folder: str | Path,
    web_path_prefix: str,
) -> str:
    """Copy a rendered page PNG to its deterministic immutable owned path."""

    source = Path(png_path)
    destination_dir = Path(output_folder)
    destination_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ImageCropError("Rendered source image is unavailable.") from exc

    with os.fdopen(source_fd, "rb") as source_file:
        if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
            raise ImageCropError("Rendered source image is not a regular file.")
        digest = _hash_open_file(source_file)
        filename = f"image_source_{digest}.png"
        destination = destination_dir / filename
        try:
            source_file.seek(0)
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
        except FileExistsError:
            try:
                existing_fd = os.open(
                    destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(existing_fd, "rb") as existing_file:
                    if not stat.S_ISREG(os.fstat(existing_file.fileno()).st_mode):
                        raise ImageCropError("Retained source image is unavailable.")
                    if _hash_open_file(existing_file) != digest:
                        raise ImageCropError("Retained source image is invalid.")
            except ImageCropError:
                raise
            except OSError as exc:
                raise ImageCropError("Retained source image is unavailable.") from exc
        except OSError as exc:
            raise ImageCropError("Could not retain the rendered source image.") from exc
        else:
            try:
                with os.fdopen(destination_fd, "wb") as destination_file:
                    source_file.seek(0)
                    while chunk := source_file.read(_COPY_CHUNK_SIZE):
                        destination_file.write(chunk)
                    destination_file.flush()
                    os.fsync(destination_file.fileno())
            except OSError as exc:
                with suppress(OSError):
                    destination.unlink(missing_ok=True)
                raise ImageCropError(
                    "Could not retain the rendered source image."
                ) from exc

    return f"{web_path_prefix.rstrip('/')}/{filename}"


def crop_image_file(
    source_png: str | Path,
    bbox: list[float],
    output_folder: str | Path,
    web_path_prefix: str,
) -> str:
    """Create a PNG crop using the exact requested bounds and return its owned URL."""

    normalized = normalize_bbox(bbox)
    if normalized is None:
        raise ImageCropError("Image bounding box is invalid.")
    destination_dir = Path(output_folder)
    destination_dir.mkdir(parents=True, exist_ok=True)
    filename = f"extracted_{uuid.uuid4().hex[:8]}.png"
    save_path = destination_dir / filename
    try:
        with Image.open(source_png) as source_image:
            width, height = source_image.size
            box = _pixel_box(normalized, width, height)
            with source_image.crop(box) as cropped_image:
                cropped_image.save(save_path, format="PNG")
    except (OSError, ValueError) as exc:
        raise ImageCropError("Could not create the image crop.") from exc
    return f"{web_path_prefix.rstrip('/')}/{filename}"


def process_extracted_images(
    html_content: str,
    png_path: str,
    output_folder: str,
    web_path_prefix: str,
    *,
    page_index: int | None = None,
    crop_sources: MutableMapping[str, ImageRecord] | None = None,
) -> str:
    """Crop model boxes from a rendered page and optionally retain provenance."""

    soup = BeautifulSoup(html_content, "html.parser")
    try:
        with Image.open(png_path) as source_image:
            source_image.verify()
    except (OSError, ValueError, SyntaxError) as exc:
        logger.warning(
            "Could not open rendered PDF page image (%s).", type(exc).__name__
        )
        return html_content

    source_url: str | None = None
    source_attempted = False
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if not src:
            continue
        if crop_sources is not None and source_attempted and source_url is None:
            continue
        bbox = _bbox_from_src(src)
        if bbox is None:
            continue
        try:
            crop_url = crop_image_file(png_path, bbox, output_folder, web_path_prefix)
        except ImageCropError as exc:
            logger.warning(
                "Could not crop rendered PDF page image (%s).", type(exc).__name__
            )
            continue
        if crop_sources is not None and not source_attempted:
            source_attempted = True
            try:
                source_url = retain_source_png(png_path, output_folder, web_path_prefix)
            except ImageCropError as exc:
                logger.warning(
                    "Could not retain rendered PDF page image (%s).", type(exc).__name__
                )
                with suppress(OSError):
                    (Path(output_folder) / Path(urlsplit(crop_url).path).name).unlink(
                        missing_ok=True
                    )
                continue
        if crop_sources is not None and source_url is None:
            with suppress(OSError):
                (Path(output_folder) / Path(urlsplit(crop_url).path).name).unlink(
                    missing_ok=True
                )
            continue
        img["src"] = crop_url
        if crop_sources is not None and source_url is not None:
            crop_sources[crop_url] = {
                "source_url": source_url,
                "page_index": page_index,
                "bbox": bbox,
            }
    return str(soup)


def build_page_image_payload(
    html_parts: list[str | None],
    png_paths: list[str | Path | None],
    *,
    uploads_dir: str | Path,
) -> list[ImageRecord]:
    """Build the existing pending-PDF image-review payload."""

    pages_data: list[ImageRecord] = []
    uploads_root = Path(uploads_dir)
    for index, html_content in enumerate(html_parts):
        if html_content is None:
            continue
        soup = BeautifulSoup(html_content, "html.parser")
        page_images: list[ImageRecord] = []
        for img_idx, img in enumerate(soup.find_all("img")):
            bbox_match = _BBOX_RE.findall(str(img.get("src") or ""))
            if not bbox_match:
                continue
            coordinates = list(map(float, bbox_match[0]))
            if all(value <= 1.0 for value in coordinates):
                coordinates = [value * 1000 for value in coordinates]
            caption = ""
            parent = img.find_parent("figure")
            figcaption = parent.find("figcaption") if parent is not None else None
            if figcaption is not None:
                caption = figcaption.get_text().strip()
            page_images.append(
                {
                    "img_idx": img_idx,
                    "bbox": coordinates,
                    "caption": caption,
                    "keep": True,
                }
            )

        if not page_images or index >= len(png_paths):
            continue
        png_path = png_paths[index]
        if not png_path:
            continue
        local_png = Path(png_path)
        if not local_png.exists():
            continue
        try:
            relative_png = local_png.relative_to(uploads_root)
        except ValueError:
            continue
        pages_data.append(
            {
                "page_index": index,
                "image_url": "/uploads/" + relative_png.as_posix(),
                "images": page_images,
            }
        )
    return pages_data


def normalize_image_review_payload(
    payload: object, html_parts: list[str | None]
) -> tuple[list[ImageRecord] | None, str | None]:
    """Validate the existing resumable image-review request shape."""

    if not isinstance(payload, list):
        return None, "Image review payload must be an array."
    payload_pages = cast(list[Any], payload)
    if len(payload_pages) > len(html_parts):
        return None, "Image review payload contains too many pages."

    normalized_pages: list[ImageRecord] = []
    seen_pages: set[int] = set()
    for raw_page_data in payload_pages:
        if not isinstance(raw_page_data, dict):
            return None, "Each image review page must be an object."
        page_data = cast(dict[str, Any], raw_page_data)
        page_index = page_data.get("page_index")
        if isinstance(page_index, bool) or not isinstance(page_index, int):
            return None, "Each image review page needs an integer page_index."
        if (
            page_index in seen_pages
            or page_index < 0
            or page_index >= len(html_parts)
            or html_parts[page_index] is None
        ):
            return None, "Image review page_index is invalid or duplicated."
        seen_pages.add(page_index)

        images = page_data.get("images")
        if not isinstance(images, list):
            return (
                None,
                "Each image review page needs an images array with at most 500 items.",
            )
        image_values = cast(list[Any], images)
        if len(image_values) > _MAX_REVIEW_IMAGES_PER_GROUP:
            return (
                None,
                "Each image review page needs an images array with at most 500 items.",
            )

        normalized_images: list[ImageRecord] = []
        seen_image_indexes: set[int] = set()
        for raw_image in image_values:
            if not isinstance(raw_image, dict):
                return None, "Each reviewed image must be an object."
            image = cast(dict[str, Any], raw_image)
            image_index = image.get("img_idx")
            if (
                isinstance(image_index, bool)
                or not isinstance(image_index, int)
                or image_index < 0
            ):
                return None, "Each reviewed image needs a non-negative integer img_idx."
            if image_index in seen_image_indexes:
                return None, "Image review img_idx values must be unique per page."
            seen_image_indexes.add(image_index)
            bbox = image.get("bbox")
            if not isinstance(bbox, (list, tuple)):
                return None, "Each reviewed image needs a four-value bounding box."
            bbox_values = cast(list[Any] | tuple[Any, ...], bbox)
            if len(bbox_values) != 4:
                return None, "Each reviewed image needs a four-value bounding box."
            try:
                coordinates = [float(value) for value in bbox_values]
            except TypeError, ValueError:
                return None, "Image bounding box values must be numbers."
            if (
                not all(
                    math.isfinite(value) and 0 <= value <= 1000 for value in coordinates
                )
                or coordinates[0] >= coordinates[2]
                or coordinates[1] >= coordinates[3]
            ):
                return (
                    None,
                    "Image bounding boxes must be ordered values between 0 and 1000.",
                )

            caption = image.get("caption", "")
            if not isinstance(caption, str) or len(caption) > 2_000:
                return (
                    None,
                    "Image captions must be strings no longer than 2000 characters.",
                )
            keep = image.get("keep", True)
            if not isinstance(keep, bool):
                return None, "Image keep values must be booleans."
            normalized_images.append(
                {
                    "img_idx": image_index,
                    "bbox": coordinates,
                    "caption": caption,
                    "keep": keep,
                }
            )
        normalized_pages.append({"page_index": page_index, "images": normalized_images})
    return normalized_pages, None


def _owned_document_image(
    value: object,
    *,
    uploads_dir: Path,
    owner_id: int | str,
    document_dir: Path,
) -> tuple[Path, str] | None:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
    ):
        return None
    raw_path = parsed.path
    if raw_path.startswith("/uploads/"):
        relative = raw_path.removeprefix("/uploads/")
    elif raw_path.startswith("/"):
        return None
    else:
        relative = None
    path_parts = (
        PurePosixPath(relative).parts
        if relative is not None
        else PurePosixPath(raw_path).parts
    )
    if not path_parts or any(part in {"", ".", ".."} for part in path_parts):
        return None
    owner_root = (uploads_dir / str(owner_id)).resolve()
    try:
        candidate = (
            uploads_dir.joinpath(*path_parts)
            if relative is not None
            else document_dir.joinpath(*path_parts)
        ).resolve(strict=True)
        candidate.relative_to(owner_root)
        candidate.relative_to(document_dir)
    except OSError, ValueError, SyntaxError:
        return None
    if not _regular_file(candidate):
        return None
    try:
        web_url = "/uploads/" + candidate.relative_to(uploads_dir).as_posix()
    except ValueError:
        return None
    return candidate, web_url


def _is_readable_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except OSError, ValueError, SyntaxError:
        return False


def _physical_page_index(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def build_completed_image_groups(
    html_content: str,
    image_crops: object,
    *,
    uploads_dir: str | Path,
    owner_id: int | str,
    document_dir: str | Path,
) -> list[ImageRecord]:
    """Build server-owned source groups for completed-document crop review."""

    uploads_root = Path(uploads_dir).resolve()
    source_dir = Path(document_dir).resolve(strict=True)
    metadata: dict[str, Any] = (
        cast(dict[str, Any], image_crops) if isinstance(image_crops, dict) else {}
    )
    soup = BeautifulSoup(html_content, "html.parser")
    groups: list[ImageRecord] = []
    by_source: dict[tuple[str, int | None], ImageRecord] = {}

    for img_idx, img in enumerate(soup.find_all("img")):
        current = _owned_document_image(
            img.get("src"),
            uploads_dir=uploads_root,
            owner_id=owner_id,
            document_dir=source_dir,
        )
        if current is None or not _is_readable_image(current[0]):
            raise ImageCropError("A document image is unavailable.")

        _current_path, current_url = current
        entry: Any = metadata.get(str(img.get("src") or ""))
        source: tuple[Path, str] | None = None
        bbox: list[float] | None = None
        source_page_index: int | None = None
        if isinstance(entry, dict):
            entry_data = cast(dict[str, Any], entry)
            bbox = normalize_bbox(entry_data.get("bbox"))
            source_page_index = _physical_page_index(entry_data.get("page_index"))
            candidate = _owned_document_image(
                entry_data.get("source_url"),
                uploads_dir=uploads_root,
                owner_id=owner_id,
                document_dir=source_dir,
            )
            if (
                candidate is not None
                and _is_readable_image(candidate[0])
                and bbox is not None
            ):
                source = candidate

        is_page_source = bool(
            source is not None and _SOURCE_IMAGE_NAME_RE.fullmatch(source[0].name)
        )
        if source is not None:
            image_url = source[1]
            group_key = (image_url, source_page_index)
            existing_group = by_source.get(group_key)
            if existing_group is None:
                if is_page_source:
                    label = (
                        f"Page {source_page_index + 1}"
                        if source_page_index is not None
                        else f"Page {len(groups) + 1}"
                    )
                    source_kind = "page"
                else:
                    label = f"Image {img_idx + 1}"
                    source_kind = "saved_image"
                group: ImageRecord = {
                    "page_index": len(groups),
                    "image_url": image_url,
                    "label": label,
                    "source_kind": source_kind,
                    "images": [],
                }
                if source_page_index is not None:
                    group["source_page_index"] = source_page_index
                by_source[group_key] = group
                groups.append(group)
            else:
                group = existing_group
        else:
            image_url = current_url
            group = {
                "page_index": len(groups),
                "image_url": image_url,
                "label": f"Image {img_idx + 1}",
                "source_kind": "saved_image",
                "images": [],
            }
            groups.append(group)
            bbox = [0.0, 0.0, 1000.0, 1000.0]
            source_page_index = None

        caption = ""
        figure = img.find_parent("figure")
        if figure is not None:
            figcaption = figure.find("figcaption")
            if figcaption is not None:
                caption = figcaption.get_text().strip()
        group_images = cast(list[ImageRecord], group["images"])
        group_images.append(
            {
                "img_idx": img_idx,
                "bbox": bbox or [0.0, 0.0, 1000.0, 1000.0],
                "caption": caption,
                "keep": True,
            }
        )
    return groups


def normalize_completed_crop_payload(
    payload: object,
    groups: list[ImageRecord],
    *,
    page_count: int,
) -> tuple[list[ImageRecord] | None, str | None]:
    """Validate a completed-crop request against server-reconstructed groups."""

    if not isinstance(payload, dict):
        return None, "Invalid image-crop request."
    request_data = cast(dict[str, Any], payload)
    if set(request_data) != {"expected_updated_at", "pages"}:
        return None, "Invalid image-crop request."
    expected_updated_at = request_data.get("expected_updated_at")
    if (
        not isinstance(expected_updated_at, str)
        or not expected_updated_at.strip()
        or len(expected_updated_at) > 128
    ):
        return None, "A document revision is required."
    pages = request_data.get("pages")
    if not isinstance(pages, list):
        return None, "All image source groups must be represented exactly once."
    page_values = cast(list[Any], pages)
    if len(page_values) != len(groups):
        return None, "All image source groups must be represented exactly once."

    expected_groups: dict[int, ImageRecord] = {
        group["page_index"]: group for group in groups
    }
    seen_groups: set[int] = set()
    seen_images: set[int] = set()
    normalized: list[ImageRecord] = []
    expected_image_count = sum(len(group["images"]) for group in groups)
    if expected_image_count > 20_000:
        return None, "The document has too many images for one crop update."

    for raw_page in page_values:
        if not isinstance(raw_page, dict):
            return None, "Each image source group has invalid fields."
        page = cast(dict[str, Any], raw_page)
        if set(page) - {
            "page_index",
            "images",
            "source_page_index",
        }:
            return None, "Each image source group has invalid fields."
        page_index = page.get("page_index")
        if (
            isinstance(page_index, bool)
            or not isinstance(page_index, int)
            or page_index not in expected_groups
        ):
            return None, "Image source group page_index is invalid."
        if page_index in seen_groups:
            return None, "Image source group page_index is duplicated."
        seen_groups.add(page_index)
        source_group = expected_groups[page_index]

        source_page_index = None
        if "source_page_index" in page:
            source_page_index = page.get("source_page_index")
            if (
                source_group.get("source_kind") != "saved_image"
                or isinstance(source_page_index, bool)
                or not isinstance(source_page_index, int)
                or source_page_index < 0
                or source_page_index >= page_count
            ):
                return None, "Original PDF page index is invalid for this image group."

        images = page.get("images")
        if not isinstance(images, list):
            return None, "All current images in each source group must be represented."
        image_values = cast(list[Any], images)
        source_images = cast(list[ImageRecord], source_group["images"])
        if len(image_values) != len(source_images):
            return None, "All current images in each source group must be represented."
        expected_images = {image["img_idx"] for image in source_images}
        normalized_images: list[ImageRecord] = []
        for raw_image in image_values:
            if not isinstance(raw_image, dict):
                return None, "Each image crop needs only img_idx and bbox."
            image = cast(dict[str, Any], raw_image)
            if set(image) != {"img_idx", "bbox"}:
                return None, "Each image crop needs only img_idx and bbox."
            img_idx = image.get("img_idx")
            if (
                isinstance(img_idx, bool)
                or not isinstance(img_idx, int)
                or img_idx not in expected_images
                or img_idx in seen_images
            ):
                return None, "Image index is invalid or duplicated."
            seen_images.add(img_idx)
            bbox = normalize_bbox(image.get("bbox"))
            if bbox is None:
                return (
                    None,
                    "Image bounding boxes must be finite, ordered values between 0 and 1000.",
                )
            normalized_images.append({"img_idx": img_idx, "bbox": bbox})
        normalized.append(
            {
                "page_index": page_index,
                "source_page_index": source_page_index,
                "images": normalized_images,
            }
        )

    if seen_groups != set(expected_groups) or len(seen_images) != expected_image_count:
        return (
            None,
            "All current image groups and images must be represented exactly once.",
        )
    return normalized, None


def render_pdf_page_png(
    pdf_path: str | Path,
    page_index: int,
    output_folder: str | Path,
    *,
    max_edge: int = 2048,
) -> Path:
    """Render one physical PDF page in full, with a bounded longest edge."""

    from .parsers import pdf_rendering

    if pdf_rendering.fitz is None:
        raise ImageCropError("PDF page rendering is unavailable.")
    fitz_api = cast(_FitzApi, pdf_rendering.fitz)
    optimal_scale = cast(
        Callable[[Any, int], float],
        pdf_rendering.get_optimal_scale_factor,
    )
    try:
        document = fitz_api.open(pdf_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ImageCropError("PDF source is unavailable.") from exc
    output = Path(output_folder)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"docproc_reanchor_{uuid.uuid4().hex}.png"
    try:
        if page_index < 0 or page_index >= len(document):
            raise ImageCropError("Original PDF page index is invalid.")
        page = document[page_index]
        longest_edge = max(float(page.rect.width), float(page.rect.height))
        if longest_edge <= 0:
            raise ImageCropError("Original PDF page has invalid dimensions.")
        scale = min(optimal_scale(page, max_edge), max_edge / longest_edge)
        pixmap = page.get_pixmap(matrix=fitz_api.Matrix(scale, scale), alpha=False)
        while max(int(pixmap.width), int(pixmap.height)) > max_edge:
            correction = max_edge / max(int(pixmap.width), int(pixmap.height))
            scale *= correction
            pixmap = page.get_pixmap(matrix=fitz_api.Matrix(scale, scale), alpha=False)
        pixmap.save(path)
        return path
    except ImageCropError:
        path.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        path.unlink(missing_ok=True)
        raise ImageCropError("Could not render the original PDF page.") from exc
    finally:
        document.close()


def upload_url(path: str | Path, uploads_dir: str | Path) -> str:
    resolved = Path(path).resolve(strict=True)
    root = Path(uploads_dir).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ImageCropError("Image path is outside the upload root.") from exc
    return "/uploads/" + relative.as_posix()
