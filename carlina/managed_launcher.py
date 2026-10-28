"""Bind an optional data root before running a managed Carlina entry point."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from collections.abc import Sequence
from pathlib import Path

_ENTRY_MODULES = ("carlina.documents", "carlina.bootstrap_manager")
_INVALID_CONFIGURATION = "Invalid managed launcher configuration."


class ManagedLauncherError(Exception):
    """Raised when a managed entry point or data-root binding is invalid."""


def _read_data_root(data_root_file: Path) -> str:
    try:
        value = data_root_file.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError, UnicodeError:
        raise ManagedLauncherError(_INVALID_CONFIGURATION) from None

    if (
        not value
        or "\r" in value
        or "\n" in value
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise ManagedLauncherError(_INVALID_CONFIGURATION)
    return value


def run_managed_entrypoint(
    entry_module: str,
    *,
    data_root_file: Path | None,
    argv: Sequence[str],
) -> None:
    """Run one supported entry module with its optional exact data-root binding."""
    if entry_module not in _ENTRY_MODULES:
        raise ManagedLauncherError(_INVALID_CONFIGURATION)

    if data_root_file is not None:
        os.environ["CARLINA_HOME"] = _read_data_root(data_root_file)

    original_argv = sys.argv
    sys.argv = [entry_module, *argv]
    try:
        runpy.run_module(entry_module, run_name="__main__")
    finally:
        sys.argv = original_argv


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the managed launcher arguments and dispatch the selected entry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-module", choices=_ENTRY_MODULES, required=True)
    parser.add_argument("--data-root-file", type=Path)
    parser.add_argument("entry_args", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)

    entry_args = arguments.entry_args
    if entry_args and entry_args[0] == "--":
        entry_args = entry_args[1:]

    try:
        run_managed_entrypoint(
            arguments.entry_module,
            data_root_file=arguments.data_root_file,
            argv=entry_args,
        )
    except ManagedLauncherError:
        print(_INVALID_CONFIGURATION, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
