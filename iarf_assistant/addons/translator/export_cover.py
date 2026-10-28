"""Capture and resolve bounded, owner-scoped Translator export covers."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from PIL import Image, ImageOps

from carlina.core.semantic_html import MAX_INLINE_IMAGE_CHARS as _MAX_INLINE_IMAGE_CHARS

from ...core.upload_paths import resolve_upload_reference, uploads_root

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 700 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_THUMBNAIL_SIZE = (1600, 2400)
JPEG_QUALITIES = (90, 80, 70, 60, 50, 40, 30, 20, 10)
MAX_INLINE_IMAGE_CHARS = _MAX_INLINE_IMAGE_CHARS


def pin_export_metadata_cover(metadata: Mapping, *, user_id: int) -> dict:
    """Copy admitted metadata and freeze a replacement cover's exact bytes."""
    result = dict(metadata)
    path = result.get("cover_upload_path")
    if path:
        result["cover_upload_path"] = capture_export_cover(
            user_id=user_id, upload_path=path
        )["upload_path"]
    return result


_CANONICAL_PATH_RE = re.compile(
    r"^/uploads/(?P<owner>[1-9][0-9]*)/translator-covers/"
    r"(?P<digest>[0-9a-f]{64})\.jpg$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WITNESS_KEYS = frozenset({"upload_path", "media_type", "byte_length", "sha256"})


class ExportCoverError(ValueError):
    """Bounded export-cover failure containing only a stable public code."""

    def __init__(self, code: str):
        if not isinstance(code, str) or not code.startswith("export-cover-"):
            code = "export-cover-invalid"
        self.code = code
        super().__init__(code)


def _owner_text(user_id: int) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ExportCoverError("export-cover-owner")
    return str(user_id)


def _uploads_and_owner(owner: str) -> tuple[Path, Path]:
    try:
        root = uploads_root().resolve()
        owner_raw = root / owner
        if owner_raw.is_symlink() or not owner_raw.is_dir():
            raise ExportCoverError("export-cover-owner-root")
        return root, owner_raw.resolve()
    except ExportCoverError:
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        raise ExportCoverError("export-cover-owner-root") from None


def _contains_symlink(path: Path, root: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == root or current.parent == current:
            return False
        current = current.parent


def _validate_web_path(upload_path: str, owner: str, *, canonical_only: bool) -> None:
    if not isinstance(upload_path, str):
        raise ExportCoverError("export-cover-path-type")
    prefix = f"/uploads/{owner}/"
    if not upload_path.startswith(prefix) or upload_path == prefix:
        raise ExportCoverError("export-cover-path")
    if any(
        char in {"\\", "%", "?", "#"} or ord(char) < 32 or ord(char) == 127
        for char in upload_path
    ):
        raise ExportCoverError("export-cover-path")
    try:
        parsed = urlsplit(upload_path)
    except ValueError:
        raise ExportCoverError("export-cover-path") from None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ExportCoverError("export-cover-path")
    relative = upload_path.removeprefix(prefix)
    if not relative or relative.startswith("/") or "//" in relative:
        raise ExportCoverError("export-cover-path")
    if any(part in {".", ".."} for part in PurePosixPath(relative).parts):
        raise ExportCoverError("export-cover-path")
    canonical = _CANONICAL_PATH_RE.fullmatch(upload_path)
    in_cover_directory = relative == "translator-covers" or relative.startswith(
        "translator-covers/"
    )
    if canonical_only and canonical is None:
        raise ExportCoverError("export-cover-canonical-path")
    if in_cover_directory and canonical is None:
        raise ExportCoverError("export-cover-canonical-path")


def _owned_path(
    upload_path: str,
    owner: str,
    *,
    canonical_only: bool = False,
) -> tuple[Path, Path]:
    _validate_web_path(upload_path, owner, canonical_only=canonical_only)
    root, owner_root = _uploads_and_owner(owner)
    try:
        candidate = resolve_upload_reference(upload_path, uploads_dir=root)
        if candidate is None:
            raise ExportCoverError("export-cover-path")
        candidate = candidate.resolve()
        candidate.relative_to(owner_root)
        relative = PurePosixPath(
            upload_path.removeprefix("/uploads/").replace("\\", "/")
        )
        lexical = root.joinpath(*relative.parts)
        if _contains_symlink(lexical, owner_root):
            raise ExportCoverError("export-cover-path")
        return owner_root, candidate
    except ExportCoverError:
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        raise ExportCoverError("export-cover-path") from None


def _require_regular_file(path: Path, *, code: str) -> os.stat_result:
    try:
        info = path.stat()
    except OSError, RuntimeError, TypeError, ValueError:
        raise ExportCoverError(code) from None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ExportCoverError(code)
    return info


def _read_bounded(
    path: Path, *, maximum: int, size_code: str, missing_code: str
) -> bytes:
    info = _require_regular_file(path, code=missing_code)
    if info.st_size > maximum:
        raise ExportCoverError(size_code)
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    except OSError, RuntimeError, TypeError, ValueError:
        raise ExportCoverError(missing_code) from None
    if len(data) > maximum:
        raise ExportCoverError(size_code)
    return data


def _is_animated(image: Image.Image) -> bool:
    return (
        bool(getattr(image, "is_animated", False))
        or int(getattr(image, "n_frames", 1) or 1) > 1
    )


def _inspect_image(
    data: bytes,
    *,
    formats: frozenset[str],
    normalized_dimensions: bool = False,
    require_clean_metadata: bool = False,
) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in formats:
                raise ExportCoverError("export-cover-image-format")
            if _is_animated(image):
                raise ExportCoverError("export-cover-image-animated")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ExportCoverError("export-cover-image-dimensions")
            if normalized_dimensions and (
                width > MAX_THUMBNAIL_SIZE[0] or height > MAX_THUMBNAIL_SIZE[1]
            ):
                raise ExportCoverError("export-cover-canonical-dimensions")
            if require_clean_metadata and (
                "exif" in image.info or "icc_profile" in image.info
            ):
                raise ExportCoverError("export-cover-canonical-metadata")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if _is_animated(image):
                raise ExportCoverError("export-cover-image-animated")
            return width, height
    except ExportCoverError:
        raise
    except Image.DecompressionBombError, OSError, ValueError, TypeError, SyntaxError:
        raise ExportCoverError("export-cover-image-invalid") from None


def _canonical_file(
    upload_path: str,
    owner: str,
    expected_digest: str | None = None,
    *,
    expected_length: int | None = None,
) -> tuple[Path, bytes, str]:
    _owner_root, path = _owned_path(upload_path, owner, canonical_only=True)
    data = _read_bounded(
        path,
        maximum=MAX_OUTPUT_BYTES,
        size_code="export-cover-output-size",
        missing_code="export-cover-canonical-missing",
    )
    digest = hashlib.sha256(data).hexdigest()
    name_match = _CANONICAL_PATH_RE.fullmatch(upload_path)
    path_digest = name_match.group("digest") if name_match else None
    if path_digest is None or digest != path_digest:
        raise ExportCoverError("export-cover-canonical-hash")
    if expected_digest is not None and digest != expected_digest:
        raise ExportCoverError("export-cover-witness-hash")
    if expected_length is not None and len(data) != expected_length:
        raise ExportCoverError("export-cover-witness-size")
    _inspect_image(
        data,
        formats=frozenset({"JPEG"}),
        normalized_dimensions=True,
        require_clean_metadata=True,
    )
    return path, data, digest


def _prepare_image(data: bytes) -> Image.Image:
    _inspect_image(data, formats=frozenset({"JPEG", "PNG"}))
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            oriented = ImageOps.exif_transpose(image)
            if oriented.mode in {"RGBA", "LA", "P"} or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                prepared = Image.alpha_composite(background, rgba).convert("RGB")
                rgba.close()
                background.close()
            else:
                prepared = oriented.convert("RGB")
            if prepared.size[0] * prepared.size[1] > MAX_IMAGE_PIXELS:
                prepared.close()
                raise ExportCoverError("export-cover-image-dimensions")
            prepared.thumbnail(MAX_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            return prepared
    except ExportCoverError:
        raise
    except Image.DecompressionBombError, OSError, ValueError, TypeError, SyntaxError:
        raise ExportCoverError("export-cover-image-invalid") from None


def _encode_jpeg(image: Image.Image) -> bytes:
    try:
        for quality in JPEG_QUALITIES:
            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            data = output.getvalue()
            if len(data) <= MAX_OUTPUT_BYTES:
                return data
    except OSError, ValueError, TypeError:
        raise ExportCoverError("export-cover-image-encode") from None
    raise ExportCoverError("export-cover-output-size")


def _witness(owner: str, digest: str, data: bytes) -> dict[str, object]:
    return {
        "upload_path": f"/uploads/{owner}/translator-covers/{digest}.jpg",
        "media_type": "image/jpeg",
        "byte_length": len(data),
        "sha256": digest,
    }


def _ensure_cover_directory(owner_root: Path) -> Path:
    directory = owner_root / "translator-covers"
    try:
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise ExportCoverError("export-cover-storage")
        else:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                if directory.is_symlink() or not directory.is_dir():
                    raise ExportCoverError("export-cover-storage")
        return directory
    except ExportCoverError:
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        raise ExportCoverError("export-cover-storage") from None


def _write_exclusive(owner_root: Path, data: bytes, digest: str) -> bool:
    directory = _ensure_cover_directory(owner_root)
    target = directory / f"{digest}.jpg"
    created = False
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return False
    except OSError, RuntimeError, TypeError, ValueError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            try:
                if target.is_file() and not target.is_symlink():
                    target.unlink()
            except OSError:
                pass
        raise ExportCoverError("export-cover-storage") from None
    return True


def _remove_new_cover(path: Path, expected: bytes) -> None:
    try:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == expected:
            path.unlink()
    except OSError, RuntimeError, ValueError:
        return


def capture_export_cover(*, user_id: int, upload_path: str) -> dict[str, object]:
    """Normalize one owner-uploaded JPEG/PNG into a bounded canonical JPEG."""

    owner = _owner_text(user_id)
    _validate_web_path(upload_path, owner, canonical_only=False)
    canonical_match = _CANONICAL_PATH_RE.fullmatch(upload_path)
    if canonical_match is not None:
        _path, data, digest = _canonical_file(upload_path, owner)
        return _witness(owner, digest, data)

    owner_root, source_path = _owned_path(upload_path, owner)
    source_data = _read_bounded(
        source_path,
        maximum=MAX_INPUT_BYTES,
        size_code="export-cover-input-size",
        missing_code="export-cover-source-missing",
    )
    image = _prepare_image(source_data)
    try:
        encoded = _encode_jpeg(image)
    finally:
        image.close()
    digest = hashlib.sha256(encoded).hexdigest()
    output_path = f"/uploads/{owner}/translator-covers/{digest}.jpg"
    created = _write_exclusive(owner_root, encoded, digest)
    try:
        _path, stored, stored_digest = _canonical_file(output_path, owner)
    except ExportCoverError:
        if created:
            _remove_new_cover(
                owner_root / "translator-covers" / f"{digest}.jpg", encoded
            )
        raise
    if stored_digest != digest or stored != encoded:
        if created:
            _remove_new_cover(
                owner_root / "translator-covers" / f"{digest}.jpg", encoded
            )
        raise ExportCoverError("export-cover-storage-conflict")
    return _witness(owner, digest, stored)


def _encode_uri(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    uri = f"data:image/jpeg;base64,{encoded}"
    if len(uri) > MAX_INLINE_IMAGE_CHARS:
        raise ExportCoverError("export-cover-inline-size")
    return uri


def resolve_export_cover_path(*, user_id: int, upload_path: str) -> str:
    """Resolve one canonical owner path to a validated JPEG data URI."""

    owner = _owner_text(user_id)
    path_match = (
        _CANONICAL_PATH_RE.fullmatch(upload_path)
        if isinstance(upload_path, str)
        else None
    )
    if path_match is None or path_match.group("owner") != owner:
        raise ExportCoverError("export-cover-canonical-path")
    _path, data, _digest = _canonical_file(upload_path, owner)
    return _encode_uri(data)


def resolve_export_cover(*, user_id: int, cover: Mapping[str, object]) -> str:
    """Validate a canonical cover witness and resolve it to a JPEG data URI."""

    owner = _owner_text(user_id)
    if not isinstance(cover, Mapping):
        raise ExportCoverError("export-cover-witness-type")
    if set(cover) != _WITNESS_KEYS:
        raise ExportCoverError("export-cover-witness-keys")
    upload_path = cover["upload_path"]
    media_type = cover["media_type"]
    byte_length = cover["byte_length"]
    digest = cover["sha256"]
    if media_type != "image/jpeg":
        raise ExportCoverError("export-cover-witness-media-type")
    if (
        not isinstance(byte_length, int)
        or isinstance(byte_length, bool)
        or not 0 < byte_length <= MAX_OUTPUT_BYTES
    ):
        raise ExportCoverError("export-cover-witness-size")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ExportCoverError("export-cover-witness-hash")
    if not isinstance(upload_path, str):
        raise ExportCoverError("export-cover-witness-path")
    path_match = _CANONICAL_PATH_RE.fullmatch(upload_path)
    if path_match is None or path_match.group("owner") != owner:
        raise ExportCoverError("export-cover-witness-path")
    _path, data, _actual_digest = _canonical_file(
        upload_path,
        owner,
        expected_digest=digest,
        expected_length=byte_length,
    )
    return _encode_uri(data)


__all__ = [
    "ExportCoverError",
    "capture_export_cover",
    "resolve_export_cover",
    "resolve_export_cover_path",
]
