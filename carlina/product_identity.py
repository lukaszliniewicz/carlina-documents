"""Canonical Carlina product identity and environment compatibility helpers."""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from carlina.runtime_profiles import RuntimeProfile


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    """Stable public identity exposed to Python, templates, and the frontend."""

    name: str
    short_name: str
    descriptor: str
    mark: str
    distribution_name: str
    cli_name: str
    environment_prefix: str
    import_package: str
    repository_url: str

    def public_dict(self) -> dict[str, str]:
        """Return the non-secret identity contract sent to browser clients."""

        return asdict(self)


PRODUCT_IDENTITY = ProductIdentity(
    name="Carlina",
    short_name="Carlina",
    descriptor="Local-first agent workbench",
    mark="C",
    distribution_name="carlina",
    cli_name="carlina",
    environment_prefix="CARLINA_",
    import_package="carlina",
    repository_url=("https://github.com/lukaszliniewicz/iarf_assistant-Refactor"),
)

LEGACY_ENVIRONMENT_PREFIX = "IARF_"


def release_repository_url(profile: RuntimeProfile | str) -> str:
    """Return the stable release repository for a resolved application profile."""

    selected_profile = RuntimeProfile(profile)
    if selected_profile is RuntimeProfile.DOCUMENTS:
        return "https://github.com/lukaszliniewicz/carlina-documents"
    return PRODUCT_IDENTITY.repository_url


def environment_value(
    canonical_name: str,
    *legacy_names: str,
    default: str | None = None,
    environment: Mapping[str, str] | None = None,
    warn_legacy: bool = True,
) -> str | None:
    """Read a canonical variable with ordered, non-destructive aliases.

    An explicitly present canonical variable wins even when its value is an
    empty string. Legacy aliases remain supported and emit a visible warning
    so operators can migrate configuration deliberately.
    """

    active_environment = environment if environment is not None else os.environ
    if canonical_name in active_environment:
        return active_environment[canonical_name]

    for legacy_name in legacy_names:
        if legacy_name not in active_environment:
            continue
        if warn_legacy:
            warnings.warn(
                (
                    f"{legacy_name} is deprecated; use {canonical_name} "
                    "instead. The legacy alias remains supported during the "
                    "Carlina compatibility period."
                ),
                FutureWarning,
                stacklevel=2,
            )
        return active_environment[legacy_name]
    return default


def product_environment_value(
    suffix: str,
    *,
    default: str | None = None,
    environment: Mapping[str, str] | None = None,
    warn_legacy: bool = True,
) -> str | None:
    """Read ``CARLINA_<suffix>`` with its legacy-prefix compatibility alias."""

    normalized_suffix = str(suffix or "").strip().upper()
    if not normalized_suffix:
        raise ValueError("Environment variable suffix cannot be empty.")
    return environment_value(
        f"{PRODUCT_IDENTITY.environment_prefix}{normalized_suffix}",
        f"{LEGACY_ENVIRONMENT_PREFIX}{normalized_suffix}",
        default=default,
        environment=environment,
        warn_legacy=warn_legacy,
    )


__all__ = [
    "LEGACY_ENVIRONMENT_PREFIX",
    "PRODUCT_IDENTITY",
    "ProductIdentity",
    "environment_value",
    "product_environment_value",
    "release_repository_url",
]
