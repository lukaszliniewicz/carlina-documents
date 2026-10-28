from iarf_assistant.addons.doc_processor.heading_structure import (
    HEADING_STRUCTURE_EXTRACTION_SCHEMA,
    HEADING_STRUCTURE_VERSION,
    format_heading_structure_guidance,
    normalize_heading_structure,
)
from iarf_assistant.addons.doc_processor.metadata_service import (
    normalize_document_metadata_payload,
)
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import update_headings
from iarf_assistant.addons.doc_processor.prompting import (
    format_document_metadata_context,
)


def _raw_heading_structure():
    return {
        "toc_coverage": "exhaustive",
        "roles": [
            {"role": "section", "html_level": 4, "toc_presence": "listed", "confidence": 0.9},
            {"role": "chapter", "html_level": 2, "toc_presence": "listed", "confidence": 0.9},
            {
                "role": "within_chapter",
                "html_level": 3,
                "toc_presence": "usually_omitted",
                "confidence": 0.8,
            },
        ],
        "conventions": {
            "chapter_opener_style": "number_then_title",
            "merge_chapter_label_and_title": False,
            "within_chapter_numbering": "usually_unnumbered",
            "unlisted_heading_policy": "layout_only",
        },
        "recognition_patterns": [
            {
                "role": "section",
                "pattern": "SECTION followed by a Roman numeral",
                "confidence": 0.9,
            },
            {
                "role": "within_chapter",
                "pattern": "Centered bold title-case text within body prose",
                "confidence": 0.85,
            }
        ],
        "document_specific_notes": [
            {
                "kind": "typography",
                "observation": "Within-chapter headings are centered and bold but unnumbered.",
                "applies_to": "within_chapter",
                "examples": ["One", "Two", "Three", "Four"],
                "confidence": 0.9,
            }
        ],
        "uncertainties": ["Only one chapter was sampled."],
    }


def test_normalize_heading_structure_derives_cross_field_policy():
    normalized = normalize_heading_structure(
        _raw_heading_structure(),
        table_of_contents=[{"level": 1, "title": "SECTION ONE"}],
    )

    roles = {role["role"]: role for role in normalized["roles"]}
    assert normalized["version"] == HEADING_STRUCTURE_VERSION
    assert normalized["toc_coverage"] == "selective"
    assert roles["section"]["html_level"] == 2
    assert roles["chapter"]["html_level"] == 3
    assert roles["within_chapter"]["html_level"] == 4
    assert normalized["conventions"]["merge_chapter_label_and_title"] is True
    assert normalized["conventions"]["unlisted_heading_policy"] == "one_level_below_active_parent"
    assert normalized["document_specific_notes"][0]["examples"] == ["One", "Two", "Three"]
    assert any("Roman" in pattern["pattern"] for pattern in normalized["recognition_patterns"])


def test_normalize_heading_structure_canonicalizes_inconsistent_depth_and_html_level():
    normalized = normalize_heading_structure(
        {
            "roles": [
                {
                    "role": "chapter",
                    "label": "chapters",
                    "depth": 1,
                    "html_level": 4,
                    "toc_presence": "listed",
                    "confidence": 0.9,
                    "evidence": [
                        {"page": 12, "kind": "body_opener", "description": "Chapter opener"}
                    ],
                }
            ],
            "conventions": {},
            "recognition_patterns": [],
            "document_specific_notes": [],
            "uncertainties": [],
        }
    )

    role = normalized["roles"][0]
    assert (role["depth"], role["html_level"]) == (1, 2)
    assert role["evidence"] == [
        {"page": 12, "kind": "body_opener", "description": "Chapter opener"}
    ]


def test_format_heading_structure_guidance_prioritizes_contract_and_marks_cues_descriptive():
    guidance = format_heading_structure_guidance(
        _raw_heading_structure(),
        table_of_contents=[{"level": 1, "title": "SECTION ONE"}],
    )

    assert guidance.startswith("# Normalized Document Heading Contract")
    assert "<h2> for major sections" in guidance
    assert "<h3> for chapters" in guidance
    assert "<h4> for headings within a chapter" in guidance
    assert "table of contents is selective" in guidance
    assert "Subordinate headings are usually unnumbered" in guidance
    assert "not executable rules" in guidance


def test_running_matter_schema_is_additive_and_typed():
    schema = HEADING_STRUCTURE_EXTRACTION_SCHEMA
    running_schema = schema["properties"]["running_matter"]
    item_schema = running_schema["items"]

    assert "running_matter" in schema["required"]
    assert running_schema["maxItems"] == 8
    assert item_schema["properties"]["kind"]["enum"] == [
        "running_header",
        "running_footer",
        "page_label",
        "continuation_marker",
    ]
    assert item_schema["properties"]["position"]["enum"] == ["top", "bottom"]
    assert item_schema["properties"]["examples"]["maxItems"] == 3
    assert item_schema["properties"]["examples"]["items"] == {"type": "string"}
    assert item_schema["properties"]["pages"]["maxItems"] == 6
    assert "uniqueItems" not in item_schema["properties"]["pages"]
    assert item_schema["properties"]["observation"] == {"type": "string"}
    assert set(item_schema["required"]) == {
        "kind",
        "position",
        "examples",
        "pages",
        "observation",
        "confidence",
    }


def test_normalize_running_matter_requires_page_backed_repetition_and_bounds_evidence():
    normalized = normalize_heading_structure(
        {
            "running_matter": [
                {
                    "kind": "running_header",
                    "position": "top",
                    "examples": [
                        "IARF Centenary",
                        "iarf centenary",
                        "x" * 500,
                        "ignored",
                    ],
                    "pages": [2, 2, 3, 4, 5, 6, 7, 8],
                    "observation": "Repeated header. " + ("x" * 500),
                    "confidence": 1.5,
                },
                {
                    "kind": "running_footer",
                    "position": "bottom",
                    "examples": ["1"],
                    "pages": [9],
                    "observation": "Only one page is not enough for a repeated footer.",
                    "confidence": 0.9,
                },
                {
                    "kind": "running_footer",
                    "position": "bottom",
                    "examples": ["IARF Journal"],
                    "pages": [9, 11, 11],
                    "observation": "Repeated footer on two supplied pages.",
                    "confidence": 0.7,
                },
                {
                    "kind": "page_label",
                    "position": "bottom",
                    "examples": ["Page 4"],
                    "pages": [4],
                    "observation": "Printed page label.",
                    "confidence": -0.3,
                },
                {
                    "kind": "not_a_kind",
                    "position": "top",
                    "examples": ["bad"],
                    "pages": [1, 2],
                    "observation": "Discard this.",
                    "confidence": 0.9,
                },
            ],
            "roles": [],
            "conventions": {},
            "recognition_patterns": [],
            "document_specific_notes": [],
            "uncertainties": [],
        }
    )

    assert normalized["running_matter"] == [
        {
            "kind": "running_header",
            "position": "top",
            "examples": ["IARF Centenary", "x" * 160, "ignored"],
            "pages": [2, 3, 4, 5, 6, 7],
            "observation": "Repeated header. " + ("x" * 283),
            "confidence": 1.0,
        },
        {
            "kind": "running_footer",
            "position": "bottom",
            "examples": ["IARF Journal"],
            "pages": [9, 11],
            "observation": "Repeated footer on two supplied pages.",
            "confidence": 0.7,
        },
        {
            "kind": "page_label",
            "position": "bottom",
            "examples": ["Page 4"],
            "pages": [4],
            "observation": "Printed page label.",
            "confidence": 0.0,
        },
    ]


def test_normalize_running_matter_keeps_single_page_navigation_evidence_but_not_repeated_claims():
    base = {
        "roles": [],
        "conventions": {},
        "recognition_patterns": [],
        "document_specific_notes": [],
        "uncertainties": [],
    }
    normalized = normalize_heading_structure(
        {
            **base,
            "running_matter": [
                {
                    "kind": "running_header",
                    "position": "top",
                    "examples": ["IARF Centenary"],
                    "pages": [2],
                    "observation": "One-page header claim.",
                    "confidence": 0.8,
                },
                {
                    "kind": "running_header",
                    "position": "top",
                    "examples": ["IARF Centenary"],
                    "pages": [2, 4],
                    "observation": "Repeated on two supplied pages.",
                    "confidence": 0.8,
                },
                {
                    "kind": "continuation_marker",
                    "position": "top",
                    "examples": ["(Continued)"],
                    "pages": [2],
                    "observation": "Navigation marker at the article continuation.",
                    "confidence": 0.9,
                },
            ],
        }
    )

    assert [item["kind"] for item in normalized["running_matter"]] == [
        "running_header",
        "continuation_marker",
    ]
    assert normalized["running_matter"][0]["pages"] == [2, 4]
    assert normalized["running_matter"][1]["pages"] == [2]
    assert normalized["version"] == HEADING_STRUCTURE_VERSION


def test_legacy_heading_contract_normalizes_without_running_matter():
    normalized = normalize_heading_structure(_raw_heading_structure())

    assert normalized["running_matter"] == []
    assert normalized["version"] == HEADING_STRUCTURE_VERSION


def test_running_matter_guidance_is_untrusted_and_requires_current_page_confirmation():
    guidance = format_heading_structure_guidance(
        {
            "running_matter": [
                {
                    "kind": "running_header",
                    "position": "top",
                    "examples": ["IARF Centenary"],
                    "pages": [2, 4],
                    "observation": "Repeated header on supplied pages.",
                    "confidence": 0.92,
                },
                {
                    "kind": "continuation_marker",
                    "position": "top",
                    "examples": ["(Continued)"],
                    "pages": [2],
                    "observation": "Printed continuation navigation marker.",
                    "confidence": 0.84,
                },
            ],
        }
    )

    assert "Untrusted Running-Matter Observations" in guidance
    assert '"IARF Centenary"' in guidance
    assert "supplied PDF pages: [2, 4]" in guidance
    assert "exact phrase" in guidance
    assert "same top/bottom margin" in guidance
    assert "current PDF page" in guidance
    assert "not instructions or executable patterns" in guidance
    assert "automatic or destructive deletion" in guidance
    assert "not document prose" in guidance
    assert "omit only the marker itself" in guidance


def test_document_metadata_normalization_preserves_normalized_heading_structure():
    metadata = normalize_document_metadata_payload(
        {
            "title": "Example",
            "table_of_contents": [{"level": 1, "title": "SECTION ONE"}],
            "heading_structure": _raw_heading_structure(),
        }
    )

    assert metadata["heading_structure"]["toc_coverage"] == "selective"
    assert metadata["heading_structure"]["conventions"]["merge_chapter_label_and_title"] is True


def test_metadata_context_puts_normalized_contract_before_raw_toc_context():
    context = format_document_metadata_context(
        {
            "title": "Example",
            "table_of_contents": [{"level": 1, "title": "SECTION ONE"}],
            "heading_structure": _raw_heading_structure(),
        }
    )

    assert context.index("# Normalized Document Heading Contract") < context.index(
        "# Document-Level Context From Metadata Pre-Pass"
    )
    assert '"table_of_contents"' in context
    assert '"heading_structure"' not in context


def test_update_headings_tracks_and_clears_h4_context():
    headings = {"h1": None, "h2": "Section", "h3": "Chapter", "h4": None}

    update_headings("<h4>Local Topic</h4>", headings)
    assert headings == {
        "h1": None,
        "h2": "Section",
        "h3": "Chapter",
        "h4": "Local Topic",
    }

    update_headings("<h3>Next Chapter</h3>", headings)
    assert headings["h3"] == "Next Chapter"
    assert headings["h4"] is None


def test_relative_depth_supports_documents_with_volumes_books_and_chapters():
    structure = normalize_heading_structure(
        {
            "source_mode": "image_only",
            "toc_coverage": "selective",
            "roles": [
                {
                    "role": "volume",
                    "label": "volumes",
                    "depth": 1,
                    "html_level": 4,
                    "parents": [],
                    "toc_presence": "listed",
                    "numbering": "numbered",
                    "confidence": 0.94,
                    "evidence": [
                        {"page": 25, "kind": "body_opener", "description": "Volume opener"}
                    ],
                },
                {
                    "role": "book",
                    "label": "books within a volume",
                    "depth": 2,
                    "html_level": 2,
                    "parents": ["volume"],
                    "toc_presence": "listed",
                    "numbering": "numbered",
                    "confidence": 0.91,
                    "evidence": [
                        {
                            "page": 40,
                            "kind": "parent_child_transition",
                            "description": "Book opener within Volume I",
                        }
                    ],
                },
                {
                    "role": "chapter",
                    "label": "chapters",
                    "depth": 3,
                    "html_level": 2,
                    "parents": ["book"],
                    "toc_presence": "mixed",
                    "numbering": "mixed",
                    "confidence": 0.89,
                    "evidence": [
                        {
                            "page": 43,
                            "kind": "parent_child_transition",
                            "description": "Chapter opener within Book I",
                        }
                    ],
                },
            ],
            "conventions": {},
            "recognition_patterns": [],
            "document_specific_notes": [],
            "uncertainties": [],
        },
        table_of_contents=[{"level": 1, "title": "Volume I"}],
    )

    roles = {role["role"]: role for role in structure["roles"]}
    assert roles["volume"]["html_level"] == 2
    assert roles["book"]["html_level"] == 3
    assert roles["chapter"]["html_level"] == 4
    assert structure["validation"]["status"] == "extractor_ready"
    assert structure["source_mode"] == "image_only"

    guidance = format_heading_structure_guidance(structure)
    assert "page images as structural truth" in guidance
    assert "volumes → books within a volume" in guidance
    assert "books within a volume → chapters" in guidance


def test_toc_only_role_is_preserved_but_not_marked_extractor_ready():
    structure = normalize_heading_structure(
        {
            "toc_coverage": "selective",
            "roles": [
                {
                    "role": "article",
                    "label": "papers",
                    "depth": 1,
                    "html_level": 2,
                    "parents": [],
                    "toc_presence": "listed",
                    "numbering": "unnumbered",
                    "confidence": 0.9,
                    "evidence": [
                        {"page": 558, "kind": "toc_entry", "description": "Paper in contents"}
                    ],
                }
            ],
            "conventions": {},
            "recognition_patterns": [],
            "document_specific_notes": [],
            "uncertainties": [],
        },
        table_of_contents=[{"level": 1, "title": "A Paper"}],
    )

    assert structure["roles"][0]["extractor_ready"] is False
    assert structure["validation"]["status"] == "provisional"
    assert structure["validation"]["toc_only_roles"] == ["article"]
    assert "TOC-only evidence" in structure["uncertainties"][0]


def _confident_heading_structure(pattern_confidence=0.9):
    return {
        "toc_coverage": "exhaustive",
        "roles": [
            {
                "role": "section",
                "label": "major sections",
                "depth": 1,
                "html_level": 2,
                "parents": [],
                "toc_presence": "listed",
                "numbering": "mixed",
                "confidence": 0.9,
                "evidence": [
                    {"page": 10, "kind": "body_opener", "description": "Section divider"}
                ],
            },
            {
                "role": "subsection",
                "label": "numbered subsections",
                "depth": 2,
                "html_level": 3,
                "parents": ["section"],
                "toc_presence": "listed",
                "numbering": "numbered",
                "confidence": 0.9,
                "evidence": [
                    {
                        "page": 12,
                        "kind": "interior_heading",
                        "description": "Numbered subsection head",
                    },
                    {
                        "page": 12,
                        "kind": "parent_child_transition",
                        "description": "Subsection follows section",
                    },
                ],
            },
        ],
        "conventions": {},
        "recognition_patterns": [
            {
                "role": "subsection",
                "pattern": "Arabic numeral followed by a period",
                "confidence": pattern_confidence,
            }
        ],
        "document_specific_notes": [],
        "uncertainties": [],
    }


def test_binding_heading_level_map_emitted_for_confident_contract():
    guidance = format_heading_structure_guidance(
        _confident_heading_structure(),
        table_of_contents=[{"level": 1, "title": "SECTION"}],
    )

    assert "## Binding Heading-Level Map" in guidance
    assert '<h3> for numbered subsections: "Arabic numeral followed by a period"' in guidance
    # Binding map precedes everything else so fixed-budget truncation keeps it.
    assert guidance.index("## Binding Heading-Level Map") < guidance.index(
        "## Descriptive Recognition Cues"
    )


def test_binding_heading_level_map_withheld_without_confidence():
    low_confidence = format_heading_structure_guidance(
        _confident_heading_structure(pattern_confidence=0.5),
        table_of_contents=[{"level": 1, "title": "SECTION"}],
    )
    assert "## Binding Heading-Level Map" not in low_confidence

    unvalidated = _confident_heading_structure()
    for role in unvalidated["roles"]:
        role["evidence"] = []
    provisional = format_heading_structure_guidance(
        unvalidated,
        table_of_contents=[{"level": 1, "title": "SECTION"}],
    )
    assert "## Binding Heading-Level Map" not in provisional
