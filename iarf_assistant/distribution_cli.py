"""Argument parsing and rendering for managed distribution commands."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from carlina.core.installation_handoff import write_terminal_event
from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationState,
)
from carlina.core.recovery_policy import (
    RecoveryCoverage,
    RecoveryDecisionSource,
    RecoveryPolicyError,
)
from carlina.product_identity import product_environment_value, release_repository_url
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.backup_service import BackupError
from iarf_assistant.core.managed_install import (
    ManagedCompatibilityError,
    ManagedInstallationLayout,
    ManagedInstallError,
    ManagedReleaseManager,
)
from iarf_assistant.core.release_downloads import ReleaseDownloadError
from iarf_assistant.core.release_manifest import ReleaseManifestError
from iarf_assistant.core.storage_report import (
    build_storage_report,
    format_storage_report,
)


def default_release_index_url(channel: str) -> str:
    configured = product_environment_value(
        f"RELEASE_INDEX_{channel.upper()}_URL",
        default="",
    )
    if configured:
        return configured
    if channel == "preview":
        raise ReleaseManifestError(
            "The preview channel has no mutable default URL. Pass --manifest with "
            "the exact preview release index or configure "
            "CARLINA_RELEASE_INDEX_PREVIEW_URL."
        )
    return (
        f"{release_repository_url(resolve_runtime_composition().profile)}"
        "/releases/latest/download/"
        f"carlina-release-index-{channel}.json"
    )


def add_distribution_parsers(subparsers: Any) -> None:
    install = subparsers.add_parser(
        "install",
        help="Adopt an unpacked, signed managed release during bootstrap.",
    )
    _add_release_source_arguments(install)
    install.add_argument("--artifact", required=True)
    install.add_argument("--adopt-version-dir", required=True)
    install.add_argument(
        "--expected-index-sha256",
        help="Require this exact signed release-index hash before adoption.",
    )
    install.add_argument(
        "--retry-transaction",
        dest="retry_transaction_id",
        help=(
            "Reconcile this retained adoption transaction; requires --version "
            "and --expected-index-sha256."
        ),
    )
    install.add_argument(
        "--resume-bootstrap-claim",
        dest="resume_bootstrap_claim_id",
        help=(
            "Resume this ready native bootstrap claim under the installation leases; "
            "requires --version and --expected-index-sha256."
        ),
    )

    update = subparsers.add_parser(
        "update",
        help="Check or apply a signed side-by-side managed release.",
    )
    _add_release_source_arguments(update)
    update.add_argument("--check", action="store_true")
    update.add_argument(
        "--stop",
        action="store_true",
        help="Stop the managed server before applying the update.",
    )
    update.add_argument(
        "--systemd-unit",
        metavar="UNIT",
        help="Stop and verify this user systemd .service before applying the update.",
    )
    update.add_argument(
        "--acknowledge-recovery-required",
        metavar="TRANSACTION_ID",
        help=(
            "After reviewing a fenced no-recovery failure, allow a new target "
            "to continue from that exact unresolved transaction."
        ),
    )
    _add_recovery_arguments(update)

    rollback = subparsers.add_parser(
        "rollback",
        help="Activate the prior program version, with paired data restore when required.",
    )
    rollback.add_argument("--program-root")
    rollback.add_argument("--restore-data", action="store_true")
    rollback.add_argument("--stop", action="store_true")
    rollback.add_argument(
        "--systemd-unit",
        metavar="UNIT",
        help="Stop and verify this user systemd .service before rolling back.",
    )
    _add_installation_event_arguments(rollback)

    storage = subparsers.add_parser("storage", help="Inspect owned storage.")
    storage_subparsers = storage.add_subparsers(
        dest="storage_command",
        required=True,
    )
    storage_report = storage_subparsers.add_parser(
        "report",
        help="Report disk use by retention category.",
    )
    storage_report.add_argument("--program-root")
    storage_report.add_argument("--json", action="store_true")

    uninstall = subparsers.add_parser(
        "uninstall",
        help="Show program/data removal boundaries without deleting anything.",
    )
    uninstall.add_argument("--program-root")
    uninstall.add_argument("--json", action="store_true")


def _add_release_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", help="Signed release-index path or HTTPS URL.")
    parser.add_argument("--channel", choices=("stable", "preview"), default="stable")
    parser.add_argument("--version", dest="requested_version")
    parser.add_argument("--program-root")
    parser.add_argument("--trust-store")
    parser.add_argument(
        "--allow-unsigned-local",
        action="store_true",
        help="Permit an unsigned local-file index for development tests only.",
    )
    _add_installation_event_arguments(parser)


def _add_installation_event_arguments(parser: argparse.ArgumentParser) -> None:
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument(
        "--events-jsonl",
        action="store_true",
        help="Stream versioned installation events to stdout; diagnostics use stderr.",
    )
    parser.add_argument(
        "--operation-id",
        help="Lowercase 32-hex event operation ID; requires --events-jsonl.",
    )
    parser.add_argument(
        "--cancel-file",
        help="Request cooperative cancellation when this file exists; requires --events-jsonl.",
    )
    parser.add_argument(
        "--event-sequence-start",
        type=int,
        help="Continue a bootstrap event sequence; requires --events-jsonl.",
    )
    parser.add_argument(
        "--event-terminal-file",
        help="Write the terminal event exclusively to this handoff file instead of stdout.",
    )


def _add_recovery_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--recovery-coverage",
        choices=tuple(RecoveryCoverage),
        default=RecoveryCoverage.AUTOMATIC,
        help=(
            "Select mutable-data recovery material. Impact remains opaque for "
            "legacy release indexes regardless of this choice."
        ),
    )
    parser.add_argument(
        "--recovery-reason",
        help="Short audit reason for an explicit recovery selection.",
    )
    parser.add_argument(
        "--recovery-policy-id",
        help="Stable audit policy ID authorizing an explicit recovery selection.",
    )
    parser.add_argument(
        "--recovery-actor",
        help="Actor recorded for an explicit selection (defaults to the local user).",
    )
    parser.add_argument(
        "--recovery-unattended",
        action="store_true",
        help=(
            "Record an explicit selection as unattended; a policy ID is required "
            "when it is narrower than the automatic default."
        ),
    )


def _json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _console_safe_text(value: object, *, encoding: str | None = None) -> str:
    """Render errors even when the Windows console uses a legacy code page."""

    selected = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    return str(value).encode(selected, errors="backslashreplace").decode(selected)


def _source(args: argparse.Namespace) -> str:
    return str(args.manifest or default_release_index_url(args.channel))


def _manager(args: argparse.Namespace, paths: AppPaths) -> ManagedReleaseManager:
    return ManagedReleaseManager(
        paths,
        program_root=args.program_root,
        trust_store=getattr(args, "trust_store", None),
    )


def validate_installation_event_arguments(args: argparse.Namespace) -> None:
    events = bool(getattr(args, "events_jsonl", False))
    if not events and (
        getattr(args, "operation_id", None) is not None
        or getattr(args, "cancel_file", None) is not None
        or getattr(args, "event_sequence_start", None) is not None
        or getattr(args, "event_terminal_file", None) is not None
    ):
        raise RecoveryPolicyError("Installation event controls require --events-jsonl.")
    if events and getattr(args, "check", False):
        raise RecoveryPolicyError("--events-jsonl cannot be combined with --check.")
    if events and (
        getattr(args, "stop", False) or getattr(args, "systemd_unit", None) is not None
    ):
        raise RecoveryPolicyError(
            "Event mode requires an already stopped runtime; omit --stop and --systemd-unit."
        )
    sequence = getattr(args, "event_sequence_start", None)
    if sequence is not None and (type(sequence) is not int or sequence < 0):
        raise RecoveryPolicyError("Installation event sequence must be non-negative.")
    destination = getattr(args, "event_terminal_file", None)
    if destination is not None:
        terminal_path = Path(destination).expanduser().absolute()
        try:
            terminal_path.lstat()
        except FileNotFoundError:
            if not terminal_path.parent.is_dir():
                raise RecoveryPolicyError(
                    "Installation event handoff needs an existing parent directory."
                ) from None
        except (OSError, ValueError):
            raise RecoveryPolicyError(
                "Installation event handoff destination cannot be inspected."
            ) from None
        else:
            raise RecoveryPolicyError(
                "Installation event handoff destination must be absent."
            )


def validate_installation_retry_arguments(args: argparse.Namespace) -> None:
    """Reject malformed adoption retry controls before runtime access."""

    if args.command != "install":
        return
    expected = getattr(args, "expected_index_sha256", None)
    transaction = getattr(args, "retry_transaction_id", None)
    claim = getattr(args, "resume_bootstrap_claim_id", None)
    if expected is not None and (
        type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None
    ):
        raise RecoveryPolicyError(
            "Expected release-index hash must be 64 lowercase hexadecimal characters."
        )
    if transaction is not None:
        if (
            type(transaction) is not str
            or re.fullmatch(r"[0-9a-f]{32}", transaction) is None
        ):
            raise RecoveryPolicyError(
                "Retry transaction ID must be 32 lowercase hexadecimal characters."
            )
        if expected is None or not getattr(args, "requested_version", None):
            raise RecoveryPolicyError(
                "Adoption retry requires --version and --expected-index-sha256."
            )
    if claim is not None:
        if type(claim) is not str or re.fullmatch(r"[0-9a-f]{32}", claim) is None:
            raise RecoveryPolicyError(
                "Native bootstrap resume claim must be 32 lowercase hexadecimal characters."
            )
        if transaction is not None:
            raise RecoveryPolicyError(
                "Native bootstrap resume and explicit adoption retry cannot be combined."
            )
        if expected is None or not getattr(args, "requested_version", None):
            raise RecoveryPolicyError(
                "Native bootstrap resume requires --version and --expected-index-sha256."
            )


def validate_installation_arguments(args: argparse.Namespace) -> None:
    """Validate lifecycle controls before resolving an application's runtime."""

    validate_installation_event_arguments(args)
    validate_installation_retry_arguments(args)


def _execute_installation[T](
    args: argparse.Namespace,
    manager: ManagedReleaseManager,
    action: Callable[[dict[str, Any]], T],
) -> T:
    if not getattr(args, "events_jsonl", False):
        return action({})
    output = sys.stdout
    destination = getattr(args, "event_terminal_file", None)
    terminal_path = (
        Path(destination).expanduser().absolute() if destination is not None else None
    )
    pending_terminal: InstallationEvent | None = None
    sequence_start = getattr(args, "event_sequence_start", None)
    cancel_path = (
        Path(args.cancel_file).expanduser().absolute()
        if args.cancel_file is not None
        else None
    )

    def emit(event: InstallationEvent) -> None:
        nonlocal pending_terminal
        if terminal_path is not None and event.state is not InstallationState.RUNNING:
            pending_terminal = event
            return
        output.write(event.json_line())
        output.flush()

    def cancellation_requested() -> bool:
        if cancel_path is None:
            return False
        try:
            status = cancel_path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(status.st_mode):
            raise RecoveryPolicyError("Cancellation control must be a regular file.")
        return True

    try:
        lifecycle = InstallationLifecycle(
            args.command,
            manager.application_profile,
            operation_id=args.operation_id,
            sequence_start=0 if sequence_start is None else sequence_start,
            event_sink=emit,
            cancel_requested=cancellation_requested
            if cancel_path is not None
            else None,
        )
    except ValueError, TypeError:
        raise RecoveryPolicyError("Installation event options are invalid.") from None
    try:
        try:
            with redirect_stdout(sys.stderr):
                return action({"lifecycle": lifecycle})
        except InstallationCancelled:
            lifecycle.cancel()
            raise
        except BaseException:
            # Manager failures already carry a terminal record. This also covers
            # source/argument errors raised before a manager operation is entered.
            lifecycle.fail(InstallationErrorCode.INVALID_SOURCE)
            raise
    finally:
        if terminal_path is not None and pending_terminal is not None:
            write_terminal_event(terminal_path, pending_terminal)


def _print_distribution_error(
    args: argparse.Namespace, message: str, *, event_message: str
) -> None:
    events = bool(getattr(args, "events_jsonl", False))
    print(
        _console_safe_text(event_message if events else message),
        file=sys.stderr if events else sys.stdout,
    )


def _recovery_options(args: argparse.Namespace) -> dict[str, Any]:
    requested = RecoveryCoverage(args.recovery_coverage)
    explicit = requested is not RecoveryCoverage.AUTOMATIC
    override_fields = (
        args.recovery_reason,
        args.recovery_policy_id,
        args.recovery_actor,
        args.recovery_unattended,
    )
    if not explicit and any(override_fields):
        raise RecoveryPolicyError(
            "Recovery override fields require an explicit --recovery-coverage."
        )
    if not explicit:
        return {
            "recovery_coverage": requested,
            "recovery_decision_source": RecoveryDecisionSource.AUTOMATIC,
        }
    return {
        "recovery_coverage": requested,
        "recovery_decision_source": (
            RecoveryDecisionSource.UNATTENDED
            if args.recovery_unattended
            else RecoveryDecisionSource.INTERACTIVE
        ),
        "recovery_actor": args.recovery_actor or getpass.getuser(),
        "recovery_reason": args.recovery_reason,
        "recovery_policy_id": args.recovery_policy_id,
    }


def handle_distribution_command(
    args: argparse.Namespace,
    paths: AppPaths,
) -> int | None:
    """Handle a distribution command, or return ``None`` for another router."""

    try:
        if args.command in {"install", "update", "rollback"}:
            validate_installation_arguments(args)
        if args.command == "install":
            retry_options = {
                name: getattr(args, name)
                for name in (
                    "expected_index_sha256",
                    "retry_transaction_id",
                    "resume_bootstrap_claim_id",
                )
                if getattr(args, name, None) is not None
            }
            manager = _manager(args, paths)
            result = _execute_installation(
                args,
                manager,
                lambda options: manager.adopt_installed_release(
                    _source(args),
                    args.artifact,
                    args.adopt_version_dir,
                    channel=args.channel,
                    requested_version=args.requested_version,
                    allow_unsigned_local=args.allow_unsigned_local,
                    **retry_options,
                    **options,
                ),
            )
            if args.json:
                _json(result.to_dict())
            elif not getattr(args, "events_jsonl", False):
                print(f"Installed and activated Carlina {result.active_version}.")
                print(
                    "Initialize data and create the first administrator with: "
                    f"{_launcher_hint(result.active_version, args.program_root)} init"
                )
            return 0
        if args.command == "update":
            manager = _manager(args, paths)
            recovery_options = _recovery_options(args)
            if args.check:
                if args.acknowledge_recovery_required is not None:
                    raise RecoveryPolicyError(
                        "--acknowledge-recovery-required applies only to an update, "
                        "not --check."
                    )
                result = manager.check(
                    _source(args),
                    channel=args.channel,
                    requested_version=args.requested_version,
                    allow_unsigned_local=args.allow_unsigned_local,
                    **recovery_options,
                )
                if args.json:
                    _json(result.to_dict())
                else:
                    _print_check(result)
                if result.compatibility and not result.compatibility.compatible:
                    return 2
                return 0
            result = _execute_installation(
                args,
                manager,
                lambda options: manager.update(
                    _source(args),
                    channel=args.channel,
                    requested_version=args.requested_version,
                    allow_unsigned_local=args.allow_unsigned_local,
                    acknowledge_recovery_required=(args.acknowledge_recovery_required),
                    **recovery_options,
                    **options,
                ),
            )
            if args.json:
                _json(result.to_dict())
            elif not getattr(args, "events_jsonl", False):
                print(
                    f"Activated Carlina {result.active_version}"
                    + (
                        f" (previously {result.previous_version})."
                        if result.previous_version
                        else "."
                    )
                )
                if result.backup_path:
                    print(f"Paired rollback backup: {result.backup_path}")
                if result.database_checkpoint_path:
                    print(
                        f"Paired database checkpoint: {result.database_checkpoint_path}"
                    )
                _print_recovery_plan(result.recovery_plan)
            return 0
        if args.command == "rollback":
            manager = _manager(args, paths)
            result = _execute_installation(
                args,
                manager,
                lambda options: manager.rollback(
                    restore_data=args.restore_data,
                    **options,
                ),
            )
            if args.json:
                _json(result.to_dict())
            elif not getattr(args, "events_jsonl", False):
                print(
                    f"Rolled back Carlina {result.previous_version} to "
                    f"{result.active_version}."
                )
                print(
                    "Paired data snapshot restored."
                    if result.data_restored
                    else "Program version changed; data was preserved."
                )
            return 0
        if args.command == "storage" and args.storage_command == "report":
            layout = ManagedInstallationLayout.resolve(args.program_root)
            report = build_storage_report(
                paths,
                managed_recovery_root=layout.recovery_dir,
            )
            if args.json:
                _json(report.to_dict())
            else:
                print(format_storage_report(report))
            return 0
        if args.command == "uninstall":
            layout = ManagedInstallationLayout.resolve(args.program_root)
            launcher_path = (
                str(layout.root)
                if os.name == "nt"
                else str(os.path.expanduser(f"~/.local/bin/{_launcher_name()}"))
            )
            payload = {
                "program_root": str(layout.root),
                "active_version": layout.current_version(),
                "data_root": str(paths.data_root),
                "config_dir": str(paths.config_dir),
                "launcher_path": launcher_path,
                "program_removal": (
                    "Remove only program_root after stopping Carlina. This leaves all "
                    "data, secrets, add-ons, and backups intact."
                ),
                "data_removal": (
                    "Export a verified backup first, then remove data_root and the "
                    "separate config directory only as a distinct, explicit action."
                ),
                "automatic_deletion_performed": False,
            }
            if args.json:
                _json(payload)
            else:
                print("Carlina uninstall plan (no files were changed)")
                print(f"Program: {payload['program_root']}")
                print(f"Data:    {payload['data_root']}")
                print(f"Config:  {payload['config_dir']}")
                print(f"Launcher/PATH entry: {payload['launcher_path']}")
                print(
                    "\nProgram removal preserves data. Export with 'carlina backup' first."
                )
                print(
                    "Data deletion is intentionally separate and is not performed by "
                    "this command."
                )
            return 0
    except InstallationCancelled:
        _print_distribution_error(
            args,
            "Installation cancelled at a safe boundary.",
            event_message="Installation cancelled at a safe boundary.",
        )
        return 130
    except (ManagedCompatibilityError, ReleaseManifestError) as exc:
        _print_distribution_error(
            args,
            f"Compatibility check failed: {exc}",
            event_message="Compatibility check failed.",
        )
        return 2
    except (
        ManagedInstallError,
        RecoveryPolicyError,
        ReleaseDownloadError,
        BackupError,
        OSError,
    ) as exc:
        _print_distribution_error(
            args,
            f"Managed release operation failed: {exc}",
            event_message="Managed release operation failed.",
        )
        return 1
    return None


def _print_check(result: Any) -> None:
    if result.available_version is None:
        print(
            f"Carlina {result.current_version or 'unknown'} is current on the "
            f"{result.channel} channel."
        )
        return
    print(
        f"Carlina {result.available_version} is available for {result.platform} "
        f"(current: {result.current_version or 'not managed'})."
    )
    if result.signer_key_id:
        print(f"Authenticated by release key: {result.signer_key_id}")
    else:
        print("Warning: local development index is unsigned.")
    compatibility = result.compatibility
    if compatibility is None:
        return
    if compatibility.compatible:
        print("Compatibility preflight passed.")
    else:
        print("Compatibility preflight blocked the release:")
        for issue in compatibility.issues:
            if issue.blocking:
                print(f"- {issue.code}: {issue.message}")
    _print_recovery_plan(compatibility.recovery_plan)


def _print_recovery_plan(plan: Any) -> None:
    print(
        "Recovery: impact "
        f"{plan.effective_impact}; default {plan.default_coverage}; "
        f"selected {plan.selected_coverage} ({plan.mechanism})."
    )
    if plan.uncovered_domains:
        print(
            "Uncovered effect domains: "
            + ", ".join(str(domain) for domain in plan.uncovered_domains)
        )
    if plan.recent_recovery_anchor:
        print(f"Receipt-bound recent anchor: {plan.recent_recovery_anchor}")


def _launcher_hint(version: str, program_root: str | None) -> str:
    del version
    layout = ManagedInstallationLayout.resolve(program_root)
    name = _launcher_name() + (".cmd" if os.name == "nt" else "")
    return str(layout.root / name)


def _launcher_name() -> str:
    profile = resolve_runtime_composition().profile
    return "carlina-documents" if profile is RuntimeProfile.DOCUMENTS else "carlina"


__all__ = [
    "add_distribution_parsers",
    "default_release_index_url",
    "handle_distribution_command",
]
