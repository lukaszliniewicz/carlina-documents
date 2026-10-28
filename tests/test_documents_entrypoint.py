"""Profile-specific launchers select paths before managed command side effects."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carlina import documents
from carlina.profile_binding import bind_runtime_profile
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant import bootstrap, bootstrap_manager, bootstrap_state
from iarf_assistant.core import doctor
from iarf_assistant.core.app_paths import resolve_app_paths

pytestmark = [pytest.mark.fast, pytest.mark.contract]


@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_status_reports_explicit_profile_without_renaming_the_framework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile)
    paths = resolve_app_paths(
        program_root=tmp_path / "program",
        environment={"CARLINA_HOME": str(tmp_path / "data")},
    )
    monkeypatch.setattr(bootstrap_manager, "_active_paths", lambda: paths)
    monkeypatch.setattr(bootstrap_state, "is_runtime_running", Mock(return_value=False))
    monkeypatch.setattr(
        doctor,
        "run_doctor",
        Mock(
            return_value=SimpleNamespace(
                layout={}, to_dict=lambda: {"summary": {}}, exit_code=lambda: 0
            )
        ),
    )
    monkeypatch.setattr(
        bootstrap_manager,
        "_managed_installation_layout",
        lambda: SimpleNamespace(
            root=tmp_path,
            current_version=lambda: None,
            journal_file=tmp_path / "missing-journal",
        ),
    )
    monkeypatch.setattr(
        bootstrap_manager, "_managed_server_running", Mock(return_value=False)
    )
    output = Mock()
    monkeypatch.setattr(bootstrap_manager, "_print_json", output)
    assert (
        bootstrap_manager._status_command(
            SimpleNamespace(host="127.0.0.1", port=5005, json=True)
        )
        == 0
    )
    assert output.call_args.args[0]["application_profile"] == profile
    assert output.call_args.args[0]["product"] == "Carlina"


def test_documents_launcher_passes_fixed_profile_without_eager_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = Mock(return_value=17)
    monkeypatch.setattr("carlina.bootstrap_manager.manager_main", manager)

    assert documents.main(["status", "--json"]) == 17
    manager.assert_called_once_with(
        ["status", "--json"], profile=RuntimeProfile.DOCUMENTS
    )


def test_documents_entrypoint_rejects_workbench_override_before_loading_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = Mock(side_effect=AssertionError("paths must not be loaded"))
    monkeypatch.setattr(bootstrap, "load_bootstrap_paths", paths)

    with pytest.raises(SystemExit) as failure:
        documents.main(["start", "--application-profile", "workbench"])

    assert failure.value.code == 2
    paths.assert_not_called()


@pytest.mark.parametrize("command", ["init", "start"])
def test_explicit_command_profile_is_bound_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    root = tmp_path / "fresh-documents"
    paths = resolve_app_paths(
        program_root=tmp_path / "program", environment={"CARLINA_HOME": str(root)}
    )
    loading = Mock(return_value=paths)
    dispatched = Mock(return_value=13)
    monkeypatch.setattr(bootstrap, "load_bootstrap_paths", loading)
    monkeypatch.setattr(bootstrap, "setup_main", dispatched)
    monkeypatch.setattr(bootstrap_manager, "start_app", dispatched)
    monkeypatch.delenv("CARLINA_PROGRAM_ROOT", raising=False)
    monkeypatch.delenv("IARF_PROGRAM_ROOT", raising=False)
    monkeypatch.setenv("CARLINA_PROFILE", "workbench")
    monkeypatch.delenv("CARLINA_PORT", raising=False)
    monkeypatch.delenv("IARF_PORT", raising=False)

    assert (
        bootstrap_manager.manager_main([command, "--application-profile", "documents"])
        == 13
    )

    assert loading.call_args_list[0].kwargs == {"profile": RuntimeProfile.DOCUMENTS}
    dispatched.assert_called_once()
    assert os.environ["CARLINA_PROFILE"] == "documents"
    assert not root.exists()
    if command == "init":
        assert dispatched.call_args.args[0][-2:] == [
            "--application-profile",
            "documents",
        ]
    else:
        assert dispatched.call_args.args[1] == 5005


def test_fixed_documents_profile_refuses_existing_workbench_root_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "retained-workbench"
    bind_runtime_profile(root, RuntimeProfile.WORKBENCH)
    paths = resolve_app_paths(
        program_root=tmp_path / "program", environment={"CARLINA_HOME": str(root)}
    )
    before = {
        path.name: path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    dispatch = Mock(side_effect=AssertionError("dispatch must not run"))
    monkeypatch.setattr(bootstrap, "load_bootstrap_paths", Mock(return_value=paths))
    monkeypatch.setattr(bootstrap_manager, "start_app", dispatch)
    monkeypatch.setenv("CARLINA_PROFILE", "workbench")

    assert documents.main(["start"]) == 1

    dispatch.assert_not_called()
    assert os.environ["CARLINA_PROFILE"] == "workbench"
    assert before == {
        path.name: path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_checkout_environment_is_skipped_for_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("CARLINA_HOME=unrelated-root\n", encoding="utf-8")
    loader = Mock(side_effect=AssertionError("checkout dotenv must not be loaded"))
    monkeypatch.setattr(bootstrap, "load_dotenv", loader)
    monkeypatch.setenv("CARLINA_PROFILE", "workbench")

    assert not bootstrap.load_project_environment(tmp_path, profile="documents")
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    assert not bootstrap.load_project_environment(tmp_path)
    loader.assert_not_called()
