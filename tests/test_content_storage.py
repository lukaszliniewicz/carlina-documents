"""Real SQLite, restart and recovery acceptance for both content policies."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Engine
from sqlalchemy.exc import StatementError

from carlina.content_codec import ContentPolicyError
from carlina.content_storage import content_codec_from_context
from carlina.profile_binding import bind_runtime_profile, profile_marker_path
from carlina.runtime_profiles import ContentPolicy, RuntimeProfile
from iarf_assistant.addons.doc_processor import worker
from iarf_assistant.addons.doc_processor.mcp_review import (
    HISTORY_KEY,
    DocumentReviewError,
    DocumentReviewService,
)
from iarf_assistant.addons.translator.block_workflow_config import (
    _model_transport_identity,
)
from iarf_assistant.addons.translator.models import (
    TranslationAnalysisAttempt,
    TranslationBlockWorkflowConfig,
    TranslationContextArtifactRevision,
    TranslationOperationEvidenceReuse,
    TranslationResearchRun,
)
from iarf_assistant.core.app_paths import AppPaths, resolve_app_paths
from iarf_assistant.core.backup_service import (
    BackupIntegrityError,
    create_backup,
    restore_backup,
    verify_backup,
)
from iarf_assistant.core.encryption import (
    EncryptionIntegrityError,
    KeyUnavailable,
    configure_encryption,
    encryption_service_from_context,
)
from iarf_assistant.database.types import ContentType, EncryptedType
from iarf_assistant.models import ApiKey, LLMProvider

pytestmark = [pytest.mark.integration, pytest.mark.contract]

_TEXT = "<p>Łódź: Καλημέρα. enc:v2:literal and plain:v1:literal.</p>"
_PAYLOAD = {"text": _TEXT, "segments": ["Καλημέρα", "κόσμος"]}
_SECRET = {"token": "synthetic-protected-provider-token"}


def _app(
    root: Path,
    policy: ContentPolicy,
    *,
    initialize: bool = True,
    configure_keys: bool = True,
) -> tuple[Flask, AppPaths]:
    paths = resolve_app_paths(
        root.parent / "program", environment={"CARLINA_HOME": str(root)}
    )
    profile = (
        RuntimeProfile.DOCUMENTS
        if policy is ContentPolicy.PLAINTEXT_V1
        else RuntimeProfile.WORKBENCH
    )
    binding = bind_runtime_profile(root, profile)
    assert binding.content_policy is policy
    paths.ensure_directories()
    application = Flask("storage-acceptance", instance_path=str(paths.instance_dir))
    application.secret_key = "synthetic-independent-session-signing-key"
    application.extensions["carlina_profile_binding"] = binding
    if configure_keys:
        configure_encryption(application, paths, initialize=initialize)
    return application, paths


def _database(paths: AppPaths) -> tuple[Engine, Table]:
    metadata = MetaData()
    table = Table(
        "content_acceptance",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("text", ContentType(sqltypes.Text)),
        Column("payload", ContentType(sqltypes.JSON)),
        Column("secret", EncryptedType(sqltypes.JSON)),
    )
    engine = create_engine("sqlite:///" + str(paths.database_file))
    metadata.create_all(engine)
    return engine, table


def _seed(app: Flask, engine: Engine, table: Table) -> None:
    with app.app_context(), engine.begin() as connection:
        connection.execute(
            table.insert().values(id=1, text=_TEXT, payload=_PAYLOAD, secret=_SECRET)
        )


@pytest.mark.parametrize("policy", list(ContentPolicy))
def test_sqlite_content_and_secrets_survive_restart(
    tmp_path: Path, policy: ContentPolicy
):
    app, paths = _app(tmp_path / policy.value, policy)
    engine, table = _database(paths)
    _seed(app, engine, table)
    with engine.connect() as connection:
        raw = connection.exec_driver_sql(
            "SELECT text, payload, secret FROM content_acceptance"
        ).one()
        expected_prefix = (
            "plain:v1:" if policy is ContentPolicy.PLAINTEXT_V1 else "enc:v2:"
        )
        assert raw[0].startswith(expected_prefix)
        assert raw[1].startswith(expected_prefix)
        assert (_TEXT in raw[0]) is (policy is ContentPolicy.PLAINTEXT_V1)
        assert raw[2].startswith("enc:v2:")
        assert _SECRET["token"] not in raw[2]
    marker_before = profile_marker_path(paths.data_root).read_bytes()
    engine.dispose()

    restarted, restarted_paths = _app(paths.data_root, policy, initialize=False)
    restarted.secret_key = "different-session-key-after-restart"
    assert profile_marker_path(paths.data_root).read_bytes() == marker_before
    reopened, reopened_table = _database(restarted_paths)
    with restarted.app_context(), reopened.connect() as connection:
        restored = connection.execute(select(reopened_table)).one()
        assert (restored.text, restored.payload, restored.secret) == (
            _TEXT,
            _PAYLOAD,
            _SECRET,
        )
    reopened.dispose()


@pytest.mark.parametrize("policy", list(ContentPolicy))
def test_frozen_provider_url_copies_stay_encrypted_after_restart(
    tmp_path: Path, policy: ContentPolicy
):
    app, paths = _app(tmp_path / policy.value, policy)
    endpoint = "https://synthetic-user:synthetic-password@example.invalid?token=fixture"
    routing = _model_transport_identity(
        ApiKey(api_base=endpoint, api_version=None),
        LLMProvider(name="OpenAI", provider_kind="openai_compatible"),
    )
    assert "synthetic-password" in routing["api_base"]
    assert "token=fixture" in routing["api_base"]
    payload = {"model": {"transport_identity": routing}}
    columns = (
        TranslationBlockWorkflowConfig.__table__.c.config_json,
        TranslationAnalysisAttempt.__table__.c.model_json,
        TranslationResearchRun.__table__.c.model_json,
        TranslationContextArtifactRevision.__table__.c.model_json,
        TranslationOperationEvidenceReuse.__table__.c.selection_json,
    )
    # Exercise the actual production column codecs in an independent SQLite table.
    metadata = MetaData()
    table = Table(
        "frozen_credentials_acceptance",
        metadata,
        Column("id", Integer, primary_key=True),
        *(
            Column(f"record_{index}", column.type)
            for index, column in enumerate(columns)
        ),
    )
    engine = create_engine("sqlite:///" + str(paths.database_file))
    metadata.create_all(engine)
    with app.app_context(), engine.begin() as connection:
        connection.execute(
            table.insert().values(
                id=1, **{f"record_{index}": payload for index in range(len(columns))}
            )
        )
    with engine.connect() as connection:
        raw = connection.exec_driver_sql(
            "SELECT * FROM frozen_credentials_acceptance"
        ).one()
        for value in raw[1:]:
            assert value.startswith("enc:v2:")
            assert "synthetic-password" not in value
            assert "token=fixture" not in value
    engine.dispose()
    restarted, _ = _app(paths.data_root, policy, initialize=False)
    engine = create_engine("sqlite:///" + str(paths.database_file))
    with restarted.app_context(), engine.connect() as connection:
        restored = connection.execute(select(table)).one()
        assert all(value == payload for value in restored[1:])
    engine.dispose()


@pytest.mark.parametrize("policy", list(ContentPolicy))
def test_missing_binding_has_no_testing_or_config_fallback(
    tmp_path: Path, policy: ContentPolicy
):
    app, paths = _app(tmp_path / policy.value, policy)
    engine, table = _database(paths)
    app.testing = True
    app.config["CARLINA_CONTENT_POLICY"] = policy.value
    app.extensions.pop("carlina_profile_binding")
    with app.app_context(), engine.begin() as connection:
        with pytest.raises(StatementError, match="policy is not configured"):
            connection.execute(table.insert().values(id=1, text=_TEXT))
        # Protected fields retain their own key service, without a content-policy guess.
        connection.execute(table.insert().values(id=2, secret=_SECRET))
        assert (
            connection.execute(
                select(table.c.secret).where(table.c.id == 2)
            ).scalar_one()
            == _SECRET
        )
    engine.dispose()


@pytest.mark.parametrize("policy", list(ContentPolicy))
def test_data_backup_requires_separate_original_keys_for_full_recovery(
    tmp_path: Path, policy: ContentPolicy
):
    app, paths = _app(tmp_path / "original", policy)
    engine, table = _database(paths)
    _seed(app, engine, table)
    engine.dispose()
    with app.app_context():
        keys = encryption_service_from_context().key_store
    (paths.state_dir / "runtime" / "leader.json").write_text(
        '{"pid":0}', encoding="utf-8"
    )
    backup = create_backup(paths, key_store=keys)
    manifest = verify_backup(backup.path, available_key_ids=keys.key_ids())
    assert "payload/state/runtime/profile.json" in {
        entry["path"] for entry in manifest["files"]
    }
    assert "payload/state/runtime/leader.json" not in {
        entry["path"] for entry in manifest["files"]
    }
    restore_root = tmp_path / "restored"
    restore_backup(backup.path, restore_root, key_store=keys)
    assert not (restore_root / "state" / "runtime" / "leader.json").exists()
    diagnostic, restored_paths = _app(restore_root, policy, configure_keys=False)
    recovered_engine, recovered_table = _database(restored_paths)
    with diagnostic.app_context(), recovered_engine.connect() as connection:
        if policy is ContentPolicy.PLAINTEXT_V1:
            assert (
                connection.execute(select(recovered_table.c.text)).scalar_one() == _TEXT
            )
        else:
            with pytest.raises(KeyUnavailable):
                connection.execute(select(recovered_table.c.text)).scalar_one()
        with pytest.raises(KeyUnavailable):
            connection.execute(select(recovered_table.c.secret)).scalar_one()
        with pytest.raises(StatementError):
            connection.execute(recovered_table.update().values(secret=_SECRET))
    with pytest.raises(KeyUnavailable):
        configure_encryption(diagnostic, restored_paths, initialize=False)
    recovered_engine.dispose()

    # This is explicit key recovery, separately from the data-only backup above.
    shutil.copyfile(paths.master_keys_file, restored_paths.master_keys_file)
    restored_paths.master_keys_file.chmod(0o600)
    recovered_app, _ = _app(restore_root, policy, initialize=False)
    recovered_engine, recovered_table = _database(restored_paths)
    with recovered_app.app_context(), recovered_engine.connect() as connection:
        row = connection.execute(select(recovered_table)).one()
        assert (row.text, row.payload, row.secret) == (_TEXT, _PAYLOAD, _SECRET)
    recovered_engine.dispose()


def test_invalid_profile_archive_refuses_activation_and_retains_destination(
    tmp_path: Path,
):
    app, paths = _app(tmp_path / "source", ContentPolicy.PLAINTEXT_V1)
    engine, table = _database(paths)
    _seed(app, engine, table)
    engine.dispose()
    with app.app_context():
        keys = encryption_service_from_context().key_store
    profile_marker_path(paths.data_root).write_text(
        '{"unsupported":true}', encoding="utf-8"
    )
    backup = create_backup(paths, key_store=keys)
    _destination_app, destination = _app(
        tmp_path / "destination", ContentPolicy.INSTALLATION_ENVELOPE_V2
    )
    retained_marker = profile_marker_path(destination.data_root).read_bytes()
    retained_keys = destination.master_keys_file.read_bytes()
    with pytest.raises(
        BackupIntegrityError, match="Restored runtime profile is invalid"
    ):
        restore_backup(backup.path, destination.data_root, key_store=keys)
    assert profile_marker_path(destination.data_root).read_bytes() == retained_marker
    assert destination.master_keys_file.read_bytes() == retained_keys
    assert not destination.database_file.exists()


@pytest.mark.parametrize("policy", list(ContentPolicy))
def test_other_policy_records_are_rejected(tmp_path: Path, policy: ContentPolicy):
    app, paths = _app(tmp_path / policy.value, policy)
    engine, table = _database(paths)
    with engine.begin() as connection:
        foreign = (
            "enc:v2:foreign"
            if policy is ContentPolicy.PLAINTEXT_V1
            else "plain:v1:foreign"
        )
        connection.exec_driver_sql(
            "INSERT INTO content_acceptance (id, text) VALUES (1, ?)", (foreign,)
        )
    with (
        app.app_context(),
        engine.connect() as connection,
        pytest.raises(ContentPolicyError, match="configured content policy"),
    ):
        connection.execute(select(table.c.text)).scalar_one()
    engine.dispose()


def test_plaintext_json_and_encrypted_authentication_fail_closed(tmp_path: Path):
    plain_app, plain_paths = _app(tmp_path / "plain", ContentPolicy.PLAINTEXT_V1)
    engine, table = _database(plain_paths)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO content_acceptance (id, payload) VALUES (1, ?)",
            ("plain:v1:{invalid",),
        )
    with (
        plain_app.app_context(),
        engine.connect() as connection,
        pytest.raises(ContentPolicyError, match="Stored content JSON is invalid"),
    ):
        connection.execute(select(table.c.payload)).scalar_one()
    engine.dispose()

    encrypted_app, encrypted_paths = _app(
        tmp_path / "encrypted", ContentPolicy.INSTALLATION_ENVELOPE_V2
    )
    engine, table = _database(encrypted_paths)
    _seed(encrypted_app, engine, table)
    with engine.begin() as connection:
        stored = connection.exec_driver_sql(
            "SELECT text FROM content_acceptance"
        ).scalar_one()
        damaged = stored[:-2] + ("A" if stored[-2] != "A" else "B") + stored[-1]
        connection.exec_driver_sql("UPDATE content_acceptance SET text = ?", (damaged,))
    with (
        encrypted_app.app_context(),
        engine.connect() as connection,
        pytest.raises(EncryptionIntegrityError),
    ):
        connection.execute(select(table.c.text)).scalar_one()
    engine.dispose()


@pytest.mark.parametrize("policy", list(ContentPolicy))
def test_checkpoint_and_review_history_use_same_root_codec(
    tmp_path: Path, policy: ContentPolicy
):
    app, _paths = _app(tmp_path / policy.value, policy)
    document = SimpleNamespace(
        id="synthetic-doc",
        user_id=1,
        settings={"mcp_extraction_issues_policy": worker._EXTRACTION_ISSUES_POLICY},
    )
    with app.app_context():
        issues = [
            {
                "key": "reading",
                "kind": "ambiguous_text",
                "excerpt": "Łódź?",
                "reason": "Uncertain source spelling.",
                "suggested_readings": [],
                "references": [],
            }
        ]
        pages = [{"page_number": 1, "issues": issues}]
        encoded = worker._encode_extraction_issues_checkpoint(document, pages)
        assert isinstance(encoded, str)
        prefix = "plain:v1:" if policy is ContentPolicy.PLAINTEXT_V1 else "enc:v2:"
        assert encoded.startswith(prefix)
        document.settings[worker._EXTRACTION_ISSUES_CHECKPOINT_KEY] = encoded
        assert worker._load_extraction_issues_checkpoint(document) == {1: issues}
        entry = {
            "document_id": document.id,
            "owner_user_id": 1,
            "splices": [],
            "change_id": "synthetic-change",
            "undone": False,
        }
        history = DocumentReviewService._encode_history([entry])
        assert history[0].startswith(prefix)
        document.settings[HISTORY_KEY] = history
        assert DocumentReviewService._history(document) == [entry]
        tampered = dict(entry, owner_user_id=2)
        document.settings[HISTORY_KEY] = [
            content_codec_from_context().encode_text(json.dumps(tampered))
        ]
        with pytest.raises(DocumentReviewError, match="history is unavailable"):
            DocumentReviewService._history(document)
