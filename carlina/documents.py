"""Document application entry point using Carlina's shared product engines."""

from __future__ import annotations

from typing import TYPE_CHECKING

from carlina import create_app as create_shared_app
from carlina.runtime_profiles import RuntimeProfile

if TYPE_CHECKING:
    from flask import Flask


def create_app() -> Flask:
    """Create the document composition against its separately bound data root."""

    return create_shared_app(profile=RuntimeProfile.DOCUMENTS)


def main(argv: list[str] | None = None) -> int:
    """Run the shared manager with Documents paths and service composition."""

    from carlina.bootstrap_manager import manager_main

    return manager_main(argv, profile=RuntimeProfile.DOCUMENTS)


__all__ = ["create_app", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
