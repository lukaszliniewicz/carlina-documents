"""Typed settings events exposed to trusted add-on connection adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AddonConnectionSettingsUpdate:
    """One ephemeral, secret-bearing settings update.

    ``values`` may contain newly supplied secrets and therefore must never be
    logged or persisted wholesale. A connector hook consumes it inside the
    request transaction and writes secret fields through the Secret Broker.
    """

    addon_id: str
    user_id: int
    values: Mapping[str, Any]
    changed_fields: frozenset[str]


__all__ = ["AddonConnectionSettingsUpdate"]
