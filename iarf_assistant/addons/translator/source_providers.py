"""Capability-backed source references used by Translator.

Translator owns translation projects, but not the document or subtitle records
they refer to.  This module keeps those references portable and prevents the
consumer from importing provider ORM models.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CAPABILITY_PROVIDER_REFERENCE_VERSION = 1
_CAPABILITY_PROVIDER_REFERENCE_KEYS = frozenset(
    {
        "version",
        "capability",
        "provider_addon_id",
        "provider_capability_id",
        "resource_type",
        "resource_id",
    }
)
_CAPABILITY_PROVIDER_REQUIRED_KEYS = frozenset(
    {
        "version",
        "capability",
        "provider_addon_id",
        "provider_capability_id",
    }
)


def _provider_id(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or not value.isprintable()
    ):
        raise ValueError(f"{field} must be a non-empty string.")
    if len(value) > 255:
        raise ValueError(f"{field} is too long.")
    return value


def _strict_provider_reference(
    reference: Mapping[str, object], *, capability: str
) -> dict[str, object]:
    """Validate and copy a capability-only provider reference.

    Resource identity is deliberately optional.  When present it is preserved
    as supplied, but it is never derived by provider resolution.  This keeps a
    semantic-export provider pin independent from a document/source record.
    """

    if not isinstance(reference, Mapping):
        raise TypeError("Provider reference must be an object.")
    if set(reference) - _CAPABILITY_PROVIDER_REFERENCE_KEYS:
        raise ValueError("Provider reference contains unknown fields.")
    if (
        set(reference) & _CAPABILITY_PROVIDER_REQUIRED_KEYS
        != _CAPABILITY_PROVIDER_REQUIRED_KEYS
    ):
        raise ValueError("Provider reference is missing required fields.")
    version = reference.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CAPABILITY_PROVIDER_REFERENCE_VERSION
    ):
        raise ValueError("Provider reference version is unsupported.")
    if reference.get("capability") != capability:
        raise ValueError("Provider reference capability does not match.")

    normalized: dict[str, object] = {
        "version": CAPABILITY_PROVIDER_REFERENCE_VERSION,
        "capability": capability,
        "provider_addon_id": _provider_id(
            reference.get("provider_addon_id"), field="provider_addon_id"
        ),
        "provider_capability_id": _provider_id(
            reference.get("provider_capability_id"), field="provider_capability_id"
        ),
    }
    resource_type = reference.get("resource_type")
    resource_id = reference.get("resource_id")
    if resource_type is not None or resource_id is not None:
        normalized["resource_type"] = _provider_id(resource_type, field="resource_type")
        normalized["resource_id"] = _provider_id(resource_id, field="resource_id")
    return normalized


def capability_provider_reference(
    *,
    capability: str,
    provider_addon_id: str,
    provider_capability_id: str,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
) -> dict[str, object]:
    """Build one exact provider pin, optionally carrying resource identity."""

    reference: dict[str, object] = {
        "version": CAPABILITY_PROVIDER_REFERENCE_VERSION,
        "capability": capability,
        "provider_addon_id": provider_addon_id,
        "provider_capability_id": provider_capability_id,
    }
    if resource_type is not None or resource_id is not None:
        if resource_type is None or resource_id is None:
            raise ValueError("resource_type and resource_id must be paired.")
        reference.update(
            {
                "resource_type": resource_type,
                "resource_id": str(resource_id),
            }
        )
    return _strict_provider_reference(reference, capability=capability)


def capability_provider_reference_from_settings(
    settings: Mapping[str, object] | None,
    *,
    setting_key: str,
    capability: str,
) -> dict[str, object]:
    """Extract a strict provider pin from one named settings key.

    A missing key is treated as no pin so creation can select a provider.  A
    present foreign-capability pin is rejected instead of silently selecting a
    different current provider.
    """

    if not isinstance(settings, Mapping) or not isinstance(setting_key, str):
        return {}
    value = settings.get(setting_key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{setting_key} must contain a provider reference object.")
    if value.get("capability") != capability:
        raise ValueError("Provider reference capability does not match.")
    return _strict_provider_reference(value, capability=capability)


@dataclass(frozen=True, slots=True)
class ResolvedSourceProvider:
    requirement_id: str
    capability: str
    provider_addon_id: str
    provider_capability_id: str
    client: Any

    def provider_reference(self) -> dict[str, object]:
        """Return the provider pin without inventing resource identity."""

        return capability_provider_reference(
            capability=self.capability,
            provider_addon_id=self.provider_addon_id,
            provider_capability_id=self.provider_capability_id,
        )

    def reference(self, *, resource_type: str, resource_id: str | int) -> dict:
        return capability_provider_reference(
            capability=self.capability,
            provider_addon_id=self.provider_addon_id,
            provider_capability_id=self.provider_capability_id,
            resource_type=str(resource_type),
            resource_id=str(resource_id),
        )


def source_reference(settings: dict | None, *, capability: str) -> dict:
    """Return a compatible pinned source reference from project settings."""

    if not isinstance(settings, dict):
        return {}
    reference = settings.get("source_reference")
    if not isinstance(reference, dict):
        return {}
    declared_capability = str(reference.get("capability") or "").strip()
    if declared_capability and declared_capability != capability:
        return {}
    return dict(reference)


def resolve_source_provider(
    context,
    *,
    requirement_id: str,
    capability: str,
    user_id: int,
    reference: dict | None = None,
) -> ResolvedSourceProvider:
    """Resolve one declared provider, optionally pinned by a stored reference."""

    stored = dict(reference or {})
    declared_capability = stored.get("capability")
    if declared_capability and declared_capability != capability:
        raise ValueError("Provider reference capability does not match.")
    handle = context.get_capability_provider(
        requirement_id,
        int(user_id),
        provider_addon_id=(stored.get("provider_addon_id") or None),
        provider_capability_id=(stored.get("provider_capability_id") or None),
    )
    config = {
        "current_user_id": int(user_id),
        "capability_requirement_id": requirement_id,
        "capability_provider_addon_id": handle.provider_addon_id,
        "capability_provider_id": handle.provider_capability_id,
    }
    client = handle.initialize_client(config)
    return ResolvedSourceProvider(
        requirement_id=requirement_id,
        capability=capability,
        provider_addon_id=handle.provider_addon_id,
        provider_capability_id=handle.provider_capability_id,
        client=client,
    )


def resolve_capability_provider(
    context,
    *,
    requirement_id: str,
    capability: str,
    user_id: int,
    settings: Mapping[str, object] | None,
    setting_key: str,
) -> ResolvedSourceProvider:
    """Resolve a capability-only provider pin stored under one settings key."""

    return resolve_source_provider(
        context,
        requirement_id=requirement_id,
        capability=capability,
        user_id=user_id,
        reference=capability_provider_reference_from_settings(
            settings,
            setting_key=setting_key,
            capability=capability,
        ),
    )
