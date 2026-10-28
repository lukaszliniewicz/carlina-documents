"""Operator commands follow the host's resolved application composition."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

from iarf_assistant import cli

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_ASSISTANT_COMMANDS = {"rebuild-rag-index", "import-news-data"}
_SHARED_COMMANDS = {"init-app", "create-user", "grant-admin", "addons", "mcp"}


@pytest.mark.parametrize("assistant_services", [False, True])
def test_cli_registration_retains_shared_commands_and_gates_assistant_commands(
    assistant_services: bool,
) -> None:
    app = Flask("cli-composition")
    cli.register_cli(app, assistant_services=assistant_services)

    assert app.cli.commands.keys() >= _SHARED_COMMANDS
    assert _ASSISTANT_COMMANDS & app.cli.commands.keys() == (
        _ASSISTANT_COMMANDS if assistant_services else set()
    )
    runner = app.test_cli_runner()
    for command in sorted(_ASSISTANT_COMMANDS):
        result = runner.invoke(args=[command, "--help"])
        assert result.exit_code == (0 if assistant_services else 2)
        if assistant_services and command == "import-news-data":
            assert "--file" in result.output
            assert "--email" in result.output


def test_legacy_cli_registration_defaults_to_workbench_commands() -> None:
    app = Flask("legacy-cli-composition")

    cli.register_cli(app)

    assert app.cli.commands.keys() >= _SHARED_COMMANDS | _ASSISTANT_COMMANDS


@pytest.mark.parametrize("context_ids", [[], [17, 23]])
def test_workbench_rag_command_retains_rebuild_and_addon_notification(
    monkeypatch: pytest.MonkeyPatch, context_ids: list[int]
) -> None:
    contexts = [SimpleNamespace(id=identifier) for identifier in context_ids]
    query = Mock()
    query.all.return_value = contexts
    monkeypatch.setattr(cli, "DataContext", SimpleNamespace(query=query))
    app = Flask("workbench-rag-cli")
    services = Mock()
    app.rag_service = services.rag
    app.addon_manager = services.addons
    cli.register_cli(app, assistant_services=True)

    result = app.test_cli_runner().invoke(args=["rebuild-rag-index"])

    assert result.exit_code == 0, result.output
    if context_ids:
        assert services.mock_calls == [
            ("rag.delete_all_embeddings", (), {}),
            *(
                ("rag.generate_embeddings_for_context", (identifier,), {})
                for identifier in context_ids
            ),
            ("addons.notify_rag_rebuild", (), {}),
        ]
    else:
        assert services.mock_calls == []
