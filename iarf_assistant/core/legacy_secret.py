"""Command-scoped loading for a legacy Flask secret kept outside Carlina data."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path

FLASK_SECRET_KEY = "FLASK_SECRET_KEY"
# A legacy Flask secret is a short opaque value, not a certificate or key bundle.
# Bound reads to 4 KiB so an accidental or hostile file cannot consume memory.
MAX_LEGACY_SECRET_BYTES = 4 * 1024


class LegacySecretFileError(RuntimeError):
    """Raised when a command-scoped legacy-secret file is unsafe or malformed."""


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _validate_file_stat(file_stat: os.stat_result) -> None:
    if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat):
        raise LegacySecretFileError("Legacy secret file must not be a link.")
    if not stat.S_ISREG(file_stat.st_mode):
        raise LegacySecretFileError("Legacy secret file must be a regular file.")
    if os.name == "posix":
        if file_stat.st_uid != os.geteuid():
            raise LegacySecretFileError(
                "Legacy secret file must be owned by the current user."
            )
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise LegacySecretFileError(
                "Legacy secret file must not grant group or other permissions."
            )


def _same_opened_file(before_open: os.stat_result, opened: os.stat_result) -> bool:
    return before_open.st_dev == opened.st_dev and before_open.st_ino == opened.st_ino


def _supports_secure_legacy_secret_files(*, platform_name: str | None = None) -> bool:
    """Whether stdlib checks can enforce this command's file contract."""

    return (os.name if platform_name is None else platform_name) != "nt"


def _read_bounded_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_LEGACY_SECRET_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > MAX_LEGACY_SECRET_BYTES:
        raise LegacySecretFileError(
            f"Legacy secret file must be at most {MAX_LEGACY_SECRET_BYTES} bytes."
        )
    return payload


def _normalize_secret(payload: bytes) -> str:
    if b"\x00" in payload:
        raise LegacySecretFileError("Legacy secret file must not contain NUL bytes.")
    try:
        secret = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LegacySecretFileError("Legacy secret file must be valid UTF-8.") from exc

    if secret.endswith("\r\n"):
        secret = secret[:-2]
    elif secret.endswith("\n"):
        secret = secret[:-1]
    if "\r" in secret or "\n" in secret:
        raise LegacySecretFileError(
            "Legacy secret file must contain exactly one secret record."
        )
    if not secret:
        raise LegacySecretFileError("Legacy secret file must not be empty.")
    return secret


def read_legacy_secret_file(path: str | os.PathLike[str]) -> str:
    """Read one secure, UTF-8 legacy secret without retaining the source path.

    On POSIX, opening with ``O_NOFOLLOW`` and checking descriptor metadata closes
    the practical lstat/open race. Windows is refused until native ACL and
    effective-access verification is available.
    """

    if not _supports_secure_legacy_secret_files():
        raise LegacySecretFileError(
            "Legacy secret files are not supported on this platform."
        )
    candidate = Path(path)
    if not candidate.is_absolute():
        raise LegacySecretFileError("Legacy secret file path must be absolute.")
    try:
        before_open = os.lstat(candidate)
        _validate_file_stat(before_open)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(candidate, flags)
    except (OSError, ValueError) as exc:
        raise LegacySecretFileError(
            "Could not securely open legacy secret file."
        ) from exc

    try:
        opened = os.fstat(descriptor)
        _validate_file_stat(opened)
        if not _same_opened_file(before_open, opened):
            raise LegacySecretFileError(
                "Legacy secret file changed while it was being opened."
            )
        return _normalize_secret(_read_bounded_descriptor(descriptor))
    except OSError as exc:
        raise LegacySecretFileError(
            "Could not securely read legacy secret file."
        ) from exc
    finally:
        os.close(descriptor)


def install_legacy_secret_from_file(
    path: str | os.PathLike[str],
    *,
    environment: MutableMapping[str, str] | None = None,
) -> str:
    """Install the one command-scoped secret into the compatibility environment."""

    active_environment = os.environ if environment is None else environment
    if FLASK_SECRET_KEY in active_environment:
        raise LegacySecretFileError(
            "FLASK_SECRET_KEY is already set; refusing an ambiguous legacy secret."
        )
    secret = read_legacy_secret_file(path)
    active_environment[FLASK_SECRET_KEY] = secret
    return secret


@contextmanager
def legacy_secret_file_environment(
    path: str | os.PathLike[str] | None,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> Iterator[None]:
    """Temporarily install a file secret without overwriting another owner."""

    if path is None:
        yield
        return
    active_environment = os.environ if environment is None else environment
    installed_secret = install_legacy_secret_from_file(
        path, environment=active_environment
    )
    try:
        yield
    finally:
        if active_environment.get(FLASK_SECRET_KEY) == installed_secret:
            active_environment.pop(FLASK_SECRET_KEY, None)


__all__ = [
    "FLASK_SECRET_KEY",
    "MAX_LEGACY_SECRET_BYTES",
    "LegacySecretFileError",
    "install_legacy_secret_from_file",
    "legacy_secret_file_environment",
    "read_legacy_secret_file",
]
