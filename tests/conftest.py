"""Repository-wide pytest classification used by local and CI lanes."""

from __future__ import annotations

from pathlib import Path

import pytest

FAST_MODULES = {
    "test_addon_contracts.py",
    "test_bootstrap.py",
    "test_bootstrap_manager.py",
    "test_database_utils.py",
    "test_execution_scope.py",
    "test_frontend_builds.py",
    "test_frontend_phase0_tooling.py",
    "test_frontend_phase1_contracts.py",
    "test_logging_observability.py",
    "test_model_capabilities.py",
    "test_news_digest_publication_contract.py",
    "test_product_identity.py",
    "test_release_artifacts.py",
    "test_release_exception_policy.py",
    "test_runtime_contracts.py",
    "test_runtime_policy.py",
    "test_upload_paths.py",
}

ADDON_PREFIXES = (
    "test_aligner_mfa_",
    "test_audio_core_",
    "test_doc_processor_",
    "test_email_",
    "test_googledrive_",
    "test_library_importer",
    "test_meeting_minutes_",
    "test_news_",
    "test_scraping_",
    "test_sentence_segmenter",
    "test_stripe_",
    "test_stt_dataset_",
    "test_subtitle_studio",
    "test_translator_",
)


@pytest.fixture(autouse=True)
def isolate_platform_app_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep platform-cache/config resolution inside each test's temp tree."""

    platform_root = tmp_path / "platformdirs"
    monkeypatch.setattr(
        "iarf_assistant.core.app_paths._platform_roots",
        lambda: (
            platform_root / "data",
            platform_root / "config",
            platform_root / "cache",
            platform_root / "logs",
        ),
    )


def _add_marker(item: pytest.Item, name: str) -> None:
    if item.get_closest_marker(name) is None:
        item.add_marker(getattr(pytest.mark, name))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Assign stable area/scope markers without editing 129 legacy modules."""

    for item in items:
        filename = Path(str(item.fspath)).name
        _add_marker(
            item,
            "addon" if filename.startswith(ADDON_PREFIXES) else "core",
        )
        if filename in FAST_MODULES:
            _add_marker(item, "fast")
        if "contract" in filename or "manifest" in filename:
            _add_marker(item, "contract")
        if "e2e" in filename or "end_to_end" in filename:
            _add_marker(item, "e2e")
        elif any(
            token in filename
            for token in (
                "migration",
                "routes",
                "worker",
                "integrated",
                "lifecycle",
            )
        ):
            _add_marker(item, "integration")
