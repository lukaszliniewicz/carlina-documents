"""Data-root paths for rebuildable add-on dependency environments."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

DEPENDENCY_INPUTS = (
    "manifest.json",
    "requirements.txt",
    "requirements.lock",
    "pixi.toml",
    "pixi.lock",
)
_CONDA_SPEC_PATTERN = re.compile(
    r"^(?:(?P<channel>[a-zA-Z0-9_.-]+)::)?"
    r"(?P<name>[a-zA-Z0-9_.-]+)"
    r"(?P<constraint>.*)$"
)
_PYTHON_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_GENERATION_LABEL_MAX_LENGTH = 24
_GENERATION_DIGEST_LENGTH = 24


@dataclass(frozen=True, slots=True)
class AddonEnvironmentPaths:
    """Immutable locations for one dependency-input generation."""

    root: Path
    lib_dir: Path
    pixi_project_dir: Path
    pixi_manifest: Path


def dependency_fingerprint(addon_dir: str | Path) -> str:
    """Hash every file that can change a legacy built-in environment."""

    root = Path(addon_dir).resolve()
    digest = hashlib.sha256()
    for name in DEPENDENCY_INPUTS:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"builtin-{digest.hexdigest()[:20]}"


def dependency_generation_name(
    environment_id: str,
    dependency_sha256: str,
    contract_sha256: str = "",
) -> str:
    """Return a short, collision-resistant dependency generation name.

    Pixi nests its own project and environment directories below this name.
    Embedding full lock and contract hashes therefore crosses common Windows
    path limits once Conda metadata filenames are appended. The readable label
    is bounded and non-authoritative; the digest covers every complete input.
    """

    raw_environment_id = str(environment_id or "default").strip() or "default"
    readable_label = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_environment_id)
    readable_label = (
        readable_label.strip("._-")[:_GENERATION_LABEL_MAX_LENGTH] or "default"
    )
    payload = json.dumps(
        [raw_environment_id, str(dependency_sha256), str(contract_sha256)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:_GENERATION_DIGEST_LENGTH]
    return f"{readable_label}-{digest}"


def python_environment_fingerprint(environment: object) -> str:
    """Hash only inputs that can change a Python environment's contents."""

    dependency_kind = getattr(environment, "environment", "")
    dependency_lock = getattr(environment, "lock", None)
    setup_actions = tuple(getattr(environment, "setup", ()) or ())
    payload = {
        "id": str(getattr(environment, "id", "default") or "default"),
        "environment": str(
            getattr(dependency_kind, "value", dependency_kind) or ""
        ),
        "python_version": str(
            getattr(environment, "python_version", "") or ""
        ),
        "lock": (
            {
                "format": str(getattr(dependency_lock, "format", "") or ""),
                "sha256": str(getattr(dependency_lock, "sha256", "") or ""),
                "require_hashes": bool(
                    getattr(dependency_lock, "require_hashes", False)
                ),
            }
            if dependency_lock is not None
            else None
        ),
        "setup": [
            {
                "id": str(getattr(action, "id", "") or ""),
                "kind": str(getattr(action, "kind", "") or ""),
                "provider": str(getattr(action, "provider", "") or ""),
                "target": str(getattr(action, "target", "") or ""),
                "runtime_unit": str(
                    getattr(action, "runtime_unit", "") or ""
                ),
            }
            for action in setup_actions
        ],
        "manifest_sha256": str(
            getattr(environment, "manifest_sha256", "") or ""
        ),
        "pip_packages": sorted(
            str(item) for item in (getattr(environment, "pip_packages", ()) or ())
        ),
        "conda_packages": sorted(
            str(item)
            for item in (getattr(environment, "conda_packages", ()) or ())
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_environment_paths(
    environments_root: str | Path,
    addon_id: str,
    addon_dir: str | Path,
    *,
    generation: str | None = None,
) -> AddonEnvironmentPaths:
    """Resolve a collision-safe, hash-addressed environment under DataRoot."""

    root = Path(environments_root).resolve()
    addon_root = (root / str(addon_id)).resolve()
    addon_root.relative_to(root)
    selected_generation = generation or dependency_fingerprint(addon_dir)
    generation_root = (addon_root / selected_generation).resolve()
    generation_root.relative_to(addon_root)
    pixi_project = generation_root / "pixi"
    return AddonEnvironmentPaths(
        root=generation_root,
        lib_dir=generation_root / "lib",
        pixi_project_dir=pixi_project,
        pixi_manifest=pixi_project / "pixi.toml",
    )


def _toml_string(value: str) -> str:
    """Return a JSON/TOML-compatible quoted string."""

    return json.dumps(str(value), ensure_ascii=False)


def _current_pixi_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "win-64"
    if system == "darwin":
        return "osx-arm64" if machine in {"arm64", "aarch64"} else "osx-64"
    if system == "linux":
        return "linux-aarch64" if machine in {"arm64", "aarch64"} else "linux-64"
    raise RuntimeError(f"Pixi managed processes are unsupported on {system!r}.")


def _python_constraint(value: str) -> str:
    selected = str(value or "3.11").strip()
    if _PYTHON_VERSION_PATTERN.fullmatch(selected):
        parts = selected.split(".")
        return selected if len(parts) == 3 else f"{selected}.*"
    if not selected or any(char in selected for char in "\r\n\t\"'"):
        raise ValueError(f"Invalid managed-process Python version: {value!r}.")
    return selected


def _conda_dependency(spec: str) -> tuple[str, str, str | None]:
    raw = str(spec or "").strip()
    match = _CONDA_SPEC_PATTERN.fullmatch(raw)
    if match is None:
        raise ValueError(f"Invalid Pixi/Conda dependency specification: {spec!r}.")
    name = match.group("name")
    constraint = match.group("constraint").strip()
    if constraint.startswith("=") and not constraint.startswith(("==", ">=", "<=")):
        constraint = constraint[1:].strip()
    if any(char in constraint for char in "\r\n\t\"'"):
        raise ValueError(f"Invalid Pixi/Conda dependency constraint: {spec!r}.")
    return name, constraint or "*", match.group("channel")


def render_generated_pixi_manifest(
    addon_id: str,
    *,
    python_version: str = "3.11",
    conda_packages: tuple[str, ...] | list[str] = (),
    pip_packages: tuple[str, ...] | list[str] = (),
    pixi_platform: str | None = None,
    pixi_platforms: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Render a minimal current-platform Pixi project from reviewed inputs.

    Generated projects live below DataRoot and are rebuildable. Direct URL and
    environment-marker PyPI requirements are deliberately rejected: packaged
    add-ons should carry a reviewed ``pixi.toml``/``pixi.lock`` when they need
    those more expressive dependency forms.
    """

    project_name = re.sub(r"[^a-z0-9-]+", "-", str(addon_id).lower()).strip("-")
    if not project_name:
        raise ValueError("A stable add-on ID is required for a Pixi project.")

    conda_entries: dict[str, tuple[str, str | None]] = {
        "python": (_python_constraint(python_version), None)
    }
    for raw_spec in conda_packages:
        name, constraint, channel = _conda_dependency(raw_spec)
        key = name.lower()
        value = (constraint, channel)
        if key in conda_entries and conda_entries[key] != value:
            raise ValueError(f"Conflicting Pixi dependency declarations for {name!r}.")
        conda_entries[key] = value

    pypi_entries: dict[str, tuple[str, tuple[str, ...]]] = {}
    for raw_spec in pip_packages:
        try:
            requirement = Requirement(str(raw_spec).strip())
        except InvalidRequirement as exc:
            raise ValueError(
                f"Invalid PyPI dependency specification: {raw_spec!r}."
            ) from exc
        if requirement.url is not None or requirement.marker is not None:
            raise ValueError(
                "Generated Pixi projects do not accept direct URLs or environment "
                f"markers: {raw_spec!r}. Ship a reviewed pixi.toml instead."
            )
        key = requirement.name.lower()
        value = (str(requirement.specifier) or "*", tuple(sorted(requirement.extras)))
        if key in pypi_entries and pypi_entries[key] != value:
            raise ValueError(f"Conflicting PyPI dependency declarations for {key!r}.")
        pypi_entries[key] = value

    if pixi_platform is not None and pixi_platforms is not None:
        raise ValueError("Declare either pixi_platform or pixi_platforms, not both.")
    platforms = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (pixi_platforms or (pixi_platform or _current_pixi_platform(),))
            if str(item).strip()
        )
    )
    if not platforms:
        raise ValueError("At least one Pixi target platform is required.")

    lines = [
        "[workspace]",
        f"name = {_toml_string(f'carlina-addon-{project_name}')}",
        'version = "0.0.0"',
        'channels = ["conda-forge"]',
        "platforms = [" + ", ".join(_toml_string(item) for item in platforms) + "]",
        "conda-pypi-map = {}",
        "",
        "[dependencies]",
    ]
    for name, (constraint, channel) in sorted(conda_entries.items()):
        if channel:
            lines.append(
                f"{_toml_string(name)} = "
                f"{{ version = {_toml_string(constraint)}, "
                f"channel = {_toml_string(channel)} }}"
            )
        else:
            lines.append(f"{_toml_string(name)} = {_toml_string(constraint)}")

    if pypi_entries:
        lines.extend(("", "[pypi-dependencies]"))
        for name, (constraint, extras) in sorted(pypi_entries.items()):
            if extras:
                rendered_extras = ", ".join(_toml_string(item) for item in extras)
                lines.append(
                    f"{_toml_string(name)} = "
                    f"{{ version = {_toml_string(constraint)}, "
                    f"extras = [{rendered_extras}] }}"
                )
            else:
                lines.append(f"{_toml_string(name)} = {_toml_string(constraint)}")
    return "\n".join(lines) + "\n"


def prepare_generated_pixi_project(
    paths: AddonEnvironmentPaths,
    addon_id: str,
    *,
    python_version: str = "3.11",
    conda_packages: tuple[str, ...] | list[str] = (),
    pip_packages: tuple[str, ...] | list[str] = (),
) -> Path:
    """Atomically materialize a generated Pixi manifest below DataRoot."""

    content = render_generated_pixi_manifest(
        addon_id,
        python_version=python_version,
        conda_packages=conda_packages,
        pip_packages=pip_packages,
    )
    paths.pixi_project_dir.mkdir(parents=True, exist_ok=True)
    destination = paths.pixi_manifest
    if destination.is_file() and destination.read_text(encoding="utf-8") == content:
        return destination
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def prepare_pixi_project(
    paths: AddonEnvironmentPaths,
    addon_dir: str | Path,
    *,
    manifest_path: str | Path = "pixi.toml",
    lock_path: str | Path = "pixi.lock",
) -> Path:
    """Copy immutable Pixi inputs into the rebuildable environment root."""

    source_root = Path(addon_dir).resolve()
    source_manifest = (source_root / manifest_path).resolve()
    source_manifest.relative_to(source_root)
    if not source_manifest.is_file():
        raise FileNotFoundError(f"Pixi manifest not found: {source_manifest}")
    paths.pixi_project_dir.mkdir(parents=True, exist_ok=True)
    sources = (
        ("pixi.toml", source_manifest),
        ("pixi.lock", (source_root / lock_path).resolve()),
    )
    for name, source in sources:
        source.relative_to(source_root)
        if not source.is_file():
            continue
        destination = paths.pixi_project_dir / name
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            continue
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    return paths.pixi_manifest


__all__ = [
    "AddonEnvironmentPaths",
    "dependency_fingerprint",
    "dependency_generation_name",
    "prepare_generated_pixi_project",
    "prepare_pixi_project",
    "python_environment_fingerprint",
    "render_generated_pixi_manifest",
    "resolve_environment_paths",
]
