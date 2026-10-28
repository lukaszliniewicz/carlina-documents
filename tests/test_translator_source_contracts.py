import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor.api_client import DocProcessorClient
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.translator import routes
from iarf_assistant.addons.translator.source_providers import (
    resolve_source_provider,
    source_reference,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User


ROOT = Path(__file__).resolve().parents[1]


class SourceContext:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def get_capability_provider(self, requirement_id, user_id, **selection):
        self.calls.append((requirement_id, user_id, selection))
        return SimpleNamespace(
            provider_addon_id="source-addon",
            provider_capability_id="source-provider",
            initialize_client=lambda config: (
                self.calls.append(("initialize", config)),
                self.client,
            )[1],
        )


def test_source_reference_pins_provider_without_exposing_provider_models():
    client = object()
    context = SourceContext(client)
    reference = {
        "capability": "document.processing",
        "provider_addon_id": "source-addon",
        "provider_capability_id": "source-provider",
    }

    resolved = resolve_source_provider(
        context,
        requirement_id="document_processor",
        capability="document.processing",
        user_id=7,
        reference=reference,
    )

    assert resolved.client is client
    assert context.calls[0] == (
        "document_processor",
        7,
        {
            "provider_addon_id": "source-addon",
            "provider_capability_id": "source-provider",
        },
    )
    assert resolved.reference(resource_type="document", resource_id="doc-1") == {
        "version": 1,
        "capability": "document.processing",
        "provider_addon_id": "source-addon",
        "provider_capability_id": "source-provider",
        "resource_type": "document",
        "resource_id": "doc-1",
    }


def test_source_reference_rejects_another_capability():
    settings = {
        "source_reference": {
            "capability": "subtitles.authoring",
            "provider_addon_id": "subtitle_studio",
        }
    }

    assert source_reference(settings, capability="document.processing") == {}


def test_translator_document_catalog_uses_declared_capability():
    app = Flask(__name__)
    bind_content_policy(app)
    source_client = SimpleNamespace(
        get_documents=lambda **kwargs: [
            {
                "id": "doc-1",
                "filename": "Source.docx",
                "status": "completed",
                "semantic_contract_mode": "completed",
            },
            {
                "id": "doc-legacy",
                "filename": "Legacy.docx",
                "status": "completed",
                "semantic_contract_mode": "relaxed_legacy",
            },
        ]
    )
    context = SourceContext(source_client)
    routes.blueprint.addon_context = context

    with app.test_request_context("/addons/translator/api/document_sources"):
        with patch.object(routes, "current_user", SimpleNamespace(id=11)):
            response = routes.get_document_sources.__wrapped__()

    assert response.get_json() == [
        {
            "id": "doc-1",
            "filename": "Source.docx",
            "status": "completed",
            "semantic_contract_mode": "completed",
        }
    ]
    assert context.calls[0][0:2] == ("document_processor", 11)




def test_source_clients_enforce_user_scope_and_return_data_contracts(tmp_path):
    app = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
    )
    db.init_app(app)

    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="owner@example.test", password_hash="test"),
                User(id=2, email="other@example.test", password_hash="test"),
                ProcessedDocument(
                    id="doc-owner",
                    user_id=1,
                    filename="Owner.docx",
                    file_path="/uploads/owner.docx",
                    status="completed",
                    output_html="<p>Private source</p>",
                    settings={
                        "document_metadata": {
                            "language": "English",
                            "title": "Owner source",
                            "private_prompt": "do not expose",
                        }
                    },
                ),
                ProcessedDocument(
                    id="doc-other",
                    user_id=2,
                    filename="Other.docx",
                    file_path="/uploads/other.docx",
                    status="completed",
                ),
            ]
        )
        db.session.commit()

        documents = DocProcessorClient(None, {"current_user_id": 1})
        listed = documents.get_documents(status="completed")
        source = documents.get_document("doc-owner", include_content=True)
        denied = documents.get_document("doc-other", include_content=True)
        assert [item["id"] for item in listed] == ["doc-owner"]
        assert "file_path" not in listed[0]
        assert listed[0]["language"] == "en"
        assert listed[0]["metadata"] == {
            "language": "en",
            "title": "Owner source",
        }
        assert "private_prompt" not in listed[0]["metadata"]
        assert "output_html" not in listed[0]
        assert listed[0]["semantic_contract_mode"] == "relaxed_legacy"
        assert source["output_html"] == "<p>Private source</p>"
        assert denied is None
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_translator_declares_optional_source_contracts_without_package_coupling():
    manifest = json.loads(
        (ROOT / "iarf_assistant/addons/translator/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = {
        item["id"]: item for item in manifest["requires"]["capabilities"]
    }

    assert manifest["requires"]["packages"] == []
    assert manifest["dependencies"]["addons"] == []
    assert requirements["document_processor"]["cardinality"] == "optional"
    assert requirements["subtitle_source"]["cardinality"] == "optional"
    assert "read" in requirements["document_processor"]["operations"]
    assert "render" in requirements["subtitle_source"]["operations"]


def test_translator_source_has_no_provider_import_or_private_http_boundary():
    backend = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "iarf_assistant/addons/translator/routes.py",
            "iarf_assistant/addons/translator/worker.py",
        )
    )
    frontend = (
        ROOT
        / "iarf_assistant/addons/translator/frontend/src/projects/ProjectsApp.svelte"
    ).read_text(encoding="utf-8")

    assert "addons.doc_processor" not in backend
    assert "addons.subtitle_studio" not in backend
    assert "/addons/doc_processor/api" not in frontend
