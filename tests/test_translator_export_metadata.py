from copy import deepcopy

import pytest

from iarf_assistant.addons.translator.export_metadata import (
    ExportMetadataError,
    apply_export_metadata,
    normalize_export_metadata,
)


def _error_code(value):
    with pytest.raises(ExportMetadataError) as caught:
        normalize_export_metadata(value)
    return caught.value.code


def test_normalize_export_metadata_accepts_all_fields_and_copies_values():
    value = {
        "title": "  Export title  ",
        "subtitle": "",
        "description": "First line\n\tSecond line",
        "publisher": "Example Press",
        "publication_date": "2024-02-29",
        "rights": "Line one\nLine two",
        "journal_or_series": "Series",
        "volume": "1",
        "authors": [],
        "translators": ["  Ada Translator  "],
        "editors": ["Editor"],
        "subjects": [],
        "other_contributors": [{"name": "Illustrator", "role": "ill"}],
        "identifiers": [{"scheme": "DOI", "value": " 10.1234/example "}],
        "cover_upload_path": "/uploads/cover.png",
    }

    normalized = normalize_export_metadata(value)

    assert normalized == {
        "title": "Export title",
        "subtitle": "",
        "description": "First line\n\tSecond line",
        "publisher": "Example Press",
        "publication_date": "2024-02-29",
        "rights": "Line one\nLine two",
        "journal_or_series": "Series",
        "volume": "1",
        "authors": [],
        "translators": ["Ada Translator"],
        "editors": ["Editor"],
        "subjects": [],
        "other_contributors": [{"name": "Illustrator", "role": "ill"}],
        "identifiers": [{"scheme": "DOI", "value": "10.1234/example"}],
        "cover_upload_path": "/uploads/cover.png",
    }
    assert value["translators"] == ["  Ada Translator  "]
    assert value["identifiers"][0]["value"] == " 10.1234/example "


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, "export-metadata-type"),
        ({"unknown": "value"}, "export-metadata-key"),
        ({"title": ""}, "export-metadata-title"),
        ({"title": "\nTitle"}, "export-metadata-title"),
        ({"subtitle": 1}, "export-metadata-subtitle"),
        ({"authors": [" "]}, "export-metadata-authors"),
        ({"authors": ["a"] * 51}, "export-metadata-authors"),
        ({"subjects": ["a"] * 65}, "export-metadata-subjects"),
        ({"description": "x" * 4001}, "export-metadata-description"),
        ({"rights": "x" * 4001}, "export-metadata-rights"),
        ({"other_contributors": [{"name": "x", "role": "ILL"}]}, "export-metadata-other-contributors"),
        ({"other_contributors": [{"name": "x", "role": "il"}]}, "export-metadata-other-contributors"),
        ({"other_contributors": [{"name": "x", "role": "ill", "extra": "x"}]}, "export-metadata-other-contributors"),
        ({"identifiers": [{"scheme": "ISSN", "value": "x"}]}, "export-metadata-identifiers"),
        ({"identifiers": [{"scheme": "DOI", "value": ""}]}, "export-metadata-identifiers"),
        ({"cover_upload_path": "cover.png"}, "export-metadata-cover-upload-path"),
        ({"cover_upload_path": "/uploads/a?download=1"}, "export-metadata-cover-upload-path"),
        ({"cover_upload_path": "/uploads/a\\b"}, "export-metadata-cover-upload-path"),
    ],
)
def test_normalize_export_metadata_rejects_invalid_values(value, code):
    assert _error_code(value) == code


@pytest.mark.parametrize(
    "value",
    [
        {"publication_date": "2024-00"},
        {"publication_date": "2024-13"},
        {"publication_date": "2024-02-30"},
        {"publication_date": "2024-1-01"},
        {"publication_date": "0000"},
    ],
)
def test_normalize_export_metadata_rejects_invalid_publication_dates(value):
    assert _error_code(value) == "export-metadata-publication-date"


def test_normalize_export_metadata_rejects_controls_except_description_and_rights():
    assert _error_code({"publisher": "bad\tvalue"}) == "export-metadata-publisher"
    assert _error_code({"description": "bad\x00value"}) == "export-metadata-description"
    assert normalize_export_metadata({"description": "ok\nvalue"})["description"] == "ok\nvalue"
    assert normalize_export_metadata({"rights": "ok\tvalue"})["rights"] == "ok\tvalue"


def test_normalize_export_metadata_enforces_utf8_string_and_json_size_limits():
    with pytest.raises(ExportMetadataError) as caught:
        normalize_export_metadata({"description": "é" * 4000})
    assert caught.value.code == "export-metadata-description"

    with pytest.raises(ExportMetadataError) as caught:
        normalize_export_metadata({"authors": ["n" * 300] * 30})
    assert caught.value.code == "export-metadata-size"


def test_apply_export_metadata_is_pure_and_drops_source_edition_leaks():
    source = {
        "title": "Source title",
        "language": "ru",
        "contributors": {
            "authors": ["Source author"],
            "translators": ["Source translator"],
            "editors": ["Source editor"],
            "other": [{"name": "Source artist", "role": "ill"}],
        },
        "publisher": "Source publisher",
        "publication_date": "2020-01-01",
        "rights": "Source rights",
        "isbn": "old-isbn",
        "issn": "old-issn",
        "identifiers": [{"scheme": "DOI", "value": "old-doi"}],
        "source": "source-witness",
        "description": "Source description",
        "subjects": ["Source subject"],
        "subtitle": "Source subtitle",
        "journal_or_series": "Source series",
        "volume": "9",
        "issue": "2",
        "publication_type": "book",
    }
    original = deepcopy(source)

    projected = apply_export_metadata(source, {}, "en")

    assert source == original
    assert projected["language"] == "en"
    assert projected["title"] == "Source title"
    assert projected["contributors"] == {"authors": ["Source author"]}
    assert projected["publication_type"] == "book"
    for field in (
        "publisher",
        "publication_date",
        "rights",
        "isbn",
        "issn",
        "identifiers",
        "source",
        "description",
        "subjects",
        "subtitle",
        "journal_or_series",
        "volume",
        "issue",
    ):
        assert field not in projected


def test_apply_export_metadata_maps_overrides_to_packager_shapes_and_never_html():
    source = {
        "title": "Source title",
        "contributors": {"authors": ["Source author"], "other": [{"name": "old", "role": "ill"}]},
        "html": "<main>unchanged</main>",
    }
    overrides = {
        "title": "Translated title",
        "authors": ["New author"],
        "translators": ["New translator"],
        "editors": [],
        "other_contributors": [{"name": "New artist", "role": "art"}],
        "publisher": "New publisher",
        "publication_date": "2025",
        "subjects": ["History"],
        "identifiers": [{"scheme": "ISBN", "value": "not-validated-here"}],
        "cover_upload_path": "/uploads/cover.png",
    }

    projected = apply_export_metadata(source, overrides, "en")

    assert projected["title"] == "Translated title"
    assert projected["language"] == "en"
    assert projected["contributors"] == {
        "authors": ["New author"],
        "translators": ["New translator"],
        "editors": [],
        "other": [{"name": "New artist", "role": "art"}],
    }
    assert projected["publisher"] == "New publisher"
    assert projected["publication_date"] == "2025"
    assert projected["subjects"] == ["History"]
    assert projected["identifiers"] == [{"scheme": "ISBN", "value": "not-validated-here"}]
    assert projected["html"] == "<main>unchanged</main>"
    assert "cover_upload_path" not in projected


def test_apply_export_metadata_does_not_invent_source_translator_or_other_roles():
    source = {
        "title": "Source title",
        "contributors": {
            "authors": ["Author"],
            "translators": ["Book translator"],
            "editors": ["Book editor"],
            "other": [{"name": "Artist", "role": "ill"}],
        },
    }

    projected = apply_export_metadata(source, {"title": "Target title"}, "en")

    assert projected["contributors"] == {"authors": ["Author"]}
