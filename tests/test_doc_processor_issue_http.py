from __future__ import annotations

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager

from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.routes import blueprint
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def issue_http(tmp_path):
    app = Flask(__name__)
    bind_content_policy(app)
    app.config.update(TESTING=True, SECRET_KEY="synthetic", SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'issues.db'}")
    db.init_app(app)
    login = LoginManager(app)
    login.user_loader(lambda identity: db.session.get(User, int(identity)))
    app.extensions["carlina_encryption"] = EncryptionService(InstallationKeyStore.ephemeral())
    app.register_blueprint(blueprint)
    with app.app_context():
        db.create_all()
        db.session.add_all([User(id=i, email=f"issue{i}@example.test", password_hash="x") for i in (1, 2)])
        db.session.add(ProcessedDocument(id="doc", user_id=1, filename="synthetic.pdf", file_path="synthetic.pdf", status="completed", output_html="<main><p>Łucja</p></main>"))
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True
    yield app, client
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_issue_http_review_conflict_and_owner_isolation(issue_http):
    app, client = issue_http
    url = "/addons/doc_processor/api/documents/doc/issues"
    listing = client.get(url)
    assert listing.status_code == 200
    revision = listing.json["current_revision"]
    report = {"key": "name-1", "kind": "name_check", "excerpt": "Łucja", "reason": "Check the printed name.", "suggested_readings": [], "references": []}
    created = client.post(url, json={"expected_revision": revision, "idempotency_key": "report", "page_number": 1, "report": report})
    assert created.status_code == 200, created.json
    detail_url = f"{url}/{created.json['id']}"
    args = {"expected_revision": revision, "expected_version": created.json["version"], "idempotency_key": "resolve", "status": "dismissed", "note": "The original spelling is correct."}
    assert client.patch(detail_url, json=args).status_code == 200
    assert client.patch(detail_url, json=args).status_code == 200
    conflict = client.patch(detail_url, json={**args, "idempotency_key": "stale"})
    assert conflict.status_code == 409
    assert client.get(f"{url}?status=dismissed").json["total"] == 1
    assert client.get(f"{url}?limit=bad").status_code == 400
    assert client.post(url, json={"unexpected": True}).status_code == 400
    with app.app_context():
        assert db.session.get(ProcessedDocument, "doc").output_html == "<main><p>Łucja</p></main>"
    with client.session_transaction() as session:
        session["_user_id"] = "2"
    assert client.get(url).status_code == 404
    assert client.get(detail_url).status_code == 404
    assert client.patch(detail_url, json=args).status_code == 404
