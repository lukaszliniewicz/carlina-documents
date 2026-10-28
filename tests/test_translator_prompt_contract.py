"""Regression contract for the legacy Translator prompt composition.

These tests intentionally exercise only the deterministic message builder.  A
provider/model call is neither needed nor desirable here: the purpose of the
contract is to make changes to the carefully tuned legacy prompt visible before
the block engine starts using a different storage path.
"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from iarf_assistant.addons.translator import worker
from iarf_assistant.addons.translator.prompts import DEFAULT_TRANSLATOR_SYSTEM_PROMPT

SOURCE_BATCH = [
    SimpleNamespace(id="segment-1", original_text='A synthetic source "sentence".')
]


def _project(*, settings=None, working_memory=None, saved_prompt=None):
    addon_settings = {}
    if saved_prompt is not None:
        addon_settings = {"translator": {"prompt_translator_agent": saved_prompt}}
    return SimpleNamespace(
        id="project-1",
        source_lang="English",
        target_lang="Polish",
        settings=settings or {},
        working_memory=working_memory,
        user=SimpleNamespace(addon_settings=addon_settings),
    )


def _glossary_term():
    return SimpleNamespace(
        source_term="synthetic source",
        target_term="syntetyczne źródło",
        context_notes="CONTRACT_GLOSSARY_SENTINEL",
        is_user_defined=True,
        needs_review=False,
        status="approved",
    )


def _render(
    *,
    settings=None,
    working_memory=None,
    glossary_terms=None,
    translation_memory="",
    saved_prompt=None,
    custom_prompt="",
):
    project_settings = dict(settings or {})
    if custom_prompt:
        project_settings["custom_prompt"] = custom_prompt
    project = _project(
        settings=project_settings,
        working_memory=working_memory,
        saved_prompt=saved_prompt,
    )
    with patch.object(
        worker, "_query_translation_memory", return_value=translation_memory
    ):
        return worker._translation_messages(
            project,
            SOURCE_BATCH,
            glossary_terms=[] if glossary_terms is None else glossary_terms,
        )


def _normalized(messages):
    """Serialize only the stable rendered message contract for hashing."""

    payload = [
        {"role": message["role"], "content": message["content"].replace("\r\n", "\n")}
        for message in messages
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(messages):
    return hashlib.sha256(_normalized(messages).encode("utf-8")).hexdigest()


def test_message_builder_has_exact_system_then_user_shape_for_representative_matrix():
    custom_saved_prompt = (
        "CUSTOM_SAVED_PROMPT_SENTINEL {source_lang}->{target_lang}; "
        "glossary={glossary}; memory={memory}; tm={translation_memory}"
    )
    cases = {
        "minimal": _render(),
        "pre-analysis-context": _render(
            settings={"enable_pre_analysis": True},
            working_memory="PRE_ANALYSIS_CONTEXT_SENTINEL",
        ),
        "research-enabled-with-context": _render(
            settings={
                "enable_research": True,
                "research_plugin_ids": ["synthetic-research-provider"],
            },
            working_memory="RESEARCH_CONTEXT_SENTINEL",
        ),
        "glossary-and-translation-memory": _render(
            glossary_terms=[_glossary_term()],
            translation_memory="TM_CONTEXT_SENTINEL",
        ),
        "custom-saved-prompt": _render(saved_prompt=custom_saved_prompt),
        "project-custom-prompt": _render(
            custom_prompt="PROJECT_CUSTOM_PROMPT_SENTINEL"
        ),
    }

    for messages in cases.values():
        assert [message["role"] for message in messages] == ["system", "user"]
        assert list(messages[0]) == ["role", "content"]
        assert list(messages[1]) == ["role", "content"]
        assert messages[1]["content"].startswith("<untrusted_source_batch>\n")
        assert messages[1]["content"].endswith("\n</untrusted_source_batch>")


def test_prompt_context_inclusion_and_exclusion_preserves_current_builder_invariants():
    minimal = _render()
    working_memory_context = _render(
        settings={"enable_pre_analysis": True},
        working_memory="PRE_ANALYSIS_CONTEXT_SENTINEL",
    )
    working_memory_context_without_pre_analysis = _render(
        settings={"enable_pre_analysis": False},
        working_memory="PRE_ANALYSIS_CONTEXT_SENTINEL",
    )
    no_working_memory_context = _render(
        settings={"enable_pre_analysis": True},
        working_memory=None,
    )
    research_with_context = _render(
        settings={
            "enable_research": True,
            "research_plugin_ids": ["RESEARCH_PROVIDER_SENTINEL"],
        },
        working_memory="RESEARCH_CONTEXT_SENTINEL",
    )
    research_disabled_with_context = _render(
        settings={"enable_research": False},
        working_memory="RESEARCH_CONTEXT_SENTINEL",
    )
    no_research_context = _render(settings={"enable_research": False})
    glossary_and_tm = _render(
        glossary_terms=[_glossary_term()],
        translation_memory="TM_CONTEXT_SENTINEL",
    )

    minimal_system = minimal[0]["content"]
    working_memory_system = working_memory_context[0]["content"]
    working_memory_without_pre_analysis_system = (
        working_memory_context_without_pre_analysis[0]["content"]
    )
    no_working_memory_system = no_working_memory_context[0]["content"]
    research_system = research_with_context[0]["content"]
    research_disabled_system = research_disabled_with_context[0]["content"]
    no_research_system = no_research_context[0]["content"]
    glossary_and_tm_system = glossary_and_tm[0]["content"]

    # These settings affect earlier/later workflow phases.  The current
    # message builder injects working_memory whenever it is present, without
    # making either setting a second gate.
    assert "PRE_ANALYSIS_CONTEXT_SENTINEL" in working_memory_system
    assert "PRE_ANALYSIS_CONTEXT_SENTINEL" in working_memory_without_pre_analysis_system
    assert "PRE_ANALYSIS_CONTEXT_SENTINEL" not in no_working_memory_system
    assert "None." in no_working_memory_system
    assert _normalized(working_memory_context) == _normalized(
        working_memory_context_without_pre_analysis
    )

    # The legacy builder has no separate research placeholder.  Enabling the
    # setting controls later tool availability; it must not inject provider
    # identifiers or fabricate research context into this initial prompt.  As
    # with pre-analysis, supplied working_memory is included independently of
    # the setting value.
    assert "RESEARCH_CONTEXT_SENTINEL" in research_system
    assert "RESEARCH_CONTEXT_SENTINEL" in research_disabled_system
    assert "RESEARCH_CONTEXT_SENTINEL" not in no_research_system
    assert "RESEARCH_PROVIDER_SENTINEL" not in research_system
    assert _normalized(research_with_context) == _normalized(
        research_disabled_with_context
    )
    assert _normalized(minimal) == _normalized(no_research_context)

    assert "CONTRACT_GLOSSARY_SENTINEL" in glossary_and_tm_system
    assert "synthetic source" in glossary_and_tm_system
    assert "TM_CONTEXT_SENTINEL" in glossary_and_tm_system
    assert "CONTRACT_GLOSSARY_SENTINEL" not in minimal_system
    assert "TM_CONTEXT_SENTINEL" not in minimal_system


def test_custom_saved_prompt_is_followed_by_mandatory_output_contract():
    saved_prompt = (
        "CUSTOM_SAVED_PROMPT_SENTINEL {source_lang}->{target_lang}; "
        "literal {{json:true}}"
    )
    messages = _render(saved_prompt=saved_prompt)
    system = messages[0]["content"]

    assert system.startswith(
        "CUSTOM_SAVED_PROMPT_SENTINEL English->Polish; literal {json:true}"
    )
    assert worker.TARGET_OUTPUT_CONTRACT in system
    assert system.index("CUSTOM_SAVED_PROMPT_SENTINEL") < system.index(
        worker.TARGET_OUTPUT_CONTRACT
    )
    assert "exactly one non-empty `<text>...</text>` child" in system
    assert "only top-level sibling `<target>`" in system


def test_project_custom_prompt_is_appended_once_after_mandatory_output_contract():
    system = _render(custom_prompt="PROJECT_CUSTOM_PROMPT_SENTINEL")[0]["content"]
    minimal_system = _render()[0]["content"]
    project_instructions = (
        "<project_instructions>PROJECT_CUSTOM_PROMPT_SENTINEL"
        "</project_instructions>"
    )

    assert system.count("PROJECT_CUSTOM_PROMPT_SENTINEL") == 1
    assert "PROJECT_CUSTOM_PROMPT_SENTINEL" not in minimal_system
    assert system.count("<project_instructions>") == 1
    assert system.count("</project_instructions>") == 1
    assert worker.TARGET_OUTPUT_CONTRACT in system
    assert system.index(worker.TARGET_OUTPUT_CONTRACT) < system.index(
        project_instructions
    )
    assert system.endswith(project_instructions)


def test_legacy_prompt_rendered_message_snapshots_are_stable():
    custom_saved_prompt = (
        "CUSTOM_SAVED_PROMPT_SENTINEL {source_lang}->{target_lang}; "
        "glossary={glossary}; memory={memory}; tm={translation_memory}"
    )
    matrix = {
        "minimal": _render(),
        "pre-analysis-context": _render(
            settings={"enable_pre_analysis": True},
            working_memory="PRE_ANALYSIS_CONTEXT_SENTINEL",
        ),
        "research-enabled-with-context": _render(
            settings={"enable_research": True},
            working_memory="RESEARCH_CONTEXT_SENTINEL",
        ),
        "glossary-and-translation-memory": _render(
            glossary_terms=[_glossary_term()],
            translation_memory="TM_CONTEXT_SENTINEL",
        ),
        "custom-saved-prompt": _render(saved_prompt=custom_saved_prompt),
        "project-custom-prompt": _render(
            custom_prompt="PROJECT_CUSTOM_PROMPT_SENTINEL"
        ),
    }

    assert {name: _digest(messages) for name, messages in matrix.items()} == {
        "minimal": "c993b3bd3f925832af81b946eb73554c2ca8371683a277dc378c005be2eff039",
        "pre-analysis-context": "a312b0cd9857de85eb19b69760a363466d987da8f5628cbffe9864953fb48dd3",
        "research-enabled-with-context": "2a86833b50d96cbfcad88277b6956a6ca36c18d3a744868426d2348eca0883ec",
        "glossary-and-translation-memory": "a40630cd5976e3760853672b450185d2b12737fee802a01eadbca177ce86b00d",
        "custom-saved-prompt": "402e08f51af0f9c4a26bc3c7d624e3855a8d87d42ee2c524b5b1bc09e0a92518",
        "project-custom-prompt": "cc218c3419477ccf015ccfb5351c097ccfa9ed54e0b58a5d368d763379f0e900",
    }


def test_default_prompt_source_is_the_fallback_when_no_saved_prompt_exists():
    messages = _render()
    assert DEFAULT_TRANSLATOR_SYSTEM_PROMPT.splitlines()[0].format(
        source_lang="English", target_lang="Polish"
    ) in messages[0]["content"]
