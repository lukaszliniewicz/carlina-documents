from types import SimpleNamespace

import pytest

from iarf_assistant.addons.doc_processor.intake import build_effective_settings
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import PdfParser
from iarf_assistant.addons.doc_processor.reasoning_snapshot import (
    ReasoningSnapshotError,
    build_reasoning_snapshot,
    reasoning_debug_metadata,
    thinking_config_for_call,
    validate_reasoning_snapshot,
)


def _model(
    model_id="vertex_ai/gemini-3.7-flash", *, levels="low,medium,high", supports=True
):
    return SimpleNamespace(
        model_id=model_id,
        supports_reasoning=supports,
        supported_reasoning_levels=levels,
        provider=SimpleNamespace(name="vertex_ai"),
    )


def _settings(model="vertex_ai/gemini-3.7-flash", profile="book_quality"):
    return {
        "parsing_model": model,
        "metadata_model": model,
        "processing_profile": profile,
    }


def _user(**slots):
    return SimpleNamespace(
        addon_settings={"doc_processor": {"reasoning": {"slots": slots}}}
    )


def _patch_model(monkeypatch, model):
    monkeypatch.setattr(
        "iarf_assistant.addons.doc_processor.reasoning_snapshot.get_visible_model",
        lambda _user, _model_id: model,
    )


def test_book_quality_uses_highest_advertised_sane_level(monkeypatch):
    _patch_model(monkeypatch, _model())
    snapshot = build_reasoning_snapshot(_settings(), _user())
    assert snapshot["slots"]["parsing"]["level"] == "high"
    assert snapshot["slots"]["parsing"]["source"] == "book_quality_default"


def test_book_quality_allows_explicit_reasoning_off(monkeypatch):
    model = _model(levels="off,high")
    _patch_model(monkeypatch, model)
    snapshot = build_reasoning_snapshot(
        _settings(),
        _user(parsing={"model_id": model.model_id, "level": "off"}),
    )
    assert snapshot["slots"]["parsing"]["level"] == "off"
    assert snapshot["slots"]["parsing"]["source"] == "explicit_addon_preference"


def test_gemini_authoritative_levels_do_not_fabricate_minimal_or_off(monkeypatch):
    model = _model(levels="low,medium,high")
    _patch_model(monkeypatch, model)
    with pytest.raises(ReasoningSnapshotError, match="not advertised"):
        build_reasoning_snapshot(
            _settings(),
            _user(parsing={"model_id": model.model_id, "level": "minimal"}),
        )


def test_custom_explicit_low_is_snapshotted(monkeypatch):
    model = _model()
    _patch_model(monkeypatch, model)
    snapshot = build_reasoning_snapshot(
        _settings(profile="custom"),
        _user(parsing={"model_id": model.model_id, "level": "low"}),
    )
    assert snapshot["slots"]["parsing"]["level"] == "low"
    assert snapshot["slots"]["parsing"]["source"] == "explicit_addon_preference"


def test_custom_default_uses_model_default_without_provider_level(monkeypatch):
    model = _model()
    _patch_model(monkeypatch, model)
    snapshot = build_reasoning_snapshot(_settings(profile="custom"), _user())
    assert snapshot["slots"]["parsing"]["level"] == "default"
    assert snapshot["slots"]["parsing"]["source"] == "model_default"
    assert thinking_config_for_call(snapshot, "parsing", model.model_id) == {
        "level": "default"
    }


def test_snapshot_does_not_change_when_user_preference_mutates(monkeypatch):
    model = _model()
    _patch_model(monkeypatch, model)
    user = _user(parsing={"model_id": model.model_id, "level": "low"})
    snapshot = build_reasoning_snapshot(_settings(profile="custom"), user)
    user.addon_settings["doc_processor"]["reasoning"]["slots"]["parsing"]["level"] = (
        "high"
    )
    assert thinking_config_for_call(snapshot, "parsing", model.model_id) == {
        "level": "low"
    }


def test_model_without_reasoning_is_visible_quality_downgrade(monkeypatch):
    model = _model(levels=None, supports=False)
    _patch_model(monkeypatch, model)
    snapshot = build_reasoning_snapshot(_settings(), _user())
    entry = snapshot["slots"]["parsing"]
    assert entry["level"] == "off"
    assert entry["source"] == "no_reasoning"


def test_support_claim_without_levels_fails_book_quality(monkeypatch):
    _patch_model(monkeypatch, _model(levels=None, supports=True))
    with pytest.raises(ReasoningSnapshotError, match="claims reasoning support"):
        build_reasoning_snapshot(_settings(), _user())


def test_model_mismatch_fails_closed():
    snapshot = {
        "version": 1,
        "slots": {
            slot: {
                "model_id": "provider/model",
                "provider": "provider",
                "level": "high",
                "supported_levels": ["high"],
                "source": "book_quality_default",
            }
            for slot in ("parsing", "metadata")
        },
    }
    with pytest.raises(ReasoningSnapshotError, match="mismatch"):
        thinking_config_for_call(snapshot, "parsing", "provider/other")


def test_pdf_parser_uses_snapshot_for_every_stage_helper():
    snapshot = {
        "version": 1,
        "slots": {
            "parsing": {
                "model_id": "provider/parser",
                "provider": "provider",
                "level": "high",
                "supported_levels": ["high"],
                "source": "book_quality_default",
            },
            "metadata": {
                "model_id": "provider/metadata",
                "provider": "provider",
                "level": "medium",
                "supported_levels": ["medium"],
                "source": "explicit_addon_preference",
            },
        },
    }
    parser = PdfParser.__new__(PdfParser)
    parser.settings = {"reasoning_snapshot": snapshot}

    assert parser._thinking_config("parsing", "provider/parser") == {"level": "high"}
    assert parser._thinking_config("metadata", "provider/metadata") == {
        "level": "medium"
    }
    with pytest.raises(ReasoningSnapshotError, match="mismatch"):
        parser._thinking_config("parsing", "provider/other")


def test_validation_is_bounded_and_debug_metadata_excludes_supported_levels():
    snapshot = {
        "version": 1,
        "slots": {
            slot: {
                "model_id": "provider/model",
                "provider": "provider",
                "level": "off",
                "supported_levels": [],
                "source": "no_reasoning",
            }
            for slot in ("parsing", "metadata")
        },
    }
    normalized = validate_reasoning_snapshot(snapshot)
    debug = reasoning_debug_metadata(normalized)
    assert debug["slots"]["parsing"] == {
        "model_id": "provider/model",
        "provider": "provider",
        "level": "off",
        "source": "no_reasoning",
    }


def test_snapshot_model_must_match_settings():
    snapshot = {
        "version": 1,
        "slots": {
            slot: {
                "model_id": "provider/model",
                "provider": "provider",
                "level": "default",
                "supported_levels": ["high"],
                "source": "model_default",
            }
            for slot in ("parsing", "metadata")
        },
    }
    with pytest.raises(ReasoningSnapshotError, match="mismatch"):
        validate_reasoning_snapshot(
            snapshot, settings={"parsing_model": "provider/other"}
        )


def test_effective_settings_preserve_snapshot_after_preference_mutation(monkeypatch):
    model = _model()
    _patch_model(monkeypatch, model)
    user = _user(parsing={"model_id": model.model_id, "level": "low"})
    settings = _settings(profile="custom")
    settings["reasoning_snapshot"] = build_reasoning_snapshot(settings, user)
    user.addon_settings["doc_processor"]["reasoning"]["slots"]["parsing"]["level"] = (
        "high"
    )
    effective = build_effective_settings(settings, user=user)
    assert effective["reasoning_snapshot"] == settings["reasoning_snapshot"]


def test_effective_settings_recompute_snapshot_for_project_model_override(monkeypatch):
    old_model = _model("provider/old", levels="low,medium,high")
    new_model = _model("provider/new", levels="low,medium,high")
    models = {old_model.model_id: old_model, new_model.model_id: new_model}
    monkeypatch.setattr(
        "iarf_assistant.addons.doc_processor.reasoning_snapshot.get_visible_model",
        lambda _user, model_id: models.get(model_id),
    )
    user = _user()
    settings = _settings(old_model.model_id, profile="custom")
    settings["metadata_model"] = old_model.model_id
    settings["reasoning_snapshot"] = build_reasoning_snapshot(settings, user)
    effective = build_effective_settings(
        settings,
        {"parsing_model": new_model.model_id, "metadata_model": new_model.model_id},
        user=user,
    )
    assert (
        effective["reasoning_snapshot"]["slots"]["parsing"]["model_id"]
        == new_model.model_id
    )
