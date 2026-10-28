"""Policy, marker, and callback behavior for the durable text codec."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from carlina.content_codec import (
    ENCRYPTED_PREFIX,
    PLAINTEXT_PREFIX,
    ContentCodec,
    ContentPolicyError,
    TextTransform,
)
from carlina.runtime_profiles import ContentPolicy, RootProfileBinding, RuntimeProfile

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _binding(
    policy: ContentPolicy,
    *,
    profile: RuntimeProfile = RuntimeProfile.DOCUMENTS,
    legacy: bool = False,
) -> RootProfileBinding:
    return RootProfileBinding(profile, policy, legacy)


@pytest.mark.parametrize(
    ("profile", "policy", "legacy"),
    [
        (RuntimeProfile.WORKBENCH, ContentPolicy.INSTALLATION_ENVELOPE_V2, False),
        (RuntimeProfile.WORKBENCH, ContentPolicy.INSTALLATION_ENVELOPE_V2, True),
        (RuntimeProfile.DOCUMENTS, ContentPolicy.INSTALLATION_ENVELOPE_V2, False),
        (RuntimeProfile.DOCUMENTS, ContentPolicy.INSTALLATION_ENVELOPE_V2, True),
        (RuntimeProfile.DOCUMENTS, ContentPolicy.PLAINTEXT_V1, False),
    ],
)
def test_supported_binding_combinations_construct_codec(
    profile: RuntimeProfile, policy: ContentPolicy, legacy: bool
):
    codec = ContentCodec(_binding(policy, profile=profile, legacy=legacy))

    assert codec.binding == RootProfileBinding(profile, policy, legacy)


@pytest.mark.parametrize(
    "binding",
    [
        RootProfileBinding(RuntimeProfile.WORKBENCH, ContentPolicy.PLAINTEXT_V1, False),
        RootProfileBinding(RuntimeProfile.WORKBENCH, ContentPolicy.PLAINTEXT_V1, True),
        RootProfileBinding(RuntimeProfile.DOCUMENTS, ContentPolicy.PLAINTEXT_V1, True),
        cast(RootProfileBinding, object()),
        cast(
            RootProfileBinding,
            RootProfileBinding(
                cast(RuntimeProfile, "documents"), ContentPolicy.PLAINTEXT_V1, False
            ),
        ),
        cast(
            RootProfileBinding,
            RootProfileBinding(
                RuntimeProfile.DOCUMENTS,
                cast(ContentPolicy, "plaintext-v1"),
                False,
            ),
        ),
        cast(
            RootProfileBinding,
            RootProfileBinding(
                RuntimeProfile.DOCUMENTS,
                ContentPolicy.PLAINTEXT_V1,
                cast(bool, 0),
            ),
        ),
    ],
)
def test_invalid_binding_combinations_fail_with_content_free_error(
    binding: RootProfileBinding,
):
    with pytest.raises(ContentPolicyError) as failure:
        ContentCodec(binding)

    assert str(failure.value) == "The content codec binding is invalid."


@pytest.mark.parametrize("value", [None, 0, b"text", object()])
def test_non_string_encode_and_decode_inputs_are_rejected(value: object):
    codec = ContentCodec(_binding(ContentPolicy.PLAINTEXT_V1))

    with pytest.raises(ContentPolicyError) as encode_failure:
        codec.encode_text(value)  # type: ignore[arg-type]
    with pytest.raises(ContentPolicyError) as decode_failure:
        codec.decode_text(value)  # type: ignore[arg-type]

    assert str(encode_failure.value) == "The content codec requires string text."
    assert str(decode_failure.value) == "The content codec requires string text."


def test_plaintext_round_trips_exact_unicode_empty_and_literal_markers():
    codec = ContentCodec(_binding(ContentPolicy.PLAINTEXT_V1))
    values = ["", "Καλημέρα — こんにちは 🌍", "enc:v2:literal", "plain:v1:literal"]

    for value in values:
        stored = codec.encode_text(value)
        assert stored == f"{PLAINTEXT_PREFIX}{value}"
        assert codec.decode_text(stored) == value


@pytest.mark.parametrize(
    "stored",
    ["", "untagged", "enc:v2:ciphertext", "plain:v2:text", "other:v1:text"],
)
def test_plaintext_policy_rejects_untagged_or_other_versions(stored: str):
    codec = ContentCodec(_binding(ContentPolicy.PLAINTEXT_V1))

    with pytest.raises(ContentPolicyError) as failure:
        codec.decode_text(stored)

    assert str(failure.value) == (
        "The stored text does not match the configured content policy."
    )


@pytest.mark.parametrize("value", ["unpaired-\ud800", "enc:v2:\udfff"])
def test_plaintext_policy_rejects_non_utf8_encodable_text(value: str):
    codec = ContentCodec(_binding(ContentPolicy.PLAINTEXT_V1))

    with pytest.raises(ContentPolicyError) as failure:
        codec.encode_text(value)

    assert str(failure.value) == "The content codec requires UTF-8-encodable text."
    assert value not in str(failure.value)


def test_plaintext_policy_needs_no_transform_callbacks():
    codec = ContentCodec(_binding(ContentPolicy.PLAINTEXT_V1))

    assert codec.encode_text("plain: stays literal") == "plain:v1:plain: stays literal"
    assert codec.decode_text("plain:v1:plain: stays literal") == "plain: stays literal"


def test_encrypted_policy_uses_and_verifies_exact_v2_envelope():
    binding = _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2)
    seen: list[str] = []

    def encrypt(value: str) -> str:
        return f"{ENCRYPTED_PREFIX}{value[::-1]}"

    def decrypt(value: str) -> str:
        seen.append(value)
        return value[len(ENCRYPTED_PREFIX) :][::-1]

    codec = ContentCodec(binding, encrypt=encrypt, decrypt=decrypt)
    stored = codec.encode_text("hello 世界")

    assert stored == "enc:v2:界世 olleh"
    assert codec.decode_text(stored) == "hello 世界"
    assert seen == [stored]


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "opaque-ciphertext",
        "sys:legacy-ciphertext",
        "usr:legacy-ciphertext",
        "enc:v1:ciphertext",
        "enc:v20:ciphertext",
        "plain:v1:plaintext",
        "plain:v2:plaintext",
        "other:v1:value",
    ],
)
def test_nonlegacy_encrypted_policy_rejects_every_non_v2_marker(stored: str):
    called = False

    def decrypt(value: str) -> str:
        nonlocal called
        called = True
        return value

    codec = ContentCodec(
        _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2), decrypt=decrypt
    )

    with pytest.raises(ContentPolicyError):
        codec.decode_text(stored)

    assert not called


@pytest.mark.parametrize(
    "stored",
    [
        "sys:legacy-ciphertext",
        "usr:legacy-ciphertext",
        "prefixless-ciphertext",
        "<p>legacy payload: https://example.invalid</p>",
        "enc:v2:current-ciphertext",
    ],
)
def test_legacy_encrypted_policy_passes_supported_forms_to_decrypt(stored: str):
    seen: list[str] = []

    def decrypt(value: str) -> str:
        seen.append(value)
        return "decoded"

    codec = ContentCodec(
        _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2, legacy=True),
        decrypt=decrypt,
    )

    assert codec.decode_text(stored) == "decoded"
    assert seen == [stored]


@pytest.mark.parametrize(
    "stored", ["plain:", "plain:v1:secret", "plain:v2:secret", "enc:v1:old"]
)
def test_legacy_encrypted_policy_rejects_plain_namespace_and_bad_versions(
    stored: str,
):
    codec = ContentCodec(
        _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2, legacy=True),
        decrypt=lambda value: value,
    )

    with pytest.raises(ContentPolicyError) as failure:
        codec.decode_text(stored)

    assert stored not in str(failure.value)


def test_encrypted_policy_requires_callbacks_without_fallback():
    codec = ContentCodec(_binding(ContentPolicy.INSTALLATION_ENVELOPE_V2))

    with pytest.raises(ContentPolicyError) as encode_failure:
        codec.encode_text("sensitive text")
    with pytest.raises(ContentPolicyError) as decode_failure:
        codec.decode_text("enc:v2:sensitive ciphertext")

    assert str(encode_failure.value) == (
        "The configured content codec callback is unavailable."
    )
    assert str(decode_failure.value) == (
        "The configured content codec callback is unavailable."
    )
    assert "sensitive" not in str(encode_failure.value)
    assert "sensitive" not in str(decode_failure.value)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (None, "The content codec callback returned invalid text."),
        (b"enc:v2:bytes", "The content codec callback returned invalid text."),
        ("plain:v1:text", "The content codec callback returned invalid text."),
        ("enc:v1:old", "The content codec callback returned invalid text."),
        ("enc:v2:\ud800", "The content codec requires UTF-8-encodable text."),
    ],
)
def test_encrypted_policy_rejects_invalid_encrypt_callback_results(
    result: object, message: str
):
    def encrypt(_value: str) -> str:
        return cast(str, result)

    codec = ContentCodec(
        _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2), encrypt=encrypt
    )

    with pytest.raises(ContentPolicyError) as failure:
        codec.encode_text("private input")

    assert str(failure.value) == message
    assert "private input" not in str(failure.value)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (None, "The content codec callback returned invalid text."),
        (b"decoded", "The content codec callback returned invalid text."),
        ("decoded\ud800", "The content codec requires UTF-8-encodable text."),
    ],
)
def test_encrypted_policy_rejects_invalid_decrypt_callback_results(
    result: object, message: str
):
    def decrypt(_value: str) -> str:
        return cast(str, result)

    codec = ContentCodec(
        _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2), decrypt=decrypt
    )

    with pytest.raises(ContentPolicyError) as failure:
        codec.decode_text("enc:v2:private ciphertext")

    assert str(failure.value) == message
    assert "private ciphertext" not in str(failure.value)


@pytest.mark.parametrize("operation", ["encode", "decode"])
def test_transform_exceptions_propagate_as_the_same_object(operation: str):
    failure = RuntimeError("transform failed")

    def transform(_value: str) -> str:
        raise failure

    if operation == "encode":
        codec = ContentCodec(
            _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2), encrypt=transform
        )
        with pytest.raises(RuntimeError) as caught:
            codec.encode_text("input")
    else:
        codec = ContentCodec(
            _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2), decrypt=transform
        )
        with pytest.raises(RuntimeError) as caught:
            codec.decode_text("enc:v2:ciphertext")

    assert caught.value is failure


def test_callback_fields_are_frozen_and_hidden_from_repr():
    class SecretTransform:
        def __call__(self, value: str) -> str:
            return f"{ENCRYPTED_PREFIX}{value}"

        def __repr__(self) -> str:
            return "secret-installation-key"

    transform = SecretTransform()
    codec = ContentCodec(
        _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2),
        encrypt=cast(TextTransform, transform),
        decrypt=cast(TextTransform, transform),
    )

    assert "secret-installation-key" not in repr(codec)
    with pytest.raises(FrozenInstanceError):
        codec.encrypt = None  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        codec.binding = _binding(ContentPolicy.PLAINTEXT_V1)  # type: ignore[misc]


@pytest.mark.parametrize("field_name", ["encrypt", "decrypt"])
def test_non_callable_transform_is_rejected_without_echoing_it(field_name: str):
    kwargs = {field_name: object()}

    with pytest.raises(ContentPolicyError) as failure:
        ContentCodec(
            _binding(ContentPolicy.INSTALLATION_ENVELOPE_V2),
            **kwargs,  # type: ignore[arg-type]
        )

    assert str(failure.value) == "The configured content codec callback is invalid."
