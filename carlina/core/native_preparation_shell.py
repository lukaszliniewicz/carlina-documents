"""Render opt-in Linux preparation custody without a bundled interpreter."""

from __future__ import annotations

from carlina.core.bootstrap_ownership import BOOTSTRAP_CLAIM_HEADER
from carlina.core.native_preparation import (
    MAX_PREPARATION_BYTES,
    MAX_PROGRAM_ROOT_BYTES,
    PREPARATION_HEADER,
    PREPARATION_NAME,
    PREPARATION_RETIREMENT_DIRNAME,
)

_CLAIM = r"""
preparation_claim_matches() (
  [ -d "$PP_ROOT" ] && [ ! -L "$PP_ROOT" ] || exit 1
  [ "$(cd "$PP_ROOT" && pwd -P)" = "$PP_ROOT" ] || exit 1
  [ -d "$PP_ROOT/versions" ] && [ ! -L "$PP_ROOT/versions" ] || exit 1
  [ -d "$PP_VERSION" ] && [ ! -L "$PP_VERSION" ] || exit 1
  [ "$(dirname "$PP_VERSION")" = "$PP_ROOT/versions" ] || exit 1
  case "$PP_NONCE:$PP_IDENTITY" in *[!0-9a-f:]*) exit 1 ;; esac
  [ "${#PP_NONCE}" -eq 32 ] && [ "${#PP_IDENTITY}" -eq 64 ] || exit 1
  PP_CLAIM="$PP_VERSION/.carlina-bootstrap-claim"
  [ -f "$PP_CLAIM" ] && [ ! -L "$PP_CLAIM" ] || exit 1
  [ "$(wc -c < "$PP_CLAIM" | tr -d '[:space:]')" = '@@CLAIM_SIZE@@' ] || exit 1
  PP_EXPECTED=$(printf '%s\n%s\n%s\n' '@@CLAIM_HEADER@@' "$PP_NONCE" "$PP_IDENTITY" | sha256sum | cut -d ' ' -f 1) || exit 1
  PP_ACTUAL=$(sha256sum "$PP_CLAIM" | cut -d ' ' -f 1) || exit 1
  [ "$PP_EXPECTED" = "$PP_ACTUAL" ]
)
"""

_READER = r"""
read_preparation_completion() (
  PP_ROOT=$1 PP_VERSION=$2 PP_NONCE=$3 PP_IDENTITY=$4
  preparation_claim_matches || exit 1
  PP_RECORD="$PP_VERSION/@@PREPARATION_NAME@@"
  [ -f "$PP_RECORD" ] && [ ! -L "$PP_RECORD" ] || exit 1
  PP_SIZE=$(wc -c < "$PP_RECORD" | tr -d '[:space:]') || exit 1
  [ "$PP_SIZE" -gt 0 ] && [ "$PP_SIZE" -le @@MAX_BYTES@@ ] || exit 1
  PP_BEFORE=$(stat -Lc '%d:%i' "$PP_RECORD") || exit 1
  exec 8< "$PP_RECORD" || exit 1
  [ "$PP_BEFORE" = "$(stat -Lc '%d:%i' /proc/self/fd/8)" ] || exit 1
  PP_TRAILING=''
  IFS= read -r PP_HEADER <&8 && IFS= read -r PP_CLAIM_ID <&8 &&
    IFS= read -r PP_DIGEST <&8 && IFS= read -r PP_ENCODED_ROOT <&8 &&
    IFS= read -r PP_EXIT <&8 || exit 1
  if IFS= read -r PP_TRAILING <&8; then exit 1; fi
  [ -z "$PP_TRAILING" ] || exit 1
  exec 8<&-
  [ "$PP_HEADER" = '@@PREPARATION_HEADER@@' ] &&
    [ "$PP_CLAIM_ID" = "$PP_NONCE" ] && [ "$PP_DIGEST" = "$PP_IDENTITY" ] || exit 1
  PP_ROOT_BYTES=$(printf '%s' "$PP_ROOT" | wc -c | tr -d '[:space:]') || exit 1
  [ "$PP_ROOT_BYTES" -le @@MAX_ROOT_BYTES@@ ] || exit 1
  PP_EXPECTED_ROOT=$(printf '%s' "$PP_ROOT" | base64 | tr -d '\n') || exit 1
  [ "$PP_ENCODED_ROOT" = "$PP_EXPECTED_ROOT" ] || exit 1
  case "$PP_EXIT" in ''|*[!0-9]*|0[0-9]*) exit 1 ;; esac
  [ "${#PP_EXIT}" -le 10 ] && [ "$PP_EXIT" -le 4294967295 ] || exit 1
  printf '%s\n' "$PP_EXIT"
)
"""

_ENTRY = r"""#!/bin/sh
set -eu
PP_GROUP_FILE=$1 PP_NONCE=$2 PP_UNIT=$3 PP_ARTIFACT=$4 PP_MODE=$5 PP_CANCEL=$6
PP_GROUP=''
while IFS= read -r PP_LINE; do
  case "$PP_LINE" in 0::/*) PP_GROUP=${PP_LINE#0::} ;; esac
done < /proc/self/cgroup
case "$PP_GROUP" in /*/"$PP_UNIT") ;; *) exit 1 ;; esac
case "$PP_GROUP" in *'/../'*|*'/./'*|*'//'*) exit 1 ;; esac
(umask 077; set -C; printf '%s\n%s\n%s\n' 'carlina.preparation-group/1' "$PP_NONCE" "$PP_GROUP" > "$PP_GROUP_FILE")
# Only the custodian needs the inherited maintenance lease.
exec 9>&-
trap - HUP PIPE INT TERM
case "$PP_MODE" in
  bundle) exec bash "$PP_ARTIFACT" ;;
  publisher)
    set -- --progress-records-v1
    if [ -n "$PP_CANCEL" ]; then set -- "$@" --cancel-file "$PP_CANCEL"; fi
    exec bash "$PP_ARTIFACT" "$@" ;;
  *) exit 2 ;;
esac
"""

_PUBLISHER_VALIDATOR = r"""
validate_preparation_publisher_progress() {
  # This reader never owns public events. Completion must also prove the private
  # publisher result, so a later extraction retry cannot bypass that protocol.
  LC_ALL=C awk -v native_exit="$PP_NATIVE_EXIT" '
    BEGIN { ready = 0; result = 0; invalid = 0 }
    {
      sub(/\r$/, "", $0)
      if (length($0) > 4096 || result || $0 ~ /[^ -~]/) { invalid = 1; next }
      count = split($0, field, " ")
      canonical = field[1]
      for (i = 2; i <= count; i++) canonical = canonical " " field[i]
      if ($0 != canonical || field[1] != "carlina.publisher-bootstrap/1") { invalid = 1; next }
      if (field[2] == "ready") {
        if (ready || count != 2) invalid = 1
        ready = 1
      } else if (field[2] == "phase") {
        if (!ready || count != 6 || field[3] !~ /^(preflight|download|verification|unpacking|environment)$/ ||
            field[4] !~ /^(true|false)$/) { invalid = 1; next }
        if (field[5] != "null" || field[6] != "null") {
          if (field[3] != "download" || field[5] !~ /^(0|[1-9][0-9]*)$/ ||
              field[6] !~ /^[1-9][0-9]*$/ || length(field[5]) > 15 || length(field[6]) > 15 ||
              field[5] + 0 > field[6] + 0) invalid = 1
        }
      } else if (field[2] == "result") {
        outcome = field[3] ":" field[4] ":" field[5]
        if (!ready || count != 5) invalid = 1
        if (outcome == "succeeded:null:false") { if (native_exit != 0) invalid = 1 }
        else if (outcome == "cancelled:cancelled:true") { if (native_exit != 130) invalid = 1 }
        else if (outcome ~ /^failed:(download_failed:true|verification_failed:false|operation_failed:false)$/) {
          if (native_exit == 0) invalid = 1
        } else invalid = 1
        result = 1
      } else invalid = 1
    }
    END { if (!ready || !result || invalid) exit 1 }
  ' "$PP_WORK/preparation-native.stdout" || return 1
  # awk accepts an unterminated last line; the wire protocol does not.
  [ "$(tail -c 1 "$PP_WORK/preparation-native.stdout" | od -An -tu1 | tr -d '[:space:]')" = 10 ]
}
"""

_CUSTODIAN = r"""#!/bin/sh
set -eu
umask 077
# Controller signals and a closed diagnostic stream cannot abandon preparation.
trap '' HUP PIPE INT TERM
PP_ROOT=$1 PP_VERSION=$2 PP_WORK=$3 PP_NONCE=$4 PP_IDENTITY=$5 PP_EXTRACT=$6 PP_ARTIFACT=$7
PP_MODE=${8:-bundle} PP_CANCEL=${9:-}
@@COMMON@@
@@PUBLISHER_VALIDATOR@@
preparation_claim_matches || exit 2
case "$PP_MODE:$PP_CANCEL" in bundle:) ;; publisher:|publisher:/*) ;; *) exit 2 ;; esac
[ "$(dirname "$PP_WORK")" = "$PP_ROOT" ] || exit 2
case "$(basename "$PP_WORK")" in .carlina-install.*) ;; *) exit 2 ;; esac
for PP_DIR in "$PP_ROOT/state" "$PP_WORK" "$PP_EXTRACT"; do
  [ -d "$PP_DIR" ] && [ ! -L "$PP_DIR" ] || exit 2
done
[ "$(dirname "$PP_EXTRACT")" = "$PP_WORK" ] || exit 2
[ "$(dirname "$PP_ARTIFACT")" = "$PP_WORK" ] || exit 2
[ -f "$PP_ARTIFACT" ] && [ ! -L "$PP_ARTIFACT" ] || exit 2
[ ! -L "$PP_ROOT/state/managed-install.lock" ] || exit 2
[ "$(stat -Lc '%d:%i' /proc/self/fd/9)" = "$(stat -Lc '%d:%i' "$PP_ROOT/state/managed-install.lock")" ] || exit 2
flock -n 9 || exit 2
PP_RECORD="$PP_VERSION/@@PREPARATION_NAME@@"
[ ! -e "$PP_RECORD" ] && [ ! -L "$PP_RECORD" ] || exit 2
PP_UNIT="carlina-preparation-$PP_NONCE.scope"
PP_LOAD=$(systemctl --user show "$PP_UNIT" --property=LoadState --value 2>/dev/null) || :
[ "$PP_LOAD" = not-found ] || exit 2
PP_ENTRY="$PP_WORK/preparation-entry.sh"
(set -C; cat > "$PP_ENTRY" <<'CARLINA_PREPARATION_ENTRY'
@@ENTRY@@
CARLINA_PREPARATION_ENTRY
)
PP_GROUP_FILE="$PP_WORK/preparation-group"
(
  cd "$PP_VERSION"
  export TMPDIR="$PP_EXTRACT"
  exec systemd-run --user --scope --quiet --expand-environment=no \
    --unit="$PP_UNIT" --property=KillMode=control-group -- \
    sh "$PP_ENTRY" "$PP_GROUP_FILE" "$PP_NONCE" "$PP_UNIT" "$PP_ARTIFACT" "$PP_MODE" "$PP_CANCEL"
) > "$PP_WORK/preparation-native.stdout" 2> "$PP_WORK/preparation-native.stderr" < /dev/null &
PP_LAUNCHER=$!
if wait "$PP_LAUNCHER"; then PP_NATIVE_EXIT=0; else PP_NATIVE_EXIT=$?; fi
# The direct unpacker may have exited while detached descendants still write.
[ -f "$PP_GROUP_FILE" ] && [ ! -L "$PP_GROUP_FILE" ] || exit 2
[ "$(wc -c < "$PP_GROUP_FILE" | tr -d '[:space:]')" -le 8192 ] || exit 2
{ IFS= read -r PP_GROUP_HEADER && IFS= read -r PP_GROUP_NONCE && IFS= read -r PP_GROUP; } < "$PP_GROUP_FILE" || exit 2
[ "$PP_GROUP_HEADER" = carlina.preparation-group/1 ] && [ "$PP_GROUP_NONCE" = "$PP_NONCE" ] || exit 2
case "$PP_GROUP" in /*/"$PP_UNIT") ;; *) exit 2 ;; esac
case "$PP_GROUP" in *'/../'*|*'/./'*|*'//'*) exit 2 ;; esac
PP_CGROUP="/sys/fs/cgroup$PP_GROUP"
while [ -e "$PP_CGROUP/cgroup.events" ]; do
  PP_POPULATED=$(awk '$1 == "populated" { print $2 }' "$PP_CGROUP/cgroup.events") || {
    [ ! -e "$PP_CGROUP" ] && break
    exit 2
  }
  case "$PP_POPULATED" in 0) break ;; 1) sleep 0.1 ;; *) exit 2 ;; esac
done
if [ "$PP_MODE" = publisher ]; then validate_preparation_publisher_progress || exit 2; fi
preparation_claim_matches || exit 2
PP_ROOT_BYTES=$(printf '%s' "$PP_ROOT" | wc -c | tr -d '[:space:]') || exit 2
[ "$PP_ROOT_BYTES" -le @@MAX_ROOT_BYTES@@ ] || exit 2
PP_ROOT_B64=$(printf '%s' "$PP_ROOT" | base64 | tr -d '\n') || exit 2
PP_TEMP="$PP_VERSION/@@PREPARATION_NAME@@.$PP_NONCE.tmp"
(set -C; printf '%s\n%s\n%s\n%s\n%s\n' '@@PREPARATION_HEADER@@' "$PP_NONCE" "$PP_IDENTITY" "$PP_ROOT_B64" "$PP_NATIVE_EXIT" > "$PP_TEMP") || exit 2
sync "$PP_TEMP" || exit 2
ln -T -- "$PP_TEMP" "$PP_RECORD" || exit 2
sync "$PP_RECORD" && sync "$PP_VERSION" || exit 2
rm -- "$PP_TEMP"
if [ "$PP_NATIVE_EXIT" -eq 0 ]; then exit 0; else exit 1; fi
"""

_PARENT = r"""
check_preparation_capability() {
  [ "$(uname -s)" = Linux ] || return 1
  for PP_TOOL in systemd-run systemctl stat flock sha256sum sync ln; do
    command -v "$PP_TOOL" >/dev/null 2>&1 || return 1
  done
  [ -r /sys/fs/cgroup/cgroup.controllers ] && [ -r /proc/self/cgroup ] || return 1
  PP_SYSTEMD=$(systemd-run --version | head -n 1 | cut -d ' ' -f 2) || return 1
  case "$PP_SYSTEMD" in ''|*[!0-9]*) return 1 ;; esac
  [ "$PP_SYSTEMD" -ge 254 ] || return 1
  systemctl --user show --property=Version --value >/dev/null 2>&1
}
run_supervised_preparation() {
  PREPARATION_KNOWN=0
  PP_MODE=${1:-bundle} PP_CANCEL=${2:-}
  if [ "$PP_MODE" = publisher ]; then
    (umask 077; set -C; : > "$WORK/preparation-native.stdout") || return 1
  fi
  PP_SCRIPT="$WORK/preparation-custodian.sh"
  (umask 077; set -C; cat > "$PP_SCRIPT" <<'CARLINA_PREPARATION_CUSTODIAN'
@@CUSTODIAN@@
CARLINA_PREPARATION_CUSTODIAN
  ) || return 1
  # Both processes retain the same open-file-description lock. Parent loss does
  # not release the custodian's copy, and normal handoff keeps the original.
  sh "$PP_SCRIPT" "$PROGRAM_ROOT" "$VERSION_DIR" "$WORK" "$BOOTSTRAP_CLAIM_ID" \
    "$BOOTSTRAP_IDENTITY" "$EXTRACT_TMP" "$ARTIFACT_PATH" "$PP_MODE" "$PP_CANCEL" \
    > "$WORK/preparation-custodian.stdout" 2> "$WORK/preparation-custodian.stderr" < /dev/null &
  PREPARATION_PID=$!
  if [ "$PP_MODE" = publisher ]; then
    # The tail is an observer only. Its lifetime/exit never proves custody. The
    # owned wait and validated completion below remain the authority.
    tail --pid="$PREPARATION_PID" --sleep-interval=0.1 -n +1 -f \
      "$WORK/preparation-native.stdout" 9>&- &
    PP_OBSERVER=$!
  fi
  if wait "$PREPARATION_PID"; then PREPARATION_HOST_EXIT=0; else PREPARATION_HOST_EXIT=$?; fi
  if [ "$PP_MODE" = publisher ]; then wait "$PP_OBSERVER" || return 1; fi
  PREPARATION_EXIT=$(read_preparation_completion "$PROGRAM_ROOT" "$VERSION_DIR" "$BOOTSTRAP_CLAIM_ID" "$BOOTSTRAP_IDENTITY") || return 1
  if [ "$PREPARATION_EXIT" -eq 0 ]; then
    [ "$PREPARATION_HOST_EXIT" -eq 0 ] || return 1
  else
    [ "$PREPARATION_HOST_EXIT" -eq 1 ] || return 1
  fi
  PREPARATION_KNOWN=1
  if [ "$PP_MODE" = publisher ]; then
    # Private IPC from this owned waiter, independent of untrusted stdout. The
    # outer shell validates it again after the public-progress pipe finishes.
    (umask 077; set -C; printf '%s\n' "$PREPARATION_EXIT" > "$WORK/preparation-observed") || return 1
    printf 'carlina.publisher-process/1 exit %s\n' "$PREPARATION_EXIT"
  fi
  [ "$PREPARATION_EXIT" -eq 0 ]
}
"""

_RECOVERY = r"""
validate_stopped_preparation_recovery() (
  [ "$BOOTSTRAP_LEASE_OWNED" -eq 1 ] || exit 1
  PP_ROOT=$PROGRAM_ROOT PP_VERSION=$VERSION_DIR PP_NONCE=$BOOTSTRAP_CLAIM_ID PP_IDENTITY=$BOOTSTRAP_IDENTITY
  preparation_claim_matches || exit 1
  [ -d "$PP_ROOT/state" ] && [ ! -L "$PP_ROOT/state" ] || exit 1
  RP_LOCK="$PP_ROOT/state/managed-install.lock"
  [ -f "$RP_LOCK" ] && [ ! -L "$RP_LOCK" ] || exit 1
  [ "$(stat -Lc '%d:%i' /proc/self/fd/9)" = "$(stat -Lc '%d:%i' "$RP_LOCK")" ] || exit 1
  flock -n 9 || exit 1
  RP_EXIT=$(read_preparation_completion "$PP_ROOT" "$PP_VERSION" "$PP_NONCE" "$PP_IDENTITY") || exit 1
  [ "$RP_EXIT" -ne 0 ] || exit 1
  for RP_MARKER in "$PP_ROOT/current" "$PP_ROOT/previous" \
      "$PP_VERSION/.carlina-bootstrap-binding.json" \
      "$PP_VERSION/.carlina-bootstrap-preparation-binding.json"; do
    [ ! -e "$RP_MARKER" ] && [ ! -L "$RP_MARKER" ] || exit 1
  done
  # Native recovery ends before managed adoption. Unknown future state belongs
  # to the manager too; do not parse a second recovery journal before Python.
  for RP_STATE in "$PP_ROOT/state"/* "$PP_ROOT/state"/.[!.]* "$PP_ROOT/state"/..?*; do
    [ -e "$RP_STATE" ] || [ -L "$RP_STATE" ] || continue
    case "$(basename "$RP_STATE")" in
      managed-install.lock) ;;
      @@RETIREMENT_DIRECTORY@@)
        [ -d "$RP_STATE" ] && [ ! -L "$RP_STATE" ] || exit 1
        [ "$(stat -Lc '%a' "$RP_STATE")" = 700 ] && [ "$(stat -Lc '%u' "$RP_STATE")" = "$(id -u)" ] || exit 1
        RP_DESTINATION="$RP_STATE/$(basename "$PP_VERSION").$PP_NONCE"
        [ ! -e "$RP_DESTINATION" ] && [ ! -L "$RP_DESTINATION" ] || exit 1 ;;
      *) exit 1 ;;
    esac
  done
  for RP_GENERATION in "$PP_ROOT/versions"/* "$PP_ROOT/versions"/.[!.]* "$PP_ROOT/versions"/..?*; do
    [ -e "$RP_GENERATION" ] || [ -L "$RP_GENERATION" ] || continue
    [ -d "$RP_GENERATION" ] && [ ! -L "$RP_GENERATION" ] || exit 1
    RP_RECEIPT="$RP_GENERATION/release-receipt.json"
    [ ! -e "$RP_RECEIPT" ] && [ ! -L "$RP_RECEIPT" ] || exit 1
  done
)
retire_stopped_preparation() {
  validate_stopped_preparation_recovery || return 1
  # Refuse platforms whose mv could silently copy across filesystems.
  mv --no-copy --no-clobber --no-target-directory --help >/dev/null 2>&1 || return 1
  RP_SOURCE_ID=$(stat -Lc '%d:%i' "$VERSION_DIR") || return 1
  RP_CLAIM_HASH=$(sha256sum "$VERSION_DIR/.carlina-bootstrap-claim" | cut -d ' ' -f 1) || return 1
  RP_PROOF_HASH=$(sha256sum "$VERSION_DIR/@@PREPARATION_NAME@@" | cut -d ' ' -f 1) || return 1
  RP_BASE="$PROGRAM_ROOT/state/@@RETIREMENT_DIRECTORY@@"
  if [ ! -e "$RP_BASE" ] && [ ! -L "$RP_BASE" ]; then
    (umask 077; mkdir -- "$RP_BASE") || return 1
    sync "$PROGRAM_ROOT/state" || return 1
  fi
  [ -d "$RP_BASE" ] && [ ! -L "$RP_BASE" ] || return 1
  [ "$(stat -Lc '%a' "$RP_BASE")" = 700 ] || return 1
  [ "$(stat -Lc '%u' "$RP_BASE")" = "$(id -u)" ] || return 1
  RETIRED_PREPARATION="$RP_BASE/$(basename "$VERSION_DIR").$BOOTSTRAP_CLAIM_ID"
  validate_stopped_preparation_recovery || return 1
  [ ! -e "$RETIRED_PREPARATION" ] && [ ! -L "$RETIRED_PREPARATION" ] || return 1
  [ "$(stat -Lc '%d:%i' "$VERSION_DIR")" = "$RP_SOURCE_ID" ] || return 1
  mv --no-copy --no-clobber --no-target-directory -- "$VERSION_DIR" "$RETIRED_PREPARATION" || return 1
  [ ! -e "$VERSION_DIR" ] && [ ! -L "$VERSION_DIR" ] || return 1
  [ -d "$RETIRED_PREPARATION" ] && [ ! -L "$RETIRED_PREPARATION" ] || return 1
  [ "$(stat -Lc '%d:%i' "$RETIRED_PREPARATION")" = "$RP_SOURCE_ID" ] || return 1
  [ "$(sha256sum "$RETIRED_PREPARATION/.carlina-bootstrap-claim" | cut -d ' ' -f 1)" = "$RP_CLAIM_HASH" ] || return 1
  [ "$(sha256sum "$RETIRED_PREPARATION/@@PREPARATION_NAME@@" | cut -d ' ' -f 1)" = "$RP_PROOF_HASH" ] || return 1
  sync "$PROGRAM_ROOT/versions" && sync "$RP_BASE"
}
"""


def _tokens(source: str) -> str:
    return (
        source.replace("@@CLAIM_HEADER@@", BOOTSTRAP_CLAIM_HEADER)
        .replace("@@CLAIM_SIZE@@", str(len(BOOTSTRAP_CLAIM_HEADER) + 99))
        .replace("@@PREPARATION_NAME@@", PREPARATION_NAME)
        .replace("@@PREPARATION_HEADER@@", PREPARATION_HEADER)
        .replace("@@MAX_BYTES@@", str(MAX_PREPARATION_BYTES))
        .replace("@@MAX_ROOT_BYTES@@", str(MAX_PROGRAM_ROOT_BYTES))
        .replace("@@RETIREMENT_DIRECTORY@@", PREPARATION_RETIREMENT_DIRNAME)
    )


def render_shell_preparation_custodian() -> str:
    """Render the separate Linux process that owns the lease and user scope."""

    return _tokens(
        _CUSTODIAN.replace("@@COMMON@@", _CLAIM)
        .replace("@@PUBLISHER_VALIDATOR@@", _PUBLISHER_VALIDATOR)
        .replace("@@ENTRY@@", _ENTRY)
    )


def render_shell_preparation_helpers() -> str:
    """Render lazy carrier adapters without changing its ordinary execution."""

    return _tokens(
        _CLAIM
        + _READER
        + _RECOVERY
        + _PARENT.replace("@@CUSTODIAN@@", render_shell_preparation_custodian())
    )
