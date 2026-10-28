#!/usr/bin/env python3
"""Build the core Tailwind stylesheet with the pinned standalone CLI."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


TAILWIND_VERSION = "4.3.0"

ASSETS = {
    ("linux", "x64"): (
        "tailwindcss-linux-x64",
        "73f0e5459054e5cfaa8ab6f3b940f3fbe0f13cc7fd83bc24e7c655033c203400",
    ),
    ("linux", "arm64"): (
        "tailwindcss-linux-arm64",
        "8f48dcb72be3b351c10563c5329b4638ba8516820dc3b3a1609625a166e87cbd",
    ),
    ("darwin", "x64"): (
        "tailwindcss-macos-x64",
        "2ba252f770817091e6d0d12a84e0dd531bcc29aad1bfd9d976a3aff1a071b67a",
    ),
    ("darwin", "arm64"): (
        "tailwindcss-macos-arm64",
        "56b4bbc62dbdc4614a78930d9c6986423a2ec63e4e640144a59a5d95c914322e",
    ),
    ("windows", "x64"): (
        "tailwindcss-windows-x64.exe",
        "e4303d06a337899d426583508f55c143ce89a585f90ed080f482d02fd83093c1",
    ),
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "iarf_assistant" / "static"
DEFAULT_INPUT = STATIC_DIR / "tailwind.css"
DEFAULT_OUTPUT = STATIC_DIR / "output.css"
DEFAULT_BIN_DIR = PROJECT_ROOT / "scripts" / ".bin"


def _normalize_system() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "darwin"
    if system == "linux":
        return "linux"
    return system


def _normalize_machine() -> str:
    machine = platform.machine().lower().replace("_", "-")
    if machine in {"amd64", "x86-64", "x86_64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine


def _asset_for_current_platform() -> tuple[str, str]:
    system = _normalize_system()
    machine = _normalize_machine()
    asset = ASSETS.get((system, machine))
    if not asset:
        supported = ", ".join(f"{os_name}/{arch}" for os_name, arch in sorted(ASSETS))
        raise SystemExit(
            f"Unsupported Tailwind CLI platform: {system}/{machine}. Supported: {supported}."
        )
    return asset


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name, suffix=".download", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temp_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        temp_path.replace(destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _ensure_cli(force: bool = False) -> Path:
    asset_name, expected_sha = _asset_for_current_platform()
    cli_path = DEFAULT_BIN_DIR / f"tailwindcss-v{TAILWIND_VERSION}-{asset_name}"
    if cli_path.exists() and not force:
        actual_sha = _sha256(cli_path)
        if actual_sha == expected_sha:
            return cli_path
        cli_path.unlink()

    url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{TAILWIND_VERSION}/{asset_name}"
    print(f"Downloading Tailwind CSS CLI v{TAILWIND_VERSION}: {asset_name}", flush=True)
    _download(url, cli_path)
    actual_sha = _sha256(cli_path)
    if actual_sha != expected_sha:
        cli_path.unlink(missing_ok=True)
        raise SystemExit(
            f"Downloaded Tailwind CLI checksum mismatch. Expected {expected_sha}, got {actual_sha}."
        )

    if _normalize_system() != "windows":
        cli_path.chmod(cli_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return cli_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the core Tailwind CSS output.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input CSS file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output CSS file.")
    parser.add_argument("--watch", action="store_true", help="Run Tailwind in watch mode.")
    parser.add_argument("--minify", action="store_true", help="Minify the generated CSS.")
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Download the pinned Tailwind CLI even if a verified copy exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    input_path = args.input.resolve()
    output_path = args.output.resolve()

    if not input_path.exists():
        raise SystemExit(f"Tailwind input file does not exist: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path = _ensure_cli(force=args.force_download)

    temp_root = None
    command_output_path = output_path
    if not args.watch:
        temp_root = Path(tempfile.mkdtemp(prefix="tailwind-output-", dir=str(output_path.parent)))
        command_output_path = temp_root / "dist" / output_path.name

    command = [str(cli_path), "-i", str(input_path), "-o", str(command_output_path)]
    if args.minify:
        command.append("--minify")
    if args.watch:
        command.append("--watch")

    print("Running:", " ".join(command), flush=True)
    try:
        exit_code = subprocess.call(command, cwd=PROJECT_ROOT)
        if exit_code == 0 and temp_root is not None:
            command_output_path.replace(output_path)
        return exit_code
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
