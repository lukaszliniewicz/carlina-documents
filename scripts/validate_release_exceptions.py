#!/usr/bin/env python3
"""Validate that every dependency/license exception is owned and expiring."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT_ROOT / "policy" / "release-exceptions.json"
REQUIRED_TEXT_FIELDS = {
    "id",
    "kind",
    "package",
    "owner",
    "reason",
    "remediation",
    "expires",
}


def _load_policy(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Policy file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Policy file is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("Policy root must be a JSON object.")
    return payload


def validate_policy(
    payload: dict,
    *,
    today: date | None = None,
    public_release: bool = False,
) -> list[str]:
    errors: list[str] = []
    active_date = today or datetime.now(timezone.utc).date()
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    exceptions = payload.get("exceptions")
    if not isinstance(exceptions, list):
        return [*errors, "exceptions must be an array"]

    seen_ids: set[str] = set()
    for index, record in enumerate(exceptions):
        label = f"exceptions[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{label} must be an object")
            continue
        for field in sorted(REQUIRED_TEXT_FIELDS):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}.{field} must be a non-empty string")
        if record.get("kind") == "advisory":
            advisory_ids = record.get("advisory_ids")
            if (
                not isinstance(advisory_ids, list)
                or not advisory_ids
                or not all(
                    isinstance(value, str) and value.strip() for value in advisory_ids
                )
            ):
                errors.append(f"{label}.advisory_ids must contain non-empty strings")
        record_id = str(record.get("id") or "")
        if record_id in seen_ids:
            errors.append(f"duplicate exception id: {record_id}")
        seen_ids.add(record_id)
        try:
            expiry = date.fromisoformat(str(record.get("expires") or ""))
        except ValueError:
            errors.append(f"{label}.expires must be an ISO date")
        else:
            if expiry < active_date:
                errors.append(f"{record_id or label} expired on {expiry.isoformat()}")
        contexts = record.get("allowed_contexts")
        if (
            not isinstance(contexts, list)
            or not contexts
            or not all(isinstance(value, str) and value.strip() for value in contexts)
        ):
            errors.append(f"{label}.allowed_contexts must contain non-empty strings")
        if not isinstance(record.get("release_blocking"), bool):
            errors.append(f"{label}.release_blocking must be boolean")
        elif public_release and record["release_blocking"]:
            errors.append(
                f"{record_id or label} blocks public release: "
                f"{record.get('remediation', '')}"
            )
    return errors


def pip_audit_ignored_advisories(payload: dict) -> list[str]:
    """Return stable pip-audit IDs from validated advisory exceptions."""

    identifiers = {
        str(identifier).strip()
        for record in payload.get("exceptions", [])
        if isinstance(record, dict) and record.get("kind") == "advisory"
        for identifier in record.get("advisory_ids", [])
        if isinstance(identifier, str) and identifier.strip()
    }
    return sorted(identifiers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate owned, time-bounded Carlina release exceptions."
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY,
        help="Path to the release exception policy.",
    )
    parser.add_argument(
        "--public-release",
        action="store_true",
        help="Also fail while any exception is marked release-blocking.",
    )
    parser.add_argument(
        "--print-pip-audit-ignores",
        action="store_true",
        help="Print validated advisory IDs as one pip-audit ignore per line.",
    )
    args = parser.parse_args(argv)

    try:
        payload = _load_policy(args.policy)
        errors = validate_policy(
            payload,
            public_release=args.public_release,
        )
    except (TypeError, ValueError) as exc:
        print(f"Release exception policy error: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if args.print_pip_audit_ignores:
        for identifier in pip_audit_ignored_advisories(payload):
            print(identifier)
        return 0
    print(f"Release exceptions are current: {args.policy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
