from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
from flask import Flask

from iarf_assistant.addons.doc_processor import routes


def test_export_image_resolution_is_scoped_to_the_document_directory(tmp_path: Path):
    app = Flask(__name__, root_path=str(tmp_path))
    document_dir = tmp_path / "uploads" / "7" / "doc_processor_docs" / "doc-1"
    document_dir.mkdir(parents=True)
    source_path = document_dir / "source.pdf"
    source_path.write_bytes(b"%PDF")
    contained_image = document_dir / "figure.png"
    contained_image.write_bytes(b"png")
    outside_image = document_dir.parent / "private.png"
    outside_image.write_bytes(b"private")

    with app.app_context():
        assert routes._resolve_export_image_src("figure.png", source_path) == contained_image.resolve().as_uri()
        assert routes._resolve_export_image_src("../private.png", source_path) == ""
        assert routes._resolve_export_image_src(
            "/uploads/7/doc_processor_docs/private.png",
            source_path,
        ) == ""
        assert routes._resolve_export_image_src("https://tracker.example/pixel.png", source_path) == ""


def test_html_export_embeds_contained_images_and_removes_external_sources(tmp_path: Path):
    app = Flask(__name__, root_path=str(tmp_path))
    document_dir = tmp_path / "uploads" / "7" / "doc_processor_docs" / "doc-1"
    document_dir.mkdir(parents=True)
    source_path = document_dir / "source.pdf"
    source_path.write_bytes(b"%PDF")
    (document_dir / "figure.png").write_bytes(b"png")
    document = SimpleNamespace(file_path=str(source_path))
    soup = BeautifulSoup(
        '<img id="contained" src="figure.png"><img id="external" src="https://tracker.example/pixel.png">',
        "html.parser",
    )

    with app.app_context():
        routes._embed_local_images_in_html(soup, document)

    assert soup.find(id="contained")["src"].startswith("data:image/png;base64,")
    assert not soup.find(id="external").has_attr("src")


def test_image_review_payload_rejects_nonfinite_and_duplicate_coordinates():
    valid, error = routes._normalize_image_review_payload(
        [{"page_index": 0, "images": [{"img_idx": 1, "bbox": [0, 0, float("nan"), 10]}]}],
        ["<p>page</p>"],
    )
    assert valid is None
    assert "ordered values" in error

    valid, error = routes._normalize_image_review_payload(
        [
            {"page_index": 0, "images": []},
            {"page_index": 0, "images": []},
        ],
        ["<p>page</p>", "<p>another page</p>"],
    )
    assert valid is None
    assert "duplicated" in error


def test_image_review_payload_normalizes_valid_values():
    normalized, error = routes._normalize_image_review_payload(
        [
            {
                "page_index": 0,
                "images": [
                    {
                        "img_idx": 2,
                        "bbox": [1, 2.5, 900, 999],
                        "caption": "Diagram",
                        "keep": False,
                    }
                ],
            }
        ],
        ["<p>page</p>"],
    )

    assert error is None
    assert normalized == [
        {
            "page_index": 0,
            "images": [
                {
                    "img_idx": 2,
                    "bbox": [1.0, 2.5, 900.0, 999.0],
                    "caption": "Diagram",
                    "keep": False,
                }
            ],
        }
    ]
