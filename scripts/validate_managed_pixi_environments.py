from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__:
    from .managed_pixi_validation import (
        ManagedPixiValidationError,
        resolve_pixi_executable,
        validate_managed_environments,
        write_report,
    )
else:
    from managed_pixi_validation import (
        ManagedPixiValidationError,
        resolve_pixi_executable,
        validate_managed_environments,
        write_report,
    )

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT_DIR / "policy" / "managed-pixi-smoke.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize and smoke every independently versioned managed Pixi "
            "environment without touching repository or DataRoot environments."
        )
    )
    parser.add_argument("--repository-root", type=Path, default=ROOT_DIR)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--pixi", help="Pixi executable path or command name.")
    parser.add_argument(
        "--addon",
        action="append",
        default=[],
        help="Validate only this add-on ID; repeat for multiple add-ons.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Per-command timeout; defaults to 1800 seconds.",
    )
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help="Check copied locks without materializing or importing packages.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--report", type=Path, help="Optional JSON report path.")
    args = parser.parse_args(argv)

    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")

    try:
        repository_root = args.repository_root.expanduser().resolve()
        pixi = resolve_pixi_executable(repository_root, args.pixi)
        policy_path = args.policy
        if not policy_path.is_absolute():
            policy_path = repository_root / policy_path
        report = validate_managed_environments(
            repository_root,
            policy_path=policy_path,
            pixi=pixi,
            addon_ids=set(args.addon) or None,
            timeout_seconds=args.timeout_seconds,
            lock_only=args.lock_only,
            quiet=args.json_output,
        )
    except ManagedPixiValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.report:
        write_report(args.report, report)
    if args.json_output:
        print(json.dumps(report, sort_keys=True))
    else:
        mode = "lock checks" if args.lock_only else "materialization/import smokes"
        print(
            f"Managed Pixi validation passed: {report['environment_count']} "
            f"environment(s), {mode}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
