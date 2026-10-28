"""Flask adapter for the root-bound ordinary-content codec."""

from __future__ import annotations

from flask import current_app, has_app_context, has_request_context, session

from carlina.content_codec import ContentCodec, ContentPolicyError
from carlina.runtime_profiles import RootProfileBinding
from iarf_assistant.core.encryption import (
    encryption_service_from_context,
    resolve_legacy_user_key,
)


def content_codec_from_context(*, legacy_strategy: str = "user") -> ContentCodec:
    """Require an explicit binding; resolve encryption only when it is needed."""

    if not has_app_context():
        raise ContentPolicyError("Content storage requires an application context.")
    binding: object = current_app.extensions.get("carlina_profile_binding")
    if not isinstance(binding, RootProfileBinding):
        raise ContentPolicyError("Content storage policy is not configured.")
    if legacy_strategy not in {"system", "user"}:
        raise ContentPolicyError("Content storage legacy strategy is unsupported.")

    def encrypt_text(value: str) -> str:
        return encryption_service_from_context().encrypt(value)

    def decrypt_text(value: str) -> str:
        raw_token: object = (
            session.get("encryption_key") if has_request_context() else None
        )
        token = raw_token if isinstance(raw_token, str) else None
        secret = current_app.secret_key
        return encryption_service_from_context().decrypt(
            value,
            legacy_system_secret=secret,
            legacy_user_key=resolve_legacy_user_key(token),
            legacy_strategy=legacy_strategy,
        )

    return ContentCodec(binding=binding, encrypt=encrypt_text, decrypt=decrypt_text)


__all__ = ["content_codec_from_context"]
