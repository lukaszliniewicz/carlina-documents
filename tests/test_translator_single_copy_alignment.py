"""Single-copy v8 transport, canonical projection, and legacy compatibility."""

from __future__ import annotations

import json

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
    target_alignment_scope,
)
from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.block_translation import (
    _output_skeleton,
    build_window_material,
)
from iarf_assistant.addons.translator.compact_markup import compact_source_block
from iarf_assistant.addons.translator.target_rendering import (
    render_target_block_fragment,
)
from iarf_assistant.addons.translator.window_contract import (
    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
    _target_result_schema,
    _translation_instruction,
)
from iarf_assistant.addons.translator.window_execution import normalize_window_response

pytest_plugins = ("test_translator_block_translation",)
V7 = SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
V8 = SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION


def _translate(plan, record, content, groups, *, index=0, fields=None):
    block = plan.blocks[index]
    source = compact_source_block(block, number=1, implicit_outer_wrappers=True)
    result = {
        "n": 1,
        "content": content,
        "fields": source["fields"] if fields is None else fields,
        "notes": [],
        "alignment": {"groups": groups},
    }
    return normalize_window_response(
        {"results": [result]},
        plan=plan,
        plan_id=record.id,
        window=_window(plan, start=index, end=index + 1),
        writable_source_contract_version=V8,
        annotation_profile="none",
        target_language="en",
    )


@pytest.mark.parametrize(
    "source,content,groups,expected",
    [
        (
            "One source sentence.",
            "First.<sb/> Second.<sb/>",
            [{"source": [1], "target": [1, 2]}],
            ("First.", "Second."),
        ),
        (
            "First. Second.",
            "Both together.<sb/>",
            [{"source": [1, 2], "target": [1]}],
            ("Both together.",),
        ),
        (
            "First. Second.",
            "  First. <sb/>\n Second.<sb/>  ",
            [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
            ("First.", "Second."),
        ),
        (
            "1844 – 1928",
            "1844 – 1928<sb/>",
            [{"source": [1], "target": [1]}],
            ("1844 – 1928",),
        ),
        (
            "A heading",
            "An English heading<sb/>",
            [{"source": [1], "target": [1]}],
            ("An English heading",),
        ),
    ],
)
def test_v8_complete_projection_and_monotonic_partitions(
    app, source, content, groups, expected
):
    _, plan, record, _ = _state(f"<html><body><p>{source}</p></body></html>")
    anchors = plan.blocks[0].anchors
    candidates, notes, manifests = _translate(plan, record, content, groups)
    assert not notes and manifests[0].target_sentences == expected
    projection = target_alignment_projection(plan.blocks[0], candidates[0])
    assert "".join(span.text for span in manifests[0].target_spans) == projection
    assert plan.blocks[0].anchors == anchors
    rendered = render_target_block_fragment(
        plan, record.id, plan.blocks[0], candidates[0], target_language="en"
    )
    assert "<sb" not in rendered and "<sb" not in json.dumps(candidates[0].to_dict())


def test_v8_nested_protected_link_spans_sentence_boundary(app):
    _, plan, record, _ = _state(
        '<html><body><p>Start <a href="#n"><b>first. Second</b></a> end.</p></body></html>'
    )
    source = compact_source_block(
        plan.blocks[0], number=1, implicit_outer_wrappers=True
    )
    content = source["content"].replace("first.", "first.<sb/>") + "<sb/>"
    candidates, _, manifests = _translate(
        plan,
        record,
        content,
        [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
    )
    assert manifests[0].target_sentences == ("Start first.", "Second end.")
    rendered = render_target_block_fragment(
        plan, record.id, plan.blocks[0], candidates[0], target_language="en"
    )
    assert rendered.count('href="#n"') == 1 and rendered.count("<b ") == 1
    assert "<sb" not in rendered


@pytest.mark.parametrize(
    "source,content,expected",
    [
        ("Original.", "Zażółć &amp; Ω.<sb/>", "Zażółć & Ω."),
        ("Original.", "Quoted &amp;lsquo;word&amp;rsquo;.<sb/>", "Quoted ‘word’."),
        ("Original.", "An&nbsp;example.<sb/>", "An\u00a0example."),
        (
            "Use &lt;sb/&gt; literally.",
            "Use &lt;sb/&gt; literally.<sb/>",
            "Use <sb/> literally.",
        ),
        (
            "Use &amp;lsquo; literally.",
            "Use &amp;lsquo; literally.<sb/>",
            "Use &lsquo; literally.",
        ),
    ],
)
def test_v8_entities_unicode_and_literal_marker_text(app, source, content, expected):
    _, plan, record, _ = _state(f"<html><body><p>{source}</p></body></html>")
    candidates, _, manifests = _translate(
        plan, record, content, [{"source": [1], "target": [1]}]
    )
    assert target_alignment_projection(plan.blocks[0], candidates[0]) == expected
    assert manifests[0].target_sentences == (expected,)


@pytest.mark.parametrize(
    "content",
    [
        "One.",
        "One.<sb/> Tail.",
        "<sb/>One.<sb/>",
        "One.<sb/><sb/>",
        "One.<sb/>   <sb/>",
        'One.<sb x="1"/>',
        "One.<sb>text</sb>",
        "One.<sb><b/></sb>",
        'One.<sb xmlns=""/>',
        'One.<sb xmlns:x="x"/>',
        'One.<x:sb xmlns:x="x"/>',
        "One.<!--<sb/>-->",
        "One.<?sb?>",
        '<!DOCTYPE root [<!ENTITY x "One.">]>&x;<sb/>',
        "One.<sb",
        "One.<sb/></bad>",
    ],
)
def test_v8_rejects_missing_forged_or_empty_markers(app, content):
    _, plan, record, _ = _state("<html><body><p>One.</p></body></html>")
    with pytest.raises(BlockContractError):
        _translate(plan, record, content, [{"source": [1], "target": [1]}])


@pytest.mark.parametrize(
    "groups",
    [
        [{"source": [1], "target": [2]}],
        [{"source": [1, 1], "target": [1]}],
        [{"source": [2], "target": [1]}],
        [{"source": [1], "target": [1, 2]}],
        [{"source": [True], "target": [1]}],
        [],
    ],
)
def test_v8_still_rejects_invalid_alignment_groups(app, groups):
    _, plan, record, _ = _state("<html><body><p>One.</p></body></html>")
    with pytest.raises(BlockContractError):
        _translate(plan, record, "One.<sb/>", groups)


def test_v8_protected_literals_remain_atomic_and_exact(app):
    _, plan, record, _ = _state(
        "<html><body><p>Use <code>literal</code> here.</p></body></html>"
    )
    source = compact_source_block(
        plan.blocks[0], number=1, implicit_outer_wrappers=True
    )
    assert "<lit" in source["content"]
    content = source["content"] + "<sb/>"
    candidates, _, manifests = _translate(
        plan, record, content, [{"source": [1], "target": [1]}]
    )
    assert manifests[0].target_sentences == ("Use literal here.",)
    assert "literal" in target_alignment_projection(plan.blocks[0], candidates[0])
    with pytest.raises(BlockContractError, match="compact-literal-shape"):
        _translate(
            plan,
            record,
            content.replace("literal</lit>", "literal<sb/></lit>"),
            [{"source": [1], "target": [1]}],
        )


def test_v8_attached_footnote_marker_is_in_target_projection(app):
    _, plan, record, _ = _state(
        '<html><body><p>Passage<a role="doc-noteref" href="#n">1</a>.</p><aside id="n" role="doc-footnote"><p>Note.</p></aside></body></html>'
    )
    block = plan.blocks[0]
    source = compact_source_block(block, number=1, implicit_outer_wrappers=True)
    candidates, _, manifests = _translate(
        plan, record, source["content"] + "<sb/>", [{"source": [1], "target": [1]}]
    )
    assert manifests[0].target_sentences == ("Passage1.",)
    rendered = render_target_block_fragment(
        plan, record.id, block, candidates[0], target_language="en"
    )
    assert 'href="#n"' in rendered and "<sb" not in rendered


def test_v8_attribute_only_scope_decodes_once_and_strips_markers(app):
    _, plan, record, _ = _state(
        '<html><body><img src="cover.jpg" alt="Source description."></body></html>'
    )
    index = next(
        i
        for i, block in enumerate(plan.blocks)
        if target_alignment_scope(block).startswith("attribute:")
    )
    source = compact_source_block(
        plan.blocks[index], number=1, implicit_outer_wrappers=True
    )
    fields = [
        {
            "name": source["fields"][0]["name"],
            "value": "Élodie &amp; Ω.<sb/> Use &lt;sb/&gt; literally.<sb/>",
        }
    ]
    candidates, _, manifests = _translate(
        plan,
        record,
        source["content"],
        [{"source": [1], "target": [1, 2]}],
        index=index,
        fields=fields,
    )
    assert (
        candidates[0].lexical_attributes[0].value == "Élodie & Ω. Use <sb/> literally."
    )
    assert manifests[0].target_sentences == ("Élodie & Ω.", "Use <sb/> literally.")
    fields[0]["value"] = "<b>Forbidden.</b><sb/>"
    with pytest.raises(BlockContractError):
        _translate(
            plan,
            record,
            source["content"],
            [{"source": [1], "target": [1]}],
            index=index,
            fields=fields,
        )


@pytest.mark.parametrize(
    "version,keys", [(V7, {"groups", "target_sentences"}), (V8, {"groups"})]
)
def test_versioned_packet_schema_and_instructions(app, version, keys):
    _, plan, record, _ = _state()
    profile = _profile()
    config = _config(plan, record, profile)
    config.update(writable_source_contract_version=version, annotation_profile="none")
    packet = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=_window(plan),
        frozen_config=config,
        workflow_config=_workflow_config(),
        execution_profile=profile,
        source_plan_record=record,
        include_continuity=False,
        token_counter=lambda **_: 10,
    ).packet
    schema = packet.response_format["json_schema"]["schema"]
    alignment = schema["properties"]["results"]["items"]["properties"]["alignment"]
    assert set(alignment["properties"]) == keys and set(alignment["required"]) == keys
    assert alignment["additionalProperties"] is False
    assert schema == _target_result_schema(version)
    assert ("<sb/>" in _translation_instruction(version)) == (version == V8)
    assert (
        "must quote the target alignment projection"
        in _translation_instruction(version)
    ) == (version == V7)


def test_v8_output_budget_removes_duplicate_target_prose(app):
    text = "A deliberately long source sentence " + "with detail " * 40 + "."
    _, plan, record, _ = _state(f"<html><body><p>{text}</p></body></html>")
    old = _output_skeleton(
        plan=plan,
        source_plan_id=record.id,
        blocks=plan.blocks,
        writable_source_contract_version=V7,
    )
    new = _output_skeleton(
        plan=plan,
        source_plan_id=record.id,
        blocks=plan.blocks,
        writable_source_contract_version=V8,
    )
    assert len(new.encode()) < len(old.encode()) * 0.7
    result = json.loads(new)["results"][0]
    assert set(result["alignment"]) == {"groups"}
    assert "<sb/>" in result["content"]


@pytest.mark.parametrize(
    "profile,prompt_hash,schema_hash",
    [
        (
            "none",
            "2f5f334e0467176184c0225dff72405af145ac32e659a954f1b6cb5bb566e8be",
            "39daafdb84bf874ae5be1cd35e4b26a64fc5255d565fcf59f93829608b858633",
        ),
        (
            "restrained",
            "3c315cdae6973cea9bc2afa018e214260f7fc7c20ec93536ddcf2790ae2b19a5",
            "1aa1a2adca6accd3e9006dddb2855bd0fd819ae5916435dac08973c3aed30a24",
        ),
        (
            "study",
            "36a12b00c7e93143ecb0b173506076432cf616473f5e902d18762616f46f85d7",
            "c446455762bee1342f37cdbe77b510d28ef16f3a6448343c6eb944ea42a13eff",
        ),
    ],
)
def test_v7_frozen_prompt_and_schema_bytes_remain_unchanged(
    profile, prompt_hash, schema_hash
):
    # Digests measured from the clean pre-v8 main worktree at 13b7cf69.
    import hashlib

    assert (
        hashlib.sha256(
            _translation_instruction(V7, annotation_profile=profile).encode()
        ).hexdigest()
        == prompt_hash
    )
    schema = json.dumps(
        _target_result_schema(V7, profile),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert hashlib.sha256(schema.encode()).hexdigest() == schema_hash


def test_v8_bounded_marker_count(app):
    _, plan, record, _ = _state("<html><body><p>One.</p></body></html>")
    with pytest.raises(BlockContractError, match="sentence-marker-limit"):
        _translate(plan, record, "x<sb/>" * 4097, [{"source": [1], "target": [1]}])


def test_v8_cannot_put_sentence_markers_inside_void_elements(app):
    _, plan, record, _ = _state("<html><body><p>One<br/>line.</p></body></html>")
    source = compact_source_block(
        plan.blocks[0], number=1, implicit_outer_wrappers=True
    )
    assert "<br/>" in source["content"]
    with pytest.raises(BlockContractError, match="compact-void-content"):
        _translate(
            plan,
            record,
            source["content"].replace("<br/>", "<br><sb/></br>") + "<sb/>",
            [{"source": [1], "target": [1]}],
        )


def test_v8_rejects_the_old_duplicate_alignment_field(app):
    _, plan, record, _ = _state("<html><body><p>One.</p></body></html>")
    with pytest.raises(BlockContractError, match="sentence-alignment-shape"):
        normalize_window_response(
            {
                "results": [
                    {
                        "n": 1,
                        "content": "One.<sb/>",
                        "fields": [],
                        "notes": [],
                        "alignment": {
                            "target_sentences": ["One."],
                            "groups": [{"source": [1], "target": [1]}],
                        },
                    }
                ]
            },
            plan=plan,
            plan_id=record.id,
            window=_window(plan),
            writable_source_contract_version=V8,
            annotation_profile="none",
            target_language="en",
        )


@pytest.mark.parametrize("text", [
    "Tel.: 123", "mail@example.test", "www.example.test", "1. [Editor, 1904]",
    "One. Second.", "A work made by human hands! - for viewing.", "Final words! -",
])
def test_v8_preserves_explicit_fragment_units_without_punctuation_guessing(app, text):
    _, plan, record, _ = _state("<html><body><p>One.</p></body></html>")
    candidates, _notes, manifests = _translate(
        plan, record, text + "<sb/>", [{"source": [1], "target": [1]}]
    )
    assert target_alignment_projection(plan.blocks[0], candidates[0]) == text
    assert manifests[0].target_sentences == (text,)
