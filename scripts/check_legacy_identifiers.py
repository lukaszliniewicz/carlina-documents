"""Enforce the reviewed legacy-identifier inventory during the Carlina rename."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_POLICY = Path("policy/legacy-identifiers.json")
MAX_SCANNED_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScopeResult:
    scope_id: str
    expected: int
    actual: int
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    pattern: str
    scopes: tuple[ScopeResult, ...]
    unclassified: tuple[tuple[str, int], ...]

    @property
    def valid(self) -> bool:
        return not self.unclassified and all(
            scope.actual == scope.expected for scope in self.scopes
        )


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _tracked_paths(repository: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return tuple(path.decode("utf-8") for path in completed.stdout.split(b"\0") if path)


def _read_text(repository: Path, relative_path: str) -> str | None:
    path = repository / relative_path
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCANNED_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return None


def _validated_scope_ids(scopes: Any) -> frozenset[str]:
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("Legacy identifier policy must declare ordered scopes.")

    scope_ids: set[str] = set()
    for scope in scopes:
        if not isinstance(scope, dict):
            raise ValueError("Every legacy identifier scope must be an object.")
        scope_id = str(scope.get("id") or "").strip()
        paths = scope.get("paths")
        if not scope_id or scope_id in scope_ids:
            raise ValueError(
                "Legacy identifier scope IDs must be unique and non-empty."
            )
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"Scope '{scope_id}' must declare paths.")
        scope_ids.add(scope_id)
    return frozenset(scope_ids)


def _validate_rules(rules: Any, scope_ids: frozenset[str]) -> None:
    if not isinstance(rules, list) or not rules:
        raise ValueError("Legacy identifier policy must declare rules.")

    rule_ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Every legacy identifier rule must be an object.")
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id or rule_id in rule_ids:
            raise ValueError("Legacy identifier rule IDs must be unique and non-empty.")
        try:
            re.compile(str(rule["pattern"]))
        except (KeyError, re.error) as exc:
            raise ValueError(f"Rule '{rule_id}' has an invalid pattern: {exc}") from exc
        expected = rule.get("expected_by_scope")
        if not isinstance(expected, dict) or set(expected) != scope_ids:
            raise ValueError(
                f"Rule '{rule_id}' must define one expectation for every scope."
            )
        if any(type(value) is not int or value < 0 for value in expected.values()):
            raise ValueError(
                f"Rule '{rule_id}' expectations must be non-negative integers."
            )
        rule_ids.add(rule_id)


def _validated_policy(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    if policy.get("schema_version") != 1:
        raise ValueError("Legacy identifier policy must use schema_version 1.")
    scope_ids = _validated_scope_ids(policy.get("scopes"))
    _validate_rules(policy.get("rules"), scope_ids)
    return policy


def audit_repository(
    repository: Path,
    policy: Mapping[str, Any],
    *,
    tracked_paths: Sequence[str] | None = None,
) -> tuple[RuleResult, ...]:
    """Return exact-scope results for tracked, bounded, UTF-8 text files."""

    checked_policy = _validated_policy(policy)
    ignored = tuple(str(value) for value in checked_policy.get("ignored_paths", []))
    paths = (
        tuple(tracked_paths)
        if tracked_paths is not None
        else _tracked_paths(repository)
    )
    texts = {
        path: text
        for path in paths
        if not _matches_any(path, ignored)
        if (text := _read_text(repository, path)) is not None
    }
    scopes = tuple(checked_policy["scopes"])
    results: list[RuleResult] = []

    for rule in checked_policy["rules"]:
        expression = re.compile(str(rule["pattern"]))
        expected = rule["expected_by_scope"]
        counts = {str(scope["id"]): 0 for scope in scopes}
        matched_paths: dict[str, set[str]] = {
            str(scope["id"]): set() for scope in scopes
        }
        unclassified: list[tuple[str, int]] = []

        for relative_path, text in texts.items():
            count = sum(1 for _match in expression.finditer(text))
            if not count:
                continue
            selected_scope = next(
                (
                    scope
                    for scope in scopes
                    if _matches_any(relative_path, scope["paths"])
                ),
                None,
            )
            if selected_scope is None:
                unclassified.append((relative_path, count))
                continue
            scope_id = str(selected_scope["id"])
            counts[scope_id] += count
            matched_paths[scope_id].add(relative_path)

        results.append(
            RuleResult(
                rule_id=str(rule["id"]),
                pattern=str(rule["pattern"]),
                scopes=tuple(
                    ScopeResult(
                        scope_id=str(scope["id"]),
                        expected=int(expected[str(scope["id"])]),
                        actual=counts[str(scope["id"])],
                        paths=tuple(sorted(matched_paths[str(scope["id"])])),
                    )
                    for scope in scopes
                ),
                unclassified=tuple(sorted(unclassified)),
            )
        )
    return tuple(results)


def _payload(results: Sequence[RuleResult]) -> dict[str, Any]:
    return {
        "valid": all(result.valid for result in results),
        "rules": [
            {
                "id": result.rule_id,
                "valid": result.valid,
                "unclassified": [
                    {"path": path, "occurrences": count}
                    for path, count in result.unclassified
                ],
                "scopes": [
                    {
                        "id": scope.scope_id,
                        "expected": scope.expected,
                        "actual": scope.actual,
                        "paths": list(scope.paths),
                    }
                    for scope in result.scopes
                ],
            }
            for result in results
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Carlina's exact legacy-identifier inventory."
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Include matching file paths in human-readable output.",
    )
    args = parser.parse_args(argv)

    repository = Path(__file__).resolve().parents[1]
    policy_path = (repository / args.policy).resolve()
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    results = audit_repository(repository, policy)
    payload = _payload(results)
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for result in results:
            marker = "OK" if result.valid else "FAIL"
            print(f"[{marker}] {result.rule_id}")
            for scope in result.scopes:
                suffix = ""
                if args.show_paths and scope.paths:
                    suffix = "\n      " + "\n      ".join(scope.paths)
                print(
                    f"  {scope.scope_id}: {scope.actual} "
                    f"(expected {scope.expected}){suffix}"
                )
            for path, count in result.unclassified:
                print(f"  unclassified: {path} ({count})")
    return 0 if payload["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
