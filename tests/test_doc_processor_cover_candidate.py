import json
from copy import deepcopy
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from jsonschema import Draft202012Validator, ValidationError

from iarf_assistant.addons.doc_processor.api_client import (
    DocProcessorClient,
    _bounded_metadata,
)
from iarf_assistant.addons.doc_processor.cover_candidate import (
    COVER_CANDIDATE_SCHEMA,
    metadata_schema_with_cover_candidate,
    normalize_cover_candidate,
    pdf_cover_candidate_enabled,
)
from iarf_assistant.addons.doc_processor.metadata_service import (
    get_canonical_document_metadata,
    normalize_document_metadata_payload,
    sync_document_metadata,
)
from iarf_assistant.addons.doc_processor.models import (
    DocumentMetadata,
    ProcessedDocument,
)
from iarf_assistant.addons.doc_processor.note_arrangement import (
    metadata_schema_with_note_arrangement,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User

_VALID_CANDIDATE = {
    "page": 1,
    "kind": "cover",
    "evidence": "Title and author are printed on the front cover.",
}


@pytest.mark.parametrize(
    "page",
    [
        None,
        True,
        False,
        1.0,
        "1",
        0,
        -1,
        1_000_001,
        pytest.param(10**4301, id="oversized-integer"),
    ],
)
def test_cover_candidate_rejects_invalid_page_values(page):
    assert normalize_cover_candidate({**_VALID_CANDIDATE, "page": page}) == {}


@pytest.mark.parametrize("kind", [None, "Cover", "titlePage", "front_cover", 1])
def test_cover_candidate_rejects_invalid_kind_values(kind):
    assert normalize_cover_candidate({**_VALID_CANDIDATE, "kind": kind}) == {}


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        [],
        {},
        {"page": 1, "kind": "cover"},
        {**_VALID_CANDIDATE, "evidence": "  \n "},
    ],
)
def test_cover_candidate_rejects_null_invalid_or_empty_values(candidate):
    assert normalize_cover_candidate(candidate) == {}


def test_cover_candidate_normalization_trims_caps_and_drops_undeclared_fields():
    raw = {
        "page": 2,
        "kind": "title_page",
        "evidence": f"  {'witness ' * 100}  ",
        "path": "/private/source.pdf",
        "resource": {"path": "/private/source.pdf"},
        "provider_object": "private-id",
    }
    original = deepcopy(raw)

    assert normalize_cover_candidate(raw) == {
        "page": 2,
        "kind": "title_page",
        "evidence": ("witness " * 100).strip()[:500],
    }
    assert len(normalize_cover_candidate(raw)["evidence"]) == 500
    assert raw == original


def test_cover_candidate_schema_is_nullable_closed_and_validated():
    assert COVER_CANDIDATE_SCHEMA["type"] == ["object", "null"]
    assert COVER_CANDIDATE_SCHEMA["additionalProperties"] is False
    assert COVER_CANDIDATE_SCHEMA["required"] == ["page", "kind", "evidence"]
    properties = COVER_CANDIDATE_SCHEMA["properties"]
    assert properties["page"] == {"type": "integer", "minimum": 1, "maximum": 1_000_000}
    assert properties["kind"] == {
        "type": "string",
        "enum": ["cover", "title_page"],
    }
    assert properties["evidence"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
    }

    validator = Draft202012Validator(COVER_CANDIDATE_SCHEMA)
    validator.validate(None)
    validator.validate(_VALID_CANDIDATE)
    with pytest.raises(ValidationError):
        validator.validate({**_VALID_CANDIDATE, "path": "/private/source.pdf"})
    with pytest.raises(ValidationError):
        validator.validate({**_VALID_CANDIDATE, "page": True})


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, True),
        ({}, True),
        ({"mcp_execution_mode": "managed"}, True),
        ({"mcp_execution_mode": "other"}, True),
        ({"mcp_execution_mode": "passive"}, False),
        (
            {
                "mcp_execution_mode": "passive",
                "mcp_cover_candidate_policy": "pdf-page-v1",
            },
            True,
        ),
        (
            {
                "mcp_execution_mode": "passive",
                "mcp_cover_candidate_policy": "other",
            },
            False,
        ),
    ],
)
def test_cover_candidate_policy_gate(settings, expected):
    assert pdf_cover_candidate_enabled(settings) is expected


def test_cover_candidate_schema_wrapper_preserves_disabled_identity_and_composes():
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "metadata": {"nested": ["kept"]},
    }
    original = deepcopy(schema)
    passive_unmarked = {"mcp_execution_mode": "passive"}

    assert metadata_schema_with_cover_candidate(schema, passive_unmarked) is schema
    assert schema == original

    selected = metadata_schema_with_cover_candidate(schema)
    assert selected is not schema
    assert selected["properties"]["cover_candidate"] == COVER_CANDIDATE_SCHEMA
    assert selected["required"] == ["title", "cover_candidate"]
    assert selected["metadata"] is not schema["metadata"]
    assert schema == original

    settings = {
        "mcp_execution_mode": "passive",
        "mcp_cover_candidate_policy": "pdf-page-v1",
        "mcp_note_arrangement_policy": "source-arrangement-v1",
    }
    cover_then_notes = metadata_schema_with_note_arrangement(
        metadata_schema_with_cover_candidate(schema, settings), settings
    )
    notes_then_cover = metadata_schema_with_cover_candidate(
        metadata_schema_with_note_arrangement(schema, settings), settings
    )
    for composed in (cover_then_notes, notes_then_cover):
        assert "cover_candidate" in composed["properties"]
        assert "note_arrangement" in composed["properties"]
        assert "cover_candidate" in composed["required"]
        assert "note_arrangement" in composed["required"]


def test_document_metadata_keeps_only_nonempty_normalized_cover_candidate():
    assert normalize_document_metadata_payload(
        {"cover_candidate": {**_VALID_CANDIDATE, "path": "/private/source.pdf"}}
    ) == {"cover_candidate": _VALID_CANDIDATE}
    assert normalize_document_metadata_payload({"cover_candidate": {}}) == {}
    assert (
        normalize_document_metadata_payload(
            {"cover_candidate": {**_VALID_CANDIDATE, "page": "1"}}
        )
        == {}
    )


def test_cover_candidate_reaches_record_backed_model_and_source_dto(tmp_path):
    app = _make_app(tmp_path)
    with app.app_context():
        db.create_all()
        db.session.add(
            User(id=1, email="cover-candidate@example.test", password_hash="test")
        )
        document = ProcessedDocument(
            id="cover-candidate-document",
            user_id=1,
            filename="synthetic.pdf",
            file_path="/uploads/1/synthetic.pdf",
            status="completed",
            settings={
                "document_metadata": {
                    "title": "Settings title",
                    "cover_candidate": {
                        **_VALID_CANDIDATE,
                        "source_path": "/private/source.pdf",
                    },
                }
            },
        )
        db.session.add(document)
        db.session.flush()
        sync_document_metadata(
            document,
            {
                "title": "Settings title",
                "cover_candidate": {
                    **_VALID_CANDIDATE,
                    "source_path": "/private/source.pdf",
                },
            },
        )
        assert document.metadata_record is not None
        document.metadata_record.title = "Record title"
        db.session.commit()
        db.session.remove()

        stored = db.session.get(ProcessedDocument, "cover-candidate-document")
        assert stored is not None
        assert "cover_candidate" not in DocumentMetadata.__table__.columns
        canonical = get_canonical_document_metadata(stored)
        model_view = stored.to_dict()
        source_view = DocProcessorClient(None, {"current_user_id": 1})._source_payload(
            stored
        )

        assert canonical["title"] == "Record title"
        assert canonical["cover_candidate"] == _VALID_CANDIDATE
        assert model_view["metadata"]["title"] == "Record title"
        assert model_view["metadata"]["cover_candidate"] == _VALID_CANDIDATE
        assert source_view["metadata"]["cover_candidate"] == _VALID_CANDIDATE
        assert "source_path" not in source_view["metadata"]["cover_candidate"]

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_cover_candidate_source_dto_respects_total_byte_cap_and_input_immutability():
    metadata = {
        "title": "😀" * 6000,
        "subtitle": "🟩" * 3000,
        "publication_type": "publication " * 20,
        "publisher": "🙂" * 300,
        "publication_date": "2026 " * 20,
        "journal_or_series": "©" * 300,
        "volume": "volume " * 10,
        "issue": "issue " * 10,
        "isbn": "9" * 100,
        "issn": "8" * 100,
        "contributors": {
            "authors": [f"😀Author {index}" for index in range(20)],
            "editors": [f"🟩Editor {index}" for index in range(20)],
            "translators": [f"🙂Translator {index}" for index in range(20)],
            "other": [
                {"role": "illustrator", "name": f"©Artist {index}"}
                for index in range(20)
            ],
        },
        "identifiers": [
            {"scheme": "DOI", "value": f"{index}-" + "1" * 200} for index in range(20)
        ],
        "note_arrangement": {
            "placement": "document_endnotes",
            "numbering": "document",
            "scope_heading_level": 2,
            "source_pages": list(range(1, 80)),
            "evidence": "é" * 5000,
        },
        "cover_candidate": {**_VALID_CANDIDATE, "path": "/private/source.pdf"},
        "private_setting": "must not cross the DTO boundary",
    }
    original = deepcopy(metadata)

    bounded = _bounded_metadata(metadata)

    assert len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= 8 * 1024
    assert bounded["cover_candidate"] == _VALID_CANDIDATE
    assert "path" not in bounded["cover_candidate"]
    assert "private_setting" not in bounded
    assert metadata == original


def test_processed_document_legacy_metadata_dictionary_keeps_absent_key_shape():
    legacy = ProcessedDocument(
        id="legacy-document",
        user_id=1,
        filename="legacy.txt",
        file_path="/uploads/1/legacy.txt",
        status="completed",
        settings={"document_metadata": {"title": "Legacy title"}},
    )

    metadata = legacy.to_dict()["metadata"]
    assert metadata == {"title": "Legacy title"}
    assert json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) == (
        '{"title":"Legacy title"}'
    )
    assert "cover_candidate" not in metadata


def test_processed_document_drops_invalid_settings_only_cover_candidate():
    document = ProcessedDocument(
        id="invalid-cover-candidate-document",
        user_id=1,
        filename="legacy.txt",
        file_path="/uploads/1/legacy.txt",
        status="completed",
        settings={
            "document_metadata": {
                "title": "Legacy title",
                "cover_candidate": {
                    **_VALID_CANDIDATE,
                    "page": True,
                    "path": "/private/source.pdf",
                },
            }
        },
    )

    metadata = document.to_dict()["metadata"]
    assert metadata == {"title": "Legacy title"}


def _make_app(tmp_path: Path) -> Flask:
    app = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(tmp_path / "uploads"),
    )
    db.init_app(app)
    return app
