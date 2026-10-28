"""Root-bound codecs for durable text content.

This module is deliberately independent of storage, Flask, and cryptography.
The host supplies any encryption transforms after resolving the root policy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from carlina.runtime_profiles import ContentPolicy, RootProfileBinding, RuntimeProfile

PLAINTEXT_PREFIX = "plain:v1:"
ENCRYPTED_PREFIX = "enc:v2:"

type TextTransform = Callable[[str], str]

_PLAINTEXT_NAMESPACE = "plain:"
_ENCRYPTED_NAMESPACE = "enc:"

_BINDING_ERROR = "The content codec binding is invalid."
_CALLBACK_ERROR = "The configured content codec callback is unavailable."
_CALLBACK_INVALID_ERROR = "The configured content codec callback is invalid."
_CALLBACK_RESULT_ERROR = "The content codec callback returned invalid text."
_POLICY_ERROR = "The stored text does not match the configured content policy."
_TEXT_TYPE_ERROR = "The content codec requires string text."
_UTF8_ERROR = "The content codec requires UTF-8-encodable text."


class ContentPolicyError(RuntimeError):
    """Text cannot be safely encoded or decoded under its root policy."""


def _checked_text(value: object, *, error: str) -> str:
    if type(value) is not str:
        raise ContentPolicyError(error)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ContentPolicyError(_UTF8_ERROR) from None
    return value


@dataclass(frozen=True, slots=True)
class ContentCodec:
    """Encode and decode text using one already-resolved root binding."""

    binding: RootProfileBinding
    encrypt: TextTransform | None = field(default=None, repr=False)
    decrypt: TextTransform | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        binding = self.binding
        if (
            type(binding) is not RootProfileBinding
            or type(binding.profile) is not RuntimeProfile
            or type(binding.content_policy) is not ContentPolicy
            or type(binding.legacy) is not bool
        ):
            raise ContentPolicyError(_BINDING_ERROR)

        if binding.content_policy is ContentPolicy.PLAINTEXT_V1 and (
            binding.profile is not RuntimeProfile.DOCUMENTS or binding.legacy
        ):
            raise ContentPolicyError(_BINDING_ERROR)

        if self.encrypt is not None and not callable(self.encrypt):
            raise ContentPolicyError(_CALLBACK_INVALID_ERROR)
        if self.decrypt is not None and not callable(self.decrypt):
            raise ContentPolicyError(_CALLBACK_INVALID_ERROR)

    def encode_text(self, value: str) -> str:
        """Encode a text value without changing or inferring its policy."""

        text = _checked_text(value, error=_TEXT_TYPE_ERROR)
        if self.binding.content_policy is ContentPolicy.PLAINTEXT_V1:
            return f"{PLAINTEXT_PREFIX}{text}"

        if self.binding.content_policy is not ContentPolicy.INSTALLATION_ENVELOPE_V2:
            raise ContentPolicyError(_BINDING_ERROR)
        if self.encrypt is None:
            raise ContentPolicyError(_CALLBACK_ERROR)

        encoded = _checked_text(self.encrypt(text), error=_CALLBACK_RESULT_ERROR)
        if not encoded.startswith(ENCRYPTED_PREFIX):
            raise ContentPolicyError(_CALLBACK_RESULT_ERROR)
        return encoded

    def decode_text(self, value: str) -> str:
        """Decode a stored value only when its marker matches the root policy."""

        stored = _checked_text(value, error=_TEXT_TYPE_ERROR)
        policy = self.binding.content_policy

        if policy is ContentPolicy.PLAINTEXT_V1:
            if not stored.startswith(PLAINTEXT_PREFIX):
                raise ContentPolicyError(_POLICY_ERROR)
            return stored[len(PLAINTEXT_PREFIX) :]

        if policy is not ContentPolicy.INSTALLATION_ENVELOPE_V2:
            raise ContentPolicyError(_BINDING_ERROR)
        if stored.startswith(_PLAINTEXT_NAMESPACE):
            raise ContentPolicyError(_POLICY_ERROR)

        if stored.startswith(_ENCRYPTED_NAMESPACE):
            if not stored.startswith(ENCRYPTED_PREFIX):
                raise ContentPolicyError(_POLICY_ERROR)
        elif not self.binding.legacy:
            raise ContentPolicyError(_POLICY_ERROR)

        if self.decrypt is None:
            raise ContentPolicyError(_CALLBACK_ERROR)
        return _checked_text(self.decrypt(stored), error=_CALLBACK_RESULT_ERROR)


__all__ = [
    "ENCRYPTED_PREFIX",
    "PLAINTEXT_PREFIX",
    "ContentCodec",
    "ContentPolicyError",
    "TextTransform",
]
