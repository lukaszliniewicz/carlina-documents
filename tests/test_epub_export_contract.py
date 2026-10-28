from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from carlina.core.epub_export import (
    EPUB_EXPORT_PROFILE,
    EPUB_QUALITY_SCHEMA,
    EpubExportContractError,
    effective_epub_recipe,
    epub_projection_options_sha256,
    normalize_epub_image_mode,
    normalize_epub_options,
    normalize_epub_quality,
    normalize_epub_recipe,
    semantic_epub_review_sha256,
)
from carlina.core.semantic_export import (
    SemanticExportArtifact,
    SemanticExportContractError,
    SemanticExportRequest,
)


def _request(
    *, export_format: str = "epub", options: object = None, mode: str = "auto"
):
    html = '<main><h1 id="title">A synthetic title</h1><p>Body.</p></main>'
    return SemanticExportRequest(
        semantic_html=html,
        semantic_html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        metadata={"title": "A synthetic title"},
        resources=(),
        format=export_format,
        include_source_toc=False,
        profile_id="doc_processor",
        profile_version="1",
        epub_options=options,
        epub_image_mode=mode,
    )


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp"])
def test_image_mode_recipe_and_semantic_request_roundtrip(mode):
    assert normalize_epub_image_mode(mode) == mode
    recipe = normalize_epub_recipe(
        {
            "epub_options": {},
            "epub_cover": {},
            "epub_image_mode": mode,
        }
    )
    assert effective_epub_recipe(recipe, stale=True)["epub_image_mode"] == mode
    request = _request(mode=mode)
    payload = request.to_dict()
    assert ("epub_image_mode" in payload) is (mode != "auto")
    assert SemanticExportRequest.from_dict(payload) == request
    if mode != "auto":
        assert semantic_epub_review_sha256(payload) != semantic_epub_review_sha256(
            _request().to_dict()
        )
    else:
        assert payload == _request().to_dict()


@pytest.mark.parametrize(
    "mode", [None, True, False, 1, [], {}, "JPEG", " jpeg", "preserve", ""]
)
def test_image_mode_contract_is_strict(mode):
    with pytest.raises(EpubExportContractError, match="epub-image-mode-invalid"):
        normalize_epub_image_mode(mode)
    payload = _request().to_dict()
    payload["epub_image_mode"] = mode
    with pytest.raises(SemanticExportContractError, match="epub-image-mode-invalid"):
        SemanticExportRequest.from_dict(payload)


@pytest.mark.parametrize("export_format", ["html", "docx", "pdf"])
@pytest.mark.parametrize("mode", ["jpeg", "webp"])
def test_non_epub_semantic_request_rejects_conversion(export_format, mode):
    with pytest.raises(SemanticExportContractError, match="epub-image-mode-format"):
        replace(_request(export_format=export_format), epub_image_mode=mode)


def test_semantic_provider_forwards_image_mode_to_packager(monkeypatch):
    from iarf_assistant.addons.doc_processor import epub_packager, epub_validation
    from iarf_assistant.addons.doc_processor.semantic_export import (
        export_semantic_document,
    )

    calls = []

    class Packager:
        def __init__(self, _document, output, **options):
            calls.append(options)
            self.output = output

        def build(self):
            Path(self.output).write_bytes(b"synthetic epub")

        def quality_report(self, **_checks):
            return None

    monkeypatch.setattr(epub_packager, "EPUBPackager", Packager)
    monkeypatch.setattr(
        epub_validation,
        "validate_epub_file",
        lambda *_args, **_kwargs: SimpleNamespace(
            passed=True,
            epubcheck="not_run",
            version=None,
            ace="not_run",
            ace_version=None,
        ),
    )
    request = _request(mode="jpeg")
    artifact = export_semantic_document(request.to_dict(), resolver=SimpleNamespace())
    assert artifact.data == b"synthetic epub"
    assert calls[0]["image_mode"] == "jpeg"


def _quality_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": EPUB_QUALITY_SCHEMA,
        "profile": EPUB_EXPORT_PROFILE,
        "input_sha256": "1" * 64,
        "options_sha256": "2" * 64,
        "artifact_sha256": "3" * 64,
        "content_sha256": "4" * 64,
        "checks": {
            "structure": "passed",
            "epubcheck": "not_run",
            "ace": "not_run",
            "manual": "not_reviewed",
        },
        "counts": {
            "notes": 1,
            "references": 1,
            "backlinks": 1,
            "headings": 2,
            "images": 0,
            "tables": 0,
            "pages": 0,
        },
        "issues": [
            {
                "code": "image-description-review",
                "severity": "review",
                "target": "figure-1",
            }
        ],
        "issue_count": 1,
        "epubcheck_version": None,
    }
    report.update(overrides)
    return report


def test_absent_epub_options_are_legacy_while_empty_options_select_named_defaults():
    assert normalize_epub_options(None) == {}

    defaults = normalize_epub_options({})

    assert defaults == {
        "profile": "publishing-v2",
        "chapter_level": None,
        "note_placement": "source",
        "group_endnotes": True,
        "direction": "source",
        "roles": {},
        "page_labels": {},
        "table_headers": {},
        "image_descriptions": {},
        "decorative_images": [],
        "accessibility_evaluation": None,
        "image_policy": "balanced",
        "photographic_images": [],
        "long_descriptions": {},
        "front_matter_order": [],
        "omitted_front_matter": {},
        "front_matter_edits": {},
        "front_matter_breaks": {},
    }
    legacy = _request(options=None)
    publishing = _request(options={})
    assert "epub_options" not in legacy.to_dict()
    assert publishing.to_dict()["epub_options"] == defaults


@pytest.mark.parametrize(
    "options",
    [
        {"profile": "future-profile"},
        {"unknown": True},
        {"chapter_level": True},
        {"chapter_level": 0},
        {"chapter_level": 7},
        {"note_placement": "nearest"},
        {"direction": "auto"},
        {"group_endnotes": 1},
        {"roles": {"chapter": "unknown-role"}},
        {"page_labels": {"page-1": "é" * 65}},
        {"table_headers": {"table-1": "both"}},
        {"image_descriptions": {"image-1": "é" * 2049}},
        {"decorative_images": ["same", "same"]},
        {
            "decorative_images": ["image-1"],
            "image_descriptions": {"image-1": "A reviewed description."},
        },
    ],
)
def test_epub_options_reject_values_outside_the_closed_contract(options: object):
    with pytest.raises(EpubExportContractError):
        normalize_epub_options(options)


def test_epub_review_maps_enforce_entry_and_utf8_byte_limits():
    roles = {f"role-{index}": "chapter" for index in range(512)}
    assert len(normalize_epub_options({"roles": roles})["roles"]) == 512
    with pytest.raises(ValueError):
        normalize_epub_options({"roles": {**roles, "role-over": "chapter"}})

    page_label = "é" * 64
    assert (
        normalize_epub_options({"page_labels": {"page-1": page_label}})["page_labels"][
            "page-1"
        ]
        == page_label
    )
    descriptions = {"image-1": "é" * 2048}
    assert (
        normalize_epub_options({"image_descriptions": descriptions})[
            "image_descriptions"
        ]
        == descriptions
    )

    pages = {f"page-{index}": "x" for index in range(2048)}
    assert len(normalize_epub_options({"page_labels": pages})["page_labels"]) == 2048
    with pytest.raises(ValueError):
        normalize_epub_options({"page_labels": {**pages, "page-over": "x"}})

    tables = {f"table-{index}": "row" for index in range(512)}
    assert (
        len(normalize_epub_options({"table_headers": tables})["table_headers"]) == 512
    )
    with pytest.raises(ValueError):
        normalize_epub_options({"table_headers": {**tables, "table-over": "row"}})

    decorative = [f"image-{index}" for index in range(512)]
    assert (
        len(
            normalize_epub_options({"decorative_images": decorative})[
                "decorative_images"
            ]
        )
        == 512
    )
    with pytest.raises(ValueError):
        normalize_epub_options({"decorative_images": [*decorative, "image-over"]})

    too_many_bytes = {f"image-{index}": "x" * 4096 for index in range(33)}
    with pytest.raises(ValueError):
        normalize_epub_options({"image_descriptions": too_many_bytes})


def test_epub_options_are_copied_and_request_options_are_frozen_and_serializable():
    source = {
        "roles": {"chapter-1": "chapter"},
        "decorative_images": ["ornament-1"],
    }
    normalized = normalize_epub_options(source)
    source["roles"]["chapter-1"] = "appendix"
    source["decorative_images"].append("ornament-2")
    assert normalized["roles"] == {"chapter-1": "chapter"}
    assert normalized["decorative_images"] == ["ornament-1"]

    request_source = {"roles": {"chapter-1": "chapter"}}
    request = _request(options=request_source)
    request_source["roles"]["chapter-1"] = "appendix"
    assert request.epub_options["roles"]["chapter-1"] == "chapter"
    with pytest.raises(TypeError):
        request.epub_options["roles"]["chapter-1"] = "appendix"
    assert json.loads(json.dumps(request.to_dict()))["epub_options"]["roles"] == {
        "chapter-1": "chapter"
    }


def _evaluation(**overrides: object) -> dict[str, object]:
    return {
        "content_sha256": "5" * 64,
        "options_sha256": "6" * 64,
        "evaluator": "Independent reviewer",
        "summary": "The synthetic edition was reviewed.",
        "sufficient_modes": ["textual", "visual"],
        "conforms_to": "EPUB Accessibility 1.1 - WCAG 2.2 Level AA",
        "credential": None,
        "hazards": ["unknown"],
        **overrides,
    }


def test_accessibility_evaluation_is_closed_bounded_and_excluded_from_projection_hash():
    projection_options = normalize_epub_options({"chapter_level": 2})
    evaluation = _evaluation(
        options_sha256=epub_projection_options_sha256(projection_options)
    )
    evaluated = normalize_epub_options(
        {"chapter_level": 2, "accessibility_evaluation": evaluation}
    )

    assert evaluated["accessibility_evaluation"] == evaluation
    assert epub_projection_options_sha256(evaluated) == epub_projection_options_sha256(
        projection_options
    )


@pytest.mark.parametrize(
    "conformance",
    [
        "EPUB Accessibility 1.1 - WCAG 2.0 Level A",
        "EPUB Accessibility 1.1 - WCAG 2.1 Level AAA",
        "EPUB Accessibility 1.1 - WCAG 2.2 Level AA",
    ],
)
def test_accessibility_evaluation_accepts_documented_primitive_boundaries(
    conformance: str,
):
    evaluation = _evaluation(
        evaluator="e" * 512,
        summary="s" * 4096,
        credential="c" * 512,
        sufficient_modes=["textual,visual"],
        conforms_to=conformance,
        hazards=["sound"],
    )
    options = normalize_epub_options({"accessibility_evaluation": evaluation})
    assert options["accessibility_evaluation"] == evaluation


@pytest.mark.parametrize(
    "evaluation",
    [
        {**_evaluation(), "extra": "field"},
        {**_evaluation(), "content_sha256": "short"},
        {**_evaluation(), "evaluator": "é" * 257},
        {**_evaluation(), "summary": "é" * 2049},
        {**_evaluation(), "credential": "é" * 257},
        {**_evaluation(), "sufficient_modes": ["audio"]},
        {**_evaluation(), "sufficient_modes": [[]]},
        {**_evaluation(), "hazards": ["none", "sound"]},
        {**_evaluation(), "hazards": ["unknown", "flashing"]},
        {**_evaluation(), "conforms_to": "WCAG 2.2 AA"},
    ],
)
def test_invalid_accessibility_evaluations_raise_value_error(evaluation: object):
    with pytest.raises(ValueError):
        normalize_epub_options({"accessibility_evaluation": evaluation})


def test_epub_options_are_epub_only_and_legacy_request_keysets_remain_unchanged():
    legacy = _request(export_format="html")
    assert set(legacy.to_dict()) == {
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
    assert (
        SemanticExportRequest.from_dict(legacy.to_dict()).to_dict() == legacy.to_dict()
    )

    with pytest.raises(SemanticExportContractError):
        _request(export_format="html", options={})


def test_quality_receipt_has_an_exact_source_free_normalized_shape_and_matching_hash():
    report = _quality_report()
    normalized = normalize_epub_quality(report)
    assert set(normalized) == set(report)
    assert normalized == report

    artifact = SemanticExportArtifact(
        format="epub",
        media_type="application/epub+zip",
        byte_length=42,
        sha256="3" * 64,
        validation_profile="epub-v1",
        validation_profile_version="1",
        quality=report,
    )
    payload = artifact.to_dict()
    assert payload["quality"] == report
    assert "semantic_html" not in json.dumps(payload)
    assert "source_text" not in json.dumps(payload)

    with pytest.raises(SemanticExportContractError):
        SemanticExportArtifact(
            format="epub",
            media_type="application/epub+zip",
            byte_length=42,
            sha256="5" * 64,
            validation_profile="epub-v1",
            validation_profile_version="1",
            quality=report,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "field"},
        {
            "checks": {
                "structure": [],
                "epubcheck": "not_run",
                "ace": "not_run",
                "manual": "not_reviewed",
            }
        },
        {
            "counts": {
                "notes": [],
                "references": 0,
                "backlinks": 0,
                "headings": 0,
                "images": 0,
                "tables": 0,
                "pages": 0,
            }
        },
        {"issues": [{"code": "Bad Code", "severity": "review", "target": "x"}]},
        {"issues": [{"code": "image-review", "severity": [], "target": "x"}]},
        {"issues": [{"code": "image-review", "severity": "review", "target": []}]},
        {
            "issue_count": 201,
            "issues": [{"code": "image-review", "severity": "review", "target": "x"}]
            * 201,
        },
        {"artifact_sha256": "short"},
    ],
)
def test_malformed_quality_receipts_raise_value_errors_including_unhashable_fields(
    change: dict[str, object],
):
    report = _quality_report()
    if "extra" in change:
        report["unexpected"] = change["extra"]
    else:
        report.update(change)

    with pytest.raises(ValueError):
        normalize_epub_quality(report)


def test_quality_receipt_has_bounded_issue_details_and_legacy_artifact_keys():
    report = _quality_report(
        issues=[
            {"code": f"image-review-{index}", "severity": "review", "target": "image-1"}
            for index in range(200)
        ],
        issue_count=205,
    )
    normalized = normalize_epub_quality(report)
    assert len(normalized["issues"]) == 200
    assert normalized["issue_count"] == 205

    legacy = SemanticExportArtifact(
        format="epub",
        media_type="application/epub+zip",
        byte_length=0,
        sha256="a" * 64,
        validation_profile="epub-v1",
        validation_profile_version="1",
    )
    assert set(legacy.to_dict()) == {
        "format",
        "media_type",
        "byte_length",
        "sha256",
        "validation_profile",
        "validation_profile_version",
    }
