"""First-account setup keeps secrets in pipes and serializes account creation."""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from sqlalchemy import event

from carlina.core.initial_admin import (
    MAX_ADMIN_INPUT_BYTES,
    InitialAdminInputError,
    encode_initial_admin,
    parse_initial_admin,
    read_initial_admin,
)
from iarf_assistant import bootstrap, cli
from iarf_assistant.extensions import db
from iarf_assistant.models import User

_PASSWORD = "synthetic-pipe-password"


def _payload() -> bytes:
    return json.dumps({"email": "reader@example.test", "password": _PASSWORD}).encode()


def test_private_input_roundtrip_and_safe_repr() -> None:
    credentials = read_initial_admin(io.BytesIO(_payload()))
    assert credentials.email == "reader@example.test"
    assert credentials.password == _PASSWORD
    assert (
        parse_initial_admin(encode_initial_admin(credentials).encode()) == credentials
    )
    assert _PASSWORD not in repr(credentials)
    assert credentials.email not in repr(credentials)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b"null",
        b'"text"',
        b"\xff",
        b"{}",
        b'{"email":"a@b","password":"a","password":"b"}',
        b'{"email":"a@b","password":"a","extra":true}',
        b'{"email":true,"password":"a"}',
        b'{"email":"a@b","password":false}',
        b'{"email":"a b@c","password":"a"}',
        b'{"email":"a@","password":"a"}',
        b'{"email":"@b","password":"a"}',
        b'{"email":"a@b","password":""}',
        b'{"email":"a@b","password":"\\u0000"}',
        b'{"email":"\\ud800@b","password":"a"}',
        b'{"email":"a@b","password":"\\ud800"}',
        json.dumps({"email": "a" * 100 + "@b", "password": _PASSWORD}).encode(),
        json.dumps({"email": "a@b", "password": "a" * 4097}).encode(),
        b" " * (MAX_ADMIN_INPUT_BYTES + 1),
    ],
)
def test_malformed_input_never_echoes_payload(payload: bytes) -> None:
    with pytest.raises(InitialAdminInputError) as failure:
        read_initial_admin(io.BytesIO(payload))
    assert str(failure.value) == "Initial administrator input is invalid."


def test_reader_stops_at_the_input_limit() -> None:
    stream = io.BytesIO(b"x" * (MAX_ADMIN_INPUT_BYTES * 3))
    with pytest.raises(InitialAdminInputError):
        read_initial_admin(stream)
    assert stream.tell() == MAX_ADMIN_INPUT_BYTES + 1


def test_database_child_gets_secret_only_in_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr(bootstrap, "run_command", execute)
    payload = _payload().decode()
    bootstrap.initialize_database(None, None, admin_input=payload)
    command = execute.call_args.args[0]
    assert command[-1] == "--admin-stdin"
    assert not any(_PASSWORD in value or "reader@" in value for value in command)
    assert execute.call_args.kwargs["input_text"] == payload


def test_actual_pipe_does_not_put_password_in_output_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "ensure_runtime_dirs", lambda: None)
    log = tmp_path / "setup.log"
    command = [
        sys.executable,
        "-c",
        "import json,sys; data=json.load(sys.stdin); assert data['password']; print('received private input')",
    ]
    result = bootstrap.run_command(
        command, log_path=log, input_text=_payload().decode()
    )
    assert result.returncode == 0
    assert _PASSWORD not in log.read_text()
    assert _PASSWORD not in capsys.readouterr().out


def test_invalid_setup_input_fails_before_path_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = Mock(side_effect=AssertionError("must not resolve paths"))
    monkeypatch.setattr(bootstrap, "load_bootstrap_paths", paths)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"invalid")))
    assert bootstrap.setup_main(["--admin-stdin"]) == 1
    paths.assert_not_called()
    assert "Initial administrator input is invalid." in capsys.readouterr().out


def test_two_independent_writers_create_only_one_initial_account(
    tmp_path: Path,
) -> None:
    app = Flask("initial-admin-race")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'accounts.sqlite'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    ready = threading.Barrier(2)
    connections: set[int] = set()
    results: list[bool] = []
    failures: list[BaseException] = []
    with app.app_context():
        User.__table__.create(db.engine)
        engine = db.engine

    def before_execute(connection, _cursor, statement, _parameters, _context, _many):
        if statement == "BEGIN IMMEDIATE":
            connections.add(id(connection.connection.driver_connection))
            ready.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", before_execute)

    def create(email: str) -> None:
        try:
            with app.app_context():
                results.append(cli._create_first_admin(email, _PASSWORD))
        except BaseException as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=create, args=(f"reader{n}@example.test",))
        for n in (1, 2)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert not any(thread.is_alive() for thread in threads)
        assert not failures
        assert len(connections) == 2
        assert sorted(results) == [False, True]
        with app.app_context():
            users = User.query.all()
            assert len(users) == 1 and users[0].is_admin
            assert users[0].check_password(_PASSWORD)
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
        with app.app_context():
            db.session.remove()
            engine.dispose()
