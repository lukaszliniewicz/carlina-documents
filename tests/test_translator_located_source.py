"""Single-copy v9 source locators and native-plan replay checks."""

from __future__ import annotations

import html
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_translator_block_translation import (
    _config,
    _profile,
    _state,
    _window,
    _workflow_config,
)

from iarf_assistant.addons.translator.alignment_contract import (
    target_alignment_projection,
)
from iarf_assistant.addons.translator.block_contract import (
    BlockContractError,
    BlockWirePart,
    SourceAnchor,
    SourceSlot,
    sha256_text,
)
from iarf_assistant.addons.translator.block_translation import build_window_material
from iarf_assistant.addons.translator.compact_markup import (
    compact_source_block,
    validate_located_source_block,
)
from iarf_assistant.addons.translator.passive_preflight import preflight_completion
from iarf_assistant.addons.translator.window_contract import (
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION as V9,
)
from iarf_assistant.addons.translator.window_contract import (
    ContextSlot,
    _strict_response_grammar,
    _target_result_schema,
    _writable_payload,
    render_provider_packet,
)
from iarf_assistant.addons.translator.window_execution import normalize_window_response
from iarf_assistant.addons.translator.window_persistence import (
    persist_window_attempt,
    rehydrate_window_attempt,
)

pytest_plugins = ("test_translator_block_translation",)


@pytest.mark.parametrize(
    "source,segment",
    [
        ("<p>One. Two.</p>", ["One.", " Two."]),
        ("<h2>Zażółć Ω.</h2>", None),
        ("<p>A &amp; B. C&#13;D.</p>", None),
        ("<p>A &NotEqualTilde; B. &amp;lsquo; literal.</p>", None),
        ("<p>Cafe\u0301. Два.</p>", None),
        ("<p>A <em>first. Second</em> end.</p>", None),
        ('<p>A <a href="#n">first. Second</a> end.</p>', None),
        ("<p>A <code>literal</code> end.</p>", None),
        ("<p>Use <code>&lt;sb/&gt; literal</code> here.</p>", None),
        ('<p><a href="#n" title="Label">Body text.</a></p>', None),
        ("<p>Begin <span hidden>protected</span> end.</p>", None),
        ("<p>A<br/>B.</p>", None),
        ("<p>A<br/>B.</p>", ["A\n", "B."]),
        ("<p>A<wbr/>B.</p>", None),
        ('<img src="x" alt="Description &amp; more.">', None),
    ],
)
def test_v9_locators_cover_native_source_once(app, source, segment):
    kwargs = {"sentence_segmenter": lambda _text: segment} if segment else {}
    _, plan, _, _ = _state(f"<html><body>{source}</body></html>", **kwargs)
    assert all(
        slot.disposition != "excluded-with-reason"
        and slot.projection_start is not None
        and slot.projection_end is not None
        for block in plan.blocks
        for slot in block.slots
    )
    if "hidden" in source:
        assert any(
            entry.disposition == "excluded-with-reason"
            for entry in plan.coverage.entries
        )
    for block in plan.blocks:
        row = compact_source_block(
            block, number=1, implicit_outer_wrappers=True, locate_sentences=True
        )
        validate_located_source_block(block, row, number=1)
        assert all(set(sentence) == {"n", "spans"} for sentence in row["sentences"])
        for anchor, sentence in zip(block.anchors, row["sentences"], strict=True):
            restored = []
            for span in sentence["spans"]:
                value = (
                    row["content"]
                    if span["field"] == "content"
                    else next(
                        field["value"]
                        for field in row["fields"]
                        if field["name"] == span["field"]
                    )
                )
                piece = value[span["start"] : span["end"]]
                restored.append(
                    "\n"
                    if piece.startswith(("<br", "<wbr"))
                    else (html.unescape(piece) if span["field"] == "content" else piece)
                )
            assert "".join(restored) == block.projection[anchor.start : anchor.end]
        assert "text" not in json.dumps(row["sentences"])


def test_v9_packet_has_one_source_copy_and_ordinal_only_grammar(app):
    _, plan, record, _ = _state("<html><body><p>Unique Ω sentence.</p></body></html>")
    config = _config(plan, record, _profile())
    config["writable_source_contract_version"] = V9
    config["annotation_profile"] = "none"
    material = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=_window(plan),
        frozen_config=config,
        workflow_config=_workflow_config(),
        execution_profile=_profile(),
        source_plan_record=record,
        include_continuity=False,
        token_counter=lambda **_: 10,
    )
    writable = next(
        slot for slot in material.packet.slots if slot.slot_type == "writable_source"
    )
    payload = json.loads(writable.content)
    assert payload["contract_version"] == V9
    assert writable.content.count("Unique Ω sentence.") == 1
    assert set(
        _target_result_schema(V9)["properties"]["results"]["items"]["properties"][
            "alignment"
        ]["properties"]
    ) == {"groups"}
    grammar = _strict_response_grammar(payload, V9)
    assert "source_sentence_count" in grammar
    assert "Unique Ω sentence." not in grammar
    assert "spans" not in grammar
    preflight = preflight_completion(
        {"source": "translator_block_translation_window", **material.packet.to_dict()},
        {
            "content": {
                "results": [
                    {
                        "n": 1,
                        "content": "Translated Ω sentence.<sb/>",
                        "fields": [],
                        "notes": [],
                        "alignment": {"groups": [{"source": [1], "target": [1]}]},
                    }
                ]
            }
        },
    )
    assert preflight is not None and preflight["validation_errors"] == []
    changed = deepcopy(payload)
    changed["blocks"][0]["sentences"][0]["spans"][0]["start"] += 1
    with pytest.raises(BlockContractError, match="compact-source-locator-replay"):
        validate_located_source_block(plan.blocks[0], changed["blocks"][0], number=1)
    for key, invalid in (
        ("field", "missing"),
        ("start", True),
        ("end", len(payload["blocks"][0]["content"]) + 1),
    ):
        malformed = deepcopy(payload)
        malformed["blocks"][0]["sentences"][0]["spans"][0][key] = invalid
        content = json.dumps(malformed, ensure_ascii=False)
        slot = replace(writable, content=content, artifact_hash=sha256_text(content))
        with pytest.raises(
            BlockContractError, match="window-writable-response-contract"
        ):
            _writable_payload(slot)
    malformed = deepcopy(payload)
    malformed["blocks"][0]["sentences"][0]["n"] = 2
    content = json.dumps(malformed, ensure_ascii=False)
    slot = replace(writable, content=content, artifact_hash=sha256_text(content))
    with pytest.raises(BlockContractError, match="window-writable-response-contract"):
        _writable_payload(slot)


@pytest.mark.parametrize(
    "source",
    [
        "<p>A <code>literal</code> end.</p>",
        '<p>Passage<a role="doc-noteref" href="#n">1</a>.</p><aside id="n" role="doc-footnote"><p>Note.</p></aside>',
        '<img src="cover.jpg" alt="Source &amp; description.">',
    ],
)
def test_v9_completion_roundtrip_preserves_visible_projection(app, source):
    _, plan, record, _ = _state(f"<html><body>{source}</body></html>")
    for index, block in enumerate(plan.blocks):
        compact = compact_source_block(
            block, number=1, implicit_outer_wrappers=True, locate_sentences=True
        )
        fields = deepcopy(compact["fields"])
        content = compact["content"]
        if not content and fields:
            fields[0]["value"] = html.escape(fields[0]["value"], quote=False) + "<sb/>"
        else:
            content += "<sb/>"
        response = {
            "results": [
                {
                    "n": 1,
                    "content": content,
                    "fields": fields,
                    "notes": [],
                    "alignment": {
                        "groups": [
                            {
                                "source": list(range(1, len(block.anchors) + 1)),
                                "target": [1],
                            }
                        ]
                    },
                }
            ]
        }
        candidates, notes, manifests = normalize_window_response(
            response,
            plan=plan,
            plan_id=record.id,
            window=_window(plan, start=index, end=index + 1),
            writable_source_contract_version=V9,
            annotation_profile="none",
            target_language="en",
        )
        assert not notes
        assert target_alignment_projection(block, candidates[0]) == block.projection
        assert (
            "".join(span.text for span in manifests[0].target_spans) == block.projection
        )
        response["results"][0]["sentences"] = compact["sentences"]
        with pytest.raises(BlockContractError, match="window-response-contract"):
            normalize_window_response(
                response,
                plan=plan,
                plan_id=record.id,
                window=_window(plan, start=index, end=index + 1),
                writable_source_contract_version=V9,
                annotation_profile="none",
                target_language="en",
            )


@pytest.mark.parametrize(
    "source",
    [
        "<p>Unique Ω sentence.</p>",
        '<p><a href="#n" title="Label">Body text.</a></p>',
    ],
)
def test_v9_replay_rejects_range_valid_but_incomplete_locator(app, source):
    _, plan, record, operation = _state(f"<html><body>{source}</body></html>")
    if "title=" in source:
        assert {block.role for block in plan.blocks} == {
            "paragraph",
            "accessibility_text",
        }
    config = _config(plan, record, _profile())
    config["writable_source_contract_version"] = V9
    config["annotation_profile"] = "none"
    material = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=_window(plan),
        frozen_config=config,
        workflow_config=_workflow_config(),
        execution_profile=_profile(),
        source_plan_record=record,
        include_continuity=False,
        token_counter=lambda **_: 10,
    )
    original = material.packet
    admission = {
        "admitted": True,
        "mode": "manual",
        "requested_output_tokens": original.requested_output_tokens,
    }
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=original,
        settings={"synthetic": True},
        model_profile={"resolved": "test/model"},
        admission=admission,
    )
    assert (
        rehydrate_window_attempt(attempt, record).payload_hash == original.payload_hash
    )
    writable = next(
        slot for slot in original.slots if slot.slot_type == "writable_source"
    )
    altered = json.loads(writable.content)
    altered["blocks"][0]["sentences"][0]["spans"][0]["start"] += 1
    content = json.dumps(
        altered, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    replacement = replace(writable, content=content, artifact_hash=sha256_text(content))
    slots = tuple(replacement if slot is writable else slot for slot in original.slots)
    forged = render_provider_packet(
        slots=slots,
        target_language="pl",
        requested_output_tokens=original.requested_output_tokens,
        native_json_schema=True,
    )
    changed = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=forged,
        settings={"synthetic": True},
        model_profile={"resolved": "test/model"},
        admission=admission,
    )
    with pytest.raises(BlockContractError, match="compact-source-locator-replay"):
        rehydrate_window_attempt(changed, record)


def test_v9_preserves_note_and_anchor_on_single_copy_completion(app):
    _, plan, record, _ = _state(
        "<html><body><p>A culture-bound term.</p></body></html>"
    )
    response = {
        "results": [
            {
                "n": 1,
                "content": "A culture-bound term.<sb/>",
                "fields": [],
                "notes": [
                    {"anchor": "culture-bound term", "body": "A concise reader note."}
                ],
                "alignment": {"groups": [{"source": [1], "target": [1]}]},
            }
        ]
    }
    candidates, notes, manifests = normalize_window_response(
        response,
        plan=plan,
        plan_id=record.id,
        window=_window(plan),
        writable_source_contract_version=V9,
        annotation_profile="restrained",
        target_language="en",
    )
    assert (
        target_alignment_projection(plan.blocks[0], candidates[0])
        == plan.blocks[0].projection
    )
    assert manifests[0].groups[0].source_ordinals == (1,)
    assert [(note.anchor_text, note.body) for note in notes] == [
        ("culture-bound term", "A concise reader note.")
    ]


def test_v9_synthetic_merge_boundary_has_a_real_space_locator(app):
    _, plan, _, _ = _state("<html><body><p>First.</p></body></html>")
    block = plan.blocks[0]
    offset = len(block.projection)
    boundary = SourceSlot(
        id="synthetic-boundary",
        locator="derived:merge-boundary:test",
        kind="boundary",
        disposition="translatable",
        policy_code="owner-merge-boundary",
        raw_start=block.source_end,
        raw_end=block.source_end,
        projection_start=offset,
        projection_end=offset + 1,
        source_value="",
        source_hash=sha256_text(""),
    )
    projection = block.projection + " "
    anchor = SourceAnchor(
        id="synthetic-anchor",
        order=1,
        start=0,
        end=len(projection),
        source_hash=sha256_text(projection),
    )
    merged = replace(
        block,
        projection=projection,
        projection_hash=sha256_text(projection),
        slots=(*block.slots, boundary),
        wire=(*block.wire, BlockWirePart(kind="slot", ref_id=boundary.id)),
        anchors=(anchor,),
    )
    row = compact_source_block(
        merged, number=1, implicit_outer_wrappers=True, locate_sentences=True
    )
    assert row["content"].endswith(" ")
    last = row["sentences"][0]["spans"][-1]
    assert row["content"][last["start"] : last["end"]] == " "


def test_v9_accepts_native_projection_order_with_field_before_content(app):
    _, plan, _, _ = _state("<html><body><p>Body text.</p></body></html>")
    block = plan.blocks[0]
    field = SourceSlot(
        id="synthetic-title",
        locator=f"{block.locator}/@title",
        kind="attribute",
        disposition="translatable",
        policy_code="accessibility-lexical",
        raw_start=block.source_start,
        raw_end=block.source_start + 5,
        projection_start=0,
        projection_end=5,
        source_value="Label",
        source_hash=sha256_text("Label"),
    )
    projection = "Label" + block.projection
    mixed = replace(
        block,
        projection=projection,
        projection_hash=sha256_text(projection),
        slots=(
            field,
            *(
                replace(
                    slot,
                    projection_start=slot.projection_start + 5,
                    projection_end=slot.projection_end + 5,
                )
                for slot in block.slots
            ),
        ),
        wire=(BlockWirePart(kind="slot", ref_id=field.id), *block.wire),
        anchors=(
            SourceAnchor(
                id="synthetic-mixed-anchor",
                order=1,
                start=0,
                end=len(projection),
                source_hash=sha256_text(projection),
            ),
        ),
    )
    row = compact_source_block(
        mixed, number=1, implicit_outer_wrappers=True, locate_sentences=True
    )
    assert [span["field"] for span in row["sentences"][0]["spans"]] == [
        "title",
        "content",
    ]
    content = json.dumps(
        {"contract_version": V9, "annotation_profile": "none", "blocks": [row]}
    )
    source = ContextSlot(
        "writable_source", content, {}, "synthetic", "v9", sha256_text(content), {}
    )
    assert _writable_payload(source)[0] == V9


def test_v9_synthetic_packet_byte_measurement(app):
    prose = " ".join(
        f"Sentence {number} " + ("alpha " * 20) + "ends." for number in range(1, 9)
    )
    _, plan, _, _ = _state(f"<html><body><p>{prose}</p></body></html>")
    block = plan.blocks[0]
    sizes = {}
    for version, located in (("v8", False), ("v9", True)):
        row = compact_source_block(
            block,
            number=1,
            implicit_outer_wrappers=True,
            include_sentences=not located,
            locate_sentences=located,
        )
        packet = {
            "contract_version": f"translation-block-writable-source-{version}",
            "annotation_profile": "none",
            "blocks": [row],
        }
        sizes[version] = len(
            json.dumps(
                packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
    print(
        f"synthetic writable packet bytes: v8={sizes['v8']} v9={sizes['v9']} delta={sizes['v9'] - sizes['v8']}"
    )
    assert sizes["v9"] < sizes["v8"]
