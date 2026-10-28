"""Run the reviewed strict Python quality lane without mutating the checkout."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, TypeGuard, cast

CheckName = Literal["policy", "lint", "format", "types"]

_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "production_roots",
        "test_roots",
        "excluded_paths",
        "legacy_files",
        "strict_files",
        "typing_baseline_file",
    }
)


class PolicyError(ValueError):
    """A content-free policy or source-inventory validation failure."""


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    schema_version: int
    production_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    legacy_files: Mapping[str, tuple[str, ...]]
    strict_files: tuple[str, ...]
    typing_baseline_file: str


@dataclass(frozen=True, slots=True)
class SourceScope:
    strict_files: tuple[str, ...]
    strict_production_files: tuple[str, ...]
    legacy_files: tuple[str, ...]
    policy: QualityPolicy = field(repr=False)
    repository: Path = field(repr=False)


def _object(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PolicyError(message)
    raw_object = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in raw_object.items():
        if not isinstance(key, str):
            raise PolicyError(message)
        result[key] = item
    return result


def _read_json(path: Path, message: str) -> object:
    try:
        parsed: object = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except OSError, UnicodeError, json.JSONDecodeError, ValueError:
        raise PolicyError(message) from None
    return parsed


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _relative_path(value: str, *, suffix: str | None = None) -> str:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or (len(value) > 1 and value[1] == ":")
    ):
        raise PolicyError("invalid relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyError("invalid relative path")
    pure_path = PurePosixPath(value)
    if pure_path.is_absolute() or pure_path.as_posix() != value:
        raise PolicyError("invalid relative path")
    if suffix is not None and pure_path.suffix != suffix:
        raise PolicyError("invalid path suffix")
    return value


def _root_name(value: str) -> str:
    root = _relative_path(value)
    if "/" in root or any(char in root for char in "*?[]"):
        raise PolicyError("invalid source root")
    return root


def _string_tuple(value: object, message: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PolicyError(message)
    raw_items = cast(list[object], value)
    result: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            raise PolicyError(message)
        result.append(item)
    return tuple(result)


def _duplicates(values: Sequence[str]) -> bool:
    return len(set(values)) != len(values)


def _diagnostics_are_native_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(isinstance(diagnostic, dict) for diagnostic in cast(list[object], value))


def _pattern_parts(pattern: str) -> tuple[str, ...]:
    if (
        not pattern
        or pattern.startswith("/")
        or "\\" in pattern
        or (len(pattern) > 1 and pattern[1] == ":")
    ):
        raise PolicyError("invalid exclusion pattern")
    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyError("invalid exclusion pattern")
    if any("**" in part and part != "**" for part in parts):
        raise PolicyError("invalid exclusion pattern")
    return tuple(parts)


def _covers_every_source_path_under_root(parts: tuple[str, ...], root: str) -> bool:
    prefix_length = 0
    while prefix_length < len(parts) and parts[prefix_length] == "**":
        prefix_length += 1
    if prefix_length >= len(parts):
        return True
    if not fnmatch.fnmatchcase(root, parts[prefix_length]):
        return False
    tail = parts[prefix_length + 1 :]
    if not tail or any(part not in {"*", "**", "*.py"} for part in tail):
        return False
    return "**" in tail and sum(part != "**" for part in tail) <= 1


def _is_global_python_blanket(parts: tuple[str, ...]) -> bool:
    if "**" not in parts or any(part not in {"*", "**", "*.py"} for part in parts):
        return False
    required = tuple(part for part in parts if part != "**")
    return len(required) <= 2 and (
        not required
        or (
            required[-1] in {"*", "*.py"} and all(part == "*" for part in required[:-1])
        )
    )


def load_policy(path: Path) -> QualityPolicy:
    """Load and validate the strict-lane policy using only standard-library code."""
    data = _object(
        _read_json(path, "invalid Python quality policy JSON"), "invalid policy"
    )
    if len(data) != len(_POLICY_KEYS) or not all(key in _POLICY_KEYS for key in data):
        raise PolicyError("invalid policy fields")
    version = data["schema_version"]
    if type(version) is not int or version != 1:
        raise PolicyError("unsupported policy schema")

    production_roots = tuple(
        _root_name(value)
        for value in _string_tuple(data["production_roots"], "invalid production roots")
    )
    test_roots = tuple(
        _root_name(value)
        for value in _string_tuple(data["test_roots"], "invalid test roots")
    )
    all_roots = (*production_roots, *test_roots)
    if (
        not production_roots
        or not test_roots
        or _duplicates(production_roots)
        or _duplicates(test_roots)
        or _duplicates(all_roots)
    ):
        raise PolicyError("invalid or duplicate source roots")
    for index, root in enumerate(all_roots):
        if any(other.startswith(f"{root}/") for other in all_roots[index + 1 :]):
            raise PolicyError("overlapping source roots")

    exclusions = tuple(_string_tuple(data["excluded_paths"], "invalid exclusions"))
    if _duplicates(exclusions):
        raise PolicyError("duplicate exclusion pattern")
    parsed_exclusions = tuple(_pattern_parts(pattern) for pattern in exclusions)
    if any(_is_global_python_blanket(parts) for parts in parsed_exclusions):
        raise PolicyError("exclusions cover all Python sources")
    if any(
        _covers_every_source_path_under_root(parts, root)
        for parts in parsed_exclusions
        for root in all_roots
    ):
        raise PolicyError("exclusions cover a source root")

    raw_legacy = _object(data["legacy_files"], "invalid legacy inventory")
    allowed_roots = frozenset(all_roots)
    legacy: dict[str, tuple[str, ...]] = {}
    all_legacy_paths: set[str] = set()
    for root, raw_paths in raw_legacy.items():
        if root not in allowed_roots:
            raise PolicyError("legacy inventory names an unknown root")
        relative_paths = tuple(
            _relative_path(value, suffix=".py")
            for value in _string_tuple(raw_paths, "invalid legacy inventory")
        )
        if _duplicates(relative_paths):
            raise PolicyError("duplicate legacy path")
        repository_paths = {f"{root}/{relative}" for relative in relative_paths}
        if all_legacy_paths.intersection(repository_paths):
            raise PolicyError("duplicate legacy path")
        all_legacy_paths.update(repository_paths)
        legacy[root] = relative_paths

    strict_files = tuple(
        _relative_path(value, suffix=".py")
        for value in _string_tuple(data["strict_files"], "invalid strict inventory")
    )
    if _duplicates(strict_files):
        raise PolicyError("duplicate strict path")
    for source_path in strict_files:
        if not _path_in_roots(source_path, all_roots):
            raise PolicyError("strict path is outside source roots")
        if source_path in all_legacy_paths:
            raise PolicyError("promoted path remains in legacy inventory")

    baseline_value = data["typing_baseline_file"]
    if not isinstance(baseline_value, str):
        raise PolicyError("invalid typing baseline path")
    baseline_path = _relative_path(baseline_value, suffix=".json")

    return QualityPolicy(
        schema_version=1,
        production_roots=production_roots,
        test_roots=test_roots,
        excluded_paths=exclusions,
        legacy_files=MappingProxyType(dict(sorted(legacy.items()))),
        strict_files=strict_files,
        typing_baseline_file=baseline_path,
    )


def _repository_path(repository: Path) -> Path:
    try:
        resolved = repository.resolve(strict=True)
    except OSError:
        raise PolicyError("repository root is unavailable") from None
    if not resolved.is_dir():
        raise PolicyError("repository root is unavailable")
    return resolved


def collect_python_paths(repository: Path) -> tuple[str, ...]:
    """Return tracked and non-ignored untracked Python paths from Git."""
    root = _repository_path(repository)
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError:
        raise PolicyError("source inventory command failed") from None
    if completed.returncode != 0:
        raise PolicyError("source inventory command failed")
    paths: set[str] = set()
    for raw_path in completed.stdout.split(b"\x00"):
        if not raw_path or not raw_path.endswith(b".py"):
            continue
        decoded = os.fsdecode(raw_path)
        paths.add(_relative_path(decoded, suffix=".py"))
    return tuple(sorted(paths))


def _path_in_roots(source_path: str, roots: Sequence[str]) -> bool:
    return any(source_path.startswith(f"{root}/") for root in roots)


def _glob_matches(pattern: str, source_path: str) -> bool:
    pattern_parts = tuple(pattern.split("/"))
    path_parts = tuple(source_path.split("/"))

    def matches(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return matches(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and matches(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
            and matches(pattern_index + 1, path_index + 1)
        )

    return matches(0, 0)


def _repo_file(repository: Path, source_path: str) -> Path:
    return repository.joinpath(*PurePosixPath(source_path).parts)


def _legacy_repository_paths(policy: QualityPolicy) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{root}/{relative}"
            for root, relative_paths in policy.legacy_files.items()
            for relative in relative_paths
        )
    )


def _check_root_directories(repository: Path, roots: Sequence[str]) -> None:
    for root in roots:
        root_path = repository / root
        try:
            resolved = root_path.resolve(strict=True)
        except OSError:
            raise PolicyError("configured source root is unavailable") from None
        if not resolved.is_relative_to(repository) or not resolved.is_dir():
            raise PolicyError("configured source root is unavailable")


def _validate_source_file(repository: Path, source_path: str) -> None:
    source = _repo_file(repository, source_path)
    try:
        resolved = source.resolve(strict=True)
    except OSError, RuntimeError:
        raise PolicyError("source inventory contains an unavailable file") from None
    if not resolved.is_relative_to(repository):
        raise PolicyError("source symlink leaves the repository")
    if not resolved.is_file():
        raise PolicyError("source inventory contains an unavailable file")


def resolve_scope(
    repository: Path,
    policy: QualityPolicy,
    *,
    paths: Sequence[str] | None = None,
) -> SourceScope:
    """Resolve the current strict and legacy source files for one repository."""
    root = _repository_path(repository)
    all_roots = (*policy.production_roots, *policy.test_roots)
    _check_root_directories(root, all_roots)

    discovered = collect_python_paths(root) if paths is None else paths
    candidates: set[str] = set()
    for raw_path in discovered:
        source_path = _relative_path(raw_path)
        if not source_path.endswith(".py") or not _path_in_roots(
            source_path, all_roots
        ):
            continue
        _validate_source_file(root, source_path)
        candidates.add(source_path)

    legacy_paths = _legacy_repository_paths(policy)
    strict_policy_paths = policy.strict_files
    required_paths = (*legacy_paths, *strict_policy_paths)
    if any(source_path not in candidates for source_path in required_paths):
        raise PolicyError("declared source inventory contains a missing file")

    for source_path in required_paths:
        if any(
            _glob_matches(pattern, source_path) for pattern in policy.excluded_paths
        ):
            raise PolicyError("declared source inventory is excluded")

    excluded = {
        source_path
        for source_path in candidates
        if any(_glob_matches(pattern, source_path) for pattern in policy.excluded_paths)
    }
    if candidates and candidates == excluded:
        raise PolicyError("exclusions cover all Python sources")
    for production_root in policy.production_roots:
        root_sources = {
            source_path
            for source_path in candidates
            if source_path.startswith(f"{production_root}/")
        }
        if root_sources and root_sources.issubset(excluded):
            raise PolicyError("exclusions cover a production root")

    eligible = candidates - excluded
    legacy_set = set(legacy_paths)
    strict_files = tuple(sorted(eligible - legacy_set))
    strict_production_files = tuple(
        source_path
        for source_path in strict_files
        if _path_in_roots(source_path, policy.production_roots)
    )
    return SourceScope(
        strict_files=strict_files,
        strict_production_files=strict_production_files,
        legacy_files=tuple(sorted(legacy_set)),
        policy=policy,
        repository=root,
    )


def _production_legacy_paths(policy: QualityPolicy) -> frozenset[str]:
    production_roots = frozenset(policy.production_roots)
    return frozenset(
        f"{root}/{relative}"
        for root, relative_paths in policy.legacy_files.items()
        if root in production_roots
        for relative in relative_paths
    )


def _validate_typing_baseline(repository: Path, scope: SourceScope) -> None:
    baseline = _repo_file(repository, scope.policy.typing_baseline_file)
    try:
        resolved = baseline.resolve(strict=False)
    except OSError, RuntimeError:
        raise PolicyError("invalid typing baseline location") from None
    if not resolved.is_relative_to(repository):
        raise PolicyError("typing baseline leaves the repository")
    if not baseline.exists():
        return

    data = _object(
        _read_json(baseline, "invalid native typing baseline"), "invalid baseline"
    )
    if set(data) != {"files"}:
        raise PolicyError("invalid native typing baseline")
    files = _object(data["files"], "invalid native typing baseline")
    allowed_legacy = _production_legacy_paths(scope.policy)
    in_scope_legacy = set(scope.legacy_files).intersection(allowed_legacy)
    for key, diagnostics in files.items():
        if not key.startswith("./"):
            raise PolicyError("invalid native baseline path")
        relative = _relative_path(key[2:], suffix=".py")
        if f"./{relative}" != key or relative not in in_scope_legacy:
            raise PolicyError("native baseline path is outside legacy production scope")
        if not _diagnostics_are_native_list(diagnostics):
            raise PolicyError("invalid native baseline diagnostics")


def _strict_typecheck_config(repository: Path, scope: SourceScope) -> int:
    if not scope.strict_production_files:
        print(
            f"types: checked 0 strict production files; "
            f"{len(scope.legacy_files)} legacy files in scope"
        )
        return 0

    temporary_parent: Path | None = None
    for candidate in (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and not resolved.is_relative_to(repository):
            temporary_parent = resolved
            break
    if temporary_parent is None:
        print("types: external temporary directory unavailable", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(
            prefix="carlina-python-quality-", dir=temporary_parent
        ) as temporary_name:
            temporary_root = Path(temporary_name)
            config_path = temporary_root / "basedpyrightconfig.json"
            unmatched_baseline = temporary_root / "strict-baseline-never-created.json"
            config = {
                "extends": str(repository / "pyrightconfig.json"),
                "include": [
                    str(_repo_file(repository, item))
                    for item in scope.strict_production_files
                ],
                "typeCheckingMode": "strict",
                "extraPaths": [str(repository)],
                "baselineFile": str(unmatched_baseline),
            }
            config_path.write_text(
                json.dumps(config, indent=2) + "\n", encoding="utf-8"
            )
            environment = os.environ.copy()
            environment["CI"] = "true"
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "basedpyright",
                        "--project",
                        str(config_path),
                        "--warnings",
                    ],
                    cwd=repository,
                    env=environment,
                    check=False,
                )
            except OSError:
                print("types: checker failed to start", file=sys.stderr)
                return 127
            print(
                f"types: checked {len(scope.strict_production_files)} strict production files; "
                f"{len(scope.legacy_files)} legacy files in scope"
            )
            return completed.returncode
    except OSError:
        print("types: temporary configuration failed", file=sys.stderr)
        return 2


def run_check(repository: Path, scope: SourceScope, check: CheckName) -> int:
    """Run one policy, Ruff, format, or isolated strict typing check."""
    root = _repository_path(repository)
    if root != scope.repository:
        raise PolicyError("scope belongs to a different repository")
    if check == "policy":
        _validate_typing_baseline(root, scope)
        print(
            f"policy: checked {len(scope.strict_files)} strict files; "
            f"{len(scope.legacy_files)} legacy files in scope"
        )
        return 0
    if check == "types":
        return _strict_typecheck_config(root, scope)

    files = scope.strict_files
    if not files:
        print(
            f"{check}: checked 0 strict files; {len(scope.legacy_files)} legacy files in scope"
        )
        return 0
    command = [sys.executable, "-m", "ruff"]
    if check == "lint":
        command.extend(["check", "--config", str(root / "ruff-strict.toml")])
    elif check == "format":
        command.extend(
            ["format", "--check", "--config", str(root / "ruff-strict.toml")]
        )
    else:
        raise PolicyError("unknown check")
    command.extend(str(_repo_file(root, source_path)) for source_path in files)
    try:
        completed = subprocess.run(command, cwd=root, check=False)
    except OSError:
        print(f"{check}: checker failed to start", file=sys.stderr)
        return 127
    print(
        f"{check}: checked {len(files)} strict files; "
        f"{len(scope.legacy_files)} legacy files in scope"
    )
    return completed.returncode


def _is_check_name(value: str) -> TypeGuard[CheckName]:
    return value in {"policy", "lint", "format", "types"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    script_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", type=Path, default=script_root)
    parser.add_argument(
        "--policy", type=Path, default=Path("policy/python-quality.json")
    )
    parser.add_argument(
        "--check", choices=("policy", "lint", "format", "types"), default="policy"
    )
    arguments = parser.parse_args(argv)

    root = arguments.root
    policy_path = arguments.policy
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    try:
        policy = load_policy(policy_path)
        scope = resolve_scope(root, policy)
        if not _is_check_name(arguments.check):
            raise PolicyError("unknown check")
        return run_check(root, scope, arguments.check)
    except PolicyError as error:
        print(f"Python quality policy error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
