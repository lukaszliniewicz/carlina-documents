"""Bounded private-pipe input for first-administrator setup."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import BinaryIO, cast

MAX_ADMIN_INPUT_BYTES = 8192


class InitialAdminInputError(ValueError):
    """Administrator input is invalid, without including its contents."""

    def __init__(self) -> None:
        super().__init__("Initial administrator input is invalid.")


@dataclass(frozen=True, repr=False)
class AdminCredentials:
    """Credentials held only in memory during local initialization."""

    email: str
    password: str


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InitialAdminInputError()
        result[key] = value
    return result


def parse_initial_admin(payload: bytes) -> AdminCredentials:
    """Validate one closed UTF-8 JSON record; never echo input in errors."""
    if not payload or len(payload) > MAX_ADMIN_INPUT_BYTES:
        raise InitialAdminInputError()
    try:
        decoded: object = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
    except ValueError, UnicodeError, RecursionError:
        raise InitialAdminInputError() from None
    if not isinstance(decoded, dict):
        raise InitialAdminInputError()
    record = cast(dict[str, object], decoded)
    if set(record) != {"email", "password"}:
        raise InitialAdminInputError()
    email, password = record["email"], record["password"]
    if not isinstance(email, str) or not isinstance(password, str):
        raise InitialAdminInputError()
    email = email.strip()
    if (
        not 1 <= len(email) <= 100
        or email.count("@") != 1
        or email.startswith("@")
        or email.endswith("@")
        or any(ord(char) <= 32 or ord(char) == 127 for char in email)
        or not password
        or "\0" in password
    ):
        raise InitialAdminInputError()
    try:
        email.encode("utf-8")
        if len(password.encode("utf-8")) > 4096:
            raise InitialAdminInputError()
    except UnicodeError:
        raise InitialAdminInputError() from None
    return AdminCredentials(email, password)


def read_initial_admin(stream: BinaryIO) -> AdminCredentials:
    """Read a bounded pipe until EOF, supplied by the local setup controller."""
    try:
        payload = stream.read(MAX_ADMIN_INPUT_BYTES + 1)
    except OSError, ValueError:
        raise InitialAdminInputError() from None
    return parse_initial_admin(payload)


def encode_initial_admin(credentials: AdminCredentials) -> str:
    """Encode the validated pipe record without changing the password."""
    payload = json.dumps(
        {"email": credentials.email, "password": credentials.password},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        parse_initial_admin(payload.encode("utf-8"))
    except UnicodeError:
        raise InitialAdminInputError() from None
    return payload
