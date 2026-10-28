from __future__ import annotations

from copy import deepcopy

import pytest
from test_translator_target_persistence import (
    _alignment,
    _candidate,
    _plan_for,
    _project,
)
from test_translator_target_persistence import (
    app as target_persistence_app,  # noqa: F401
)

from iarf_assistant.addons.translator import epub_editions as translator_store
from iarf_assistant.addons.translator.models import (
    TranslationProject,
    TranslationSourcePlan,
)
from iarf_assistant.addons.translator.target_persistence import (
    activate_target_revision,
    persist_target_revision,
)
from iarf_assistant.extensions import db

pytestmark = pytest.mark.usefixtures("target_persistence_app")


def _source_plan_snapshot(source_plan_id: str) -> dict[str, object]:
    record = db.session.get(TranslationSourcePlan, source_plan_id)
    assert record is not None
    return {
        column.name: deepcopy(getattr(record, column.name))
        for column in TranslationSourcePlan.__table__.columns
    }


def _project_source_snapshot(project_id: str) -> dict[str, object]:
    project = db.session.get(TranslationProject, project_id)
    assert project is not None
    return {
        "document_id": project.document_id,
        "source_snapshot": deepcopy(project.source_snapshot),
        "source_content_hash": project.source_content_hash,
        "source_captured_at": project.source_captured_at,
        "engine_contract": project.engine_contract,
        "settings": deepcopy(project.settings),
    }


def test_verified_translation_recipe_stales_only_block_reviews() -> None:
    project = _project("epub-edition-verified@example.test")
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    first_revision = persist_target_revision(
        record,
        _candidate(plan, record, block, "First synthetic target."),
        target_language="pl",
        author_type="model",
        verified_plan=plan,
    )
    first_alignment = _alignment(record, first_revision)
    pointer = activate_target_revision(
        record,
        block_id=block.id,
        revision_id=first_revision.id,
        alignment_revision_id=first_alignment.id,
        expected_version=0,
        verified_plan=plan,
    )
    db.session.commit()

    project_id = project.id
    owner_id = project.user_id
    source_plan_id = record.id
    first_revision_id = first_revision.id
    active_target_version = pointer.version
    source_plan_before = _source_plan_snapshot(source_plan_id)
    project_before = _project_source_snapshot(project_id)
    initial_revision = translator_store.current_source_revision(project)
    recipe = {
        "epub_options": {
            "note_placement": "chapter",
            "roles": {block.id: "chapter"},
        },
        "epub_cover": {},
        "epub_image_mode": "auto",
    }
    saved = translator_store.save_epub_edition(
        owner_id=owner_id,
        project_id=project_id,
        recipe=recipe,
        expected_version=0,
        expected_source_revision=initial_revision,
    )
    assert saved["version"] == 1
    assert saved["source_revision"] == initial_revision

    db.session.remove()
    loaded = translator_store.get_epub_edition(
        owner_id=owner_id,
        project_id=project_id,
    )
    assert loaded["recipe"]["epub_options"]["note_placement"] == "chapter"
    assert loaded["recipe"]["epub_options"]["roles"] == {block.id: "chapter"}
    assert _source_plan_snapshot(source_plan_id) == source_plan_before
    assert _project_source_snapshot(project_id) == project_before

    project_row = db.session.get(TranslationProject, project_id)
    source_record = db.session.get(TranslationSourcePlan, source_plan_id)
    assert project_row is not None and source_record is not None
    second_revision = persist_target_revision(
        source_record,
        _candidate(plan, source_record, block, "Second synthetic target."),
        target_language="pl",
        author_type="model",
        parent_revision_id=first_revision_id,
        verified_plan=plan,
    )
    second_alignment = _alignment(source_record, second_revision)
    activate_target_revision(
        source_record,
        block_id=block.id,
        revision_id=second_revision.id,
        alignment_revision_id=second_alignment.id,
        expected_version=active_target_version,
        verified_plan=plan,
    )
    db.session.commit()

    new_revision = translator_store.current_source_revision(project_row)
    assert new_revision != initial_revision
    db.session.remove()
    stale = translator_store.get_epub_edition(
        owner_id=owner_id,
        project_id=project_id,
    )
    assert stale["version"] == 1
    assert stale["stale"] is True
    assert stale["recipe"]["epub_options"]["roles"] == {block.id: "chapter"}
    assert stale["effective_recipe"]["epub_options"]["roles"] == {}
    assert stale["effective_recipe"]["epub_options"]["note_placement"] == "chapter"

    with pytest.raises(translator_store.EpubEditionStoreError) as stale_save:
        translator_store.save_epub_edition(
            owner_id=owner_id,
            project_id=project_id,
            recipe=recipe,
            expected_version=1,
            expected_source_revision=initial_revision,
        )
    assert stale_save.value.code == "epub-edition-source-stale"
    db.session.remove()
    still_stale = translator_store.get_epub_edition(
        owner_id=owner_id,
        project_id=project_id,
    )
    assert still_stale["version"] == 1
    assert still_stale["recipe"]["epub_options"]["note_placement"] == "chapter"

    cleared = translator_store.clear_epub_edition(
        owner_id=owner_id,
        project_id=project_id,
        expected_version=1,
    )
    assert cleared["version"] == 2
    assert cleared["recipe"] is None
    assert cleared["source_revision"] is None
    assert _source_plan_snapshot(source_plan_id) == source_plan_before
    assert _project_source_snapshot(project_id) == project_before
