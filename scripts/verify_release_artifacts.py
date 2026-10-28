#!/usr/bin/env python3
"""Inspect Carlina wheel/sdist contents against release allow/deny budgets."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

if __package__:
    from .addon_publication_policy import (
        AddonPublicationPolicyError,
        load_addon_publication_policy,
        non_public_addon_member,
    )
else:
    from addon_publication_policy import (
        AddonPublicationPolicyError,
        load_addon_publication_policy,
        non_public_addon_member,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DENIED_SEGMENTS = {
    ".bin",
    ".git",
    ".pixi",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "env",
    "instance",
    "logs",
    "node_modules",
    "temp",
    "uploads",
}
DENIED_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".key",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
}
WHEEL_SOURCE_SUFFIXES = {".svelte", ".ts", ".tsx"}
REQUIRED_CORE_ARTIFACT_MEMBERS = (
    "iarf_assistant/static/css/public.css",
    "iarf_assistant/static/js/csrf.js",
    "iarf_assistant/templates/public_layout.html",
)
CORE_FRONTEND_MANIFEST_MEMBER = "iarf_assistant/static/frontend/manifest.json"
# Publishing-v2 adds four modules, two migrations, two UI components and five tests.
MAX_WHEEL_FILES = 1_264
MAX_SDIST_FILES = 2_248
MAX_FILES = {"wheel": MAX_WHEEL_FILES, "sdist": MAX_SDIST_FILES}
# dev22's JPEG encoder, selector and contracts measure 26,409,040 / 43,543,823
# bytes. Keep member/expanded-size limits and a narrow measured byte envelope.
MAX_ARCHIVE_BYTES = {
    "wheel": 26_410_000,
    "sdist": 43_550_000,
}
MAX_EXPANDED_BYTES = {
    "wheel": 80 * 1024 * 1024,
    "sdist": 180 * 1024 * 1024,
}


@dataclass(frozen=True)
class ArtifactMember:
    name: str
    size: int
    digest: str
    link: bool = False
    payload: bytes | None = None


@dataclass(frozen=True)
class ArtifactReport:
    path: Path
    kind: str
    file_count: int
    expanded_bytes: int
    content_digest: str
    errors: tuple[str, ...]


def _artifact_kind(path: Path) -> str:
    lower_name = path.name.lower()
    if lower_name.endswith(".whl"):
        return "wheel"
    if lower_name.endswith((".tar.gz", ".tgz", ".zip")):
        return "sdist"
    raise ValueError(f"Unsupported artifact type: {path.name}")


def _zip_members(path: Path) -> list[ArtifactMember]:
    with zipfile.ZipFile(path) as archive:
        members = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            payload = archive.read(info)
            members.append(
                ArtifactMember(
                    info.filename,
                    info.file_size,
                    hashlib.sha256(payload).hexdigest(),
                    payload=(
                        payload
                        if info.filename.casefold().endswith("/manifest.json")
                        else None
                    ),
                )
            )
        return members


def _tar_members(path: Path) -> list[ArtifactMember]:
    with tarfile.open(path, mode="r:*") as archive:
        members: list[ArtifactMember] = []
        for member in archive.getmembers():
            if not (member.isfile() or member.issym() or member.islnk()):
                continue
            content = b""
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is not None:
                    content = extracted.read()
            members.append(
                ArtifactMember(
                    member.name,
                    member.size if member.isfile() else 0,
                    hashlib.sha256(content).hexdigest(),
                    link=member.issym() or member.islnk(),
                    payload=(
                        content
                        if member.name.casefold().endswith("/manifest.json")
                        else None
                    ),
                )
            )
        return members


def _normalized_parts(name: str, *, kind: str) -> tuple[str, ...] | None:
    if not name or "\x00" in name or "\\" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    parts = path.parts
    if kind == "sdist" and len(parts) > 1:
        parts = parts[1:]
    return tuple(parts)


def _content_digest(members: list[ArtifactMember]) -> str:
    digest = hashlib.sha256()
    for member in sorted(members, key=lambda value: value.name):
        digest.update(member.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(member.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(member.digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _is_generated_addon_dependency_tree(parts: tuple[str, ...]) -> bool:
    """Identify generated add-on dependencies without rejecting source lib."""

    lowered = tuple(part.casefold() for part in parts)
    try:
        addons_index = lowered.index("addons")
    except ValueError:
        return False
    dependency_index = addons_index + 2
    return (
        addons_index > 0
        and lowered[addons_index - 1] == "iarf_assistant"
        and len(lowered) > dependency_index
        and lowered[dependency_index] in {"env", "lib"}
    )


def _declared_addon_output_errors(
    members: dict[str, ArtifactMember],
) -> list[str]:
    """Require every compiled add-on UI output declared by its manifest."""

    available = {name.casefold(): name for name in members}
    errors: list[str] = []
    for name, member in sorted(members.items()):
        parts = PurePosixPath(name).parts
        if (
            len(parts) != 4
            or parts[0].casefold() != "iarf_assistant"
            or parts[1].casefold() != "addons"
            or parts[3].casefold() != "manifest.json"
        ):
            continue
        expected_manifest_name = str(
            PurePosixPath("iarf_assistant", "addons", parts[2], "manifest.json")
        )
        if name != expected_manifest_name:
            errors.append(
                "add-on frontend manifest is missing or has wrong case: "
                + expected_manifest_name
            )
            continue
        if member.payload is None:
            errors.append(f"add-on manifest payload is unavailable: {name}")
            continue
        try:
            manifest = json.loads(member.payload.decode("utf-8-sig"))
        except UnicodeDecodeError, json.JSONDecodeError:
            errors.append(f"add-on manifest is not valid UTF-8 JSON: {name}")
            continue
        frontend = manifest.get("frontend") if isinstance(manifest, dict) else None
        build = frontend.get("build") if isinstance(frontend, dict) else None
        if not isinstance(build, dict):
            continue
        outputs = build.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            errors.append(f"compiled add-on frontend declares no outputs: {name}")
            continue
        addon_root = PurePosixPath(*parts[:3])
        for raw_output in outputs:
            value = str(raw_output or "").strip().replace("\\", "/")
            relative = PurePosixPath(value)
            if (
                not value
                or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                errors.append(
                    f"add-on manifest declares an unsafe frontend output: {name}"
                )
                continue
            expected = str(addon_root / "static" / relative)
            actual = available.get(expected.casefold())
            if actual != expected:
                errors.append(
                    "declared add-on frontend output is missing or has wrong case: "
                    + expected
                )
    return errors


def _declared_core_frontend_output_errors(
    members: dict[str, ArtifactMember],
) -> list[str]:
    """Require every JS/CSS asset named by Vite's packaged core manifest."""

    manifest_name = CORE_FRONTEND_MANIFEST_MEMBER
    manifest_member = members.get(manifest_name)
    if manifest_member is None:
        actual = next(
            (name for name in members if name.casefold() == manifest_name.casefold()),
            None,
        )
        if actual is not None:
            return [
                "core frontend manifest is missing or has wrong case: " + manifest_name
            ]
        return ["required core frontend manifest is missing: " + manifest_name]
    if manifest_member.payload is None:
        return ["core frontend manifest payload is unavailable"]
    try:
        manifest = json.loads(manifest_member.payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["core frontend manifest is not valid UTF-8 JSON"]
    if not isinstance(manifest, dict):
        return ["core frontend manifest must be an object"]

    available = {name.casefold(): name for name in members}
    errors: list[str] = []
    for record_name, record in sorted(manifest.items()):
        if not isinstance(record, dict):
            errors.append(f"core frontend manifest record is invalid: {record_name}")
            continue
        for field in ("file", "css", "assets"):
            values = [record.get(field)] if field == "file" else record.get(field, [])
            if not isinstance(values, list):
                errors.append(
                    f"core frontend manifest {field} field is invalid: {record_name}"
                )
                continue
            for raw_value in values:
                value = str(raw_value or "").strip().replace("\\", "/")
                relative = PurePosixPath(value)
                if (
                    not value
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    errors.append(
                        f"core frontend manifest declares an unsafe asset: {record_name}"
                    )
                    continue
                expected = str(PurePosixPath("iarf_assistant/static/frontend") / relative)
                actual = available.get(expected.casefold())
                if actual != expected:
                    errors.append(
                        "declared core frontend output is missing or has wrong case: "
                        + expected
                    )
    return errors


def _declare_addon_dependency_artifact(
    raw_value: object,
    *,
    field: str,
    manifest_name: str,
    addon_root: PurePosixPath,
    available: set[str],
    declared: set[str],
    errors: list[str],
) -> None:
    value = str(raw_value or "").strip().replace("\\", "/")
    if not value:
        return
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (relative.parts and ":" in relative.parts[0])
    ):
        errors.append(
            f"add-on manifest declares an unsafe dependency {field}: {manifest_name}"
        )
        return
    expected = str(addon_root / relative)
    declared.add(expected.casefold())
    if expected.casefold() not in available:
        errors.append(f"declared add-on dependency artifact is missing: {expected}")


def _declared_addon_dependency_errors(
    members: dict[str, ArtifactMember],
) -> list[str]:
    """Reject missing or stale add-on dependency inputs in release archives."""

    available = {name.casefold() for name in members}
    errors: list[str] = []
    for name, member in sorted(members.items()):
        parts = PurePosixPath(name).parts
        if (
            len(parts) != 4
            or parts[0].casefold() != "iarf_assistant"
            or parts[1].casefold() != "addons"
            or parts[3].casefold() != "manifest.json"
        ):
            continue
        if member.payload is None:
            continue
        try:
            manifest = json.loads(member.payload.decode("utf-8-sig"))
        except UnicodeDecodeError, json.JSONDecodeError:
            continue
        if not isinstance(manifest, dict):
            continue

        addon_root = PurePosixPath(*parts[:3])
        declared: set[str] = set()

        dependencies = manifest.get("dependencies") or {}
        python_dependencies = (
            dependencies.get("python") if isinstance(dependencies, dict) else None
        )
        if isinstance(python_dependencies, dict):
            raw_environments = python_dependencies.get("environments")
            if raw_environments is None:
                raw_environments = [python_dependencies] if python_dependencies else []
            if isinstance(raw_environments, list):
                for environment in raw_environments:
                    if not isinstance(environment, dict):
                        continue
                    for field in ("lockfile", "manifest"):
                        _declare_addon_dependency_artifact(
                            environment.get(field),
                            field=field,
                            manifest_name=name,
                            addon_root=addon_root,
                            available=available,
                            declared=declared,
                            errors=errors,
                        )

        runtime = manifest.get("runtime") or {}
        raw_units = runtime.get("units") if isinstance(runtime, dict) else None
        if isinstance(raw_units, list):
            for unit in raw_units:
                if not isinstance(unit, dict):
                    continue
                raw_artifacts = unit.get("dependency_artifacts") or []
                if not isinstance(raw_artifacts, list):
                    continue
                for artifact in raw_artifacts:
                    if not isinstance(artifact, dict):
                        continue
                    _declare_addon_dependency_artifact(
                        artifact.get("path"),
                        field="runtime dependency artifact",
                        manifest_name=name,
                        addon_root=addon_root,
                        available=available,
                        declared=declared,
                        errors=errors,
                    )

        actual = {
            candidate.casefold()
            for candidate in members
            if PurePosixPath(candidate).is_relative_to(addon_root)
            and PurePosixPath(candidate) != addon_root
            and (
                PurePosixPath(candidate).name.casefold() == "pixi.toml"
                or (
                    PurePosixPath(candidate).suffix.casefold() == ".lock"
                    and PurePosixPath(candidate).name != ".lock"
                )
            )
        }
        for stale in sorted(actual - declared):
            errors.append(f"undeclared add-on dependency artifact: {stale}")
    return errors


def inspect_artifact(
    path: Path, *, application_profile: str = "workbench"
) -> ArtifactReport:
    if application_profile not in {"workbench", "documents"}:
        raise ValueError("Unsupported application profile.")
    path = path.resolve()
    kind = _artifact_kind(path)
    if not path.is_file():
        raise ValueError(f"Artifact does not exist: {path}")
    members = _zip_members(path) if zipfile.is_zipfile(path) else _tar_members(path)
    errors: list[str] = []
    expanded_bytes = sum(max(0, member.size) for member in members)
    file_budget = MAX_FILES[kind]
    archive_budget = MAX_ARCHIVE_BYTES[kind]
    if application_profile == "documents" and kind == "wheel":
        try:
            policy = json.loads(
                (PROJECT_ROOT / "policy/documents-closure.json").read_text(encoding="utf-8")
            )
            wheel_budget = policy["wheel"]
            file_budget = wheel_budget["file_count"]
            archive_budget = wheel_budget["file_bytes"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("Documents wheel budgets are unavailable or invalid.") from exc
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (file_budget, archive_budget)
        ):
            raise ValueError("Documents wheel budgets must be positive integers.")
    try:
        excluded_addon_ids = frozenset(
            load_addon_publication_policy(PROJECT_ROOT).excluded_ids
        )
    except AddonPublicationPolicyError as exc:
        excluded_addon_ids = frozenset()
        errors.append(f"add-on publication policy is invalid: {exc}")

    if len(members) > file_budget:
        errors.append(f"contains {len(members)} files; budget is {file_budget}")
    if path.stat().st_size > archive_budget:
        errors.append(
            f"archive size is {path.stat().st_size} bytes; budget is "
            f"{archive_budget}"
        )
    if expanded_bytes > MAX_EXPANDED_BYTES[kind]:
        errors.append(
            f"expanded size is {expanded_bytes} bytes; budget is "
            f"{MAX_EXPANDED_BYTES[kind]}"
        )

    normalized_names: list[str] = []
    normalized_members: dict[str, ArtifactMember] = {}
    for member in members:
        parts = _normalized_parts(member.name, kind=kind)
        if parts is None or not parts:
            errors.append(f"unsafe member path: {member.name!r}")
            continue
        normalized_name = "/".join(parts)
        normalized_names.append(normalized_name)
        normalized_members[normalized_name] = member
        excluded_addon_id = non_public_addon_member(parts, excluded_addon_ids)
        if excluded_addon_id is not None:
            errors.append(
                f"non-public add-on {excluded_addon_id!r} leaked into release "
                f"artifact: {member.name}"
            )
        lowered_parts = {part.casefold() for part in parts}
        denied = sorted(lowered_parts.intersection(DENIED_SEGMENTS))
        if denied:
            errors.append(f"denied path segment {denied[0]!r}: {member.name}")
        if _is_generated_addon_dependency_tree(parts):
            errors.append(f"generated add-on dependency tree: {member.name}")
        leaf = parts[-1].casefold()
        is_root_example = kind == "sdist" and parts == (".env.example",)
        if not is_root_example and (leaf == ".env" or leaf.startswith(".env.")):
            errors.append(f"environment file is not distributable: {member.name}")
        if any(leaf.endswith(suffix) for suffix in DENIED_SUFFIXES):
            errors.append(f"runtime/secret file is not distributable: {member.name}")
        if member.link:
            errors.append(f"links are not permitted in release archives: {member.name}")
        if kind == "wheel" and PurePosixPath(leaf).suffix in WHEEL_SOURCE_SUFFIXES:
            errors.append(f"frontend source leaked into wheel: {member.name}")

    lowered_names = [name.casefold() for name in normalized_names]
    if kind == "wheel":
        required_fragments = (
            ".dist-info/metadata",
            ".dist-info/licenses/license",
        )
    else:
        required_fragments = (
            "license",
            "readme.md",
            "third_party_notices.md",
            "pyproject.toml",
        )
    for required in required_fragments:
        if not any(name.endswith(required) for name in lowered_names):
            errors.append(f"required artifact member is missing: *{required}")
    available_members = set(normalized_names)
    for required in REQUIRED_CORE_ARTIFACT_MEMBERS:
        if required not in available_members:
            errors.append(f"required core artifact member is missing: {required}")
    errors.extend(_declared_addon_output_errors(normalized_members))
    errors.extend(_declared_core_frontend_output_errors(normalized_members))
    errors.extend(_declared_addon_dependency_errors(normalized_members))

    return ArtifactReport(
        path=path,
        kind=kind,
        file_count=len(members),
        expanded_bytes=expanded_bytes,
        content_digest=_content_digest(members),
        errors=tuple(dict.fromkeys(errors)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Carlina release artifact contents and budgets."
    )
    parser.add_argument(
        "artifacts",
        nargs="*",
        type=Path,
        help="Wheel/sdist paths (defaults to dist/*).",
    )
    parser.add_argument(
        "--application-profile",
        choices=("workbench", "documents"),
        default="workbench",
        help="Use the selected application's checked wheel budgets.",
    )
    args = parser.parse_args(argv)
    artifacts = args.artifacts or sorted((PROJECT_ROOT / "dist").glob("*"))
    if not artifacts:
        print("No release artifacts were found.", file=sys.stderr)
        return 1

    failed = False
    kinds: set[str] = set()
    for artifact in artifacts:
        try:
            report = inspect_artifact(artifact, application_profile=args.application_profile)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            failed = True
            print(f"FAIL {artifact}: {exc}", file=sys.stderr)
            continue
        kinds.add(report.kind)
        status = "FAIL" if report.errors else "PASS"
        print(
            f"{status} {report.path.name}: {report.file_count} files, "
            f"{report.expanded_bytes} expanded bytes, "
            f"manifest sha256 {report.content_digest}"
        )
        for error in report.errors:
            failed = True
            print(f"  - {error}", file=sys.stderr)
    missing_kinds = {"wheel", "sdist"} - kinds
    if missing_kinds:
        failed = True
        print(
            "Missing required artifact type(s): " + ", ".join(sorted(missing_kinds)),
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
