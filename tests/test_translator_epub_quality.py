import hashlib
from types import SimpleNamespace

import pytest

from carlina.core.epub_export import (
    epub_review_sha256,
    epub_value_sha256,
    normalize_epub_options,
    normalize_epub_quality,
)
from iarf_assistant.addons.translator import worker
from iarf_assistant.addons.translator.block_export import BlockExportError
from iarf_assistant.addons.translator.operations import canonical_request_fingerprint

DATA = b"synthetic publishing-v1 artifact"
INPUT_SHA256 = "a" * 64
OPTIONS = normalize_epub_options({})
BASE_RECEIPT = {
    "format": "epub",
    "media_type": "application/epub+zip",
    "byte_length": len(DATA),
    "sha256": hashlib.sha256(DATA).hexdigest(),
    "validation_profile": "semantic-export-v1",
    "validation_profile_version": "1",
}


def _quality(*, input_sha256=INPUT_SHA256, options_sha256=None, artifact_sha256=None):
    return {
        "schema": "epub-quality-v1",
        "profile": "publishing-v1",
        "input_sha256": input_sha256,
        "options_sha256": options_sha256 or epub_value_sha256(OPTIONS),
        "artifact_sha256": artifact_sha256 or BASE_RECEIPT["sha256"],
        "content_sha256": hashlib.sha256(b"opf and package content").hexdigest(),
        "checks": {
            "structure": "passed",
            "epubcheck": "not_run",
            "ace": "not_run",
            "manual": "not_reviewed",
        },
        "counts": {
            "notes": 0,
            "references": 0,
            "backlinks": 0,
            "headings": 0,
            "images": 0,
            "tables": 0,
            "pages": 0,
        },
        "issues": [],
        "issue_count": 0,
        "epubcheck_version": None,
    }


def test_worker_accepts_exact_legacy_six_field_receipt_without_quality():
    receipt = worker._validate_semantic_export_receipt(
        dict(BASE_RECEIPT),
        DATA,
        export_format="epub",
        semantic_html_sha256=INPUT_SHA256,
        epub_options=None,
    )

    assert receipt == BASE_RECEIPT
    assert set(receipt) == {
        "format",
        "media_type",
        "byte_length",
        "sha256",
        "validation_profile",
        "validation_profile_version",
    }


def test_worker_binds_epub_review_hash_to_exact_semantic_request():
    snapshot = {
        "format": "epub",
        "semantic_html_sha256": INPUT_SHA256,
        "metadata": {"title": "Reviewed edition"},
        "resources": [{"sha256": "c" * 64}],
        "epub_options": normalize_epub_options({"direction": "rtl"}),
    }
    request_payload = {
        **snapshot,
        "semantic_html": "<main>same rendered HTML</main>",
    }
    review_hash = epub_review_sha256(snapshot)

    worker._verify_epub_review_hash(request_payload, review_hash)

    for field, changed_value in (
        ("epub_options", normalize_epub_options({"direction": "ltr"})),
        ("metadata", {"title": "Changed edition"}),
    ):
        changed_request = {
            **request_payload,
            field: changed_value,
        }
        assert changed_request["semantic_html"] == request_payload["semantic_html"]
        assert (
            changed_request["semantic_html_sha256"]
            == request_payload["semantic_html_sha256"]
        )
        with pytest.raises(BlockExportError, match="epub-review-stale"):
            worker._verify_epub_review_hash(changed_request, review_hash)

    with pytest.raises(BlockExportError, match="epub-review-hash-invalid"):
        worker._verify_epub_review_hash(request_payload, "not-a-sha256")
    worker._verify_epub_review_hash({}, None)


def test_worker_includes_normalized_epub_review_hash_in_request_fingerprint():
    payload = {
        "format": "epub",
        "source_content_hash": "b" * 64,
        "source_plan_id": "plan-1",
        "source_plan_hash": "c" * 64,
        "semantic_export_provider": {"version": 1},
        "numbering_revision": 0,
        "export_metadata": {},
        "epub_review_hash": "A" * 64,
    }
    request = {
        **payload,
        "epub_review_hash": "a" * 64,
    }
    operation = SimpleNamespace(
        source_content_hash=payload["source_content_hash"],
        source_plan_id=payload["source_plan_id"],
        source_plan_hash=payload["source_plan_hash"],
        request_fingerprint=canonical_request_fingerprint(request),
    )

    assert worker._export_metadata_for_operation(payload, operation) == {}
    changed = {**payload, "epub_review_hash": "d" * 64}
    with pytest.raises(ValueError, match="changed before execution"):
        worker._export_metadata_for_operation(changed, operation)


def test_worker_rejects_epub_review_hash_in_non_epub_queue_payload():
    payload = {
        "format": "docx",
        "source_content_hash": "b" * 64,
        "source_plan_id": "plan-1",
        "source_plan_hash": "c" * 64,
        "semantic_export_provider": {"version": 1},
        "epub_review_hash": INPUT_SHA256,
    }
    operation = SimpleNamespace(
        source_content_hash=payload["source_content_hash"],
        source_plan_id=payload["source_plan_id"],
        source_plan_hash=payload["source_plan_hash"],
        request_fingerprint="unused",
    )

    with pytest.raises(BlockExportError, match="epub-review-hash-format"):
        worker._export_metadata_for_operation(payload, operation)


def test_worker_accepts_and_copies_quality_bound_to_request_options_and_artifact():
    quality = _quality()
    artifact = {**BASE_RECEIPT, "quality": quality}

    receipt = worker._validate_semantic_export_receipt(
        artifact,
        DATA,
        export_format="epub",
        semantic_html_sha256=INPUT_SHA256,
        epub_options=OPTIONS,
    )

    assert receipt["quality"] == normalize_epub_quality(quality)
    assert receipt["quality"] is not quality


@pytest.mark.parametrize(
    ("artifact", "data", "export_format", "semantic_html_sha256", "options"),
    [
        ({**BASE_RECEIPT}, DATA, "epub", INPUT_SHA256, OPTIONS),
        (
            {**BASE_RECEIPT, "quality": {**_quality(), "unexpected": "value"}},
            DATA,
            "epub",
            INPUT_SHA256,
            OPTIONS,
        ),
        (
            {**BASE_RECEIPT, "quality": _quality(input_sha256="b" * 64)},
            DATA,
            "epub",
            INPUT_SHA256,
            OPTIONS,
        ),
        (
            {**BASE_RECEIPT, "quality": _quality(options_sha256="b" * 64)},
            DATA,
            "epub",
            INPUT_SHA256,
            OPTIONS,
        ),
        (
            {**BASE_RECEIPT, "quality": _quality(artifact_sha256="b" * 64)},
            DATA,
            "epub",
            INPUT_SHA256,
            OPTIONS,
        ),
        ({**BASE_RECEIPT, "quality": _quality()}, DATA, "epub", INPUT_SHA256, None),
        ({**BASE_RECEIPT, "quality": _quality()}, DATA, "docx", INPUT_SHA256, OPTIONS),
        ({**BASE_RECEIPT, "unexpected": True}, DATA, "epub", INPUT_SHA256, None),
        (BASE_RECEIPT, b"changed bytes", "epub", INPUT_SHA256, None),
    ],
)
def test_worker_rejects_missing_altered_or_unrequested_quality(
    artifact, data, export_format, semantic_html_sha256, options
):
    with pytest.raises(BlockExportError, match="semantic-export-provider-result"):
        worker._validate_semantic_export_receipt(
            artifact,
            data,
            export_format=export_format,
            semantic_html_sha256=semantic_html_sha256,
            epub_options=options,
        )
