"""Protected-secret and ordinary-content SQLAlchemy storage types."""

from __future__ import annotations

import json

from flask import current_app, has_request_context, session
from sqlalchemy import types as sqltypes
from sqlalchemy.types import Text, TypeDecorator

from carlina.content_codec import ContentPolicyError
from carlina.content_storage import content_codec_from_context
from carlina.runtime_profiles import ContentPolicy
from iarf_assistant.core.encryption import (
    encryption_service_from_context,
    resolve_legacy_user_key,
)


class EncryptedType(TypeDecorator):
    """Persist text/JSON as authenticated, versioned encryption envelopes.

    ``strategy`` is retained solely to decode legacy ``sys:``/``usr:`` and
    prefixless rows.  Every new value uses the persistent installation key so
    password changes, background work, and restarts share one deliberate key
    lifecycle.
    """

    impl = Text
    cache_ok = True

    def __init__(self, underlying_type: object = None, strategy: str = "user", **kwargs):
        super().__init__(**kwargs)
        if strategy not in {"system", "user"}:
            raise ValueError("EncryptedType strategy must be 'system' or 'user'.")
        self.underlying_type = underlying_type
        self.strategy = strategy
        self.underlying_type_instance: object = (
            underlying_type() if isinstance(underlying_type, type) else underlying_type
        )
        self.is_json = isinstance(self.underlying_type_instance, sqltypes.JSON)

    @staticmethod
    def _make_utf8_safe_text(value):
        if not isinstance(value, str):
            return value
        return value.encode("utf-8", errors="replace").decode("utf-8")

    @classmethod
    def _make_utf8_safe_json(cls, value):
        if isinstance(value, str):
            return cls._make_utf8_safe_text(value)
        if isinstance(value, list):
            return [cls._make_utf8_safe_json(item) for item in value]
        if isinstance(value, tuple):
            return [cls._make_utf8_safe_json(item) for item in value]
        if isinstance(value, dict):
            return {
                cls._make_utf8_safe_text(key)
                if isinstance(key, str)
                else key: cls._make_utf8_safe_json(item)
                for key, item in value.items()
            }
        return value

    def _serialize_value_for_storage(self, value) -> str:
        if self.is_json:
            return json.dumps(
                self._make_utf8_safe_json(value),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return self._make_utf8_safe_text(str(value))

    def process_bind_param(self, value, dialect):
        """Encrypt every non-null write; absence of a key is a hard failure."""

        del dialect
        if value is None:
            return None
        plaintext = self._serialize_value_for_storage(value)
        return encryption_service_from_context().encrypt(plaintext)

    def process_result_value(self, value, dialect):
        """Decrypt strict envelopes while preserving documented legacy reads."""

        del dialect
        if value is None:
            return None
        legacy_user_key = resolve_legacy_user_key(
            session.get("encryption_key") if has_request_context() else None
        )
        plaintext = encryption_service_from_context().decrypt(
            value,
            legacy_system_secret=current_app.secret_key,
            legacy_user_key=legacy_user_key,
            legacy_strategy=self.strategy,
        )
        if not self.is_json:
            return plaintext
        if not isinstance(plaintext, str) or not plaintext.strip():
            return None
        try:
            return json.loads(plaintext)
        except (json.JSONDecodeError, TypeError):
            # This is compatible only for historical plaintext JSON rows.  An
            # authenticated v2 envelope has already passed integrity checks.
            return None


class ContentType(EncryptedType):
    """Persist ordinary text/JSON using the explicitly bound root policy.

    SQL storage remains TEXT. ``EncryptedType`` retains its unconditional
    protection for secrets and mixed-purpose settings.
    """

    cache_ok = True

    def process_bind_param(self, value, dialect):
        del dialect
        if value is None:
            return None
        return content_codec_from_context(legacy_strategy=self.strategy).encode_text(
            self._serialize_value_for_storage(value)
        )

    def process_result_value(self, value, dialect):
        del dialect
        if value is None:
            return None
        codec = content_codec_from_context(legacy_strategy=self.strategy)
        plaintext = codec.decode_text(value)
        if not self.is_json:
            return plaintext
        try:
            return json.loads(plaintext)
        except (json.JSONDecodeError, TypeError):
            if codec.binding.content_policy is ContentPolicy.PLAINTEXT_V1:
                raise ContentPolicyError("Stored content JSON is invalid.") from None
            # Preserve the existing encrypted-root historical JSON read contract.
            return None


__all__ = ["ContentType", "EncryptedType"]
