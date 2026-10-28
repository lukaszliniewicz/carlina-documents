from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

import iarf_assistant.core.app_paths as app_paths
from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError
from iarf_assistant.core.app_paths import (
    addon_environment_cache_path,
    load_runtime_environment,
    resolve_app_paths,
)

_ORIGINAL_PLATFORM_ROOTS = app_paths._platform_roots


def _roots(root: Path) -> tuple[Path, Path, Path, Path]:
    return (
        root / "data",
        root / "config",
        root / "cache",
        root / "logs",
    )


def _patch_platform_roots(monkeypatch, tmp_path: Path):
    roots = {
        RuntimeProfile.WORKBENCH: _roots(tmp_path / "workbench"),
        RuntimeProfile.DOCUMENTS: _roots(tmp_path / "documents"),
    }
    calls: list[tuple[object, ...]] = []

    def platform_roots(*args: object) -> tuple[Path, Path, Path, Path]:
        calls.append(args)
        profile = args[0] if args else RuntimeProfile.WORKBENCH
        return roots[profile]

    monkeypatch.setattr(app_paths, "_platform_roots", platform_roots)
    return roots, calls


class _UnreadableEnvironment(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"environment key was read: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("environment keys were read")

    def __len__(self) -> int:
        raise AssertionError("environment keys were read")


def test_platform_roots_use_distinct_profile_names_without_creating_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []

    class FakePlatformDirs:
        def __init__(
            self,
            *,
            appname: str,
            appauthor: bool,
            roaming: bool,
            ensure_exists: bool,
        ) -> None:
            calls.append(
                {
                    "appname": appname,
                    "appauthor": appauthor,
                    "roaming": roaming,
                    "ensure_exists": ensure_exists,
                }
            )
            base = tmp_path / appname.replace(" ", "-")
            self.user_data_path = base / "data"
            self.user_config_path = base / "config"
            self.user_cache_path = base / "cache"
            self.user_log_path = base / "logs"

    monkeypatch.setattr(app_paths, "PlatformDirs", FakePlatformDirs)
    monkeypatch.setattr(app_paths, "_platform_roots", _ORIGINAL_PLATFORM_ROOTS)
    program = tmp_path / "program"
    package = program / "iarf_assistant"

    workbench = resolve_app_paths(
        program,
        package_root=package,
        environment={},
    )
    documents = resolve_app_paths(
        program,
        package_root=package,
        environment={},
        profile=RuntimeProfile.DOCUMENTS,
    )

    assert [call["appname"] for call in calls] == ["Carlina", "Carlina Documents"]
    assert all(
        call["appauthor"] is False
        and call["roaming"] is False
        and call["ensure_exists"] is False
        for call in calls
    )
    assert workbench.data_root == (tmp_path / "Carlina" / "data").resolve()
    assert documents.data_root == (tmp_path / "Carlina-Documents" / "data").resolve()
    assert workbench.config_dir == (tmp_path / "Carlina" / "config").resolve()
    assert documents.config_dir == (tmp_path / "Carlina-Documents" / "config").resolve()
    assert workbench.cache_dir == (tmp_path / "Carlina" / "cache").resolve()
    assert documents.cache_dir == (tmp_path / "Carlina-Documents" / "cache").resolve()
    assert workbench.log_dir == (tmp_path / "Carlina" / "logs").resolve()
    assert documents.log_dir == (tmp_path / "Carlina-Documents" / "logs").resolve()
    assert workbench.secrets_dir == workbench.data_root / "secrets"
    assert documents.secrets_dir == documents.data_root / "secrets"
    assert workbench.addon_environments_dir == addon_environment_cache_path(
        workbench.data_root,
        workbench.cache_dir,
    )
    assert documents.addon_environments_dir == addon_environment_cache_path(
        documents.data_root,
        documents.cache_dir,
    )
    assert workbench.migration_target_root == workbench.data_root
    assert documents.migration_target_root == documents.data_root
    assert not (tmp_path / "Carlina").exists()
    assert not (tmp_path / "Carlina-Documents").exists()


def test_profile_environment_selects_paths_and_explicit_profile_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots, calls = _patch_platform_roots(monkeypatch, tmp_path)

    documents_from_environment = resolve_app_paths(
        tmp_path / "program",
        environment={"CARLINA_PROFILE": "documents"},
    )
    documents_explicit = resolve_app_paths(
        tmp_path / "program",
        environment={"CARLINA_PROFILE": "workbench"},
        profile="documents",
    )
    workbench_explicit = resolve_app_paths(
        tmp_path / "program",
        environment={"CARLINA_PROFILE": "documents"},
        profile=RuntimeProfile.WORKBENCH,
    )
    workbench_default = resolve_app_paths(tmp_path / "program", environment={})

    assert documents_from_environment.data_root == roots[RuntimeProfile.DOCUMENTS][0]
    assert documents_explicit.data_root == roots[RuntimeProfile.DOCUMENTS][0]
    assert workbench_explicit.data_root == roots[RuntimeProfile.WORKBENCH][0]
    assert workbench_default.data_root == roots[RuntimeProfile.WORKBENCH][0]
    assert calls == [
        (RuntimeProfile.DOCUMENTS,),
        (RuntimeProfile.DOCUMENTS,),
        (),
        (),
    ]


def test_documents_never_selects_checkout_legacy_but_workbench_still_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots, _ = _patch_platform_roots(monkeypatch, tmp_path)
    program = tmp_path / "program"
    package = program / "iarf_assistant"
    package.mkdir(parents=True)
    legacy = program / "instance"
    legacy.mkdir()
    legacy_database = legacy / "iarf_ai.db"
    legacy_database.write_bytes(b"legacy")

    documents = resolve_app_paths(
        program,
        package_root=package,
        environment={"CARLINA_PROFILE": "documents"},
        allow_legacy=True,
    )
    workbench = resolve_app_paths(
        program,
        package_root=package,
        environment={},
        allow_legacy=True,
    )

    assert documents.mode == "platform"
    assert documents.data_root == roots[RuntimeProfile.DOCUMENTS][0]
    assert documents.database_file != legacy_database.resolve()
    assert documents.migration_target_root == roots[RuntimeProfile.DOCUMENTS][0]
    assert workbench.mode == "legacy"
    assert workbench.database_file == legacy_database.resolve()
    assert legacy_database.read_bytes() == b"legacy"


def test_documents_home_secrets_and_worker_overrides_keep_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots, _ = _patch_platform_roots(monkeypatch, tmp_path)
    home = tmp_path / "documents-home"
    data_alias = tmp_path / "data-alias"
    secrets = tmp_path / "local-keys"
    workers = tmp_path / "worker-cache"

    paths = resolve_app_paths(
        tmp_path / "program",
        environment={
            "CARLINA_PROFILE": "workbench",
            "CARLINA_HOME": str(home),
            "CARLINA_DATA_ROOT": str(data_alias),
            "CARLINA_SECRETS_DIR": str(secrets),
            "CARLINA_ADDON_ENVIRONMENTS_DIR": str(workers),
        },
        profile=RuntimeProfile.DOCUMENTS,
    )

    assert paths.mode == "portable"
    assert paths.data_root == home.resolve()
    assert paths.config_dir == home.resolve() / "config"
    assert paths.cache_dir == home.resolve() / "cache"
    assert paths.log_dir == home.resolve() / "logs"
    assert paths.secrets_dir == secrets.resolve()
    assert paths.master_keys_file == secrets.resolve() / "master-keys.json"
    assert paths.session_key_file == secrets.resolve() / "session.key"
    assert paths.addon_environments_dir == workers.resolve()
    assert paths.migration_target_root == roots[RuntimeProfile.DOCUMENTS][0]
    assert not home.exists()
    assert not data_alias.exists()
    assert not secrets.exists()
    assert not workers.exists()


def test_checkout_dotenv_cannot_change_default_workbench_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots, _ = _patch_platform_roots(monkeypatch, tmp_path)
    program = tmp_path / "program"
    package = program / "iarf_assistant"
    package.mkdir(parents=True)
    (program / ".env").write_text(
        "CARLINA_PROFILE=documents\nCARLINA_CHECKOUT_SENTINEL=checkout\n",
        encoding="utf-8",
    )
    canonical_config = roots[RuntimeProfile.WORKBENCH][1] / "carlina.env"
    canonical_config.parent.mkdir(parents=True)
    canonical_config.write_text(
        "CARLINA_CANONICAL_SENTINEL=workbench\n",
        encoding="utf-8",
    )
    process_environment: dict[str, str] = {}
    monkeypatch.setattr(app_paths.os, "environ", process_environment)

    dotenv_reads: list[Path] = []
    original_dotenv_values = app_paths.dotenv_values

    def record_dotenv_read(path: str | Path):
        dotenv_reads.append(Path(path))
        return original_dotenv_values(path)

    monkeypatch.setattr(app_paths, "dotenv_values", record_dotenv_read)

    paths = load_runtime_environment(program, package_root=package)

    assert paths.mode == "platform"
    assert paths.data_root == roots[RuntimeProfile.WORKBENCH][0]
    assert paths.migration_target_root == roots[RuntimeProfile.WORKBENCH][0]
    assert dotenv_reads == [(program / ".env").resolve(), canonical_config.resolve()]
    assert "CARLINA_PROFILE" not in process_environment
    assert process_environment["CARLINA_CHECKOUT_SENTINEL"] == "checkout"
    assert process_environment["CARLINA_CANONICAL_SENTINEL"] == "workbench"


@pytest.mark.parametrize("profile_from_environment", [False, True])
def test_documents_environment_ignores_checkout_dotenv_and_preserves_process_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_from_environment: bool,
):
    roots, _ = _patch_platform_roots(monkeypatch, tmp_path)
    program = tmp_path / "program"
    package = program / "iarf_assistant"
    package.mkdir(parents=True)
    checkout_home = tmp_path / "checkout-home"
    documents_home = tmp_path / "documents-home"
    (program / ".env").write_text(
        f"CARLINA_HOME={checkout_home}\n"
        "CARLINA_CHECKOUT_SENTINEL=checkout\n"
        "CARLINA_PROCESS_VALUE=checkout\n",
        encoding="utf-8",
    )
    canonical_config = roots[RuntimeProfile.DOCUMENTS][1] / "carlina.env"
    canonical_config.parent.mkdir(parents=True)
    canonical_config.write_text(
        f"CARLINA_HOME={documents_home}\n"
        "CARLINA_PROFILE=workbench\n"
        "CARLINA_CANONICAL_SENTINEL=documents\n"
        "CARLINA_PROCESS_VALUE=canonical\n",
        encoding="utf-8",
    )
    process_environment = {"CARLINA_PROCESS_VALUE": "process"}
    requested_profile = None
    if profile_from_environment:
        process_environment["CARLINA_PROFILE"] = "documents"
    else:
        requested_profile = RuntimeProfile.DOCUMENTS
    monkeypatch.setattr(app_paths.os, "environ", process_environment)

    dotenv_reads: list[Path] = []
    original_dotenv_values = app_paths.dotenv_values

    def record_dotenv_read(path: str | Path):
        dotenv_reads.append(Path(path))
        return original_dotenv_values(path)

    monkeypatch.setattr(app_paths, "dotenv_values", record_dotenv_read)

    paths = load_runtime_environment(
        program,
        package_root=package,
        profile=requested_profile,
    )

    assert paths.mode == "portable"
    assert paths.data_root == documents_home.resolve()
    assert paths.migration_target_root == roots[RuntimeProfile.DOCUMENTS][0]
    assert dotenv_reads == [canonical_config.resolve()]
    assert "CARLINA_CHECKOUT_SENTINEL" not in process_environment
    assert process_environment["CARLINA_CANONICAL_SENTINEL"] == "documents"
    assert process_environment["CARLINA_PROCESS_VALUE"] == "process"
    assert process_environment.get("CARLINA_PROFILE") == (
        "documents" if profile_from_environment else None
    )
    assert not checkout_home.exists()
    assert not documents_home.exists()


def test_unsupported_profile_is_rejected_before_environment_or_path_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    program = tmp_path / "program"
    environment = _UnreadableEnvironment()
    monkeypatch.setattr(
        app_paths,
        "_platform_roots",
        lambda *args: pytest.fail("platform roots were read for an invalid profile"),
    )

    with pytest.raises(RuntimeProfileError, match="profile is unsupported"):
        resolve_app_paths(
            program,
            environment=environment,
            profile="unsupported",
        )

    monkeypatch.setattr(
        app_paths,
        "os",
        SimpleNamespace(environ=environment),
    )
    monkeypatch.setattr(
        app_paths,
        "dotenv_values",
        lambda *args: pytest.fail("dotenv was read for an invalid profile"),
    )
    with pytest.raises(RuntimeProfileError, match="profile is unsupported"):
        load_runtime_environment(program, profile="unsupported")

    assert not program.exists()
