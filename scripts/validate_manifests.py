from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from iarf_assistant.core.addon_contracts import ManifestCompatibilityParser
from iarf_assistant.core.addons.contracts import (
    AddonSourceKind,
    validate_addon_contract,
)


def validate_manifest_files(
    addons_dir: Path,
    *,
    strict: bool = False,
) -> list[str]:
    parser = ManifestCompatibilityParser()
    errors: list[str] = []

    for manifest_path in sorted(addons_dir.glob("*/manifest.json")):
        rel = manifest_path.as_posix()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: invalid JSON: {exc}")
            continue

        if strict:
            validation = validate_addon_contract(
                manifest,
                addon_dir=manifest_path.parent,
                source_kind=AddonSourceKind.INSTALLED,
                strict=True,
            )
            for diagnostic in validation.diagnostics:
                location = f" ({diagnostic.path})" if diagnostic.path else ""
                errors.append(
                    f"{rel}: {diagnostic.severity} {diagnostic.code}{location}: "
                    f"{diagnostic.message}"
                )
            if validation.contract is None:
                continue

        elements = parser.parse(manifest, addon_dir=manifest_path.parent, is_active=True)
        for reason in elements.package.status.reasons:
            errors.append(f"{rel}: {reason}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate IARF addon manifest.json files.")
    parser.add_argument(
        "addons_dir",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "iarf_assistant" / "addons"),
        help="Directory containing addon folders. Defaults to iarf_assistant/addons.",
    )
    args = parser.parse_args(argv)

    addons_dir = Path(args.addons_dir)
    errors = validate_manifest_files(addons_dir, strict=True)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    count = len(list(addons_dir.glob("*/manifest.json")))
    print(f"Validated {count} addon manifest(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
