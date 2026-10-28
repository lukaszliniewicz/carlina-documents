from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

_APPLICATION_ROOT = "iarf" + "_assistant"
_TRANSLATOR_ROOT = f"{_APPLICATION_ROOT}.addons.translator"
_block_contract = import_module(f"{_TRANSLATOR_ROOT}.block_contract")
_block_persistence = import_module(f"{_TRANSLATOR_ROOT}.block_persistence")
_block_planner = import_module(f"{_TRANSLATOR_ROOT}.block_planner")
_block_workspace = import_module(f"{_TRANSLATOR_ROOT}.block_workspace_api")
_models = import_module(f"{_TRANSLATOR_ROOT}.models")
_qa = import_module(f"{_TRANSLATOR_ROOT}.qa_diagnostics")
_target_contract = import_module(f"{_TRANSLATOR_ROOT}.target_contract")
_encryption = import_module(f"{_APPLICATION_ROOT}.core.encryption")
db = import_module(f"{_APPLICATION_ROOT}.extensions").db
User = import_module(f"{_APPLICATION_ROOT}.models").User

AttestedDocumentSnapshot = _block_contract.AttestedDocumentSnapshot
persist_source_plan = _block_persistence.persist_source_plan
source_attestation_for_project = _block_persistence.source_attestation_for_project
plan_document_blocks = _block_planner.plan_document_blocks
persist_manual_candidate = _block_workspace.persist_manual_candidate
TranslationProject = _models.TranslationProject
_GlossaryIndex = _qa._GlossaryIndex
_citation_findings = _qa._citation_findings
_length_findings = _qa._length_findings
_linguistic_findings = _qa._linguistic_findings
_prepare_glossary_entries = _qa._prepare_glossary_entries
_punctuation_findings = _qa._punctuation_findings
_terminology_findings = _qa._terminology_findings
qa_diagnostics = _qa.qa_diagnostics
TargetBlockCandidate = _target_contract.TargetBlockCandidate
TargetTextPart = _target_contract.TargetTextPart
structural_fingerprint = _target_contract.structural_fingerprint
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'qa.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-qa-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    context = application.app_context()
    context.push()
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _project() -> TranslationProject:
    owner = User(email="qa@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="QA",
        source_lang="de",
        target_lang="en",
        status="setup",
        engine_contract="document-block-v1",
        document_id="qa-doc",
        settings={
            "source_type": "document",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "qa-doc",
            },
        },
    )
    db.session.add(project)
    db.session.commit()
    return project


def _plan(
    project: TranslationProject,
    paragraphs: tuple[str, ...] | None = None,
):
    paragraph_html = (
        "<p>Siehe [Eingabe, 1845] für die weitere Diskussion.</p>"
        if paragraphs is None
        else "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    )
    source = (
        "<html><body>"
        '<span id="doc-page-1" class="pagebreak" role="doc-pagebreak" title="1"></span>'
        f"{paragraph_html}</body></html>"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="qa-doc",
        document_id="qa-doc",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-19T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="de",
    )
    record, _created = persist_source_plan(project, plan)
    db.session.commit()
    return plan, record


def _candidate(plan, record, block, text: str) -> TargetBlockCandidate:
    return TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(TargetTextPart(kind="text", text=text),),
        lexical_attributes=(),
    )


def test_qa_reports_reference_punctuation_and_physical_page(app):
    project = _project()
    plan, record = _plan(project)
    block = next(block for block in plan.blocks if "Eingabe" in block.projection)
    persist_manual_candidate(
        project,
        block.id,
        _candidate(
            plan,
            record,
            block,
            "See [Petition, 1845] for the further discussion!!",
        ),
        0,
        author_user_id=project.user_id,
    )
    db.session.commit()

    result = qa_diagnostics(project, include_spelling=False)
    codes = {item["code"] for item in result["items"]}
    assert "citation-key-mutated-or-missing" in codes
    assert "repeated-punctuation" in codes
    assert result["summary"]["by_severity"]["integrity"] >= 1
    citation = next(
        item for item in result["items"] if item["code"] == "citation-key-mutated-or-missing"
    )
    assert citation["source_pages"] == [1]
    assert citation["evidence"]["source_key"] == "[Eingabe, 1845]"

    references = qa_diagnostics(
        project, categories=("references",), include_spelling=False
    )
    assert references["items"]
    assert {item["category"] for item in references["items"]} == {"references"}


def test_qa_reuses_per_block_diagnostics_for_unchanged_targets(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project()
    plan, record = _plan(project)
    block = next(block for block in plan.blocks if "Eingabe" in block.projection)
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "See [Eingabe, 1845]."),
        0,
        author_user_id=project.user_id,
    )
    db.session.commit()

    original = qa_diagnostics.__globals__["_diagnose_block"]
    calls: list[str] = []

    def counted(**kwargs):
        calls.append(str(kwargs["block"].id))
        return original(**kwargs)

    monkeypatch.setitem(qa_diagnostics.__globals__, "_diagnose_block", counted)
    first = qa_diagnostics(project, include_spelling=False)
    second = qa_diagnostics(project, include_spelling=False)

    assert first["summary"] == second["summary"]
    assert calls == [block.id]


def test_qa_truncates_noisy_categories_honestly(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project()
    plan, record = _plan(project)
    block = next(block for block in plan.blocks if "Eingabe" in block.projection)
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "First!! Second?? Third;;"),
        0,
        author_user_id=project.user_id,
    )
    db.session.commit()
    monkeypatch.setitem(
        qa_diagnostics.__globals__["MAX_QA_FINDINGS_BY_SEVERITY"],
        "warning",
        1,
    )

    result = qa_diagnostics(
        project,
        categories=("punctuation",),
        include_spelling=False,
    )

    assert result["summary"]["total"] == 1
    assert result["summary"]["truncated"] is True
    assert result["summary"]["omitted_total"] == 2
    assert result["summary"]["omitted_by_category"]["punctuation"] == 2


class _Dictionary:
    def __init__(self, language: str, valid: set[str], suggestions=None):
        self.language = language
        self.available = True
        self.valid = {item.casefold() for item in valid}
        self.suggestions = suggestions or {}

    def check(self, word: str) -> bool:
        return word.casefold() in self.valid

    def suggest(self, word: str):
        return tuple(self.suggestions.get(word.casefold(), ()))


def test_linguistic_qa_detects_join_split_leakage_and_spelling():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    target_dictionary = _Dictionary(
        "en_US",
        {"of", "the", "translation", "text"},
        {"ofthe": ("of the",)},
    )
    source_dictionary = _Dictionary("de_DE", {"und", "freiheit"})
    findings = _linguistic_findings(
        workspace=workspace,
        pages={"block-1": [7]},
        block=block,
        target_version=1,
        source_text="Ausgangstext.",
        target_text="ofthe trans lation und Freiheit",
        target_dictionary=target_dictionary,
        source_dictionary=source_dictionary,
        accepted_words=set(),
    )
    codes = {item["code"] for item in findings}
    assert {"joined-token", "split-token", "source-language-leakage"} <= codes
    assert "spelling-suspect" in codes
    assert all(item["source_pages"] == [7] for item in findings)


def test_disabling_spelling_keeps_token_and_language_checks():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    target_dictionary = _Dictionary(
        "en_US",
        {"of", "the", "translation"},
        {"ofthe": ("of the",)},
    )
    source_dictionary = _Dictionary("de_DE", {"und", "freiheit", "eingabe"})
    findings = _linguistic_findings(
        workspace=workspace,
        pages={"block-1": [7]},
        block=block,
        target_version=1,
        source_text="Ausgangstext.",
        target_text="[Eingabe, 1845] ofthe trans lation und Freiheit",
        target_dictionary=target_dictionary,
        source_dictionary=source_dictionary,
        accepted_words=set(),
        include_spelling=False,
    )
    codes = {item["code"] for item in findings}
    assert {"joined-token", "split-token", "source-language-leakage"} <= codes
    assert "spelling-suspect" not in codes
    assert not any(
        item.get("target", {}).get("match") == "Eingabe" for item in findings
    )


def test_accepted_contextual_target_variant_satisfies_glossary_qa():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    entry = {
        "concept_id": "concept-1",
        "source_term": "freireligiös",
        "target_term": "free-religious",
        "accepted_target_forms": ["Free Religious"],
        "match_policy": "preferred",
        "authority": "human_locked",
    }
    accepted = _terminology_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="Die freireligiöse Gemeinde",
        target_text="The Free Religious congregation",
        entries=_prepare_glossary_entries([entry]),
    )
    assert accepted == []

    divergent = _terminology_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="freireligiös",
        target_text="free religion",
        entries=_prepare_glossary_entries([entry]),
    )
    assert [item["code"] for item in divergent] == ["glossary-preferred-divergence"]


def test_glossary_index_returns_only_plausible_candidates():
    entries = [
        {
            "concept_id": f"concept-{index}",
            "source_term": f"term{index}",
            "source_forms": [f"term{index}"],
            "target_term": f"target{index}",
            "accepted_target_forms": [],
            "observations": [],
        }
        for index in range(500)
    ]
    entries.append(
        {
            "concept_id": "observed",
            "source_term": "not printed here",
            "source_forms": ["not printed here"],
            "target_term": "observed target",
            "accepted_target_forms": [],
            "observations": [{"block_id": "block-9"}],
        }
    )
    index = _GlossaryIndex(entries)

    lexical = index.candidates("This contains term321 only.", "block-1")
    assert [item[0]["concept_id"] for item in lexical] == ["concept-321"]

    observed = index.candidates("No lexical term is present.", "block-9")
    assert [item[0]["concept_id"] for item in observed] == ["observed"]


def test_glossary_index_handles_unspaced_scripts():
    index = _GlossaryIndex(
        [
            {
                "concept_id": "japanese-term",
                "source_term": "自由宗教",
                "source_forms": ["自由宗教"],
                "target_term": "free religion",
                "accepted_target_forms": [],
                "observations": [],
            },
            {
                "concept_id": "other-term",
                "source_term": "神道",
                "source_forms": ["神道"],
                "target_term": "Shinto",
                "accepted_target_forms": [],
                "observations": [],
            },
        ]
    )
    candidates = index.candidates("これは自由宗教についての文章です。", "block-jp")
    assert [item[0]["concept_id"] for item in candidates] == ["japanese-term"]


def test_terminology_matching_does_not_accept_substrings_inside_words():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    entries = _prepare_glossary_entries(
        [
            {
                "concept_id": "concept-1",
                "source_term": "free",
                "target_term": "libre",
                "match_policy": "exact",
            }
        ]
    )
    assert _terminology_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="Freedom is discussed here.",
        target_text="La liberté est discutée ici.",
        entries=entries,
    ) == []


def test_exact_glossary_policy_preserves_case_unless_variant_is_declared():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    entry = {
        "concept_id": "concept-1",
        "source_term": "Freireligiös",
        "target_term": "Free Religious",
        "accepted_target_forms": [],
        "match_policy": "exact",
    }
    findings = _terminology_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="Freireligiös",
        target_text="free religious",
        entries=_prepare_glossary_entries([entry]),
    )
    assert [item["code"] for item in findings] == ["glossary-exact-missing"]

    entry["accepted_target_forms"] = ["free religious"]
    assert _terminology_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="Freireligiös",
        target_text="free religious",
        entries=_prepare_glossary_entries([entry]),
    ) == []


def test_reference_integrity_reports_added_keys_without_double_counting_mutations():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    added = _citation_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="See [Eingabe, 1845].",
        target_text="See [Eingabe, 1845] and [Petition, 1846].",
    )
    assert [item["code"] for item in added] == ["citation-key-added"]
    assert added[0]["evidence"]["target_key"] == "[Petition, 1846]"

    mutated = _citation_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="See [Eingabe, 1845].",
        target_text="See [Petition, 1845].",
    )
    assert [item["code"] for item in mutated] == [
        "citation-key-mutated-or-missing"
    ]
    assert mutated[0]["evidence"]["kind"] == "mutation"
    assert mutated[0]["evidence"]["target_key"] == "[Petition, 1845]"


def test_repeated_punctuation_allows_three_dot_ellipsis():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    findings = _punctuation_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="Source.",
        target_text="Wait... Really.. No!!!!",
    )
    assert [item["evidence"]["sequence"] for item in findings] == ["..", "!!!!"]


def test_length_ratio_allows_expected_cjk_expansion():
    workspace = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    block = SimpleNamespace(id="block-1", order=1, role="paragraph")
    assert _length_findings(
        workspace=workspace,
        pages={"block-1": [1]},
        block=block,
        target_version=1,
        source_text="自由宗教" * 8,
        target_text="A deliberately expanded English rendering of the source passage. " * 3,
    ) == []


class _ControlledDictionary:
    def __init__(self, language: str, *, signature: str = "controlled-v1"):
        self.requested_language = language
        self.available = language == "en"
        self.language = "en_US" if self.available else None
        self.signature = signature

    def check(self, word: str) -> bool:
        return word.casefold() not in {"mispell", "badword"}

    def suggest(self, _word: str):
        return ()


def _persist_paragraph_targets(project, plan, record, target_texts):
    blocks = [
        block
        for block in plan.blocks
        if any(source_word in block.projection for source_word in ("alpha", "beta"))
    ]
    blocks.sort(key=lambda block: block.order)
    assert len(blocks) == len(target_texts)
    for block, text in zip(blocks, target_texts, strict=True):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, text),
            0,
            author_user_id=project.user_id,
        )
    db.session.commit()
    return blocks


def test_token_grouping_preserves_and_pages_every_occurrence(app, monkeypatch):
    project = _project()
    plan, record = _plan(project, ("Source alpha.", "Source beta."))
    blocks = _persist_paragraph_targets(
        project, plan, record, ("Mispell alpha.", "MISPELL Badword beta.")
    )
    monkeypatch.setattr(
        _qa,
        "_EnchantDictionary",
        lambda language: _ControlledDictionary(language),
    )

    grouped = qa_diagnostics(project, categories=("spelling",), group_by="token")

    assert grouped["summary"]["total"] == 3
    assert grouped["summary"]["total_groups"] == 2
    assert grouped["summary"]["unique_token_count"] == 2
    assert grouped["summary"]["preview_omitted_occurrences"] == 1
    assert grouped["summary"]["omissions_scope"] == "scan"
    assert all(len(item["preview_occurrences"]) == 1 for item in grouped["groups"])
    group = next(item for item in grouped["groups"] if item["token"] == "Mispell")
    assert group["token"] == "Mispell"
    assert group["count"] == 2
    assert group["preview_omitted"] == 1
    assert group["preview_occurrences"][0]["block_id"] in {block.id for block in blocks}
    assert group["preview_occurrences"][0]["location"]["target"] == {
        "start": 0,
        "end": 7,
    }
    first_group_page = qa_diagnostics(
        project, categories=("spelling",), group_by="token", limit=1
    )
    assert len(first_group_page["groups"]) == 1
    assert first_group_page["pagination"]["total"] == 2
    assert first_group_page["pagination"]["has_more"] is True

    group_id = group["id"]
    first_page = qa_diagnostics(
        project,
        categories=("spelling",),
        group_by="token",
        group_id=group_id,
        include_excerpts=False,
        limit=1,
    )
    assert len(first_page["occurrences"]) == 1
    assert first_page["pagination"]["total"] == 2
    assert first_page["summary"]["total"] == 3
    assert first_page["summary"]["total_groups"] == 2
    assert first_page["summary"]["unique_token_count"] == 2
    assert "target" not in first_page["occurrences"][0]
    assert "source" not in first_page["occurrences"][0]
    cursor = first_page["pagination"]["next_cursor"]
    assert cursor
    second_page = qa_diagnostics(
        project,
        categories=("spelling",),
        group_by="token",
        group_id=group_id,
        include_excerpts=False,
        limit=1,
        cursor=cursor,
    )
    assert len(second_page["occurrences"]) == 1
    assert first_page["occurrences"][0]["id"] != second_page["occurrences"][0]["id"]
    assert {
        item["id"] for item in first_page["occurrences"] + second_page["occurrences"]
    } == {
        item["id"]
        for item in qa_diagnostics(project, categories=("spelling",))["items"]
        if item["target"]["match"].casefold() == "mispell"
    }


def test_default_findings_remain_individual_and_excerpts_can_be_disabled(
    app, monkeypatch
):
    project = _project()
    plan, record = _plan(project, ("Source alpha.", "Source beta."))
    _persist_paragraph_targets(
        project, plan, record, ("Mispell alpha.", "Mispell beta.")
    )
    monkeypatch.setattr(
        _qa,
        "_EnchantDictionary",
        lambda language: _ControlledDictionary(language),
    )

    result = qa_diagnostics(project, categories=("spelling",), include_excerpts=False)

    assert result["summary"]["total"] == 2
    assert result["summary"]["total_groups"] == 2
    assert result["summary"]["grouping"] == "finding"
    assert len(result["items"]) == 2
    assert all(
        "source" not in item and "target" not in item for item in result["items"]
    )
    assert all("_qa_location" not in item for item in result["items"])


def test_grouping_keeps_code_severity_and_integrity_findings_separate():
    same_token = {"match": "Mispell"}
    findings = [
        {
            "id": f"{index:024x}",
            "category": category,
            "code": code,
            "severity": severity,
            "target": same_token,
            "evidence": {
                "source_term": "Freireligiös"
                if category == "terminology" and index % 2
                else "freireligiös"
                if category == "terminology"
                else None
            },
        }
        for index, (category, code, severity) in enumerate(
            (
                ("spelling", "spelling-suspect", "advisory"),
                ("tokens", "joined-token", "warning"),
                ("spelling", "spelling-suspect", "warning"),
                ("terminology", "glossary-preferred-divergence", "advisory"),
                ("terminology", "glossary-preferred-divergence", "advisory"),
                ("integrity", "target-structural-integrity", "integrity"),
                ("integrity", "target-structural-integrity", "integrity"),
            ),
            start=1,
        )
    ]

    groups = _qa._group_findings(findings)

    assert len(groups) == 6
    assert [
        group["count"] for group in groups if group["category"] == "terminology"
    ] == [2]
    assert all(
        group["count"] == 1
        for group in groups
        if group["category"] not in {"terminology"}
    )
    assert len({group["id"] for group in groups}) == 6


def test_grouping_mode_filter_and_target_edits_invalidate_cursors(app, monkeypatch):
    project = _project()
    plan, record = _plan(project, ("Source alpha.", "Source beta."))
    blocks = _persist_paragraph_targets(
        project, plan, record, ("Mispell alpha.", "Badword beta.")
    )
    monkeypatch.setattr(
        _qa,
        "_EnchantDictionary",
        lambda language: _ControlledDictionary(language),
    )

    first = qa_diagnostics(
        project, categories=("spelling",), group_by="finding", limit=1
    )
    cursor = first["pagination"]["next_cursor"]
    assert cursor
    for options in (
        {"group_by": "token"},
        {"include_excerpts": False},
        {"categories": ("tokens",)},
    ):
        request = {
            "categories": ("spelling",),
            "limit": 1,
            "cursor": cursor,
            **options,
        }
        with pytest.raises(ValueError) as raised:
            qa_diagnostics(project, **request)
        assert getattr(raised.value, "code", None) == "review-cursor-mismatch"

    pointers, _targets = _block_workspace._rehydrate_workspace_objects(
        _block_workspace.load_block_source_plan(project)
    )
    edited_block = blocks[0]
    persist_manual_candidate(
        project,
        edited_block.id,
        _candidate(plan, record, edited_block, "Mispell alpha edited."),
        int(pointers[edited_block.id].version),
        author_user_id=project.user_id,
    )
    db.session.commit()
    with pytest.raises(ValueError) as raised:
        qa_diagnostics(
            project,
            categories=("spelling",),
            group_by="finding",
            limit=1,
            cursor=cursor,
        )
    assert getattr(raised.value, "code", None) == "review-cursor-mismatch"


def test_dictionary_signature_changes_cache_and_spellcheck_capability(app, monkeypatch):
    project = _project()
    plan, record = _plan(project)
    block = next(block for block in plan.blocks if "Eingabe" in block.projection)
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "A stable target."),
        0,
        author_user_id=project.user_id,
    )
    db.session.commit()
    signatures = {"en": "dictionary-a", "de": "source-v1"}
    target_available = {"en": True}

    class MutableSignatureDictionary(_ControlledDictionary):
        def __init__(self, language: str):
            super().__init__(language, signature=signatures[language])
            if language == "en" and not target_available[language]:
                self.available = False
                self.language = None

    monkeypatch.setattr(_qa, "_EnchantDictionary", MutableSignatureDictionary)
    original = qa_diagnostics.__globals__["_diagnose_block"]
    calls = []

    def counted(**kwargs):
        calls.append(kwargs["block"].id)
        return original(**kwargs)

    monkeypatch.setitem(qa_diagnostics.__globals__, "_diagnose_block", counted)

    first = qa_diagnostics(project, include_spelling=False)
    qa_diagnostics(project, include_spelling=False)
    assert calls == [block.id]
    assert first["capabilities"]["spellcheck"]["available"] is True
    assert first["capabilities"]["semantic_review"] is False

    signatures["en"] = "dictionary-b"
    second = qa_diagnostics(project, include_spelling=False)
    assert calls == [block.id, block.id]
    assert second["capabilities"]["spellcheck"]["available"] is True

    target_available["en"] = False
    unavailable = qa_diagnostics(project, include_spelling=False)
    assert unavailable["capabilities"]["spellcheck"]["available"] is False


def test_group_id_validation_and_lookup_are_controlled(app, monkeypatch):
    project = _project()
    plan, record = _plan(project, ("Source alpha.", "Source beta."))
    _persist_paragraph_targets(
        project, plan, record, ("Mispell alpha.", "Badword beta.")
    )
    monkeypatch.setattr(
        _qa,
        "_EnchantDictionary",
        lambda language: _ControlledDictionary(language),
    )

    for group_id, expected in (
        ("bad", "qa-group-id-invalid"),
        ("f" * 24, "qa-group-not-found"),
    ):
        with pytest.raises(_qa.QaDiagnosticsError) as raised:
            qa_diagnostics(
                project, categories=("spelling",), group_by="token", group_id=group_id
            )
        assert raised.value.code == expected

    with pytest.raises(_qa.QaDiagnosticsError) as raised:
        qa_diagnostics(project, group_id="f" * 24)
    assert raised.value.code == "qa-group-id-invalid"
