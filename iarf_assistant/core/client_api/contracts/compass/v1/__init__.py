"""Compass v1 owner-scoped capture ingest and relation-contract resources.

The loaders intentionally return newly parsed dictionaries.  Contract callers
therefore cannot mutate a process-wide cached document and accidentally change
what a later validator, code generator, or route implementation sees.

Capture request/detail representations remain frozen.  The capture-relation
schema is a separate additive resource so clients can opt into evidence
topology without receiving an accidental CaptureDetail shape change.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

CAPTURE_SCHEMA_RESOURCE = "capture.schema.json"
CAPTURE_RELATION_SCHEMA_RESOURCE = "capture-relation.schema.json"
OPENAPI_RESOURCE = "openapi.yaml"


@cache
def _resource_bytes(name: str) -> bytes:
    """Read a fixed packaged contract resource once as immutable bytes."""

    return resources.files(__package__).joinpath(name).read_bytes()


def capture_schema_bytes() -> bytes:
    """Return the immutable standalone Draft 2020-12 schema bytes."""

    return _resource_bytes(CAPTURE_SCHEMA_RESOURCE)


def capture_relation_schema_bytes() -> bytes:
    """Return the immutable standalone capture-relation schema bytes."""

    return _resource_bytes(CAPTURE_RELATION_SCHEMA_RESOURCE)


def openapi_bytes() -> bytes:
    """Return the immutable OpenAPI 3.1.2 document bytes."""

    return _resource_bytes(OPENAPI_RESOURCE)


@contextmanager
def openapi_resource_path() -> Iterator[Path]:
    """Yield an OpenAPI path whose sibling schema references remain resolvable.

    ``importlib.resources.as_file`` materializes the complete contract package
    directory when the wheel is imported from a zip. Keeping the context open
    therefore preserves ``./capture.schema.json`` beside ``openapi.yaml`` for
    generic OpenAPI/JSON Schema resolvers. The yielded path must not escape the
    context manager.
    """

    with resources.as_file(resources.files(__package__)) as package_directory:
        yield package_directory / OPENAPI_RESOURCE


def load_capture_schema() -> dict[str, Any]:
    """Load a fresh mutable representation of the standalone JSON Schema."""

    return json.loads(capture_schema_bytes())


def load_capture_relation_schema() -> dict[str, Any]:
    """Load a fresh mutable relation-contract schema representation."""

    return json.loads(capture_relation_schema_bytes())


def load_openapi_document() -> dict[str, Any]:
    """Load a fresh mutable representation of the OpenAPI document."""

    document = yaml.safe_load(openapi_bytes())
    if not isinstance(document, dict):  # Defensive against a corrupted package.
        raise TypeError("Compass OpenAPI resource must contain an object.")
    return document


__all__ = [
    "CAPTURE_SCHEMA_RESOURCE",
    "CAPTURE_RELATION_SCHEMA_RESOURCE",
    "OPENAPI_RESOURCE",
    "capture_relation_schema_bytes",
    "capture_schema_bytes",
    "load_capture_relation_schema",
    "load_capture_schema",
    "load_openapi_document",
    "openapi_bytes",
    "openapi_resource_path",
]
