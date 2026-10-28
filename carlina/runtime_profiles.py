"""Typed runtime profile and content-policy values for data roots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuntimeProfile(StrEnum):
    """Application composition selected for one data root."""

    WORKBENCH = "workbench"
    DOCUMENTS = "documents"


class ContentPolicy(StrEnum):
    """Supported codec policy for newly written durable content."""

    INSTALLATION_ENVELOPE_V2 = "installation-envelope-v2"
    PLAINTEXT_V1 = "plaintext-v1"


@dataclass(frozen=True, slots=True)
class RootProfileBinding:
    """Profile and codec associated with a root, including legacy status."""

    profile: RuntimeProfile
    content_policy: ContentPolicy
    legacy: bool


class RuntimeProfileError(RuntimeError):
    """A data root cannot safely be read or bound to a runtime profile."""


__all__ = [
    "ContentPolicy",
    "RootProfileBinding",
    "RuntimeProfile",
    "RuntimeProfileError",
]
