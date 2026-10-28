"""Bounded local/HTTPS retrieval for signed Carlina release metadata and packs."""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from iarf_assistant.core.release_manifest import MAX_RELEASE_INDEX_BYTES

DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60.0


class ReleaseDownloadError(RuntimeError):
    """A release source was unsafe, unavailable, oversized, or corrupted."""


@dataclass(frozen=True, slots=True)
class DownloadedReleaseFile:
    path: Path
    sha256: str
    size: int
    source: str


def _source_scheme(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    if len(parsed.scheme) == 1 and source[1:3] in {":\\", ":/"}:
        return ""
    return parsed.scheme.casefold()


def is_local_release_source(source: str | os.PathLike[str]) -> bool:
    value = str(source)
    return _source_scheme(value) in {"", "file"}


def resolve_artifact_source(
    artifact_reference: str,
    *,
    index_source: str | os.PathLike[str],
) -> str:
    """Resolve relative artifact references against a local file or HTTPS index."""

    reference = str(artifact_reference or "").strip()
    if not reference:
        raise ReleaseDownloadError("Release artifact URL is empty.")
    reference_scheme = _source_scheme(reference)
    base = str(index_source)
    base_scheme = _source_scheme(base)
    if reference_scheme:
        if reference_scheme not in {"https", "file"}:
            raise ReleaseDownloadError(
                f"Release artifacts require HTTPS or a local file, not {reference_scheme!r}."
            )
        if base_scheme == "https" and reference_scheme != "https":
            raise ReleaseDownloadError(
                "A network release index may reference only HTTPS artifacts."
            )
        return reference

    if base_scheme == "https":
        return urllib.parse.urljoin(base, reference)
    if base_scheme == "file":
        base_path = Path(urllib.request.url2pathname(urllib.parse.urlparse(base).path))
    elif base_scheme:
        raise ReleaseDownloadError(
            f"Unsupported release-index scheme: {base_scheme!r}."
        )
    else:
        base_path = Path(base)
    base_directory = base_path.expanduser().resolve().parent
    resolved = (base_directory / reference).resolve()
    try:
        resolved.relative_to(base_directory)
    except ValueError as exc:
        raise ReleaseDownloadError(
            "Relative local release artifacts cannot escape the index directory."
        ) from exc
    return str(resolved)


def _local_path(source: str | os.PathLike[str]) -> Path:
    value = str(source)
    scheme = _source_scheme(value)
    if scheme == "file":
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc not in {"", "localhost"}:
            raise ReleaseDownloadError("Remote file:// authorities are not permitted.")
        return Path(urllib.request.url2pathname(parsed.path)).expanduser().resolve()
    if scheme:
        raise ReleaseDownloadError(f"Source is not local: {value}")
    return Path(value).expanduser().resolve()


def read_release_index_source(
    source: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> bytes:
    value = str(source)
    if is_local_release_source(value):
        path = _local_path(value)
        try:
            if path.stat().st_size > MAX_RELEASE_INDEX_BYTES:
                raise ReleaseDownloadError("Release index exceeds the size limit.")
            return path.read_bytes()
        except OSError as exc:
            raise ReleaseDownloadError(f"Could not read release index: {path}") from exc

    if _source_scheme(value) != "https":
        raise ReleaseDownloadError("Network release indexes require HTTPS.")
    request = urllib.request.Request(
        value,
        headers={"Accept": "application/json", "User-Agent": "Carlina-Updater/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if _source_scheme(final_url) != "https":
                raise ReleaseDownloadError("Release-index redirect left HTTPS.")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_RELEASE_INDEX_BYTES:
                raise ReleaseDownloadError("Release index exceeds the size limit.")
            raw = response.read(MAX_RELEASE_INDEX_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        if isinstance(exc, ReleaseDownloadError):
            raise
        raise ReleaseDownloadError(
            f"Could not download release index: {value}"
        ) from exc
    if len(raw) > MAX_RELEASE_INDEX_BYTES:
        raise ReleaseDownloadError("Release index exceeds the size limit.")
    return raw


def _copy_and_hash(
    source,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    progress: Callable[[int, int | None], None] | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> DownloadedReleaseFile:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.part-{os.getpid()}-{uuid.uuid4().hex}"
    )
    digest = hashlib.sha256()
    written = 0
    try:
        with temporary.open("xb") as target:
            while chunk := source.read(4 * 1024 * 1024):
                if cancellation_check is not None:
                    cancellation_check()
                written += len(chunk)
                if written > expected_size:
                    raise ReleaseDownloadError(
                        "Release artifact is larger than its signed size."
                    )
                digest.update(chunk)
                target.write(chunk)
                if progress is not None:
                    progress(written, expected_size)
            target.flush()
            os.fsync(target.fileno())
        actual_digest = digest.hexdigest()
        if written != expected_size:
            raise ReleaseDownloadError(
                f"Release artifact size mismatch: expected {expected_size}, got {written}."
            )
        if actual_digest != expected_sha256:
            raise ReleaseDownloadError("Release artifact SHA-256 mismatch.")
        os.replace(temporary, destination)
        return DownloadedReleaseFile(
            destination,
            actual_digest,
            written,
            "",
        )
    finally:
        temporary.unlink(missing_ok=True)


def download_release_artifact(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    expected_size: int,
    expected_sha256: str,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    progress: Callable[[int, int | None], None] | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> DownloadedReleaseFile:
    """Download exactly the signed bytes and publish them atomically."""

    if expected_size <= 0 or expected_size > 20 * 1024**3:
        raise ReleaseDownloadError(
            "Signed artifact size is outside the supported budget."
        )
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ReleaseDownloadError("Signed artifact SHA-256 is invalid.")
    value = str(source)
    destination_path = Path(destination).expanduser().resolve()
    if is_local_release_source(value):
        source_path = _local_path(value)
        try:
            with source_path.open("rb") as handle:
                result = _copy_and_hash(
                    handle,
                    destination_path,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    progress=progress,
                    cancellation_check=cancellation_check,
                )
        except OSError as exc:
            raise ReleaseDownloadError(
                f"Could not copy local release artifact: {source_path}"
            ) from exc
        return DownloadedReleaseFile(
            result.path,
            result.sha256,
            result.size,
            str(source_path),
        )

    if _source_scheme(value) != "https":
        raise ReleaseDownloadError("Network release artifacts require HTTPS.")
    request = urllib.request.Request(
        value,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "Carlina-Updater/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if _source_scheme(final_url) != "https":
                raise ReleaseDownloadError("Release-artifact redirect left HTTPS.")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) != expected_size:
                raise ReleaseDownloadError(
                    "Release artifact Content-Length differs from its signed size."
                )
            result = _copy_and_hash(
                response,
                destination_path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                progress=progress,
                cancellation_check=cancellation_check,
            )
    except (OSError, urllib.error.URLError, ValueError) as exc:
        if isinstance(exc, ReleaseDownloadError):
            raise
        raise ReleaseDownloadError(
            f"Could not download release artifact: {value}"
        ) from exc
    return DownloadedReleaseFile(
        result.path,
        result.sha256,
        result.size,
        value,
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DEFAULT_DOWNLOAD_TIMEOUT_SECONDS",
    "DownloadedReleaseFile",
    "ReleaseDownloadError",
    "download_release_artifact",
    "is_local_release_source",
    "read_release_index_source",
    "resolve_artifact_source",
    "sha256_file",
]
