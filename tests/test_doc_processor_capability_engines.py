from types import SimpleNamespace

from iarf_assistant.addons.doc_processor.api_client import DocProcessorClient


def _candidate(
    addon_id="extractor_pymupdf",
    provider_id="pymupdf",
    engine="pymupdf",
):
    return SimpleNamespace(
        addon_id=addon_id,
        provider=SimpleNamespace(
            id=provider_id,
            operations=("extract", "ocr"),
            qualifiers={
                "engine": engine,
                "display_name": "PyMuPDF",
                "formats": ["pdf", "png"],
                "features": ["ocr", "images", "fast"],
            },
        ),
    )


class Context:
    def __init__(self, extractor=None):
        self.candidate = _candidate()
        self.extractor = extractor or (lambda *_args, **_kwargs: "# extracted")
        self.calls = []

    def capability_candidates(self, requirement_id):
        assert requirement_id == "extractors"
        return (self.candidate,)

    def get_provider_records(self, domain):
        raise AssertionError(f"legacy {domain!r} provider registry was consulted")

    def get_addon(self, addon_id):
        assert addon_id == self.candidate.addon_id
        return SimpleNamespace(manifest={"name": "Fallback name"})

    def get_capability_provider(self, requirement_id, user_id, **selection):
        self.calls.append((requirement_id, user_id, selection))
        extractor = self.extractor
        return SimpleNamespace(
            initialize_client=lambda config: SimpleNamespace(
                extract_document=lambda filepath, **kwargs: extractor(
                    filepath,
                    config=config,
                    **kwargs,
                )
            )
        )


def test_engine_catalog_comes_from_capability_metadata():
    client = DocProcessorClient(Context(), {"current_user_id": 5})

    engines = client.get_engines()
    engine = next(item for item in engines if item["id"] == "pymupdf")

    assert engine["name"] == "PyMuPDF"
    assert engine["formats"] == ["pdf", "png"]
    assert engine["features"] == ["ocr", "images", "fast"]
    assert engine["addon_id"] == "extractor_pymupdf"
    assert engine["provider_capability_id"] == "pymupdf"


def test_external_extraction_executes_through_selected_capability_handle():
    captured = {}

    def extract(filepath, **kwargs):
        captured.update(filepath=filepath, **kwargs)
        return "# result"

    context = Context(extract)
    client = DocProcessorClient(context, {"current_user_id": 7, "purpose": "test"})
    metrics = {"pages": 0}

    result = client.parse_document(
        "document.pdf",
        engine="pymupdf",
        metrics_dict=metrics,
        force_ocr=True,
    )

    assert result == "# result"
    assert context.calls == [
        (
            "extractors",
            7,
            {
                "provider_addon_id": "extractor_pymupdf",
                "provider_capability_id": "pymupdf",
            },
        )
    ]
    assert captured["filepath"] == "document.pdf"
    assert captured["config"]["purpose"] == "test"
    assert captured["metrics_dict"] is metrics
    assert captured["force_ocr"] is True


def test_auto_pdf_selection_prefers_compatible_capability_engine():
    client = DocProcessorClient(Context(), {"current_user_id": 3})

    assert client.select_engine("document.pdf") == "pymupdf"


def test_engine_failure_is_logged_without_returning_internal_detail():
    def fail(*_args, **_kwargs):
        raise RuntimeError("secret infrastructure detail")

    client = DocProcessorClient(Context(fail), {"current_user_id": 1})

    result = client.parse_document("document.pdf", engine="pymupdf")

    assert result == "Error: The extraction engine failed."
    assert "secret" not in result
