from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.check_python_policy import (
    PolicyError,
    QualityPolicy,
    SourceScope,
    load_policy,
    resolve_scope,
    run_check,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _make_policy(
    repository: Path,
    *,
    legacy_files: dict[str, list[str]] | None = None,
    strict_files: list[str] | None = None,
    excluded_paths: list[str] | None = None,
) -> Path:
    policy_path = repository / "policy" / "python-quality.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "production_roots": ["app"],
                "test_roots": ["tests"],
                "excluded_paths": excluded_paths or [],
                "legacy_files": legacy_files or {},
                "strict_files": strict_files or [],
                "typing_baseline_file": "policy/python-typing-baseline.json",
            }
        ),
        encoding="utf-8",
    )
    return policy_path


def _make_roots(repository: Path) -> None:
    (repository / "app").mkdir(parents=True, exist_ok=True)
    (repository / "tests").mkdir(parents=True, exist_ok=True)
    (repository / "pyrightconfig.json").write_text("{}\n", encoding="utf-8")


def _write_source(repository: Path, relative_path: str) -> None:
    target = repository / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _scope(
    repository: Path,
    *,
    legacy_files: dict[str, list[str]] | None = None,
    strict_files: list[str] | None = None,
    paths: tuple[str, ...] = (),
) -> SourceScope:
    _make_roots(repository)
    for source_path in paths:
        _write_source(repository, source_path)
    policy_path = _make_policy(
        repository,
        legacy_files=legacy_files,
        strict_files=strict_files,
    )
    policy: QualityPolicy = load_policy(policy_path)
    return resolve_scope(repository, policy, paths=paths)


def test_tracked_and_untracked_sources_ratchet_legacy_directories(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _make_roots(repository)
    for source_path in (
        "app/legacy.py",
        "app/promoted.py",
        "tests/legacy_test.py",
    ):
        _write_source(repository, source_path)
    _git(repository, "init", "--quiet")
    _git(repository, "add", "app/legacy.py", "app/promoted.py", "tests/legacy_test.py")
    _write_source(repository, "app/new_module.py")
    _write_source(repository, "tests/test_new_module.py")
    policy_path = _make_policy(
        repository,
        legacy_files={"app": ["legacy.py"], "tests": ["legacy_test.py"]},
        strict_files=["app/promoted.py"],
    )

    scope = resolve_scope(repository, load_policy(policy_path))

    assert scope.strict_files == (
        "app/new_module.py",
        "app/promoted.py",
        "tests/test_new_module.py",
    )
    assert scope.strict_production_files == ("app/new_module.py", "app/promoted.py")
    assert scope.legacy_files == ("app/legacy.py", "tests/legacy_test.py")


def test_removed_or_promoted_legacy_requires_inventory_update(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _make_roots(repository)
    _write_source(repository, "app/current.py")
    policy_path = _make_policy(repository, legacy_files={"app": ["removed.py"]})
    with pytest.raises(PolicyError):
        resolve_scope(repository, load_policy(policy_path), paths=("app/current.py",))

    promoted_policy = _make_policy(
        repository,
        legacy_files={"app": ["current.py"]},
        strict_files=["app/current.py"],
    )
    with pytest.raises(PolicyError):
        load_policy(promoted_policy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strict_files", ["../outside.py"]),
        ("strict_files", ["app/readme.txt"]),
        ("strict_files", ["outside/test_new.py"]),
        ("excluded_paths", ["**/*.py"]),
        ("excluded_paths", ["app/**"]),
        ("production_roots", ["app", "app"]),
    ],
)
def test_policy_rejects_invalid_paths_roots_and_blanket_exclusions(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repository = tmp_path / "repo"
    _make_roots(repository)
    policy_path = _make_policy(repository)
    policy_data = json.loads(policy_path.read_text(encoding="utf-8"))
    policy_data[field] = value
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")

    with pytest.raises(PolicyError):
        load_policy(policy_path)


def test_policy_rejects_unknown_fields_and_legacy_paths_outside_roots(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _make_roots(repository)
    policy_path = _make_policy(repository)
    policy_data = json.loads(policy_path.read_text(encoding="utf-8"))
    policy_data["future_setting"] = True
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(policy_path)

    policy_path = _make_policy(repository, legacy_files={"outside": ["old.py"]})
    with pytest.raises(PolicyError):
        load_policy(policy_path)


def test_scope_rejects_source_symlink_that_escapes_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _make_roots(repository)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "app" / "escape.py").symlink_to(outside)
    policy_path = _make_policy(repository)

    with pytest.raises(PolicyError, match="symlink") as error:
        resolve_scope(repository, load_policy(policy_path), paths=("app/escape.py",))
    assert "escape.py" not in str(error.value)


@pytest.mark.parametrize(
    "baseline_path",
    [
        "./app/new.py",
        "./app/promoted.py",
        "./tests/legacy_test.py",
        "./outside.py",
        "../outside.py",
        "/outside.py",
    ],
)
def test_native_baseline_accepts_only_scoped_legacy_production_files(
    tmp_path: Path,
    baseline_path: str,
) -> None:
    repository = tmp_path / "repo"
    scope = _scope(
        repository,
        legacy_files={"app": ["legacy.py"], "tests": ["legacy_test.py"]},
        strict_files=["app/promoted.py"],
        paths=(
            "app/legacy.py",
            "app/promoted.py",
            "app/new.py",
            "tests/legacy_test.py",
        ),
    )
    baseline = repository / "policy" / "python-typing-baseline.json"
    baseline.write_text(
        json.dumps({"files": {baseline_path: [{"code": "reportUnknownMemberType"}]}}),
        encoding="utf-8",
    )

    with pytest.raises(PolicyError):
        run_check(repository, scope, "policy")


def test_native_baseline_is_allowed_to_be_missing(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    scope = _scope(repository, paths=("app/new.py",))

    assert run_check(repository, scope, "policy") == 0


def test_promoted_test_is_lint_strict_but_excluded_from_strict_typing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    scope = _scope(
        repository,
        legacy_files={"tests": ["legacy_test.py"]},
        strict_files=["tests/test_promoted.py"],
        paths=("tests/legacy_test.py", "tests/test_promoted.py"),
    )
    mocked_run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
    monkeypatch.setattr("scripts.check_python_policy.subprocess.run", mocked_run)

    assert scope.strict_files == ("tests/test_promoted.py",)
    assert scope.strict_production_files == ()
    assert run_check(repository, scope, "lint") == 0
    assert mocked_run.call_args.args[0][-1] == str(
        repository / "tests" / "test_promoted.py"
    )

    mocked_run.reset_mock()
    assert run_check(repository, scope, "types") == 0
    mocked_run.assert_not_called()


def test_strict_typecheck_uses_an_external_unbaselined_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    scope = _scope(
        repository,
        legacy_files={"app": ["legacy.py"]},
        paths=("app/legacy.py", "app/new.py", "tests/test_new.py"),
    )
    captured_config: dict[str, object] = {}
    captured_baseline_exists: list[bool] = []

    def inspect_config(
        command: list[str], *, env: dict[str, str], **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        config_path = Path(command[4])
        captured_config.update(json.loads(config_path.read_text(encoding="utf-8")))
        captured_baseline_exists.append(Path(captured_config["baselineFile"]).exists())
        assert env["CI"] == "true"
        return subprocess.CompletedProcess(args=command, returncode=0)

    mocked_run = Mock(side_effect=inspect_config)
    monkeypatch.setattr("scripts.check_python_policy.subprocess.run", mocked_run)

    assert run_check(repository, scope, "types") == 0

    command = mocked_run.call_args.args[0]
    environment = mocked_run.call_args.kwargs["env"]
    config_path = Path(command[4])
    config = captured_config
    baseline_path = Path(config["baselineFile"])
    assert command[:4] == [sys.executable, "-m", "basedpyright", "--project"]
    assert command[-1] == "--warnings"
    assert config["extends"] == str(repository / "pyrightconfig.json")
    assert config["include"] == [str(repository / "app" / "new.py")]
    assert config["typeCheckingMode"] == "strict"
    assert config["extraPaths"] == [str(repository)]
    assert baseline_path.parent == config_path.parent
    assert captured_baseline_exists == [False]
    assert environment["CI"] == "true"


@pytest.mark.parametrize("check", ["lint", "format"])
def test_ruff_nonzero_status_is_propagated_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
) -> None:
    repository = tmp_path / "repo"
    scope = _scope(repository, paths=("app/new module.py",))
    mocked_run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=17))
    monkeypatch.setattr("scripts.check_python_policy.subprocess.run", mocked_run)

    assert run_check(repository, scope, check) == 17  # type: ignore[arg-type]

    command = mocked_run.call_args.args[0]
    assert command[0:3] == [sys.executable, "-m", "ruff"]
    assert "shell" not in mocked_run.call_args.kwargs
    assert command[-1] == str(repository / "app" / "new module.py")


def test_empty_strict_scope_is_an_explicit_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    scope = _scope(
        repository,
        legacy_files={"app": ["legacy.py"]},
        paths=("app/legacy.py",),
    )
    mocked_run = Mock()
    monkeypatch.setattr("scripts.check_python_policy.subprocess.run", mocked_run)

    assert run_check(repository, scope, "lint") == 0
    mocked_run.assert_not_called()
