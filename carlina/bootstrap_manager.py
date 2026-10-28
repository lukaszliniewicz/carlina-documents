"""Canonical managed-installation command module."""

from __future__ import annotations

from iarf_assistant.bootstrap_manager import manager_main

__all__ = ["manager_main"]


if __name__ == "__main__":
    raise SystemExit(manager_main())
