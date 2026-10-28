"""Enforce the reviewed Documents source, lock, and wheel closure budgets."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import struct
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, NoReturn, cast

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
POLICY_PATH: Final = PROJECT_ROOT / "policy" / "documents-closure.json"
if not __package__ and os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

if TYPE_CHECKING or not __package__:
    from scripts import measure_documents_closure as collector
else:
    from . import measure_documents_closure as collector

_SOURCE_GROUPS: Final = (
    "carlina",
    "iarf_assistant.host",
    "iarf_assistant.addons.doc_processor",
    "iarf_assistant.addons.sentence_segmenter",
    "iarf_assistant.addons.translator",
)
_LOCK_CONTEXTS: Final = (
    "documents",
    "doc_processor:host",
    "doc_processor:document_processing",
    "sentence_segmenter:native_segmentation",
    "translator:host",
)
_PLATFORMS: Final = ("linux-64", "win-64")
_POLICY_KEYS: Final = frozenset(
    {
        "schema_version",
        "source",
        "host_retention",
        "locks",
        "wheel",
        "fresh_documents_host_module_limit",
    }
)
_HOST_RETENTION_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HOST_RETENTION_GLOBS: Final = frozenset("*?[]")
_HOST_RETENTION_NOTICES: Final = frozenset({"LICENSE", "THIRD_PARTY_NOTICES.md"})
_INVALID_INPUT: Final = "Documents closure policy or measurement input is invalid."
_INVALID_ARGUMENTS: Final = "Documents closure arguments are invalid."


@dataclass(frozen=True, slots=True)
class _Policy:
    source: dict[str, dict[str, int]]
    host_retention: frozenset[str]
    locks: dict[str, dict[str, int]]
    wheel_file_bytes: int
    wheel_file_count: int
    fresh_documents_host_module_limit: int


class _ArgumentError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _ArgumentError from None


def _invalid() -> NoReturn:
    raise ValueError(_INVALID_INPUT) from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _invalid()
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        _invalid()
    return {cast(str, key): item for key, item in raw.items()}


def _require_keys(value: dict[str, object], expected: frozenset[str]) -> None:
    if value.keys() != expected:
        _invalid()


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        _invalid()
    return value


def _normalized_member_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _invalid()
    if any(
        character in _HOST_RETENTION_GLOBS or unicodedata.category(character) == "Cc"
        for character in value
    ):
        _invalid()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _invalid()
    return value


def _host_retention_member(value: object) -> str:
    member = _normalized_member_path(value)
    if not (
        member.startswith("carlina/")
        or (
            member.startswith("iarf_assistant/")
            and not member.startswith("iarf_assistant/addons/")
            and member != "iarf_assistant/addons"
        )
        or member in _HOST_RETENTION_NOTICES
    ):
        _invalid()
    return member


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _load_policy() -> _Policy:
    try:
        status = POLICY_PATH.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_size > 1024 * 1024:
            _invalid()
        raw_policy = json.loads(
            POLICY_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        _invalid()

    policy = _object(raw_policy)
    _require_keys(policy, _POLICY_KEYS)
    version = policy["schema_version"]
    if type(version) is not int or version != 2:
        _invalid()

    source = _object(policy["source"])
    _require_keys(source, frozenset({"groups"}))
    groups = _object(source["groups"])
    _require_keys(groups, frozenset(_SOURCE_GROUPS))
    source_limits: dict[str, dict[str, int]] = {}
    for name in _SOURCE_GROUPS:
        group = _object(groups[name])
        _require_keys(group, frozenset({"member_count", "bytes"}))
        source_limits[name] = {
            "member_count": _nonnegative_int(group["member_count"]),
            "bytes": _nonnegative_int(group["bytes"]),
        }

    raw_host_retention = _object(policy["host_retention"])
    if not raw_host_retention:
        _invalid()
    host_retention: set[str] = set()
    for name, raw_group in raw_host_retention.items():
        if _HOST_RETENTION_NAME.fullmatch(name) is None:
            _invalid()
        group = _object(raw_group)
        _require_keys(group, frozenset({"reason", "members"}))
        reason = group["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 800:
            _invalid()
        raw_members = group["members"]
        if not isinstance(raw_members, list) or not raw_members:
            _invalid()
        members = [
            _host_retention_member(member) for member in cast(list[object], raw_members)
        ]
        if len(members) != len(set(members)) or host_retention.intersection(members):
            _invalid()
        host_retention.update(members)

    locks = _object(policy["locks"])
    _require_keys(locks, frozenset({"platforms"}))
    platforms = _object(locks["platforms"])
    _require_keys(platforms, frozenset(_PLATFORMS))
    lock_limits: dict[str, dict[str, int]] = {}
    for platform in _PLATFORMS:
        contexts = _object(platforms[platform])
        _require_keys(contexts, frozenset(_LOCK_CONTEXTS))
        lock_limits[platform] = {
            context: _nonnegative_int(contexts[context]) for context in _LOCK_CONTEXTS
        }

    wheel = _object(policy["wheel"])
    _require_keys(wheel, frozenset({"file_bytes", "file_count"}))
    return _Policy(
        source=source_limits,
        host_retention=frozenset(host_retention),
        locks=lock_limits,
        wheel_file_bytes=_nonnegative_int(wheel["file_bytes"]),
        wheel_file_count=_nonnegative_int(wheel["file_count"]),
        fresh_documents_host_module_limit=_nonnegative_int(
            policy["fresh_documents_host_module_limit"]
        ),
    )


def _named_records(
    value: object, *, expected_names: tuple[str, ...], name_field: str
) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        _invalid()
    records: dict[str, dict[str, object]] = {}
    for raw_record in cast(list[object], value):
        record = _object(raw_record)
        name = record.get(name_field)
        if not isinstance(name, str) or name not in expected_names or name in records:
            _invalid()
        records[name] = record
    if records.keys() != frozenset(expected_names):
        _invalid()
    return records


def _source_violations(measured: object, policy: _Policy) -> list[str]:
    summary = _object(measured)
    groups = _named_records(
        summary.get("groups"), expected_names=_SOURCE_GROUPS, name_field="name"
    )
    violations: list[str] = []
    all_members: set[str] = set()
    retained_members: set[str] = set()
    for name in _SOURCE_GROUPS:
        record = groups[name]
        limits = policy.source[name]
        for metric in ("member_count", "bytes"):
            value = _nonnegative_int(record.get(metric))
            if value > limits[metric]:
                violations.append(f"source.{name}.{metric}")
        raw_members = record.get("members")
        if not isinstance(raw_members, list):
            _invalid()
        members = [
            _normalized_member_path(member)
            for member in cast(list[object], raw_members)
        ]
        if members != sorted(members):
            _invalid()
        if len(members) != len(set(members)) or all_members.intersection(members):
            _invalid()
        member_count = _nonnegative_int(record.get("member_count"))
        if member_count != len(members):
            _invalid()
        all_members.update(members)
        if name in {"carlina", "iarf_assistant.host"}:
            retained_members.update(members)
    if retained_members - policy.host_retention:
        violations.append("source.host_retention.unclassified")
    if policy.host_retention - retained_members:
        violations.append("source.host_retention.stale")
    return violations


def _lock_violations(measured: object, policy: _Policy, platform: str) -> list[str]:
    summary = _object(measured)
    if summary.get("platform") != platform:
        _invalid()
    contexts = _named_records(
        summary.get("contexts"), expected_names=_LOCK_CONTEXTS, name_field="context"
    )
    violations: list[str] = []
    for context in _LOCK_CONTEXTS:
        package_count = _nonnegative_int(contexts[context].get("package_count"))
        if package_count > policy.locks[platform][context]:
            violations.append(f"locks.{context}.package_count")
    return violations


def _wheel_violations(measured: object, policy: _Policy) -> list[str]:
    summary = _object(measured)
    violations: list[str] = []
    for metric, limit in (
        ("file_bytes", policy.wheel_file_bytes),
        ("file_count", policy.wheel_file_count),
    ):
        value = _nonnegative_int(summary.get(metric))
        if value > limit:
            violations.append(f"wheel.{metric}")
    return violations


def verify_closure(
    root: Path, *, platform: str, wheel: Path | None = None
) -> tuple[str, ...]:
    """Return safe budget labels, or reject malformed policy and measurements."""
    if (
        not isinstance(cast(object, root), Path)
        or platform not in _PLATFORMS
        or (wheel is not None and not isinstance(cast(object, wheel), Path))
    ):
        _invalid()

    try:
        policy = _load_policy()
        source = collector.source_metrics(root)
        locks = collector.locked_dependencies(root, platform)
        measured_wheel = collector.wheel_metrics(wheel) if wheel is not None else None
        violations = _source_violations(source, policy)
        violations.extend(_lock_violations(locks, policy, platform))
        if measured_wheel is not None:
            violations.extend(_wheel_violations(measured_wheel, policy))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        UnicodeError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        struct.error,
        RecursionError,
    ):
        _invalid()
    return tuple(sorted(violations))


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--platform", choices=_PLATFORMS, default="linux-64")
    parser.add_argument("--wheel", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        violations = verify_closure(args.root, platform=args.platform, wheel=args.wheel)
    except _ArgumentError:
        print(_INVALID_ARGUMENTS, file=sys.stderr)
        return 2
    except ValueError:
        print(_INVALID_INPUT, file=sys.stderr)
        return 2

    if violations:
        print(
            f"Documents closure budget check failed: {len(violations)} violation(s).",
            file=sys.stderr,
        )
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1

    wheel_status = "checked" if args.wheel is not None else "not supplied"
    print(
        "Documents closure budget check passed: "
        f"{len(_SOURCE_GROUPS)} source groups, "
        f"{len(_LOCK_CONTEXTS)} lock contexts, wheel {wheel_status}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
