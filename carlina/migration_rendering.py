"""Alembic rendering for declared secret and ordinary-content storage types."""

from __future__ import annotations

from typing import Literal

from alembic.autogenerate.api import AutogenContext
from sqlalchemy import types as sqltypes

from iarf_assistant.database.types import ContentType, EncryptedType


def _underlying_expression(value: object) -> str:
    if value is None:
        return "None"
    if isinstance(value, sqltypes.JSON):
        return f"sa.JSON(none_as_null={value.none_as_null!r})"
    if isinstance(value, sqltypes.Text):
        return f"sa.Text(collation={value.collation!r})"
    if isinstance(value, sqltypes.String):
        return f"sa.String(length={value.length!r}, collation={value.collation!r})"
    raise ValueError("Storage migration underlying type is unsupported.")


def render_storage_type(
    kind: str, value: object, autogen_context: AutogenContext
) -> str | Literal[False]:
    """Preserve purpose and legacy strategy without private Alembic APIs."""

    if kind != "type" or not isinstance(value, EncryptedType):
        return False
    name = "ContentType" if isinstance(value, ContentType) else "EncryptedType"
    autogen_context.imports.add(f"from iarf_assistant.database.types import {name}")
    return (
        f"{name}({_underlying_expression(value.underlying_type_instance)}, "
        f"strategy={value.strategy!r})"
    )


__all__ = ["render_storage_type"]
