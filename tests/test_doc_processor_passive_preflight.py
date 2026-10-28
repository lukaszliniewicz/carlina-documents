from __future__ import annotations

import json

import pytest

from iarf_assistant.addons.doc_processor.page_batch_response import (
    page_batch_response_schema,
)
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import concatenate_html_pages
from iarf_assistant.addons.doc_processor.parsers.semantic_validation import (
    extraction_note_input_contract,
)
from iarf_assistant.addons.doc_processor.passive_preflight import (
    PASSIVE_PAGE_BATCH_V2_SCHEMA,
    PASSIVE_PAGE_BATCH_V3_SCHEMA,
    preflight_completion,
    validate_page_declaration,
)
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
)


def _request(page_numbers=(7, 8)):
    return {
        "source": "doc_processor_pdf_batch",
        "messages": [{"role": "user", "content": "untrusted page instructions"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": PASSIVE_PAGE_BATCH_V2_SCHEMA,
                "strict": True,
                "schema": page_batch_response_schema(
                    page_numbers, allow_blank=True
                ),
            },
        },
    }


def _request_v3(page_numbers=(7, 8)):
    return {
        "source": "doc_processor_pdf_batch",
        "messages": [{"role": "user", "content": "untrusted page instructions"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": PASSIVE_PAGE_BATCH_V3_SCHEMA,
                "strict": True,
                "schema": page_batch_response_schema(
                    page_numbers, allow_blank=True, allow_issues=True
                ),
            },
        },
    }


def _strict_request(page_numbers=(7, 8)):
    request = _request(page_numbers)
    request["dispatch"] = {
        "extraction_contract": extraction_note_input_contract(),
    }
    return request


def _response(*pages):
    return {"content": json.dumps({"pages": list(pages)})}


def _page(number, html, blank=False):
    return {"page_number": number, "html": html, "blank": blank}


def _issue_report():
    return {
        "key": "name-1",
        "kind": "name_check",
        "excerpt": "Łódź",
        "reason": "The scan makes the final character uncertain.",
        "suggested_readings": ["Łódź"],
        "references": [],
    }


def _page_v3(number, html, issues=None, blank=False):
    return {
        "page_number": number,
        "html": html,
        "blank": blank,
        "issues": [] if issues is None else issues,
    }


def _roots(body="", notes=""):
    return f"<main_content>{body}</main_content><footnotes>{notes}</footnotes>"


def test_preflight_accepts_exact_ordered_ids_and_safe_links():
    result = preflight_completion(
        _request(),
        _response(
            _page(7, _roots('<p>One <a href="https://example.org">link</a>.</p>')),
            _page(8, _roots("<p>Two.</p>")),
        ),
    )

    assert result == {
        "contract": "document-page-preflight-v1",
        "scope": "leased_packet_only",
        "validated_blocks": 2,
        "validation_errors": [],
    }


def test_preflight_reports_packet_list_style_conflict_without_page_content():
    secret = "private-list-source"
    for request in (_request((7, 8)), _request_v3((7, 8))):
        page = (
            _page_v3
            if request["response_format"]["json_schema"]["name"]
            == PASSIVE_PAGE_BATCH_V3_SCHEMA
            else _page
        )
        result = preflight_completion(
            request,
            _response(
                page(
                    7,
                    _roots(
                        "<ol type='a' class='continues'><li>First source item</li></ol>"
                    ),
                ),
                page(
                    8,
                    _roots(f"<ol type='A' class='continued'><li>{secret}</li></ol>"),
                ),
            ),
        )

        assert result["validation_errors"] == [
            {
                "path": "/content/pages/1/html",
                "code": "list-continuation-style-conflict",
            }
        ]
        assert secret not in repr(result)


def test_preflight_reports_packet_list_ordinal_conflict_without_page_content():
    secret = "private-ordinal-source"
    result = preflight_completion(
        _request((7, 8)),
        _response(
            _page(
                7,
                _roots("<ol start='3'><li>Three</li><li>Four</li></ol>"),
            ),
            _page(
                8,
                _roots(
                    "<ol class='continued'><li value='5' "
                    f"class='continued-from-previous'>{secret}</li></ol>"
                ),
            ),
        ),
    )

    assert result["validation_errors"] == [
        {
            "path": "/content/pages/1/html",
            "code": "list-continuation-ordinal-conflict",
        }
    ]
    assert secret not in repr(result)


def test_preflight_checks_list_joins_in_footnotes_as_well_as_main_content():
    secret = "private-footnote-source"
    result = preflight_completion(
        _request((7, 8)),
        _response(
            _page(
                7,
                "<main_content><p>Body one.</p></main_content>"
                "<footnotes><div class='footnote continues' id='f1'>"
                "<ol type='a' class='continues'><li>First note.</li></ol>"
                "</div></footnotes>",
            ),
            _page(
                8,
                "<main_content><p>Body two.</p></main_content>"
                "<footnotes><div class='footnote continued' id='f2'>"
                "<ol type='A' class='continued'><li>"
                f"{secret}</li></ol></div></footnotes>",
            ),
        ),
    )

    assert result["validation_errors"] == [
        {
            "path": "/content/pages/1/html",
            "code": "list-continuation-style-conflict",
        }
    ]
    assert secret not in repr(result)


def test_preflight_joins_only_an_exact_packet_local_predecessor():
    secret = "private-predecessor-source"
    request = _request((8,))
    request["dispatch"] = {
        "boundary_input": {
            "last_page": 7,
            "last_page_html": _roots(
                "<ol type='a' class='continues'><li>Previous item</li></ol>"
            ),
        }
    }
    result = preflight_completion(
        request,
        _response(
            _page(
                8,
                _roots(f"<ol type='A' class='continued'><li>{secret}</li></ol>"),
            )
        ),
    )

    assert result["validation_errors"] == [
        {
            "path": "/content/pages/0/html",
            "code": "list-continuation-style-conflict",
        }
    ]
    assert secret not in repr(result)


@pytest.mark.parametrize(
    "boundary_input",
    [
        {
            "last_page": 6,
            "last_page_html": _roots("<ol type='a'><li>Previous</li></ol>"),
        },
        {
            "last_page": True,
            "last_page_html": _roots("<ol type='a'><li>Previous</li></ol>"),
        },
        {"last_page": 7, "last_page_html": 123},
    ],
)
def test_preflight_ignores_nonadjacent_or_malformed_predecessor_evidence(
    boundary_input,
):
    request = _request((8,))
    request["dispatch"] = {"boundary_input": boundary_input}

    result = preflight_completion(
        request,
        _response(
            _page(
                8,
                _roots("<ol type='A' class='continued'><li>Current item</li></ol>"),
            )
        ),
    )

    assert result["validation_errors"] == []


def test_preflight_does_not_join_packet_pages_across_a_physical_gap():
    result = preflight_completion(
        _request((7, 9)),
        _response(
            _page(
                7,
                _roots("<ol type='a' class='continues'><li>Previous item</li></ol>"),
            ),
            _page(
                9,
                _roots("<ol type='A' class='continued'><li>Current item</li></ol>"),
            ),
        ),
    )

    assert result["validation_errors"] == []


def test_preflight_derives_page_order_from_leased_schema_and_hides_content():
    secret = "do-not-echo-page-content"
    result = preflight_completion(
        _request((7, 8)),
        _response(_page(8, _roots(f"<p>{secret}</p>")), _page(7, _roots("<p>Two</p>"))),
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/order", "code": "page-order"}
    ]
    assert secret not in repr(result)


def test_preflight_rejects_missing_blank_flag_for_empty_roots():
    result = preflight_completion(
        _request((7,)),
        {"content": json.dumps({"pages": [{"page_number": 7, "html": _roots()}]})},
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/blank", "code": "page-blank-required"}
    ]


def test_preflight_rejects_non_boolean_blank_without_echoing_value():
    result = preflight_completion(
        _request((7,)), _response(_page(7, _roots("<p>private text</p>"), "yes"))
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/blank", "code": "page-blank-type"}
    ]
    assert "private text" not in repr(result)
    assert "yes" not in repr(result)


@pytest.mark.parametrize(
    "body",
    [
        "<p>visible text</p>",
        '<img src="data:image/png;base64,AA==">',
        "<figure></figure>",
    ],
)
def test_preflight_rejects_blank_true_with_any_content(body):
    result = preflight_completion(
        _request((7,)), _response(_page(7, _roots(body), True))
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/blank", "code": "blank-requires-empty-html"}
    ]


def test_preflight_rejects_empty_roots_with_blank_false_regardless_of_ocr():
    result = preflight_completion(
        _request((7,)), _response(_page(7, _roots(" \n<!-- comment -->\t"), False))
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/blank", "code": "blank-declaration-required"}
    ]


@pytest.mark.parametrize("body", ["<p></p>", "<div> \t </div>", "<br>"])
def test_preflight_requires_blank_declaration_for_empty_wrappers_and_breaks(body):
    result = preflight_completion(
        _request((7,)), _response(_page(7, _roots(body), False))
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/blank", "code": "blank-declaration-required"}
    ]


@pytest.mark.parametrize(
    "body",
    [
        '<img src="data:image/png;base64,AA==">',
        "<figure></figure>",
        "<p>visible text</p>",
    ],
)
def test_preflight_accepts_nontext_image_carriers_or_visible_text_as_not_blank(body):
    result = preflight_completion(
        _request((7,)), _response(_page(7, _roots(body), False))
    )

    assert result["validated_blocks"] == 1
    assert result["validation_errors"] == []


def test_page_declaration_allows_toc_omission_marker_with_blank_false():
    toc_marker = _roots().replace(
        "<main_content></main_content>",
        '<main_content><nav class="source-toc"></nav></main_content>',
    )

    result = validate_page_declaration(toc_marker, declared_blank=False)

    assert result.valid
    assert result.source_toc_omitted


@pytest.mark.parametrize(
    ("html", "code"),
    [
        ("<main_content>loose text</main_content><footnotes></footnotes>", "unstructured-text"),
        (
            '<main_content><p><a class="footnote-ref" href="#missing">1</a></p></main_content><footnotes></footnotes>',
            "footnote-reference",
        ),
    ],
)
def test_preflight_rejects_unstructured_or_invalid_link_markup(html, code):
    result = preflight_completion(_request((7,)), _response(_page(7, html)))

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/html", "code": code}
    ]


@pytest.mark.parametrize(
    "marker",
    [
        '<a class="footnote-ref" href="#f7">7</a>',
        '<sup><a class="footnote-ref" href="#f7">7</a></sup>',
    ],
)
def test_strict_preflight_rejects_output_only_footnote_references_early(marker):
    private_note = "private-note-text"
    result = preflight_completion(
        _strict_request((7,)),
        _response(
            _page(
                7,
                _roots(
                    f"<p>{marker}</p>",
                    f"<div class='footnote' id='f7'>{private_note}</div>",
                ),
            )
        ),
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/html", "code": "footnote-input-shape"}
    ]
    assert private_note not in repr(result)


def test_strict_preflight_rejects_direct_link_to_known_note_body():
    result = preflight_completion(
        _strict_request((7,)),
        _response(
            _page(
                7,
                _roots(
                    '<p><a href="#f7">7</a></p>',
                    "<div class='footnote' id='f7'>Private note.</div>",
                ),
            )
        ),
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/html", "code": "footnote-input-shape"}
    ]


def test_strict_preflight_uses_only_trusted_request_contract():
    request = _request((7,))
    result = preflight_completion(
        request,
        {
            **_response(
                _page(
                    7,
                    _roots(
                        '<p><a class="footnote-ref" href="#f7">7</a></p>',
                        "<div class='footnote' id='f7'>Note.</div>",
                    ),
                )
            ),
            "dispatch": {"extraction_contract": extraction_note_input_contract()},
        },
    )

    assert result["validated_blocks"] == 1
    assert result["validation_errors"] == []


def test_strict_preflight_linked_marker_survives_page_assembly_and_canonicalization(
):
    request = _strict_request((7, 8))
    response = _response(
        _page(
            7,
            _roots(
                '<p>Linked <span class="footnote_marker" '
                'data-footnote-scope="chapter-a">'
                '<a href="#later-note-body">8</a></span>; x<sup>2</sup> and '
                'scoped <span class="footnote_marker" '
                'data-footnote-scope="chapter-a">8</span>; '
                '<a href="https://example.org">ordinary</a>.</p>'
            ),
        ),
        _page(
            8,
            "<main_content><p>Next page.</p></main_content>"
            "<footnotes><div class='footnote' id='later-note-body' "
            "data-footnote-scope='chapter-a' data-footnote-label='8'>"
            "Later note body.</div></footnotes>",
        ),
    )

    preflight = preflight_completion(request, response)
    assert preflight["validation_errors"] == []
    pages = json.loads(response["content"])["pages"]
    assembled = concatenate_html_pages(
        [page["html"] for page in pages], page_numbers=[7, 8]
    )
    canonical = canonicalize_document_html(assembled, user_id=1)

    assert not canonical.errors
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(canonical.html, "html.parser")
    references = soup.find_all("a", class_="footnote-ref")
    assert len(references) == 2
    assert all(
        reference.get("href", "").startswith("#") for reference in references
    )
    assert all(
        soup.find(id=reference["href"][1:]) is not None for reference in references
    )
    math_superscript = next(
        sup for sup in soup.find_all("sup") if sup.find("a") is None
    )
    assert math_superscript.get_text() == "2"
    assert soup.find("a", href="https://example.org") is not None


def test_legacy_preflight_keeps_accepting_canonical_footnote_markup():
    result = preflight_completion(
        _request((7,)),
        _response(
            _page(
                7,
                _roots(
                    '<p><sup><a class="footnote-ref" href="#f7">7</a></sup></p>',
                    "<div class='footnote' id='f7'>Note.</div>",
                ),
            )
        ),
    )

    assert result["validated_blocks"] == 1
    assert result["validation_errors"] == []


@pytest.mark.parametrize(
    "packet",
    [
        {"source": "other-addon", "response_format": {"type": "json_object"}},
        {
            "source": "doc_processor_pdf_batch",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "document_page_batch",
                    "schema": page_batch_response_schema([7]),
                },
            },
        },
    ],
)
def test_preflight_bypasses_unrelated_and_legacy_packets(packet):
    assert preflight_completion(packet, _response(_page(7, _roots("<p>x</p>")))) is None


def test_preflight_rejects_v2_packet_with_inconsistent_leased_schema():
    request = _request((7,))
    request["response_format"]["json_schema"]["schema"]["properties"]["pages"][
        "minItems"
    ] = 2

    result = preflight_completion(request, _response(_page(7, _roots("<p>x</p>"))))

    assert result["validation_errors"] == [
        {
            "path": "/request/response_format",
            "code": "page-batch-request-schema",
        }
    ]


def test_preflight_accepts_v3_issue_array_with_native_unicode():
    result = preflight_completion(
        _request_v3((7,)),
        _response(
            _page_v3(
                7,
                _roots("<p>Łódź</p>"),
                [_issue_report()],
            )
        ),
    )

    assert result == {
        "contract": "document-page-preflight-v1",
        "scope": "leased_packet_only",
        "validated_blocks": 1,
        "validation_errors": [],
    }


def test_preflight_rejects_invalid_v3_issue_reports_without_echoing_content():
    secret = "private_issue_evidence"
    issue = {**_issue_report(), "key": "bad key", "excerpt": secret}
    result = preflight_completion(
        _request_v3((7,)),
        _response(_page_v3(7, _roots("<p>Visible</p>"), [issue])),
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/issues", "code": "page-issues-invalid"}
    ]
    assert secret not in repr(result)


def test_preflight_rejects_nonempty_issue_array_for_blank_v3_page():
    result = preflight_completion(
        _request_v3((7,)),
        _response(
            _page_v3(
                7,
                _roots(),
                [_issue_report()],
                blank=True,
            )
        ),
    )

    assert result["validation_errors"] == [
        {"path": "/content/pages/0/issues", "code": "page-blank-issues"}
    ]


def test_preflight_rejects_inconsistent_v3_issue_schema():
    request = _request_v3((7,))
    request["response_format"]["json_schema"]["schema"]["properties"]["pages"][
        "items"
    ]["required"].remove("issues")

    result = preflight_completion(
        request,
        _response(_page_v3(7, _roots("<p>Visible</p>"))),
    )

    assert result["validation_errors"] == [
        {
            "path": "/request/response_format",
            "code": "page-batch-request-schema",
        }
    ]
