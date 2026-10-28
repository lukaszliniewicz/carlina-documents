from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from iarf_assistant.core.addons.contracts import (
    AddonSourceKind,
    DependencyEnvironment,
    RuntimeMode,
    validate_addon_contract,
)

ADDON_ONLY_ROOT_DENYLIST = frozenset(
    canonicalize_name(name)
    for name in (
        "atproto",
        "ddgs",
        "defusedxml",
        "GitPython",
        "google-api-python-client",
        "google-auth-httplib2",
        "google-auth-oauthlib",
        "html-to-markdown",
        "httplib2",
        "imap-tools",
        "Markdown",
        "nltk",
        "patchright",
        "pyasn1",
        "pysbd",
        "rapidfuzz",
        "stripe",
        "SudachiPy",
        "sudachidict_core",
        "tweepy",
        "trafilatura",
    )
)
PYTHON_MINOR_FLOOR = re.compile(r">=\s*([0-9]+\.[0-9]+)")


def _requirements_from_source(path: Path) -> tuple[dict[str, str], list[str]]:
    requirements: dict[str, str] = {}
    errors: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            errors.append(f"{path}:{line_number}: invalid requirement: {exc}")
            continue
        requirements[canonicalize_name(requirement.name)] = str(requirement.specifier)
    return requirements, errors


def _locked_versions(path: Path) -> tuple[dict[str, str], list[str]]:
    """Read exact versions from a hash-locked pip requirements file."""

    logical_lines: list[str] = []
    current: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        current.append(line[:-1].strip() if continued else line)
        if not continued:
            logical_lines.append(" ".join(current))
            current = []
    if current:
        logical_lines.append(" ".join(current))

    versions: dict[str, str] = {}
    errors: list[str] = []
    for logical_line in logical_lines:
        if logical_line.startswith("--"):
            continue
        requirement_text = logical_line.split(" --hash=", 1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            errors.append(f"{path}: invalid locked requirement: {exc}")
            continue
        exact_versions = [
            specifier.version
            for specifier in requirement.specifier
            if specifier.operator == "==" and not specifier.version.endswith(".*")
        ]
        if len(exact_versions) != 1:
            errors.append(
                f"{path}: {requirement.name!r} does not have one exact version pin."
            )
            continue
        versions[canonicalize_name(requirement.name)] = exact_versions[0]
    return versions, errors


def _core_runtime_versions(lock_path: Path) -> dict[str, set[str]]:
    payload = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    packages = payload.get("packages") or []
    packages_by_locator: dict[str, dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        locator = package.get("pypi") or package.get("conda")
        if locator:
            packages_by_locator[str(locator)] = package

    runtime = (payload.get("environments") or {}).get("runtime") or {}
    platform_packages = runtime.get("packages") or {}
    versions: dict[str, set[str]] = defaultdict(set)
    for references in platform_packages.values():
        for reference in references or ():
            if not isinstance(reference, dict) or not reference:
                continue
            locator = str(next(iter(reference.values())))
            package = packages_by_locator.get(locator) or {}
            name = package.get("name")
            version = package.get("version")
            if name and version:
                versions[canonicalize_name(str(name))].add(str(version))
    return dict(versions)


def _host_python_minor(pyproject: dict[str, Any]) -> str:
    requires_python = str(pyproject["project"]["requires-python"])
    match = PYTHON_MINOR_FLOOR.search(requires_python)
    if match is None:
        raise ValueError(
            "project.requires-python must contain an exact supported minor floor."
        )
    return match.group(1)


def _has_upper_bound(requirement: Requirement) -> bool:
    return any(
        specifier.operator in {"<", "<=", "==", "~=", "==="}
        for specifier in requirement.specifier
    )


def _pixi_local_path_dependencies(manifest: dict[str, Any]) -> list[str]:
    """Return local path dependencies that break after cache materialization."""

    local_dependencies: list[str] = []
    for section_name in ("dependencies", "pypi-dependencies"):
        dependencies = manifest.get(section_name) or {}
        if not isinstance(dependencies, dict):
            continue
        for package_name, declaration in dependencies.items():
            if isinstance(declaration, dict) and declaration.get("path") is not None:
                local_dependencies.append(f"{section_name}.{package_name}")
    return sorted(local_dependencies)


def audit_dependency_ownership(
    repository_root: Path,
) -> tuple[list[str], dict[str, int]]:
    repository_root = repository_root.resolve()
    addons_root = repository_root / "iarf_assistant" / "addons"
    pyproject = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    host_python_minor = _host_python_minor(pyproject)
    errors: list[str] = []

    root_requirements = [
        Requirement(item) for item in pyproject["project"]["dependencies"]
    ]
    workbench_requirements = [
        Requirement(item)
        for item in pyproject["project"]["optional-dependencies"].get("workbench", ())
    ]
    errors.extend(
        f"Root runtime requirement {requirement.name!r} needs a tested upper bound."
        for requirement in root_requirements
        if not requirement.specifier or not _has_upper_bound(requirement)
    )
    errors.extend(
        f"Workbench requirement {requirement.name!r} needs a tested upper bound."
        for requirement in workbench_requirements
        if not requirement.specifier or not _has_upper_bound(requirement)
    )
    for raw_requirement in pyproject.get("build-system", {}).get("requires", ()):
        requirement = Requirement(raw_requirement)
        if not requirement.specifier or not _has_upper_bound(requirement):
            errors.append(
                f"Build requirement {requirement.name!r} needs a tested upper bound."
            )

    pixi_manifest = tomllib.loads(
        (repository_root / "pixi.toml").read_text(encoding="utf-8")
    )
    for package_name, raw_constraint in (
        pixi_manifest.get("dependencies") or {}
    ).items():
        constraint = str(raw_constraint or "").strip()
        bounded = constraint != "*" and (
            "<" in constraint or "==" in constraint or constraint.endswith(".*")
        )
        if not bounded:
            errors.append(
                f"Pixi runtime requirement {package_name!r} needs a tested upper bound."
            )
    root_names = {canonicalize_name(item.name) for item in root_requirements}
    workbench_names = {canonicalize_name(item.name) for item in workbench_requirements}
    quality_feature = (pixi_manifest.get("feature") or {}).get("quality") or {}
    quality_dependencies = quality_feature.get("pypi-dependencies") or {}
    if not isinstance(quality_dependencies, dict):
        errors.append("Pixi feature.quality.pypi-dependencies must be a table.")
        quality_dependencies = {}
    quality_names: set[str] = set()
    for package_name, raw_constraint in quality_dependencies.items():
        quality_names.add(canonicalize_name(package_name))
        if not isinstance(raw_constraint, str):
            errors.append(
                f"Pixi quality requirement {package_name!r} must use a version string."
            )
            continue
        constraint = raw_constraint.strip()
        try:
            requirement = Requirement(f"{package_name}{constraint}")
        except InvalidRequirement as exc:
            errors.append(
                f"Pixi quality requirement {package_name!r} is invalid: {exc}"
            )
            continue
        if not constraint or not _has_upper_bound(requirement):
            errors.append(
                f"Pixi quality requirement {package_name!r} needs a tested upper bound."
            )
    test_environment_names = root_names | workbench_names | quality_names
    leaked = sorted((root_names | workbench_names) & ADDON_ONLY_ROOT_DENYLIST)
    if leaked:
        errors.append(
            "Root runtime directly owns add-on-only distributions: " + ", ".join(leaked)
        )

    try:
        core_versions = _core_runtime_versions(repository_root / "pixi.lock")
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"Could not inspect the root runtime lock: {exc}")
        core_versions = {}

    manifest_count = 0
    environment_count = 0
    in_process_count = 0
    managed_count = 0
    hashed_lock_count = 0
    container_dependency_artifact_count = 0
    shared_versions: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    in_process_direct_owners: dict[str, set[str]] = defaultdict(set)

    for manifest_path in sorted(addons_root.glob("*/manifest.json")):
        manifest_count += 1
        addon_id = manifest_path.parent.name
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{addon_id}: could not read manifest: {exc}")
            continue

        python_block = (manifest.get("dependencies") or {}).get("python")
        if python_block is not None and (
            not isinstance(python_block, dict) or "environments" not in python_block
        ):
            errors.append(
                f"{addon_id}: Python dependencies must use the explicit environments list."
            )

        validation = validate_addon_contract(
            manifest,
            addon_dir=manifest_path.parent,
            source_kind=AddonSourceKind.INSTALLED,
            strict=True,
        )
        errors.extend(
            (
                f"{addon_id}: {diagnostic.severity} {diagnostic.code} "
                f"({diagnostic.path}): {diagnostic.message}"
            )
            for diagnostic in validation.diagnostics
        )
        contract = validation.contract
        if contract is None:
            continue

        declared_locks = {
            environment.lock.path
            for environment in contract.python_environments
            if environment.lock is not None
        }
        declared_manifests = {
            environment.manifest_path
            for environment in contract.python_environments
            if environment.manifest_path
        }
        container_artifacts = {
            artifact.path
            for runtime_unit in contract.runtime_units
            if runtime_unit.mode == RuntimeMode.CONTAINER
            for artifact in runtime_unit.dependency_artifacts
        }
        container_dependency_artifact_count += len(container_artifacts)
        dockerfiles = tuple(manifest_path.parent.rglob("Dockerfile"))
        docker_sources = "\n".join(
            path.read_text(encoding="utf-8", errors="replace") for path in dockerfiles
        )
        for artifact_path in sorted(container_artifacts):
            artifact_name = Path(artifact_path).name
            if (
                artifact_path not in docker_sources
                and artifact_name not in docker_sources
            ):
                errors.append(
                    f"{addon_id}: container dependency artifact {artifact_path!r} "
                    "is not consumed by a Dockerfile."
                )

        errors.extend(
            f"{addon_id}: orphan dependency lock {relative_path!r}."
            for artifact in manifest_path.parent.rglob("*.lock")
            if artifact.name != ".lock"
            for relative_path in (
                artifact.relative_to(manifest_path.parent).as_posix(),
            )
            if relative_path not in declared_locks
            and relative_path not in container_artifacts
        )
        for artifact in manifest_path.parent.rglob("pixi.toml"):
            artifact_path = artifact.relative_to(manifest_path.parent).as_posix()
            if artifact_path not in declared_manifests:
                errors.append(
                    f"{addon_id}: orphan managed-environment artifact {artifact_path!r}."
                )
        for source_path in manifest_path.parent.rglob("requirements*.txt"):
            relative_source = source_path.relative_to(manifest_path.parent).as_posix()
            companion_lock = (
                source_path.with_suffix(".lock")
                .relative_to(manifest_path.parent)
                .as_posix()
            )
            if (
                companion_lock not in declared_locks
                and companion_lock not in container_artifacts
                and source_path.name not in docker_sources
                and relative_source not in docker_sources
            ):
                errors.append(
                    f"{addon_id}: requirement source {relative_source!r} is neither locked nor used by a Dockerfile."
                )

        for environment in contract.python_environments:
            environment_count += 1
            environment_label = f"{addon_id}:{environment.id}"
            if environment.environment == DependencyEnvironment.IN_PROCESS_VENDOR:
                in_process_count += 1
                if environment.python_version != host_python_minor:
                    errors.append(
                        f"{environment_label}: in-process Python {environment.python_version!r} "
                        f"does not match host Python {host_python_minor!r}."
                    )
                for raw_requirement in environment.pip_packages:
                    requirement = Requirement(raw_requirement)
                    in_process_direct_owners[canonicalize_name(requirement.name)].add(
                        environment_label
                    )
            else:
                managed_count += 1
                if environment.manifest_path:
                    managed_manifest_path = (
                        manifest_path.parent / environment.manifest_path
                    )
                    try:
                        managed_manifest = tomllib.loads(
                            managed_manifest_path.read_text(encoding="utf-8")
                        )
                    except (OSError, tomllib.TOMLDecodeError) as exc:
                        errors.append(
                            f"{environment_label}: could not inspect managed "
                            f"manifest {environment.manifest_path!r}: {exc}"
                        )
                    else:
                        local_paths = _pixi_local_path_dependencies(managed_manifest)
                        if local_paths:
                            errors.append(
                                f"{environment_label}: managed Pixi manifests are "
                                "copied into a cache and cannot use local path "
                                "dependencies: " + ", ".join(local_paths)
                            )

            if environment.lock is None:
                continue
            lock_path = manifest_path.parent / environment.lock.path
            if environment.lock.format != "pip_requirements":
                continue
            hashed_lock_count += 1
            source_path = lock_path.with_suffix(".txt")
            if not source_path.is_file():
                errors.append(
                    f"{environment_label}: missing requirement source {source_path.name!r}."
                )
                continue
            source_requirements, source_errors = _requirements_from_source(source_path)
            errors.extend(source_errors)
            declared_requirements: dict[str, str] = {}
            for raw_requirement in environment.pip_packages:
                requirement = Requirement(raw_requirement)
                declared_requirements[canonicalize_name(requirement.name)] = str(
                    requirement.specifier
                )
            if source_requirements != declared_requirements:
                errors.append(
                    f"{environment_label}: manifest pip_packages do not match {source_path.name}."
                )

            locked_versions, lock_errors = _locked_versions(lock_path)
            errors.extend(lock_errors)
            if environment.environment != DependencyEnvironment.IN_PROCESS_VENDOR:
                continue
            for package_name, package_version in locked_versions.items():
                shared_versions[package_name][package_version].add(environment_label)
                core_package_versions = core_versions.get(package_name)
                if (
                    core_package_versions
                    and package_version not in core_package_versions
                ):
                    errors.append(
                        f"{environment_label}: {package_name}=={package_version} conflicts "
                        f"with core lock versions {sorted(core_package_versions)}."
                    )

    for package_name, versions in sorted(shared_versions.items()):
        if len(versions) <= 1:
            continue
        ownership = "; ".join(
            f"{version}: {', '.join(sorted(owners))}"
            for version, owners in sorted(versions.items())
        )
        errors.append(
            f"In-process vendor version conflict for {package_name}: {ownership}"
        )

    missing_test_dependencies = sorted(
        set(in_process_direct_owners) - test_environment_names
    )
    if missing_test_dependencies:
        details = "; ".join(
            f"{package_name} ({', '.join(sorted(in_process_direct_owners[package_name]))})"
            for package_name in missing_test_dependencies
        )
        errors.append(
            "Pixi quality environment omits direct in-process add-on dependencies: "
            + details
        )

    summary = {
        "manifests": manifest_count,
        "python_environments": environment_count,
        "in_process_environments": in_process_count,
        "managed_pixi_environments": managed_count,
        "hashed_pip_locks": hashed_lock_count,
        "container_dependency_artifacts": container_dependency_artifact_count,
        "root_direct_dependencies": len(root_requirements),
        "workbench_direct_dependencies": len(workbench_requirements),
        "quality_direct_dependencies": len(quality_names),
        "in_process_direct_dependencies": len(in_process_direct_owners),
        "shared_locked_packages": len(shared_versions),
    }
    return errors, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Carlina core/add-on dependency ownership."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=ROOT_DIR,
        help="Repository root; defaults to the parent of scripts/.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    errors, summary = audit_dependency_ownership(args.repository_root)
    if args.json_output:
        print(json.dumps({"valid": not errors, "summary": summary, "errors": errors}))
    else:
        for error in errors:
            print(error, file=sys.stderr)
        print(
            "Dependency ownership: "
            + ", ".join(f"{key}={value}" for key, value in summary.items())
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
