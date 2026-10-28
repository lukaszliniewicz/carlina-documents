# pyright: strict
"""Lazy imports for the optional sqlite-vec vector backend."""

from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import Final

SQLITE_VEC_AVAILABLE: Final[bool] = find_spec("sqlite_vec") is not None


def numpy_module() -> ModuleType:
    """Load NumPy only when vector operations need it."""

    return import_module("numpy")


def sqlite_vec_module() -> ModuleType:
    """Load sqlite-vec only when vector operations need it."""

    return import_module("sqlite_vec")
