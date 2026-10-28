from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from iarf_assistant import chat_builtin_tools
from iarf_assistant.core import memory_defaults

EXPECTED_MEMORY_CATEGORY_DEFINITIONS = [
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


def test_memory_defaults_values_and_copy_isolation() -> None:
    defaults = memory_defaults.DEFAULT_MEMORY_CATEGORY_DEFINITIONS

    assert defaults == EXPECTED_MEMORY_CATEGORY_DEFINITIONS
    assert memory_defaults.MEMORY_TIER1_LIMIT_USER == 80
    assert memory_defaults.MEMORY_TIER1_LIMIT_ORG == 120
    assert memory_defaults.MEMORY_RECALL_INCLUDE_TIER2_DEFAULT is False

    first_copy = memory_defaults.get_default_memory_category_definitions()
    second_copy = memory_defaults.get_default_memory_category_definitions()
    assert first_copy is not second_copy
    assert first_copy[0] is not second_copy[0]
    assert first_copy[0] is not defaults[0]

    first_copy[0]["label"] = "Changed"
    first_copy.pop()

    assert second_copy == EXPECTED_MEMORY_CATEGORY_DEFINITIONS
    assert defaults == EXPECTED_MEMORY_CATEGORY_DEFINITIONS


def test_chat_builtin_tools_reexports_memory_defaults() -> None:
    assert (
        chat_builtin_tools.DEFAULT_MEMORY_CATEGORY_DEFINITIONS
        is memory_defaults.DEFAULT_MEMORY_CATEGORY_DEFINITIONS
    )
    assert (
        chat_builtin_tools.MEMORY_TIER1_LIMIT_USER
        == memory_defaults.MEMORY_TIER1_LIMIT_USER
    )
    assert (
        chat_builtin_tools.MEMORY_TIER1_LIMIT_ORG
        == memory_defaults.MEMORY_TIER1_LIMIT_ORG
    )
    assert (
        chat_builtin_tools.MEMORY_RECALL_INCLUDE_TIER2_DEFAULT
        is memory_defaults.MEMORY_RECALL_INCLUDE_TIER2_DEFAULT
    )
    assert (
        chat_builtin_tools.get_default_memory_category_definitions
        is memory_defaults.get_default_memory_category_definitions
    )
    assert [
        entry["key"] for entry in EXPECTED_MEMORY_CATEGORY_DEFINITIONS
    ] == chat_builtin_tools.MEMORY_CATEGORIES
    assert {
        entry["key"]: entry["label"] for entry in EXPECTED_MEMORY_CATEGORY_DEFINITIONS
    } == chat_builtin_tools.MEMORY_CATEGORY_LABELS


def test_memory_defaults_import_does_not_load_chat_or_application_modules() -> None:
    script = """
import sys
import iarf_assistant.core.memory_defaults

forbidden_prefixes = (
    "iarf_assistant.chat_",
    "iarf_assistant.models",
    "iarf_assistant.extensions",
    "iarf_assistant.database",
)
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix) for prefix in forbidden_prefixes)
)
assert not loaded, loaded
"""
    repository_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
