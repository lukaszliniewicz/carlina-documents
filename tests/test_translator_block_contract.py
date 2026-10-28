from __future__ import annotations

import hashlib

import pytest

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.doc_processor.api_client import (
    DOCUMENT_SNAPSHOT_CONTRACT_VERSION as PROVIDER_SNAPSHOT_CONTRACT_VERSION,
)
from iarf_assistant.addons.translator.block_contract import (
    DOCUMENT_SNAPSHOT_CONTRACT_VERSION,
    AttestedDocumentSnapshot,
    BlockContractError,
    SourceProviderAttestation,
)


def test_snapshot_contract_version_matches_document_provider() -> None:
    assert DOCUMENT_SNAPSHOT_CONTRACT_VERSION == PROVIDER_SNAPSHOT_CONTRACT_VERSION


def _dto(html: str = "<p>Source</p>") -> dict[str, object]:
    digest = hashlib.sha256(html.encode()).hexdigest()
    return {
        "id": "doc-1",
        "document_id": "doc-1",
        "snapshot_contract_version": DOCUMENT_SNAPSHOT_CONTRACT_VERSION,
        "status": "completed",
        "output_html": html,
        "content_hash": digest,
        "semantic_contract_version": SEMANTIC_HTML_CONTRACT_VERSION,
        "semantic_contract_mode": "completed",
        "semantic_content_hash": digest,
        "metadata": {"title": "Synthetic"},
        "provenance": {"source": "test"},
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("status", "processing", "snapshot-status"),
        ("semantic_contract_mode", "relaxed_legacy", "semantic-contract-mode"),
        ("semantic_contract_version", "old", "semantic-contract-version"),
        ("content_hash", "0" * 64, "snapshot-content-hash"),
        ("semantic_content_hash", "0" * 64, "snapshot-semantic-hash"),
        ("document_id", "other", "snapshot-id-mismatch"),
    ],
)
def test_snapshot_rejects_unattested_or_tampered_fields(
    field: str, value: str, code: str
) -> None:
    dto = _dto()
    dto[field] = value
    with pytest.raises(BlockContractError, match=f"^{code}$"):
        AttestedDocumentSnapshot.from_dict(dto)


def test_snapshot_canonical_round_trip_and_bounded_error() -> None:
    snapshot = AttestedDocumentSnapshot.from_dict(_dto())
    assert (
        AttestedDocumentSnapshot.from_dict(snapshot.to_dict()).to_dict()
        == snapshot.to_dict()
    )
    bad = _dto("<p>private source content</p>")
    bad["content_hash"] = "invalid"
    with pytest.raises(BlockContractError) as error:
        AttestedDocumentSnapshot.from_dict(bad)
    assert "private source" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", 1),
        ("id", 1),
        ("captured_at", "not-a-timestamp"),
    ],
)
def test_snapshot_bad_mapping_types_are_bounded(field: str, value: object) -> None:
    dto = _dto()
    dto[field] = value
    with pytest.raises(BlockContractError):
        AttestedDocumentSnapshot.from_dict(dto)


def test_source_attestation_is_canonical_and_rejects_boolean_version() -> None:
    attestation = SourceProviderAttestation(
        provider_addon_id=" doc-processor ",
        provider_capability_id=" document.processing ",
        owner_id=" 17 ",
        resource_id=" doc-1 ",
    )
    assert attestation.to_dict() == {
        "version": 1,
        "capability": "document.processing",
        "provider_addon_id": "doc-processor",
        "provider_capability_id": "document.processing",
        "resource_type": "document",
        "resource_id": "doc-1",
        "owner_id": "17",
    }
    with pytest.raises(BlockContractError, match="source-attestation-contract"):
        SourceProviderAttestation.from_dict(
            {**attestation.to_dict(), "version": True}
        )
