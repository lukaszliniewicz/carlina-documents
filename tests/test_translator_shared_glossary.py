"""Synthetic shared glossary persistence and adoption regression checks."""

from __future__ import annotations

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import text
from test_translator_block_translation import _state
from test_translator_block_translation import app as fixture_app  # noqa: F401
from test_translator_migrations import _migrate_phase5_database, _migration_config
from test_translator_working_glossary import _lock_with_usage

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator import shared_glossary as shared
from iarf_assistant.addons.translator.analysis_contract import private_witness
from iarf_assistant.addons.translator.block_contract import AttestedDocumentSnapshot
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.block_workspace_api import (
    _load_glossary_source_plan,
)
from iarf_assistant.addons.translator.glossary_persistence import (
    activate_glossary_revision,
)
from iarf_assistant.addons.translator.models import (
    TranslationGlossaryActive,
    TranslationGlossaryRevision,
    TranslationProject,
    TranslationProjectGlossaryBinding,
)
from iarf_assistant.addons.translator.working_glossary import propose_glossary
from iarf_assistant.extensions import db

pytestmark = pytest.mark.usefixtures("fixture_app")


def _seed():
    project, _plan, record, operation = _state()
    operation.state = "completed"
    db.session.commit()
    proposal = propose_glossary(project, source_plan_id=record.id,
        entries=[{"source_term": "bank", "target_term": "brzeg", "sense": "river"}],
        idempotency_key="shared-seed")
    db.session.commit()
    return project, proposal["items"][0]


def _volume(template: TranslationProject, name: str) -> TranslationProject:
    project = TranslationProject(user_id=template.user_id, name=name,
        source_lang=template.source_lang, target_lang=template.target_lang,
        engine_contract=template.engine_contract, settings=copy.deepcopy(template.settings))
    db.session.add(project)
    db.session.flush()
    html = f"<html><body><p>{name} distinct source.</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    plan = plan_document_blocks(AttestedDocumentSnapshot(
        id="doc-1", document_id="doc-1", output_html=html,
        content_hash=digest, semantic_content_hash=digest,
        metadata={}, provenance={}, captured_at="2026-09-30T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    ), project_id=project.id, source_attestation=source_attestation_for_project(project),
        source_language=project.source_lang)
    persist_source_plan(project, plan)
    db.session.commit()
    return project


def test_revision_loop_and_historical_retry():
    project, local = _seed()
    created = shared.create_asset(project.user_id, project_id=project.id, name="River",
        concept_ids=[local["concept_id"]], idempotency_key="create")
    asset_id = created["id"]
    assert shared.create_asset(project.user_id, project_id=project.id, name="River",
        concept_ids=[local["concept_id"]], idempotency_key="create") == created
    with pytest.raises(shared.SharedGlossaryError, match="idempotency-conflict"):
        shared.create_asset(project.user_id, project_id=project.id, name="Other",
            concept_ids=[local["concept_id"]], idempotency_key="create")
    state = shared.project_state(project.user_id, project_id=project.id)
    assert state["binding"] is None
    preview = shared.preview_import(project.user_id, project_id=project.id,
        asset_id=asset_id, revision=1, mode="link")
    term_id = preview["rows"][0]["id"]
    applied = shared.apply_import(project.user_id, project_id=project.id, asset_id=asset_id,
        revision=1, mode="link", preview_token=preview["preview_token"],
        decisions=[], idempotency_key="link")
    assert applied["binding"]["adopted_revision"] == 1
    assert applied["imported_count"] == 0
    proposal = shared.propose_contribution(project.user_id, project_id=project.id,
        asset_id=asset_id, expected_revision=1, concept_ids=[], retire_ids=[term_id],
        reason="No longer used", idempotency_key="retire")
    accepted = shared.review_contribution(project.user_id, asset_id=asset_id,
        proposal_id=proposal["proposal_id"], expected_revision=1,
        decision="accept", idempotency_key="accept")
    assert accepted["revision"] == 2
    assert shared.get_asset(project.user_id, asset_id=asset_id, revision=2)["items"] == []
    update = shared.preview_import(project.user_id, project_id=project.id,
        asset_id=asset_id, revision=2, mode="update")
    assert update["rows"][0]["status"] == "retired"
    shared.apply_import(project.user_id, project_id=project.id, asset_id=asset_id,
        revision=2, mode="update", preview_token=update["preview_token"],
        decisions=[{"id": term_id, "choice": "local"}], idempotency_key="update")
    assert shared.apply_import(project.user_id, project_id=project.id, asset_id=asset_id,
        revision=1, mode="link", preview_token=preview["preview_token"],
        decisions=[], idempotency_key="link") == applied
    assert shared.get_asset(project.user_id, asset_id=asset_id, revision=1)["items"][0]["id"] == term_id
    backward = shared.preview_import(project.user_id, project_id=project.id,
        asset_id=asset_id, revision=1, mode="update")
    shared.apply_import(project.user_id, project_id=project.id, asset_id=asset_id,
        revision=1, mode="update", preview_token=backward["preview_token"],
        decisions=[{"id": term_id, "choice": "local"}], idempotency_key="backward")
    assert shared.project_state(project.user_id, project_id=project.id)["binding"]["adopted_revision"] == 1


def test_encryption_owner_idle_and_origin_survival():
    project, local = _seed()
    owner_id = project.user_id
    created = shared.create_asset(owner_id, project_id=project.id, name="River",
        concept_ids=[local["concept_id"]], idempotency_key="create")
    asset_id = created["id"]
    raw = db.session.execute(text("SELECT metadata_json FROM translation_shared_glossary WHERE id=:id"), {"id": asset_id}).scalar_one()
    assert "River" not in raw
    raw_terms = db.session.execute(text("SELECT payload_json FROM translation_shared_glossary_revision WHERE asset_id=:id"), {"id": asset_id}).scalar_one()
    assert "brzeg" not in raw_terms
    preview = shared.preview_import(project.user_id, project_id=project.id, asset_id=asset_id,
        revision=1, mode="copy")
    term_id = preview["rows"][0]["id"]
    with pytest.raises(shared.SharedGlossaryError, match="asset-not-found"):
        shared.get_asset(project.user_id + 100, asset_id=asset_id)
    # One active operation blocks materialization, while publication is allowed.
    from iarf_assistant.addons.translator.models import TranslationOperation
    operation = TranslationOperation.query.filter_by(project_id=project.id).first()
    operation.state = "running"
    db.session.commit()
    with pytest.raises(shared.SharedGlossaryError, match="project-busy"):
        shared.apply_import(project.user_id, project_id=project.id, asset_id=asset_id,
            revision=1, mode="copy", preview_token=preview["preview_token"],
            decisions=[{"id": term_id, "choice": "incoming"}], idempotency_key="copy")
    db.session.rollback()
    operation.state = "completed"
    db.session.commit()
    shared.apply_import(project.user_id, project_id=project.id, asset_id=asset_id,
        revision=1, mode="copy", preview_token=preview["preview_token"],
        decisions=[{"id": term_id, "choice": "incoming"}], idempotency_key="copy")
    assert shared.project_state(project.user_id, project_id=project.id)["binding"] is None
    db.session.execute(text("DELETE FROM translation_project WHERE id=:id"), {"id": project.id})
    db.session.commit()
    assert shared.get_asset(owner_id, asset_id=asset_id)["id"] == asset_id


def test_three_way_conflict_and_stale_preview():
    project, local = _seed()
    owner = project.user_id
    record_id = local["revision_id"]  # distinct from the active source plan
    created = shared.create_asset(owner, project_id=project.id, name="River",
        concept_ids=[local["concept_id"]], idempotency_key="create")
    asset_id = created["id"]
    preview = shared.preview_import(owner, project_id=project.id, asset_id=asset_id,
        revision=1, mode="link")
    shared.apply_import(owner, project_id=project.id, asset_id=asset_id,
        revision=1, mode="link", preview_token=preview["preview_token"],
        decisions=[], idempotency_key="link")
    state = shared.project_state(owner, project_id=project.id)
    source_plan_id = state["source_plan_id"]
    assert source_plan_id != record_id
    first = propose_glossary(project, source_plan_id=source_plan_id,
        entries=[{"source_term": "bank", "target_term": "nabrzeże", "sense": "river",
                  "concept_id": local["concept_id"], "expected_revision": local["revision"]}],
        idempotency_key="local-edit-1")
    db.session.commit()
    with pytest.raises(shared.SharedGlossaryError, match="preview-stale"):
        shared.apply_import(owner, project_id=project.id, asset_id=asset_id,
            revision=1, mode="update", preview_token=preview["preview_token"],
            decisions=[], idempotency_key="stale")
    db.session.rollback()
    proposal = shared.propose_contribution(owner, project_id=project.id, asset_id=asset_id,
        expected_revision=1, concept_ids=[local["concept_id"]], reason="Better rendering",
        idempotency_key="contribute")
    shared.review_contribution(owner, asset_id=asset_id, proposal_id=proposal["proposal_id"],
        expected_revision=1, decision="accept", idempotency_key="review")
    propose_glossary(project, source_plan_id=source_plan_id,
        entries=[{"source_term": "bank", "target_term": "rzeka", "sense": "river",
                  "concept_id": local["concept_id"], "expected_revision": first["items"][0]["revision"]}],
        idempotency_key="local-edit-2")
    db.session.commit()
    update = shared.preview_import(owner, project_id=project.id, asset_id=asset_id,
        revision=2, mode="update")
    assert update["rows"][0]["status"] == "conflict"
    assert update["rows"][0]["can_take_incoming"]
    term_id = update["rows"][0]["id"]
    shared.apply_import(owner, project_id=project.id, asset_id=asset_id,
        revision=2, mode="update", preview_token=update["preview_token"],
        decisions=[{"id": term_id, "choice": "local"}], idempotency_key="keep-local")
    final = shared.project_state(owner, project_id=project.id)
    assert final["local_terms"][0]["target_term"] == "rzeka"
    assert final["binding"]["adopted_revision"] == 2


def test_stale_plan_detach_copy_link_and_integrity():
    project, local = _seed()
    owner = project.user_id
    first = shared.create_asset(owner, project_id=project.id, name="One",
        concept_ids=[local["concept_id"]], idempotency_key="one")
    second = shared.create_asset(owner, project_id=project.id, name="Two",
        concept_ids=[local["concept_id"]], idempotency_key="two")
    first_preview = shared.preview_import(owner, project_id=project.id,
        asset_id=first["id"], revision=1, mode="link")
    shared.apply_import(owner, project_id=project.id, asset_id=first["id"],
        revision=1, mode="link", preview_token=first_preview["preview_token"],
        decisions=[], idempotency_key="link")
    copy_preview = shared.preview_import(owner, project_id=project.id,
        asset_id=second["id"], revision=1, mode="copy")
    copied = shared.apply_import(owner, project_id=project.id, asset_id=second["id"],
        revision=1, mode="copy", preview_token=copy_preview["preview_token"],
        decisions=[{"id": copy_preview["rows"][0]["id"], "choice": "incoming"}],
        idempotency_key="copy")
    assert copied["binding"]["asset_id"] == first["id"]
    binding = db.session.get(TranslationProjectGlossaryBinding, project.id)
    binding.source_plan_id = "historical-source-plan"
    binding.binding_receipt = private_witness(shared._binding_witness(binding),
        purpose="translator-shared-glossary-binding-v1")
    db.session.commit()
    state = shared.project_state(owner, project_id=project.id)
    assert state["binding"]["stale_source_plan"] is True
    with pytest.raises(shared.SharedGlossaryError, match="source-plan-conflict"):
        shared.preview_import(owner, project_id=project.id, asset_id=first["id"],
            revision=1, mode="update")
    detached = shared.detach_asset(owner, project_id=project.id,
        expected_version=state["binding_version"], idempotency_key="detach")
    assert detached["binding"] is None
    assert shared.project_state(owner, project_id=project.id)["source_plan_id"] != "historical-source-plan"
    # Ciphertext swaps are detectable even when decryption itself succeeds.
    encrypted = db.session.execute(text(
        "SELECT metadata_json FROM translation_shared_glossary WHERE id=:id"),
        {"id": second["id"]}).scalar_one()
    db.session.execute(text(
        "UPDATE translation_shared_glossary SET metadata_json=:payload WHERE id=:id"),
        {"payload": encrypted, "id": first["id"]})
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(shared.SharedGlossaryError, match="metadata-corrupt"):
        shared.get_asset(owner, asset_id=first["id"])


def test_late_failure_rolls_back_and_receipt_identity(monkeypatch):
    project, local = _seed()
    real_save = shared._save_receipt

    def late_failure(*_args, **_kwargs):
        raise RuntimeError("synthetic late failure")

    monkeypatch.setattr(shared, "_save_receipt", late_failure)
    with pytest.raises(RuntimeError, match="synthetic late failure"):
        shared.create_asset(project.user_id, project_id=project.id, name="River",
            concept_ids=[local["concept_id"]], idempotency_key="rollback")
    assert shared.list_assets(project.user_id)["items"] == []
    monkeypatch.setattr(shared, "_save_receipt", real_save)
    created = shared.create_asset(project.user_id, project_id=project.id, name="River",
        concept_ids=[local["concept_id"]], idempotency_key="real")
    raw = db.session.execute(text(
        "SELECT payload_json FROM translation_shared_glossary_receipt WHERE retry_key='real'"
    )).scalar_one()
    db.session.execute(text(
        "UPDATE translation_shared_glossary_receipt SET payload_json=:payload WHERE retry_key='real'"
    ), {"payload": db.session.execute(text(
        "SELECT metadata_json FROM translation_shared_glossary WHERE id=:id"
    ), {"id": created["id"]}).scalar_one()})
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(shared.SharedGlossaryError, match="receipt-corrupt"):
        shared.create_asset(project.user_id, project_id=project.id, name="River",
            concept_ids=[local["concept_id"]], idempotency_key="real")
    assert "River" not in raw


def test_migration_027_empty_downgrade_and_data_refusal(tmp_path):
    db_path = tmp_path / "shared-migration.db"
    config = _migration_config(db_path)
    application = _migrate_phase5_database(db_path, tmp_path / "migration-instance",
        revision="026_numbering_overlay")
    with application.app_context():
        command.upgrade(config, "027_shared_glossaries")
        command.downgrade(config, "026_numbering_overlay")
        command.upgrade(config, "027_shared_glossaries")
        with db.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO user (id,email,password_hash) VALUES (77,'shared@example.test','x')"
            ))
            connection.execute(text(
                "INSERT INTO translation_shared_glossary "
                "(id,owner_user_id,current_revision,metadata_json,metadata_receipt,created_at,updated_at) "
                "VALUES ('asset-1',77,1,'encrypted-placeholder','receipt','2026-09-30','2026-09-30')"
            ))
        with pytest.raises(RuntimeError, match="cannot downgrade"):
            command.downgrade(config, "026_numbering_overlay")


def test_distinct_volumes_pin_update_back_and_copy():
    volume_one, local = _seed()
    owner = volume_one.user_id
    asset = shared.create_asset(owner, project_id=volume_one.id, name="Two volumes",
        concept_ids=[local["concept_id"]], idempotency_key="asset")
    asset_id = asset["id"]
    v1 = shared.preview_import(owner, project_id=volume_one.id, asset_id=asset_id,
        revision=1, mode="link")
    shared.apply_import(owner, project_id=volume_one.id, asset_id=asset_id,
        revision=1, mode="link", preview_token=v1["preview_token"],
        decisions=[], idempotency_key="link-one")
    volume_two = _volume(volume_one, "Volume II")
    v2_copy = shared.preview_import(owner, project_id=volume_two.id, asset_id=asset_id,
        revision=1, mode="link")
    shared.apply_import(owner, project_id=volume_two.id, asset_id=asset_id,
        revision=1, mode="link", preview_token=v2_copy["preview_token"],
        decisions=[{"id": v2_copy["rows"][0]["id"], "choice": "incoming"}],
        idempotency_key="x" * 160)
    local_two = shared.project_state(owner, project_id=volume_two.id)["local_terms"][0]
    source_plan_two = shared.project_state(owner, project_id=volume_two.id)["source_plan_id"]
    propose_glossary(volume_two, source_plan_id=source_plan_two,
        entries=[{"source_term": "bank", "target_term": "nabrzeże", "sense": "river",
                  "concept_id": local_two["concept_id"], "expected_revision": local_two["revision"]}],
        idempotency_key="volume-two-edit")
    db.session.commit()
    proposal = shared.propose_contribution(owner, project_id=volume_two.id,
        asset_id=asset_id, expected_revision=1, concept_ids=[local_two["concept_id"]],
        reason="Volume II choice", idempotency_key="contribute-two")
    assert proposal["changes"][0]["origin"]["revision_id"]
    assert proposal["changes"][0]["origin"]["revision"] == 2
    shared.review_contribution(owner, asset_id=asset_id, proposal_id=proposal["proposal_id"],
        expected_revision=1, decision="accept", idempotency_key="accept-two")
    preview_two = shared.preview_import(owner, project_id=volume_one.id, asset_id=asset_id,
        revision=2, mode="update")
    term_id = preview_two["rows"][0]["id"]
    shared.apply_import(owner, project_id=volume_one.id, asset_id=asset_id,
        revision=2, mode="update", preview_token=preview_two["preview_token"],
        decisions=[{"id": term_id, "choice": "incoming"}], idempotency_key="update-two")
    preview_one = shared.preview_import(owner, project_id=volume_one.id, asset_id=asset_id,
        revision=1, mode="update")
    shared.apply_import(owner, project_id=volume_one.id, asset_id=asset_id,
        revision=1, mode="update", preview_token=preview_one["preview_token"],
        decisions=[{"id": term_id, "choice": "incoming"}], idempotency_key="return-one")
    assert shared.project_state(owner, project_id=volume_one.id)["local_terms"][0]["target_term"] == "brzeg"
    volume_three = _volume(volume_one, "Volume III")
    third = shared.preview_import(owner, project_id=volume_three.id, asset_id=asset_id,
        revision=2, mode="copy")
    shared.apply_import(owner, project_id=volume_three.id, asset_id=asset_id,
        revision=2, mode="copy", preview_token=third["preview_token"],
        decisions=[{"id": term_id, "choice": "incoming"}], idempotency_key="copy-three")
    assert shared.project_state(owner, project_id=volume_three.id)["binding"] is None
    assert shared.project_state(owner, project_id=volume_three.id)["local_terms"][0]["target_term"] == "nabrzeże"


def test_second_import_chunk_failure_rolls_back_all(monkeypatch):
    first, local = _seed()
    owner = first.user_id
    source_plan_id = shared.project_state(owner, project_id=first.id)["source_plan_id"]
    added = propose_glossary(first, source_plan_id=source_plan_id,
        entries=[{"source_term": f"term-{i}", "target_term": f"cel-{i}",
                  "sense": "distinct"} for i in range(32)], idempotency_key="many-terms")
    db.session.commit()
    asset = shared.create_asset(owner, project_id=first.id, name="Thirty three",
        concept_ids=[local["concept_id"], *[item["concept_id"] for item in added["items"]]],
        idempotency_key="asset")
    second = _volume(first, "Volume II")
    preview = shared.preview_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="link")
    real_propose = shared.propose_glossary
    called = 0

    def fail_second(*args, **kwargs):
        nonlocal called
        called += 1
        if called == 2:
            raise RuntimeError("synthetic second chunk failure")
        return real_propose(*args, **kwargs)

    monkeypatch.setattr(shared, "propose_glossary", fail_second)
    with pytest.raises(RuntimeError, match="second chunk failure"):
        shared.apply_import(owner, project_id=second.id, asset_id=asset["id"],
            revision=1, mode="link", preview_token=preview["preview_token"],
            decisions=[{"id": row["id"], "choice": "incoming"} for row in preview["rows"]],
            idempotency_key="many-import")
    assert called == 2
    assert shared.project_state(owner, project_id=second.id)["local_terms"] == []
    assert shared.project_state(owner, project_id=second.id)["binding"] is None
    monkeypatch.setattr(shared, "propose_glossary", real_propose)
    applied = shared.apply_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="link", preview_token=preview["preview_token"],
        decisions=[{"id": row["id"], "choice": "incoming"} for row in preview["rows"]],
        idempotency_key="many-import")
    assert applied["imported_count"] == 33


def test_independent_sessions_contend_on_same_preview(request):
    application = request.getfixturevalue("fixture_app")
    first, local = _seed()
    owner = first.user_id
    asset = shared.create_asset(owner, project_id=first.id, name="Concurrent",
        concept_ids=[local["concept_id"]], idempotency_key="asset")
    second = _volume(first, "Volume II")
    preview = shared.preview_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="link")
    term_id = preview["rows"][0]["id"]
    second_id = second.id
    asset_id = asset["id"]
    db.session.remove()
    barrier = Barrier(2)

    def attempt(key):
        with application.app_context():
            barrier.wait(timeout=10)
            try:
                return shared.apply_import(owner, project_id=second_id, asset_id=asset_id,
                    revision=1, mode="link", preview_token=preview["preview_token"],
                    decisions=[{"id": term_id, "choice": "incoming"}], idempotency_key=key)
            except shared.SharedGlossaryError as exc:
                return exc.code
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("contest-a", "contest-b")))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert "shared-glossary-preview-stale" in outcomes
    assert len(shared.project_state(owner, project_id=second_id)["local_terms"]) == 1


def test_independent_sessions_replay_same_retry_key(request):
    application = request.getfixturevalue("fixture_app")
    first, local = _seed()
    owner = first.user_id
    asset = shared.create_asset(owner, project_id=first.id, name="Concurrent retry",
        concept_ids=[local["concept_id"]], idempotency_key="asset")
    second = _volume(first, "Volume II")
    second_id = second.id
    asset_id = asset["id"]
    preview = shared.preview_import(owner, project_id=second_id, asset_id=asset_id,
        revision=1, mode="link")
    term_id = preview["rows"][0]["id"]
    token = preview["preview_token"]
    db.session.remove()
    barrier = Barrier(2)

    def attempt(_unused):
        with application.app_context():
            barrier.wait(timeout=10)
            try:
                return shared.apply_import(owner, project_id=second_id, asset_id=asset_id,
                    revision=1, mode="link", preview_token=token,
                    decisions=[{"id": term_id, "choice": "incoming"}],
                    idempotency_key="same-retry")
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert results[0] == results[1]
    assert results[0]["imported_count"] == 1
    assert len(shared.project_state(owner, project_id=second_id)["local_terms"]) == 1


def test_suppressed_import_and_stale_proposal_rejection():
    first, local = _seed()
    owner = first.user_id
    asset = shared.create_asset(owner, project_id=first.id, name="Suppressed",
        concept_ids=[local["concept_id"]], idempotency_key="asset")
    second = _volume(first, "Volume II")
    preview = shared.preview_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="link")
    term_id = preview["rows"][0]["id"]
    shared.apply_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="link", preview_token=preview["preview_token"],
        decisions=[{"id": term_id, "choice": "local"}], idempotency_key="suppress")
    again = shared.preview_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="update")
    assert again["rows"][0]["status"] == "local"
    assert again["rows"][0]["suppressed"] is True
    assert shared.project_state(owner, project_id=second.id)["local_terms"] == []
    first_proposal = shared.propose_contribution(owner, project_id=first.id,
        asset_id=asset["id"], expected_revision=1, concept_ids=[], retire_ids=[term_id],
        reason="Retire one", idempotency_key="first-proposal")
    second_proposal = shared.propose_contribution(owner, project_id=first.id,
        asset_id=asset["id"], expected_revision=1, concept_ids=[], retire_ids=[term_id],
        reason="Retire two", idempotency_key="second-proposal")
    shared.review_contribution(owner, asset_id=asset["id"],
        proposal_id=first_proposal["proposal_id"], expected_revision=1,
        decision="accept", idempotency_key="first-review")
    rejected = shared.review_contribution(owner, asset_id=asset["id"],
        proposal_id=second_proposal["proposal_id"], expected_revision=2,
        decision="reject", idempotency_key="second-review")
    assert rejected["state"] == "rejected" and rejected["revision"] == 2


def test_owner_deletion_cascades_asset_and_binding():
    project, local = _seed()
    owner = project.user_id
    asset = shared.create_asset(owner, project_id=project.id, name="Owned",
        concept_ids=[local["concept_id"]], idempotency_key="asset")
    preview = shared.preview_import(owner, project_id=project.id, asset_id=asset["id"],
        revision=1, mode="link")
    shared.apply_import(owner, project_id=project.id, asset_id=asset["id"],
        revision=1, mode="link", preview_token=preview["preview_token"],
        decisions=[], idempotency_key="link")
    db.session.execute(text("DELETE FROM user WHERE id=:owner"), {"owner": owner})
    db.session.commit()
    assert db.session.execute(text(
        "SELECT COUNT(*) FROM translation_shared_glossary WHERE id=:id"
    ), {"id": asset["id"]}).scalar_one() == 0


def test_locked_local_term_and_language_pair_guard(request):
    application = request.getfixturevalue("fixture_app")
    first, local = _seed()
    owner = first.user_id
    asset = shared.create_asset(owner, project_id=first.id, name="Locked guard",
        concept_ids=[local["concept_id"]], idempotency_key="asset")
    second = _volume(first, "Volume II")
    record, _ = _load_glossary_source_plan(second)
    _concept, _proposal, locked = _lock_with_usage(application, record, "bank", "rzeka", "river", "preferred")
    activate_glossary_revision(locked)
    db.session.commit()
    assert TranslationGlossaryRevision.query.filter_by(project_id=second.id).count() == 2
    assert TranslationGlossaryActive.query.count() == 1
    assert shared.project_state(owner, project_id=second.id)["local_terms"]
    preview = shared.preview_import(owner, project_id=second.id, asset_id=asset["id"],
        revision=1, mode="link")
    row = preview["rows"][0]
    assert row["local"]["authority"] == "human_locked"
    assert row["can_take_incoming"] is False
    with pytest.raises(shared.SharedGlossaryError, match="incoming-forbidden"):
        shared.apply_import(owner, project_id=second.id, asset_id=asset["id"],
            revision=1, mode="link", preview_token=preview["preview_token"],
            decisions=[{"id": row["id"], "choice": "incoming"}], idempotency_key="forbidden")
    assert shared.project_state(owner, project_id=second.id)["local_terms"][0]["target_term"] == "rzeka"
    second.target_lang = "de"
    db.session.commit()
    with pytest.raises(shared.SharedGlossaryError, match="language-mismatch"):
        shared.preview_import(owner, project_id=second.id, asset_id=asset["id"],
            revision=1, mode="copy")
