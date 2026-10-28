import copy
import hashlib
import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest

from carlina.core.epub_export import normalize_epub_options
from carlina.core.semantic_export import (
    SemanticExportRequest,
    SemanticExportResource,
)
from iarf_assistant.addons.translator import worker
from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.block_export import (
    BlockExportError,
    build_semantic_export_request,
    normalize_epub_review_hash,
    normalize_export_metadata,
    semantic_export_review_hash,
)
from iarf_assistant.addons.translator.operations import canonical_request_fingerprint

HTML = "<main><h1 id='one'>Translated</h1><p>Target.</p></main>"


def _resource(ref="res_abcdefghijklmnop") -> SemanticExportResource:
    return SemanticExportResource(
        ref=ref,
        media_type="image/png",
        filename="figure.png",
        byte_length=1,
        sha256="a" * 64,
        owner_attestation="b" * 64,
    )


def _source_metadata():
    return {
        "title": "A translated book",
        "publication_date": "2024-01-02",
        "language": "ru",
        "contributors": {
            "authors": ["Author"],
            "editors": ["Editor"],
            "translators": ["Source Translator"],
            "other": [{"role": "illustrator", "name": "Other"}],
        },
    }


def _canonical_sha256(value):
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record(*, resources=(), target_lang="pl", source_metadata=None, provenance=None):
    snapshot = SimpleNamespace(
        id="document-1",
        document_id="document-1",
        content_hash="c" * 64,
        semantic_content_hash="d" * 64,
        metadata=_source_metadata() if source_metadata is None else source_metadata,
        provenance=(
            {"knowledge_source_id": "source-1"} if provenance is None else provenance
        ),
        resources=tuple(resources),
        filename="book.html",
        captured_at="2026-08-26T10:00:00+00:00",
        source_updated_at="2026-08-25T10:00:00+00:00",
    )
    plan = SimpleNamespace(plan_hash="e" * 64, snapshot=snapshot)
    record = SimpleNamespace(
        id="plan-1",
        plan_hash=plan.plan_hash,
        source_content_hash=snapshot.content_hash,
        source_document_id=snapshot.document_id,
        project=SimpleNamespace(target_lang=target_lang),
    )
    return record, plan


def _build(
    *,
    export_format="epub",
    html=HTML,
    resources=(),
    target_lang="pl",
    source_metadata=None,
    provenance=None,
    **kwargs,
):
    record, plan = _record(
        resources=resources,
        target_lang=target_lang,
        source_metadata=source_metadata,
        provenance=provenance,
    )
    with (
        patch(
            "iarf_assistant.addons.translator.block_export.rehydrate_source_plan",
            return_value=plan,
        ),
        patch(
            "iarf_assistant.addons.translator.block_export.render_persisted_target_document",
            return_value=html,
        ),
    ):
        return build_semantic_export_request(
            record, export_format=export_format, **kwargs
        )


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp"])
def test_semantic_export_builder_binds_image_mode(mode):
    request = _build(epub_image_mode=mode)
    assert request.epub_image_mode == mode
    assert request.to_dict().get("epub_image_mode", "auto") == mode
    if mode != "auto":
        assert semantic_export_review_hash(request.to_dict()) != semantic_export_review_hash(_build().to_dict())


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"unsupported_field": "Unknown"},
        {"title": 1},
        {"title": "  "},
        {"authors": ["  "]},
        {"translators": "Translator"},
    ],
)
def test_normalize_export_metadata_rejects_invalid_overrides(value):
    with pytest.raises(BlockExportError, match="export-metadata"):
        normalize_export_metadata(value)


def test_normalize_export_metadata_trims_and_copies_values():
    value = {"title": "  New title ", "authors": [" A "]}

    normalized = normalize_export_metadata(value)

    assert normalized == {"title": "New title", "authors": ["A"]}
    assert normalized is not value
    assert normalized["authors"] is not value["authors"]


def test_cover_presentation_uses_translated_edition_metadata_and_frozen_pdf_witness():
    request = _build(
        export_format="epub",
        epub_cover={"mode": "generated"},
        export_metadata={"title": "Translated title", "authors": ["Edition Author"]},
    )
    assert request.to_dict()["metadata"]["epub-cover"] == {"mode": "generated"}
    assert request.metadata["title"] == "Translated title"
    assert request.to_dict()["metadata"]["contributors"]["authors"] == [
        "Edition Author"
    ]
    pdf = _build(
        export_format="epub",
        epub_cover={"mode": "pdf_page", "page": 4},
        provenance={"source_pdf_sha256": "a" * 64},
    )
    assert pdf.metadata["source-snapshot"]["pdf-sha256"] == "a" * 64
    assert "epub-cover" not in _build(export_format="epub").metadata
    with pytest.raises(BlockExportError, match="semantic-export-cover-option"):
        _build(export_format="html", epub_cover={"mode": "generated"})


def test_export_request_applies_target_metadata_overrides_without_mutating_source():
    source_metadata = _source_metadata()
    source_metadata_before = copy.deepcopy(source_metadata)
    request = _build(
        target_lang="en",
        source_metadata=source_metadata,
        export_metadata={
            "title": "  English edition ",
            "authors": ["  New Author "],
            "translators": ["  New Translator "],
        },
    )

    assert request.metadata["language"] == "en"
    assert request.metadata["title"] == "English edition"
    assert request.to_dict()["metadata"]["contributors"] == {
        "authors": ["New Author"],
        "translators": ["New Translator"],
    }
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata_before
    )
    assert source_metadata == source_metadata_before


def test_export_request_omits_source_translator_when_no_export_override():
    source_metadata = _source_metadata()
    request = _build(target_lang="en", source_metadata=source_metadata)

    assert request.metadata["language"] == "en"
    assert "translators" not in request.to_dict()["metadata"]["contributors"]
    assert "other" not in request.to_dict()["metadata"]["contributors"]
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )


def test_export_request_uses_format_toc_defaults():
    assert _build(export_format="epub").include_source_toc is False
    assert _build(export_format="html").include_source_toc is True
    assert _build(export_format="docx").include_source_toc is True


def test_export_request_binds_target_source_metadata_resources_and_witnesses():
    resource = _resource()
    source_metadata = _source_metadata()
    source_provenance = {"knowledge_source_id": "source-1"}
    html = (
        "<main><h1 id='one'>Translated</h1>"
        "<figure><img src='.carlina-resources/res_abcdefghijklmnop' "
        "alt='Figure'></figure></main>"
    )
    request = _build(
        export_format="epub",
        html=html,
        resources=(resource,),
        source_metadata=source_metadata,
        provenance=source_provenance,
        operation_witness={"id": "operation-1", "record_hash": "f" * 64},
        provider_witness={
            "capability": "document.semantic_export",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "semantic_export",
        },
    )

    assert html in request.semantic_html
    assert (
        request.semantic_html_sha256
        == hashlib.sha256(request.semantic_html.encode()).hexdigest()
    )
    assert request.resources == (resource,)
    assert request.metadata["title"] == "A translated book"
    assert "publication_date" not in request.metadata
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )
    assert request.metadata["source-provenance-sha256"] == _canonical_sha256(
        source_provenance
    )
    assert request.metadata["source-snapshot"]["resources-count"] == 1
    assert request.metadata["source-snapshot"]["resources-sha256"] == (
        _canonical_sha256([resource.to_dict()])
    )
    assert request.metadata["source-plan"] == {
        "id": "plan-1",
        "plan-hash": "e" * 64,
        "content-hash": "c" * 64,
        "numbering-revision": 0,
    }
    assert request.metadata["target"] == {
        "language": "pl",
        "semantic-html-sha256": request.semantic_html_sha256,
    }
    assert (
        request.metadata["operation-witness-sha256"]
        == hashlib.sha256(
            request.metadata["operation-witness-json"].encode()
        ).hexdigest()
    )
    assert (
        request.metadata["provider-witness-sha256"]
        == hashlib.sha256(
            request.metadata["provider-witness-json"].encode()
        ).hexdigest()
    )


def test_preflight_witness_matches_worker_request_with_delivery_operation_witness():
    resource = _resource()
    html = (
        "<main><h1 id='one'>Translated</h1>"
        "<figure><img src='.carlina-resources/res_abcdefghijklmnop' "
        "alt='Figure'></figure></main>"
    )
    options = normalize_epub_options({"direction": "rtl"})
    provider_witness = {
        "version": 1,
        "capability": "document.semantic_export",
        "provider_addon_id": "doc_processor",
        "provider_capability_id": "semantic_export",
    }
    common = {
        "export_format": "epub",
        "html": html,
        "resources": (resource,),
        "epub_options": options,
        "export_metadata": {"title": "Reviewed title"},
        "provider_witness": provider_witness,
    }

    preflight_request = _build(**common)
    preflight_payload = preflight_request.to_dict()
    review_hash = semantic_export_review_hash(preflight_payload)

    operation = SimpleNamespace(
        id="operation-1",
        request_fingerprint=canonical_request_fingerprint(
            {
                "format": "epub",
                "numbering_revision": 0,
                "export_metadata": {"title": "Reviewed title"},
                "source_content_hash": "c" * 64,
                "source_plan_id": "plan-1",
                "source_plan_hash": "e" * 64,
                "semantic_export_provider": provider_witness,
                "epub_options": options,
                "epub_review_hash": review_hash,
            }
        ),
        source_content_hash="c" * 64,
        source_plan_id="plan-1",
        source_plan_hash="e" * 64,
    )
    operation_witness = {
        "id": operation.id,
        "request_fingerprint": operation.request_fingerprint,
        "source_content_hash": operation.source_content_hash,
        "source_plan_id": operation.source_plan_id,
        "source_plan_hash": operation.source_plan_hash,
        "numbering_revision": 0,
    }
    worker_request = _build(**common, operation_witness=operation_witness)
    worker_payload = worker_request.to_dict()

    assert "operation-witness-json" not in preflight_payload["metadata"]
    assert "operation-witness-sha256" not in preflight_payload["metadata"]
    assert "operation-witness-json" in worker_payload["metadata"]
    assert "operation-witness-sha256" in worker_payload["metadata"]
    assert semantic_export_review_hash(worker_payload) == review_hash
    worker._verify_epub_review_hash(worker_payload, review_hash)

    changed_metadata = copy.deepcopy(worker_payload)
    changed_metadata["metadata"]["title"] = "Changed title"

    changed_options = copy.deepcopy(worker_payload)
    changed_options["epub_options"] = normalize_epub_options(
        {"direction": "ltr"}
    )

    changed_provider = copy.deepcopy(worker_payload)
    provider_value = json.loads(
        changed_provider["metadata"]["provider-witness-json"]
    )
    provider_value["provider_addon_id"] = "different-provider"
    provider_json = json.dumps(
        provider_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    changed_provider["metadata"]["provider-witness-json"] = provider_json
    changed_provider["metadata"]["provider-witness-sha256"] = hashlib.sha256(
        provider_json.encode("utf-8")
    ).hexdigest()

    changed_resources = copy.deepcopy(worker_payload)
    changed_resources["resources"][0]["sha256"] = "f" * 64

    for changed_request in (
        changed_metadata,
        changed_options,
        changed_provider,
        changed_resources,
    ):
        assert changed_request["semantic_html"] == worker_payload["semantic_html"]
        assert changed_request["semantic_html_sha256"] == worker_payload[
            "semantic_html_sha256"
        ]
        with pytest.raises(BlockExportError, match="epub-review-stale"):
            worker._verify_epub_review_hash(changed_request, review_hash)


def test_export_request_rejects_invalid_format_before_rehydration():
    record, _plan = _record()
    with (
        patch(
            "iarf_assistant.addons.translator.block_export.rehydrate_source_plan"
        ) as rehydrate,
        pytest.raises(BlockExportError, match="semantic-export-format"),
    ):
        build_semantic_export_request(record, export_format="rtf")
    rehydrate.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BlockContractError("target-document-block-missing"), "target-incomplete"),
        (BlockContractError("target-document-validation"), "target-tampered"),
    ],
)
def test_export_request_fails_closed_for_incomplete_or_tampered_target(error, expected):
    record, plan = _record()
    with (
        patch(
            "iarf_assistant.addons.translator.block_export.rehydrate_source_plan",
            return_value=plan,
        ),
        patch(
            "iarf_assistant.addons.translator.block_export.render_persisted_target_document",
            side_effect=error,
        ),
        pytest.raises(BlockExportError, match=expected),
    ):
        build_semantic_export_request(record, export_format="epub")


def test_html_with_opaque_resources_is_delegated_to_owner_provider():
    resource = _resource()
    html = "<main><img src='.carlina-resources/res_abcdefghijklmnop'></main>"
    request = _build(export_format="html", html=html, resources=(resource,))
    assert request.resources == (resource,)
    assert html in request.semantic_html


def test_resource_manifest_must_match_target_references():
    with pytest.raises(BlockExportError, match="target-resource-manifest"):
        _build(export_format="epub", resources=(_resource(),))


@pytest.mark.parametrize("export_format", ["epub", "html", "docx", "pdf"])
def test_large_source_resource_manifest_round_trips_for_every_export_format(
    export_format,
):
    resources = tuple(_resource(f"res_{index:016d}") for index in range(32))
    canonical_manifest = json.dumps(
        [resource.to_dict() for resource in resources],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(canonical_manifest.encode("utf-8")) > 4096
    html = (
        "<main><h1>Images</h1>"
        + "".join(
            f"<img src='.carlina-resources/{resource.ref}'>" for resource in resources
        )
        + "</main>"
    )

    request = _build(
        export_format=export_format,
        html=html,
        resources=resources,
    )
    restored = SemanticExportRequest.from_dict(request.to_dict())

    assert request.resources == resources
    assert restored.resources == resources
    assert restored.metadata == request.metadata
    assert request.metadata["source-snapshot"]["resources-count"] == 32
    assert request.metadata["source-snapshot"]["resources-sha256"] == (
        _canonical_sha256([resource.to_dict() for resource in resources])
    )


def test_large_source_metadata_and_provenance_are_hashed_without_provider_leaks():
    source_metadata = {
        "title": "A translated book",
        "publication_date": "刊行日-" + "é" * 3000,
        "internal_private_blob": "PRIVATE-SOURCE-BLOB-" + "機密" * 3000,
    }
    source_provenance = {
        "private_provenance": "PRIVATE-PROVENANCE-" + "資料" * 3000,
    }
    metadata_before = copy.deepcopy(source_metadata)
    provenance_before = copy.deepcopy(source_provenance)
    assert len(json.dumps(source_metadata, ensure_ascii=False).encode("utf-8")) > 4096
    assert len(json.dumps(source_provenance, ensure_ascii=False).encode("utf-8")) > 4096

    request = _build(
        source_metadata=source_metadata,
        provenance=source_provenance,
    )
    serialized_request = json.dumps(request.to_dict(), ensure_ascii=False)

    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )
    assert request.metadata["source-provenance-sha256"] == _canonical_sha256(
        source_provenance
    )
    assert "publication_date" not in request.metadata
    assert "internal_private_blob" not in request.metadata
    assert "private_provenance" not in request.metadata
    assert "PRIVATE-SOURCE-BLOB-" not in serialized_request
    assert "PRIVATE-PROVENANCE-" not in serialized_request
    assert "刊行日-" not in serialized_request
    assert source_metadata == metadata_before
    assert source_provenance == provenance_before


def test_epub_export_request_forwards_closed_note_arrangement_projection():
    source_metadata = _source_metadata()
    source_metadata["note_arrangement"] = {
        "placement": "chapter_endnotes",
        "numbering": "chapter",
        "scope_heading_level": 2,
        "source_pages": [4, 1, 4],
        "evidence": "Notes follow the chapters and restart by chapter.",
        "private_source_observation": "PRIVATE-NOTE-OBSERVATION",
    }
    metadata_before = copy.deepcopy(source_metadata)

    request = _build(
        source_metadata=source_metadata,
        export_metadata={"title": "Translated edition"},
    )
    provider_metadata = request.to_dict()["metadata"]
    serialized = json.dumps(provider_metadata, ensure_ascii=False)

    assert provider_metadata["note_arrangement"] == {
        "placement": "chapter_endnotes",
        "numbering": "chapter",
        "scope_heading_level": 2,
        "source_pages": [1, 4],
        "evidence": "Notes follow the chapters and restart by chapter.",
    }
    assert provider_metadata["title"] == "Translated edition"
    assert provider_metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )
    assert "private_source_observation" not in serialized
    assert "PRIVATE-NOTE-OBSERVATION" not in serialized
    assert source_metadata == metadata_before

    with pytest.raises(BlockExportError, match="export-metadata-key"):
        _build(
            source_metadata=source_metadata,
            export_metadata={
                "note_arrangement": {"placement": "none"},
            },
        )


@pytest.mark.parametrize("arrangement", ["missing", None, {}, [], "invalid"])
def test_epub_export_request_omits_absent_or_empty_note_arrangement(arrangement):
    source_metadata = _source_metadata()
    if arrangement != "missing":
        source_metadata["note_arrangement"] = arrangement

    request = _build(source_metadata=source_metadata)

    assert "note_arrangement" not in request.metadata


def test_epub_export_request_sanitizes_malformed_note_arrangement_values():
    source_metadata = _source_metadata()
    source_metadata["note_arrangement"] = {
        "placement": "endnotes",
        "numbering": "global",
        "scope_heading_level": True,
        "source_pages": [True, False, 0, -1, "2", 1, 2, 1_000_000_001],
        "evidence": None,
        "private_source_observation": "PRIVATE-MALFORMED-NOTE-OBSERVATION",
    }
    metadata_before = copy.deepcopy(source_metadata)

    request = _build(source_metadata=source_metadata)
    provider_metadata = request.to_dict()["metadata"]
    serialized = json.dumps(provider_metadata, ensure_ascii=False)

    assert provider_metadata["note_arrangement"] == {
        "placement": "unknown",
        "numbering": "unknown",
        "scope_heading_level": None,
        "source_pages": [1, 2],
        "evidence": "",
    }
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )
    assert "PRIVATE-MALFORMED-NOTE-OBSERVATION" not in serialized
    assert source_metadata == metadata_before


def test_epub_export_request_bounds_unicode_evidence_and_source_pages():
    source_metadata = {
        "note_arrangement": {
            "placement": "document_endnotes",
            "numbering": "section",
            "scope_heading_level": 4,
            "source_pages": list(range(40, 0, -1))
            + [True, False, 0, -5, "7", 1_000_000_001],
            "evidence": "🙂" * 1_000,
        }
    }
    metadata_before = copy.deepcopy(source_metadata)

    request = _build(source_metadata=source_metadata)
    arrangement = request.to_dict()["metadata"]["note_arrangement"]

    assert arrangement["source_pages"] == list(range(1, 33))
    assert len(arrangement["source_pages"]) == 32
    assert len(arrangement["evidence"]) == 800
    assert len(arrangement["evidence"].encode("utf-8")) <= 4096
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )
    assert source_metadata == metadata_before


def test_epub_export_request_accepts_frozen_mapping_and_tuple_containers():
    source_metadata = MappingProxyType(
        {
            "note_arrangement": MappingProxyType(
                {
                    "placement": "page_footnotes",
                    "numbering": "page",
                    "scope_heading_level": 3,
                    "source_pages": (3, 1, 3),
                    "evidence": "Footnotes appear on the cited pages.",
                    "private_source_observation": "PRIVATE-FROZEN-NOTE-OBSERVATION",
                }
            )
        }
    )
    equivalent_source = {
        "note_arrangement": {
            "placement": "page_footnotes",
            "numbering": "page",
            "scope_heading_level": 3,
            "source_pages": [3, 1, 3],
            "evidence": "Footnotes appear on the cited pages.",
            "private_source_observation": "PRIVATE-FROZEN-NOTE-OBSERVATION",
        }
    }

    request = _build(source_metadata=source_metadata)
    provider_metadata = request.to_dict()["metadata"]
    serialized = json.dumps(provider_metadata, ensure_ascii=False)

    assert provider_metadata["note_arrangement"] == {
        "placement": "page_footnotes",
        "numbering": "page",
        "scope_heading_level": 3,
        "source_pages": [1, 3],
        "evidence": "Footnotes appear on the cited pages.",
    }
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        equivalent_source
    )
    assert source_metadata["note_arrangement"]["source_pages"] == (3, 1, 3)
    assert "PRIVATE-FROZEN-NOTE-OBSERVATION" not in serialized


@pytest.mark.parametrize("export_format", ["html", "docx", "pdf"])
def test_non_epub_export_request_does_not_forward_note_arrangement(export_format):
    source_metadata = {
        **_source_metadata(),
        "note_arrangement": {
            "placement": "chapter_endnotes",
            "numbering": "chapter",
            "scope_heading_level": 2,
            "source_pages": [1],
            "evidence": "Source evidence.",
        },
    }

    request = _build(export_format=export_format, source_metadata=source_metadata)

    assert "note_arrangement" not in request.metadata
    assert request.metadata["source-metadata-sha256"] == _canonical_sha256(
        source_metadata
    )


def test_epub_options_are_frozen_request_fields_and_keep_legacy_omission():
    legacy = _build(export_format="epub")
    assert "epub_options" not in legacy.to_dict()

    options = normalize_epub_options({})
    request = _build(export_format="epub", epub_options={})

    assert request.to_dict()["epub_options"] == options
    assert request.epub_options is not None
    assert request.to_dict()["epub_options"] == options
    assert "epub_options" not in request.metadata
    with pytest.raises(TypeError):
        request.epub_options["direction"] = "rtl"


def test_epub_review_hash_is_normalized_and_rejects_non_sha256_values():
    assert normalize_epub_review_hash("  " + "A" * 64 + "  ") == "a" * 64
    for value in (None, "a" * 63, "g" * 64, "a" * 64 + "0"):
        with pytest.raises(BlockExportError, match="epub-review-hash-invalid"):
            normalize_epub_review_hash(value)


def test_epub_options_reject_invalid_and_non_epub_requests_with_source_free_codes():
    with pytest.raises(BlockExportError, match="epub-options-profile"):
        _build(export_format="epub", epub_options={"profile": "unknown"})

    for export_format in ("html", "docx", "pdf"):
        with pytest.raises(BlockExportError, match="semantic-export-epub-options-format"):
            _build(export_format=export_format, epub_options={})
