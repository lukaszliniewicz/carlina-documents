"""Bootstrap a Pixi worker with an explicitly declared host-runtime bridge."""

from __future__ import annotations

import argparse
import runpy
import site
import sys
from collections.abc import Sequence
from pathlib import Path


def run_managed_worker(
    script: str | Path,
    *,
    host_site_paths: Sequence[str | Path] = (),
    script_args: Sequence[str] = (),
) -> None:
    """Run a worker after appending trusted host site-package directories.

    The Pixi interpreter initializes its own site-packages before this function
    runs. ``site.addsitedir`` therefore appends the host runtime instead of
    allowing it to shadow dependencies declared by the add-on environment.
    """

    script_path = Path(script).expanduser().resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"Managed worker entrypoint not found: {script_path}")

    for raw_path in host_site_paths:
        site_path = Path(raw_path).expanduser().resolve()
        if not site_path.is_dir():
            raise FileNotFoundError(
                f"Managed worker host-runtime path not found: {site_path}"
            )
        site.addsitedir(str(site_path))

    previous_argv = sys.argv
    try:
        sys.argv = [str(script_path), *(str(value) for value in script_args)]
        runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = previous_argv


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a Carlina managed worker with a host-runtime bridge."
    )
    parser.add_argument("--host-site", action="append", default=[])
    parser.add_argument("script")
    parser.add_argument("script_args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    run_managed_worker(
        parsed.script,
        host_site_paths=parsed.host_site,
        script_args=parsed.script_args,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
