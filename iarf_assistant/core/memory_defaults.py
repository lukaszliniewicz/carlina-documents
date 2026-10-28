"""Dependency-free defaults shared by memory settings and chat tools."""

from typing import Final

DEFAULT_MEMORY_CATEGORY_DEFINITIONS: Final[list[dict[str, str]]] = [
    {"key": "likes_dislikes", "label": "Likes & Dislikes"},
    {"key": "health_accessibility", "label": "Health & Accessibility"},
    {"key": "professional", "label": "Professional"},
    {"key": "family", "label": "Family"},
    {"key": "pastimes", "label": "Pastimes"},
    {"key": "communication_style", "label": "Communication Style"},
    {"key": "projects", "label": "Projects"},
    {"key": "constraints", "label": "Constraints"},
    {"key": "identity", "label": "Identity"},
    {"key": "organization_policy", "label": "Organization Policy"},
    {"key": "other", "label": "Other"},
]

MEMORY_TIER1_LIMIT_USER: Final[int] = 80
MEMORY_TIER1_LIMIT_ORG: Final[int] = 120
MEMORY_RECALL_INCLUDE_TIER2_DEFAULT: Final[bool] = False


def get_default_memory_category_definitions() -> list[dict[str, str]]:
    """Return a copy of the default categories and their dictionaries."""
    return [dict(entry) for entry in DEFAULT_MEMORY_CATEGORY_DEFINITIONS]
