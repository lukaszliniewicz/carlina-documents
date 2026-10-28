from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
import pytest
from bs4 import BeautifulSoup
from content_storage_support import bind_content_policy
from flask import Flask
from PIL import Image, ImageDraw
from werkzeug.exceptions import NotFound

from iarf_assistant.addons.doc_processor import image_crops, routes, worker
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import PdfParser
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    process_extracted_images,
)
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
    semantic_settings,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User


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


def _write_source_page(path: Path) -> None:
    image = Image.new("RGB", (100, 100), (250, 220, 20))
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, 74, 74), fill=(20, 80, 220))
    image.save(path, format="PNG")


def _write_pdf(path: Path, page_count: int = 2) -> None:
    document = fitz.open()
    colors = [(0.2, 0.8, 0.2), (0.75, 0.1, 0.7)]
    for page_index in range(page_count):
        page = document.new_page(width=100, height=100)
        page.draw_rect(page.rect, color=None, fill=colors[page_index % len(colors)])
    document.save(path)
    document.close()


def _make_document(
    tmp_path: Path,
    *,
    include_legacy_image: bool = False,
    status: str = "completed",
) -> tuple[Flask, str, Path, Path, str]:
    app = _make_app(tmp_path)
    uploads_dir = tmp_path / "uploads"
    document_dir = uploads_dir / "1" / "doc_processor_docs" / "crop-doc"
    document_dir.mkdir(parents=True)
    pdf_path = document_dir / "source.pdf"
    _write_pdf(pdf_path)
    base_png = document_dir / "temporary-render.png"
    _write_source_page(base_png)
    web_prefix = image_crops.upload_url(document_dir, uploads_dir)
    retained_url = image_crops.retain_source_png(base_png, document_dir, web_prefix)
    initial_bbox = [100.0, 100.0, 900.0, 900.0]
    crop_url = image_crops.crop_image_file(
        base_png, initial_bbox, document_dir, web_prefix
    )
    base_png.unlink()

    image_crops_by_src = {
        crop_url: {
            "source_url": retained_url,
            "page_index": 0,
            "bbox": initial_bbox,
        }
    }
    legacy_url = None
    if include_legacy_image:
        legacy_path = document_dir / "legacy-image.png"
        _write_source_page(legacy_path)
        legacy_url = image_crops.upload_url(legacy_path, uploads_dir)

    legacy_markup = (
        f'<figure><img src="{legacy_url}" alt="Legacy art" />'
        "<figcaption>Legacy caption</figcaption></figure>"
        if legacy_url
        else ""
    )
    output_html = (
        '<main_content><p id="edited-text">Edited text remains.</p>'
        f'<figure><img src="{crop_url}" alt="Blue diagram" width="80" height="80" />'
        "<figcaption>Original caption</figcaption></figure>"
        f"{legacy_markup}"
        '<p><a href="#note-1" role="doc-noteref">1</a></p>'
        '</main_content><footnotes><div id="note-1" role="doc-endnote">'
        "Footnote remains.</div></footnotes>"
    )
    canonical = canonicalize_document_html(
        output_html,
        user_id=1,
        source_file_path="/uploads/1/doc_processor_docs/crop-doc/source.pdf",
        uploads_dir=uploads_dir,
    )
    assert not canonical.errors
    settings = semantic_settings({}, canonical)
    settings["image_crops"] = image_crops_by_src

    with app.app_context():
        db.create_all()
        db.session.add(
            User(id=1, email="crop-owner@example.test", password_hash="test")
        )
        document = ProcessedDocument(
            id="crop-doc",
            user_id=1,
            filename="source.pdf",
            file_path="/uploads/1/doc_processor_docs/crop-doc/source.pdf",
            status=status,
            output_html=canonical.html,
            settings=settings,
        )
        db.session.add(document)
        db.session.commit()
        document_id = document.id
    return app, document_id, uploads_dir, document_dir, retained_url


def _invoke_route(app, route_function, document, *, method: str, json=None):
    document_id = getattr(document, "id", document)
    with (
        app.test_request_context(
            f"/addons/doc_processor/api/documents/{document_id}/image-crops",
            method=method,
            json=json,
        ),
        patch.object(routes, "current_user", SimpleNamespace(id=1)),
    ):
        return route_function.__wrapped__(document_id)


def _status(result) -> int:
    return result[1] if isinstance(result, tuple) else result.status_code


def _body(result) -> dict:
    response = result[0] if isinstance(result, tuple) else result
    return response.get_json()


def _cleanup_db() -> None:
    db.session.remove()
    db.drop_all()
    db.engine.dispose()


def test_materialization_retains_original_png_and_legacy_bbox_provenance(tmp_path):
    source_path = tmp_path / "render.png"
    output_dir = tmp_path / "owned-output"
    _write_source_page(source_path)
    crop_sources = {}

    output = process_extracted_images(
        '<figure><img src="model[0.1, 0.2, 0.8, 0.9]" /></figure>',
        str(source_path),
        str(output_dir),
        "/uploads/1/doc_processor_docs/doc/render",
        page_index=6,
        crop_sources=crop_sources,
    )

    soup = BeautifulSoup(output, "html.parser")
    crop_url = soup.img["src"]
    crop_entry = crop_sources[crop_url]
    source_url = crop_entry["source_url"]
    retained_path = output_dir / Path(source_url).name
    assert source_url.endswith(
        f"image_source_{hashlib.sha256(source_path.read_bytes()).hexdigest()}.png"
    )
    assert retained_path.is_file()
    assert crop_entry == {
        "source_url": source_url,
        "page_index": 6,
        "bbox": [100.0, 200.0, 800.0, 900.0],
    }

    source_path.unlink()
    with Image.open(retained_path) as retained:
        assert retained.size == (100, 100)


def test_materialization_crops_exact_bounds_and_preserves_separate_figcaption(tmp_path):
    source_path = tmp_path / "render-with-frame.png"
    output_dir = tmp_path / "owned-output"
    image = Image.new("RGB", (100, 100), (250, 220, 20))
    draw = ImageDraw.Draw(image)
    draw.rectangle((22, 22, 77, 77), outline=(180, 30, 40), width=2)
    draw.rectangle((25, 25, 74, 74), fill=(20, 80, 220))
    image.save(source_path, format="PNG")
    crop_sources = {}

    output = process_extracted_images(
        '<figure><img src="model[250, 250, 750, 750]" alt="Blue artwork" />'
        "<figcaption>Printed figcaption</figcaption></figure>",
        str(source_path),
        str(output_dir),
        "/uploads/1/doc_processor_docs/exact-crop",
        page_index=3,
        crop_sources=crop_sources,
    )

    soup = BeautifulSoup(output, "html.parser")
    crop_url = soup.img["src"]
    crop_path = output_dir / Path(crop_url).name
    with Image.open(crop_path) as cropped:
        assert cropped.size == (50, 50)
        assert cropped.getextrema() == ((20, 20), (80, 80), (220, 220))
    captions = soup.find_all("figcaption")
    assert len(captions) == 1
    assert captions[0].get_text() == "Printed figcaption"

    source_url = (
        "/uploads/1/doc_processor_docs/exact-crop/image_source_"
        f"{hashlib.sha256(source_path.read_bytes()).hexdigest()}.png"
    )
    assert crop_sources[crop_url] == {
        "source_url": source_url,
        "page_index": 3,
        "bbox": [250.0, 250.0, 750.0, 750.0],
    }
    with Image.open(output_dir / Path(source_url).name) as retained:
        assert retained.size == (100, 100)


def test_get_groups_retained_page_and_legacy_image_with_current_captions(tmp_path):
    app, document_id, _uploads_dir, _document_dir, retained_url = _make_document(
        tmp_path, include_legacy_image=True
    )
    try:
        with app.app_context():
            result = _invoke_route(
                app, routes.get_completed_image_crops, document_id, method="GET"
            )
            assert _status(result) == 200
            body = _body(result)
            assert body["page_count"] == 2
            assert isinstance(body["expected_updated_at"], str)
            assert len(body["pages"]) == 2
            page_group, saved_image_group = body["pages"]
            assert page_group["page_index"] == 0
            assert page_group["image_url"] == retained_url
            assert page_group["source_kind"] == "page"
            assert page_group["source_page_index"] == 0
            assert page_group["images"][0]["caption"] == "Original caption"
            assert page_group["images"][0]["keep"] is True
            assert saved_image_group["page_index"] == 1
            assert saved_image_group["source_kind"] == "saved_image"
            assert saved_image_group["images"][0]["caption"] == "Legacy caption"
            assert "output_html" not in body
    finally:
        with app.app_context():
            _cleanup_db()


def test_get_groups_provenance_images_by_source_and_page_index(tmp_path):
    uploads_dir = tmp_path / "uploads"
    document_dir = uploads_dir / "1" / "doc_processor_docs" / "grouped"
    document_dir.mkdir(parents=True)
    source_path = document_dir / "saved-original.png"
    first_crop = document_dir / "first-crop.png"
    second_crop = document_dir / "second-crop.png"
    for path in (source_path, first_crop, second_crop):
        _write_source_page(path)
    source_url = image_crops.upload_url(source_path, uploads_dir)
    first_url = image_crops.upload_url(first_crop, uploads_dir)
    second_url = image_crops.upload_url(second_crop, uploads_dir)
    bbox = [100.0, 100.0, 900.0, 900.0]
    groups = image_crops.build_completed_image_groups(
        (
            f'<figure><img src="{first_url}" /><figcaption>First</figcaption></figure>'
            f'<figure><img src="{second_url}" /><figcaption>Second</figcaption></figure>'
        ),
        {
            first_url: {"source_url": source_url, "page_index": 1, "bbox": bbox},
            second_url: {"source_url": source_url, "page_index": 1, "bbox": bbox},
        },
        uploads_dir=uploads_dir,
        owner_id=1,
        document_dir=document_dir,
    )

    assert len(groups) == 1
    assert groups[0]["page_index"] == 0
    assert groups[0]["source_kind"] == "saved_image"
    assert groups[0]["source_page_index"] == 1
    assert groups[0]["image_url"] == source_url
    assert [image["img_idx"] for image in groups[0]["images"]] == [0, 1]
    assert [image["caption"] for image in groups[0]["images"]] == [
        "First",
        "Second",
    ]


def test_completed_put_recrops_from_retained_source_and_preserves_document_markup(
    tmp_path,
):
    app, document_id, uploads_dir, _document_dir, retained_url = _make_document(
        tmp_path
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            get_result = _invoke_route(
                app, routes.get_completed_image_crops, document, method="GET"
            )
            group = _body(get_result)["pages"][0]
            current_revision = routes._document_revision(document)
            current_image = group["images"][0]
            request = {
                "expected_updated_at": current_revision,
                "pages": [
                    {
                        "page_index": group["page_index"],
                        "images": [
                            {
                                "img_idx": current_image["img_idx"],
                                "bbox": [400, 400, 600, 600],
                            }
                        ],
                    }
                ],
            }
            with (
                patch.object(routes, "_enqueue_job", side_effect=AssertionError),
                patch.object(routes, "_get_task_queue", side_effect=AssertionError),
                patch(
                    "iarf_assistant.addons.doc_processor.knowledge_adapter.sync_processed_document_source"
                ) as sync_source,
            ):
                result = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=request,
                )
            assert _status(result) == 200
            assert _body(result) == {"success": True}
            sync_source.assert_called_once_with(document)
            db.session.refresh(document)
            assert routes._document_revision(document) != current_revision
            after_first = BeautifulSoup(document.output_html, "html.parser")
            first_crop = after_first.find("img", alt="Blue diagram")["src"]
            first_path = uploads_dir / first_crop.removeprefix("/uploads/")
            with Image.open(first_path) as cropped:
                assert cropped.size == (20, 20)
                assert cropped.getextrema() == ((20, 20), (80, 80), (220, 220))
            assert (
                after_first.find(id="edited-text").get_text() == "Edited text remains."
            )
            assert after_first.find("figcaption").get_text() == "Original caption"
            assert after_first.find(id="note-1").get_text() == "Footnote remains."
            assert after_first.find("img", alt="Blue diagram").get("width") is None
            assert after_first.find("img", alt="Blue diagram").get("height") is None

            second_group = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"][0]
            second_revision = routes._document_revision(document)
            restore_request = {
                "expected_updated_at": second_revision,
                "pages": [
                    {
                        "page_index": second_group["page_index"],
                        "images": [
                            {
                                "img_idx": second_group["images"][0]["img_idx"],
                                "bbox": [0, 0, 1000, 1000],
                            }
                        ],
                    }
                ],
            }
            with patch(
                "iarf_assistant.addons.doc_processor.knowledge_adapter.sync_processed_document_source"
            ):
                restored = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=restore_request,
                )
            assert _status(restored) == 200
            db.session.refresh(document)
            restored_html = BeautifulSoup(document.output_html, "html.parser")
            restored_src = restored_html.find("img", alt="Blue diagram")["src"]
            restored_path = uploads_dir / restored_src.removeprefix("/uploads/")
            with Image.open(restored_path) as restored_image:
                assert restored_image.size == (100, 100)
                assert restored_image.getpixel((0, 0)) == (250, 220, 20)
            assert document.settings["image_crops"][restored_src] == {
                "source_url": retained_url,
                "page_index": 0,
                "bbox": [0.0, 0.0, 1000.0, 1000.0],
            }
            assert (
                restored_html.find(id="edited-text").get_text()
                == "Edited text remains."
            )
            assert restored_html.find("figcaption").get_text() == "Original caption"
            assert restored_html.find(id="note-1").get_text() == "Footnote remains."
    finally:
        with app.app_context():
            _cleanup_db()


def test_legacy_image_can_be_trimmed_then_reanchored_to_original_pdf_page(
    tmp_path,
):
    app, document_id, uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path, include_legacy_image=True
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            all_groups = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"]
            first_request = {
                "expected_updated_at": routes._document_revision(document),
                "pages": [
                    {
                        "page_index": group["page_index"],
                        "images": [
                            {"img_idx": image["img_idx"], "bbox": image["bbox"]}
                            for image in group["images"]
                        ],
                    }
                    for group in all_groups
                ],
            }
            first_request["pages"][1]["images"][0]["bbox"] = [200, 200, 800, 800]
            with patch(
                "iarf_assistant.addons.doc_processor.knowledge_adapter.sync_processed_document_source"
            ):
                result = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=first_request,
                )
            assert _status(result) == 200
            db.session.refresh(document)
            cropped_html = BeautifulSoup(document.output_html, "html.parser")
            legacy_img = cropped_html.find("img", alt="Legacy art")
            cropped_src = legacy_img["src"]
            old_source_url = document.settings["image_crops"][cropped_src]["source_url"]
            assert old_source_url.endswith("legacy-image.png")

            all_groups = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"]
            reanchor_request = {
                "expected_updated_at": routes._document_revision(document),
                "pages": [
                    {
                        "page_index": group["page_index"],
                        **({"source_page_index": 1} if index == 1 else {}),
                        "images": [
                            {"img_idx": image["img_idx"], "bbox": image["bbox"]}
                            for image in group["images"]
                        ],
                    }
                    for index, group in enumerate(all_groups)
                ],
            }
            reanchor_request["pages"][1]["images"][0]["bbox"] = [
                0,
                0,
                1000,
                1000,
            ]
            with patch(
                "iarf_assistant.addons.doc_processor.knowledge_adapter.sync_processed_document_source"
            ):
                reanchored = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=reanchor_request,
                )
            assert _status(reanchored) == 200
            db.session.refresh(document)
            final_html = BeautifulSoup(document.output_html, "html.parser")
            final_src = final_html.find("img", alt="Legacy art")["src"]
            final_metadata = document.settings["image_crops"][final_src]
            assert final_metadata["page_index"] == 1
            assert Path(final_metadata["source_url"]).name.startswith("image_source_")
            assert final_metadata["source_url"] != old_source_url
            final_path = uploads_dir / final_src.removeprefix("/uploads/")
            with Image.open(final_path) as final_image:
                pixel = final_image.getpixel((0, 0))
                assert all(
                    abs(actual - expected) <= 1
                    for actual, expected in zip(pixel, (191, 26, 179), strict=True)
                )
            final_group = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"][1]
            assert final_group["source_kind"] == "page"
            assert final_group["source_page_index"] == 1
    finally:
        with app.app_context():
            _cleanup_db()


def test_completed_crop_payload_rejects_duplicates_nonfinite_boxes_and_bad_pages():
    groups = [
        {
            "page_index": 0,
            "source_kind": "page",
            "images": [{"img_idx": 0, "bbox": [0, 0, 1000, 1000]}],
        }
    ]
    base = {
        "expected_updated_at": "revision",
        "pages": [{"page_index": 0, "images": [{"img_idx": 0, "bbox": [1, 2, 3, 4]}]}],
    }
    valid, error = image_crops.normalize_completed_crop_payload(
        base, groups, page_count=2
    )
    assert error is None
    assert valid[0]["images"][0]["bbox"] == [1.0, 2.0, 3.0, 4.0]

    duplicate_group = {**base, "pages": base["pages"] * 2}
    assert image_crops.normalize_completed_crop_payload(
        duplicate_group, groups, page_count=2
    )[1]
    nonfinite = {
        **base,
        "pages": [
            {
                "page_index": 0,
                "images": [{"img_idx": 0, "bbox": [0, 0, float("nan"), 1]}],
            }
        ],
    }
    assert image_crops.normalize_completed_crop_payload(
        nonfinite, groups, page_count=2
    )[1]
    bad_page = {
        "expected_updated_at": "revision",
        "pages": [
            {
                "page_index": 0,
                "source_page_index": 2,
                "images": [{"img_idx": 0, "bbox": [1, 2, 3, 4]}],
            }
        ],
    }
    legacy_groups = [{**groups[0], "source_kind": "saved_image"}]
    assert image_crops.normalize_completed_crop_payload(
        bad_page, legacy_groups, page_count=2
    )[1]
    changed_caption = {
        **base,
        "pages": [
            {
                "page_index": 0,
                "images": [
                    {
                        "img_idx": 0,
                        "bbox": [1, 2, 3, 4],
                        "caption": "untrusted replacement",
                    }
                ],
            }
        ],
    }
    assert image_crops.normalize_completed_crop_payload(
        changed_caption, groups, page_count=2
    )[1]


def test_completed_image_groups_reject_path_escape_foreign_and_remote_images(tmp_path):
    uploads_dir = tmp_path / "uploads"
    document_dir = uploads_dir / "1" / "doc_processor_docs" / "crop-doc"
    foreign_dir = uploads_dir / "2" / "private"
    document_dir.mkdir(parents=True)
    foreign_dir.mkdir(parents=True)
    safe_path = document_dir / "safe.png"
    foreign_path = foreign_dir / "foreign.png"
    _write_source_page(safe_path)
    _write_source_page(foreign_path)

    unsafe_urls = [
        "/uploads/1/doc_processor_docs/crop-doc/../../2/private/foreign.png",
        "/uploads/2/private/foreign.png",
        "https://images.example.test/remote.png",
    ]
    for unsafe_url in unsafe_urls:
        with pytest.raises(image_crops.ImageCropError):
            image_crops.build_completed_image_groups(
                f'<main_content><img src="{unsafe_url}" /></main_content>',
                {},
                uploads_dir=uploads_dir,
                owner_id=1,
                document_dir=document_dir,
            )

    symlink_path = document_dir / "symlink.png"
    symlink_path.symlink_to(foreign_path)
    with pytest.raises(image_crops.ImageCropError):
        image_crops.build_completed_image_groups(
            '<main_content><img src="/uploads/1/doc_processor_docs/crop-doc/symlink.png" /></main_content>',
            {},
            uploads_dir=uploads_dir,
            owner_id=1,
            document_dir=document_dir,
        )


def test_unchanged_put_is_a_revision_preserving_noop_and_missing_revision_fails(
    tmp_path,
):
    app, document_id, _uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            original_html = document.output_html
            original_markdown = document.output_markdown
            revision = routes._document_revision(document)
            pages = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"]
            unchanged_request = {
                "expected_updated_at": revision,
                "pages": [
                    {
                        "page_index": group["page_index"],
                        "images": [
                            {"img_idx": image["img_idx"], "bbox": image["bbox"]}
                            for image in group["images"]
                        ],
                    }
                    for group in pages
                ],
            }
            with patch(
                "iarf_assistant.addons.doc_processor.knowledge_adapter.sync_processed_document_source",
                side_effect=AssertionError("no-op must not synchronize"),
            ):
                unchanged = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=unchanged_request,
                )
            assert _status(unchanged) == 200
            assert _body(unchanged) == {"success": True}
            db.session.refresh(document)
            assert routes._document_revision(document) == revision
            assert document.output_html == original_html
            assert document.output_markdown == original_markdown

            missing_revision = _invoke_route(
                app,
                routes.update_completed_image_crops,
                document,
                method="PUT",
                json={"expected_updated_at": "", "pages": []},
            )
            assert _status(missing_revision) == 400
            db.session.refresh(document)
            assert routes._document_revision(document) == revision
            assert document.output_html == original_html
    finally:
        with app.app_context():
            _cleanup_db()


def test_reanchor_render_failure_keeps_document_revision_and_text(tmp_path):
    app, document_id, _uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path, include_legacy_image=True
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            original_html = document.output_html
            revision = routes._document_revision(document)
            groups = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"]
            request = {
                "expected_updated_at": revision,
                "pages": [
                    {
                        "page_index": group["page_index"],
                        **({"source_page_index": 1} if index == 1 else {}),
                        "images": [
                            {"img_idx": image["img_idx"], "bbox": image["bbox"]}
                            for image in group["images"]
                        ],
                    }
                    for index, group in enumerate(groups)
                ],
            }
            with patch.object(
                routes,
                "render_pdf_page_png",
                side_effect=image_crops.ImageCropError("synthetic render failure"),
            ):
                response = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=request,
                )
            assert _status(response) == 409
            db.session.refresh(document)
            assert routes._document_revision(document) == revision
            assert document.output_html == original_html
    finally:
        with app.app_context():
            _cleanup_db()


def test_completed_crop_route_rejects_foreign_owner_stale_revision_and_status(
    tmp_path,
):
    app, document_id, _uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            status_document = ProcessedDocument(
                id="processing-doc",
                user_id=1,
                filename="source.pdf",
                file_path=document.file_path,
                status="processing",
                output_html=document.output_html,
                settings=document.settings,
            )
            foreign_document = ProcessedDocument(
                id="foreign-doc",
                user_id=2,
                filename="source.pdf",
                file_path=document.file_path,
                status="completed",
                output_html=document.output_html,
                settings=document.settings,
            )
            db.session.add_all([status_document, foreign_document])
            db.session.add(
                User(id=2, email="foreign@example.test", password_hash="test")
            )
            db.session.commit()

            status_response = _invoke_route(
                app,
                routes.get_completed_image_crops,
                status_document,
                method="GET",
            )
            assert _status(status_response) == 409

            with pytest.raises(NotFound) as foreign_error:
                _invoke_route(
                    app,
                    routes.get_completed_image_crops,
                    foreign_document,
                    method="GET",
                )
            assert getattr(foreign_error.value, "code", None) == 404

            old_revision = routes._document_revision(document)
            fresh_html = document.output_html.replace(
                "Edited text remains.", "Newer human text wins."
            )
            semantic = canonicalize_document_html(
                fresh_html,
                user_id=1,
                source_file_path=document.file_path,
                uploads_dir=tmp_path / "uploads",
            )
            document.output_html = semantic.html
            document.settings = semantic_settings(document.settings, semantic)
            db.session.commit()
            stale_request = {
                "expected_updated_at": old_revision,
                "pages": [
                    {
                        "page_index": 0,
                        "images": [{"img_idx": 0, "bbox": [300, 300, 700, 700]}],
                    }
                ],
            }
            stale_response = _invoke_route(
                app,
                routes.update_completed_image_crops,
                document,
                method="PUT",
                json=stale_request,
            )
            assert _status(stale_response) == 409
            assert set(_body(stale_response)) == {"error"}
            db.session.refresh(document)
            assert "Newer human text wins." in document.output_html
            assert "Original caption" in document.output_html
            assert "Footnote remains." in document.output_html
    finally:
        with app.app_context():
            _cleanup_db()


def test_completed_crop_rolls_back_document_if_source_sync_fails(tmp_path):
    app, document_id, _uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            old_html = document.output_html
            old_markdown = document.output_markdown
            old_revision = routes._document_revision(document)
            group = _body(
                _invoke_route(
                    app, routes.get_completed_image_crops, document, method="GET"
                )
            )["pages"][0]
            request = {
                "expected_updated_at": old_revision,
                "pages": [
                    {
                        "page_index": group["page_index"],
                        "images": [{"img_idx": 0, "bbox": [350, 350, 650, 650]}],
                    }
                ],
            }
            with patch(
                "iarf_assistant.addons.doc_processor.knowledge_adapter.sync_processed_document_source",
                side_effect=RuntimeError("synthetic sync failure"),
            ):
                response = _invoke_route(
                    app,
                    routes.update_completed_image_crops,
                    document,
                    method="PUT",
                    json=request,
                )
            assert _status(response) == 409
            db.session.refresh(document)
            assert document.output_html == old_html
            assert document.output_markdown == old_markdown
            assert routes._document_revision(document) == old_revision
    finally:
        with app.app_context():
            _cleanup_db()


def test_worker_stores_crop_sources_only_for_completed_semantic_output():
    crop_sources = {
        "/uploads/1/doc_processor_docs/crop-doc/extracted_abcd.png": {
            "source_url": "/uploads/1/doc_processor_docs/crop-doc/image_source_abc.png",
            "page_index": 2,
            "bbox": [10, 20, 900, 950],
        }
    }
    html = "<main_content><p>Safe text.</p></main_content><footnotes></footnotes>"
    doc = SimpleNamespace(
        user_id=1,
        file_path="/uploads/1/doc_processor_docs/crop-doc/source.pdf",
        settings={"image_crops": {"old": "metadata"}},
        output_html=None,
        output_markdown=None,
    )

    worker._store_semantic_output(
        doc, html, completed=False, image_crop_sources=crop_sources
    )
    assert doc.settings["image_crops"] == {"old": "metadata"}

    worker._store_semantic_output(
        doc, html, completed=True, image_crop_sources=crop_sources
    )
    assert doc.settings["image_crops"] == crop_sources


def test_fresh_pdf_worker_completion_carries_parser_crop_sources_into_settings(
    tmp_path,
):
    app, document_id, _uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            document.status = "created"
            settings = dict(document.settings)
            settings["engine_id"] = "llm_vision"
            settings["pdf_options"] = {
                "correct_images": False,
                "extract_images": True,
            }
            document.settings = settings
            db.session.commit()
            crop_sources = dict(document.settings["image_crops"])
            output_html = document.output_html

            class FreshParser:
                def __init__(self):
                    self.settings = {}
                    self.image_crop_sources = crop_sources
                    self.metrics = {}

                def process(self, *, auto_mode, **_kwargs):
                    assert auto_mode is False
                    assert self.settings["pdf_options"]["correct_images"] is False
                    return "COMPLETED", output_html

            parser = FreshParser()

            def parser_factory(_path, _user_id, _llm_service, *, settings):
                parser.settings = settings
                return parser

            queue = SimpleNamespace(set_status=lambda *_args, **_kwargs: None)
            payload = {"document_id": document_id, "engine_id": "llm_vision"}
            with (
                patch(
                    "iarf_assistant.addons.doc_processor.execution_recovery.validate_delivery",
                    return_value=True,
                ),
                patch.object(worker, "_wait_for_unchanged_passive_checkpoint"),
                patch.object(worker, "_reserve_passive_write", return_value=False),
                patch.object(
                    worker, "_reserve_document_result_persistence", return_value=True
                ),
                patch.object(worker, "_sync_document_metadata"),
                patch.object(worker, "get_parser", side_effect=parser_factory),
                patch.object(worker, "LLMService"),
                patch.object(worker, "sync_processed_document_source"),
                patch.object(worker, "_sync_project_entry"),
            ):
                result = worker.process_document(payload, queue, "fresh-crop-job")

            assert result["message"] == "Document parsed successfully."
            db.session.refresh(document)
            assert document.status == "completed"
            assert document.settings["image_crops"] == crop_sources
    finally:
        with app.app_context():
            _cleanup_db()


def test_resume_pdf_worker_stores_new_parser_crop_sources(tmp_path):
    app, document_id, _uploads_dir, _document_dir, _retained_url = _make_document(
        tmp_path
    )
    try:
        with app.app_context():
            document = db.session.get(ProcessedDocument, document_id)
            document.status = "reviewing_images"
            settings = dict(document.settings)
            settings["pdf_state"] = {
                "phase": "PAUSED_IMAGE_REVIEW",
                "included_pages": [0],
                "html_parts": [document.output_html],
                "png_paths": [None],
                "boundary_flags": {},
            }
            document.settings = settings
            db.session.commit()
            crop_sources = dict(document.settings["image_crops"])
            output_html = document.output_html

            class ResumeParser:
                def __init__(self):
                    self.image_crop_sources = crop_sources
                    self.metrics = {}

                def resume_processing(self):
                    return "COMPLETED", output_html

            parser = ResumeParser()
            queue = SimpleNamespace(set_status=lambda *_args, **_kwargs: None)
            with (
                patch.object(worker, "get_parser", return_value=parser) as get_parser,
                patch.object(worker, "LLMService"),
                patch.object(
                    worker, "_reserve_document_result_persistence", return_value=True
                ),
                patch.object(worker, "sync_processed_document_source"),
                patch.object(worker, "_sync_project_entry"),
            ):
                result = worker.resume_pdf_parsing(
                    {"document_id": document_id}, queue, "resume-crop-job"
                )

            assert result["message"] == "PDF parsing resumed and completed."
            assert (
                get_parser.call_args.kwargs["state"]["phase"] == "PAUSED_IMAGE_REVIEW"
            )
            db.session.refresh(document)
            assert document.status == "completed"
            assert document.settings["image_crops"] == crop_sources
    finally:
        with app.app_context():
            _cleanup_db()


def test_pdf_parser_resume_materializes_crops_and_collects_original_page(tmp_path):
    app, _document_id, uploads_dir, document_dir, _retained_url = _make_document(
        tmp_path
    )
    temporary_png = document_dir / "resume-temporary.png"
    _write_source_page(temporary_png)
    try:
        with app.app_context():
            parser = PdfParser(
                str(document_dir / "source.pdf"),
                1,
                None,
                settings={
                    "parsing_model": "synthetic/no-provider",
                    "pdf_options": {
                        "extract_images": True,
                        "correct_images": False,
                        "llm_boundary_merge": False,
                    },
                },
                state={
                    "html_parts": [
                        (
                            '<main_content><figure><img src="model[0.1,0.2,0.8,0.9]" />'
                            "</figure></main_content>"
                        )
                    ],
                    "included_pages": [0],
                    "png_paths": [str(temporary_png)],
                    "boundary_flags": {},
                },
            )
            status, output_html = parser.resume_processing()

        assert status == "COMPLETED"
        assert len(parser.image_crop_sources) == 1
        crop_url, provenance = next(iter(parser.image_crop_sources.items()))
        assert crop_url in output_html
        assert provenance["page_index"] == 0
        assert provenance["bbox"] == [100.0, 200.0, 800.0, 900.0]
        retained_path = uploads_dir / provenance["source_url"].removeprefix("/uploads/")
        assert retained_path.is_file()
        assert retained_path.name.startswith("image_source_")
        assert not temporary_png.exists()
    finally:
        with app.app_context():
            _cleanup_db()
