"""Explicit preparation command; importing or booting never downloads a model."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .sat_assets import SatAssetsError, prepare_sat_assets, verified_sat_assets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the pinned native SaT model.")
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="Verify without downloading."
    )
    args = parser.parse_args(argv)
    root = args.cache_root
    if not isinstance(root, Path):
        parser.error("A model cache root is required.")
    try:
        assets = verified_sat_assets(root) if args.check else prepare_sat_assets(root)
    except SatAssetsError as error:
        print(str(error))
        return 1
    print(f"Pinned SaT model is ready: {assets.fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
