from __future__ import annotations

import base64
import hashlib
import io
import random
import zipfile
from xml.etree import ElementTree as ET

import pytest
from PIL import Image
from test_doc_processor_epub_publishing import (
    OPF_NS,
    _build,
    _contents,
    _package,
)

from carlina.core.epub_export import (
    EpubExportContractError,
    effective_epub_recipe,
    normalize_epub_options,
    normalize_epub_quality,
)
from iarf_assistant.addons.doc_processor.epub_quality import preflight_epub
from iarf_assistant.addons.doc_processor.epub_validation import validate_epub_structure

FRONT = (
    '<main><h1 id="half">Book</h1><p id="editor1">Edited by Someone</p>'
    '<h1 id="title" epub:type="titlepage">Book</h1><p id="editor2">Edited by Someone</p>'
    '<h1 id="rights" epub:type="copyright-page">Rights</h1>'
    '<p id="legal">Copyright 2026. All rights reserved.</p><p id="credit">Cover art: synthetic example.</p>'
    '<h1 id="contributors" epub:type="contributors">Contributors</h1><p id="bio">An editor.</p>'
    '<h2 id="chapter" epub:type="chapter">A long chapter title</h2><p id="body">Body words.</p></main>'
)
METADATA = {"title": "Book", "language": "en"}


def _image_uri(image: Image.Image) -> tuple[str, bytes]:
    stream = io.BytesIO()
    image.save(stream, "PNG")
    data = stream.getvalue()
    return "data:image/png;base64," + base64.b64encode(data).decode(), data


def test_v2_uses_one_manifested_stylesheet_and_visible_contents_in_spine(tmp_path):
    packager, path, source = _build(
        tmp_path, FRONT, options={"chapter_level": 2}, title="Book"
    )
    package = _package(path)
    manifest = package.find("opf:manifest", OPF_NS)
    assert manifest is not None
    styles = [item for item in manifest if item.get("media-type") == "text/css"]
    assert len(styles) == 1
    with zipfile.ZipFile(path) as archive:
        css = archive.read("EPUB/" + styles[0].get("href")).decode()
        for name in [name for name in archive.namelist() if name.endswith(".xhtml")]:
            root = ET.fromstring(archive.read(name))
            assert (
                root.find('.//{http://www.w3.org/1999/xhtml}link[@rel="stylesheet"]')
                is not None
            )
            assert root.find(".//{http://www.w3.org/1999/xhtml}style") is None
        assert "h1 { font-size: 1.5em" in css and "break-after: auto" in css
        assert "margin-inline" in css and "white-space: nowrap" not in css
        assert "color:" not in css and "font-family:" not in css
    spine = [item.get("idref") for item in package.find("opf:spine", OPF_NS)]
    assert spine.index("nav") == 3
    assert validate_epub_structure(str(path)) == (True, None)
    assert source.output_html == FRONT
    assert packager.quality_report()["profile"] == "publishing-v2"


def test_explicit_v1_retains_inline_styles_and_original_spine(tmp_path):
    _, path, _ = _build(
        tmp_path,
        FRONT,
        options={"profile": "publishing-v1", "chapter_level": 2},
        title="Book",
    )
    package = _package(path)
    assert all(item.get("idref") != "nav" for item in package.find("opf:spine", OPF_NS))
    assert all(
        item.get("media-type") != "text/css"
        for item in package.find("opf:manifest", OPF_NS)
    )
    with zipfile.ZipFile(path) as archive:
        assert (
            ET.fromstring(archive.read("EPUB/content-001.xhtml")).find(
                ".//{http://www.w3.org/1999/xhtml}style"
            )
            is not None
        )
    options = normalize_epub_options({"profile": "publishing-v1"})
    assert set(options) == {
        "profile",
        "chapter_level",
        "note_placement",
        "group_endnotes",
        "direction",
        "roles",
        "page_labels",
        "table_headers",
        "image_descriptions",
        "decorative_images",
        "accessibility_evaluation",
    }


def test_editorial_omissions_edits_order_and_legal_text_remain_source_scoped(tmp_path):
    reviewed = preflight_epub(FRONT, METADATA, {"chapter_level": 2})
    units = reviewed["outline"]["front_matter"]
    assert units[1]["blocks"][0]["duplicate_of"] == "editor1"
    choices = {
        "chapter_level": 2,
        "omitted_front_matter": {"half": units[0]["sha256"]},
        "front_matter_order": ["rights", "title", "contributors"],
        "front_matter_edits": {
            "bio": {
                "sha256": units[3]["blocks"][0]["sha256"],
                "text": "Editor and researcher.",
            }
        },
    }
    _, path, source = _build(tmp_path, FRONT, title="Book", options=choices)
    text = " ".join(" ".join(root.itertext()) for _, root in _contents(path))
    assert text.count("Edited by Someone") == 1
    assert "Copyright 2026. All rights reserved." in text
    assert "Cover art: synthetic example." in text
    assert "Editor and researcher." in text and "Body words." in text
    assert text.index("Copyright") < text.index("Edited by")
    assert (
        source.output_html == FRONT and source.settings["document_metadata"] == METADATA
    )
    assert validate_epub_structure(str(path)) == (True, None)


@pytest.mark.parametrize("text", ["x" * 4097, "漢" * 1366])
def test_long_front_matter_previews_cannot_replace_unseen_text(text):
    html = FRONT.replace("Copyright 2026. All rights reserved.", text)
    review = preflight_epub(html, METADATA, {"chapter_level": 2})
    block = next(
        unit for unit in review["outline"]["front_matter"] if unit["id"] == "rights"
    )["blocks"][0]
    assert block["truncated"] is True and block["editable"] is False
    assert len(block["text"].encode("utf-8")) <= 4096
    with pytest.raises(EpubExportContractError, match="epub-front-matter-edit-target"):
        preflight_epub(
            html,
            METADATA,
            {
                "chapter_level": 2,
                "front_matter_edits": {
                    block["id"]: {
                        "sha256": block["sha256"],
                        "text": "Only the excerpt.",
                    }
                },
            },
        )


def test_editorial_review_rejects_changed_text_linked_targets_and_invalid_order():
    units = preflight_epub(FRONT, METADATA, {"chapter_level": 2})["outline"][
        "front_matter"
    ]
    choices = {"chapter_level": 2, "omitted_front_matter": {"half": units[0]["sha256"]}}
    with pytest.raises(EpubExportContractError, match="epub-front-matter-stale"):
        preflight_epub(
            FRONT.replace("Edited by Someone", "Edited by Another", 1),
            METADATA,
            choices,
        )
    linked = FRONT.replace("Body words.", '<a href="#half">Title</a>')
    with pytest.raises(EpubExportContractError, match="epub-front-matter-linked"):
        preflight_epub(linked, METADATA, choices)
    with pytest.raises(EpubExportContractError, match="epub-front-matter-order"):
        preflight_epub(
            FRONT, METADATA, {"chapter_level": 2, "front_matter_order": ["rights"]}
        )
    with pytest.raises(EpubExportContractError, match="epub-front-matter-target"):
        preflight_epub(
            FRONT,
            METADATA,
            {"chapter_level": 2, "omitted_front_matter": {"body": "1" * 64}},
        )


def test_explicit_front_section_start_creates_copyright_page_without_changing_source(
    tmp_path,
):
    html = '<main><h1 id="title" epub:type="titlepage">Book</h1><p id="editor">Edited by Someone</p><span id="page-2" epub:type="pagebreak" role="doc-pagebreak" aria-label="2"></span><p id="legal">Copyright 2026.</p><p id="credit">Cover art credit.</p><h2 id="chapter" epub:type="chapter">Body</h2><p>Words.</p></main>'
    initial = preflight_epub(html, METADATA, {"chapter_level": 2})
    legal = next(
        block
        for block in initial["outline"]["front_matter"][0]["blocks"]
        if block["id"] == "legal"
    )
    assert legal["can_start_section"] is True
    choices = {
        "chapter_level": 2,
        "front_matter_breaks": {
            "legal": {"sha256": legal["sha256"], "role": "copyright-page"}
        },
    }
    reviewed = preflight_epub(html, METADATA, choices)
    assert [
        (unit["id"], unit["role"]) for unit in reviewed["outline"]["front_matter"]
    ] == [("title", "titlepage"), ("legal", "copyright-page")]
    _, path, source = _build(tmp_path, html, title="Book", options=choices)
    contents = _contents(path)
    copyright_page = next(
        root
        for _, root in contents
        if any(tag.get("id") == "legal" for tag in root.iter())
    )
    assert any(tag.get("id") == "page-2" for tag in copyright_page.iter())
    assert "Copyright 2026." in " ".join(copyright_page.itertext())
    assert "Cover art credit." in " ".join(copyright_page.itertext())
    assert "Edited by Someone" not in " ".join(copyright_page.itertext())
    assert any(
        tag.get("class") == "epub-copyright-page" for tag in copyright_page.iter()
    )
    assert source.output_html == html
    assert validate_epub_structure(str(path)) == (True, None)
    assert [
        item.get("idref") for item in _package(path).find("opf:spine", OPF_NS)
    ].index("nav") == 2


def test_front_section_start_requires_top_level_source_witness():
    html = '<main><h1 epub:type="titlepage">Book</h1><div><p id="nested">Legal paragraph</p></div><h2 epub:type="chapter">Body</h2><p id="body">Words.</p></main>'
    for target in ("nested", "body"):
        with pytest.raises(
            EpubExportContractError, match="epub-front-matter-break-target"
        ):
            preflight_epub(
                html,
                METADATA,
                {
                    "chapter_level": 2,
                    "front_matter_breaks": {
                        target: {"sha256": "a" * 64, "role": "copyright-page"}
                    },
                },
            )
    plain = html.replace(
        '<div><p id="nested">Legal paragraph</p></div>',
        '<p id="legal">Legal paragraph</p>',
    )
    with pytest.raises(EpubExportContractError, match="epub-front-matter-stale"):
        preflight_epub(
            plain,
            METADATA,
            {
                "chapter_level": 2,
                "front_matter_breaks": {
                    "legal": {"sha256": "a" * 64, "role": "copyright-page"}
                },
            },
        )
    for value in (
        {"sha256": "a" * 64, "role": "chapter"},
        {"sha256": "a" * 64, "role": []},
    ):
        with pytest.raises(
            EpubExportContractError, match="epub-options-front-matter-break"
        ):
            normalize_epub_options({"front_matter_breaks": {"legal": value}})
    options = normalize_epub_options(
        {
            "front_matter_edits": {
                "legal": {"sha256": "a" * 64, "text": "Line one\nLine two"}
            }
        }
    )
    assert options["front_matter_edits"]["legal"]["text"] == "Line one Line two"
    with pytest.raises(EpubExportContractError, match="epub-options-front-matter-edit"):
        normalize_epub_options(
            {
                "front_matter_edits": {
                    "legal": {"sha256": "a" * 64, "text": "Bad\x00control"}
                }
            }
        )


def test_unmarked_front_illustration_can_be_explicitly_omitted(tmp_path):
    uri, _ = _image_uri(Image.new("RGB", (12, 16), "blue"))
    html = f'<main><figure><img src="{uri}" alt="Extracted illustration" /></figure><h1 epub:type="titlepage">Book</h1><h2 epub:type="chapter">Body</h2><p>Words.</p></main>'
    initial = preflight_epub(html, METADATA, {"chapter_level": 2})
    unit = initial["outline"]["front_matter"][0]
    assert unit["id"] == "epub-image-1" and unit["title"] == "Extracted illustration"
    _, path, source = _build(
        tmp_path,
        html,
        title="Book",
        options={
            "chapter_level": 2,
            "omitted_front_matter": {unit["id"]: unit["sha256"]},
        },
    )
    assert not any(
        root.findall(".//{http://www.w3.org/1999/xhtml}img")
        for _, root in _contents(path)
    )
    assert source.output_html == html


@pytest.mark.parametrize("reused", [False, True])
def test_cover_compression_keeps_unclassified_interior_bytes(tmp_path, reused):
    # Deterministic, high-color photographic texture; line art is tested separately.
    generator = random.Random(41)
    data = bytes(
        max(0, min(255, level + generator.randint(-16, 16)))
        for y in range(768)
        for x in range(512)
        for level in (x * 255 // 511, y * 255 // 767, (x + y) * 255 // 1278)
    )
    uri, original = _image_uri(Image.frombytes("RGB", (512, 768), data))
    interior_uri, interior = (
        (uri, original) if reused else _image_uri(Image.new("RGB", (20, 30), "blue"))
    )
    html = f'<main><figure epub:type="cover"><img id="cover" class="cover" alt="Cover" src="{uri}" /></figure><h2 epub:type="chapter">Body</h2><img id="interior" alt="Painting" src="{interior_uri}" /></main>'
    packager, path, source = _build(tmp_path, html, options={"chapter_level": 2})
    with zipfile.ZipFile(path) as archive:
        pictures = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.endswith((".png", ".jpg"))
        }
        assert interior in pictures.values()
        jpeg = next(value for name, value in pictures.items() if name.endswith(".jpg"))
        assert len(jpeg) < len(original)
        with Image.open(io.BytesIO(jpeg)) as optimized:
            assert optimized.size == (512, 768)
    report = normalize_epub_quality(
        packager.quality_report(ace="passed", ace_version="1.4.6")
    )
    assert report["checks"]["ace"] == "passed" and report["ace_version"] == "1.4.6"
    assert report["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["image_bytes"]["source"] == len(original) + (
        0 if reused else len(interior)
    )
    assert report["image_bytes"]["packaged"] == sum(
        len(data) for data in pictures.values()
    )
    if not reused:
        assert report["image_bytes"]["packaged"] < report["image_bytes"]["source"]
    assert source.output_html == html


@pytest.mark.parametrize("kind", ["line", "alpha", "preserve"])
def test_balanced_preserves_line_art_alpha_and_explicit_originals(tmp_path, kind):
    image = Image.new(
        "RGBA" if kind == "alpha" else "RGB",
        (50, 80),
        (200, 10, 20, 127) if kind == "alpha" else "white",
    )
    uri, original = _image_uri(image)
    html = f'<main><h2 epub:type="chapter">Body</h2><img id="picture" alt="Diagram" src="{uri}" /></main>'
    options = {
        "chapter_level": 2,
        "photographic_images": ["picture"],
        "image_policy": "preserve" if kind == "preserve" else "balanced",
    }
    _, path, _ = _build(tmp_path, html, options=options)
    with zipfile.ZipFile(path) as archive:
        assert original in [
            archive.read(name) for name in archive.namelist() if name.endswith(".png")
        ]


def test_long_description_confirmation_is_separate_and_evidence_bound(tmp_path):
    uri, _ = _image_uri(Image.new("RGB", (10, 10), "red"))
    html = f'<main><h2 epub:type="chapter">Chart</h2><figure><img id="chart" alt="Two values" src="{uri}" /><div class="image-description-v1" id="details"><p>First 10; second 20.</p></div><p class="image-description-link"><a href="#details">Full description</a></p></figure></main>'
    options = {"chapter_level": 2, "image_descriptions": {"chart": "Two values"}}
    preflight = preflight_epub(html, METADATA, options)
    image = preflight["outline"]["images"][0]
    assert image["description_id"] == "details"
    assert any(
        issue["code"] == "long-description-review"
        for issue in preflight["quality"]["issues"]
    )
    reviewed = {**options, "long_descriptions": {"chart": image["description_sha256"]}}
    packager, path, _ = _build(tmp_path, html, options=reviewed)
    assert not any(
        issue["code"] == "long-description-review"
        for issue in packager.quality_report()["issues"]
    )
    assert packager.quality_report()["checks"]["manual"] == "not_reviewed"
    metadata = _package(path).find("opf:metadata", OPF_NS)
    assert not any(
        node.get("property") in {"schema:accessModeSufficient", "dcterms:conformsTo"}
        for node in metadata
    )
    with pytest.raises(EpubExportContractError, match="epub-options-description-stale"):
        preflight_epub(html.replace("second 20", "second 30"), METADATA, reviewed)


def test_stale_recipe_keeps_general_preferences_but_clears_content_decisions():
    recipe = {
        "epub_options": {
            "note_placement": "chapter",
            "chapter_level": 2,
            "omitted_front_matter": {"old": "a" * 64},
            "front_matter_breaks": {
                "legal": {"sha256": "a" * 64, "role": "copyright-page"}
            },
            "image_descriptions": {"picture": "Old alt"},
        },
        "epub_cover": {"mode": "pdf_page", "page": 1, "crop": [0, 0, 0.5, 1]},
        "epub_image_mode": "auto",
    }
    effective = effective_epub_recipe(recipe, stale=True)
    assert effective["epub_options"]["omitted_front_matter"] == {}
    assert effective["epub_options"]["front_matter_breaks"] == {}
    assert effective["epub_options"]["image_descriptions"] == {}
    assert effective["epub_options"]["note_placement"] == "chapter"
    assert effective["epub_cover"] == {}
    assert recipe["epub_options"]["omitted_front_matter"] == {"old": "a" * 64}
    generated = {**recipe, "epub_cover": {"mode": "generated"}}
    assert effective_epub_recipe(generated, stale=True)["epub_cover"] == {
        "mode": "generated"
    }


def test_diagram_review_binds_topology_and_translatable_labels(tmp_path):
    html = (
        '<main><h2 epub:type="chapter">Process</h2><figure class="mermaid-diagram-v1" id="diagram1">'
        '<pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]\nn2["{{b}}"]\nn1 --&gt; n2</code></pre>'
        '<div class="mermaid-labels"><p id="diagram1--a">First</p><p id="diagram1--b">Second</p></div></figure></main>'
    )
    options = {
        "chapter_level": 2,
        "image_descriptions": {"diagram1": "Two-step process. Structure follows."},
    }
    reviewed = preflight_epub(html, METADATA, options)
    digest = reviewed["outline"]["images"][0]["description_sha256"]
    choices = {**options, "long_descriptions": {"diagram1": digest}}
    packager, path, _ = _build(tmp_path, html, options=choices)
    assert not any(
        issue["code"] == "long-description-review"
        for issue in packager.quality_report()["issues"]
    )
    assert validate_epub_structure(str(path)) == (True, None)
    for changed in (
        html.replace("Second", "A different meaning"),
        html.replace("n1 --&gt; n2", "n2 --&gt; n1"),
    ):
        with pytest.raises(
            EpubExportContractError, match="epub-options-description-stale"
        ):
            preflight_epub(changed, METADATA, choices)


def test_optional_photograph_encoder_failure_preserves_original(tmp_path, monkeypatch):
    uri, original = _image_uri(Image.new("RGB", (30, 40), "red"))
    html = f'<main><h2 epub:type="chapter">Body</h2><img id="photo" alt="Photo" src="{uri}" /></main>'
    from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager

    def cannot_optimize(_image):
        raise OSError("Synthetic encoder failure")

    monkeypatch.setattr(
        EPUBPackager, "_image_is_low_color", staticmethod(cannot_optimize)
    )
    _, path, _ = _build(
        tmp_path, html, options={"chapter_level": 2, "photographic_images": ["photo"]}
    )
    with zipfile.ZipFile(path) as archive:
        assert original in [
            archive.read(name) for name in archive.namelist() if name.endswith(".png")
        ]
