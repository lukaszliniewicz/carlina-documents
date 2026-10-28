#!/usr/bin/env python3
"""Render no-Python bootstrap installers from a verified signed release index."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carlina.core.bootstrap_ownership import (  # noqa: E402
    BOOTSTRAP_BINDING_NAME,
    BOOTSTRAP_CLAIM_HEADER,
    BOOTSTRAP_CLAIM_NAME,
    BootstrapIdentity,
)
from carlina.core.native_download_scripts import (
    render_powershell_download_helpers,
    render_shell_download_helpers,
)
from carlina.core.native_preparation_shell import render_shell_preparation_helpers
from carlina.core.native_preparation_windows import (
    render_powershell_preparation_helpers,
)
from carlina.runtime_profiles import RuntimeProfile  # noqa: E402
from iarf_assistant.core.release_manifest import (  # noqa: E402
    PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND,
    load_trusted_keys,
    parse_release_index,
    verify_release_index,
)


class InstallerRenderError(RuntimeError):
    pass


def _validated_application_profile(value: str) -> str:
    try:
        return RuntimeProfile(value).value
    except TypeError, ValueError:
        raise InstallerRenderError(
            "Installer application profile is unsupported."
        ) from None


def _application_profile_probe() -> str:
    """Return the isolated stdlib command that checks an unpacked profile."""

    return textwrap.dedent(
        """\
        import json
        import os
        import re
        import stat
        import sys

        def _unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        def _allow_missing(expected):
            if expected != "workbench":
                raise ValueError

        def _inside(root, path):
            try:
                common = os.path.commonpath((root, path))
            except ValueError:
                return False
            return os.path.normcase(common) == os.path.normcase(root)

        def _run():
            if len(sys.argv) != 3:
                raise ValueError
            expected = sys.argv[2]
            if expected not in ("workbench", "documents"):
                raise ValueError
            environment_root = os.path.abspath(sys.argv[1])
            root_status = os.lstat(environment_root)
            if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
                raise ValueError
            real_root = os.path.realpath(environment_root)
            if os.name == "nt":
                components = ("Lib", "site-packages", "carlina", "distribution.json")
            else:
                components = ("lib", "python3.14", "site-packages", "carlina", "distribution.json")
            current = environment_root
            for component in components[:-1]:
                current = os.path.join(current, component)
                try:
                    status = os.lstat(current).st_mode
                except FileNotFoundError:
                    _allow_missing(expected)
                    return
                if stat.S_ISLNK(status) or not stat.S_ISDIR(status):
                    raise ValueError
            descriptor = os.path.join(environment_root, *components)
            try:
                status = os.lstat(descriptor).st_mode
            except FileNotFoundError:
                _allow_missing(expected)
                return
            if stat.S_ISLNK(status) or not stat.S_ISREG(status):
                raise ValueError
            real_descriptor = os.path.realpath(descriptor)
            if not _inside(real_root, real_descriptor):
                raise ValueError
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor_fd = os.open(descriptor, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor_fd).st_mode):
                    raise ValueError
                with os.fdopen(descriptor_fd, "rb", closefd=False) as stream:
                    raw = stream.read(4097)
            finally:
                os.close(descriptor_fd)
            if len(raw) > 4096:
                raise ValueError
            distribution = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_object
            )
            required_keys = {
                "format",
                "schema_version",
                "application_profile",
                "source_revision",
                "source_date_epoch",
                "addon_ids",
            }
            if type(distribution) is not dict or set(distribution) != required_keys:
                raise ValueError
            if distribution["format"] != "carlina.application-distribution":
                raise ValueError
            if type(distribution["schema_version"]) is not int or distribution["schema_version"] != 1:
                raise ValueError
            if distribution["application_profile"] != "documents":
                raise ValueError
            revision = distribution["source_revision"]
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise ValueError
            epoch = distribution["source_date_epoch"]
            if type(epoch) is not int or epoch <= 0:
                raise ValueError
            if distribution["addon_ids"] != ["doc_processor", "sentence_segmenter", "translator"]:
                raise ValueError
            if expected != "documents":
                raise ValueError

        try:
            _run()
        except Exception:
            sys.stderr.write("Application profile validation failed.\\n")
            raise SystemExit(1)
        raise SystemExit(0)
        """
    ).strip()


def _installation_handoff_probe() -> str:
    return """import sys
from pathlib import Path
try:
    from carlina.core.installation_handoff import (
        HANDOFF_PROTOCOL_VERSION, read_terminal_event, write_terminal_event,
    )
    if type(HANDOFF_PROTOCOL_VERSION) is not int or HANDOFF_PROTOCOL_VERSION != 1:
        raise ValueError()
    if sys.argv[1] == 'read':
        event = read_terminal_event(
            Path(sys.argv[2]), operation='install',
            operation_id=sys.argv[3], application_profile=sys.argv[4],
            sequence_start=int(sys.argv[5]), exit_code=int(sys.argv[6]),
        )
        sys.stdout.write(event.json_line())
    elif sys.argv[1] != 'probe':
        raise ValueError()
except (ImportError, OSError, ValueError, TypeError, IndexError):
    print('Installation event handoff could not be verified.', file=sys.stderr)
    raise SystemExit(1)
"""


def _bootstrap_ownership_probe() -> str:
    return """import base64, json, sys
from pathlib import Path
try:
    from carlina.core.bootstrap_ownership import (
        BOOTSTRAP_CLAIM_PROTOCOL_VERSION, BootstrapIdentity,
        bind_bootstrap_generation, validate_bootstrap_generation,
    )
    from iarf_assistant.core.app_paths import load_runtime_environment
    if type(BOOTSTRAP_CLAIM_PROTOCOL_VERSION) is not int or BOOTSTRAP_CLAIM_PROTOCOL_VERSION != 1:
        raise ValueError()
    identity = BootstrapIdentity(**json.loads(base64.b64decode(sys.argv[4], validate=True)))
    program_root = Path(sys.argv[1])
    paths = load_runtime_environment(program_root, profile=identity.application_profile)
    mode = sys.argv[5]
    if mode in ('prepared-bind', 'prepared-resume'):
        from carlina.core.bootstrap_ownership import (
            BOOTSTRAP_PREPARATION_PROTOCOL_VERSION, validate_bootstrap_preparation,
        )
        if type(BOOTSTRAP_PREPARATION_PROTOCOL_VERSION) is not int or BOOTSTRAP_PREPARATION_PROTOCOL_VERSION != 1:
            raise ValueError()
        validate_bootstrap_preparation(
            Path(sys.argv[2]), identity, claim_id=sys.argv[3], program_root=program_root,
        )
        mode = mode.removeprefix('prepared-')
    if mode == 'resume':
        from carlina.core.bootstrap_ownership import BOOTSTRAP_RESUME_PROTOCOL_VERSION
        if type(BOOTSTRAP_RESUME_PROTOCOL_VERSION) is not int or BOOTSTRAP_RESUME_PROTOCOL_VERSION != 1:
            raise ValueError()
        claim_id = validate_bootstrap_generation(
            Path(sys.argv[2]), identity, claim_id=sys.argv[3],
            program_root=program_root, data_root=paths.data_root,
        )
        if claim_id != sys.argv[3]:
            raise ValueError()
    elif mode == 'bind':
        claim_id = bind_bootstrap_generation(
            Path(sys.argv[2]), identity, claim_id=sys.argv[3],
            program_root=program_root, data_root=paths.data_root,
        )
    else:
        raise ValueError()
    sys.stdout.write('carlina.bootstrap-ready/1 ' + claim_id + ' ' + identity.digest)
except (ImportError, OSError, ValueError, TypeError, IndexError):
    print('Bootstrap generation ownership could not be verified.', file=sys.stderr)
    raise SystemExit(1)
"""


def _shell_bootstrap_ownership_helpers() -> str:
    return r"""
release_bootstrap_lease() {
  if [ "$BOOTSTRAP_LEASE_OWNED" -eq 1 ]; then
    exec 9>&-
    BOOTSTRAP_LEASE_OWNED=0
  fi
}
owned_bootstrap_generation() {
  [ -d "$VERSION_DIR" ] && [ ! -L "$VERSION_DIR" ] || return 1
  [ -d "$PROGRAM_ROOT/versions" ] && [ ! -L "$PROGRAM_ROOT/versions" ] || return 1
  [ -f "$BOOTSTRAP_CLAIM_PATH" ] && [ ! -L "$BOOTSTRAP_CLAIM_PATH" ] || return 1
  CLAIM_SIZE=$(wc -c < "$BOOTSTRAP_CLAIM_PATH" | tr -d '[:space:]') || return 1
  [ "$CLAIM_SIZE" = "$BOOTSTRAP_CLAIM_SIZE" ] || return 1
  CLAIM_EXPECTED=$(printf '%s\n%s\n%s\n' "$BOOTSTRAP_CLAIM_HEADER" "$BOOTSTRAP_CLAIM_ID" "$BOOTSTRAP_IDENTITY" | hash_file -) || return 1
  CLAIM_ACTUAL=$(hash_file "$BOOTSTRAP_CLAIM_PATH") || return 1
  [ "$CLAIM_EXPECTED" = "$CLAIM_ACTUAL" ]
}
read_ready_bootstrap_generation() {
  [ -d "$VERSION_DIR" ] && [ ! -L "$VERSION_DIR" ] || return 1
  [ -d "$PROGRAM_ROOT/versions" ] && [ ! -L "$PROGRAM_ROOT/versions" ] || return 1
  [ -f "$BOOTSTRAP_CLAIM_PATH" ] && [ ! -L "$BOOTSTRAP_CLAIM_PATH" ] || return 1
  CLAIM_SIZE=$(wc -c < "$BOOTSTRAP_CLAIM_PATH" | tr -d '[:space:]') || return 1
  [ "$CLAIM_SIZE" = "$BOOTSTRAP_CLAIM_SIZE" ] || return 1
  { IFS= read -r CLAIM_HEADER && IFS= read -r BOOTSTRAP_CLAIM_ID && IFS= read -r CLAIM_IDENTITY; } < "$BOOTSTRAP_CLAIM_PATH" || return 1
  [ "$CLAIM_HEADER" = "$BOOTSTRAP_CLAIM_HEADER" ] && [ "$CLAIM_IDENTITY" = "$BOOTSTRAP_IDENTITY" ] || return 1
  case "$BOOTSTRAP_CLAIM_ID" in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#BOOTSTRAP_CLAIM_ID}" -eq 32 ] || return 1
  owned_bootstrap_generation || return 1
  [ -f "$BOOTSTRAP_BINDING_PATH" ] && [ ! -L "$BOOTSTRAP_BINDING_PATH" ] || return 1
  BINDING_SIZE=$(wc -c < "$BOOTSTRAP_BINDING_PATH" | tr -d '[:space:]') || return 1
  [ "$BINDING_SIZE" -gt 0 ] && [ "$BINDING_SIZE" -le 65536 ]
}
create_bootstrap_generation() {
  if [ -L "$PROGRAM_ROOT/versions" ]; then echo 'Bootstrap versions path is unsafe.' >&2; return 1; fi
  mkdir -p "$PROGRAM_ROOT/versions" || return 1
  mkdir "$VERSION_DIR" || return 1
  CREATED_VERSION_DIR=1
  BOOTSTRAP_CLAIM_ID=$(od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]') || return 1
  case "$BOOTSTRAP_CLAIM_ID" in ''|*[!0-9a-f]*) echo 'Bootstrap claim identity is invalid.' >&2; return 1 ;; esac
  if [ "${#BOOTSTRAP_CLAIM_ID}" -ne 32 ]; then echo 'Bootstrap claim identity is invalid.' >&2; return 1; fi
  (umask 077; set -C; printf '%s\n%s\n%s\n' "$BOOTSTRAP_CLAIM_HEADER" "$BOOTSTRAP_CLAIM_ID" "$BOOTSTRAP_IDENTITY" > "$BOOTSTRAP_CLAIM_PATH")
}
"""


def _powershell_bootstrap_ownership_helpers() -> str:
    return r"""
function Close-BootstrapLease {
    if ($null -ne $script:bootstrapLease) {
        $script:bootstrapLease.Dispose()
        $script:bootstrapLease = $null
    }
}
function Test-OwnedBootstrapGeneration([string]$ClaimId = $bootstrapClaimId) {
    try {
        foreach ($path in @($versionsRoot, $versionDir, $bootstrapClaimPath)) {
            $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        }
        $item = Get-Item -LiteralPath $bootstrapClaimPath -Force -ErrorAction Stop
        if ($item.PSIsContainer -or $item.Length -ne $bootstrapClaimSize) { return $false }
        $expected = [Text.Encoding]::ASCII.GetBytes("$bootstrapClaimHeader`n$ClaimId`n$bootstrapIdentity`n")
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { $expectedHash = [BitConverter]::ToString($algorithm.ComputeHash($expected)).Replace('-', '').ToLowerInvariant() }
        finally { $algorithm.Dispose() }
        $actual = (Get-FileHash -LiteralPath $bootstrapClaimPath -Algorithm SHA256).Hash.ToLowerInvariant()
        return $actual -ceq $expectedHash
    } catch { return $false }
}
function Read-MatchingBootstrapClaim {
    foreach ($path in @($versionsRoot, $versionDir)) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Retained bootstrap generation path is unsafe.'
        }
    }
    $item = Get-Item -LiteralPath $bootstrapClaimPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -ne $bootstrapClaimSize) {
        throw 'Retained bootstrap claim is invalid.'
    }
    $frame = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($bootstrapClaimPath)).Split("`n")
    if ($frame.Length -ne 4 -or $frame[1] -cnotmatch '^[0-9a-f]{32}$' -or -not (Test-OwnedBootstrapGeneration -ClaimId $frame[1])) {
        throw 'Retained bootstrap claim does not match this installer.'
    }
    return $frame[1]
}
function Read-ReadyBootstrapClaim {
    $claim = Read-MatchingBootstrapClaim
    $binding = Get-Item -LiteralPath $bootstrapBindingPath -Force -ErrorAction Stop
    if ($binding.PSIsContainer -or ($binding.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $binding.Length -lt 1 -or $binding.Length -gt 65536) {
        throw 'Retained bootstrap generation is not ready.'
    }
    return $claim
}
function New-BootstrapGeneration {
    New-Item -ItemType Directory -Path $versionsRoot -Force | Out-Null
    if (((Get-Item -LiteralPath $versionsRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Bootstrap versions path is unsafe.'
    }
    New-Item -ItemType Directory -Path $versionDir | Out-Null
    $script:createdVersionDir = $true
    $script:bootstrapClaimId = [guid]::NewGuid().ToString('N')
    $claimStream = [IO.FileStream]::new($bootstrapClaimPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $claimBytes = [Text.Encoding]::ASCII.GetBytes("$bootstrapClaimHeader`n$bootstrapClaimId`n$bootstrapIdentity`n")
        $claimStream.Write($claimBytes, 0, $claimBytes.Length)
        $claimStream.Flush($true)
    } finally { $claimStream.Dispose() }
}
function Remove-OwnedBootstrapScratch {
    if ($script:bootstrapNativeActive) { return $false }
    if ($createdVersionDir -and -not $registered -and -not $adoptionStarted -and (Test-Path -LiteralPath $versionDir)) {
        if ($null -eq $script:bootstrapLease -or -not (Test-OwnedBootstrapGeneration)) { return $false }
        try { Remove-Item -LiteralPath $versionDir -Recurse -Force -ErrorAction Stop }
        catch { return $false }
    }
    if ($work -and (Test-Path -LiteralPath $work)) {
        try { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction Stop }
        catch { return $false }
    }
    return $true
}
"""


def _shell_event_helpers(application_profile: str) -> str:
    return r"""
emit_installation_event() {
  if [ "$EVENTS_JSONL" -ne 1 ]; then return; fi
  EVENT_SEQUENCE=$((EVENT_SEQUENCE + 1))
  if [ "$2" = null ]; then EVENT_CODE=null; else EVENT_CODE="\"$2\""; fi
  printf '{"format":"carlina.installation-event","schema_version":1,"operation_id":"%s","sequence":%s,"operation":"install","application_profile":"__PROFILE__","phase":"%s","state":"%s","bytes_completed":%s,"bytes_total":%s,"error_code":%s,"retryable":%s,"cancellation_safe":%s}\n' \
    "$EVENT_ID" "$EVENT_SEQUENCE" "$EVENT_PHASE" "$1" "${5:-null}" "${6:-null}" "$EVENT_CODE" "$3" "$4" || :
}
installation_checkpoint() {
  EVENT_PHASE=$1
  emit_installation_event running null false "$2"
  if [ "$2" != true ]; then return; fi
  check_installation_cancellation
}
finish_installation() {
  INSTALL_EXIT=$?
  trap - EXIT HUP INT TERM
  stop_bootstrap_download
  if ! cleanup; then
    release_bootstrap_lease
    echo 'Installation scratch cleanup failed; inspect the retained state.' >&2
    exit 1
  fi
  release_bootstrap_lease
  if [ "$EVENTS_JSONL" -eq 1 ] && [ "$ADOPTION_STARTED" -eq 0 ] && [ "$EVENT_STREAM_OWNED" -eq 1 ]; then
    if [ "$EVENT_CANCELLED" -eq 1 ]; then
      emit_installation_event cancelled cancelled true true
    else
      emit_installation_event failed "$EVENT_ERROR" "$EVENT_RETRYABLE" true
    fi
  fi
  exit "$INSTALL_EXIT"
}
""".replace("__PROFILE__", application_profile)


def _shell_download_helpers() -> str:
    """Render outer-installer adapters and the shared event transport."""

    adapters = r"""
check_installation_cancellation() {
  if [ "$EVENTS_JSONL" -ne 1 ] || [ -z "$CANCEL_FILE" ]; then return; fi
  CANCEL_PARENT=${CANCEL_FILE%/*}
  if [ -z "$CANCEL_PARENT" ]; then CANCEL_PARENT=/; fi
  if [ ! -d "$CANCEL_PARENT" ] || [ ! -x "$CANCEL_PARENT" ]; then
    EVENT_ERROR=operation_failed
    EVENT_RETRYABLE=false
    echo 'Cancellation control needs an accessible existing parent directory.' >&2
    exit 1
  fi
  if [ -e "$CANCEL_FILE" ] || [ -L "$CANCEL_FILE" ]; then
    if [ ! -f "$CANCEL_FILE" ] || [ -L "$CANCEL_FILE" ]; then
      EVENT_ERROR=operation_failed
      EVENT_RETRYABLE=false
      echo 'Cancellation control must be a regular file.' >&2
      exit 1
    fi
    EVENT_CANCELLED=1
    exit 130
  fi
}
check_download_cancellation() {
  check_installation_cancellation
}
report_download_progress() {
  EVENT_PHASE=download
  emit_installation_event running null false true "$1" "$2"
}
mark_download_size_failure() {
  EVENT_ERROR=verification_failed
  EVENT_RETRYABLE=false
}
download_https() {
  if [ "$EVENTS_JSONL" -eq 1 ]; then
    download_https_event "$@"
    return $?
  fi
  DOWNLOAD_URL=$1
  DOWNLOAD_PATH=$2
  curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --tlsv1.2 \
    -o "$DOWNLOAD_PATH" "$DOWNLOAD_URL"
  return $?
}
"""
    return adapters + render_shell_download_helpers()


def _powershell_event_helpers(application_profile: str) -> str:
    return r"""
function Write-InstallationDiagnostic([string]$Message) {
    try { [Console]::Error.WriteLine($Message) } catch {}
}
function Send-InstallationEvent(
    [string]$State, [object]$Code, [bool]$Retryable, [bool]$Safe,
    [object]$Completed = $null, [object]$Total = $null
) {
    if (-not $EventsJsonl) { return }
    $script:eventSequence += 1
    $record = [ordered]@{
        format = 'carlina.installation-event'; schema_version = 1
        operation_id = $script:eventOperationId; sequence = $script:eventSequence
        operation = 'install'; application_profile = '__PROFILE__'
        phase = $script:eventPhase; state = $State
        bytes_completed = $Completed; bytes_total = $Total
        error_code = $Code; retryable = $Retryable; cancellation_safe = $Safe
    }
    try { [Console]::Out.WriteLine(($record | ConvertTo-Json -Compress)) } catch {}
}
function Invoke-InstallationCancellationCheck([bool]$Safe) {
    if (-not $EventsJsonl -or -not $Safe -or -not $CancelFile) { return }
    try {
        $parent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($CancelFile)) -Force -ErrorAction Stop
        if (-not $parent.PSIsContainer) { throw 'Invalid cancellation parent.' }
    } catch {
        $script:eventError = 'operation_failed'; $script:eventRetryable = $false
        throw 'Cancellation control needs an accessible existing parent directory.'
    }
    try {
        $control = Get-Item -LiteralPath $CancelFile -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return
    } catch {
        $script:eventError = 'operation_failed'; $script:eventRetryable = $false
        throw 'Cancellation control could not be inspected.'
    }
    if ($control.PSIsContainer -or ($control.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        $script:eventError = 'operation_failed'; $script:eventRetryable = $false
        throw 'Cancellation control must be a regular file.'
    }
    $script:eventCancelled = $true
    throw [OperationCanceledException]::new('Installation cancellation requested.')
}
function Invoke-InstallationCheckpoint([string]$Phase, [bool]$Safe) {
    $script:eventPhase = $Phase
    Send-InstallationEvent 'running' $null $false $Safe
    Invoke-InstallationCancellationCheck $Safe
}
""".replace("__PROFILE__", application_profile)


def _powershell_download_helpers() -> str:
    """Render outer event adapters and the shared HTTP transport."""

    adapters = r"""
function Invoke-DownloadCancellationCheck {
    Invoke-InstallationCancellationCheck $true
}
function Send-DownloadProgress([long]$Completed, [long]$Total) {
    $script:eventPhase = 'download'
    Send-InstallationEvent 'running' $null $false $true $Completed $Total
}
function Set-DownloadSizeFailure {
    $script:eventError = 'verification_failed'
    $script:eventRetryable = $false
}
"""
    return adapters + render_powershell_download_helpers()


def _shell_publisher_progress_helpers() -> str:
    """Keep JSONL ownership here while a pre-Python publisher runs in a pipe."""

    return r"""
consume_publisher_progress() {
  PUB_READY=0 PUB_RESULT=0 PUB_INVALID=0 PUB_EXIT='' PUB_STATE=''
  PUB_CODE=operation_failed PUB_RETRYABLE=false PUB_PROCESS_DONE=0
  PUB_CR=$(printf '\r')
  while IFS= read -r PUB_LINE || [ -n "$PUB_LINE" ]; do
    case "$PUB_LINE" in *"$PUB_CR") PUB_LINE=${PUB_LINE%"$PUB_CR"} ;; esac
    if [ "${#PUB_LINE}" -gt 4096 ] || [ "$PUB_PROCESS_DONE" -eq 1 ]; then PUB_INVALID=1; continue; fi
    IFS=' ' read -r PUB_PREFIX PUB_KIND PUB_A PUB_B PUB_C PUB_D PUB_EXTRA <<PUBLISHER_RECORD
$PUB_LINE
PUBLISHER_RECORD
    if [ "$PUB_PREFIX" = carlina.publisher-process/1 ] && [ "$PUB_KIND" = exit ]; then
      case "$PUB_A" in ''|*[!0-9]*) PUB_INVALID=1 ;; *)
        if [ "${#PUB_A}" -gt 3 ] || [ "$PUB_A" -gt 255 ]; then PUB_INVALID=1; else PUB_EXIT=$PUB_A; fi ;;
      esac
      if [ -n "$PUB_B$PUB_C$PUB_D$PUB_EXTRA" ]; then PUB_INVALID=1; fi
      PUB_PROCESS_DONE=1
      continue
    fi
    if [ "$PUB_PREFIX" != carlina.publisher-bootstrap/1 ] || [ "$PUB_RESULT" -eq 1 ]; then PUB_INVALID=1; continue; fi
    case "$PUB_KIND" in
      ready)
        if [ "$PUB_READY" -ne 0 ] || [ -n "$PUB_A$PUB_B$PUB_C$PUB_D$PUB_EXTRA" ]; then PUB_INVALID=1; fi
        PUB_READY=1 ;;
      phase)
        if [ "$PUB_READY" -ne 1 ] || [ -n "$PUB_EXTRA" ]; then PUB_INVALID=1; continue; fi
        case "$PUB_A" in preflight|download|verification|unpacking|environment) ;; *) PUB_INVALID=1; continue ;; esac
        case "$PUB_B" in true|false) ;; *) PUB_INVALID=1; continue ;; esac
        if [ "$PUB_C:$PUB_D" != null:null ]; then
          case "$PUB_C:$PUB_D" in *[!0-9:]*|:*|*:) PUB_INVALID=1; continue ;; esac
          case "$PUB_C:$PUB_D" in 0[0-9]*:*|*:0[0-9]*) PUB_INVALID=1; continue ;; esac
          if [ "${#PUB_C}" -gt 15 ] || [ "${#PUB_D}" -gt 15 ] || [ "$PUB_D" -eq 0 ] || [ "$PUB_C" -gt "$PUB_D" ] || [ "$PUB_A" != download ]; then
            PUB_INVALID=1; continue
          fi
        fi
        if [ "$PUB_INVALID" -eq 0 ]; then
          EVENT_PHASE=$PUB_A
          emit_installation_event running null false "$PUB_B" "$PUB_C" "$PUB_D"
        fi ;;
      result)
        if [ "$PUB_READY" -ne 1 ] || [ -n "$PUB_D$PUB_EXTRA" ]; then PUB_INVALID=1; fi
        case "$PUB_A:$PUB_B:$PUB_C" in
          succeeded:null:false|cancelled:cancelled:true|failed:download_failed:true|failed:verification_failed:false|failed:operation_failed:false) ;;
          *) PUB_INVALID=1 ;;
        esac
        PUB_STATE=$PUB_A PUB_CODE=$PUB_B PUB_RETRYABLE=$PUB_C PUB_RESULT=1 ;;
      *) PUB_INVALID=1 ;;
    esac
  done
  if [ "$PUB_READY" -ne 1 ] || [ "$PUB_RESULT" -ne 1 ] || [ "$PUB_PROCESS_DONE" -ne 1 ]; then PUB_INVALID=1; fi
  case "$PUB_STATE:$PUB_EXIT" in
    succeeded:0|cancelled:130) ;;
    failed:*) if [ -z "$PUB_EXIT" ] || [ "$PUB_EXIT" -eq 0 ]; then PUB_INVALID=1; fi ;;
    *) PUB_INVALID=1 ;;
  esac
  if [ "$PUB_INVALID" -eq 1 ]; then
    PUB_EXIT=1 EVENT_ERROR=incompatible_release EVENT_RETRYABLE=false EVENT_CANCELLED=0
    echo 'Publisher progress protocol is unavailable or invalid.' >&2
  else
    EVENT_ERROR=$PUB_CODE EVENT_RETRYABLE=$PUB_RETRYABLE
    if [ "$PUB_STATE" = succeeded ]; then EVENT_ERROR=operation_failed; fi
    if [ "$PUB_STATE" = cancelled ]; then EVENT_CANCELLED=1; fi
  fi
  (umask 077; set -C; printf '%s %s %s %s %s %s\n' "$EVENT_SEQUENCE" "$EVENT_PHASE" "$EVENT_ERROR" "$EVENT_RETRYABLE" "$EVENT_CANCELLED" "$PUB_EXIT" > "$PUBLISHER_CURSOR")
}
run_publisher_bootstrap() {
  PUBLISHER_CURSOR="$WORK/publisher-progress.cursor"
  PUBLISHER_SEQUENCE_START=$EVENT_SEQUENCE
  EVENT_STREAM_OWNED=0
  if (
    set +e
    set -- --progress-records-v1
    if [ -n "$CANCEL_FILE" ]; then set -- "$@" --cancel-file "$CANCEL_FILE"; fi
    (cd "$VERSION_DIR" && TMPDIR="$EXTRACT_TMP" bash "$ARTIFACT_PATH" "$@")
    PUB_CHILD_EXIT=$?
    printf 'carlina.publisher-process/1 exit %s\n' "$PUB_CHILD_EXIT"
  ) | consume_publisher_progress; then :; else
    echo 'Publisher progress handoff could not be written.' >&2; return 1
  fi
  read_publisher_progress_handoff
}
read_publisher_progress_handoff() {
  if [ ! -f "$PUBLISHER_CURSOR" ] || [ -L "$PUBLISHER_CURSOR" ]; then
    echo 'Publisher progress handoff could not be read.' >&2; return 1
  fi
  IFS=' ' read -r EVENT_SEQUENCE EVENT_PHASE EVENT_ERROR EVENT_RETRYABLE EVENT_CANCELLED PUB_EXIT PUB_EXTRA < "$PUBLISHER_CURSOR" || return 1
  case "$EVENT_SEQUENCE" in ''|*[!0-9]*) return 1 ;; esac
  if [ "${#EVENT_SEQUENCE}" -gt 15 ] || [ "$EVENT_SEQUENCE" -lt "$PUBLISHER_SEQUENCE_START" ] || [ -n "$PUB_EXTRA" ]; then return 1; fi
  case "$EVENT_PHASE" in preflight|download|verification|unpacking|environment) ;; *) return 1 ;; esac
  case "$EVENT_ERROR:$EVENT_RETRYABLE:$EVENT_CANCELLED" in
    null:false:0|download_failed:true:0|verification_failed:false:0|operation_failed:false:0|incompatible_release:false:0|cancelled:true:1) ;; *) return 1 ;;
  esac
  case "$PUB_EXIT" in ''|*[!0-9]*) return 1 ;; esac
  if [ "${#PUB_EXIT}" -gt 3 ] || [ "$PUB_EXIT" -gt 255 ]; then return 1; fi
  EVENT_STREAM_OWNED=1
  return "$PUB_EXIT"
}
run_supervised_publisher_bootstrap() {
  PREPARATION_KNOWN=0
  PUBLISHER_CURSOR="$WORK/publisher-progress.cursor"
  PUBLISHER_SEQUENCE_START=$EVENT_SEQUENCE
  EVENT_STREAM_OWNED=0
  if run_supervised_preparation publisher "$CANCEL_FILE" | consume_publisher_progress; then :; else
    echo 'Publisher progress handoff could not be written.' >&2; return 1
  fi
  PP_OBSERVED="$WORK/preparation-observed"
  if [ -f "$PP_OBSERVED" ] && [ ! -L "$PP_OBSERVED" ] &&
      [ "$(wc -c < "$PP_OBSERVED" | tr -d '[:space:]')" -le 11 ]; then
    PP_NATIVE_EXIT=$(read_preparation_completion "$PROGRAM_ROOT" "$VERSION_DIR" "$BOOTSTRAP_CLAIM_ID" "$BOOTSTRAP_IDENTITY") &&
      PP_EXPECTED=$(printf '%s\n' "$PP_NATIVE_EXIT" | sha256sum | cut -d ' ' -f 1) &&
      [ "$PP_EXPECTED" = "$(sha256sum "$PP_OBSERVED" | cut -d ' ' -f 1)" ] && PREPARATION_KNOWN=1
  fi
  if read_publisher_progress_handoff; then PP_PROGRESS_EXIT=0; else PP_PROGRESS_EXIT=$?; fi
  [ "$PREPARATION_KNOWN" -eq 1 ] || return 1
  return "$PP_PROGRESS_EXIT"
}
"""


def _powershell_publisher_progress_helpers() -> str:
    return r"""
function Receive-PublisherProgress([string]$Line) {
    if ($Line.Length -gt 4096 -or $script:publisherFinished) { $script:publisherInvalid = $true; return }
    $fields = $Line.Split(' ')
    if ($fields.Length -lt 2 -or $fields[0] -cne 'carlina.publisher-bootstrap/1') { $script:publisherInvalid = $true; return }
    switch -CaseSensitive ($fields[1]) {
        'ready' {
            if ($script:publisherReady -or $fields.Length -ne 2) { $script:publisherInvalid = $true }
            $script:publisherReady = $true
        }
        'phase' {
            if (-not $script:publisherReady -or $fields.Length -ne 6) { $script:publisherInvalid = $true; return }
            if ($fields[2] -cnotin @('preflight', 'download', 'verification', 'unpacking', 'environment') -or $fields[3] -cnotin @('true', 'false')) {
                $script:publisherInvalid = $true; return
            }
            $completed = $null; $total = $null
            if ($fields[4] -cne 'null' -or $fields[5] -cne 'null') {
                if ($fields[4] -cnotmatch '^(0|[1-9][0-9]{0,14})$' -or $fields[5] -cnotmatch '^[1-9][0-9]{0,14}$' -or $fields[2] -cne 'download') {
                    $script:publisherInvalid = $true; return
                }
                $completed = [long]$fields[4]; $total = [long]$fields[5]
                if ($completed -gt $total) { $script:publisherInvalid = $true; return }
            }
            if (-not $script:publisherInvalid) {
                $script:eventPhase = $fields[2]
                Send-InstallationEvent 'running' $null $false ($fields[3] -ceq 'true') $completed $total
            }
        }
        'result' {
            if (-not $script:publisherReady -or $fields.Length -ne 5) { $script:publisherInvalid = $true; return }
            $outcome = $fields[2..4] -join ':'
            if ($outcome -cnotin @('succeeded:null:false', 'cancelled:cancelled:true', 'failed:download_failed:true', 'failed:verification_failed:false', 'failed:operation_failed:false')) {
                $script:publisherInvalid = $true
            }
            $script:publisherState = $fields[2]; $script:publisherCode = $fields[3]
            $script:publisherRetryable = $fields[4] -ceq 'true'
            $script:publisherFinished = $true
        }
        default { $script:publisherInvalid = $true }
    }
}
function Initialize-PublisherProgress {
    $script:publisherReady = $false; $script:publisherFinished = $false; $script:publisherInvalid = $false
    $script:publisherState = ''; $script:publisherCode = 'operation_failed'; $script:publisherRetryable = $false
    $script:publisherPending = ''
}
function Receive-SpooledPublisherProgress([IO.FileStream]$Stream) {
    $buffer = [byte[]]::new(4096)
    while (($count = $Stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        for ($index = 0; $index -lt $count; $index++) {
            $item = $buffer[$index]
            if ($item -eq 10) {
                $line = $script:publisherPending
                if ($line.EndsWith("`r")) { $line = $line.Substring(0, $line.Length - 1) }
                Receive-PublisherProgress $line
                $script:publisherPending = ''
            } elseif ($item -gt 127 -or $item -eq 0) {
                $script:publisherInvalid = $true
            } elseif ($script:publisherPending.Length -lt 4097) {
                $script:publisherPending += [char]$item
            } else { $script:publisherInvalid = $true }
        }
    }
}
function Complete-PublisherProgress([long]$PublisherExit) {
    if (-not $script:publisherReady -or -not $script:publisherFinished -or $script:publisherPending) { $script:publisherInvalid = $true }
    if (($script:publisherState -ceq 'succeeded' -and $PublisherExit -ne 0) -or
        ($script:publisherState -ceq 'cancelled' -and $PublisherExit -ne 130) -or
        ($script:publisherState -ceq 'failed' -and $PublisherExit -eq 0)) { $script:publisherInvalid = $true }
    if ($script:publisherInvalid) {
        $script:eventError = 'incompatible_release'; $script:eventRetryable = $false
        throw 'Publisher progress protocol is unavailable or invalid.'
    }
    if ($script:publisherState -ceq 'succeeded') { return }
    $script:eventError = $script:publisherCode; $script:eventRetryable = $script:publisherRetryable
    if ($script:publisherState -ceq 'cancelled') {
        $script:eventCancelled = $true
        throw [OperationCanceledException]::new('Installation cancellation requested.')
    }
    throw 'Publisher runtime setup did not complete.'
}
function Invoke-PublisherBootstrap([string]$FilePath, [string[]]$Arguments) {
    Initialize-PublisherProgress
    $previousPreference = $ErrorActionPreference
    $global:LASTEXITCODE = $null
    $script:bootstrapNativeActive = $true
    try {
        $ErrorActionPreference = 'Continue'
        & $FilePath @Arguments | ForEach-Object { Receive-PublisherProgress ([string]$_) }
        $publisherExit = $global:LASTEXITCODE
        $script:bootstrapNativeActive = $false
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($null -eq $publisherExit) { $script:publisherInvalid = $true }
    Complete-PublisherProgress $publisherExit
}
"""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _resolved_url(index_url: str, reference: str) -> str:
    resolved = urljoin(index_url, reference)
    _validate_public_https_url(resolved, "release asset")
    return resolved


def _validate_public_https_url(value: str, label: str) -> None:
    try:
        parsed = urlsplit(value)
        _validated_port = parsed.port
    except ValueError as exc:
        raise InstallerRenderError(f"Published {label} URL is invalid.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InstallerRenderError("Published installers require HTTPS release assets.")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_installer(
    *,
    version: str,
    channel: str,
    index_url: str,
    index_sha256: str,
    artifact_url: str,
    artifact_filename: str,
    artifact_sha256: str,
    artifact_size: int,
    trust_b64: str,
    application_profile: str = "workbench",
    artifact_kind: str = "pixi-pack-bundle",
    artifact_platform: str = "win-64",
) -> str:
    selected_profile = _validated_application_profile(application_profile)
    is_documents = selected_profile == RuntimeProfile.DOCUMENTS.value
    if artifact_kind not in {
        "pixi-pack-bundle",
        PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND,
    } or (artifact_kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND and not is_documents):
        raise InstallerRenderError("Windows installer artifact kind is unsupported.")
    unpack_setup = """    $bundleDir = Join-Path $work 'bundle'
    New-Item -ItemType Directory -Path $bundleDir | Out-Null
    Expand-VerifiedWindowsBundle $artifactPath $bundleDir
    $archivePath = Join-Path $bundleDir 'archive.tar'
    $unpackerPath = Join-Path $bundleDir 'pixi-unpack.exe'
    $unpackArguments = @($archivePath)
"""
    if artifact_kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND:
        unpack_setup = """    $unpackerPath = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $unpackerPath -PathType Leaf)) {
        $unpackerPath = Join-Path $PSHOME 'pwsh.exe'
    }
    $unpackArguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $artifactPath)
    $archivePath = $artifactPath
"""
    publisher_artifact = artifact_kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND
    product_name = "Carlina Documents" if is_documents else "Carlina"
    launcher_name = "carlina-documents" if is_documents else "carlina"
    entry_module = "carlina.documents" if is_documents else "carlina.bootstrap_manager"
    identity = BootstrapIdentity(
        selected_profile,
        version,
        channel,
        index_sha256,
        artifact_platform,
        artifact_kind,
        artifact_filename,
        artifact_sha256,
        artifact_size,
    )
    identity_json = json.dumps(asdict(identity), sort_keys=True, separators=(",", ":"))
    program_root_default = (
        "(Join-Path $env:LOCALAPPDATA 'Programs\\CarlinaDocuments')"
        if is_documents
        else "(Join-Path $env:LOCALAPPDATA 'Programs\\Carlina')"
    )
    profile_environment = (
        "    $env:CARLINA_PROFILE = 'documents'\n" if is_documents else ""
    )
    profile_probe_b64 = _ps_quote(
        base64.b64encode(_application_profile_probe().encode("utf-8")).decode("ascii")
    )
    expected_profile = _ps_quote(selected_profile)
    values = {
        "version": _ps_quote(version),
        "channel": _ps_quote(channel),
        "index_url": _ps_quote(index_url),
        "index_sha": _ps_quote(index_sha256),
        "artifact_url": _ps_quote(artifact_url),
        "artifact_filename": _ps_quote(artifact_filename),
        "artifact_sha": _ps_quote(artifact_sha256),
        "trust_b64": _ps_quote(trust_b64),
    }
    return f"""# Generated from a signed Carlina release index. Do not edit.
[CmdletBinding()]
param(
    [string]$ProgramRoot = {program_root_default},
    [switch]$SkipInit,
    [switch]$NoPathUpdate,
    [switch]$EventsJsonl,
    [switch]$SupervisedPreparation,
    [string]$OperationId,
    [string]$CancelFile
)
$ErrorActionPreference = 'Stop'
$version = {values["version"]}
$channel = {values["channel"]}
$indexUrl = {values["index_url"]}
$indexSha256 = {values["index_sha"]}
$artifactUrl = {values["artifact_url"]}
$artifactFilename = {values["artifact_filename"]}
$artifactSha256 = {values["artifact_sha"]}
$artifactSize = {artifact_size}
$trustBase64 = {values["trust_b64"]}
$createdVersionDir = $false
$registered = $false
$adoptionStarted = $false
$resumingBootstrap = $false
$preparationAlreadyComplete = $false
$recoveringPreparation = $false
$ownershipMode = 'bind'
$resumeArguments = @()
$work = $null
$versionDir = $null
$eventOperationId = $OperationId
$eventSequence = 0
$eventPhase = 'preflight'
$eventError = 'operation_failed'
$eventRetryable = $false
$eventCancelled = $false
$terminalJson = $null
$managerExitCode = 0
$script:bootstrapLease = $null
$script:bootstrapNativeActive = $false
$bootstrapClaimId = [guid]::NewGuid().ToString('N')
$bootstrapIdentity = {_ps_quote(identity.digest)}
$bootstrapIdentityBase64 = {_ps_quote(base64.b64encode(identity_json.encode("utf-8")).decode("ascii"))}
$bootstrapClaimHeader = {_ps_quote(BOOTSTRAP_CLAIM_HEADER)}
$bootstrapClaimSize = {len(BOOTSTRAP_CLAIM_HEADER) + 99}
$bootstrapClaimPath = $null
{_powershell_bootstrap_ownership_helpers()}
{render_powershell_preparation_helpers()}
{_powershell_event_helpers(selected_profile)}
{_powershell_download_helpers()}
{_powershell_publisher_progress_helpers() if publisher_artifact else ""}
if (-not $EventsJsonl -and ($PSBoundParameters.ContainsKey('OperationId') -or $PSBoundParameters.ContainsKey('CancelFile'))) {{
    throw 'Installation event controls require -EventsJsonl.'
}}
if ($SupervisedPreparation -and (-not $EventsJsonl -or {_ps_quote(artifact_platform)} -cne 'win-64')) {{
    throw 'Supervised preparation requires event mode and a supported runtime payload.'
}}
if ($SupervisedPreparation) {{ $ownershipMode = 'prepared-bind' }}
if ($EventsJsonl) {{
    $PSNativeCommandUseErrorActionPreference = $false
    if (-not $SkipInit -or -not $NoPathUpdate) {{ throw 'Event mode requires -SkipInit and -NoPathUpdate.' }}
    if (-not $PSBoundParameters.ContainsKey('OperationId')) {{ $eventOperationId = [guid]::NewGuid().ToString('N') }}
    if ($eventOperationId.Length -ne 32 -or $eventOperationId -cnotmatch '^[0-9a-f]+$') {{ throw 'Installation operation ID is invalid.' }}
    if ($PSBoundParameters.ContainsKey('CancelFile')) {{
        if (-not $CancelFile) {{ throw 'Cancellation control path is invalid.' }}
        $CancelFile = [IO.Path]::GetFullPath($CancelFile)
    }}
}}

function Invoke-CheckedNative([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage, [switch]$Interactive) {{
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {{ throw $FailureMessage }}
    $previousPreference = $ErrorActionPreference
    $global:LASTEXITCODE = $null
    $script:bootstrapNativeActive = $true
    try {{
        # PowerShell 5.1 turns redirected native stderr into ErrorRecords.
        # Progress output must not abort a successful unpack or initialization.
        $ErrorActionPreference = 'Continue'
        if ($Interactive) {{
            # Leave console prompts attached to the host during administrator setup.
            & $FilePath @Arguments
        }} else {{
            & $FilePath @Arguments 2>&1 | ForEach-Object {{ [string]$_ }}
        }}
        $nativeExitCode = $global:LASTEXITCODE
        $script:bootstrapNativeActive = $false
    }} finally {{
        $ErrorActionPreference = $previousPreference
    }}
    if ($null -eq $nativeExitCode -or $nativeExitCode -ne 0) {{
        throw "$FailureMessage Exit code: $nativeExitCode"
    }}
}}

function Assert-FileHash([string]$Path, [string]$Expected, [long]$ExpectedSize) {{
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne $ExpectedSize) {{
        throw "Size mismatch for $Path (expected $ExpectedSize, got $($item.Length))."
    }}
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected) {{ throw "SHA-256 mismatch for $Path." }}
}}

function Assert-ExactPropertyNames($Value, [string[]]$Expected, [string]$Label) {{
    $actual = @($Value.PSObject.Properties.Name)
    $difference = @(Compare-Object -ReferenceObject $Expected -DifferenceObject $actual)
    if ($difference.Count -ne 0) {{ throw "$Label contains unexpected or missing fields." }}
}}

function Assert-ApplicationProfile([string]$Python, [string]$EnvironmentRoot, [string]$ProbePath) {{
    [System.IO.File]::WriteAllBytes($ProbePath, [Convert]::FromBase64String({profile_probe_b64}))
    Invoke-CheckedNative -FilePath $Python `
        -Arguments @('-I', '-S', $ProbePath, $EnvironmentRoot, {expected_profile}) `
        -FailureMessage 'Bundled runtime application-profile validation failed.'
}}

function Expand-VerifiedWindowsBundle([string]$Path, [string]$Destination) {{
    $expectedNames = @('archive.tar', 'pixi-unpack.exe', 'bundle.json')
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {{
        $zipEntries = @($zip.Entries)
        if ($zipEntries.Count -ne $expectedNames.Count) {{
            throw 'Windows runtime bundle has an unexpected member count.'
        }}
        foreach ($entry in $zipEntries) {{
            $unixMode = ($entry.ExternalAttributes -shr 16) -band 65535
            if (
                $entry.FullName -cne $entry.Name -or
                $expectedNames -cnotcontains $entry.FullName -or
                $entry.Length -lt 1 -or
                $entry.Length -gt 21474836480 -or
                $entry.CompressedLength -ne $entry.Length -or
                (($unixMode -band 61440) -eq 40960)
            ) {{
                throw "Windows runtime bundle has unsafe ZIP metadata: $($entry.FullName)"
            }}
        }}
        foreach ($expected in $expectedNames) {{
            if (@($zipEntries | Where-Object {{ $_.FullName -ceq $expected }}).Count -ne 1) {{
                throw "Windows runtime bundle member is missing or duplicated: $expected"
            }}
        }}
    }} finally {{
        $zip.Dispose()
    }}
    Expand-Archive -LiteralPath $Path -DestinationPath $Destination
    $entries = @(Get-ChildItem -LiteralPath $Destination -Force)
    if ($entries.Count -ne $expectedNames.Count) {{
        throw 'Windows runtime bundle has an unexpected member count.'
    }}
    foreach ($entry in $entries) {{
        if ($entry.PSIsContainer -or $expectedNames -notcontains $entry.Name) {{
            throw "Windows runtime bundle contains an unexpected member: $($entry.Name)"
        }}
    }}
    $manifestPath = Join-Path $Destination 'bundle.json'
    $manifestItem = Get-Item -LiteralPath $manifestPath
    if ($manifestItem.Length -gt 65536) {{ throw 'Windows bundle manifest is too large.' }}
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-ExactPropertyNames -Value $manifest -Expected @('format', 'members', 'schema_version') -Label 'Windows bundle manifest'
    if ($manifest.format -ne 'carlina.windows-pixi-pack' -or $manifest.schema_version -ne 1) {{
        throw 'Unsupported Windows runtime bundle format.'
    }}
    Assert-ExactPropertyNames -Value $manifest.members -Expected @('archive.tar', 'pixi-unpack.exe') -Label 'Windows bundle members'
    foreach ($name in @('archive.tar', 'pixi-unpack.exe')) {{
        $record = $manifest.members.PSObject.Properties[$name].Value
        Assert-ExactPropertyNames -Value $record -Expected @('sha256', 'size') -Label "Windows bundle member $name"
        if ($record.sha256 -notmatch '^[0-9a-f]{{64}}$' -or [long]$record.size -lt 1 -or [long]$record.size -gt 21474836480) {{
            throw "Windows bundle member $name has invalid integrity metadata."
        }}
        Assert-FileHash (Join-Path $Destination $name) $record.sha256 ([long]$record.size)
    }}
}}

function Save-HttpsFile([string]$Url, [string]$Path, [long]$ExpectedSize = 0) {{
    $initial = [Uri]$Url
    if ($initial.Scheme -ne 'https') {{ throw "HTTPS is required: $Url" }}
    if ($EventsJsonl) {{
        Save-HttpsEventFile -Url $Url -Path $Path -ExpectedSize $ExpectedSize
        return
    }}
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path `
        -PassThru -TimeoutSec 900
    $final = $response.BaseResponse.ResponseUri
    if (-not $final -and $response.BaseResponse.RequestMessage) {{
        $final = $response.BaseResponse.RequestMessage.RequestUri
    }}
    if (-not $final -or $final.Scheme -ne 'https') {{
        throw "Download redirect left HTTPS: $Url"
    }}
}}

try {{
Invoke-InstallationCheckpoint 'preflight' $true
$ProgramRoot = [System.IO.Path]::GetFullPath($ProgramRoot)
$versionsRoot = Join-Path $ProgramRoot 'versions'
$versionDir = Join-Path $ProgramRoot "versions\\$version"
$bootstrapClaimPath = Join-Path $versionDir {_ps_quote(BOOTSTRAP_CLAIM_NAME)}
$bootstrapBindingPath = Join-Path $versionDir {_ps_quote(BOOTSTRAP_BINDING_NAME)}
New-Item -ItemType Directory -Path $ProgramRoot -Force | Out-Null
$stateRoot = Join-Path $ProgramRoot 'state'
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
foreach ($path in @($stateRoot, (Join-Path $stateRoot 'managed-install.lock'))) {{
    if (Test-Path -LiteralPath $path) {{
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{ throw 'Bootstrap maintenance path is unsafe.' }}
    }}
}}
$script:bootstrapLease = [IO.FileStream]::new(
    (Join-Path $stateRoot 'managed-install.lock'), [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite, [IO.FileShare]::None
)
$programItem = Get-Item -LiteralPath $ProgramRoot
$freeBytes = $programItem.PSDrive.Free
if ($null -eq $freeBytes) {{
    throw "Could not determine free space for $ProgramRoot."
}}
$requiredBootstrapBytes = [Math]::Max(([long]$artifactSize * 6), [long](3GB))
if ([long]$freeBytes -lt $requiredBootstrapBytes) {{
    throw "{product_name} bootstrap needs at least $requiredBootstrapBytes free bytes on the program volume; $freeBytes are available."
}}
$work = Join-Path $ProgramRoot ('.carlina-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
    $retainedVersion = Get-Item -LiteralPath $versionDir -Force -ErrorAction SilentlyContinue
    if ($null -ne $retainedVersion) {{
        $eventError = 'incompatible_release'
        try {{
            $bootstrapClaimId = Read-ReadyBootstrapClaim
            $resumingBootstrap = $true
            $ownershipMode = if ($SupervisedPreparation) {{ 'prepared-resume' }} else {{ 'resume' }}
        }} catch {{
            if (-not $SupervisedPreparation) {{ throw }}
            $bootstrapClaimId = Read-MatchingBootstrapClaim
            $nativeCompletion = Read-PreparationCompletion $versionDir $bootstrapClaimId $bootstrapIdentity $ProgramRoot
            if ($nativeCompletion -eq 0) {{
                $preparationAlreadyComplete = $true
            }} else {{
                $null = Test-StoppedPreparationRecovery $script:bootstrapLease $ProgramRoot $versionDir $bootstrapClaimId $bootstrapIdentity
                $recoveringPreparation = $true
            }}
        }}
        if (-not $recoveringPreparation) {{ $resumeArguments = @('--resume-bootstrap-claim', $bootstrapClaimId) }}
    }} else {{
    New-BootstrapGeneration
    }}
    $indexPath = Join-Path $work 'release-index.json'
    $artifactPath = Join-Path $work $artifactFilename
    $trustPath = Join-Path $work 'release-trust.json'
    $eventError = 'download_failed'
    $eventRetryable = $true
    Invoke-InstallationCheckpoint 'download' $true
    Save-HttpsFile $indexUrl $indexPath
    $eventError = 'verification_failed'
    $eventRetryable = $false
    Invoke-InstallationCheckpoint 'verification' $true
    $downloadedIndexSha = (Get-FileHash -LiteralPath $indexPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadedIndexSha -ne $indexSha256) {{ throw 'Release-index SHA-256 mismatch.' }}
    $eventError = 'download_failed'
    $eventRetryable = $true
    Invoke-InstallationCheckpoint 'download' $true
    Send-InstallationEvent 'running' $null $false $true 0 ([long]$artifactSize)
    Save-HttpsFile $artifactUrl $artifactPath ([long]$artifactSize)
    $eventError = 'verification_failed'
    $eventRetryable = $false
    Invoke-InstallationCheckpoint 'verification' $true
    Assert-FileHash $artifactPath $artifactSha256 $artifactSize
    $eventPhase = 'download'
    Send-InstallationEvent 'running' $null $false $true ([long]$artifactSize) ([long]$artifactSize)
    $eventError = 'operation_failed'
    [System.IO.File]::WriteAllBytes($trustPath, [Convert]::FromBase64String($trustBase64))
    if ($recoveringPreparation) {{
        Invoke-InstallationCheckpoint 'recovery' $true
        Invoke-InstallationCheckpoint 'recovery' $false
        $retiredPreparation = Move-StoppedPreparation $script:bootstrapLease $ProgramRoot $versionDir $bootstrapClaimId $bootstrapIdentity
        New-BootstrapGeneration
    }}
    if (-not $resumingBootstrap -and -not $preparationAlreadyComplete) {{
    $extractTemp = Join-Path $work 'extract'
    New-Item -ItemType Directory -Path $extractTemp | Out-Null
{unpack_setup}    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    Push-Location $versionDir
    try {{
        if (-not $SupervisedPreparation) {{
            $env:TEMP = $extractTemp
            $env:TMP = $extractTemp
        }}
        Invoke-InstallationCheckpoint 'unpacking' $true
        Invoke-InstallationCheckpoint 'unpacking' $false
        if ($SupervisedPreparation) {{
            $script:bootstrapNativeActive = $true
            $preparationControls = @{{}}
            $publisherStream = $null
            $progressException = $null
            if (${str(publisher_artifact).lower()}) {{
                $preparationControls = @{{ PublisherProgress = $true; CancelFile = $CancelFile }}
                Initialize-PublisherProgress
            }}
            $custodian = Start-PreparationCustodian $script:bootstrapLease $work $ProgramRoot $versionDir `
                $bootstrapClaimId $bootstrapIdentity $unpackerPath $archivePath $extractTemp @preparationControls
            try {{
                do {{
                    $custodianDone = $custodian.WaitForExit(100)
                    if (${str(publisher_artifact).lower()} -and $null -eq $progressException) {{
                        try {{
                            $progressPath = Join-Path $work 'preparation-native.stdout'
                            if ($null -eq $publisherStream -and (Test-Path -LiteralPath $progressPath)) {{
                                $null = Test-CarlinaPreparationPathChain -Path $progressPath
                                $publisherStream = [IO.FileStream]::new($progressPath, [IO.FileMode]::Open,
                                    [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
                            }}
                            if ($null -ne $publisherStream) {{ Receive-SpooledPublisherProgress $publisherStream }}
                        }} catch {{ $progressException = $_ }}
                    }}
                }} while (-not $custodianDone)
                $custodianExit = $custodian.ExitCode
                $nativeCompletion = Read-PreparationCompletion $versionDir $bootstrapClaimId $bootstrapIdentity $ProgramRoot
                if (($nativeCompletion -eq 0 -and $custodianExit -ne 0) -or
                    ($nativeCompletion -ne 0 -and $custodianExit -ne 1)) {{
                    throw 'Native preparation completion is invalid.'
                }}
                $script:bootstrapNativeActive = $false
                if (${str(publisher_artifact).lower()}) {{
                    if ($null -ne $progressException) {{ throw $progressException }}
                    if ($null -eq $publisherStream) {{ $script:publisherInvalid = $true }}
                    Complete-PublisherProgress $nativeCompletion
                }} elseif ($nativeCompletion -ne 0) {{ throw 'Native preparation did not complete.' }}
            }} finally {{
                if ($null -ne $publisherStream) {{ $publisherStream.Dispose() }}
                $custodian.Dispose()
            }}
        }} elseif ($EventsJsonl -and ${str(publisher_artifact).lower()}) {{
            $unpackArguments += '-ProgressRecordsV1'
            if ($CancelFile) {{ $unpackArguments += @('-CancelFile', $CancelFile) }}
            Invoke-PublisherBootstrap -FilePath $unpackerPath -Arguments $unpackArguments
        }} elseif ($EventsJsonl) {{
            Invoke-CheckedNative -FilePath $unpackerPath `
                -Arguments $unpackArguments -FailureMessage 'Runtime unpack failed.' |
                ForEach-Object {{ Write-InstallationDiagnostic ([string]$_) }}
        }} else {{
            Invoke-CheckedNative -FilePath $unpackerPath `
                -Arguments $unpackArguments -FailureMessage 'Runtime unpack failed.'
        }}
    }} finally {{
        if ($null -eq $previousTemp) {{ Remove-Item Env:TEMP -ErrorAction SilentlyContinue }} else {{ $env:TEMP = $previousTemp }}
        if ($null -eq $previousTmp) {{ Remove-Item Env:TMP -ErrorAction SilentlyContinue }} else {{ $env:TMP = $previousTmp }}
        Pop-Location
    }}
    }}
    $eventError = 'incompatible_release'
    Invoke-InstallationCheckpoint 'verification' $true
    $python = Join-Path $versionDir 'env\\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {{
        throw "Unpacked runtime is missing: $python"
    }}
    $pixi = Join-Path $versionDir 'env\\bin\\pixi.exe'
    if (-not (Test-Path -LiteralPath $pixi -PathType Leaf)) {{
        throw "Unpacked runtime is missing its bundled Pixi executable: $pixi"
    }}
    $profileProbePath = Join-Path $work 'application-profile-probe.py'
    if ($EventsJsonl) {{
        Assert-ApplicationProfile $python (Join-Path $versionDir 'env') $profileProbePath |
            ForEach-Object {{ Write-InstallationDiagnostic ([string]$_) }}
    }} else {{
        Assert-ApplicationProfile $python (Join-Path $versionDir 'env') $profileProbePath
    }}
    $env:CARLINA_PROGRAM_ROOT = $ProgramRoot
    $env:CARLINA_MANAGED_VERSION = $version
{profile_environment}    Invoke-InstallationCheckpoint 'environment' $false
    $ownershipAck = Invoke-CheckedNative -FilePath $python -Arguments @(
        '-I', '-c', {_ps_quote(_bootstrap_ownership_probe())},
        $ProgramRoot, $versionDir, $bootstrapClaimId, $bootstrapIdentityBase64, $ownershipMode
    ) -Interactive -FailureMessage 'Bootstrap ownership protocol is unavailable.'
    if ($ownershipAck -isnot [string] -or $ownershipAck -cne "carlina.bootstrap-ready/1 $bootstrapClaimId $bootstrapIdentity") {{
        throw 'Bootstrap ownership acknowledgement is invalid.'
    }}
    if ($EventsJsonl) {{
        $handoffProbePath = Join-Path $work 'installation-handoff.py'
        [IO.File]::WriteAllBytes($handoffProbePath, [Convert]::FromBase64String('{base64.b64encode(_installation_handoff_probe().encode("utf-8")).decode("ascii")}'))
        Invoke-InstallationCheckpoint 'environment' $false
        Invoke-CheckedNative -FilePath $python -Arguments @('-I', $handoffProbePath, 'probe') `
            -FailureMessage 'Installation event protocol is unavailable.' |
            ForEach-Object {{ Write-InstallationDiagnostic ([string]$_) }}
        Invoke-InstallationCheckpoint 'verification' $true
        $handoffSequence = $eventSequence
        $terminalPath = Join-Path $work 'installation-terminal.jsonl'
        $eventArguments = @(
            '-I', '-m', '{entry_module}', 'install',
            '--manifest', $indexPath, '--artifact', $artifactPath,
            '--adopt-version-dir', $versionDir, '--channel', $channel, '--version', $version,
            '--program-root', $ProgramRoot, '--trust-store', $trustPath,
            '--expected-index-sha256', $indexSha256,
            '--events-jsonl', '--operation-id', $eventOperationId,
            '--event-sequence-start', [string]$handoffSequence,
            '--event-terminal-file', $terminalPath
        )
        $eventArguments += $resumeArguments
        if ($CancelFile) {{ $eventArguments += @('--cancel-file', $CancelFile) }}
        $adoptionStarted = $true
        Close-BootstrapLease
        try {{
            Invoke-CheckedNative -FilePath $python -Arguments $eventArguments `
                -Interactive -FailureMessage 'Managed adoption failed.'
        }} catch {{
            if ($null -ne $global:LASTEXITCODE -and $global:LASTEXITCODE -ne 0) {{
                $managerExitCode = [int]$global:LASTEXITCODE
            }} else {{ throw }}
        }}
        $terminalJson = Invoke-CheckedNative -FilePath $python -Arguments @(
            '-I', $handoffProbePath, 'read', $terminalPath, $eventOperationId,
            '{selected_profile}', [string]$handoffSequence, [string]$managerExitCode
        ) -FailureMessage 'Installation event handoff could not be verified.'
        if ($terminalJson -isnot [string] -or -not $terminalJson) {{ throw 'Installation event handoff is invalid.' }}
    }} else {{
    # Adoption can commit before its process returns to this installer.
    $adoptionStarted = $true
    Close-BootstrapLease
    Invoke-CheckedNative -FilePath $python -Arguments (@(
        '-I', '-m', '{entry_module}', 'install',
        '--manifest', $indexPath, '--artifact', $artifactPath,
        '--adopt-version-dir', $versionDir, '--channel', $channel, '--version', $version,
        '--program-root', $ProgramRoot, '--trust-store', $trustPath,
        '--expected-index-sha256', $indexSha256
    ) + $resumeArguments) -FailureMessage 'Managed registration failed.'
    $registered = $true
    if (-not $NoPathUpdate) {{
        try {{
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            $segments = @($userPath -split ';' | Where-Object {{ $_ }})
            if (-not ($segments | Where-Object {{
                [string]::Equals(
                    $_.TrimEnd('\\\\'),
                    $ProgramRoot.TrimEnd('\\\\'),
                    [StringComparison]::OrdinalIgnoreCase
                )
            }})) {{
                [Environment]::SetEnvironmentVariable(
                    'Path',
                    (($segments + $ProgramRoot) -join ';'),
                    'User'
                )
                Write-Host 'Added {product_name} to your user PATH. Open a new terminal to use it.'
            }}
            $env:Path = "$ProgramRoot;$env:Path"
        }} catch {{
            Write-Warning "{product_name} was installed, but user PATH could not be updated: $_"
        }}
    }}
    if (-not $SkipInit) {{
        Invoke-CheckedNative -FilePath (Join-Path $ProgramRoot '{launcher_name}.cmd') `
            -Arguments @('init') -Interactive -FailureMessage '{product_name} initialization failed.'
    }}
    Write-Host "{product_name} $version is installed at $ProgramRoot"
    }}
}} catch {{
    if ($EventsJsonl) {{
        $terminalJson = $null
        if ($eventCancelled) {{ $managerExitCode = 130 }} else {{ $managerExitCode = 1 }}
        Write-InstallationDiagnostic 'Installation did not complete; inspect its event stream and durable state.'
    }} else {{
        Write-Error $_
        throw
    }}
}} finally {{
    $cleanupCompleted = Remove-OwnedBootstrapScratch
    Close-BootstrapLease
    if ($EventsJsonl) {{
        if (-not $cleanupCompleted) {{
            Write-InstallationDiagnostic 'Installation scratch cleanup failed; inspect the retained state.'
            $managerExitCode = 1
        }} elseif (-not $adoptionStarted) {{
            if ($eventCancelled) {{ Send-InstallationEvent 'cancelled' 'cancelled' $true $true }}
            else {{ Send-InstallationEvent 'failed' $eventError $eventRetryable $true }}
        }} elseif ($terminalJson) {{
            try {{ [Console]::Out.WriteLine($terminalJson) }} catch {{}}
        }}
    }}
}}
if ($EventsJsonl) {{ exit $managerExitCode }}
"""


def _shell_installer(
    *,
    version: str,
    channel: str,
    index_url: str,
    index_sha256: str,
    artifact_url: str,
    artifact_filename: str,
    artifact_sha256: str,
    artifact_size: int,
    trust_b64: str,
    application_profile: str = "workbench",
    artifact_kind: str = "pixi-pack-executable",
    artifact_platform: str = "linux-64",
) -> str:
    selected_profile = _validated_application_profile(application_profile)
    is_documents = selected_profile == RuntimeProfile.DOCUMENTS.value
    if artifact_kind not in {
        "pixi-pack-executable",
        PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND,
    } or (artifact_kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND and not is_documents):
        raise InstallerRenderError("POSIX installer artifact kind is unsupported.")
    product_name = "Carlina Documents" if is_documents else "Carlina"
    launcher_name = "carlina-documents" if is_documents else "carlina"
    entry_module = "carlina.documents" if is_documents else "carlina.bootstrap_manager"
    identity = BootstrapIdentity(
        selected_profile,
        version,
        channel,
        index_sha256,
        artifact_platform,
        artifact_kind,
        artifact_filename,
        artifact_sha256,
        artifact_size,
    )
    identity_json = json.dumps(asdict(identity), sort_keys=True, separators=(",", ":"))
    profile_environment = "export CARLINA_PROFILE=documents\n" if is_documents else ""
    default_program_root = (
        """case "$(uname -s)" in
  Darwin) DOCUMENTS_PROGRAM_ROOT="$HOME/Library/Application Support/CarlinaDocuments/Program" ;;
  *) DOCUMENTS_PROGRAM_ROOT="$HOME/.local/lib/carlina-documents" ;;
esac
PROGRAM_ROOT=${CARLINA_PROGRAM_ROOT:-"$DOCUMENTS_PROGRAM_ROOT"}"""
        if is_documents
        else "PROGRAM_ROOT=${CARLINA_PROGRAM_ROOT:-$HOME/.local/lib/carlina}"
    )
    q = shlex.quote
    profile_probe = q(_application_profile_probe())
    ownership_probe = q(_bootstrap_ownership_probe())
    handoff_probe = q(_installation_handoff_probe())
    expected_profile = q(selected_profile)
    publisher_artifact = artifact_kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND
    return f"""#!/usr/bin/env sh
# Generated from a signed Carlina release index. Do not edit.
set -eu
{profile_environment}VERSION={q(version)}
CHANNEL={q(channel)}
INDEX_URL={q(index_url)}
INDEX_SHA256={q(index_sha256)}
ARTIFACT_URL={q(artifact_url)}
ARTIFACT_FILENAME={q(artifact_filename)}
ARTIFACT_SHA256={q(artifact_sha256)}
ARTIFACT_SIZE={artifact_size}
ARTIFACT_KIND={q(artifact_kind)}
ARTIFACT_PLATFORM={q(artifact_platform)}
TRUST_BASE64={q(trust_b64)}
{default_program_root}
SKIP_INIT=0
SUPERVISED_PREPARATION=0
PREPARATION_ALREADY_COMPLETE=0
RECOVERING_PREPARATION=0
REGISTERED=0
ADOPTION_STARTED=0
RESUMING_BOOTSTRAP=0
BOOTSTRAP_OWNERSHIP_MODE=bind
BOOTSTRAP_LEASE_OWNED=0
BOOTSTRAP_NATIVE_ACTIVE=0
BOOTSTRAP_DOWNLOAD_PID=''
BOOTSTRAP_CLAIM_PATH=''
BOOTSTRAP_BINDING_PATH=''
BOOTSTRAP_CLAIM_ID=''
BOOTSTRAP_CLAIM_HEADER={q(BOOTSTRAP_CLAIM_HEADER)}
BOOTSTRAP_IDENTITY={q(identity.digest)}
BOOTSTRAP_IDENTITY_BASE64={q(base64.b64encode(identity_json.encode("utf-8")).decode("ascii"))}
BOOTSTRAP_CLAIM_SIZE={len(BOOTSTRAP_CLAIM_HEADER) + 99}
CREATED_VERSION_DIR=0
CREATE_PATH_LINK=1
EVENTS_JSONL=0
EVENT_ID=''
EVENT_ID_GIVEN=0
EVENT_SEQUENCE=0
EVENT_PHASE=preflight
EVENT_ERROR=operation_failed
EVENT_RETRYABLE=false
EVENT_CANCELLED=0
EVENT_STREAM_OWNED=1
CANCEL_FILE=''
CANCEL_FILE_GIVEN=0
WORK=''
VERSION_DIR=''

while [ "$#" -gt 0 ]; do
  case "$1" in
    --program-root) PROGRAM_ROOT=$2; shift 2 ;;
    --skip-init) SKIP_INIT=1; shift ;;
    --no-path-link) CREATE_PATH_LINK=0; shift ;;
    --events-jsonl) EVENTS_JSONL=1; shift ;;
    --supervised-preparation) SUPERVISED_PREPARATION=1; shift ;;
    --operation-id) EVENT_ID=$2; EVENT_ID_GIVEN=1; shift 2 ;;
    --cancel-file) CANCEL_FILE=$2; CANCEL_FILE_GIVEN=1; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$EVENTS_JSONL" -eq 0 ] && {{ [ "$EVENT_ID_GIVEN" -eq 1 ] || [ "$CANCEL_FILE_GIVEN" -eq 1 ]; }}; then
  echo 'Installation event controls require --events-jsonl.' >&2; exit 2
fi
if [ "$SUPERVISED_PREPARATION" -eq 1 ] && {{ [ "$EVENTS_JSONL" -ne 1 ] || [ "$ARTIFACT_PLATFORM" != linux-64 ]; }}; then
  echo 'Supervised preparation requires event mode and a supported runtime payload.' >&2; exit 2
fi
if [ "$EVENTS_JSONL" -eq 1 ]; then
  if [ "$SKIP_INIT" -ne 1 ] || [ "$CREATE_PATH_LINK" -ne 0 ]; then
    echo 'Event mode requires --skip-init and --no-path-link.' >&2; exit 2
  fi
  if [ "$EVENT_ID_GIVEN" -eq 0 ]; then EVENT_ID=$(od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]'); fi
  case "$EVENT_ID" in ''|*[!0-9a-f]*) echo 'Installation operation ID is invalid.' >&2; exit 2 ;; esac
  if [ "${{#EVENT_ID}}" -ne 32 ]; then echo 'Installation operation ID is invalid.' >&2; exit 2; fi
  if [ "$CANCEL_FILE_GIVEN" -eq 1 ] && [ -z "$CANCEL_FILE" ]; then echo 'Cancellation control path is invalid.' >&2; exit 2; fi
  if [ -n "$CANCEL_FILE" ]; then case "$CANCEL_FILE" in /*) ;; *) CANCEL_FILE="$PWD/$CANCEL_FILE" ;; esac; fi
fi
{_shell_event_helpers(selected_profile)}
{_shell_download_helpers()}
{_shell_publisher_progress_helpers() if publisher_artifact else ""}
{_shell_bootstrap_ownership_helpers()}
{render_shell_preparation_helpers()}
cleanup() {{
  if [ "$BOOTSTRAP_NATIVE_ACTIVE" -eq 1 ]; then return 1; fi
  if [ "$CREATED_VERSION_DIR" -eq 1 ] && [ "$REGISTERED" -ne 1 ] && [ "$ADOPTION_STARTED" -eq 0 ] && [ -n "$VERSION_DIR" ]; then
    if [ "$BOOTSTRAP_LEASE_OWNED" -ne 1 ] || ! owned_bootstrap_generation; then return 1; fi
    rm -rf -- "$VERSION_DIR" || return 1
  fi
  if [ -n "$WORK" ]; then rm -rf -- "$WORK" || return 1; fi
}}
trap finish_installation EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
installation_checkpoint preflight true
if [ "$SUPERVISED_PREPARATION" -eq 1 ]; then
  if ! check_preparation_capability; then
    echo 'Native preparation supervision is unavailable.' >&2; exit 1
  fi
  if [ "$ARTIFACT_KIND" = publisher-bootstrap-executable ]; then
    if ! command -v tail >/dev/null 2>&1 || ! tail --version 2>/dev/null | head -n 1 | LC_ALL=C grep -q 'GNU coreutils'; then
      echo 'GNU tail is required for supervised publisher progress.' >&2; exit 1
    fi
  fi
  BOOTSTRAP_OWNERSHIP_MODE=prepared-bind
fi

if ! command -v bash >/dev/null 2>&1; then
  echo "Bash is required to unpack the runtime bundle." >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo 'Linux flock is required for bootstrap maintenance exclusion.' >&2
  exit 1
fi

hash_file() {{
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{{print $1}}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{{print $1}}'
  else echo "A SHA-256 tool is required." >&2; return 1; fi
}}
decode_base64() {{
  if printf '%s' "$1" | base64 --decode >/dev/null 2>&1; then
    printf '%s' "$1" | base64 --decode
  else
    printf '%s' "$1" | base64 -D
  fi
}}

mkdir -p "$PROGRAM_ROOT"
PROGRAM_ROOT=$(cd "$PROGRAM_ROOT" && pwd -P)
if [ -L "$PROGRAM_ROOT/state" ] || {{ [ -e "$PROGRAM_ROOT/state" ] && [ ! -d "$PROGRAM_ROOT/state" ]; }}; then
  echo 'Bootstrap maintenance directory is unsafe.' >&2; exit 1
fi
mkdir -p "$PROGRAM_ROOT/state"
BOOTSTRAP_LOCK="$PROGRAM_ROOT/state/managed-install.lock"
if [ -L "$BOOTSTRAP_LOCK" ] || {{ [ -e "$BOOTSTRAP_LOCK" ] && [ ! -f "$BOOTSTRAP_LOCK" ]; }}; then
  echo 'Bootstrap maintenance lock is unsafe.' >&2; exit 1
fi
exec 9<>"$BOOTSTRAP_LOCK"
if ! flock -n 9; then
  echo 'Another managed installation operation is running.' >&2; exit 1
fi
BOOTSTRAP_LEASE_OWNED=1
AVAILABLE_KIB=$(df -Pk "$PROGRAM_ROOT" | awk 'NR == 2 {{ print $4 }}')
case "$AVAILABLE_KIB" in
  ''|*[!0-9]*) echo "Could not determine free space for $PROGRAM_ROOT." >&2; exit 1 ;;
esac
REQUIRED_BYTES=$((ARTIFACT_SIZE * 6))
if [ "$REQUIRED_BYTES" -lt 3221225472 ]; then REQUIRED_BYTES=3221225472; fi
REQUIRED_KIB=$(((REQUIRED_BYTES + 1023) / 1024))
if [ "$AVAILABLE_KIB" -lt "$REQUIRED_KIB" ]; then
  echo "{product_name} bootstrap needs at least $REQUIRED_BYTES free bytes on the program volume; $((AVAILABLE_KIB * 1024)) are available." >&2
  exit 1
fi
WORK=$(mktemp -d "$PROGRAM_ROOT/.carlina-install.XXXXXXXX")
VERSION_DIR="$PROGRAM_ROOT/versions/$VERSION"
BOOTSTRAP_CLAIM_PATH="$VERSION_DIR/{BOOTSTRAP_CLAIM_NAME}"
BOOTSTRAP_BINDING_PATH="$VERSION_DIR/{BOOTSTRAP_BINDING_NAME}"
if [ -e "$VERSION_DIR" ] || [ -L "$VERSION_DIR" ]; then
  EVENT_ERROR=incompatible_release
  if read_ready_bootstrap_generation; then
    RESUMING_BOOTSTRAP=1
    BOOTSTRAP_OWNERSHIP_MODE=resume
    if [ "$SUPERVISED_PREPARATION" -eq 1 ]; then BOOTSTRAP_OWNERSHIP_MODE=prepared-resume; fi
  elif [ "$SUPERVISED_PREPARATION" -eq 1 ] && owned_bootstrap_generation &&
    PREPARATION_EXIT=$(read_preparation_completion "$PROGRAM_ROOT" "$VERSION_DIR" "$BOOTSTRAP_CLAIM_ID" "$BOOTSTRAP_IDENTITY"); then
    if [ "$PREPARATION_EXIT" -eq 0 ]; then
      PREPARATION_ALREADY_COMPLETE=1
    elif validate_stopped_preparation_recovery; then
      RECOVERING_PREPARATION=1
    else
      echo 'Retained preparation cannot be recovered by the native installer.' >&2; exit 1
    fi
  else
    echo 'Retained bootstrap generation is not ready for this installer.' >&2; exit 1
  fi
else
create_bootstrap_generation || exit 1
fi
INDEX_PATH="$WORK/release-index.json"
ARTIFACT_PATH="$WORK/$ARTIFACT_FILENAME"
TRUST_PATH="$WORK/release-trust.json"
EVENT_ERROR=download_failed
EVENT_RETRYABLE=true
installation_checkpoint download true
download_https "$INDEX_URL" "$INDEX_PATH"
EVENT_ERROR=verification_failed
EVENT_RETRYABLE=false
installation_checkpoint verification true
if [ "$(hash_file "$INDEX_PATH")" != "$INDEX_SHA256" ]; then echo "Release-index SHA-256 mismatch." >&2; exit 1; fi
EVENT_ERROR=download_failed
EVENT_RETRYABLE=true
installation_checkpoint download true
emit_installation_event running null false true 0 "$ARTIFACT_SIZE"
download_https "$ARTIFACT_URL" "$ARTIFACT_PATH" "$ARTIFACT_SIZE"
EVENT_ERROR=verification_failed
EVENT_RETRYABLE=false
installation_checkpoint verification true
ACTUAL_SIZE=$(wc -c < "$ARTIFACT_PATH" | tr -d ' ')
if [ "$ACTUAL_SIZE" != "$ARTIFACT_SIZE" ]; then echo "Release artifact size mismatch." >&2; exit 1; fi
if [ "$(hash_file "$ARTIFACT_PATH")" != "$ARTIFACT_SHA256" ]; then echo "Release artifact SHA-256 mismatch." >&2; exit 1; fi
EVENT_PHASE=download
emit_installation_event running null false true "$ARTIFACT_SIZE" "$ARTIFACT_SIZE"
EVENT_ERROR=operation_failed
decode_base64 "$TRUST_BASE64" > "$TRUST_PATH"
if [ "$RECOVERING_PREPARATION" -eq 1 ]; then
  installation_checkpoint recovery true
  installation_checkpoint recovery false
  if ! retire_stopped_preparation; then
    echo 'Retained preparation could not be retired safely.' >&2; exit 1
  fi
  create_bootstrap_generation || exit 1
fi
if [ "$RESUMING_BOOTSTRAP" -eq 0 ] && [ "$PREPARATION_ALREADY_COMPLETE" -eq 0 ]; then
EXTRACT_TMP="$WORK/extract"
mkdir "$EXTRACT_TMP"
installation_checkpoint unpacking true
installation_checkpoint unpacking false
BOOTSTRAP_NATIVE_ACTIVE=1
if [ "$SUPERVISED_PREPARATION" -eq 1 ]; then
  if [ "$ARTIFACT_KIND" = publisher-bootstrap-executable ]; then
    if run_supervised_publisher_bootstrap; then
      BOOTSTRAP_NATIVE_ACTIVE=0
    else
      UNPACK_EXIT=$?
      if [ "$PREPARATION_KNOWN" -eq 1 ]; then BOOTSTRAP_NATIVE_ACTIVE=0; fi
      exit "$UNPACK_EXIT"
    fi
  elif run_supervised_preparation; then
    BOOTSTRAP_NATIVE_ACTIVE=0
  else
    if [ "$PREPARATION_KNOWN" -eq 1 ]; then BOOTSTRAP_NATIVE_ACTIVE=0; fi
    echo 'Native preparation did not complete.' >&2; exit 1
  fi
elif [ "$EVENTS_JSONL" -eq 1 ] && [ "$ARTIFACT_KIND" = publisher-bootstrap-executable ]; then
  if run_publisher_bootstrap; then BOOTSTRAP_NATIVE_ACTIVE=0; else UNPACK_EXIT=$?; BOOTSTRAP_NATIVE_ACTIVE=0; exit "$UNPACK_EXIT"; fi
elif [ "$EVENTS_JSONL" -eq 1 ]; then
  if (cd "$VERSION_DIR" && TMPDIR="$EXTRACT_TMP" bash "$ARTIFACT_PATH") >&2; then BOOTSTRAP_NATIVE_ACTIVE=0; else UNPACK_EXIT=$?; BOOTSTRAP_NATIVE_ACTIVE=0; exit "$UNPACK_EXIT"; fi
else
  if (cd "$VERSION_DIR" && TMPDIR="$EXTRACT_TMP" bash "$ARTIFACT_PATH"); then BOOTSTRAP_NATIVE_ACTIVE=0; else UNPACK_EXIT=$?; BOOTSTRAP_NATIVE_ACTIVE=0; exit "$UNPACK_EXIT"; fi
fi
fi
EVENT_ERROR=incompatible_release
installation_checkpoint verification true
PYTHON="$VERSION_DIR/env/bin/python"
if [ ! -x "$PYTHON" ]; then echo "Unpacked runtime is missing: $PYTHON" >&2; exit 1; fi
PIXI="$VERSION_DIR/env/bin/pixi"
if [ ! -x "$PIXI" ]; then echo "Unpacked runtime is missing its bundled Pixi executable: $PIXI" >&2; exit 1; fi
if ! "$PYTHON" -I -S -c {profile_probe} "$VERSION_DIR/env" {expected_profile}; then
  echo "Bundled runtime application-profile validation failed." >&2
  exit 1
fi
installation_checkpoint environment false
BOOTSTRAP_NATIVE_ACTIVE=1
if BOOTSTRAP_ACK=$(CARLINA_PROGRAM_ROOT="$PROGRAM_ROOT" CARLINA_MANAGED_VERSION="$VERSION" \
  "$PYTHON" -I -c {ownership_probe} "$PROGRAM_ROOT" "$VERSION_DIR" "$BOOTSTRAP_CLAIM_ID" "$BOOTSTRAP_IDENTITY_BASE64" "$BOOTSTRAP_OWNERSHIP_MODE"); then
  BOOTSTRAP_NATIVE_ACTIVE=0
else
  OWNERSHIP_EXIT=$?
  BOOTSTRAP_NATIVE_ACTIVE=0
  exit "$OWNERSHIP_EXIT"
fi
if [ "$BOOTSTRAP_ACK" != "carlina.bootstrap-ready/1 $BOOTSTRAP_CLAIM_ID $BOOTSTRAP_IDENTITY" ]; then
  echo 'Bootstrap ownership acknowledgement is invalid.' >&2; exit 1
fi
if [ "$EVENTS_JSONL" -eq 1 ]; then
  installation_checkpoint environment false
  if ! "$PYTHON" -I -c {handoff_probe} probe; then exit 1; fi
  installation_checkpoint verification true
  HANDOFF_SEQUENCE=$EVENT_SEQUENCE
  TERMINAL_PATH="$WORK/installation-terminal.jsonl"
  set -- --events-jsonl --operation-id "$EVENT_ID" --event-sequence-start "$HANDOFF_SEQUENCE" --event-terminal-file "$TERMINAL_PATH"
  if [ "$RESUMING_BOOTSTRAP" -eq 1 ] || [ "$PREPARATION_ALREADY_COMPLETE" -eq 1 ]; then set -- "$@" --resume-bootstrap-claim "$BOOTSTRAP_CLAIM_ID"; fi
  if [ -n "$CANCEL_FILE" ]; then set -- "$@" --cancel-file "$CANCEL_FILE"; fi
  ADOPTION_STARTED=1
  release_bootstrap_lease
  set +e
  CARLINA_PROGRAM_ROOT="$PROGRAM_ROOT" CARLINA_MANAGED_VERSION="$VERSION" \
    "$PYTHON" -I -m {entry_module} install \
    --manifest "$INDEX_PATH" --artifact "$ARTIFACT_PATH" \
    --adopt-version-dir "$VERSION_DIR" --channel "$CHANNEL" --version "$VERSION" \
    --program-root "$PROGRAM_ROOT" --trust-store "$TRUST_PATH" \
    --expected-index-sha256 "$INDEX_SHA256" \
    "$@" </dev/null
  MANAGER_EXIT=$?
  set -e
  TERMINAL_EVENT=$("$PYTHON" -I -c {handoff_probe} read "$TERMINAL_PATH" "$EVENT_ID" {expected_profile} "$HANDOFF_SEQUENCE" "$MANAGER_EXIT") || exit 1
  cleanup
  WORK=''
  printf '%s\\n' "$TERMINAL_EVENT" || :
  exit "$MANAGER_EXIT"
fi
# Adoption can commit before its process returns to this installer.
set --
if [ "$RESUMING_BOOTSTRAP" -eq 1 ] || [ "$PREPARATION_ALREADY_COMPLETE" -eq 1 ]; then set -- --resume-bootstrap-claim "$BOOTSTRAP_CLAIM_ID"; fi
ADOPTION_STARTED=1
release_bootstrap_lease
CARLINA_PROGRAM_ROOT="$PROGRAM_ROOT" CARLINA_MANAGED_VERSION="$VERSION" \
  "$PYTHON" -I -m {entry_module} install \
  --manifest "$INDEX_PATH" --artifact "$ARTIFACT_PATH" \
  --adopt-version-dir "$VERSION_DIR" --channel "$CHANNEL" --version "$VERSION" \
  --program-root "$PROGRAM_ROOT" --trust-store "$TRUST_PATH" \
  --expected-index-sha256 "$INDEX_SHA256" "$@"
REGISTERED=1
if [ "$CREATE_PATH_LINK" -eq 1 ]; then
  USER_BIN="$HOME/.local/bin"
  mkdir -p "$USER_BIN"
  if [ ! -e "$USER_BIN/{launcher_name}" ] && [ ! -L "$USER_BIN/{launcher_name}" ]; then
    ln -s "$PROGRAM_ROOT/{launcher_name}" "$USER_BIN/{launcher_name}"
  elif [ "$(readlink "$USER_BIN/{launcher_name}" 2>/dev/null || true)" != "$PROGRAM_ROOT/{launcher_name}" ]; then
    echo "Warning: $USER_BIN/{launcher_name} already exists; the managed launcher was not linked." >&2
  fi
  case ":$PATH:" in
    *":$USER_BIN:"*) ;;
    *) echo "Add $USER_BIN to PATH to run '{launcher_name}' from a new shell." ;;
  esac
fi
if [ "$SKIP_INIT" -ne 1 ]; then "$PROGRAM_ROOT/{launcher_name}" init; fi
echo "{product_name} $VERSION is installed at $PROGRAM_ROOT"
"""


def render_installers(
    index_path: str | os.PathLike[str],
    trust_store_path: str | os.PathLike[str],
    *,
    index_url: str,
    output_dir: str | os.PathLike[str],
) -> list[Path]:
    _validate_public_https_url(index_url, "release-index")
    index_bytes = Path(index_path).expanduser().resolve().read_bytes()
    trust_path = Path(trust_store_path).expanduser().resolve()
    trust_bytes = trust_path.read_bytes()
    index = parse_release_index(index_bytes)
    verify_release_index(index, load_trusted_keys(trust_path))
    application_profile = _validated_application_profile(
        index.effective_application_profile
    )
    if len(index.releases) != 1:
        raise InstallerRenderError(
            "Bootstrap installers require a single-release index."
        )
    release = index.releases[0]
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    trust_b64 = base64.b64encode(trust_bytes).decode("ascii")
    outputs: list[Path] = []
    for artifact in release.artifacts:
        artifact_url = _resolved_url(index_url, artifact.url)
        common = {
            "version": release.version,
            "channel": index.channel,
            "index_url": index_url,
            "index_sha256": _sha256(index_bytes),
            "artifact_url": artifact_url,
            "artifact_filename": artifact.filename,
            "artifact_sha256": artifact.sha256,
            "artifact_size": artifact.size,
            "trust_b64": trust_b64,
            "application_profile": application_profile,
            "artifact_kind": artifact.kind,
            "artifact_platform": artifact.platform,
        }
        if artifact.platform.startswith("win-"):
            text = _powershell_installer(**common)
            suffix = ".ps1"
        else:
            text = _shell_installer(**common)
            suffix = ".sh"
        # Keep the bootstrap filename channel-stable so documentation can use
        # GitHub's /releases/latest/download URL without first discovering the
        # release version. Exact versions remain embedded and authenticated.
        installer_name = (
            "install-carlina-documents"
            if application_profile == RuntimeProfile.DOCUMENTS.value
            else "install-carlina"
        )
        output = destination / f"{installer_name}-{artifact.platform}{suffix}"
        output.write_text(text, encoding="utf-8", newline="\n")
        if suffix == ".sh" and os.name != "nt":
            output.chmod(0o755)
        outputs.append(output)
        sidecar = output.with_suffix(output.suffix + ".sha256")
        sidecar.write_text(
            f"{_sha256(output.read_bytes())}  {output.name}\n", encoding="ascii"
        )
        outputs.append(sidecar)
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render Carlina bootstrap installers from a signed release index."
    )
    parser.add_argument("--index", required=True)
    parser.add_argument("--trust-store", required=True)
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--output-dir", default="dist/installers")
    args = parser.parse_args(argv)
    try:
        outputs = render_installers(
            args.index,
            args.trust_store,
            index_url=args.index_url,
            output_dir=args.output_dir,
        )
    except (InstallerRenderError, OSError, ValueError) as exc:
        print(f"Installer render failed: {exc}", file=sys.stderr)
        return 1
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
