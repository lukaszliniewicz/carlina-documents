"""Ruby readings survive extraction, stay out of running text and export sensibly."""

from __future__ import annotations

import hashlib
import json

from bs4 import BeautifulSoup

from carlina.core.text_formats import (
    flatten_ruby_for_text_formats,
    html_to_markdown_on_the_fly,
    ruby_fallback_text,
    text_without_readings,
)
from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.page_batch_response import (
    page_batch_response_schema,
)
from iarf_assistant.addons.doc_processor.parsers.semantic_validation import (
    MARKUP_VOCABULARY_POLICY_SETTING,
    MARKUP_VOCABULARY_POLICY_VERSION,
    TABLE_CONTINUATION_POLICY_SETTING,
    TABLE_CONTINUATION_POLICY_VERSION,
    validate_markup_vocabulary,
)
from iarf_assistant.addons.doc_processor.passive_preflight import (
    PASSIVE_PAGE_BATCH_V2_SCHEMA,
    preflight_completion,
)
from iarf_assistant.addons.doc_processor.prompting import (
    extraction_contract_instruction,
)
from iarf_assistant.addons.doc_processor.ruby import add_ruby_fallback_brackets
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
)
from iarf_assistant.addons.translator import compact_markup, target_rendering
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks

OPEN, CLOSE = "\uff08", "\uff09"  # fullwidth brackets
_PARAGRAPH = "<p>彼は<ruby>漢<rt>かん</rt>字<rt>じ</rt></ruby>を読む。</p>"


def test_canonical_ruby_keeps_readings_and_drops_fallback_brackets() -> None:
    result = canonicalize_document_html(
        "<main><h2><ruby>縲絏<rt>るいせつ</rt></ruby>の章</h2>"
        "<p><ruby>漢<rp>(</rp><rt>かん</rt><rp>)</rp></ruby></p></main>",
        user_id=1,
    )
    soup = BeautifulSoup(result.html, "html.parser")
    assert soup.find("rp") is None
    assert [rt.get_text() for rt in soup.find_all("rt")] == ["るいせつ", "かん"]
    assert soup.h2["id"] == "縲絏-の章"  # readings never reach the slug
    assert not result.diagnostics


def test_malformed_ruby_is_unwrapped_with_a_warning() -> None:
    result = canonicalize_document_html(
        "<main><p>a<rt>stray</rt> <ruby>base only</ruby></p></main>", user_id=1
    )
    soup = BeautifulSoup(result.html, "html.parser")
    assert soup.find(["rt", "ruby"]) is None
    assert soup.p.get_text() == "astray base only"
    codes = {item["code"] for item in result.diagnostics}
    assert codes == {"ruby_reading_outside_ruby", "ruby_without_reading_unwrapped"}


def test_running_text_and_review_search_ignore_readings() -> None:
    soup = BeautifulSoup(_PARAGRAPH, "html.parser")
    assert text_without_readings(soup.p) == "彼は漢字を読む。"
    review = DocumentReview(f"<main>{_PARAGRAPH}</main>")
    assert review.search("漢字を読む")["total_matches"] == 1
    assert review.search("かん")["total_matches"] == 0
    assert "text" not in review.fetch(include_text=False)["items"][0]


def test_text_formats_render_readings_in_brackets() -> None:
    soup = BeautifulSoup(
        _PARAGRAPH + "<p><ruby>Tokyo<rt>Edo</rt></ruby></p>", "html.parser"
    )
    rubies = soup.find_all("ruby")
    assert [ruby_fallback_text(ruby) for ruby in rubies] == [
        f"漢字{OPEN}かんじ{CLOSE}",
        "Tokyo (Edo)",
    ]
    assert flatten_ruby_for_text_formats(soup) == 2
    assert soup.find("ruby") is None
    assert (
        html_to_markdown_on_the_fly(_PARAGRAPH).strip()
        == f"彼は漢字{OPEN}かんじ{CLOSE}を読む。"
    )


def test_epub_adds_bracket_fallbacks_once() -> None:
    soup = BeautifulSoup(_PARAGRAPH, "html.parser")
    add_ruby_fallback_brackets(soup)
    add_ruby_fallback_brackets(soup)
    assert str(soup.ruby) == (
        f"<ruby>漢<rp>{OPEN}</rp><rt>かん</rt><rp>{CLOSE}</rp>"
        f"字<rp>{OPEN}</rp><rt>じ</rt><rp>{CLOSE}</rp></ruby>"
    )


def test_translator_translates_base_text_and_leaves_readings_out() -> None:
    source = (
        "<html><body><h2>第一章 <ruby>縲絏<rt>るいせつ</rt></ruby></h2>"
        f"{_PARAGRAPH}</body></html>"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
    )
    plan = plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="doc-provider",
            provider_capability_id="semantic-document",
            owner_id="owner-1",
            resource_id="doc-1",
        ),
        source_language="ja",
    )
    translations = {
        "第一章 縲絏": "Chapter 1: Bonds",
        "彼は漢字を読む。": "He reads kanji.",
    }
    candidates = {}
    for number, block in enumerate(plan.blocks, 1):
        wire = compact_markup.compact_source_block(block, number=number)
        assert wire["content"] in translations
        candidates[block.id] = compact_markup.normalize_compact_result(
            plan=plan,
            plan_id="plan-1",
            block=block,
            expected_number=wire["n"],
            value={
                "n": wire["n"],
                "content": translations[wire["content"]],
                "fields": wire["fields"],
            },
        )
    target = target_rendering.render_target_document(
        plan, "plan-1", candidates, target_language="en"
    )
    assert target_rendering.validate_target_semantic_document(target).valid
    soup = BeautifulSoup(target, "html.parser")
    assert soup.find(["ruby", "rt"]) is None
    assert soup.h2.get_text() == "Chapter 1: Bonds"


def _vocabulary_request(*, strict: bool) -> dict[str, object]:
    request: dict[str, object] = {
        "source": "doc_processor_pdf_batch",
        "messages": [{"role": "user", "content": "page"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": PASSIVE_PAGE_BATCH_V2_SCHEMA,
                "strict": True,
                "schema": page_batch_response_schema((3,), allow_blank=True),
            },
        },
    }
    if strict:
        request["dispatch"] = {"markup_vocabulary": MARKUP_VOCABULARY_POLICY_VERSION}
    return request


def _response(html: str) -> dict[str, str]:
    page = {"page_number": 3, "html": html, "blank": False}
    return {"content": json.dumps({"pages": [page]})}


def test_vocabulary_check_names_the_element_while_the_lease_is_open() -> None:
    html = "<main_content><p><ruby>漢<rb>字</rb><rt>じ</rt></ruby></p></main_content><footnotes></footnotes>"
    strict = preflight_completion(_vocabulary_request(strict=True), _response(html))
    assert strict is not None
    assert strict["validation_errors"] == [
        {
            "path": "/content/pages/0/html",
            "code": "unsupported-element",
            "element": "rb",
        }
    ]
    retained = preflight_completion(_vocabulary_request(strict=False), _response(html))
    assert retained is not None
    assert retained["validation_errors"] == []


def test_vocabulary_accepts_ruby_and_presentational_wrappers() -> None:
    assert validate_markup_vocabulary(
        "<main_content><p><center><ruby>漢<rt>かん</rt></ruby></center></p></main_content>"
    ).valid


def test_new_contract_instructions_follow_frozen_markers() -> None:
    retained = {"mcp_execution_mode": "passive"}
    frozen = {
        "mcp_execution_mode": "passive",
        MARKUP_VOCABULARY_POLICY_SETTING: MARKUP_VOCABULARY_POLICY_VERSION,
        TABLE_CONTINUATION_POLICY_SETTING: TABLE_CONTINUATION_POLICY_VERSION,
    }
    assert extraction_contract_instruction(retained) == ""
    instruction = extraction_contract_instruction(frozen)
    assert "ruby-vocabulary-v1" in instruction
    assert "column-aligned-v1" in instruction
    assert (
        extraction_contract_instruction({"mcp_execution_mode": "managed"})
        == instruction
    )
