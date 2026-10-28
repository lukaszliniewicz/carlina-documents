"""Read immutable application identity embedded in a Carlina distribution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Final, cast

from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError

_MAX_DESCRIPTOR_BYTES: Final = 4096
_FORMAT: Final = "carlina.application-distribution"
_DOCUMENT_ADDONS: Final = ("doc_processor", "sentence_segmenter", "translator")
_DESCRIPTOR_KEYS: Final = frozenset(
    {
        "format",
        "schema_version",
        "application_profile",
        "source_revision",
        "source_date_epoch",
        "addon_ids",
    }
)
_INVALID_DESCRIPTOR_ERROR: Final = "Application distribution descriptor is malformed."
_READ_DESCRIPTOR_ERROR: Final = "Application distribution descriptor could not be read."


@dataclass(frozen=True, slots=True)
class ApplicationDistribution:
    """Version-bound identity data carried by an installed application package."""

    profile: RuntimeProfile
    source_revision: str | None
    source_date_epoch: int | None
    addon_ids: tuple[str, ...]


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
        result[key] = value
    return result


def parse_application_distribution(payload: bytes) -> ApplicationDistribution:
    """Parse the closed Documents descriptor without exposing its contents."""
    if len(payload) > _MAX_DESCRIPTOR_BYTES:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    try:
        decoded = payload.decode("utf-8")
        parsed_object: object = json.loads(
            decoded,
            object_pairs_hook=_unique_object_pairs,
        )
    except RuntimeProfileError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR) from None

    if not isinstance(parsed_object, dict):
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    parsed = cast(dict[str, object], parsed_object)
    if frozenset(parsed) != _DESCRIPTOR_KEYS:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)

    format_value = parsed["format"]
    if type(format_value) is not str or format_value != _FORMAT:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    schema_version = parsed["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    application_profile = parsed["application_profile"]
    if (
        type(application_profile) is not str
        or application_profile != RuntimeProfile.DOCUMENTS.value
    ):
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)

    revision = parsed["source_revision"]
    if type(revision) is not str or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    source_date_epoch = parsed["source_date_epoch"]
    if type(source_date_epoch) is not int or source_date_epoch <= 0:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)

    addon_ids = parsed["addon_ids"]
    if type(addon_ids) is not list:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    typed_addon_ids = cast(list[object], addon_ids)
    if any(type(addon_id) is not str for addon_id in typed_addon_ids):
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)
    if tuple(cast(list[str], typed_addon_ids)) != _DOCUMENT_ADDONS:
        raise RuntimeProfileError(_INVALID_DESCRIPTOR_ERROR)

    return ApplicationDistribution(
        profile=RuntimeProfile.DOCUMENTS,
        source_revision=revision,
        source_date_epoch=source_date_epoch,
        addon_ids=_DOCUMENT_ADDONS,
    )


def read_application_distribution() -> ApplicationDistribution:
    """Read the bounded package descriptor, defaulting descriptor-free builds."""
    try:
        resource = files("carlina").joinpath("distribution.json")
        with resource.open("rb") as stream:
            payload = stream.read(_MAX_DESCRIPTOR_BYTES + 1)
    except FileNotFoundError:
        return ApplicationDistribution(
            profile=RuntimeProfile.WORKBENCH,
            source_revision=None,
            source_date_epoch=None,
            addon_ids=(),
        )
    except ModuleNotFoundError, OSError, TypeError:
        raise RuntimeProfileError(_READ_DESCRIPTOR_ERROR) from None
    return parse_application_distribution(payload)


__all__ = [
    "ApplicationDistribution",
    "parse_application_distribution",
    "read_application_distribution",
]
