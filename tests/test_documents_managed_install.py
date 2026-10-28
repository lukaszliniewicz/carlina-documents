"""The Documents managed installation keeps product identity at each boundary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import venv
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from carlina.core.managed_probe import (
    ManagedProbeAttestation,
    ManagedProbeCapability,
)
from carlina.core.recovery_policy import RecoveryCoverage
from carlina.profile_binding import (
    bind_runtime_profile,
    profile_marker_path,
    validate_runtime_profile,
)
from carlina.runtime_profiles import ContentPolicy, RuntimeProfile, RuntimeProfileError
from iarf_assistant.core import app_paths as app_paths_module
from iarf_assistant.core import managed_install
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.core.managed_install import (
    ManagedInstallationLayout,
    ManagedInstallError,
    ManagedReleaseManager,
)
from iarf_assistant.core.release_manifest import (
    TrustedReleaseKey,
    parse_release_index,
    sign_release_index,
    trusted_key_record,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _documents_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: RuntimeProfile = RuntimeProfile.DOCUMENTS,
):
    monkeypatch.setattr(
        app_paths_module,
        "_platform_roots",
        lambda _profile=None: tuple(
            tmp_path / name
            for name in (
                "platform-data",
                "platform-config",
                "platform-cache",
                "platform-logs",
            )
        ),
    )
    root = tmp_path / "source"
    package = root / "iarf_assistant"
    package.mkdir(parents=True)
    data_root = tmp_path / f"{profile.value}-data"
    bind_runtime_profile(data_root, profile)
    return resolve_app_paths(
        root,
        package_root=package,
        environment={"CARLINA_HOME": str(data_root), "CARLINA_PROFILE": profile.value},
        allow_legacy=False,
    )


@pytest.mark.parametrize(
    ("platform", "profile", "expected"),
    [
        ("linux", "workbench", ".local/lib/carlina"),
        ("linux", "documents", ".local/lib/carlina-documents"),
        ("win32", "workbench", "Programs/Carlina"),
        ("win32", "documents", "Programs/CarlinaDocuments"),
        ("darwin", "workbench", "Library/Application Support/Carlina/Program"),
        ("darwin", "documents", "Library/Application Support/CarlinaDocuments/Program"),
    ],
)
def test_default_program_root_is_profile_specific(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    profile: str,
    expected: str,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(managed_install.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(managed_install.sys, "platform", platform)
    monkeypatch.setattr(
        managed_install,
        "os",
        SimpleNamespace(
            name="nt" if platform == "win32" else "posix", environ=os.environ
        ),
    )

    assert managed_install.default_program_root() == (tmp_path / expected).resolve()


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize(
    "profile", [RuntimeProfile.DOCUMENTS, RuntimeProfile.WORKBENCH]
)
def test_wrong_index_profile_rejected_before_program_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signed: bool,
    profile: RuntimeProfile,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile.value)
    paths = _documents_paths(tmp_path, monkeypatch, profile=profile)
    program = tmp_path / "program"
    manager = ManagedReleaseManager(paths, program_root=program)
    payload = {
        "format": "carlina.release-index",
        "schema_version": 1,
        "channel": "stable",
        "generated_at": "2026-10-04T00:00:00Z",
        "releases": [
            {
                "version": "1.1.0",
                "published_at": "2026-10-04T00:00:00Z",
                "addon_api_version": "1.0",
                "database": {
                    "known_revisions": ["rev1"],
                    "heads": ["rev1"],
                    "rollback": "compatible",
                },
                "artifacts": [
                    {
                        "platform": "linux-64",
                        "kind": "pixi-pack-executable",
                        "filename": "carlina-1.1.0-linux-64.sh",
                        "url": "carlina-1.1.0-linux-64.sh",
                        "sha256": "a" * 64,
                        "size": 1,
                    }
                ],
            }
        ],
        "signatures": [],
        "application_profile": (
            "workbench" if profile is RuntimeProfile.DOCUMENTS else "documents"
        ),
    }
    index = parse_release_index(payload)
    if signed:
        private_key = Ed25519PrivateKey.generate()
        index = sign_release_index(index, private_key)
        key = trusted_key_record(private_key)
        trusted = TrustedReleaseKey(
            key_id=key["key_id"],
            algorithm=key["algorithm"],
            public_key=key["public_key"],
            channels=tuple(key["channels"]),
        )
        monkeypatch.setattr(
            managed_install, "load_trusted_keys", lambda _path: (trusted,)
        )
    source = tmp_path / "index.json"
    source.write_text(json.dumps(index.to_dict()), encoding="utf-8")

    with pytest.raises(ManagedInstallError, match="application profile"):
        manager.update(source, allow_unsigned_local=not signed)
    assert not program.exists()
    assert validate_runtime_profile(paths.data_root, profile) is not None


def test_constructor_rejects_bound_root_conflict_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _documents_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("CARLINA_PROFILE", "workbench")
    program = tmp_path / "program"
    with pytest.raises(RuntimeProfileError, match="another runtime profile"):
        ManagedReleaseManager(paths, program_root=program)
    assert not program.exists()


@pytest.mark.parametrize(
    "policy", [ContentPolicy.PLAINTEXT_V1, ContentPolicy.INSTALLATION_ENVELOPE_V2]
)
def test_documents_launcher_profiles_and_existing_encrypted_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: ContentPolicy,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    paths = _documents_paths(tmp_path, monkeypatch)
    if policy is ContentPolicy.INSTALLATION_ENVELOPE_V2:
        marker = profile_marker_path(paths.data_root)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["content_policy"] = policy.value
        marker.write_text(json.dumps(payload), encoding="utf-8")
    binding = validate_runtime_profile(paths.data_root, RuntimeProfile.DOCUMENTS)
    assert binding is not None and binding.content_policy is policy

    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    layout.write_launchers(data_root=paths.data_root)
    for suffix, assignment in (
        ("", "export CARLINA_PROFILE=documents"),
        (".cmd", 'set "CARLINA_PROFILE=documents"'),
        (".ps1", "$env:CARLINA_PROFILE = 'documents'"),
    ):
        launcher = layout.root / f"carlina-documents{suffix}"
        content = launcher.read_text(encoding="utf-8")
        assert assignment in content
        if suffix == ".cmd":
            assert (
                '"%CARLINA_PYTHON%" -X utf8 -I '
                '"%~dp0state\\managed-launcher.py" '
                "--entry-module carlina.documents"
            ) in content
            assert "--entry-module carlina.documents" in content
            assert ' --data-root-file "%~dp0state\\data-root"' in content
            assert " -- %*" in content
            assert 'set /p "CARLINA_HOME=' not in content
        else:
            assert " -I -m carlina.documents" in content
        if suffix == ".ps1":
            assert "-Raw -Encoding UTF8" in content
            assert " -X utf8 -I -m" in content
        assert "data-root" in content
        assert "PYTHONNOUSERSITE" in content
        assert not (layout.root / f"carlina{suffix}").exists()
    assert layout.data_root_file.read_text(encoding="utf-8").strip() == str(
        paths.data_root
    )
    assert (
        json.loads(layout.metadata_file.read_text(encoding="utf-8"))[
            "application_profile"
        ]
        == "documents"
    )


def test_persisted_launcher_runs_from_older_runtime_without_launcher_module(
    tmp_path: Path,
) -> None:
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    version = "2.5.24"
    runtime = layout.version_dir(version) / "env"
    venv.EnvBuilder(with_pip=False).create(runtime)
    layout.activate(version)

    site_packages = (
        runtime / "Lib" / "site-packages"
        if os.name == "nt"
        else runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    fake_carlina = site_packages / "carlina"
    fake_carlina.mkdir(parents=True, exist_ok=True)
    (fake_carlina / "__init__.py").write_text("", encoding="utf-8")
    (fake_carlina / "documents.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "print(json.dumps({\n"
        "    'target': 'documents',\n"
        "    'home': os.environ.get('CARLINA_HOME'),\n"
        "    'argv': sys.argv,\n"
        "}))\n",
        encoding="utf-8",
    )
    (fake_carlina / "bootstrap_manager.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "print(json.dumps({\n"
        "    'target': 'bootstrap_manager',\n"
        "    'home': os.environ.get('CARLINA_HOME'),\n"
        "    'argv': sys.argv,\n"
        "}))\n",
        encoding="utf-8",
    )
    assert not (fake_carlina / "managed_launcher.py").exists()

    data_root = tmp_path / "Żółć %! documents data"
    bind_runtime_profile(data_root, RuntimeProfile.DOCUMENTS)
    layout.write_launchers(
        data_root=data_root,
        application_profile=RuntimeProfile.DOCUMENTS,
    )

    launcher_script = layout.state_dir / "managed-launcher.py"
    canonical_source = (
        files("carlina").joinpath("managed_launcher.py").read_text(encoding="utf-8")
    )
    assert launcher_script.read_text(encoding="utf-8") == canonical_source
    command = (layout.root / "carlina-documents.cmd").read_text(encoding="utf-8")
    assert (
        '"%~dp0state\\managed-launcher.py" --entry-module carlina.documents' in command
    )
    assert ' --data-root-file "%~dp0state\\data-root" -- %*' in command

    runtime_python = (
        runtime / "Scripts" / "python.exe"
        if os.name == "nt"
        else runtime / "bin" / "python"
    )
    for entry_module, target in (
        ("carlina.documents", "documents"),
        ("carlina.bootstrap_manager", "bootstrap_manager"),
    ):
        child_arguments = ["value with spaces", "Żółć %!"]
        completed = subprocess.run(
            [
                str(runtime_python),
                "-X",
                "utf8",
                "-I",
                str(launcher_script),
                "--entry-module",
                entry_module,
                "--data-root-file",
                str(layout.data_root_file),
                "--",
                *child_arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == {
            "target": target,
            "home": str(data_root),
            "argv": [entry_module, *child_arguments],
        }


def test_launcher_rejects_bound_root_conflict_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    paths = _documents_paths(tmp_path, monkeypatch)
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    with pytest.raises(RuntimeProfileError, match="another runtime profile"):
        layout.write_launchers(
            data_root=paths.data_root, application_profile="workbench"
        )
    assert not layout.root.exists()


def test_manager_rejects_existing_workbench_program_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    layout.write_launchers(application_profile=RuntimeProfile.WORKBENCH)
    before = layout.metadata_file.read_bytes()
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    paths = _documents_paths(tmp_path, monkeypatch)

    with pytest.raises(ManagedInstallError, match="another application profile"):
        ManagedReleaseManager(paths, program_root=layout.root)
    assert layout.metadata_file.read_bytes() == before
    assert not (layout.root / "carlina-documents").exists()


@pytest.mark.parametrize("initialize", [False, True])
def test_runtime_profile_mismatch_precedes_target_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialize: bool,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    paths = _documents_paths(tmp_path, monkeypatch)
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    runtime = layout.version_dir("1.1.0") / "env"
    python = runtime / ("python.exe" if os.name == "nt" else "bin/python")
    pixi = runtime / "bin" / ("pixi.exe" if os.name == "nt" else "pixi")
    python.parent.mkdir(parents=True)
    pixi.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"python")
    pixi.write_bytes(b"pixi")
    package_records = runtime / "conda-meta"
    package_records.mkdir()
    (package_records / "pixi-0.81.0-h1.json").write_text(
        json.dumps(
            {
                "name": "pixi",
                "version": "0.81.0",
                "files": ["bin/pixi.exe" if os.name == "nt" else "bin/pixi"],
            }
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        pixi.chmod(0o755)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command == [str(pixi), "--version"]:
            output = "pixi 0.81.0\n"
        elif command[1:3] == ["-I", "-c"]:
            output = "workbench\n"
        else:
            output = "1.1.0\n"
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    monkeypatch.setattr(managed_install, "_run_checked", fake_run)
    with pytest.raises(
        ManagedInstallError, match="application profile is incompatible"
    ):
        managed_install._verify_target_runtime(
            layout,
            paths,
            "1.1.0",
            initialize_data=initialize,
            application_profile=RuntimeProfile.DOCUMENTS,
        )
    assert len(commands) == 3
    assert commands[-1][1:3] == ["-I", "-c"]


def test_target_environment_pins_documents_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", "workbench")
    paths = _documents_paths(tmp_path, monkeypatch)
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    environment = managed_install._target_environment(
        paths, layout, "1.1.0", application_profile=RuntimeProfile.DOCUMENTS
    )
    assert environment["CARLINA_PROFILE"] == "documents"
    assert environment["CARLINA_HOME"] == str(paths.data_root)


def _descriptor_path(layout: ManagedInstallationLayout, version: str) -> Path:
    relative = (
        "Lib/site-packages/carlina/distribution.json"
        if os.name == "nt"
        else "lib/python3.14/site-packages/carlina/distribution.json"
    )
    return layout.version_dir(version) / "env" / relative


def _documents_descriptor() -> dict[str, object]:
    return {
        "format": "carlina.application-distribution",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": "a" * 40,
        "source_date_epoch": 1_760_000_000,
        "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
    }


def test_static_profile_fence_accepts_documents_and_legacy_workbench(
    tmp_path: Path,
) -> None:
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    descriptor = _descriptor_path(layout, "1.1.0")
    descriptor.parent.mkdir(parents=True)
    managed_install._verify_unpacked_application_profile(
        layout, "1.1.0", RuntimeProfile.WORKBENCH
    )
    descriptor.write_text(json.dumps(_documents_descriptor()), encoding="utf-8")
    managed_install._verify_unpacked_application_profile(
        layout, "1.1.0", RuntimeProfile.DOCUMENTS
    )
    with pytest.raises(
        ManagedInstallError, match="application profile is incompatible"
    ):
        managed_install._verify_unpacked_application_profile(
            layout, "1.1.0", RuntimeProfile.WORKBENCH
        )


@pytest.mark.parametrize("invalid", ["malformed", "oversized", "symlink"])
def test_static_profile_fence_rejects_unsafe_descriptors(
    tmp_path: Path, invalid: str
) -> None:
    layout = ManagedInstallationLayout.resolve(tmp_path / "program")
    descriptor = _descriptor_path(layout, "1.1.0")
    descriptor.parent.mkdir(parents=True)
    if invalid == "symlink":
        target = tmp_path / "external.json"
        target.write_text(json.dumps(_documents_descriptor()), encoding="utf-8")
        descriptor.symlink_to(target)
    else:
        descriptor.write_bytes(b"{" if invalid == "malformed" else b"x" * 4097)
    with pytest.raises((ManagedInstallError, RuntimeProfileError)):
        managed_install._verify_unpacked_application_profile(
            layout, "1.1.0", RuntimeProfile.DOCUMENTS
        )


def test_documents_rollback_keeps_profile_and_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    paths = _documents_paths(tmp_path, monkeypatch)
    manager = ManagedReleaseManager(paths, program_root=tmp_path / "program")
    layout = manager.layout
    layout.ensure()
    prior = "1.0.0"
    current = "1.1.0"
    python = (
        layout.version_dir(prior)
        / "env"
        / ("python.exe" if os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic python")
    descriptor = _descriptor_path(layout, prior)
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(json.dumps(_documents_descriptor()), encoding="utf-8")
    layout.version_dir(current).mkdir(parents=True)
    layout.activate(current)
    layout.receipt_file(current).write_text(
        json.dumps(
            {
                "format": "carlina.release-receipt",
                "schema_version": 3,
                "version": current,
                "previous_version": prior,
                "application_profile": "documents",
                "database": {"rollback": "compatible"},
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        managed_install,
        "_acquire_runtime_maintenance_lock",
        lambda _paths: SimpleNamespace(release=lambda: None),
    )
    monkeypatch.setattr(managed_install, "_assert_runtime_stopped", lambda _paths: None)
    monkeypatch.setattr(
        managed_install,
        "_verify_target_runtime",
        lambda *_args, **kwargs: calls.append(kwargs),
    )

    result = manager.rollback()

    assert result.active_version == prior
    assert layout.current_version() == prior
    assert len(calls) == 2
    assert all(
        call["application_profile"] is RuntimeProfile.DOCUMENTS for call in calls
    )
    assert "-m carlina.documents" in (layout.root / "carlina-documents").read_text(
        encoding="utf-8"
    )
    history = list(layout.history_dir.glob("rollback-*.json"))
    assert len(history) == 1
    assert (
        json.loads(history[0].read_text(encoding="utf-8"))["application_profile"]
        == "documents"
    )


@pytest.mark.parametrize("route", ["update", "adopt"])
@pytest.mark.parametrize("matching_descriptor", [False, True])
def test_documents_no_data_route_fences_profile_before_isolated_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    matching_descriptor: bool,
) -> None:
    """A signed no-data pack cannot run target code before static identity proof."""

    monkeypatch.setenv("CARLINA_PROFILE", "documents")
    paths = _documents_paths(tmp_path, monkeypatch)
    paths.ensure_directories()
    paths.write_storage_manifest()
    manager = ManagedReleaseManager(
        paths, program_root=tmp_path / "program", platform_tag="linux-64"
    )
    artifact = tmp_path / "carlina-1.1.0-linux-64.sh"
    artifact.write_bytes(b"synthetic signed pack")
    payload = {
        "format": "carlina.release-index",
        "schema_version": 2,
        "channel": "stable",
        "generated_at": "2026-10-04T00:00:00Z",
        "application_profile": "documents",
        "releases": [
            {
                "version": "1.1.0",
                "published_at": "2026-10-04T00:00:00Z",
                "addon_api_version": "1.0",
                "database": {
                    "known_revisions": ["rev1"],
                    "heads": ["rev1"],
                    "rollback": "compatible",
                },
                "artifacts": [
                    {
                        "platform": "linux-64",
                        "kind": "pixi-pack-executable",
                        "filename": artifact.name,
                        "url": artifact.name,
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        "size": artifact.stat().st_size,
                    }
                ],
                "execution": {
                    "data_impact": "none",
                    "migration": {"mode": "none"},
                    "bootstrap": {"mode": "none"},
                    "rollback": {"mode": "pointer_only"},
                    "recovery": {"automatic_coverage": "none"},
                    "probe": {
                        "protocol": "carlina.managed-release-probe",
                        "schema_version": 1,
                        "isolation": "host_filesystem_and_network_denied_v1",
                    },
                },
            }
        ],
        "signatures": [],
    }
    private_key = Ed25519PrivateKey.generate()
    index = sign_release_index(parse_release_index(payload), private_key)
    key = trusted_key_record(private_key)
    trusted = TrustedReleaseKey(
        key_id=key["key_id"],
        algorithm=key["algorithm"],
        public_key=key["public_key"],
        channels=tuple(key["channels"]),
    )
    monkeypatch.setattr(managed_install, "load_trusted_keys", lambda _path: (trusted,))
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index.to_dict()), encoding="utf-8")
    capability = ManagedProbeCapability(
        available=True,
        backend="linux-bwrap-v1",
        code="available",
        detail="synthetic capability",
        executable="/usr/bin/bwrap",
        version="0.11.0",
        executable_sha256="b" * 64,
    )
    monkeypatch.setattr(
        managed_install, "detect_managed_probe_capability", lambda _platform: capability
    )
    monkeypatch.setattr(
        managed_install.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=16 * 1024**3, used=8 * 1024**3, free=8 * 1024**3
        ),
    )

    target_dir = manager.layout.version_dir("1.1.0")

    def seed_target() -> None:
        python = (
            target_dir / "env" / ("python.exe" if os.name == "nt" else "bin/python")
        )
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"synthetic python")
        if matching_descriptor:
            descriptor = _descriptor_path(manager.layout, "1.1.0")
            descriptor.parent.mkdir(parents=True, exist_ok=True)
            descriptor.write_text(json.dumps(_documents_descriptor()), encoding="utf-8")

    if route == "update":
        monkeypatch.setattr(
            managed_install,
            "_unpack_runtime",
            lambda _artifact, _destination, **_kwargs: seed_target(),
        )
    else:
        seed_target()

    probe_calls: list[dict[str, object]] = []

    def isolated_probe(**kwargs: object) -> ManagedProbeAttestation:
        probe_calls.append(kwargs)
        return ManagedProbeAttestation(
            {
                "format": "carlina.managed-release-probe-attestation",
                "schema_version": 1,
                "status": "passed",
                "protocol": "carlina.managed-release-probe",
                "probe_schema_version": 1,
                "isolation": "host_filesystem_and_network_denied_v1",
                "backend": "linux-bwrap-v1",
                "target_version": "1.1.0",
                "policy_sha256": "c" * 64,
            }
        )

    monkeypatch.setattr(managed_install, "run_managed_release_probe", isolated_probe)
    monkeypatch.setattr(
        managed_install,
        "_verify_target_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "no-data route executed direct target probe"
        ),
    )
    monkeypatch.setattr(
        managed_install,
        "_run_checked",
        lambda *_args, **_kwargs: pytest.fail("no-data route ran target subprocess"),
    )

    def execute():
        if route == "update":
            return manager.update(index_path)
        return manager.adopt_installed_release(index_path, artifact, target_dir)

    if not matching_descriptor:
        with pytest.raises(
            ManagedInstallError, match="application profile is incompatible"
        ):
            execute()
        assert probe_calls == []
        assert manager.layout.current_version() is None
        return

    result = execute()
    assert result.recovery_plan.selected_coverage is RecoveryCoverage.NONE
    assert manager.layout.current_version() == "1.1.0"
    assert len(probe_calls) == 1
    assert probe_calls[0]["capability"] is capability
    receipt = manager.layout.read_receipt("1.1.0")
    assert receipt["application_profile"] == "documents"
    assert (
        receipt["probe_attestation"]["isolation"]
        == "host_filesystem_and_network_denied_v1"
    )
