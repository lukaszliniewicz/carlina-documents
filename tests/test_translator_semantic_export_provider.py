import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iarf_assistant.addons.translator.source_providers import (
    ResolvedSourceProvider,
    capability_provider_reference,
    capability_provider_reference_from_settings,
    resolve_capability_provider,
    resolve_source_provider,
)
from iarf_assistant.core.addons.contracts import validate_addon_contract

ROOT = Path(__file__).resolve().parents[1]
TRANSLATOR_MANIFEST = (
    ROOT / "iarf_assistant" / "addons" / "translator" / "manifest.json"
)


class ProviderContext:
    def __init__(self):
        self.calls = []
        self.client = object()

    def get_capability_provider(self, requirement_id, user_id, **selection):
        self.calls.append((requirement_id, user_id, selection))

        def initialize_client(config):
            self.calls.append(("initialize", config))
            return self.client

        return SimpleNamespace(
            provider_addon_id="doc_processor",
            provider_capability_id="semantic_export",
            initialize_client=initialize_client,
        )


def test_translator_declares_optional_user_scoped_semantic_export_requirement():
    manifest = json.loads(TRANSLATOR_MANIFEST.read_text(encoding="utf-8"))
    result = validate_addon_contract(
        manifest,
        addon_dir=TRANSLATOR_MANIFEST.parent,
        strict=False,
    )

    assert not result.errors
    requirement = next(
        item
        for item in manifest["requires"]["capabilities"]
        if item["id"] == "semantic_export"
    )
    assert requirement == {
        "id": "semantic_export",
        "capability": "document.semantic_export",
        "version": ">=1,<2",
        "operations": ["export"],
        "cardinality": "optional",
        "activation": "on_first_use",
        "scope": "user",
    }


def test_capability_provider_reference_round_trips_without_resource_identity():
    reference = capability_provider_reference(
        capability="document.semantic_export",
        provider_addon_id="doc_processor",
        provider_capability_id="semantic_export",
    )

    assert reference == {
        "version": 1,
        "capability": "document.semantic_export",
        "provider_addon_id": "doc_processor",
        "provider_capability_id": "semantic_export",
    }
    assert (
        capability_provider_reference_from_settings(
            {"semantic_export_reference": reference},
            setting_key="semantic_export_reference",
            capability="document.semantic_export",
        )
        == reference
    )
    assert "resource_type" not in reference
    assert "resource_id" not in reference

    resolved = ResolvedSourceProvider(
        requirement_id="semantic_export",
        capability="document.semantic_export",
        provider_addon_id="doc_processor",
        provider_capability_id="semantic_export",
        client=object(),
    )
    assert resolved.provider_reference() == reference


def test_semantic_export_reference_is_scoped_to_named_key_and_capability():
    reference = capability_provider_reference(
        capability="document.semantic_export",
        provider_addon_id="doc_processor",
        provider_capability_id="semantic_export",
    )

    assert (
        capability_provider_reference_from_settings(
            {"source_reference": reference},
            setting_key="semantic_export_reference",
            capability="document.semantic_export",
        )
        == {}
    )
    with pytest.raises(ValueError, match="does not match"):
        capability_provider_reference_from_settings(
            {
                "semantic_export_reference": {
                    **reference,
                    "capability": "document.processing",
                }
            },
            setting_key="semantic_export_reference",
            capability="document.semantic_export",
        )
    with pytest.raises(ValueError, match="unknown fields"):
        capability_provider_reference_from_settings(
            {
                "semantic_export_reference": {
                    **reference,
                    "unexpected": "ignored-is-not-safe",
                }
            },
            setting_key="semantic_export_reference",
            capability="document.semantic_export",
        )


def test_resolver_forwards_exact_provider_pin_owner_and_client_configuration():
    context = ProviderContext()
    reference = capability_provider_reference(
        capability="document.semantic_export",
        provider_addon_id="doc_processor",
        provider_capability_id="semantic_export",
    )

    resolved = resolve_capability_provider(
        context,
        requirement_id="semantic_export",
        capability="document.semantic_export",
        user_id=17,
        settings={"semantic_export_reference": reference},
        setting_key="semantic_export_reference",
    )

    assert resolved.client is context.client
    assert context.calls == [
        (
            "semantic_export",
            17,
            {
                "provider_addon_id": "doc_processor",
                "provider_capability_id": "semantic_export",
            },
        ),
        (
            "initialize",
            {
                "current_user_id": 17,
                "capability_requirement_id": "semantic_export",
                "capability_provider_addon_id": "doc_processor",
                "capability_provider_id": "semantic_export",
            },
        ),
    ]


def test_generic_resolver_rejects_foreign_capability_before_provider_lookup():
    context = ProviderContext()
    with pytest.raises(ValueError, match="does not match"):
        resolve_source_provider(
            context,
            requirement_id="semantic_export",
            capability="document.semantic_export",
            user_id=17,
            reference={
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc_processor",
                "provider_capability_id": "document_processor",
            },
        )
    assert context.calls == []
