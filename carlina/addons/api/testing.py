"""Dependency-light contract assertions for third-party add-on test suites."""

from __future__ import annotations

import json
from pathlib import Path

from iarf_assistant.core.addons.contracts import (
    AddonSourceKind,
    ContractValidation,
    validate_addon_contract,
)


class AddonContractAssertionError(AssertionError):
    """Raised when a package does not compile as an Add-on API v1 contract."""


def validate_package(package_dir: str | Path) -> ContractValidation:
    """Validate a source package without importing any package code."""

    root = Path(package_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return validate_addon_contract(
        manifest,
        addon_dir=root,
        source_kind=AddonSourceKind.INSTALLED,
        strict=True,
        provenance={"contract_test_path": str(root)},
    )


def assert_valid_package(package_dir: str | Path):
    """Return the immutable contract or raise a readable assertion failure."""

    result = validate_package(package_dir)
    if result.valid and result.contract is not None:
        return result.contract
    messages = [
        f"{item.code}{f' ({item.path})' if item.path else ''}: {item.message}"
        for item in result.errors
    ]
    raise AddonContractAssertionError("\n".join(messages))


__all__ = [
    "AddonContractAssertionError",
    "assert_valid_package",
    "validate_package",
]
