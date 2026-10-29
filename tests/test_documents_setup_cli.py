"""First-run activation can resume after the first account has been committed."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import Flask

from carlina.documents_setup import inspect_documents_setup
from iarf_assistant import cli
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.core.doctor import run_doctor
from iarf_assistant.extensions import db
from iarf_assistant.models import User


class _Manager:
    def __init__(self) -> None:
        self.loaded_addons: dict[str, object] = {}
        self.fail = True

    @contextmanager
    def lifecycle_maintenance_authority(self) -> Iterator[None]:
        yield

    def get_addon_dependency_notices(self, addon_id: str) -> object:
        return (
            [
                {"id": "pymupdf-agpl", "review_required": True},
                {"id": "extract-msg-gpl", "review_required": True},
            ]
            if addon_id == "doc_processor"
            else []
        )

    def activate_addon(
        self, addon_id: str, *, dependency_review: list[str] | None = None
    ) -> None:
        if self.fail and addon_id == "doc_processor":
            raise RuntimeError("synthetic interrupted preparation")
        self.loaded_addons.setdefault(addon_id, object())


def test_init_resumes_products_without_another_account_or_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    monkeypatch.setenv("CARLINA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(
        "iarf_assistant.core.app_paths._platform_roots",
        lambda *_args: (tmp_path / "unused-platform-root",) * 4,
    )
    paths = resolve_app_paths()
    paths.database_dir.mkdir(parents=True)
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{paths.database_file.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    app.extensions["carlina_paths"] = paths
    manager = _Manager()
    app.addon_manager = manager  # type: ignore[attr-defined]
    db.init_app(app)
    monkeypatch.setattr("flask_migrate.upgrade", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli, "_reconcile_builtin_addons_after_core_migration", lambda _app: {}
    )
    cli.register_cli(app, assistant_services=False)
    with app.app_context():
        db.create_all()
    payload = json.dumps(
        {"email": "first-setup@example.invalid", "password": "synthetic-password-Ω"},
        ensure_ascii=False,
    ).encode("utf-8")
    runner = app.test_cli_runner()
    failed = runner.invoke(args=["init-app", "--admin-stdin"], input=payload)
    assert failed.exit_code == 1
    assert inspect_documents_setup(paths) == "pending"
    with app.app_context():
        assert User.query.count() == 1
        user = User.query.one()
        user_id = user.id
        assert user.check_password("synthetic-password-Ω")
    assert (
        next(
            check for check in run_doctor(paths).checks if check.id == "documents.setup"
        ).status
        == "warning"
    )

    manager.fail = False
    resumed = runner.invoke(args=["init-app", "--no-admin-prompt"])
    assert resumed.exit_code == 0, resumed.output
    assert inspect_documents_setup(paths) == "complete"
    assert set(manager.loaded_addons) == {
        "sentence_segmenter",
        "doc_processor",
        "translator",
    }
    with app.app_context():
        assert User.query.count() == 1
        assert User.query.one().id == user_id
        assert User.query.one().check_password("synthetic-password-Ω")
    assert (
        next(
            check for check in run_doctor(paths).checks if check.id == "documents.setup"
        ).status
        == "ok"
    )

    manager.loaded_addons.pop("translator")
    manager.fail = True
    rerun = runner.invoke(args=["init-app", "--no-admin-prompt"])
    assert rerun.exit_code == 0, rerun.output
    assert "translator" not in manager.loaded_addons
