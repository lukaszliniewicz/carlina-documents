"""Prepare and verify the fixed local SaT CPU model assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path, PurePosixPath
from typing import IO, Literal, NoReturn, cast
from urllib.parse import urlsplit
from urllib.response import addinfourl

SatAssetsErrorCode = Literal[
    "model_assets_missing",
    "model_assets_invalid",
    "model_assets_download_failed",
]

_ERROR_MESSAGES: dict[SatAssetsErrorCode, str] = {
    "model_assets_missing": "Pinned SaT model assets are missing.",
    "model_assets_invalid": "Pinned SaT model assets are invalid.",
    "model_assets_download_failed": "Pinned SaT model assets could not be prepared.",
}

_MODEL_REPOSITORY = "segment-any-text/sat-12l-sm"
_MODEL_REVISION = "d70c72a9331b2d5a9e82baad00c64964a23a09bb"
_TOKENIZER_REPOSITORY = "FacebookAI/xlm-roberta-base"
_TOKENIZER_REVISION = "e73636d4f797dec63c3081bb6ed5c7b0bb3f2089"
_MANIFEST_LIMIT = 16 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30
_STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SatModelAssets:
    """Verified paths for the fixed SaT model and tokenizer generation."""

    root: Path
    model_dir: Path
    tokenizer_dir: Path
    fingerprint: str


class SatAssetsError(RuntimeError):
    """A content-free error from fixed SaT asset preparation or verification."""

    def __init__(self, code: SatAssetsErrorCode) -> None:
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class _AssetDescriptor:
    repository: str
    revision: str
    filename: str
    relative_path: str
    byte_length: int
    sha256: str

    @property
    def url(self) -> str:
        return (
            f"https://huggingface.co/{self.repository}/resolve/"
            f"{self.revision}/{self.filename}"
        )


# This private fixed tuple is monkeypatched only by small synthetic tests.
_ASSET_DESCRIPTORS = (
    _AssetDescriptor(
        repository=_MODEL_REPOSITORY,
        revision=_MODEL_REVISION,
        filename="model_optimized.onnx",
        relative_path="model/model_optimized.onnx",
        byte_length=556287754,
        sha256="0bb8cf275f98e1c337138bcecdf007876fb2a2b0eb4b5756ce627b2b0510c2c7",
    ),
    _AssetDescriptor(
        repository=_MODEL_REPOSITORY,
        revision=_MODEL_REVISION,
        filename="config.json",
        relative_path="model/config.json",
        byte_length=967,
        sha256="ed9094a56926e0ca1302f6c7ca9b11cf2ea0eced84d0f330ad938cba0fcc6209",
    ),
    _AssetDescriptor(
        repository=_TOKENIZER_REPOSITORY,
        revision=_TOKENIZER_REVISION,
        filename="tokenizer.json",
        relative_path="tokenizer/tokenizer.json",
        byte_length=9096718,
        sha256="a898ea75433890f6610f4e470b8ebeb0c21dce5c8dd61f892eb09eb5919d2e2c",
    ),
)


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.URLError("HTTPS redirect required")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _manifest_bytes() -> bytes:
    manifest = {
        "assets": [
            {
                "byte_length": asset.byte_length,
                "filename": asset.filename,
                "path": asset.relative_path,
                "repository": asset.repository,
                "revision": asset.revision,
                "sha256": asset.sha256,
                "url": asset.url,
            }
            for asset in _ASSET_DESCRIPTORS
        ],
        "schema_version": 1,
    }
    return (
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sat_asset_fingerprint() -> str:
    """Return the deterministic SHA-256 of the canonical fixed asset manifest."""

    return hashlib.sha256(_manifest_bytes()).hexdigest()


def _raise(code: SatAssetsErrorCode) -> NoReturn:
    raise SatAssetsError(code) from None


def _resolve_cache_root(root: Path, *, create: bool) -> Path:
    try:
        candidate = Path(os.path.abspath(os.fspath(root)))
    except OSError, TypeError, ValueError:
        _raise("model_assets_invalid")

    created = False
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if not create:
            _raise("model_assets_missing")
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            created = True
            metadata = candidate.lstat()
        except OSError:
            _raise("model_assets_download_failed")
    except OSError:
        _raise("model_assets_invalid")

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _raise("model_assets_invalid")

    if created and os.name == "posix":
        try:
            os.chmod(candidate, 0o700)
        except OSError:
            _raise("model_assets_download_failed")

    try:
        return candidate.resolve(strict=True)
    except OSError, RuntimeError:
        _raise("model_assets_invalid")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _raise("model_assets_invalid")


def _is_private(mode: int) -> bool:
    return os.name != "posix" or mode & 0o077 == 0


def _require_directory(path: Path, *, missing_code: SatAssetsErrorCode) -> None:
    metadata = _lstat(path)
    if metadata is None:
        _raise(missing_code)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not _is_private(metadata.st_mode)
    ):
        _raise("model_assets_invalid")


def _expected_layout() -> dict[str, set[str]]:
    layout: dict[str, set[str]] = {"model": set(), "tokenizer": set()}
    for asset in _ASSET_DESCRIPTORS:
        relative_path = PurePosixPath(asset.relative_path)
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 2
            or relative_path.parts[0] not in layout
            or relative_path.parts[1] in {"", ".", ".."}
        ):
            _raise("model_assets_invalid")
        layout[relative_path.parts[0]].add(relative_path.parts[1])
    if any(not filenames for filenames in layout.values()):
        _raise("model_assets_invalid")
    return layout


def _open_file(path: Path, *, write: bool) -> int:
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError:
        _raise("model_assets_invalid")


def _validate_asset_file(path: Path, asset: _AssetDescriptor, *, sync: bool) -> None:
    metadata = _lstat(path)
    if (
        metadata is None
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != asset.byte_length
        or (not sync and not _is_private(metadata.st_mode))
    ):
        _raise("model_assets_invalid")

    descriptor = _open_file(path, write=sync)
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_size != asset.byte_length
        ):
            _raise("model_assets_invalid")
        if sync and os.name == "posix":
            os.fchmod(descriptor, 0o600)

        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb" if not sync else "r+b") as stream:
            descriptor = -1
            while chunk := stream.read(_STREAM_CHUNK_BYTES):
                digest.update(chunk)
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
        if digest.hexdigest() != asset.sha256:
            _raise("model_assets_invalid")
    except OSError:
        _raise("model_assets_download_failed" if sync else "model_assets_invalid")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_generation(
    generation: Path,
    *,
    fingerprint: str,
    manifest: bytes,
    require_fingerprint_name: bool,
    sync_assets: bool,
) -> None:
    _require_directory(generation, missing_code="model_assets_missing")
    if require_fingerprint_name and generation.name != fingerprint:
        _raise("model_assets_invalid")

    layout = _expected_layout()
    try:
        if {entry.name for entry in generation.iterdir()} != {
            "manifest.json",
            "model",
            "tokenizer",
        }:
            _raise("model_assets_invalid")
    except OSError:
        _raise("model_assets_invalid")

    model_dir = generation / "model"
    tokenizer_dir = generation / "tokenizer"
    _require_directory(model_dir, missing_code="model_assets_invalid")
    _require_directory(tokenizer_dir, missing_code="model_assets_invalid")
    for directory_name, directory in (
        ("model", model_dir),
        ("tokenizer", tokenizer_dir),
    ):
        try:
            if {entry.name for entry in directory.iterdir()} != layout[directory_name]:
                _raise("model_assets_invalid")
        except OSError:
            _raise("model_assets_invalid")

    for asset in _ASSET_DESCRIPTORS:
        _validate_asset_file(
            generation.joinpath(*PurePosixPath(asset.relative_path).parts),
            asset,
            sync=sync_assets,
        )

    manifest_path = generation / "manifest.json"
    metadata = _lstat(manifest_path)
    if (
        metadata is None
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != len(manifest)
        or metadata.st_size > _MANIFEST_LIMIT
        or (not sync_assets and not _is_private(metadata.st_mode))
    ):
        _raise("model_assets_invalid")
    # Windows FlushFileBuffers requires write access. Reuse this verified
    # handle for synchronization rather than reopening a read-only handle.
    descriptor = _open_file(manifest_path, write=sync_assets)
    try:
        with os.fdopen(descriptor, "r+b" if sync_assets else "rb") as stream:
            descriptor = -1
            content = stream.read(_MANIFEST_LIMIT + 1)
            if len(content) > _MANIFEST_LIMIT or content != manifest:
                _raise("model_assets_invalid")
            if sync_assets:
                if os.name == "posix":
                    os.fchmod(stream.fileno(), 0o600)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        _raise(
            "model_assets_download_failed" if sync_assets else "model_assets_invalid"
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _assets_result(root: Path, generation: Path, fingerprint: str) -> SatModelAssets:
    return SatModelAssets(
        root=root,
        model_dir=generation / "model",
        tokenizer_dir=generation / "tokenizer",
        fingerprint=fingerprint,
    )


def verified_sat_assets(root: Path) -> SatModelAssets:
    """Read-only verification of the complete pinned generation in ``root``."""

    resolved_root = _resolve_cache_root(root, create=False)
    fingerprint = sat_asset_fingerprint()
    generation = resolved_root / fingerprint
    manifest = _manifest_bytes()
    if _lstat(generation) is None:
        _raise("model_assets_missing")
    _validate_generation(
        generation,
        fingerprint=fingerprint,
        manifest=manifest,
        require_fingerprint_name=True,
        sync_assets=False,
    )
    return _assets_result(resolved_root, generation, fingerprint)


def _fsync_directory_best_effort(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _default_fetch(url: str, destination: Path, byte_cap: int) -> None:
    """Fetch one fixed HTTPS asset with bounded streaming and no auth headers."""

    if urlsplit(url).scheme.lower() != "https" or byte_cap < 0:
        raise urllib.error.URLError("invalid fixed asset request")

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(),
        _HTTPSRedirectHandler(),
    )
    request = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "identity"},
        method="GET",
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    output_descriptor = -1
    try:
        with opener.open(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as raw_response:
            response = cast(addinfourl, raw_response)
            if (
                response.status != 200
                or urlsplit(response.url).scheme.lower() != "https"
            ):
                raise urllib.error.URLError("unexpected asset response")
            output_descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(output_descriptor, "wb") as output:
                output_descriptor = -1
                total = 0
                while chunk := response.read(
                    min(_STREAM_CHUNK_BYTES, byte_cap + 1 - total)
                ):
                    total += len(chunk)
                    if total > byte_cap:
                        raise urllib.error.URLError("asset byte cap exceeded")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except OSError, ValueError, urllib.error.URLError:
        raise urllib.error.URLError("asset download failed") from None
    finally:
        if output_descriptor >= 0:
            os.close(output_descriptor)


def _cleanup_stage(stage: Path) -> None:
    try:
        metadata = _lstat(stage)
    except SatAssetsError:
        return
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        with suppress(OSError):
            stage.unlink()
    elif stat.S_ISDIR(metadata.st_mode):
        with suppress(OSError):
            shutil.rmtree(stage)


def prepare_sat_assets(
    root: Path,
    *,
    fetch: Callable[[str, Path, int], None] | None = None,
) -> SatModelAssets:
    """Prepare or reuse the complete fixed generation under a caller-owned root."""

    resolved_root = _resolve_cache_root(root, create=True)
    fingerprint = sat_asset_fingerprint()
    generation = resolved_root / fingerprint
    manifest = _manifest_bytes()

    if _lstat(generation) is not None:
        _validate_generation(
            generation,
            fingerprint=fingerprint,
            manifest=manifest,
            require_fingerprint_name=True,
            sync_assets=False,
        )
        return _assets_result(resolved_root, generation, fingerprint)

    stage = resolved_root / f".{fingerprint}.tmp-{uuid.uuid4()}"
    owns_stage = False
    try:
        stage.mkdir(mode=0o700)
        owns_stage = True
        for directory_name in ("model", "tokenizer"):
            (stage / directory_name).mkdir(mode=0o700)
        for directory in (stage, stage / "model", stage / "tokenizer"):
            if os.name == "posix":
                os.chmod(directory, 0o700)

        fetcher = _default_fetch if fetch is None else fetch
        for asset in _ASSET_DESCRIPTORS:
            destination = stage.joinpath(*PurePosixPath(asset.relative_path).parts)
            try:
                fetcher(asset.url, destination, asset.byte_length)
            except Exception:  # Injected transport errors may carry secrets.
                _raise("model_assets_download_failed")

        manifest_path = stage / "manifest.json"
        try:
            descriptor = os.open(
                manifest_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(manifest)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            _raise("model_assets_download_failed")

        _validate_generation(
            stage,
            fingerprint=fingerprint,
            manifest=manifest,
            require_fingerprint_name=False,
            sync_assets=True,
        )
        for directory in (stage / "model", stage / "tokenizer", stage):
            _fsync_directory_best_effort(directory)

        try:
            os.rename(stage, generation)
        except OSError:
            if _lstat(generation) is None:
                _raise("model_assets_download_failed")
            _validate_generation(
                generation,
                fingerprint=fingerprint,
                manifest=manifest,
                require_fingerprint_name=True,
                sync_assets=False,
            )
            return _assets_result(resolved_root, generation, fingerprint)

        _fsync_directory_best_effort(resolved_root)
        return _assets_result(resolved_root, generation, fingerprint)
    except SatAssetsError:
        raise
    except OSError:
        _raise("model_assets_download_failed")
    finally:
        if owns_stage:
            _cleanup_stage(stage)
