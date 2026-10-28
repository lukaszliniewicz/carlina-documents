from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest
from alembic import command
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_doc_processor_migrations import _migration_config as document_migration_config
from test_translator_migrations import (
    _migrate_phase5_database,
)
from test_translator_migrations import (
    _migration_config as translator_migration_config,
)

from carlina.core.epub_export import epub_value_sha256, normalize_epub_recipe
from carlina.runtime_profiles import ContentPolicy, RuntimeProfile
from iarf_assistant.addons.doc_processor import epub_editions as document_store
from iarf_assistant.addons.doc_processor.models import (
    DocumentEpubEdition,
    DocumentMetadata,
    ProcessedDocument,
)
from iarf_assistant.addons.translator import epub_editions as translator_store
from iarf_assistant.addons.translator.block_workspace_api import (
    BlockWorkspaceIntegrityError,
)
from iarf_assistant.addons.translator.models import (
    TranslationEpubEdition,
    TranslationProject,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.models import User, db

RECIPES = (
    {
        "epub_options": {"decorative_images": ["img-cover"]},
        "epub_cover": {},
        "epub_image_mode": "auto",
    },
    {
        "epub_options": {"decorative_images": ["img-cover"]},
        "epub_cover": {"mode": "generated"},
        "epub_image_mode": "auto",
    },
)
REVISION = "a" * 64


@pytest.fixture(
    params=[ContentPolicy.PLAINTEXT_V1, ContentPolicy.INSTALLATION_ENVELOPE_V2]
)
def store_app(tmp_path: Path, request):
    policy = request.param
    app = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(
        app,
        profile=RuntimeProfile.DOCUMENTS,
        content_policy=policy,
        legacy=False,
    )
    database_path = tmp_path / "editions.db"
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 5}},
        SECRET_KEY="epub-edition-storage-test",
        TESTING=True,
    )
    db.init_app(app)
    if policy is ContentPolicy.INSTALLATION_ENVELOPE_V2:
        keys = InstallationKeyStore(tmp_path / "keys.json")
        keys.initialize()
        app.extensions["carlina_encryption"] = EncryptionService(keys)

    context = app.app_context()
    context.push()
    for model in (
        User,
        ProcessedDocument,
        DocumentMetadata,
        DocumentEpubEdition,
        TranslationProject,
        TranslationEpubEdition,
    ):
        model.__table__.create(db.engine, checkfirst=True)
    with db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    db.session.add_all(
        [
            User(id=1, email="owner@example.test", password_hash="x"),
            User(id=2, email="other@example.test", password_hash="x"),
            ProcessedDocument(
                id="document-1",
                user_id=1,
                filename="synthetic.html",
                file_path="synthetic-upload",
                status="completed",
                settings={"document_metadata": {"title": "Synthetic title"}},
                output_html="<main>PRIVATE SOURCE MARKER</main>",
            ),
            TranslationProject(
                id="project-1",
                user_id=1,
                name="Synthetic translation",
                source_lang="en",
                target_lang="pl",
                status="completed",
                engine_contract="document-block-v1",
                settings={"export_metadata": {"title": "Synthetic translation"}},
            ),
        ]
    )
    db.session.commit()
    try:
        yield app, policy
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


@pytest.mark.parametrize("mode", ["jpeg", "webp"])
@pytest.mark.parametrize(
    ("store", "key", "resource_id"),
    [
        (document_store, "document_id", "document-1"),
        (translator_store, "project_id", "project-1"),
    ],
)
def test_saved_image_mode_survives_restart_and_stale_projection(
    store_app, monkeypatch, mode, store, key, resource_id
):
    current = [REVISION]
    monkeypatch.setattr(store, "current_source_revision", lambda _parent: current[0])
    scope = {"owner_id": 1, key: resource_id}
    recipe = {**RECIPES[0], "epub_image_mode": mode}
    saved = store.save_epub_edition(
        **scope, recipe=recipe, expected_version=0, expected_source_revision=REVISION
    )
    assert saved["recipe"]["epub_image_mode"] == mode
    db.session.remove()
    assert store.get_epub_edition(**scope)["recipe"]["epub_image_mode"] == mode
    current[0] = "b" * 64
    stale = store.get_epub_edition(**scope)
    assert stale["stale"] is True
    assert stale["effective_recipe"]["epub_image_mode"] == mode


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [
        (document_store, "document-1"),
        (translator_store, "project-1"),
    ],
)
def test_recipe_cas_staleness_tombstone_and_owner_scope(
    store, resource_id: str, store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    current = [REVISION]
    hash_calls = []

    def source_revision(parent):
        hash_calls.append(parent.id)
        return current[0]

    monkeypatch.setattr(store, "current_source_revision", source_revision)
    document_before = db.session.get(ProcessedDocument, "document-1")
    document_state = (
        document_before.output_html,
        dict(document_before.settings),
        document_store.get_canonical_document_metadata(document_before),
    )
    project_before = db.session.get(TranslationProject, "project-1")
    project_state = (
        project_before.document_id,
        project_before.source_snapshot,
        project_before.source_content_hash,
        dict(project_before.settings),
    )
    get_args = {"owner_id": 1, key: resource_id}
    with pytest.raises(store.EpubEditionStoreError) as denied:
        store.get_epub_edition(owner_id=2, **{key: resource_id})
    assert denied.value.code == "epub-edition-not-found"
    assert hash_calls == []

    missing = store.get_epub_edition(**get_args)
    assert missing["version"] == 0 and missing["recipe"] is None
    cleared_missing = store.clear_epub_edition(
        owner_id=1, **{key: resource_id}, expected_version=0
    )
    assert cleared_missing["version"] == 0

    for invalid in (-1, True, 1.5):
        with pytest.raises(store.EpubEditionStoreError) as invalid_version:
            store.save_epub_edition(
                owner_id=1,
                **{key: resource_id},
                recipe=RECIPES[0],
                expected_version=invalid,
                expected_source_revision=REVISION,
            )
        assert invalid_version.value.code == "epub-edition-contract"

    with pytest.raises(store.EpubEditionStoreError) as invalid_revision:
        store.save_epub_edition(
            owner_id=1,
            **{key: resource_id},
            recipe=RECIPES[0],
            expected_version=0,
            expected_source_revision="bad",
        )
    assert invalid_revision.value.code == "epub-edition-contract"
    with pytest.raises(store.EpubEditionStoreError) as malformed:
        store.save_epub_edition(
            owner_id=1,
            **{key: resource_id},
            recipe={"unbounded": True},
            expected_version=0,
            expected_source_revision=REVISION,
        )
    assert malformed.value.code == "epub-edition-contract"
    with pytest.raises(store.EpubEditionStoreError) as unsupported_mode:
        store.save_epub_edition(
            owner_id=1,
            **{key: resource_id},
            recipe={**RECIPES[0], "epub_image_mode": "unknown"},
            expected_version=0,
            expected_source_revision=REVISION,
        )
    assert unsupported_mode.value.code == "epub-edition-contract"

    saved = store.save_epub_edition(
        owner_id=1,
        **{key: resource_id},
        recipe=RECIPES[0],
        expected_version=0,
        expected_source_revision=REVISION,
    )
    assert saved["version"] == 1 and saved["recipe"]["epub_image_mode"] == "auto"
    unchanged = store.save_epub_edition(
        owner_id=1,
        **{key: resource_id},
        recipe=RECIPES[0],
        expected_version=1,
        expected_source_revision=REVISION,
    )
    assert unchanged["version"] == 1

    db.session.remove()
    restarted = store.get_epub_edition(**get_args)
    assert restarted["recipe"] == saved["recipe"]
    assert restarted["source_revision"] == REVISION

    current[0] = "b" * 64
    with pytest.raises(store.EpubEditionStoreError) as stale_write:
        store.save_epub_edition(
            owner_id=1,
            **{key: resource_id},
            recipe=RECIPES[1],
            expected_version=1,
            expected_source_revision=REVISION,
        )
    assert stale_write.value.code == "epub-edition-source-stale"
    stale_read = store.get_epub_edition(**get_args)
    assert stale_read["stale"] is True
    assert stale_read["effective_recipe"]["epub_options"]["decorative_images"] == []

    tombstone = store.clear_epub_edition(
        owner_id=1, **{key: resource_id}, expected_version=1
    )
    assert tombstone["version"] == 2
    assert tombstone["recipe"] is None and tombstone["source_revision"] is None
    recreated = store.save_epub_edition(
        owner_id=1,
        **{key: resource_id},
        recipe=RECIPES[1],
        expected_version=2,
        expected_source_revision=current[0],
    )
    assert recreated["version"] == 3
    assert recreated["recipe"] == normalize_epub_recipe(RECIPES[1])

    if store is document_store:
        document = db.session.get(ProcessedDocument, resource_id)
        assert document.output_html == "<main>PRIVATE SOURCE MARKER</main>"
        assert document.settings == {"document_metadata": {"title": "Synthetic title"}}
        assert (
            document.output_html,
            dict(document.settings),
            document_store.get_canonical_document_metadata(document),
        ) == document_state
    else:
        project = db.session.get(TranslationProject, resource_id)
        assert project.settings == {
            "export_metadata": {"title": "Synthetic translation"}
        }
        assert (
            project.document_id,
            project.source_snapshot,
            project.source_content_hash,
            dict(project.settings),
        ) == project_state


def test_direct_source_revision_uses_html_and_canonical_metadata(store_app) -> None:
    _app, _policy = store_app
    document = db.session.get(ProcessedDocument, "document-1")
    expected = epub_value_sha256(
        {
            "html_sha256": hashlib.sha256(
                document.output_html.encode("utf-8")
            ).hexdigest(),
            "metadata": {"title": "Synthetic title"},
        }
    )
    assert document_store.current_source_revision(document) == expected


def test_translator_source_revision_binds_active_plan_targets_and_settings(
    store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    project = db.session.get(TranslationProject, "project-1")
    record = SimpleNamespace(id="source-plan", plan_hash="c" * 64)

    class FakeQuery:
        block_id = "block_id"

        def filter_by(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    block_id="block-1",
                    revision_id="revision-1",
                    version=3,
                    pair_receipt="d" * 64,
                )
            ]

        monkeypatch.setattr(
            translator_store,
            "load_block_source_plan",
            lambda _project: SimpleNamespace(record=record, active_version=7),
        )

    monkeypatch.setattr(
        translator_store,
        "TranslationTargetBlockActive",
        SimpleNamespace(
            query=FakeQuery(),
            block_id=FakeQuery.block_id,
        ),
    )
    from iarf_assistant.addons.translator import numbering_sync

    monkeypatch.setattr(numbering_sync, "numbering_revision", lambda _project_id: 5)
    expected = epub_value_sha256(
        {
            "source_plan_id": "source-plan",
            "source_plan_hash": "c" * 64,
            "active_plan_version": 7,
            "targets": [
                {
                    "block_id": "block-1",
                    "revision_id": "revision-1",
                    "version": 3,
                    "pair_receipt": "d" * 64,
                }
            ],
            "numbering_revision": 5,
            "metadata": {"title": "Synthetic translation"},
            "target_lang": "pl",
        }
    )
    assert translator_store.current_source_revision(project) == expected


def test_translator_source_plan_integrity_failure_maps_to_not_ready(
    store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    project = db.session.get(TranslationProject, "project-1")

    def unavailable(_project):
        raise BlockWorkspaceIntegrityError("source-plan-active-missing")

    monkeypatch.setattr(translator_store, "load_block_source_plan", unavailable)
    with pytest.raises(translator_store.EpubEditionStoreError) as failure:
        translator_store.current_source_revision(project)
    assert failure.value.code == "epub-edition-not-ready"


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [(document_store, "document-1"), (translator_store, "project-1")],
)
@pytest.mark.parametrize("other_session_action", ["update", "clear"])
def test_noop_save_conflicts_after_identity_map_becomes_stale(
    store, resource_id: str, other_session_action: str, store_app, monkeypatch
) -> None:
    app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    edition_model = (
        DocumentEpubEdition if store is document_store else TranslationEpubEdition
    )
    get_args = {"owner_id": 1, key: resource_id}
    monkeypatch.setattr(store, "current_source_revision", lambda _parent: REVISION)
    store.save_epub_edition(
        **get_args,
        recipe=RECIPES[0],
        expected_version=0,
        expected_source_revision=REVISION,
    )
    stale_row = db.session.get(edition_model, resource_id)
    assert stale_row.version == 1
    assert stale_row.recipe == normalize_epub_recipe(RECIPES[0])

    with app.app_context():
        if other_session_action == "update":
            changed = store.save_epub_edition(
                **get_args,
                recipe=RECIPES[1],
                expected_version=1,
                expected_source_revision=REVISION,
            )
            assert changed["version"] == 2
        else:
            cleared = store.clear_epub_edition(
                owner_id=1,
                **{key: resource_id},
                expected_version=1,
            )
            assert cleared["version"] == 2

    # The first session still has the old equal-recipe row in its identity map.
    assert stale_row.version == 1
    with pytest.raises(store.EpubEditionStoreError) as conflict:
        store.save_epub_edition(
            **get_args,
            recipe=RECIPES[0],
            expected_version=1,
            expected_source_revision=REVISION,
        )
    assert conflict.value.code == "epub-edition-conflict"
    fresh = store.get_epub_edition(**get_args)
    assert fresh["version"] == 2
    if other_session_action == "clear":
        assert fresh["recipe"] is None
    else:
        assert fresh["recipe"] == normalize_epub_recipe(RECIPES[1])


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [(document_store, "document-1"), (translator_store, "project-1")],
)
def test_independent_sessions_have_one_cas_winner(
    store, resource_id: str, store_app, monkeypatch
) -> None:
    app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    get_args = {"owner_id": 1, key: resource_id}
    monkeypatch.setattr(store, "current_source_revision", lambda _parent: REVISION)
    store.save_epub_edition(
        **get_args,
        recipe=RECIPES[0],
        expected_version=0,
        expected_source_revision=REVISION,
    )
    read_barrier = threading.Barrier(2)
    winner_release = threading.Event()
    loser_release = threading.Event()
    results: Queue[tuple[str, object]] = Queue()

    def writer(recipe, release):
        with app.app_context():
            try:
                snapshot = store.get_epub_edition(**get_args)
                results.put(("observed", snapshot["version"]))
                read_barrier.wait(timeout=5)
                if not release.wait(timeout=5):
                    raise TimeoutError("CAS writer release timed out")
                result = store.save_epub_edition(
                    **get_args,
                    recipe=recipe,
                    expected_version=1,
                    expected_source_revision=REVISION,
                )
                results.put(("saved", result))
            except store.EpubEditionStoreError as exc:
                results.put((exc.code, exc))
            finally:
                db.session.remove()

    threads = [
        threading.Thread(target=writer, args=(RECIPES[1], winner_release)),
        threading.Thread(target=writer, args=(RECIPES[0], loser_release)),
    ]
    for thread in threads:
        thread.start()
    observed = [results.get(timeout=5) for _ in threads]
    assert all(result == ("observed", 1) for result in observed)
    winner_release.set()
    winner_result = results.get(timeout=5)
    assert winner_result[0] == "saved"
    loser_release.set()
    loser_result = results.get(timeout=5)
    assert loser_result[0] == "epub-edition-conflict"
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [(document_store, "document-1"), (translator_store, "project-1")],
)
def test_interrupted_commit_rolls_back_recipe(
    store, resource_id: str, store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    monkeypatch.setattr(store, "current_source_revision", lambda _parent: REVISION)
    session = db.session()

    def interrupt(_session):
        raise RuntimeError("synthetic commit interruption")

    event.listen(session, "before_commit", interrupt)
    try:
        with pytest.raises(RuntimeError, match="synthetic commit interruption"):
            store.save_epub_edition(
                owner_id=1,
                **{key: resource_id},
                recipe=RECIPES[0],
                expected_version=0,
                expected_source_revision=REVISION,
            )
    finally:
        event.remove(session, "before_commit", interrupt)
    db.session.remove()
    with db.engine.connect() as connection:
        table = (
            "document_epub_edition"
            if store is document_store
            else "translation_epub_edition"
        )
        assert (
            connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
        )


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [(document_store, "document-1"), (translator_store, "project-1")],
)
def test_unavailable_source_keeps_recipe_readable_and_clearable(
    store, resource_id: str, store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    get_args = {"owner_id": 1, key: resource_id}
    monkeypatch.setattr(store, "current_source_revision", lambda _parent: REVISION)
    saved = store.save_epub_edition(
        **get_args,
        recipe=RECIPES[0],
        expected_version=0,
        expected_source_revision=REVISION,
    )

    def unavailable(_parent):
        raise store.EpubEditionStoreError("epub-edition-not-ready")

    monkeypatch.setattr(store, "current_source_revision", unavailable)
    loaded = store.get_epub_edition(**get_args)
    assert loaded["version"] == saved["version"]
    assert loaded["recipe"] == saved["recipe"]
    assert loaded["current_source_revision"] is None
    assert loaded["stale"] is True
    assert loaded["effective_recipe"]["epub_options"]["decorative_images"] == []

    cleared = store.clear_epub_edition(**get_args, expected_version=saved["version"])
    assert cleared["version"] == saved["version"] + 1
    assert cleared["recipe"] is None
    assert cleared["source_revision"] is None
    assert cleared["current_source_revision"] is None
    assert cleared["stale"] is False


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [(document_store, "document-1"), (translator_store, "project-1")],
)
@pytest.mark.parametrize("operation", ["get", "clear"])
def test_owner_denial_precedes_unavailable_source_lookup(
    store, resource_id: str, operation: str, store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    calls: list[object] = []

    def unavailable(parent):
        calls.append(parent)
        raise store.EpubEditionStoreError("epub-edition-not-ready")

    monkeypatch.setattr(store, "current_source_revision", unavailable)
    with pytest.raises(store.EpubEditionStoreError) as denied:
        if operation == "get":
            store.get_epub_edition(owner_id=2, **{key: resource_id})
        else:
            store.clear_epub_edition(
                owner_id=2, **{key: resource_id}, expected_version=0
            )
    assert denied.value.code == "epub-edition-not-found"
    assert calls == []


@pytest.mark.parametrize(
    ("store", "resource_id", "parent_table", "source_column", "edition_table"),
    [
        (
            document_store,
            "document-1",
            "processed_document",
            "filename",
            "document_epub_edition",
        ),
        (
            translator_store,
            "project-1",
            "translation_project",
            "name",
            "translation_epub_edition",
        ),
    ],
)
@pytest.mark.parametrize("action", ["insert", "change", "noop"])
def test_save_revalidates_source_after_independent_commit_before_edition_write(
    store,
    resource_id: str,
    parent_table: str,
    source_column: str,
    edition_table: str,
    action: str,
    store_app,
    monkeypatch,
) -> None:
    _app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    get_args = {"owner_id": 1, key: resource_id}

    def dynamic_source_revision(parent):
        marker = parent.filename if store is document_store else parent.name
        return epub_value_sha256({"synthetic_source_marker": marker})

    monkeypatch.setattr(store, "current_source_revision", dynamic_source_revision)
    expected_source = dynamic_source_revision(
        db.session.get(
            ProcessedDocument if store is document_store else TranslationProject,
            resource_id,
        )
    )
    if action != "insert":
        initial = store.save_epub_edition(
            **get_args,
            recipe=RECIPES[0],
            expected_version=0,
            expected_source_revision=expected_source,
        )
        assert initial["version"] == 1

    write_prefix = f"{edition_table}"
    source_changed = False

    def commit_source_change_before_edition_write(
        connection, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal source_changed
        normalized = statement.lower()
        is_edition_write = (
            "insert into" in normalized or "update" in normalized
        ) and write_prefix in normalized
        if source_changed or not is_edition_write:
            return
        independent = Session(bind=connection.engine)
        try:
            independent.execute(
                text(
                    f"UPDATE {parent_table} SET {source_column} = :marker WHERE id = :id"
                ),
                {"marker": f"concurrent-source-{action}", "id": resource_id},
            )
            independent.commit()
            source_changed = True
        except Exception:
            independent.rollback()
            raise
        finally:
            independent.close()

    event.listen(
        db.engine, "before_cursor_execute", commit_source_change_before_edition_write
    )
    try:
        with pytest.raises(store.EpubEditionStoreError) as stale:
            store.save_epub_edition(
                **get_args,
                recipe=RECIPES[1] if action == "change" else RECIPES[0],
                expected_version=0 if action == "insert" else 1,
                expected_source_revision=expected_source,
            )
    finally:
        event.remove(
            db.engine,
            "before_cursor_execute",
            commit_source_change_before_edition_write,
        )

    assert source_changed is True
    assert stale.value.code == "epub-edition-source-stale"
    refreshed = store.get_epub_edition(**get_args)
    assert refreshed["current_source_revision"] != expected_source
    if action == "insert":
        assert refreshed["version"] == 0
        assert refreshed["recipe"] is None
    else:
        assert refreshed["version"] == 1
        assert refreshed["source_revision"] == expected_source
        assert refreshed["recipe"] == normalize_epub_recipe(RECIPES[0])


@pytest.mark.parametrize(
    ("store", "resource_id"),
    [(document_store, "document-1"), (translator_store, "project-1")],
)
def test_save_keeps_operational_source_sql_errors_not_ready(
    store, resource_id: str, store_app, monkeypatch
) -> None:
    _app, _policy = store_app
    key = "document_id" if store is document_store else "project_id"
    calls = 0

    def source_revision(_parent):
        nonlocal calls
        calls += 1
        if calls == 1:
            return REVISION
        try:
            raise SQLAlchemyError("synthetic source SQL failure")
        except SQLAlchemyError as exc:
            raise store.EpubEditionStoreError("epub-edition-not-ready") from exc

    monkeypatch.setattr(store, "current_source_revision", source_revision)
    with pytest.raises(store.EpubEditionStoreError) as failure:
        store.save_epub_edition(
            owner_id=1,
            **{key: resource_id},
            recipe=RECIPES[0],
            expected_version=0,
            expected_source_revision=REVISION,
        )
    assert failure.value.code == "epub-edition-not-ready"
    assert calls == 2
    edition_model = (
        DocumentEpubEdition if store is document_store else TranslationEpubEdition
    )
    assert db.session.get(edition_model, resource_id) is None


def test_encrypted_recipe_is_not_plaintext(store_app) -> None:
    _app, policy = store_app
    if policy is not ContentPolicy.INSTALLATION_ENVELOPE_V2:
        pytest.skip("requires the isolated installation-encrypted root")
    source_revision = document_store.current_source_revision(
        db.session.get(ProcessedDocument, "document-1")
    )
    document_store.save_epub_edition(
        owner_id=1,
        document_id="document-1",
        recipe=RECIPES[0],
        expected_version=0,
        expected_source_revision=source_revision,
    )
    with db.engine.connect() as connection:
        raw_recipe = connection.execute(
            text(
                "SELECT recipe FROM document_epub_edition WHERE document_id='document-1'"
            )
        ).scalar_one()
        raw_html = connection.execute(
            text("SELECT output_html FROM processed_document WHERE id='document-1'")
        ).scalar_one()
    assert raw_recipe.startswith("enc:v2:")
    assert "PRIVATE SOURCE MARKER" not in raw_recipe
    assert "PRIVATE SOURCE MARKER" not in raw_html
    assert document_store.get_epub_edition(owner_id=1, document_id="document-1")[
        "recipe"
    ]


def _assert_additive_edition_schema(
    database_path: Path, table: str, resource_column: str, parent_table: str
) -> None:
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: {"type": row[2], "primary_key": row[5]}
            for row in connection.execute(f"PRAGMA table_info('{table}')")
        }
        assert columns[resource_column] == {"type": "VARCHAR(36)", "primary_key": 1}
        assert columns["recipe"]["type"] == "TEXT"
        assert columns["source_revision"]["type"] == "VARCHAR(64)"
        assert columns["updated_at"]["type"] == "DATETIME"
        foreign_keys = {
            row[3]: (row[2], row[6])
            for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
        }
        assert foreign_keys == {
            resource_column: (parent_table, "CASCADE"),
            "owner_user_id": ("user", "CASCADE"),
        }
        indexes = {
            row[1] for row in connection.execute(f"PRAGMA index_list('{table}')")
        }
        assert f"ix_{table}_owner_user_id" in indexes
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        assert "version >= 1" in create_sql
        assert "source_revision IS NULL OR length(source_revision) = 64" in create_sql


def test_epub_edition_migrations_upgrade_retry_and_downgrade(tmp_path: Path) -> None:
    document_db = tmp_path / "document-migrations.db"
    document_config = document_migration_config(document_db)
    command.upgrade(document_config, "head")
    command.upgrade(document_config, "head")
    with sqlite3.connect(document_db) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version_doc_processor"
        ).fetchone() == ("006_epub_editions",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='document_epub_edition'"
        ).fetchone() == ("document_epub_edition",)
    _assert_additive_edition_schema(
        document_db,
        "document_epub_edition",
        "document_id",
        "processed_document",
    )
    command.downgrade(document_config, "005_extraction_issue_ledger")
    with sqlite3.connect(document_db) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='document_epub_edition'"
            ).fetchone()
            is None
        )
    command.upgrade(document_config, "head")

    translator_db = tmp_path / "translator-migrations.db"
    translator_config = translator_migration_config(translator_db)
    app = _migrate_phase5_database(
        translator_db,
        tmp_path / "translator-instance",
        revision="028_review_coverage",
    )
    with app.app_context():
        command.upgrade(translator_config, "head")
        command.upgrade(translator_config, "head")
        with sqlite3.connect(translator_db) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone() == ("029_epub_editions",)
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE name='translation_epub_edition'"
            ).fetchone() == ("translation_epub_edition",)
        _assert_additive_edition_schema(
            translator_db,
            "translation_epub_edition",
            "project_id",
            "translation_project",
        )
        command.downgrade(translator_config, "028_review_coverage")
        with sqlite3.connect(translator_db) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='translation_epub_edition'"
                ).fetchone()
                is None
            )
        command.upgrade(translator_config, "head")
        db.engine.dispose()
