"""Explicit product and service selection for the shared application factory."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from carlina.distribution import read_application_distribution
from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError

DOCUMENT_ADDON_IDS = frozenset({"doc_processor", "translator", "sentence_segmenter"})


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """Fixed selections owned by an application, rather than add-on settings."""

    profile: RuntimeProfile
    addon_ids: frozenset[str] | None
    assistant_services: bool
    container_services: bool

    @property
    def default_http_port(self) -> int:
        """Leave distinct listener pairs for each app and its optional MCP port."""

        return 5005 if self.profile is RuntimeProfile.DOCUMENTS else 5003


def resolve_runtime_composition(
    profile: RuntimeProfile | str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> RuntimeComposition:
    """Resolve an exact profile within the immutable distribution's boundary."""

    distribution = read_application_distribution()
    active_environment = os.environ if environment is None else environment
    selected = profile
    if selected is None:
        selected = active_environment.get("CARLINA_PROFILE", distribution.profile)
    try:
        selected_profile = RuntimeProfile(selected)
    except ValueError, TypeError:
        raise RuntimeProfileError(
            "The requested runtime profile is unsupported."
        ) from None
    if (
        distribution.profile is RuntimeProfile.DOCUMENTS
        and selected_profile is not RuntimeProfile.DOCUMENTS
    ):
        raise RuntimeProfileError(
            "This application distribution supports only the Documents profile."
        )
    if selected_profile is RuntimeProfile.DOCUMENTS:
        return RuntimeComposition(selected_profile, DOCUMENT_ADDON_IDS, False, False)
    return RuntimeComposition(selected_profile, None, True, True)


__all__ = ["DOCUMENT_ADDON_IDS", "RuntimeComposition", "resolve_runtime_composition"]
