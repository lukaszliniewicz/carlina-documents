#!/usr/bin/env python3
"""Aggregate managed packs into a signed Carlina release-channel index."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from packaging.version import Version

from iarf_assistant.core.doctor import migration_heads
from iarf_assistant.core.release_manifest import (
    RELEASE_INDEX_FORMAT,
    RELEASE_INDEX_VERSION,
    RELEASE_TRUST_FORMAT,
    RELEASE_TRUST_VERSION,
    SUPPORTED_RELEASE_INDEX_VERSIONS,
    load_private_signing_key,
    parse_release_index,
    sign_release_index,
    trusted_key_record,
)


class ReleaseIndexBuildError(RuntimeError):
    pass


RELEASE_EXECUTION_INPUT_FORMAT = "carlina.release-execution-contract-input"
RELEASE_EXECUTION_INPUT_VERSION = 1
SUPPORTED_APPLICATION_PROFILES = frozenset({"workbench", "documents"})
_RELEASE_EXECUTION_INPUT_KEYS = frozenset(
    {
        "format",
        "schema_version",
        "release_version",
        "platforms",
        "execution",
    }
)


@dataclass(frozen=True, slots=True)
class VersionedExecutionContractInput:
    release_version: str
    platforms: tuple[str, ...]
    execution: Mapping[str, Any]


def _canonical_version(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseIndexBuildError(f"{label} must be a canonical PEP 440 version.")
    try:
        normalized = str(Version(value))
    except ValueError as exc:
        raise ReleaseIndexBuildError(
            f"{label} must be a canonical PEP 440 version."
        ) from exc
    if normalized != value:
        raise ReleaseIndexBuildError(
            f"{label} must use canonical PEP 440 spelling {normalized!r}."
        )
    return normalized


def _application_profile(value: object, label: str) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_APPLICATION_PROFILES:
        raise ReleaseIndexBuildError(f"{label} must be one of: documents, workbench.")
    return value


def parse_versioned_execution_contract_input(
    payload: object,
) -> VersionedExecutionContractInput:
    if (
        not isinstance(payload, dict)
        or frozenset(payload) != _RELEASE_EXECUTION_INPUT_KEYS
    ):
        raise ReleaseIndexBuildError(
            "Execution-contract input does not match the closed schema."
        )
    if payload.get("format") != RELEASE_EXECUTION_INPUT_FORMAT:
        raise ReleaseIndexBuildError("Execution-contract input format is unsupported.")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != RELEASE_EXECUTION_INPUT_VERSION
    ):
        raise ReleaseIndexBuildError(
            "Execution-contract input schema version is unsupported."
        )
    release_version = _canonical_version(
        payload.get("release_version"),
        "Execution-contract release version",
    )
    platforms = payload.get("platforms")
    if (
        not isinstance(platforms, list)
        or not platforms
        or not all(isinstance(platform, str) and platform for platform in platforms)
        or platforms != sorted(set(platforms))
    ):
        raise ReleaseIndexBuildError(
            "Execution-contract platforms must be a non-empty sorted unique list."
        )
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        raise ReleaseIndexBuildError(
            "Execution-contract input must contain one execution object."
        )
    if execution.get("data_impact") == "none" and platforms != ["linux-64"]:
        raise ReleaseIndexBuildError(
            "No-data publication is currently certified only for linux-64."
        )
    return VersionedExecutionContractInput(
        release_version=release_version,
        platforms=tuple(platforms),
        execution=json.loads(json.dumps(execution)),
    )


def _public_https_url(value: str, label: str) -> str:
    try:
        parsed = urlsplit(value)
        _validated_port = parsed.port
    except ValueError as exc:
        raise ReleaseIndexBuildError(f"{label} is not a valid URL.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseIndexBuildError(
            f"{label} must be a public HTTPS URL without credentials, a query, or a fragment."
        )
    return value


def _literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            return ast.literal_eval(value)
    return None


def migration_contract(migrations_dir: Path) -> dict[str, list[str]]:
    versions = migrations_dir / "versions"
    revisions: list[str] = []
    for path in sorted(versions.glob("*.py")):
        revision = _literal_assignment(path, "revision")
        if isinstance(revision, str):
            revisions.append(revision)
    heads = sorted(migration_heads(migrations_dir))
    if not revisions or not heads:
        raise ReleaseIndexBuildError(
            "Packaged migration history has no revisions or heads."
        )
    if not set(heads).issubset(revisions):
        raise ReleaseIndexBuildError(
            "Migration heads are absent from the revision set."
        )
    return {"known_revisions": sorted(set(revisions)), "heads": heads}


def _read_artifact_metadata(
    paths: list[Path],
) -> tuple[str, list[dict[str, Any]], str]:
    version: str | None = None
    artifacts: list[dict[str, Any]] = []
    platforms: set[str] = set()
    application_profiles: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseIndexBuildError(
                f"Artifact metadata is unreadable: {path}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("format") != "carlina.managed-artifact"
            or payload.get("schema_version") != 1
        ):
            raise ReleaseIndexBuildError(f"Unsupported artifact metadata: {path}")
        application_profiles.add(
            _application_profile(
                payload.get("application_profile", "workbench"),
                f"Application profile in {path.name}",
            )
        )
        candidate_version = _canonical_version(
            payload.get("version"),
            f"Artifact version in {path.name}",
        )
        if version is None:
            version = candidate_version
        elif version != candidate_version:
            raise ReleaseIndexBuildError("Managed artifacts have different versions.")
        platform = str(payload.get("platform") or "")
        if platform in platforms:
            raise ReleaseIndexBuildError(f"Duplicate managed platform: {platform}")
        platforms.add(platform)
        artifacts.append(
            {
                "platform": platform,
                "kind": str(payload.get("kind") or ""),
                "filename": str(payload.get("filename") or ""),
                "sha256": str(payload.get("sha256") or ""),
                "size": int(payload.get("size") or 0),
            }
        )
    if not version or not artifacts:
        raise ReleaseIndexBuildError("No managed artifact metadata was supplied.")
    if len(application_profiles) != 1:
        raise ReleaseIndexBuildError(
            "Managed artifacts have mixed application profiles: "
            + ", ".join(sorted(application_profiles))
            + "."
        )
    return version, artifacts, next(iter(application_profiles))


def _timestamp(explicit: str | None = None) -> str:
    if explicit:
        value = datetime.fromisoformat(explicit.replace("Z", "+00:00"))
    elif os.environ.get("SOURCE_DATE_EPOCH"):
        value = datetime.fromtimestamp(int(os.environ["SOURCE_DATE_EPOCH"]), UTC)
    else:
        value = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_release_index(
    metadata_paths: list[str | os.PathLike[str]],
    *,
    channel: str,
    base_url: str,
    rollback: str,
    addon_api_version: str,
    published_at: str | None = None,
    notes_url: str | None = None,
    signing_key: Any | None = None,
    schema_version: int = RELEASE_INDEX_VERSION,
    execution_contract: Mapping[str, Any] | None = None,
    expected_version: str | None = None,
    application_profile: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    paths = [Path(path).expanduser().resolve() for path in metadata_paths]
    version, artifacts, metadata_profile = _read_artifact_metadata(paths)
    selected_profile: str | None
    if application_profile is not None:
        selected_profile = _application_profile(
            application_profile,
            "Requested application profile",
        )
        if selected_profile != metadata_profile:
            raise ReleaseIndexBuildError(
                "Requested application profile does not match managed artifacts."
            )
    else:
        selected_profile = "documents" if metadata_profile == "documents" else None
    if expected_version is not None:
        normalized_expected = _canonical_version(
            expected_version,
            "Expected release version",
        )
        if version != normalized_expected:
            raise ReleaseIndexBuildError(
                f"Managed artifact version {version!r} does not match expected "
                f"release version {normalized_expected!r}."
            )
    if channel == "stable" and Version(version).is_prerelease:
        raise ReleaseIndexBuildError(
            "Stable release indexes cannot contain prereleases."
        )
    normalized_base = (
        _public_https_url(base_url, "Release base URL").rstrip("/") + "/"
        if base_url
        else ""
    )
    if notes_url:
        notes_url = _public_https_url(notes_url, "Release notes URL")
    for artifact in artifacts:
        artifact["url"] = (
            urljoin(normalized_base, artifact["filename"])
            if normalized_base
            else artifact["filename"]
        )
    database = migration_contract(PROJECT_ROOT / "iarf_assistant" / "migrations")
    timestamp = _timestamp(published_at)
    versioned_execution: VersionedExecutionContractInput | None = None
    if schema_version == RELEASE_INDEX_VERSION:
        if execution_contract is not None:
            raise ReleaseIndexBuildError(
                "Release-index v1 cannot carry an execution contract."
            )
    elif schema_version in SUPPORTED_RELEASE_INDEX_VERSIONS and schema_version >= 2:
        if execution_contract is None:
            raise ReleaseIndexBuildError(
                f"Release-index v{schema_version} requires an explicit execution contract."
            )
        versioned_execution = parse_versioned_execution_contract_input(
            execution_contract
        )
        if versioned_execution.release_version != version:
            raise ReleaseIndexBuildError(
                "Execution-contract release version does not match managed artifacts."
            )
        available_platforms = {str(artifact["platform"]) for artifact in artifacts}
        missing_platforms = sorted(
            set(versioned_execution.platforms) - available_platforms
        )
        if missing_platforms:
            raise ReleaseIndexBuildError(
                "Execution-contract platforms have no managed artifact: "
                + ", ".join(missing_platforms)
            )
        artifacts = [
            artifact
            for artifact in artifacts
            if artifact["platform"] in versioned_execution.platforms
        ]
        rollback_mode = versioned_execution.execution.get("rollback")
        rollback_name = (
            rollback_mode.get("mode") if isinstance(rollback_mode, dict) else None
        )
        try:
            rollback = {
                "pointer_only": "compatible",
                "restore_required": "restore_required",
            }[rollback_name]
        except (KeyError, TypeError) as exc:
            raise ReleaseIndexBuildError(
                "Execution-contract rollback mode is invalid."
            ) from exc
    else:
        raise ReleaseIndexBuildError(
            f"Unsupported release-index schema version: {schema_version}."
        )
    release_payload: dict[str, Any] = {
        "version": version,
        "published_at": timestamp,
        "addon_api_version": addon_api_version,
        "database": {**database, "rollback": rollback},
        "artifacts": sorted(artifacts, key=lambda item: item["platform"]),
        "notes_url": notes_url,
    }
    if versioned_execution is not None:
        # JSON round-trip prevents a caller-owned mapping from being retained or
        # mutated after validation/signing.
        release_payload["execution"] = json.loads(
            json.dumps(versioned_execution.execution)
        )
    payload = {
        "format": RELEASE_INDEX_FORMAT,
        "schema_version": schema_version,
        "channel": channel,
        "generated_at": timestamp,
        "releases": [release_payload],
        "signatures": [],
    }
    if selected_profile is not None:
        payload["application_profile"] = selected_profile
    index = parse_release_index(payload)
    trust_store = None
    if signing_key is not None:
        private_key = load_private_signing_key(signing_key)
        index = sign_release_index(index, private_key)
        trust_store = {
            "format": RELEASE_TRUST_FORMAT,
            "schema_version": RELEASE_TRUST_VERSION,
            "keys": [trusted_key_record(private_key)],
        }
    return index.to_dict(), trust_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and optionally sign a Carlina release-channel index."
    )
    parser.add_argument("metadata", nargs="+")
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument(
        "--rollback",
        choices=("compatible", "restore_required"),
        default="restore_required",
    )
    parser.add_argument("--addon-api-version", default="1.0")
    parser.add_argument("--published-at")
    parser.add_argument("--notes-url")
    parser.add_argument(
        "--application-profile",
        choices=sorted(SUPPORTED_APPLICATION_PROFILES),
        default=None,
        help="Bind the index to a product profile; otherwise infer Documents metadata.",
    )
    parser.add_argument(
        "--expected-version",
        help="Require artifact metadata to match this canonical release version.",
    )
    parser.add_argument(
        "--schema-version",
        type=int,
        choices=sorted(SUPPORTED_RELEASE_INDEX_VERSIONS),
        default=RELEASE_INDEX_VERSION,
        help="Emit legacy v1 by default or an explicit impact-bearing v2/v3 index.",
    )
    parser.add_argument(
        "--execution-contract",
        metavar="PATH",
        help="Closed version-bound execution-contract input required for schema v2/v3.",
    )
    parser.add_argument("--signing-key")
    parser.add_argument("--signing-key-env", default="CARLINA_RELEASE_SIGNING_KEY_B64")
    parser.add_argument("--unsigned", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--trust-store-output")
    args = parser.parse_args(argv)
    key_value: str | None = args.signing_key
    if not key_value and args.signing_key_env:
        key_value = os.environ.get(args.signing_key_env)
    if args.unsigned:
        key_value = None
    elif not key_value:
        print(
            "Release index signing key is required (or pass --unsigned for local tests).",
            file=sys.stderr,
        )
        return 2
    try:
        execution_contract = None
        if args.execution_contract:
            execution_path = Path(args.execution_contract).expanduser().resolve()
            execution_contract = json.loads(execution_path.read_text(encoding="utf-8"))
            if not isinstance(execution_contract, dict):
                raise ReleaseIndexBuildError(
                    "Execution contract must contain one JSON object."
                )
        index, trust = build_release_index(
            args.metadata,
            channel=args.channel,
            base_url=args.base_url,
            rollback=args.rollback,
            addon_api_version=args.addon_api_version,
            published_at=args.published_at,
            notes_url=args.notes_url,
            signing_key=key_value,
            schema_version=args.schema_version,
            execution_contract=execution_contract,
            expected_version=args.expected_version,
            application_profile=args.application_profile,
        )
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if args.trust_store_output:
            if trust is None:
                raise ReleaseIndexBuildError(
                    "A trust store cannot be emitted for an unsigned index."
                )
            trust_output = Path(args.trust_store_output).expanduser().resolve()
            trust_output.parent.mkdir(parents=True, exist_ok=True)
            trust_output.write_text(
                json.dumps(trust, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError, ReleaseIndexBuildError) as exc:
        print(f"Release index build failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
