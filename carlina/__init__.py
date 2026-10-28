"""Canonical public Python package for the Carlina workbench."""

from __future__ import annotations

from typing import TYPE_CHECKING

from carlina.runtime_profiles import RuntimeProfile

if TYPE_CHECKING:
    from flask import Flask


def create_app(*, profile: RuntimeProfile | str | None = None) -> Flask:
    """Create and configure the Carlina Flask application."""

    # The implementation is moving bottom-up from the compatibility package.
    # Import lazily so importing the public package remains dependency-light.
    from iarf_assistant.app import _create_app

    if profile is None:
        return _create_app()
    return _create_app(profile=profile)


__all__ = ["create_app"]
