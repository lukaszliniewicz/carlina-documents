"""Application selection and preflight guards for the shared runtime."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from carlina.distribution import ApplicationDistribution
from carlina.profile_binding import bind_runtime_profile
from carlina.runtime_composition import DOCUMENT_ADDON_IDS, resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError
from iarf_assistant import bootstrap
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.startup import register_scheduler

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def test_document_scheduler_retains_shared_and_product_job_support_only(
    monkeypatch: pytest.MonkeyPatch,
):
    from flask import Flask

    from iarf_assistant import startup

    app = Flask("document-scheduler")
    scheduler = Mock()
    monkeypatch.setattr(startup, "init_scheduler", lambda _app: scheduler)
    monkeypatch.setattr(startup, "is_worker_process", lambda: False)
    unrelated = Mock(side_effect=AssertionError("unrelated service was constructed"))
    monkeypatch.setattr(startup, "WorkflowOrchestrator", unrelated)
    monkeypatch.setattr(startup, "memory_pruner_enabled", unrelated)

    register_scheduler(app, owns_services=True, assistant_services=False)

    assert app.scheduler is scheduler
    unrelated.assert_not_called()
    assert [call.kwargs["id"] for call in scheduler.add_job.call_args_list] == [
        "core_runtime_artifact_retention"
    ]


def test_document_product_jobs_register_after_models_and_before_scheduler_resume(
    monkeypatch: pytest.MonkeyPatch,
):
    from flask import Flask

    from iarf_assistant import startup

    app = Flask("document-product-jobs")
    events: list[str] = []
    app.scheduler = Mock()
    app.addon_manager = Mock()
    monkeypatch.setattr(
        "sqlalchemy.orm.configure_mappers", lambda: events.append("models")
    )
    app.addon_manager.register_scheduled_jobs.side_effect = lambda: events.append(
        "jobs"
    )
    app.scheduler.resume.side_effect = lambda: events.append("resume")

    startup.activate_scheduler(app, register_addon_jobs=True)

    assert events == ["models", "jobs", "resume"]


def test_document_product_jobs_wait_if_model_configuration_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    from flask import Flask

    from iarf_assistant import startup

    app = Flask("document-product-jobs-failure")
    app.scheduler = Mock()
    app.addon_manager = Mock()
    configure = Mock(side_effect=RuntimeError("incomplete model registry"))
    monkeypatch.setattr("sqlalchemy.orm.configure_mappers", configure)

    startup.activate_scheduler(app, register_addon_jobs=True)

    app.addon_manager.register_scheduled_jobs.assert_not_called()
    app.scheduler.resume.assert_not_called()


def test_explicit_factory_profile_wins_over_ambient_selection():
    document = resolve_runtime_composition(
        "documents", environment={"CARLINA_PROFILE": "workbench"}
    )
    workbench = resolve_runtime_composition(
        RuntimeProfile.WORKBENCH, environment={"CARLINA_PROFILE": "documents"}
    )

    assert document.profile is RuntimeProfile.DOCUMENTS
    assert document.addon_ids == DOCUMENT_ADDON_IDS
    assert not document.assistant_services
    assert not document.container_services
    assert workbench.addon_ids is None
    assert workbench.assistant_services
    assert workbench.container_services


def test_default_and_environment_selection():
    assert (
        resolve_runtime_composition(environment={}).profile is RuntimeProfile.WORKBENCH
    )
    assert (
        resolve_runtime_composition(
            environment={"CARLINA_PROFILE": "documents"}
        ).profile
        is RuntimeProfile.DOCUMENTS
    )


def test_documents_distribution_defaults_to_its_immutable_profile(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "carlina.runtime_composition.read_application_distribution",
        lambda: ApplicationDistribution(
            RuntimeProfile.DOCUMENTS,
            "a" * 40,
            1791100000,
            tuple(sorted(DOCUMENT_ADDON_IDS)),
        ),
    )

    assert (
        resolve_runtime_composition(environment={}).profile is RuntimeProfile.DOCUMENTS
    )


@pytest.mark.parametrize("explicit", [None, RuntimeProfile.WORKBENCH])
def test_documents_distribution_rejects_workbench_before_path_resolution(
    monkeypatch: pytest.MonkeyPatch, explicit: RuntimeProfile | None, tmp_path: Path
):
    monkeypatch.setattr(
        "carlina.runtime_composition.read_application_distribution",
        lambda: ApplicationDistribution(
            RuntimeProfile.DOCUMENTS,
            "b" * 40,
            1791100000,
            tuple(sorted(DOCUMENT_ADDON_IDS)),
        ),
    )
    root = tmp_path / "uncreated-root"
    with pytest.raises(RuntimeProfileError, match="supports only the Documents"):
        resolve_app_paths(
            program_root=tmp_path / "program",
            profile=explicit,
            environment={"CARLINA_PROFILE": "workbench", "CARLINA_HOME": str(root)},
        )

    assert not root.exists()


@pytest.mark.parametrize("value", ["", "Documents", "unknown", object()])
def test_unsupported_selection_fails_without_echoing_input(value: object):
    with pytest.raises(RuntimeProfileError) as failure:
        resolve_runtime_composition(value)  # type: ignore[arg-type]

    assert str(failure.value) == "The requested runtime profile is unsupported."


@pytest.mark.parametrize("command", ["setup", "setup-check", "start"])
def test_bootstrap_rejects_profile_switch_before_any_root_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    root = tmp_path / "retained-documents"
    bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)
    paths = resolve_app_paths(
        program_root=tmp_path / "program", environment={"CARLINA_HOME": str(root)}
    )
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(bootstrap, "load_bootstrap_paths", lambda **_kwargs: paths)
    monkeypatch.setattr(bootstrap, "check_python_version", lambda: None)
    mutations = Mock(side_effect=AssertionError("preflight must precede root writes"))
    monkeypatch.setattr(bootstrap, "ensure_runtime_dirs", mutations)
    monkeypatch.setattr(bootstrap, "_append_log", mutations)
    monkeypatch.setenv("CARLINA_PROFILE", "documents")

    arguments = ["--application-profile", "workbench"]
    if command == "start":
        result = bootstrap.start_main(arguments)
    else:
        if command == "setup-check":
            arguments.append("--check-only")
        result = bootstrap.setup_main(arguments)

    assert result == 1
    mutations.assert_not_called()
    assert os.environ["CARLINA_PROFILE"] == "documents"
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize(
    "profile,target", [("workbench", ".[workbench]"), ("documents", ".")]
)
def test_checkout_install_uses_only_selected_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch, profile: str, target: str
):
    monkeypatch.setenv("CARLINA_PROFILE", profile)
    command = Mock()
    monkeypatch.setattr(bootstrap, "run_command", command)

    bootstrap.install_project()

    assert command.call_args.args[0][-1] == target
