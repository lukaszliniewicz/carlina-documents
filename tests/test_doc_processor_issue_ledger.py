from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from iarf_assistant.addons.doc_processor.extraction_issues import (
    IssueReportValidationError,
    issue_reports_schema,
    validate_issue_reports,
)
from iarf_assistant.addons.doc_processor.issue_ledger import (
    IssueLedgerService,
    import_extraction_issues,
)
from iarf_assistant.addons.doc_processor.mcp_review import _revision
from iarf_assistant.addons.doc_processor.mcp_service import (
    DocProcessorMcpService,
    DocProcessorMcpServiceError,
)
from iarf_assistant.addons.doc_processor.models import (
    DocProcessorIssue,
    ProcessedDocument,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


def _report(
    *,
    key: str = "uncertain-name",
    excerpt: str = "EVIDENCE-SECRET: Jón saw the mark.",
    kind: str = "name_check",
) -> dict:
    return {
        "key": key,
        "kind": kind,
        "excerpt": excerpt,
        "reason": "The printed name is unclear.",
        "suggested_readings": ["Jon", "Jón"],
        "references": [
            {
                "url": "https://example.test/catalog?id=7",
                "title": "Catalog record",
                "note": "Compare the handwritten entry.",
            }
        ],
    }


def _service(app: Flask, owner: int = 1) -> IssueLedgerService:
    return IssueLedgerService(
        DocProcessorMcpService(SimpleNamespace(application=app), owner)
    )


@pytest.fixture
def issue_app(tmp_path):
    app = Flask(__name__)
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'issues.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
        SECRET_KEY="test",
    )
    db.init_app(app)
    app.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=user_id, email=f"issues{user_id}@example.test", password_hash="x")
                for user_id in (1, 2)
            ]
        )
        db.session.add(
            ProcessedDocument(
                id="doc",
                user_id=1,
                filename="synthetic.pdf",
                file_path="/tmp/synthetic.pdf",
                status="completed",
                output_html="<main><p>Synthetic source text.</p></main>",
            )
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_report_validation_is_closed_bounded_unicode_and_source_free():
    from jsonschema import Draft202012Validator

    report = _report(excerpt="Jón saw the mark.\r\nNext line.")
    normalized = validate_issue_reports([report])
    assert normalized[0]["excerpt"] == "Jón saw the mark.\nNext line."
    schema = issue_reports_schema()
    assert schema["type"] == "array"
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid([report])
    assert schema["items"]["properties"]["references"]["type"] == "array"
    assert set(schema["items"]["required"]) == {
        "key",
        "kind",
        "excerpt",
        "reason",
        "suggested_readings",
        "references",
    }
    assert schema["items"]["additionalProperties"] is False

    invalid_values = [
        [{**report, "unexpected": "field"}],
        [report, report],
        [{**report, "key": "9bad"}],
        [{**report, "kind": "arbitrary"}],
        [{**report, "reason": " \n "}],
        [{**report, "excerpt": "forbidden\tcontrol"}],
        [{**report, "references": [{"url": "javascript:alert(1)", "title": "x", "note": "y"}]}],
        [
            {
                **report,
                "references": [
                    {
                        "url": "https://example.test/\npath",
                        "title": "x",
                        "note": "y",
                    }
                ],
            }
        ],
    ]
    for value in invalid_values:
        with pytest.raises(IssueReportValidationError) as error:
            validate_issue_reports(value)
        assert "EVIDENCE-SECRET" not in str(error.value)


def test_report_validation_enforces_count_field_and_total_byte_limits():
    with pytest.raises(IssueReportValidationError) as count_error:
        validate_issue_reports([_report(key=f"i{index}") for index in range(21)])
    assert count_error.value.code == "issue_reports_limit"

    with pytest.raises(IssueReportValidationError) as field_error:
        validate_issue_reports([_report(excerpt="x" * 2001)])
    assert field_error.value.code == "issue_report_limit"

    with pytest.raises(IssueReportValidationError) as byte_error:
        validate_issue_reports(
            [_report(key=f"i{index}", excerpt="é" * 1900) for index in range(20)]
        )
    assert byte_error.value.code == "issue_reports_too_large"


def test_extraction_import_is_idempotent_and_does_not_commit(issue_app):
    with issue_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        revision = _revision(document.output_html)
        pages = [{"page_number": 4, "issues": [_report()]}]
        created = import_extraction_issues(document, pages, revision)
        assert len(created) == 1
        assert created[0]["origin"] == "extraction"
        assert created[0]["page_number"] == 4
        assert db.session().in_transaction()
        db.session.rollback()
        assert db.session.query(DocProcessorIssue).count() == 0

        import_extraction_issues(document, pages, revision)
        db.session.commit()
        issue = db.session.query(DocProcessorIssue).one()
        issue.status = "confirmed"
        issue.version = 2
        db.session.commit()

        replay = import_extraction_issues(document, pages, revision)
        assert replay[0]["status"] == "confirmed"
        assert replay[0]["version"] == 2
        db.session.commit()
        assert db.session.query(DocProcessorIssue).count() == 1


def test_manual_report_update_owner_idempotency_and_ciphertext(issue_app):
    with issue_app.app_context():
        api = _service(issue_app)
        document = db.session.get(ProcessedDocument, "doc")
        revision = _revision(document.output_html)
        created = api.report(
            "doc",
            expected_revision=revision,
            idempotency_key="manual-report-1",
            page_number=2,
            report=_report(),
        )
        issue_id = created["id"]
        assert created["origin"] == "manual"
        assert created["status"] == "open"
        with pytest.raises(DocProcessorMcpServiceError) as denied:
            _service(issue_app, 2).get("doc", issue_id)
        assert denied.value.code == "not_found"

        raw_payload = db.session.execute(
            text("SELECT payload FROM doc_processor_issue WHERE id = :id"),
            {"id": issue_id},
        ).scalar_one()
        assert "EVIDENCE-SECRET" not in raw_payload
        assert db.session.get(DocProcessorIssue, issue_id).payload["report"] == _report()

        changed = api.update(
            "doc",
            issue_id,
            expected_revision=revision,
            expected_version=1,
            idempotency_key="status-confirm-1",
            status="confirmed",
            note="The name matches the catalog.",
        )
        assert changed["version"] == 2
        assert changed["history"] == [
            {
                "status": "confirmed",
                "note": "The name matches the catalog.",
                "timestamp": changed["history"][0]["timestamp"],
            }
        ]

        replay = api.update(
            "doc",
            issue_id,
            expected_revision=revision,
            expected_version=1,
            idempotency_key="status-confirm-1",
            status="confirmed",
            note="The name matches the catalog.",
        )
        assert replay["version"] == 2
        assert len(replay["history"]) == 1

        with pytest.raises(DocProcessorMcpServiceError) as conflict:
            api.update(
                "doc",
                issue_id,
                expected_revision=revision,
                expected_version=1,
                idempotency_key="status-confirm-1",
                status="resolved",
                note="Different request.",
            )
        assert conflict.value.code == "idempotency_conflict"

        listed = api.list("doc", status="confirmed", kind="name_check")
        assert listed["total"] == 1
        assert listed["counts"]["confirmed"] == 1
        assert listed["items"][0]["id"] == issue_id


def test_extraction_generation_allows_same_revision_new_evidence(issue_app):
    with issue_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        revision = _revision(document.output_html)
        page = {"page_number": 4, "issues": [_report()]}
        document.settings = {"mcp_execution_id": "generation-one"}
        db.session.commit()

        first = import_extraction_issues(document, [page], revision)
        db.session.commit()
        assert first[0]["source_revision"] == revision

        changed_same_generation = {
            **_report(),
            "reason": "The second reading came from the same extraction run.",
        }
        with pytest.raises(DocProcessorMcpServiceError) as repeated_source:
            import_extraction_issues(
                document,
                [{"page_number": 4, "issues": [changed_same_generation]}],
                revision,
            )
        assert repeated_source.value.code == "idempotency_conflict"
        db.session.rollback()

        document = db.session.get(ProcessedDocument, "doc")
        document.settings = {"mcp_execution_id": "generation-two"}
        db.session.commit()
        changed_new_generation = {
            **_report(),
            "reason": "A new extraction run produced different evidence.",
        }
        second = import_extraction_issues(
            document,
            [{"page_number": 4, "issues": [changed_new_generation]}],
            revision,
        )
        db.session.commit()
        assert second[0]["id"] != first[0]["id"]
        assert second[0]["source_revision"] == first[0]["source_revision"]
        assert db.session.query(DocProcessorIssue).count() == 2


def test_revision_version_and_history_fences(issue_app):
    with issue_app.app_context():
        api = _service(issue_app)
        document = db.session.get(ProcessedDocument, "doc")
        revision = _revision(document.output_html)
        issue = api.report(
            "doc",
            expected_revision=revision,
            idempotency_key="manual-report-fences",
            page_number=None,
            report=_report(),
        )
        with pytest.raises(DocProcessorMcpServiceError) as stale_version:
            api.update(
                "doc",
                issue["id"],
                expected_revision=revision,
                expected_version=2,
                idempotency_key="stale-version",
                status="confirmed",
                note="A note.",
            )
        assert stale_version.value.code == "version_conflict"

        document.output_html = "<main><p>Changed source.</p></main>"
        db.session.commit()
        with pytest.raises(DocProcessorMcpServiceError) as stale_revision:
            api.update(
                "doc",
                issue["id"],
                expected_revision=revision,
                expected_version=1,
                idempotency_key="stale-revision",
                status="confirmed",
                note="A note.",
            )
        assert stale_revision.value.code == "revision_conflict"
        assert api.get("doc", issue["id"])["context_stale"] is True


def test_source_compare_and_swap_rejects_an_independent_stale_context(issue_app):
    with issue_app.app_context():
        api_one = _service(issue_app)
        document = db.session.get(ProcessedDocument, "doc")
        revision = _revision(document.output_html)
        issue = api_one.report(
            "doc",
            expected_revision=revision,
            idempotency_key="manual-report-race",
            page_number=1,
            report=_report(),
        )

    barrier = threading.Barrier(2)

    def update_concurrently(idempotency_key: str) -> str:
        with issue_app.app_context():
            api = _service(issue_app)
            original_fence = api._fence_document

            def wait_then_fence(snapshot):
                barrier.wait(timeout=10)
                return original_fence(snapshot)

            api._fence_document = wait_then_fence
            try:
                api.update(
                    "doc",
                    issue["id"],
                    expected_revision=revision,
                    expected_version=1,
                    idempotency_key=idempotency_key,
                    status="confirmed",
                    note=f"Confirmed by {idempotency_key}.",
                )
                return "updated"
            except DocProcessorMcpServiceError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(update_concurrently, ("race-first", "race-second"))
        )
    assert outcomes.count("updated") == 1
    assert len(
        set(outcomes) & {"revision_conflict", "version_conflict"}
    ) == 1


def test_issue_migration_up_downgrade_and_reapply_uses_temp_database(tmp_path):
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    database_path = tmp_path / "migration.db"
    engine = sa.create_engine(f"sqlite:///{database_path}")
    metadata = sa.MetaData()
    sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "processed_document",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
    )
    metadata.create_all(engine)

    migration_config = Config(
        "iarf_assistant/addons/doc_processor/migrations/alembic.ini"
    )
    migration_config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path}"
    )
    try:
        command.stamp(migration_config, "004_doc_processor_safety_indexes")
        command.upgrade(migration_config, "head")
        inspector = sa.inspect(engine)
        assert "doc_processor_issue" in inspector.get_table_names()
        unique_columns = inspector.get_unique_constraints("doc_processor_issue")[0][
            "column_names"
        ]
        assert set(unique_columns) == {"document_id", "source_key"}

        command.downgrade(migration_config, "004_doc_processor_safety_indexes")
        assert "doc_processor_issue" not in sa.inspect(engine).get_table_names()
        command.upgrade(migration_config, "head")
        assert "doc_processor_issue" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
