"""Render native publisher bootstraps from an already validated download plan."""

from __future__ import annotations

import base64
import hashlib
import shlex
from typing import Final

from carlina.core.native_download_scripts import (
    render_powershell_download_helpers,
    render_shell_download_helpers,
)
from carlina.core.publisher_bootstrap import (
    PublisherBootstrapError,
    PublisherBootstrapPlan,
    parse_publisher_bootstrap_plan,
    publisher_bootstrap_plan_bytes,
)

PUBLISHER_BOOTSTRAP_ARTIFACT_KIND: Final = "publisher-bootstrap-executable"
MAX_EMBEDDED_SKELETON_BYTES: Final = 64 * 1024 * 1024

_LINUX_TEMPLATE: Final = r"""#!/usr/bin/env bash
# Generated Carlina Documents publisher bootstrap. Requires internet access.
set -Eeuo pipefail
umask 077
ARCHIVE_SIZE=@@ARCHIVE_SIZE@@
ARCHIVE_SHA256=@@ARCHIVE_SHA256@@
PAYLOAD_LINE=@@PAYLOAD_LINE@@
WORK=''
BOOTSTRAP_NATIVE_ACTIVE=0
BOOTSTRAP_DOWNLOAD_PID=''
PROGRESS_RECORDS_V1=false
CANCEL_ENABLED=false
CANCEL_FILE=''
CANCEL_REQUESTED=false
READY_EMITTED=false
FAILURE_CODE=operation_failed
FAILURE_RETRYABLE=false
usage_error() {
  echo 'Usage: setup.sh [--progress-records-v1 [--cancel-file PATH]]' >&2
  exit 2
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --progress-records-v1)
      if [ "$PROGRESS_RECORDS_V1" = true ]; then usage_error; fi
      PROGRESS_RECORDS_V1=true
      shift
      ;;
    --cancel-file)
      if [ "$CANCEL_ENABLED" = true ] || [ "$#" -lt 2 ] || [ -z "$2" ]; then
        usage_error
      fi
      CANCEL_ENABLED=true
      CANCEL_FILE=$2
      shift 2
      ;;
    *) usage_error ;;
  esac
done
if [ "$CANCEL_ENABLED" = true ] && [ "$PROGRESS_RECORDS_V1" != true ]; then
  usage_error
fi
if [ "$PROGRESS_RECORDS_V1" = true ]; then
  exec 3>&1 1>&2
fi
emit_record() {
  if [ "$PROGRESS_RECORDS_V1" = true ]; then
    (trap '' PIPE; printf '%s\n' "$1" >&3) 2>/dev/null || true
  fi
}
emit_phase() {
  emit_record "carlina.publisher-bootstrap/1 phase $1 $2 $3 $4"
}
emit_ready() {
  if [ "$PROGRESS_RECORDS_V1" = true ] && [ "$READY_EMITTED" != true ]; then
    emit_record 'carlina.publisher-bootstrap/1 ready'
    READY_EMITTED=true
  fi
}
validate_cancel_metadata() {
  local parent
  [ "$CANCEL_ENABLED" = true ] || return 0
  [ -n "$CANCEL_FILE" ] || return 1
  if [[ "$CANCEL_FILE" == */* ]]; then
    parent=${CANCEL_FILE%/*}
    [ -n "$parent" ] || parent=/
  else
    parent=.
  fi
  if [ ! -d "$parent" ] || [ ! -x "$parent" ]; then
    return 1
  fi
  if [ -L "$CANCEL_FILE" ]; then return 1; fi
  if [ -e "$CANCEL_FILE" ] && { [ -d "$CANCEL_FILE" ] || [ ! -f "$CANCEL_FILE" ]; }; then
    return 1
  fi
  return 0
}
check_download_cancellation() {
  if ! validate_cancel_metadata; then
    FAILURE_CODE=operation_failed
    FAILURE_RETRYABLE=false
    echo 'The cancellation request path is invalid.' >&2
    exit 1
  fi
  if [ "$CANCEL_ENABLED" = true ] && [ -e "$CANCEL_FILE" ]; then
    CANCEL_REQUESTED=true
    exit 130
  fi
}
report_download_progress() {
  emit_phase download true "$1" "$2"
}
mark_download_size_failure() {
  FAILURE_CODE=download_failed
  FAILURE_RETRYABLE=true
}
safe_checkpoint() {
  local phase=$1 completed=$2 total=$3
  emit_phase "$phase" true "$completed" "$total"
  check_download_cancellation
}
@@DOWNLOAD_HELPERS@@
begin_step() { emit_phase "$1" false "$2" "$3"; }
finish_bootstrap() {
  local status=$?
  local state=failed code=$FAILURE_CODE retryable=$FAILURE_RETRYABLE
  trap - EXIT INT TERM
  stop_bootstrap_download
  if [ "$BOOTSTRAP_NATIVE_ACTIVE" -eq 1 ]; then
    state=failed
    code=operation_failed
    retryable=false
    status=1
  elif [ -n "$WORK" ]; then
    if ! rm -rf -- "$WORK"; then
      state=failed
      code=operation_failed
      retryable=false
      status=1
    fi
    WORK=''
  fi
  if [ "$PROGRESS_RECORDS_V1" = true ] && [ "$READY_EMITTED" = true ] && [ "$BOOTSTRAP_NATIVE_ACTIVE" -ne 1 ]; then
    if [ "$CANCEL_REQUESTED" = true ] && [ "$status" -eq 130 ]; then
      state=cancelled
      code=cancelled
      retryable=true
      status=130
    elif [ "$status" -eq 0 ]; then
      state=succeeded
      code=null
      retryable=false
    fi
    emit_record "carlina.publisher-bootstrap/1 result $state $code $retryable"
  fi
  exit "$status"
}
trap finish_bootstrap EXIT
trap 'FAILURE_CODE=operation_failed; FAILURE_RETRYABLE=false; exit 130' INT
trap 'FAILURE_CODE=operation_failed; FAILURE_RETRYABLE=false; exit 143' TERM
if [ "$PROGRESS_RECORDS_V1" = true ]; then
  emit_ready
  safe_checkpoint preflight null null
  begin_step preflight null null
fi
for tool in curl base64 gzip dd head tail wc tr mktemp cut chmod rm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Runtime setup requires $tool." >&2
    exit 1
  fi
done
if [ "$PROGRESS_RECORDS_V1" = true ] && ! command -v prlimit >/dev/null 2>&1; then
  echo 'Runtime setup requires prlimit for progress-mode downloads.' >&2
  exit 1
fi
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d ' ' -f 1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d ' ' -f 1
  else echo 'Runtime setup requires a SHA-256 tool.' >&2; return 1; fi
}
assert_file() {
  local path=$1 size=$2 expected=$3
  FAILURE_CODE=verification_failed
  FAILURE_RETRYABLE=false
  if [ "$(wc -c < "$path" | tr -d ' ')" != "$size" ]; then
    echo 'A runtime input has the wrong size. Setup was stopped.' >&2
    return 1
  fi
  if [ "$(hash_file "$path")" != "$expected" ]; then
    echo 'A runtime input failed SHA-256 verification. Setup was stopped.' >&2
    return 1
  fi
}
download_file() {
  local url=$1 path=$2 size=$3 expected=$4
  FAILURE_CODE=download_failed
  FAILURE_RETRYABLE=true
  if [ "$PROGRESS_RECORDS_V1" = true ]; then
    safe_checkpoint download 0 "$size"
    if ! download_https_event "$url" "$path" "$size"; then
      echo 'Dependency download failed. Check the internet connection and retry setup.' >&2
      return 1
    fi
  else
    begin_step download 0 "$size"
    if ! curl --fail --location --silent --show-error --proto '=https' \
        --proto-redir '=https' --tlsv1.2 --connect-timeout 30 --max-time 900 \
        --max-redirs 8 --max-filesize "$size" "$url" \
        | head -c "$((size + 1))" > "$path"; then
      echo 'Dependency download failed. Check the internet connection and retry setup.' >&2
      return 1
    fi
  fi
  begin_step verification null null
  assert_file "$path" "$size" "$expected"
  safe_checkpoint download "$size" "$size"
}
download_package() {
  local url=$1 size=$2 expected=$3 offset=$4
  local path="$WORK/package-$expected"
  download_file "$url" "$path" "$size" "$expected"
  begin_step unpacking null null
  FAILURE_CODE=operation_failed
  FAILURE_RETRYABLE=false
  dd if="$path" of="$WORK/archive.tar" bs=512 seek="$((offset / 512))" conv=notrunc 2>/dev/null
  rm -- "$path"
  safe_checkpoint unpacking null null
}
if [ "$PROGRESS_RECORDS_V1" = true ]; then
  safe_checkpoint preflight null null
  begin_step preflight null null
fi
if [ -e env ] || [ -L env ]; then
  FAILURE_CODE=operation_failed
  echo 'Runtime setup requires a new version directory.' >&2
  exit 1
fi
WORK=$(mktemp -d "$PWD/.carlina-publisher.XXXXXXXX")
begin_step verification null null
FAILURE_CODE=verification_failed
FAILURE_RETRYABLE=false
tail -n +"$PAYLOAD_LINE" -- "$0" | base64 --decode > "$WORK/skeleton.tar.gz"
assert_file "$WORK/skeleton.tar.gz" @@SKELETON_SIZE@@ @@SKELETON_SHA256@@
safe_checkpoint verification null null
begin_step unpacking null null
gzip -dc -- "$WORK/skeleton.tar.gz" | head -c "$((ARCHIVE_SIZE + 1))" > "$WORK/archive.tar"
if [ "$(wc -c < "$WORK/archive.tar" | tr -d ' ')" != "$ARCHIVE_SIZE" ]; then
  FAILURE_CODE=verification_failed
  FAILURE_RETRYABLE=false
  echo 'The bundled runtime scaffold has the wrong size.' >&2
  exit 1
fi
safe_checkpoint unpacking null null
echo 'Preparing the Carlina Documents runtime. Internet access is required.'
@@PACKAGES@@
begin_step verification null null
assert_file "$WORK/archive.tar" "$ARCHIVE_SIZE" "$ARCHIVE_SHA256"
safe_checkpoint verification null null
download_file @@UNPACKER_URL@@ "$WORK/pixi-unpack" @@UNPACKER_SIZE@@ @@UNPACKER_SHA256@@
begin_step environment null null
FAILURE_CODE=operation_failed
FAILURE_RETRYABLE=false
chmod 700 "$WORK/pixi-unpack"
"$WORK/pixi-unpack" "$WORK/archive.tar"
safe_checkpoint environment null null
exit 0
"""

_WINDOWS_TEMPLATE: Final = r"""# Generated Carlina Documents publisher bootstrap. Requires internet access.
[CmdletBinding()]
param(
    [switch]$ProgressRecordsV1,
    [string]$CancelFile
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$archiveSize = [long]@@ARCHIVE_SIZE@@
$archiveSha256 = '@@ARCHIVE_SHA256@@'
$work = $null
$script:ProgressRecordsV1 = [bool]$ProgressRecordsV1
$script:CancelFileSupplied = $PSBoundParameters.ContainsKey('CancelFile')
$script:CancelFileRaw = $CancelFile
$script:CancelPath = $null
$script:CancelRequested = $false
$script:ReadyEmitted = $false
$script:FailureCode = 'operation_failed'
$script:FailureRetryable = $false
$script:ExitCode = 1
$script:ResultState = 'failed'
$script:ResultCode = 'operation_failed'
$script:ResultRetryable = $false

if ($script:CancelFileSupplied -and (-not $ProgressRecordsV1 -or [string]::IsNullOrEmpty($CancelFile))) {
    [Console]::Error.WriteLine('Invalid publisher bootstrap arguments.')
    exit 2
}
if ($MyInvocation.UnboundArguments.Count -gt 0) {
    [Console]::Error.WriteLine('Invalid publisher bootstrap arguments.')
    exit 2
}

function Write-ProgressRecord([string]$Line) {
    if (-not $script:ProgressRecordsV1) { return }
    try { [Console]::Out.WriteLine($Line) } catch { }
}

function Write-Phase([string]$Phase, [bool]$Safe, [string]$Completed, [string]$Total) {
    $safeField = if ($Safe) { 'true' } else { 'false' }
    Write-ProgressRecord "carlina.publisher-bootstrap/1 phase $Phase $safeField $Completed $Total"
}

function Write-SetupDiagnostic([string]$Message) {
    if ($script:ProgressRecordsV1) { [Console]::Error.WriteLine($Message) }
    else { Write-Host $Message }
}

function Get-CancelRequestState {
    if (-not $script:CancelFileSupplied) { return $false }
    $script:FailureCode = 'operation_failed'
    $script:FailureRetryable = $false
    try {
        $script:CancelPath = [IO.Path]::GetFullPath($script:CancelFileRaw)
        $parent = [IO.Path]::GetDirectoryName($script:CancelPath)
        if ([string]::IsNullOrEmpty($parent)) { throw 'Invalid cancellation parent.' }
        $parentItem = Get-Item -LiteralPath $parent -Force -ErrorAction Stop
        if (-not $parentItem.PSIsContainer) { throw 'Invalid cancellation parent.' }
        try {
            $item = Get-Item -LiteralPath $script:CancelPath -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            return $false
        }
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Invalid cancellation request metadata.'
        }
        return $true
    } catch {
        $script:FailureCode = 'operation_failed'
        $script:FailureRetryable = $false
        throw 'Invalid cancellation request metadata.'
    }
}

function Safe-Checkpoint([string]$Phase, [string]$Completed, [string]$Total) {
    Write-Phase $Phase $true $Completed $Total
    if (Get-CancelRequestState) {
        $script:CancelRequested = $true
        throw [OperationCanceledException]::new('Cooperative cancellation requested.')
    }
}

function Begin-Step([string]$Phase, [string]$Completed, [string]$Total) {
    Write-Phase $Phase $false $Completed $Total
}

function Invoke-DownloadCancellationCheck {
    if (-not $script:ProgressRecordsV1) { return }
    $previousFailureCode = $script:FailureCode
    $previousFailureRetryable = $script:FailureRetryable
    if (Get-CancelRequestState) {
        $script:CancelRequested = $true
        throw [OperationCanceledException]::new('Cooperative cancellation requested.')
    }
    $script:FailureCode = $previousFailureCode
    $script:FailureRetryable = $previousFailureRetryable
}

function Send-DownloadProgress([long]$Completed, [long]$Total) {
    Write-Phase 'download' $true ([string]$Completed) ([string]$Total)
}

function Set-DownloadSizeFailure {
    $script:FailureCode = 'download_failed'
    $script:FailureRetryable = $true
}

@@DOWNLOAD_HELPERS@@

function Get-PublisherResponse([Uri]$Uri) {
    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.AllowAutoRedirect = $false
    $request.UseDefaultCredentials = $false
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    $request.UserAgent = 'Carlina-Documents-publisher-bootstrap/1'
    return $request.GetResponse()
}

function Assert-VerifiedFile([string]$Path, [long]$Size, [string]$Sha256) {
    $script:FailureCode = 'verification_failed'
    $script:FailureRetryable = $false
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne $Size) { throw 'A runtime input has the wrong size. Setup was stopped.' }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256) { throw 'A runtime input failed SHA-256 verification. Setup was stopped.' }
}

function Save-PublisherFile([string]$Url, [string]$Path, [long]$Size, [string]$Sha256) {
    $script:FailureCode = 'download_failed'
    $script:FailureRetryable = $true
    if ($script:ProgressRecordsV1) {
        Safe-Checkpoint download '0' ([string]$Size)
        $script:FailureCode = 'download_failed'
        $script:FailureRetryable = $true
        Save-HttpsEventFile -Url $Url -Path $Path -ExpectedSize $Size
        Begin-Step verification 'null' 'null'
        Assert-VerifiedFile $Path $Size $Sha256
        Safe-Checkpoint download ([string]$Size) ([string]$Size)
        return
    }
    Begin-Step download '0' ([string]$Size)
    $uri = [Uri]$Url
    $deadline = [DateTime]::UtcNow.AddSeconds(900)
    for ($redirect = 0; $redirect -le 8; $redirect++) {
        if ($uri.Scheme -ne 'https' -or $uri.UserInfo) { throw 'Dependency downloads require HTTPS without credentials.' }
        $response = $null
        $inputStream = $null
        $outputStream = $null
        try {
            $response = Get-PublisherResponse $uri
            $status = [int]$response.StatusCode
            if ($status -in @(301, 302, 303, 307, 308)) {
                $location = $response.Headers['Location']
                if (-not $location) { throw 'A dependency redirect has no destination.' }
                $uri = [Uri]::new($uri, $location)
                continue
            }
            if ($status -ne 200) { throw 'A dependency publisher did not return the requested file.' }
            if ($response.ContentLength -ge 0 -and $response.ContentLength -ne $Size) {
                throw 'A dependency publisher returned an unexpected file size.'
            }
            $inputStream = $response.GetResponseStream()
            $outputStream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $buffer = New-Object byte[] 65536
            $total = [long]0
            while ($true) {
                if ([DateTime]::UtcNow -gt $deadline) { throw 'A dependency download timed out.' }
                $count = $inputStream.Read($buffer, 0, [int][Math]::Min($buffer.Length, $Size - $total + 1))
                if ($count -eq 0) { break }
                $total += $count
                if ($total -gt $Size) { throw 'A dependency download exceeded its expected size.' }
                $outputStream.Write($buffer, 0, $count)
            }
            if ($total -ne $Size) { throw 'A dependency download ended before its expected size.' }
            $outputStream.Flush($true)
        } finally {
            if ($null -ne $outputStream) { $outputStream.Dispose() }
            if ($null -ne $inputStream) { $inputStream.Dispose() }
            if ($null -ne $response) { $response.Dispose() }
        }
        Begin-Step verification 'null' 'null'
        Assert-VerifiedFile $Path $Size $Sha256
        Safe-Checkpoint download ([string]$Size) ([string]$Size)
        return
    }
    throw 'A dependency download exceeded the HTTPS redirect limit.'
}

function Expand-Skeleton([string]$Source, [string]$Destination, [long]$Size) {
    $script:FailureCode = 'verification_failed'
    $script:FailureRetryable = $false
    $inputStream = [IO.File]::OpenRead($Source)
    $gzip = $null
    $outputStream = $null
    try {
        $gzip = New-Object IO.Compression.GZipStream($inputStream, [IO.Compression.CompressionMode]::Decompress)
        $outputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = New-Object byte[] 65536
        $total = [long]0
        while ($true) {
            $count = $gzip.Read($buffer, 0, [int][Math]::Min($buffer.Length, $Size - $total + 1))
            if ($count -eq 0) { break }
            $total += $count
            if ($total -gt $Size) { throw 'The bundled runtime scaffold exceeds its expected size.' }
            $outputStream.Write($buffer, 0, $count)
        }
        if ($total -ne $Size) { throw 'The bundled runtime scaffold has the wrong size.' }
        $outputStream.Flush($true)
    } finally {
        if ($null -ne $outputStream) { $outputStream.Dispose() }
        if ($null -ne $gzip) { $gzip.Dispose() }
        $inputStream.Dispose()
    }
}

function Invoke-NativeUnpackerProcess([string]$UnpackerPath, [string]$ArchivePath) {
    & $UnpackerPath $ArchivePath
}

function Invoke-PublisherUnpacker([string]$UnpackerPath, [string]$ArchivePath) {
    $previousPreference = $ErrorActionPreference
    $global:LASTEXITCODE = $null
    try {
        $ErrorActionPreference = 'Continue'
        if ($script:ProgressRecordsV1) {
            Invoke-NativeUnpackerProcess $UnpackerPath $ArchivePath 2>&1 | ForEach-Object {
                try { [Console]::Error.WriteLine([string]$_) } catch { }
            }
        } else {
            Invoke-NativeUnpackerProcess $UnpackerPath $ArchivePath 2>&1 | ForEach-Object { [string]$_ }
        }
        $global:PublisherUnpackExitCode = $global:LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

if ($script:ProgressRecordsV1) {
    Write-ProgressRecord 'carlina.publisher-bootstrap/1 ready'
    $script:ReadyEmitted = $true
}

try {
    if ($script:ProgressRecordsV1) {
        Safe-Checkpoint preflight 'null' 'null'
        Begin-Step preflight 'null' 'null'
    }
    if ((Get-Location).Provider.Name -ne 'FileSystem') { throw 'Runtime setup requires a filesystem directory.' }
    $destination = (Get-Location).ProviderPath
    if (Test-Path -LiteralPath (Join-Path $destination 'env')) { throw 'Runtime setup requires a new version directory.' }
    $work = Join-Path $destination ('.carlina-publisher-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    $skeletonPath = Join-Path $work 'skeleton.tar.gz'
    Begin-Step verification 'null' 'null'
    $script:FailureCode = 'verification_failed'
    $script:FailureRetryable = $false
    [IO.File]::WriteAllBytes($skeletonPath, [Convert]::FromBase64String('@@SKELETON_BASE64@@'))
    Assert-VerifiedFile $skeletonPath @@SKELETON_SIZE@@ '@@SKELETON_SHA256@@'
    Safe-Checkpoint verification 'null' 'null'
    $archivePath = Join-Path $work 'archive.tar'
    Begin-Step unpacking 'null' 'null'
    Expand-Skeleton $skeletonPath $archivePath $archiveSize
    Safe-Checkpoint unpacking 'null' 'null'
    $packages = @(
@@PACKAGES@@
    )
    Write-SetupDiagnostic 'Preparing the Carlina Documents runtime. Internet access is required.'
    $script:FailureCode = 'operation_failed'
    $script:FailureRetryable = $false
    $archiveStream = [IO.File]::Open($archivePath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        foreach ($package in $packages) {
            $path = Join-Path $work ('package-' + $package.Sha256)
            Save-PublisherFile $package.Url $path $package.Size $package.Sha256
            Begin-Step unpacking 'null' 'null'
            $script:FailureCode = 'operation_failed'
            $script:FailureRetryable = $false
            $inputStream = [IO.File]::OpenRead($path)
            try {
                [void]$archiveStream.Seek($package.Offset, [IO.SeekOrigin]::Begin)
                $inputStream.CopyTo($archiveStream)
            } finally {
                $inputStream.Dispose()
            }
            Remove-Item -LiteralPath $path -Force
            $null = $archiveStream.Flush($true)
            Safe-Checkpoint unpacking 'null' 'null'
        }
        $archiveStream.Flush($true)
    } finally {
        $archiveStream.Dispose()
    }
    Begin-Step verification 'null' 'null'
    Assert-VerifiedFile $archivePath $archiveSize $archiveSha256
    Safe-Checkpoint verification 'null' 'null'
    $unpackerPath = Join-Path $work 'pixi-unpack.exe'
    Save-PublisherFile @@UNPACKER_URL@@ $unpackerPath @@UNPACKER_SIZE@@ '@@UNPACKER_SHA256@@'
    Begin-Step environment 'null' 'null'
    $script:FailureCode = 'operation_failed'
    $script:FailureRetryable = $false
    Invoke-PublisherUnpacker $unpackerPath $archivePath
    $unpackExitCode = $global:PublisherUnpackExitCode
    if ($null -eq $unpackExitCode -or $unpackExitCode -ne 0) { throw "Runtime unpack failed. Exit code: $unpackExitCode" }
    Safe-Checkpoint environment 'null' 'null'
    $script:ResultState = 'succeeded'
    $script:ResultCode = 'null'
    $script:ResultRetryable = $false
    $script:ExitCode = 0
} catch {
    if ($script:CancelRequested) {
        $script:ResultState = 'cancelled'
        $script:ResultCode = 'cancelled'
        $script:ResultRetryable = $true
        $script:ExitCode = 130
    } else {
        $script:ResultState = 'failed'
        $script:ResultCode = $script:FailureCode
        $script:ResultRetryable = $script:FailureCode -eq 'download_failed'
        $script:ExitCode = 1
        if ($script:ProgressRecordsV1) {
            [Console]::Error.WriteLine('Publisher bootstrap operation failed.')
        } else {
            Write-Error $_ -ErrorAction Continue
        }
    }
} finally {
    if ($null -ne $work -and (Test-Path -LiteralPath $work)) {
        try { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction Stop }
        catch {
            $script:ResultState = 'failed'
            $script:ResultCode = 'operation_failed'
            $script:ResultRetryable = $false
            $script:ExitCode = 1
        }
    }
    if ($script:ProgressRecordsV1 -and $script:ReadyEmitted) {
        Write-ProgressRecord "carlina.publisher-bootstrap/1 result $script:ResultState $script:ResultCode $($script:ResultRetryable.ToString().ToLowerInvariant())"
    }
}
exit $script:ExitCode
"""


def publisher_bootstrap_filename(plan: PublisherBootstrapPlan) -> str:
    validated = parse_publisher_bootstrap_plan(publisher_bootstrap_plan_bytes(plan))
    suffix = ".ps1" if validated.platform == "win-64" else ".sh"
    return f"carlina-documents-{validated.version}-{validated.platform}{suffix}"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_publisher_bootstrap(plan: PublisherBootstrapPlan, skeleton: bytes) -> bytes:
    """Compile fixed verified inputs; the native script never parses caller JSON."""

    validated = parse_publisher_bootstrap_plan(publisher_bootstrap_plan_bytes(plan))
    if len(skeleton) > MAX_EMBEDDED_SKELETON_BYTES:
        raise PublisherBootstrapError("The embedded publisher scaffold is too large.")
    if (
        len(skeleton) != validated.skeleton.size
        or hashlib.sha256(skeleton).hexdigest() != validated.skeleton.sha256
    ):
        raise PublisherBootstrapError("The publisher scaffold does not match its plan.")
    encoded = base64.b64encode(skeleton).decode("ascii")
    common = {
        "@@ARCHIVE_SIZE@@": str(validated.archive.size),
        "@@ARCHIVE_SHA256@@": validated.archive.sha256,
        "@@SKELETON_SIZE@@": str(validated.skeleton.size),
        "@@SKELETON_SHA256@@": validated.skeleton.sha256,
        "@@UNPACKER_SIZE@@": str(validated.unpacker.size),
        "@@UNPACKER_SHA256@@": validated.unpacker.sha256,
    }
    if validated.platform == "linux-64":
        text = _LINUX_TEMPLATE
        common["@@DOWNLOAD_HELPERS@@"] = render_shell_download_helpers(publisher=True)
        common["@@UNPACKER_URL@@"] = shlex.quote(validated.unpacker.url)
        common["@@PACKAGES@@"] = "\n".join(
            f"download_package {shlex.quote(package.url)} {package.size} "
            f"{package.sha256} {package.offset_data}"
            for package in validated.downloads
        )
    else:
        text = _WINDOWS_TEMPLATE
        common["@@DOWNLOAD_HELPERS@@"] = render_powershell_download_helpers(
            publisher=True
        )
        common["@@UNPACKER_URL@@"] = _powershell_quote(validated.unpacker.url)
        common["@@SKELETON_BASE64@@"] = encoded
        common["@@PACKAGES@@"] = "\n".join(
            "        @{Url="
            + _powershell_quote(package.url)
            + f";Size=[long]{package.size};Sha256='{package.sha256}';"
            + f"Offset=[long]{package.offset_data}"
            + "}"
            for package in validated.downloads
        )
    for token, value in common.items():
        text = text.replace(token, value)
    if validated.platform == "linux-64":
        text = text.replace("@@PAYLOAD_LINE@@", str(text.count("\n") + 2))
        text += "__CARLINA_PUBLISHER_PAYLOAD__\n" + encoded + "\n"
    return text.encode("utf-8")
