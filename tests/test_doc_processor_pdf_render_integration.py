"""The parser consumes child receipts without changing image contracts."""

import threading
from pathlib import Path

import fitz
import pytest

from iarf_assistant.addons.doc_processor.parsers.pdf_parser import PdfParser


def test_metadata_resume_restores_timings_before_first_page_checkpoint(
    tmp_path, monkeypatch
):
    import sys

    parser_module = sys.modules[PdfParser.__module__]
    monkeypatch.setattr(parser_module, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(PdfParser, "_load_prompt_settings", lambda self: {})
    parser = PdfParser(
        str(tmp_path / "source.pdf"),
        1,
        None,
        settings={
            "mcp_execution_id": "synthetic-execution",
            "metrics": {
                "preparation_timings": {
                    "metadata_total": {"calls": 2, "wall_seconds": 5.5}
                },
            },
        },
    )
    assert parser.state == {}
    assert parser.metrics["preparation_timings"]["metadata_total"] == {
        "calls": 2,
        "wall_seconds": 5.5,
    }


@pytest.mark.parametrize("transport", [None, "png-2000-v1"])
@pytest.mark.parametrize("crop_box", [None, (0.1, 0.1, 0.9, 0.9)])
def test_managed_preparation_preserves_bytes_and_cleans_sidecars(
    tmp_path, monkeypatch, transport, crop_box
):
    monkeypatch.setenv("CARLINA_PDF_RENDER_WORKERS", "2")
    parser = object.__new__(PdfParser)
    parser.settings = {"mcp_image_transport": transport}
    parser.output_folder = tmp_path / "output"
    parser.output_folder.mkdir()
    parser.absolute_path = tmp_path / "source.pdf"
    parser.metrics = {}
    parser.metrics_lock = threading.Lock()
    parser._render_coordination_dir = tmp_path / "coordination"
    parser._render_control_callback = None
    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=300, height=400)
        page.insert_text((20, 30), f"Synthetic page {index + 1}")
    document.save(parser.absolute_path)
    document.close()
    with fitz.open(parser.absolute_path) as document:
        expected = []
        for index in (2, 0):
            path = Path(parser._render_page_to_png(document[index], crop_box))
            expected.append(
                (path.read_bytes(), parser._image_file_to_data_url(str(path)))
            )
            path.unlink()
        with parser._prepared_page_images(
            document,
            [2, 0, 1],
            {},
            crop_box,
            encode_metadata=True,
            phase="metadata_images",
        ) as prepared:
            for index, reference in zip((2, 0), expected):
                receipt = next(prepared)
                assert receipt["page_index"] == index
                path = Path(receipt["png_path"])
                assert (path.read_bytes(), receipt["data_url"]) == reference
                path.unlink()
    # Third receipt was prepared but never consumed; the pool owns its cleanup.
    assert list(parser.output_folder.iterdir()) == []
    timing = parser.metrics["preparation_timings"]["metadata_images"]
    assert timing["pages"] == 3
    assert timing["png_bytes"] > 0
    assert timing["encoded_bytes"] > timing["png_bytes"]
    assert timing["wall_seconds"] > 0
