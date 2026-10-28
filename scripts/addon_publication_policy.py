"""Fail-closed policy for add-ons included in whole-application releases."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

LEDGER_RELATIVE_PATH = Path("docs/addon-manifest-migration-ledger.json")
ADDONS_RELATIVE_PATH = Path("iarf_assistant/addons")
PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PUBLIC_DISTRIBUTIONS = frozenset({"built_in", "first_party", "community"})
KNOWN_DISTRIBUTIONS = PUBLIC_DISTRIBUTIONS | frozenset(
    {"private", "local_development"}
)


class AddonPublicationPolicyError(ValueError):
    """The release tree and its reviewed add-on ledger disagree."""


@dataclass(frozen=True, slots=True)
class AddonPublicationPolicy:
    package_channels: tuple[tuple[str, str], ...]

    @property
    def package_ids(self) -> tuple[str, ...]:
        return tuple(package_id for package_id, _channel in self.package_channels)

    @property
    def private_ids(self) -> tuple[str, ...]:
        return tuple(
            package_id
            for package_id, channel in self.package_channels
            if channel == "private"
        )

    @property
    def public_ids(self) -> tuple[str, ...]:
        return tuple(
            package_id
            for package_id, channel in self.package_channels
            if channel in PUBLIC_DISTRIBUTIONS
        )

    @property
    def excluded_ids(self) -> tuple[str, ...]:
        return tuple(
            package_id
            for package_id, channel in self.package_channels
            if channel not in PUBLIC_DISTRIBUTIONS
        )

    def channel_for(self, package_id: str) -> str | None:
        return dict(self.package_channels).get(package_id)


def load_addon_publication_policy(root: str | Path) -> AddonPublicationPolicy:
    root_path = Path(root).resolve()
    ledger_path = root_path / LEDGER_RELATIVE_PATH
    try:
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AddonPublicationPolicyError(
            f"Could not read the add-on publication ledger: {ledger_path}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise AddonPublicationPolicyError(
            "The add-on publication ledger must use schema_version 1."
        )
    packages = payload.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise AddonPublicationPolicyError(
            "The add-on publication ledger must contain a non-empty packages map."
        )

    channels: list[tuple[str, str]] = []
    for raw_package_id, record in packages.items():
        package_id = str(raw_package_id or "").strip()
        if PACKAGE_ID_PATTERN.fullmatch(package_id) is None:
            raise AddonPublicationPolicyError(
                "The publication ledger contains an invalid package ID: "
                f"{package_id!r}."
            )
        if not isinstance(record, dict):
            raise AddonPublicationPolicyError(
                f"The publication ledger record for {package_id!r} must be an object."
            )
        channel = str(record.get("distribution") or "").strip()
        if channel not in KNOWN_DISTRIBUTIONS:
            raise AddonPublicationPolicyError(
                f"The publication ledger has an invalid distribution for "
                f"{package_id!r}: {channel!r}."
            )
        channels.append((package_id, channel))
    return AddonPublicationPolicy(tuple(sorted(channels)))


def validate_addon_publication_tree(
    root: str | Path,
    policy: AddonPublicationPolicy,
) -> None:
    """Require the exact reviewed package set and schema-2 channel agreement."""

    root_path = Path(root).resolve()
    addons_root = (root_path / ADDONS_RELATIVE_PATH).resolve()
    if not addons_root.is_relative_to(root_path) or not addons_root.is_dir():
        raise AddonPublicationPolicyError(
            f"The add-on source directory is missing: {addons_root}"
        )
    discovered = {
        item.name
        for item in addons_root.iterdir()
        if item.is_dir() and (item / "manifest.json").is_file()
    }
    expected = set(policy.package_ids)
    missing = sorted(expected - discovered)
    unreviewed = sorted(discovered - expected)
    if missing or unreviewed:
        details: list[str] = []
        if missing:
            details.append("missing from source: " + ", ".join(missing))
        if unreviewed:
            details.append("missing from ledger: " + ", ".join(unreviewed))
        raise AddonPublicationPolicyError(
            "The add-on publication ledger is not exhaustive ("
            + "; ".join(details)
            + ")."
        )

    for package_id in sorted(discovered):
        manifest_path = addons_root / package_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AddonPublicationPolicyError(
                f"Could not read add-on manifest: {manifest_path}"
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("id") != package_id:
            raise AddonPublicationPolicyError(
                f"Add-on directory {package_id!r} has a mismatched manifest ID."
            )
        if manifest.get("manifest_schema_version") != 2:
            continue
        distribution = manifest.get("distribution")
        manifest_channel = (
            str(distribution.get("channel") or "").strip()
            if isinstance(distribution, dict)
            else ""
        )
        expected_channel = policy.channel_for(package_id)
        if manifest_channel != expected_channel:
            raise AddonPublicationPolicyError(
                f"Schema-2 add-on {package_id!r} declares distribution "
                f"{manifest_channel!r}, but the publication ledger records "
                f"{expected_channel!r}."
            )


def non_public_addon_member(
    parts: tuple[str, ...],
    excluded_ids: set[str] | frozenset[str],
) -> str | None:
    """Return a non-public add-on ID when artifact parts contain one."""

    excluded_by_casefold = {
        package_id.casefold(): package_id for package_id in excluded_ids
    }
    if (
        len(parts) >= 3
        and parts[0].casefold() == "iarf_assistant"
        and parts[1].casefold() == "addons"
    ):
        return excluded_by_casefold.get(parts[2].casefold())
    return None


__all__ = [
    "ADDONS_RELATIVE_PATH",
    "AddonPublicationPolicy",
    "AddonPublicationPolicyError",
    "load_addon_publication_policy",
    "non_public_addon_member",
    "validate_addon_publication_tree",
]
