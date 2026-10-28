"""Owner-neutral contract for exporting an exact semantic document.

This module deliberately knows nothing about add-ons, databases, Flask, files,
or upload locations.  A provider validates its own resource ownership and
materialises bytes only after this boundary has accepted the immutable request.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from bs4 import BeautifulSoup, Comment

from .epub_export import (
    EpubExportContractError,
    epub_json_value,
    freeze_epub_value,
    normalize_epub_image_mode,
    normalize_epub_options,
    normalize_epub_quality,
)
from .semantic_diagrams import diagnose_mermaid_diagrams
from .semantic_html import (
    MAX_INLINE_IMAGE_CHARS,
    SAFE_CONTROL_RE,
    SAFE_DATA_IMAGE_RE,
    SAFE_IDENTIFIER_RE,
    SAFE_LINK_SCHEMES,
    SEMANTIC_DOCUMENT_TAGS,
    SEMANTIC_DROP_CONTENT_TAGS,
    SEMANTIC_HTML_CONTRACT_VERSION,
    allowed_attributes,
)
from .semantic_images import diagnose_image_descriptions

SEMANTIC_EXPORT_CONTRACT_VERSION = "semantic-export-v1"
SEMANTIC_EXPORT_FORMATS = frozenset({"html", "epub", "docx", "pdf"})
MAX_SEMANTIC_EXPORT_HTML_BYTES = 20 * 1024 * 1024
MAX_SEMANTIC_EXPORT_METADATA_BYTES = 16 * 1024
MAX_SEMANTIC_EXPORT_RESOURCES = 512
MAX_SEMANTIC_EXPORT_RESOURCE_BYTES = 25 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE_REF_RE = re.compile(r"^res_[A-Za-z0-9_-]{16,128}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SEMANTIC_RESOURCE_PREFIX = ".carlina-resources/"


class SemanticExportContractError(ValueError):
    """Bounded validation failure that contains no document content."""

    def __init__(self, code: str):
        self.code = (
            code
            if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", code)
            else "semantic-export-invalid"
        )
        super().__init__(self.code)


def default_include_source_toc(export_format: str) -> bool:
    """Return the explicit projection default for one supported format."""

    if export_format not in SEMANTIC_EXPORT_FORMATS:
        raise SemanticExportContractError("semantic-export-format")
    return export_format in {"html", "docx", "pdf"}


def opaque_resource_path(ref: str) -> str:
    """Return the inert relative semantic-HTML reference for one resource."""

    if not _RESOURCE_REF_RE.fullmatch(ref):
        raise SemanticExportContractError("semantic-export-resource-ref")
    return f"{SEMANTIC_RESOURCE_PREFIX}{ref}"


def parse_opaque_resource_path(value: object) -> str | None:
    """Return one exact opaque resource ref, never a path-like approximation."""

    if not isinstance(value, str) or not value.startswith(SEMANTIC_RESOURCE_PREFIX):
        return None
    ref = value.removeprefix(SEMANTIC_RESOURCE_PREFIX)
    return ref if _RESOURCE_REF_RE.fullmatch(ref) else None


def _string(value: object, code: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise SemanticExportContractError(code)
    return value


def _hash(value: object, code: str) -> str:
    value = _string(value, code, maximum=64)
    if not _HASH_RE.fullmatch(value):
        raise SemanticExportContractError(code)
    return value


def _json_value(value: object, *, depth: int = 0) -> Any:
    if depth > 4:
        raise SemanticExportContractError("semantic-export-metadata-depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 4096:
            raise SemanticExportContractError("semantic-export-metadata-value")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 1_000_000_000:
            raise SemanticExportContractError("semantic-export-metadata-value")
        return value
    if isinstance(value, float):
        raise SemanticExportContractError("semantic-export-metadata-value")
    if isinstance(value, list):
        if len(value) > 64:
            raise SemanticExportContractError("semantic-export-metadata-items")
        return tuple(_json_value(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise SemanticExportContractError("semantic-export-metadata-items")
        copied = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _PROFILE_RE.fullmatch(key):
                raise SemanticExportContractError("semantic-export-metadata-key")
            copied[key] = _json_value(item, depth=depth + 1)
        return MappingProxyType(copied)
    raise SemanticExportContractError("semantic-export-metadata-value")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _safe_link_value(value: str) -> bool:
    if not value or value.strip() != value or SAFE_CONTROL_RE.search(value):
        return False
    if value.startswith("//") or "\\" in value:
        return False
    if value.startswith("#"):
        return len(value) > 1 and bool(SAFE_IDENTIFIER_RE.fullmatch(value[1:]))
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    return not parsed.scheme or parsed.scheme.lower() in SAFE_LINK_SCHEMES


def validate_semantic_export_html(html: str) -> None:
    """Reject unsafe semantic HTML without repairing or serializing it.

    The structural/note validator remains provider-specific. This host-level
    guard prevents an exact HTML projection from becoming an XSS or network
    transport merely because no conversion happens for the HTML format.
    """

    soup = BeautifulSoup(html, "html.parser")
    if diagnose_image_descriptions(soup):
        raise SemanticExportContractError("semantic-export-image-description")
    if diagnose_mermaid_diagrams(soup):
        raise SemanticExportContractError("semantic-export-diagram")
    if soup.find(string=lambda value: isinstance(value, Comment)) is not None:
        raise SemanticExportContractError("semantic-export-html-safety")
    for tag in soup.find_all(True):
        name = str(tag.name or "").casefold()
        if name in SEMANTIC_DROP_CONTENT_TAGS or name not in SEMANTIC_DOCUMENT_TAGS:
            raise SemanticExportContractError("semantic-export-html-safety")
        allowed = allowed_attributes(name)
        for raw_name, raw_value in tag.attrs.items():
            attr = str(raw_name or "").casefold()
            value = (
                " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            )
            if (
                not attr
                or attr not in allowed
                or attr.startswith("on")
                or attr in {"style", "srcdoc", "formaction"}
                or SAFE_CONTROL_RE.search(value)
            ):
                raise SemanticExportContractError("semantic-export-html-safety")
            if attr in {"href", "cite"} and not _safe_link_value(value):
                raise SemanticExportContractError("semantic-export-html-safety")
            if attr == "src":
                if name != "img":
                    raise SemanticExportContractError("semantic-export-html-safety")
                if value.startswith("data:"):
                    if len(
                        value
                    ) > MAX_INLINE_IMAGE_CHARS or not SAFE_DATA_IMAGE_RE.fullmatch(
                        value
                    ):
                        raise SemanticExportContractError("semantic-export-html-safety")
                elif parse_opaque_resource_path(value) is None:
                    raise SemanticExportContractError("semantic-export-html-safety")


@dataclass(frozen=True, slots=True)
class SemanticExportResource:
    """An opaque, owner-attested immutable resource required by the target."""

    ref: str
    media_type: str
    filename: str
    byte_length: int
    sha256: str
    owner_attestation: str

    def __post_init__(self) -> None:
        if not _RESOURCE_REF_RE.fullmatch(
            _string(self.ref, "semantic-export-resource-ref", maximum=132)
        ):
            raise SemanticExportContractError("semantic-export-resource-ref")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9.+-]{0,126}",
            _string(self.media_type, "semantic-export-resource-media", maximum=255),
        ):
            raise SemanticExportContractError("semantic-export-resource-media")
        if not _SAFE_FILENAME_RE.fullmatch(
            _string(self.filename, "semantic-export-resource-filename", maximum=128)
        ):
            raise SemanticExportContractError("semantic-export-resource-filename")
        if (
            not isinstance(self.byte_length, int)
            or isinstance(self.byte_length, bool)
            or not 0 < self.byte_length <= MAX_SEMANTIC_EXPORT_RESOURCE_BYTES
        ):
            raise SemanticExportContractError("semantic-export-resource-size")
        _hash(self.sha256, "semantic-export-resource-hash")
        _hash(self.owner_attestation, "semantic-export-resource-attestation")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SemanticExportResource:
        if not isinstance(value, Mapping) or set(value) != {
            "ref",
            "media_type",
            "filename",
            "byte_length",
            "sha256",
            "owner_attestation",
        }:
            raise SemanticExportContractError("semantic-export-resource-keys")
        return cls(
            ref=cast(str, value["ref"]),
            media_type=cast(str, value["media_type"]),
            filename=cast(str, value["filename"]),
            byte_length=cast(int, value["byte_length"]),
            sha256=cast(str, value["sha256"]),
            owner_attestation=cast(str, value["owner_attestation"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "media_type": self.media_type,
            "filename": self.filename,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "owner_attestation": self.owner_attestation,
        }


@dataclass(frozen=True, slots=True)
class SemanticExportRequest:
    """Exact semantic target and projection policy for one export."""

    semantic_html: str
    semantic_html_sha256: str
    metadata: Mapping[str, Any]
    resources: tuple[SemanticExportResource, ...]
    format: str
    include_source_toc: bool
    profile_id: str
    profile_version: str
    contract_version: str = SEMANTIC_EXPORT_CONTRACT_VERSION
    semantic_contract_version: str = SEMANTIC_HTML_CONTRACT_VERSION
    start_h2_on_new_page: bool = False
    epub_options: Mapping[str, Any] | None = None
    epub_image_mode: str = "auto"

    def __post_init__(self) -> None:
        if self.contract_version != SEMANTIC_EXPORT_CONTRACT_VERSION:
            raise SemanticExportContractError("semantic-export-contract-version")
        if self.semantic_contract_version != SEMANTIC_HTML_CONTRACT_VERSION:
            raise SemanticExportContractError("semantic-export-html-contract")
        if (
            not isinstance(self.semantic_html, str)
            or not self.semantic_html.strip()
            or len(self.semantic_html.encode("utf-8")) > MAX_SEMANTIC_EXPORT_HTML_BYTES
        ):
            raise SemanticExportContractError("semantic-export-html")
        if (
            _hash(self.semantic_html_sha256, "semantic-export-html-hash")
            != hashlib.sha256(self.semantic_html.encode("utf-8")).hexdigest()
        ):
            raise SemanticExportContractError("semantic-export-html-hash")
        validate_semantic_export_html(self.semantic_html)
        if self.format not in SEMANTIC_EXPORT_FORMATS:
            raise SemanticExportContractError("semantic-export-format")
        if type(self.start_h2_on_new_page) is not bool or (
            self.start_h2_on_new_page and self.format not in {"docx", "pdf"}
        ):
            raise SemanticExportContractError("semantic-export-start-h2")
        if not isinstance(self.include_source_toc, bool):
            raise SemanticExportContractError("semantic-export-source-toc")
        try:
            epub_options = normalize_epub_options(self.epub_options)
            image_mode = normalize_epub_image_mode(self.epub_image_mode)
        except EpubExportContractError as exc:
            raise SemanticExportContractError(exc.code) from exc
        if image_mode != "auto" and self.format != "epub":
            raise SemanticExportContractError("semantic-export-epub-image-mode-format")
        if epub_options and self.format != "epub":
            raise SemanticExportContractError("semantic-export-epub-options-format")
        object.__setattr__(self, "epub_options", freeze_epub_value(epub_options))
        if not _PROFILE_RE.fullmatch(
            _string(self.profile_id, "semantic-export-profile")
        ) or not _PROFILE_RE.fullmatch(
            _string(self.profile_version, "semantic-export-profile-version")
        ):
            raise SemanticExportContractError("semantic-export-profile")
        if (
            not isinstance(self.resources, tuple)
            or len(self.resources) > MAX_SEMANTIC_EXPORT_RESOURCES
            or not all(
                isinstance(item, SemanticExportResource) for item in self.resources
            )
        ):
            raise SemanticExportContractError("semantic-export-resources")
        if len({item.ref for item in self.resources}) != len(self.resources):
            raise SemanticExportContractError("semantic-export-resource-duplicate")
        frozen_metadata = _json_value(self.metadata)
        if not isinstance(frozen_metadata, Mapping):
            raise SemanticExportContractError("semantic-export-metadata")
        if (
            len(
                json.dumps(
                    _plain(frozen_metadata),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > MAX_SEMANTIC_EXPORT_METADATA_BYTES
        ):
            raise SemanticExportContractError("semantic-export-metadata-size")
        object.__setattr__(self, "metadata", frozen_metadata)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SemanticExportRequest:
        required = {
            "contract_version",
            "semantic_contract_version",
            "semantic_html",
            "semantic_html_sha256",
            "metadata",
            "resources",
            "format",
            "include_source_toc",
            "profile_id",
            "profile_version",
        }
        if (
            not isinstance(value, Mapping)
            or not required
            <= set(value)
            <= required | {"start_h2_on_new_page", "epub_options", "epub_image_mode"}
            or not isinstance(value.get("resources"), list)
        ):
            raise SemanticExportContractError("semantic-export-request-keys")
        resources_value = value.get("resources")
        if not isinstance(resources_value, list):
            raise SemanticExportContractError("semantic-export-resources")
        resources = tuple(
            SemanticExportResource.from_dict(item)
            for item in resources_value
            if isinstance(item, Mapping)
        )
        if len(resources) != len(resources_value):
            raise SemanticExportContractError("semantic-export-resources")
        return cls(
            semantic_html=cast(str, value["semantic_html"]),
            semantic_html_sha256=cast(str, value["semantic_html_sha256"]),
            metadata=cast(Mapping[str, Any], value["metadata"]),
            resources=resources,
            format=cast(str, value["format"]),
            include_source_toc=cast(bool, value["include_source_toc"]),
            profile_id=cast(str, value["profile_id"]),
            profile_version=cast(str, value["profile_version"]),
            contract_version=cast(str, value["contract_version"]),
            semantic_contract_version=cast(str, value["semantic_contract_version"]),
            start_h2_on_new_page=cast(bool, value.get("start_h2_on_new_page", False)),
            epub_options=cast(Mapping[str, Any] | None, value.get("epub_options")),
            epub_image_mode=cast(str, value.get("epub_image_mode", "auto")),
        )

    def to_dict(self) -> dict[str, object]:
        payload = {
            "contract_version": self.contract_version,
            "semantic_contract_version": self.semantic_contract_version,
            "semantic_html": self.semantic_html,
            "semantic_html_sha256": self.semantic_html_sha256,
            "metadata": _plain(self.metadata),
            "resources": [item.to_dict() for item in self.resources],
            "format": self.format,
            "include_source_toc": self.include_source_toc,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
        }
        if self.start_h2_on_new_page:
            payload["start_h2_on_new_page"] = True
        if self.epub_options:
            payload["epub_options"] = epub_json_value(self.epub_options)
        if self.epub_image_mode != "auto":
            payload["epub_image_mode"] = self.epub_image_mode
        return payload


@dataclass(frozen=True, slots=True)
class SemanticExportArtifact:
    """Source-free result receipt; bytes remain an implementation concern."""

    format: str
    media_type: str
    byte_length: int
    sha256: str
    validation_profile: str
    validation_profile_version: str
    quality: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if (
            self.format not in SEMANTIC_EXPORT_FORMATS
            or not isinstance(self.byte_length, int)
            or self.byte_length < 0
        ):
            raise SemanticExportContractError("semantic-export-artifact")
        _hash(self.sha256, "semantic-export-artifact-hash")
        _string(self.media_type, "semantic-export-artifact-media", maximum=255)
        _string(
            self.validation_profile, "semantic-export-artifact-profile", maximum=128
        )
        _string(
            self.validation_profile_version,
            "semantic-export-artifact-profile-version",
            maximum=128,
        )
        if self.quality is not None:
            try:
                quality = normalize_epub_quality(self.quality)
            except EpubExportContractError as exc:
                raise SemanticExportContractError(exc.code) from exc
            if self.format != "epub" or quality["artifact_sha256"] != self.sha256:
                raise SemanticExportContractError("semantic-export-quality-artifact")
            object.__setattr__(self, "quality", freeze_epub_value(quality))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "format": self.format,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "validation_profile": self.validation_profile,
            "validation_profile_version": self.validation_profile_version,
        }
        if self.quality is not None:
            result["quality"] = epub_json_value(self.quality)
        return result
