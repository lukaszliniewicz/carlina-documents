from __future__ import annotations

import hashlib
import html
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from carlina.core import diagram_rendering, semantic_images
from carlina.core.diagram_rendering import (
    project_diagrams_html,
    render_diagram,
    validate_svg,
)
from carlina.core.semantic_diagrams import (
    diagnose_mermaid_diagrams,
    materialize_mermaid,
)
from carlina.core.semantic_export import (
    SemanticExportContractError,
    opaque_resource_path,
    validate_semantic_export_html,
)
from carlina.core.semantic_images import diagnose_image_descriptions
from iarf_assistant.addons.doc_processor.semantic_document import (
    namespace_semantic_fragment,
)

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="100pt" viewBox="0 0 100 100"><title>G</title></svg>'
SVG_NS = "{http://www.w3.org/2000/svg}"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _diagram(
    *, figure_id="diagram1", descriptions=True, language="en", direction="ltr"
):
    template = (
        'flowchart TD\nn1["{{start}}"]\nn2{"{{ready}}"}\nn3["{{review}}"]\n'
        'n4["{{other}}"]\nn5("{{send}}")\nn6["{{unused}}"]\nn1 --> n2\n'
        'n2 -->|"{{yes}}"| n5\nn2 -->|"{{no}}"| n3\nn3 --> n2\nn4 --- n5'
    )
    labels = {
        "start": "Start & go",
        "ready": "Ready?",
        "review": "Review",
        "other": "Review",
        "send": "Send",
        "unused": "Disconnected",
        "yes": "Yes",
        "no": "No",
    }
    paragraphs = "".join(
        f'<p id="{figure_id}--{key}">{html.escape(value)}</p>'
        for key, value in labels.items()
    )
    additional = (
        (
            f'<p class="image-alt-v1" id="{figure_id}-alt">Readiness decision. Full description follows.</p>'
            f'<div class="image-description-v1" id="{figure_id}-description"><p>A readiness decision with a revision loop.</p></div>'
        )
        if descriptions
        else ""
    )
    return (
        f'<figure class="mermaid-diagram-v1" id="{figure_id}" lang="{language}" dir="{direction}">'
        f'<pre class="mermaid-template"><code>{html.escape(template)}</code></pre>'
        f'<div class="mermaid-labels">{paragraphs}</div>'
        "<figcaption>Figure 7: The printed caption.</figcaption>"
        f"{additional}</figure>"
    )


def _crop(
    description="<p>The Yes branch leads to Send; the No branch returns to Review.</p>",
):
    return (
        f'<figure><img src="{opaque_resource_path("res_0123456789abcdef")}" alt="Readiness diagram. The description follows.">'
        "<figcaption>Figure 2: Printed caption.</figcaption>"
        '<p class="image-description-link"><a href="#crop-description">Diagram description</a></p>'
        f'<div class="image-description-v1" id="crop-description">{description}</div></figure>'
    )


@pytest.fixture
def controlled_renderer(monkeypatch):
    monkeypatch.setattr(
        diagram_rendering, "_dot_executable", lambda: Path("controlled-dot")
    )

    def run(command, **kwargs):
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(SVG if "-Tsvg" in command else b"\x89PNG\r\n\x1a\nsynthetic")
        return subprocess.CompletedProcess(command, 0)

    return run


def test_extended_descriptions_are_optional_but_claimed_structure_is_validated():
    for source in (_crop(), _diagram(), _diagram(descriptions=False)):
        soup = BeautifulSoup(source, "html.parser")
        assert diagnose_image_descriptions(soup) == ()
        validate_semantic_export_html(source)
    assert diagnose_mermaid_diagrams(BeautifulSoup(_diagram(), "html.parser")) == ()
    literal = materialize_mermaid(BeautifulSoup(_diagram(), "html.parser").figure)
    assert 'n2 -->|"Yes"| n5' in literal
    assert "readiness decision" not in literal


@pytest.mark.parametrize(
    "replacement",
    [
        lambda source: source.replace('id="crop-description"', 'id="bad/id"'),
        lambda source: source.replace('href="#crop-description"', 'href="#elsewhere"'),
        lambda source: source.replace(
            'class="image-description-v1"', 'class="image-description-v1 extra"'
        ),
        lambda source: source.replace("<div class=", "<div hidden class="),
        lambda source: source.replace("<figure>", '<figure aria-hidden="true">'),
        lambda source: source.replace("<figure>", '<figure role="img">'),
        lambda source: "<div hidden>" + source + "</div>",
        lambda source: '<div role="img">' + source + "</div>",
        lambda source: source.replace("<p>The Yes", '<p aria-hidden="true">The Yes'),
        lambda source: source.replace(
            "</figure>", '</figure><p class="image-alt-v1">Secret adaptation</p>'
        ),
        lambda source: (
            source + '<span id="crop-description">Private source text</span>'
        ),
        lambda source: source.replace(
            '<div class="image-description-v1"', '<section class="image-description-v1"'
        ).replace("</div>", "</section>"),
    ],
)
def test_malformed_markers_fail_closed_with_source_free_diagnostics(replacement):
    source = replacement(_crop())
    codes = diagnose_image_descriptions(BeautifulSoup(source, "html.parser"))
    assert codes
    assert all(code.startswith("image-") for code in codes)
    assert not any("Private" in code or "Secret" in code for code in codes)
    with pytest.raises(
        SemanticExportContractError, match="semantic-export-image-description"
    ):
        validate_semantic_export_html(source)


@pytest.mark.parametrize(
    "description",
    [
        "<h2>Invented heading</h2>",
        '<a href="#elsewhere">Unrelated link</a>',
        '<img src="other.png" alt="Nested">',
        "<figure><p>Nested</p></figure>",
        '<p class="image-alt-v1">Nested marker</p>',
        "<p></p>",
    ],
)
def test_extended_description_uses_only_bounded_navigable_text_structures(description):
    assert diagnose_image_descriptions(BeautifulSoup(_crop(description), "html.parser"))


def test_limits_count_utf8_bytes_and_elements_and_reject_duplicate_markers(monkeypatch):
    source = _crop("<p>éééééé</p>")
    soup = BeautifulSoup(source, "html.parser")
    description = soup.select_one(".image-description-v1")
    monkeypatch.setattr(
        semantic_images, "MAX_IMAGE_DESCRIPTION_BYTES", len(str(description)) + 1
    )
    assert "image-description-limit" in diagnose_image_descriptions(soup)
    monkeypatch.setattr(semantic_images, "MAX_IMAGE_DESCRIPTION_BYTES", 128 * 1024)
    monkeypatch.setattr(semantic_images, "MAX_IMAGE_DESCRIPTION_ELEMENTS", 1)
    assert "image-description-limit" in diagnose_image_descriptions(
        BeautifulSoup(_crop("<p><em>Text</em></p>"), "html.parser")
    )
    duplicate = _crop().replace(
        "</figure>",
        '<p class="image-description-link"><a href="#crop-description">Again</a></p></figure>',
    )
    assert "image-description-duplicate" in diagnose_image_descriptions(
        BeautifulSoup(duplicate, "html.parser")
    )


def test_cross_figure_description_collisions_reject_diagram_materialization():
    source = _diagram() + '<span id="diagram1-description">Collision</span>'
    soup = BeautifulSoup(source, "html.parser")
    assert "image-description-id" in diagnose_image_descriptions(soup)
    with pytest.raises(ValueError, match="diagram-duplicate-id"):
        materialize_mermaid(soup.figure)


@pytest.mark.parametrize("output_format", ["svg", "png"])
def test_projection_exposes_every_node_edge_branch_loop_and_undirected_connection(
    controlled_renderer, output_format
):
    prefix = '<article data-unchanged=" a ">Before</article>\n'
    suffix = "\n<!-- untouched --><p>After</p>"
    original = prefix + _diagram() + suffix
    projected = project_diagrams_html(
        original, output_format=output_format, runner=controlled_renderer
    )
    assert projected.startswith(prefix) and projected.endswith(suffix)
    soup = BeautifulSoup(projected, "html.parser")
    assert soup.img["alt"] == "Readiness decision. Full description follows."
    assert soup.figure["lang"] == "en" and soup.figure["dir"] == "ltr"
    assert soup.find(id="diagram1-alt") is not None
    assert (
        soup.find(id="diagram1-description").get_text()
        == "A readiness decision with a revision loop."
    )
    assert soup.get_text().count("Figure 7: The printed caption.") == 1
    assert soup.figure.find_all(True, recursive=False)[0].name == "figcaption"
    assert [
        row.get_text("|", strip=True) for row in soup.select(".diagram-nodes tbody tr")
    ] == [
        "1|Start & go|Rectangle",
        "2|Ready?|Diamond",
        "3|Review|Rectangle",
        "4|Review|Rectangle",
        "5|Send|Rounded rectangle",
        "6|Disconnected|Rectangle",
    ]
    assert [
        row.get_text("|", strip=True)
        for row in soup.select(".diagram-connections tbody tr")
    ] == [
        "1|2|Directed",
        "2|5|Directed|Yes",
        "2|3|Directed|No",
        "3|2|Directed",
        "4|5|Undirected",
    ]
    for anchor in soup.select(".image-description-link a, .diagram-description-link a"):
        assert soup.find(id=anchor["href"][1:]) is not None
    assert soup.select(
        '.diagram-nodes th[scope="col"], .diagram-connections th[scope="col"]'
    )
    assert soup.select('.diagram-nodes th[scope="row"]')
    assert not soup.img.has_attr("aria-describedby")
    assert not soup.img.has_attr("data-epub-description-reviewed")
    assert "mermaid-template" in original


def test_svg_has_a_name_and_a_complete_structural_description(controlled_renderer):
    rendered = render_diagram(
        BeautifulSoup(_diagram(), "html.parser").figure, runner=controlled_renderer
    )
    assert validate_svg(rendered) == rendered
    root = ET.fromstring(rendered)
    assert root.get("role") == "img"
    assert root.get("aria-labelledby") == "diagram-title diagram-description"
    assert (
        root.find(SVG_NS + "title").text
        == "Readiness decision. Full description follows."
    )
    description = root.find(SVG_NS + "desc").text
    assert "Node 6 (Rectangle): Disconnected." in description
    assert "From node 2; To node 5; Directed. Connection label: Yes." in description
    assert "From node 3; To node 2; Directed." in description
    assert "First node 4; Second node 5; Undirected." in description
    assert root.get(XML_LANG) == "en"


def test_description_ids_are_global_collision_safe_and_bounded(controlled_renderer):
    identifier = "d" * 110
    collision = (
        "diagram-structure-" + hashlib.sha256(identifier.encode()).hexdigest()[:20]
    )
    source = f'<p id="{collision}">Unchanged collision</p>' + _diagram(
        figure_id=identifier, descriptions=False
    )
    projected = project_diagrams_html(source, runner=controlled_renderer)
    soup = BeautifulSoup(projected, "html.parser")
    description = soup.select_one(".diagram-description-v1")
    assert description["id"] == collision + "-1"
    assert len(description["id"]) <= 128
    assert len({tag["id"] for tag in soup.find_all(id=True)}) == len(
        soup.find_all(id=True)
    )
    assert (
        soup.select_one(".diagram-description-link a")["href"]
        == "#" + description["id"]
    )


@pytest.mark.parametrize(
    "language, direction, generated_language",
    [("ar", "rtl", "ar"), ("pl-PL", "ltr", "pl"), ("ja", "ltr", "en")],
)
def test_generated_wording_has_its_own_language_without_relabelling_original_text(
    controlled_renderer, language, direction, generated_language
):
    source = _diagram(descriptions=False, language=language, direction=direction)
    soup = BeautifulSoup(
        project_diagrams_html(source, runner=controlled_renderer), "html.parser"
    )
    assert soup.figure["lang"] == language and soup.figure["dir"] == direction
    assert soup.select_one(".diagram-description-v1")["lang"] == language
    assert soup.select_one(".diagram-nodes caption")["lang"] == generated_language
    assert soup.img["lang"] == generated_language
    assert soup.select_one(".diagram-nodes td")["lang"] == language


def test_composition_namespaces_authored_descriptions_and_links_together():
    soup = BeautifulSoup(_diagram() + _crop(), "html.parser")
    original = str(soup)
    mapping = namespace_semantic_fragment(soup, namespace="doc-2")
    assert diagnose_mermaid_diagrams(soup) == ()
    assert diagnose_image_descriptions(soup) == ()
    assert (
        soup.select_one(".image-description-link a")["href"]
        == "#" + mapping["crop-description"]
    )
    assert soup.find(id=mapping["diagram1-description"]) is not None
    assert "doc-2" not in original


def test_structural_alternative_preserves_each_labels_language(controlled_renderer):
    source = BeautifulSoup(_diagram(), "html.parser")
    french = source.find(id="diagram1--review")
    french["lang"] = "fr"
    french.string = "Réviser"
    german = source.find(id="diagram1--yes")
    german["lang"] = "de"
    german.string = "Ja"
    projected = BeautifulSoup(
        project_diagrams_html(str(source), runner=controlled_renderer), "html.parser"
    )
    rows = projected.select(".diagram-nodes tbody tr")
    assert rows[2].td["lang"] == "fr" and rows[2].td.get_text() == "Réviser"
    assert rows[3].td["lang"] == "en"
    label = projected.select(".diagram-connections tbody tr")[1].find_all("td")[-1]
    assert label["lang"] == "de" and label.get_text() == "Ja"


def test_legacy_image_role_cannot_flatten_the_generated_structured_alternative(
    controlled_renderer,
):
    source = _diagram(descriptions=False).replace("<figure ", '<figure role="img" ')
    projected = BeautifulSoup(
        project_diagrams_html(source, runner=controlled_renderer), "html.parser"
    )
    assert not projected.figure.has_attr("role")
    assert projected.select_one('.diagram-nodes th[scope="row"]') is not None


def test_svg_accessibility_references_cannot_be_forged_or_dangling():
    malformed = SVG.replace(
        b"<title>G</title>", b'<title id="diagram-title">G</title>'
    ).replace(
        b"<svg ",
        b'<svg role="img" aria-labelledby="diagram-title diagram-description" ',
    )
    with pytest.raises(ValueError, match="diagram-svg-invalid"):
        validate_svg(malformed)
    with pytest.raises(ValueError, match="diagram-svg-invalid"):
        validate_svg(SVG.replace(b"<svg ", b'<svg aria-labelledby="outside" '))


def test_label_and_short_alt_references_keep_their_accessible_text(controlled_renderer):
    source = BeautifulSoup(_diagram(), "html.parser")
    source.figure["aria-labelledby"] = "diagram1-alt"
    source.figure["aria-describedby"] = "diagram1--review diagram1--yes"
    source.code.string = source.code.get_text().replace("{{other}}", "{{review}}")
    source.find(id="diagram1--other").decompose()
    projected = BeautifulSoup(
        project_diagrams_html(str(source), runner=controlled_renderer), "html.parser"
    )
    assert (
        projected.find(id="diagram1-alt").get("alt")
        == "Readiness decision. Full description follows."
    )
    assert projected.find(id="diagram1--review").get_text() == "Review"
    assert projected.find(id="diagram1--yes").get_text() == "Yes"
    assert len(projected.find_all(id="diagram1--review")) == 1
    assert projected.figure["aria-labelledby"] == "diagram1-alt"
    assert projected.figure["aria-describedby"] == "diagram1--review diagram1--yes"


@pytest.mark.parametrize(
    "language,generated", [("pl", "pl"), ("ar", "ar"), ("ja", "en")]
)
def test_fragment_projection_uses_declared_language_as_fallback(
    controlled_renderer, language, generated
):
    source = '<figure class="mermaid-diagram-v1" id="d"><pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre><div class="mermaid-labels"><p id="d--a">A</p></div></figure>'
    projected = BeautifulSoup(
        project_diagrams_html(
            source, default_language=language, runner=controlled_renderer
        ),
        "html.parser",
    )
    assert projected.select_one(".diagram-description-v1")["lang"] == language
    assert projected.select_one(".diagram-nodes td")["lang"] == language
    assert projected.select_one(".diagram-nodes caption")["lang"] == generated
    assert projected.img["lang"] == generated
    with pytest.raises(ValueError, match="diagram-language-invalid"):
        project_diagrams_html(
            source, default_language="invalid language", runner=controlled_renderer
        )
