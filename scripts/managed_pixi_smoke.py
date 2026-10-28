"""Environment-resident import and version probe for managed Pixi validation."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path
from typing import Any

SMOKE_MARKER = "CARLINA_MANAGED_PIXI_SMOKE="


def run_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    imported: list[str] = []
    origins: dict[str, str] = {}
    for module_name in payload["imports"]:
        try:
            module = importlib.import_module(module_name)
            imported.append(module_name)
            origin = getattr(module, "__file__", None)
            if isinstance(origin, str):
                origins[module_name] = str(Path(origin).resolve())
        except Exception as exc:  # noqa: BLE001 - report arbitrary import failures.
            errors.append(
                f"could not import {module_name!r}: {type(exc).__name__}: {exc}"
            )

    unexpected: list[str] = []
    for module_name in payload["forbidden_imports"]:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name and not str(exc.name or "").startswith(
                module_name + "."
            ):
                errors.append(
                    f"forbidden module {module_name!r} was found but its import "
                    f"failed on {exc.name!r}"
                )
        except Exception as exc:  # noqa: BLE001 - distinguish found/broken modules.
            errors.append(
                f"forbidden module {module_name!r} was found but raised "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            unexpected.append(module_name)
            errors.append(f"forbidden module {module_name!r} is importable")

    versions: dict[str, str] = {}
    for distribution, expected_prefix in payload["version_prefixes"].items():
        try:
            version = importlib.metadata.version(distribution)
            versions[distribution] = version
            if not version.startswith(expected_prefix):
                errors.append(
                    f"{distribution} resolved to {version!r}; expected prefix "
                    f"{expected_prefix!r}"
                )
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"distribution {distribution!r} is not installed")

    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != payload["python_version"]:
        errors.append(
            f"interpreter is Python {actual_python}; expected "
            f"{payload['python_version']}"
        )
    tools = {
        executable: shutil.which(executable)
        for executable in payload.get("executables", [])
    }
    for executable, location in tools.items():
        if location is None:
            errors.append(f"native executable {executable!r} is unavailable")
    if payload.get("require_prefix_origins"):
        prefix = Path(sys.prefix).resolve()
        for module_name, origin in origins.items():
            if not Path(origin).is_relative_to(prefix):
                errors.append(
                    f"module {module_name!r} resolved outside the managed prefix: {origin}"
                )
        for executable, location in tools.items():
            if location is not None and not Path(location).resolve().is_relative_to(prefix):
                errors.append(
                    f"native executable {executable!r} resolved outside the managed prefix: {location}"
                )
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "prefix": sys.prefix,
        "python_minor": actual_python,
        "imports": imported,
        "origins": origins,
        "tools": tools,
        "forbidden_imports_found": unexpected,
        "versions": versions,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print(
            "managed_pixi_smoke.py requires one JSON payload argument", file=sys.stderr
        )
        return 2
    try:
        payload = json.loads(arguments[0])
    except json.JSONDecodeError as exc:
        print(f"invalid managed Pixi smoke payload: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("managed Pixi smoke payload must be an object", file=sys.stderr)
        return 2
    result = run_smoke(payload)
    print(SMOKE_MARKER + json.dumps(result, sort_keys=True))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
