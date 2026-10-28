"""Codec for native preparation completion records."""

from __future__ import annotations

import base64
import binascii
import ntpath
import os
import posixpath
import re
from dataclasses import dataclass

PREPARATION_PROTOCOL_VERSION = 1
PREPARATION_NAME = ".carlina-bootstrap-preparation"
PREPARATION_HEADER = "carlina.bootstrap-preparation/1"
PREPARATION_RETIREMENT_DIRNAME = "preparation-retired"
MAX_PREPARATION_BYTES = 16_384
MAX_PROGRAM_ROOT_BYTES = 8_192

_ERROR_MESSAGE = "Native preparation completion is invalid."
_CLAIM_ID = re.compile(r"[0-9a-f]{32}\Z")
_CLAIM_ID_BYTES = re.compile(rb"[0-9a-f]{32}\Z")
_IDENTITY_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_SHA256_BYTES = re.compile(rb"[0-9a-f]{64}\Z")
_CANONICAL_DECIMAL = re.compile(rb"(?:0|[1-9][0-9]*)\Z")
_MAX_NATIVE_EXIT_CODE = 4_294_967_295


class PreparationRecordError(ValueError):
    """A native preparation completion record is invalid."""

    def __init__(self) -> None:
        super().__init__(_ERROR_MESSAGE)


def _invalid() -> PreparationRecordError:
    return PreparationRecordError()


def _validate_claim_id(claim_id: object) -> str:
    if type(claim_id) is not str or _CLAIM_ID.fullmatch(claim_id) is None:
        raise _invalid()
    return claim_id


def _validate_identity_sha256(identity_sha256: object) -> str:
    if (
        type(identity_sha256) is not str
        or _IDENTITY_SHA256.fullmatch(identity_sha256) is None
    ):
        raise _invalid()
    return identity_sha256


def _validate_program_root(program_root: object) -> bytes:
    if type(program_root) is not str or not program_root or "\0" in program_root:
        raise _invalid()
    try:
        root_bytes = program_root.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    if len(root_bytes) > MAX_PROGRAM_ROOT_BYTES or not (
        posixpath.isabs(program_root) or ntpath.isabs(program_root)
    ):
        raise _invalid()
    return root_bytes


def _validate_native_exit_code(native_exit_code: object) -> int:
    if (
        type(native_exit_code) is not int
        or not 0 <= native_exit_code <= _MAX_NATIVE_EXIT_CODE
    ):
        raise _invalid()
    return native_exit_code


@dataclass(frozen=True, slots=True)
class PreparationCompletion:
    """Completion facts emitted by a native preparation process."""

    claim_id: str
    identity_sha256: str
    program_root: str
    native_exit_code: int

    def __post_init__(self) -> None:
        _validate_claim_id(self.claim_id)
        _validate_identity_sha256(self.identity_sha256)
        _validate_program_root(self.program_root)
        _validate_native_exit_code(self.native_exit_code)

    @property
    def succeeded(self) -> bool:
        return self.native_exit_code == 0


def encode_preparation_completion(completion: PreparationCompletion) -> bytes:
    """Encode one completion as five canonical, LF-terminated ASCII lines."""

    if type(completion) is not PreparationCompletion:
        raise _invalid()
    claim_id = _validate_claim_id(completion.claim_id)
    identity_sha256 = _validate_identity_sha256(completion.identity_sha256)
    program_root = _validate_program_root(completion.program_root)
    native_exit_code = _validate_native_exit_code(completion.native_exit_code)
    encoded_root = base64.b64encode(program_root).decode("ascii")
    return (
        f"{PREPARATION_HEADER}\n{claim_id}\n{identity_sha256}\n"
        f"{encoded_root}\n{native_exit_code}\n"
    ).encode("ascii")


def parse_preparation_completion(payload: bytes) -> PreparationCompletion:
    """Parse and validate one bounded, canonical completion frame."""

    if type(payload) is not bytes or len(payload) > MAX_PREPARATION_BYTES:
        raise _invalid()

    lines = payload.split(b"\n")
    if len(lines) != 6 or lines[-1] != b"":
        raise _invalid()
    header, claim_id, identity_sha256, encoded_root, exit_code_text, _ = lines
    if (
        header != PREPARATION_HEADER.encode("ascii")
        or _CLAIM_ID_BYTES.fullmatch(claim_id) is None
        or _IDENTITY_SHA256_BYTES.fullmatch(identity_sha256) is None
        or _CANONICAL_DECIMAL.fullmatch(exit_code_text) is None
        or len(exit_code_text) > 10
    ):
        raise _invalid()

    try:
        root_bytes = base64.b64decode(encoded_root, validate=True)
    except binascii.Error, ValueError:
        raise _invalid() from None
    if base64.b64encode(root_bytes) != encoded_root:
        raise _invalid()

    try:
        program_root = root_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None

    native_exit_code = int(exit_code_text)
    if native_exit_code > _MAX_NATIVE_EXIT_CODE:
        raise _invalid()

    try:
        return PreparationCompletion(
            claim_id=claim_id.decode("ascii"),
            identity_sha256=identity_sha256.decode("ascii"),
            program_root=program_root,
            native_exit_code=native_exit_code,
        )
    except UnicodeDecodeError:
        raise _invalid() from None


def match_preparation_completion(
    payload: bytes,
    *,
    claim_id: str,
    identity_sha256: str,
    program_root: str,
) -> PreparationCompletion:
    """Parse a completion and require it to match caller-supplied identity facts."""

    expected = PreparationCompletion(
        claim_id=claim_id,
        identity_sha256=identity_sha256,
        program_root=program_root,
        native_exit_code=0,
    )
    completion = parse_preparation_completion(payload)
    if (
        completion.claim_id != expected.claim_id
        or completion.identity_sha256 != expected.identity_sha256
        or os.path.normcase(os.path.normpath(completion.program_root))
        != os.path.normcase(os.path.normpath(expected.program_root))
    ):
        raise _invalid()
    return completion
