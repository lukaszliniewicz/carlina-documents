from __future__ import annotations

import base64

import pytest

from carlina.core.native_preparation import (
    MAX_PREPARATION_BYTES,
    PREPARATION_HEADER,
    PREPARATION_NAME,
    PREPARATION_PROTOCOL_VERSION,
    PreparationCompletion,
    PreparationRecordError,
    encode_preparation_completion,
    match_preparation_completion,
    parse_preparation_completion,
)

_CLAIM_ID = "0123456789abcdef0123456789abcdef"
_IDENTITY_SHA256 = "a" * 64
_FIXED_ERROR = "Native preparation completion is invalid."


def _completion(
    program_root: str = "/opt/carlina/program",
    native_exit_code: int = 0,
) -> PreparationCompletion:
    return PreparationCompletion(
        claim_id=_CLAIM_ID,
        identity_sha256=_IDENTITY_SHA256,
        program_root=program_root,
        native_exit_code=native_exit_code,
    )


def _frame(
    *,
    header: bytes | None = None,
    claim_id: bytes | None = None,
    identity_sha256: bytes | None = None,
    encoded_root: bytes | None = None,
    exit_code: bytes | None = None,
) -> bytes:
    valid = encode_preparation_completion(_completion())
    fields = valid.split(b"\n")
    assert len(fields) == 6 and fields[-1] == b""
    replacements = (
        header,
        claim_id,
        identity_sha256,
        encoded_root,
        exit_code,
    )
    return (
        b"\n".join(
            replacement if replacement is not None else fields[index]
            for index, replacement in enumerate(replacements)
        )
        + b"\n"
    )


@pytest.mark.parametrize(
    "program_root",
    [
        "/tmp/Carlina program \n'quoted' Ω",
        "C:\\Program Files\\Carlina\\資料\n'quoted'",
        "\\\\server\\share name\\Carlina\\данные",
    ],
)
def test_round_trip_preserves_absolute_roots_and_canonical_frame(
    program_root: str,
) -> None:
    completion = _completion(program_root, native_exit_code=17)

    payload = encode_preparation_completion(completion)

    assert payload.count(b"\n") == 5
    assert payload.endswith(b"\n")
    assert b"\r" not in payload
    assert all(byte < 128 for byte in payload)
    assert payload.split(b"\n")[0] == PREPARATION_HEADER.encode("ascii")
    assert parse_preparation_completion(payload) == completion


def test_protocol_constants_and_exact_field_order() -> None:
    completion = _completion()

    assert PREPARATION_PROTOCOL_VERSION == 1
    assert PREPARATION_NAME == ".carlina-bootstrap-preparation"
    assert encode_preparation_completion(completion) == (
        f"{PREPARATION_HEADER}\n{_CLAIM_ID}\n{_IDENTITY_SHA256}\n"
        f"{base64.b64encode(completion.program_root.encode()).decode()}\n0\n"
    ).encode("ascii")


@pytest.mark.parametrize(
    ("native_exit_code", "succeeded"),
    [(0, True), (1, False), (4_294_967_295, False)],
)
def test_uint32_exit_code_bounds_round_trip(
    native_exit_code: int, succeeded: bool
) -> None:
    completion = _completion(native_exit_code=native_exit_code)

    assert completion.succeeded is succeeded
    assert parse_preparation_completion(encode_preparation_completion(completion)) == (
        completion
    )


def test_program_root_limit_is_measured_in_utf8_bytes() -> None:
    at_limit = "/" + ("é" * 4_095) + ("a" * (8_191 - 8_190))
    assert len(at_limit.encode("utf-8")) == 8_192
    completion = _completion(at_limit)

    assert parse_preparation_completion(encode_preparation_completion(completion)) == (
        completion
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claim_id", "A" * 32),
        ("claim_id", "0" * 31),
        ("identity_sha256", "A" * 64),
        ("identity_sha256", "a" * 63),
        ("program_root", ""),
        ("program_root", "relative/program"),
        ("program_root", "C:drive-relative"),
        ("program_root", "/root/with\0nul"),
        ("program_root", "/root/with-surrogate-\ud800"),
        ("program_root", "/" + ("x" * 8_192)),
        ("native_exit_code", -1),
        ("native_exit_code", 4_294_967_296),
        ("native_exit_code", True),
        ("native_exit_code", False),
        ("native_exit_code", 1.0),
    ],
)
def test_invalid_completion_fields_raise_fixed_error(field: str, value: object) -> None:
    values: dict[str, object] = {
        "claim_id": _CLAIM_ID,
        "identity_sha256": _IDENTITY_SHA256,
        "program_root": "/opt/carlina/program",
        "native_exit_code": 0,
    }
    values[field] = value

    with pytest.raises(PreparationRecordError) as raised:
        PreparationCompletion(**values)  # type: ignore[arg-type]

    assert str(raised.value) == _FIXED_ERROR


def test_fields_require_exact_string_types() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(PreparationRecordError, match=f"^{_FIXED_ERROR}$"):
        PreparationCompletion(
            claim_id=StringSubclass(_CLAIM_ID),
            identity_sha256=_IDENTITY_SHA256,
            program_root="/opt/carlina/program",
            native_exit_code=0,
        )


@pytest.mark.parametrize(
    "payload",
    [
        _frame(header=b"carlina.bootstrap-preparation/2"),
        _frame(claim_id=b"F" * 32),
        _frame(identity_sha256=b"A" * 64),
        _frame(encoded_root=b"not-base64%"),
        _frame(encoded_root=b"Lx=="),  # Noncanonical pad bits for b"/".
        _frame(encoded_root=base64.b64encode(b"/\xff")),
        _frame(exit_code=b"+1"),
        _frame(exit_code=b"-1"),
        _frame(exit_code=b"01"),
        _frame(exit_code=b" 1"),
        _frame(exit_code=b"4294967296"),
        _frame(exit_code=b"1\t"),
        _frame(exit_code=b"1\x00"),
        _frame().replace(b"\n", b"\r\n"),
        b"\xef\xbb\xbf" + _frame(),
        _frame() + b"trailing-data",
        _frame() + b"extra\n",
        _frame()[:-1],
    ],
)
def test_malformed_frames_are_rejected_without_echoing_input(payload: bytes) -> None:
    with pytest.raises(PreparationRecordError) as raised:
        parse_preparation_completion(payload)

    assert str(raised.value) == _FIXED_ERROR
    assert raised.value.__cause__ is None


def test_decode_errors_suppress_payload_context() -> None:
    secret = b"PRIVATE-PAYLOAD-SENTINEL"

    with pytest.raises(PreparationRecordError) as raised:
        parse_preparation_completion(_frame(encoded_root=secret))

    assert str(raised.value) == _FIXED_ERROR
    assert secret.decode() not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_parser_rejects_wrong_payload_types_and_oversize_frames() -> None:
    for payload in (bytearray(_frame()), memoryview(_frame()), "not bytes"):
        with pytest.raises(PreparationRecordError, match=f"^{_FIXED_ERROR}$"):
            parse_preparation_completion(payload)  # type: ignore[arg-type]

    assert MAX_PREPARATION_BYTES == 16_384
    with pytest.raises(PreparationRecordError, match=f"^{_FIXED_ERROR}$"):
        parse_preparation_completion(b"x" * (MAX_PREPARATION_BYTES + 1))


def test_match_normalizes_roots_without_resolving_them() -> None:
    expected_root = "/missing-parent/../missing-parent/program"
    stored_root = "/missing-parent/program"
    payload = encode_preparation_completion(_completion(stored_root, 23))

    matched = match_preparation_completion(
        payload,
        claim_id=_CLAIM_ID,
        identity_sha256=_IDENTITY_SHA256,
        program_root=expected_root,
    )

    assert matched.program_root == stored_root
    assert matched.native_exit_code == 23
    assert not matched.succeeded


@pytest.mark.parametrize(
    "expected",
    [
        {"claim_id": "f" * 32},
        {"identity_sha256": "b" * 64},
        {"program_root": "/opt/other/program"},
    ],
)
def test_match_refuses_claim_identity_or_root_mismatch(
    expected: dict[str, str],
) -> None:
    values = {
        "claim_id": _CLAIM_ID,
        "identity_sha256": _IDENTITY_SHA256,
        "program_root": "/opt/carlina/program",
    }
    values.update(expected)

    with pytest.raises(PreparationRecordError, match=f"^{_FIXED_ERROR}$"):
        match_preparation_completion(
            encode_preparation_completion(_completion()), **values
        )


def test_match_validates_expected_values_before_comparing() -> None:
    with pytest.raises(PreparationRecordError, match=f"^{_FIXED_ERROR}$"):
        match_preparation_completion(
            encode_preparation_completion(_completion()),
            claim_id=_CLAIM_ID,
            identity_sha256=_IDENTITY_SHA256,
            program_root="C:drive-relative",
        )


def test_encoder_rejects_non_completion_input_with_fixed_error() -> None:
    with pytest.raises(PreparationRecordError, match=f"^{_FIXED_ERROR}$"):
        encode_preparation_completion(object())  # type: ignore[arg-type]
