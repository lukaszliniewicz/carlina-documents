"""Canonical setup and server-launch entry points."""

from __future__ import annotations

from iarf_assistant.bootstrap import setup_main, start_main

__all__ = ["setup_main", "start_main"]


if __name__ == "__main__":
    raise SystemExit(setup_main())
