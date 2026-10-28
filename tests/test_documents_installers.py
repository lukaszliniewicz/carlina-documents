from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iarf_assistant.core.release_manifest import (
    ReleaseManifestError,
    ReleaseSignatureError,
    parse_release_index,
)
from scripts.build_release_index import build_release_index
from scripts.render_managed_installers import (
    InstallerRenderError,
    _application_profile_probe,  # pyright: ignore[reportPrivateUsage]
    _powershell_installer,  # pyright: ignore[reportPrivateUsage]
    _shell_installer,  # pyright: ignore[reportPrivateUsage]
    render_installers,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _signed_index(
    tmp_path: Path,
    *,
    profile: str,
    platforms: tuple[str, ...],
) -> tuple[Path, Path]:
    metadata_paths: list[str | os.PathLike[str]] = []
    for platform in platforms:
        is_windows = platform.startswith("win-")
        suffix = ".zip" if is_windows else ".sh"
        metadata: dict[str, Any] = {
            "format": "carlina.managed-artifact",
            "schema_version": 1,
            "version": "1.2.3",
            "platform": platform,
            "kind": "pixi-pack-bundle" if is_windows else "pixi-pack-executable",
            "filename": f"carlina-1.2.3-{platform}{suffix}",
            "sha256": "b" * 64,
            "size": 456,
        }
        if profile == "documents":
            metadata["application_profile"] = profile
        metadata_path = tmp_path / f"{platform}.artifact.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        metadata_paths.append(metadata_path)

    payload, trust_payload = build_release_index(
        metadata_paths,
        channel="preview",
        base_url="https://downloads.example.test/preview/",
        rollback="restore_required",
        addon_api_version="1.0",
        published_at="2026-10-04T12:00:00Z",
        signing_key=Ed25519PrivateKey.generate(),
    )
    index_path = tmp_path / "index.json"
    trust_path = tmp_path / "trust.json"
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    assert trust_payload is not None
    trust_path.write_text(json.dumps(trust_payload), encoding="utf-8")
    return index_path, trust_path


def _render(
    tmp_path: Path,
    *,
    profile: str,
    platforms: tuple[str, ...],
) -> tuple[list[Path], Path, Path]:
    index_path, trust_path = _signed_index(
        tmp_path, profile=profile, platforms=platforms
    )
    outputs = render_installers(
        index_path,
        trust_path,
        index_url="https://downloads.example.test/preview/index.json",
        output_dir=tmp_path / "installers",
    )
    return outputs, index_path, trust_path


def _valid_distribution_bytes() -> bytes:
    return json.dumps(
        {
            "format": "carlina.application-distribution",
            "schema_version": 1,
            "application_profile": "documents",
            "source_revision": "a" * 40,
            "source_date_epoch": 1,
            "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
        }
    ).encode("utf-8")


def _probe_tree(
    tmp_path: Path,
    *,
    descriptor_bytes: bytes | None,
    symlink: str | None = None,
) -> tuple[Path, Path, tuple[Path, Path]]:
    environment_root = tmp_path / "version-environment"
    if os.name == "nt":
        site_packages = environment_root / "Lib" / "site-packages"
    else:
        site_packages = environment_root / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True)
    site_marker = tmp_path / "sitecustomize-imported"
    package_marker = tmp_path / "carlina-imported"
    site_packages.joinpath("sitecustomize.py").write_text(
        f"open({str(site_marker)!r}, 'w', encoding='utf-8').write('imported')\n",
        encoding="utf-8",
    )

    package = site_packages / "carlina"
    if symlink == "package":
        package_target = site_packages / "carlina-target"
        package_target.mkdir()
        try:
            package.symlink_to(package_target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Directory symlinks are unavailable: {exc}")
        package_directory = package_target
    else:
        package.mkdir()
        package_directory = package
    package_directory.joinpath("__init__.py").write_text(
        f"open({str(package_marker)!r}, 'w', encoding='utf-8').write('imported')\n",
        encoding="utf-8",
    )

    descriptor = package_directory / "distribution.json"
    if symlink == "descriptor":
        if descriptor_bytes is not None:
            descriptor_target = tmp_path / "outside-distribution.json"
            descriptor_target.write_bytes(descriptor_bytes)
            try:
                descriptor.symlink_to(descriptor_target)
            except OSError as exc:
                pytest.skip(f"File symlinks are unavailable: {exc}")
    elif descriptor_bytes is not None:
        descriptor.write_bytes(descriptor_bytes)
    return environment_root, site_packages, (site_marker, package_marker)


def _run_profile_probe(
    tmp_path: Path,
    *,
    descriptor_bytes: bytes | None,
    expected_profile: str,
    ambient_profile: str,
    symlink: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], tuple[Path, Path]]:
    environment_root, site_packages, markers = _probe_tree(
        tmp_path,
        descriptor_bytes=descriptor_bytes,
        symlink=symlink,
    )
    wrapper = (
        "import sys\n"
        "site_packages = sys.argv.pop(1)\n"
        "probe_code = sys.argv.pop(1)\n"
        "sys.path.insert(0, site_packages)\n"
        "exec(compile(probe_code, '<application-profile-probe>', 'exec'))\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            wrapper,
            str(site_packages),
            _application_profile_probe(),
            str(environment_root),
            expected_profile,
        ],
        env={
            **os.environ,
            "CARLINA_PROFILE": ambient_profile,
            "PYTHONPATH": str(site_packages),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    return result, markers


def test_application_profile_probe_is_stdlib_only_and_accepts_documents_descriptor(
    tmp_path: Path,
) -> None:
    probe = _application_profile_probe()
    imported_modules = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(ast.parse(probe))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(ast.parse(probe))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported_modules == {"json", "os", "re", "stat", "sys"}

    result, markers = _run_profile_probe(
        tmp_path,
        descriptor_bytes=_valid_distribution_bytes(),
        expected_profile="documents",
        ambient_profile="workbench",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert all(not marker.exists() for marker in markers)


def test_application_profile_probe_accepts_legacy_workbench_without_descriptor(
    tmp_path: Path,
) -> None:
    result, markers = _run_profile_probe(
        tmp_path,
        descriptor_bytes=None,
        expected_profile="workbench",
        ambient_profile="documents",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert all(not marker.exists() for marker in markers)


@pytest.mark.parametrize(
    ("case", "expected_profile", "ambient_profile", "symlink"),
    [
        ("absent-documents", "documents", "documents", None),
        ("malformed", "documents", "workbench", None),
        ("duplicate-key", "documents", "workbench", None),
        ("oversized", "documents", "workbench", None),
        ("mismatch", "workbench", "documents", None),
        ("package-symlink", "documents", "documents", "package"),
        ("descriptor-symlink", "documents", "documents", "descriptor"),
        ("unsupported-expected", "documents-v9", "documents", None),
        ("wrong-format", "documents", "documents", None),
        ("schema-bool", "documents", "documents", None),
        ("wrong-profile", "documents", "documents", None),
        ("bad-revision", "documents", "documents", None),
        ("epoch-bool", "documents", "documents", None),
        ("epoch-zero", "documents", "documents", None),
        ("wrong-addons", "documents", "documents", None),
        ("extra-key", "documents", "documents", None),
    ],
)
def test_application_profile_probe_rejects_invalid_or_mismatched_state_without_imports(
    tmp_path: Path,
    case: str,
    expected_profile: str,
    ambient_profile: str,
    symlink: str | None,
) -> None:
    descriptor: bytes | None = _valid_distribution_bytes()
    if case == "absent-documents":
        descriptor = None
    elif case == "malformed":
        descriptor = b"not a JSON descriptor"
    elif case == "duplicate-key":
        descriptor = descriptor[:-1] + b',"format":"carlina.application-distribution"}'
    elif case == "oversized":
        descriptor += b" " * (4097 - len(descriptor))
    elif case in {
        "wrong-format",
        "schema-bool",
        "wrong-profile",
        "bad-revision",
        "epoch-bool",
        "epoch-zero",
        "wrong-addons",
        "extra-key",
    }:
        parsed: dict[str, Any] = json.loads(descriptor)
        if case == "wrong-format":
            parsed["format"] = "other"
        elif case == "schema-bool":
            parsed["schema_version"] = True
        elif case == "wrong-profile":
            parsed["application_profile"] = "workbench"
        elif case == "bad-revision":
            parsed["source_revision"] = "A" * 40
        elif case == "epoch-bool":
            parsed["source_date_epoch"] = True
        elif case == "epoch-zero":
            parsed["source_date_epoch"] = 0
        elif case == "wrong-addons":
            parsed["addon_ids"] = ["translator", "sentence_segmenter", "doc_processor"]
        else:
            parsed["unexpected"] = "field"
        descriptor = json.dumps(parsed).encode("utf-8")

    result, markers = _run_profile_probe(
        tmp_path,
        descriptor_bytes=descriptor,
        expected_profile=expected_profile,
        ambient_profile=ambient_profile,
        symlink=symlink,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Application profile validation failed.\n"
    assert all(not marker.exists() for marker in markers)


def test_signed_documents_index_renders_profile_specific_installers_and_sidecars(
    tmp_path: Path,
) -> None:
    outputs, _index_path, _trust_path = _render(
        tmp_path,
        profile="documents",
        platforms=("win-64", "linux-64", "osx-arm64"),
    )
    installers = {
        path.name: path for path in outputs if not path.name.endswith(".sha256")
    }

    expected_names = {
        "install-carlina-documents-win-64.ps1",
        "install-carlina-documents-linux-64.sh",
        "install-carlina-documents-osx-arm64.sh",
    }
    assert set(installers) == expected_names
    for name, installer in installers.items():
        sidecar = installer.with_suffix(installer.suffix + ".sha256")
        assert sidecar in outputs
        expected_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
        assert sidecar.read_text(encoding="ascii") == f"{expected_hash}  {name}\n"

    powershell = installers["install-carlina-documents-win-64.ps1"].read_text(
        encoding="utf-8"
    )
    assert (
        "[string]$ProgramRoot = (Join-Path $env:LOCALAPPDATA 'Programs\\CarlinaDocuments')"
        in powershell
    )
    assert "$env:CARLINA_PROFILE = 'documents'" in powershell
    ps_install_arguments = "'-I', '-m', 'carlina.documents', 'install'"
    assert ps_install_arguments in powershell
    assert "& python " not in powershell
    assert "Join-Path $ProgramRoot 'carlina-documents.cmd'" in powershell
    assert "Carlina Documents $version is installed" in powershell
    assert powershell.index("$env:CARLINA_PROFILE") < powershell.index(
        ps_install_arguments
    )
    assert "@('-I', '-S', $ProbePath, $EnvironmentRoot, 'documents')" in powershell
    assert "-I -S -c" not in powershell
    ps_guard_start = powershell.index("function Assert-ApplicationProfile(")
    ps_guard_end = powershell.index("function Expand-VerifiedWindowsBundle(")
    ps_guard = powershell[ps_guard_start:ps_guard_end]
    assert "$env:CARLINA_PROFILE" not in ps_guard
    encoded_probe = ps_guard.split("[Convert]::FromBase64String('", 1)[1].split(
        "')", 1
    )[0]
    assert (
        base64.b64decode(encoded_probe, validate=True).decode("utf-8")
        == _application_profile_probe()
    )
    ps_guard_call = powershell.index(
        "Assert-ApplicationProfile $python (Join-Path $versionDir 'env') $profileProbePath"
    )
    assert ps_guard_call < powershell.index(ps_install_arguments)

    linux = installers["install-carlina-documents-linux-64.sh"].read_text(
        encoding="utf-8"
    )
    assert 'DOCUMENTS_PROGRAM_ROOT="$HOME/.local/lib/carlina-documents"' in linux
    assert "export CARLINA_PROFILE=documents" in linux
    assert '"$PYTHON" -I -m carlina.documents install' in linux
    assert "python3 " not in linux
    assert "command -v python" not in linux
    assert '"$PROGRAM_ROOT/carlina-documents" init' in linux
    assert (
        'ln -s "$PROGRAM_ROOT/carlina-documents" "$USER_BIN/carlina-documents"' in linux
    )
    assert linux.index("export CARLINA_PROFILE") < linux.index('"$PYTHON" -I -m')
    shell_guard_start = linux.index('"$PYTHON" -I -S -c')
    shell_guard_end = linux.index('"$PYTHON" -I -m carlina.documents install')
    shell_guard = linux[shell_guard_start:shell_guard_end]
    assert '"$VERSION_DIR/env" documents' in shell_guard
    assert "$CARLINA_PROFILE" not in shell_guard
    shell_executable = shutil.which("sh")
    if shell_executable is not None:
        for shell_name in (
            "install-carlina-documents-linux-64.sh",
            "install-carlina-documents-osx-arm64.sh",
        ):
            syntax = subprocess.run(
                [shell_executable, "-n", str(installers[shell_name])],
                check=False,
                capture_output=True,
                text=True,
            )
            assert syntax.returncode == 0, syntax.stderr

    macos = installers["install-carlina-documents-osx-arm64.sh"].read_text(
        encoding="utf-8"
    )
    assert (
        'DOCUMENTS_PROGRAM_ROOT="$HOME/Library/Application Support/'
        'CarlinaDocuments/Program"' in macos
    )


def test_workbench_installers_keep_legacy_profile_names_paths_and_module(
    tmp_path: Path,
) -> None:
    outputs, index_path, _trust_path = _render(
        tmp_path, profile="workbench", platforms=("win-64", "linux-64")
    )
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert "application_profile" not in index_payload

    installers = {
        path.name: path for path in outputs if not path.name.endswith(".sha256")
    }
    assert set(installers) == {
        "install-carlina-win-64.ps1",
        "install-carlina-linux-64.sh",
    }
    powershell = installers["install-carlina-win-64.ps1"].read_text(encoding="utf-8")
    assert (
        "[string]$ProgramRoot = (Join-Path $env:LOCALAPPDATA 'Programs\\Carlina')"
        in powershell
    )
    assert "'-I', '-m', 'carlina.bootstrap_manager', 'install'" in powershell
    assert "Join-Path $ProgramRoot 'carlina.cmd'" in powershell
    assert "$env:CARLINA_PROFILE" not in powershell

    shell = installers["install-carlina-linux-64.sh"].read_text(encoding="utf-8")
    assert "PROGRAM_ROOT=${CARLINA_PROGRAM_ROOT:-$HOME/.local/lib/carlina}" in shell
    assert '"$PYTHON" -I -m carlina.bootstrap_manager install' in shell
    assert '"$PROGRAM_ROOT/carlina" init' in shell
    assert "CARLINA_PROFILE" not in shell


@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_powershell_native_runner_preserves_stderr_and_exit_codes(
    tmp_path: Path, profile: str
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable.")
    outputs, _index, _trust = _render(tmp_path, profile=profile, platforms=("win-64",))
    installer = next(path for path in outputs if path.suffix == ".ps1")
    script = installer.read_text(encoding="utf-8")
    runner = script[
        script.index("function Invoke-CheckedNative(") : script.index(
            "function Assert-FileHash("
        )
    ]
    probe = tmp_path / "native-probe.ps1"
    probe.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        + runner
        + r"""
if ($env:OS -eq 'Windows_NT') {
    $native = $env:ComSpec
    $successArgs = @('/d', '/c', 'echo fixture-progress 1>&2 & exit /b 0')
    $failureArgs = @('/d', '/c', 'echo fixture-failure 1>&2 & exit /b 7')
} else {
    $native = '/bin/sh'
    $successArgs = @('-c', 'printf "fixture-progress\n" >&2; exit 0')
    $failureArgs = @('-c', 'printf "fixture-failure\n" >&2; exit 7')
}
$log = Join-Path $env:CARLINA_INSTALLER_TEST_OUTPUT 'success.log'
Invoke-CheckedNative -FilePath $native -Arguments $successArgs -FailureMessage 'success lost' *> $log
if ((Get-Content -LiteralPath $log -Raw) -notmatch 'fixture-progress') { throw 'stderr lost' }
if ($ErrorActionPreference -ne 'Stop') { throw 'caller policy changed' }
$failed = $false
try {
    Invoke-CheckedNative -FilePath $native -Arguments $failureArgs -FailureMessage 'expected failure' *> (Join-Path $env:CARLINA_INSTALLER_TEST_OUTPUT 'failure.log')
} catch {
    if ($_.Exception.Message -notmatch 'Exit code: 7') { throw }
    $failed = $true
}
if (-not $failed -or $ErrorActionPreference -ne 'Stop') { throw 'failure or caller policy lost' }
$missingFailed = $false
try {
    Invoke-CheckedNative -FilePath (Join-Path $env:CARLINA_INSTALLER_TEST_OUTPUT 'missing.exe') -Arguments @() -FailureMessage 'missing command'
} catch { $missingFailed = $true }
if (-not $missingFailed) { throw 'missing command accepted' }
Invoke-CheckedNative -FilePath $native -Arguments $successArgs -Interactive -FailureMessage 'interactive success lost' *> (Join-Path $env:CARLINA_INSTALLER_TEST_OUTPUT 'interactive.log')
if ($ErrorActionPreference -ne 'Stop') { throw 'interactive caller policy changed' }
$interactiveFailed = $false
try {
    Invoke-CheckedNative -FilePath $native -Arguments $failureArgs -Interactive -FailureMessage 'interactive failure' *> (Join-Path $env:CARLINA_INSTALLER_TEST_OUTPUT 'interactive-failure.log')
} catch {
    if ($_.Exception.Message -notmatch 'Exit code: 7') { throw }
    $interactiveFailed = $true
}
if (-not $interactiveFailed -or $ErrorActionPreference -ne 'Stop') { throw 'interactive failure lost' }
Write-Output 'native stderr and exit-code checks passed'
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        env={**os.environ, "CARLINA_INSTALLER_TEST_OUTPUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "native stderr and exit-code checks passed" in result.stdout


@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_shell_installer_requires_bash_before_creating_program_root(
    tmp_path: Path, profile: str
) -> None:
    shell = shutil.which("sh")
    uname = shutil.which("uname")
    if shell is None or uname is None:
        pytest.skip("POSIX tools are unavailable.")
    outputs, _index, _trust = _render(
        tmp_path, profile=profile, platforms=("linux-64",)
    )
    installer = next(path for path in outputs if path.suffix == ".sh")
    restricted_bin = tmp_path / "restricted-bin"
    restricted_bin.mkdir()
    (restricted_bin / "uname").symlink_to(uname)
    program_root = tmp_path / "uncreated-program"
    result = subprocess.run(
        [shell, str(installer), "--program-root", str(program_root), "--skip-init"],
        env={**os.environ, "PATH": str(restricted_bin)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Bash is required to unpack the runtime bundle." in result.stderr
    assert not program_root.exists()


@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_shell_installer_unpacks_bash_bundle_from_posix_shell(
    tmp_path: Path, profile: str
) -> None:
    shell = shutil.which("sh")
    bash = shutil.which("bash")
    if shell is None or bash is None:
        pytest.skip("POSIX shell or Bash is unavailable.")
    outputs, _index, _trust = _render(
        tmp_path, profile=profile, platforms=("linux-64",)
    )
    installer = next(path for path in outputs if path.suffix == ".sh")
    script = installer.read_text(encoding="utf-8")
    unpack = next(
        line.lstrip()
        for line in script.splitlines()
        if line.lstrip().startswith('if (cd "$VERSION_DIR" &&') and ">&2" not in line
    )
    artifact = tmp_path / "bundle.sh"
    artifact.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ -d "$TMPDIR" ]]\nprintf "%s" "$BASH_VERSION" > unpacked\n',
        encoding="utf-8",
    )
    destination = tmp_path / "runtime"
    destination.mkdir()
    scratch = tmp_path / "extract"
    scratch.mkdir()
    # A platform sh need not support the Bash syntax used by pixi-pack.
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()
    ambient_sh = ambient_bin / "sh"
    ambient_sh.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    ambient_sh.chmod(0o755)
    result = subprocess.run(
        [shell, "-c", unpack],
        env={
            **os.environ,
            "VERSION_DIR": str(destination),
            "ARTIFACT_PATH": str(artifact),
            "EXTRACT_TMP": str(scratch),
            "PATH": str(ambient_bin) + os.pathsep + os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "unpacked").read_text(encoding="utf-8")


def test_documents_shell_root_override_preserves_shell_metacharacters_and_newlines(
    tmp_path: Path,
) -> None:
    if shutil.which("sh") is None:
        pytest.skip("POSIX shell is not available for the bounded argument probe.")
    outputs, _index_path, _trust_path = _render(
        tmp_path, profile="documents", platforms=("linux-64",)
    )
    installer = next(
        path
        for path in outputs
        if path.suffix == ".sh" and not path.name.endswith(".sha256")
    )
    script = installer.read_text(encoding="utf-8")
    argument_parser = script.split("\nhash_file() {", maxsplit=1)[0]
    marker = tmp_path / "injection-marker"
    override = f"{tmp_path}/root ' ; touch {marker}\nsecond-line"
    parse_only_script = argument_parser + "\nprintf '%s' \"$PROGRAM_ROOT\"\n"

    result = subprocess.run(
        ["sh", "-c", parse_only_script, "installer", "--program-root", override],
        cwd=tmp_path,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.decode("utf-8") == override
    assert not marker.exists()

    powershell = _powershell_installer(
        version="1.2.3",
        channel="preview",
        index_url="https://downloads.example.test/preview/index.json",
        index_sha256="a" * 64,
        artifact_url="https://downloads.example.test/preview/artifact.zip",
        artifact_filename="artifact.zip",
        artifact_sha256="b" * 64,
        artifact_size=456,
        trust_b64="",
        application_profile="documents",
    )
    assert "[string]$ProgramRoot" in powershell
    assert "$ProgramRoot = [System.IO.Path]::GetFullPath($ProgramRoot)" in powershell
    assert override not in powershell


def test_documents_powershell_root_override_is_passed_as_path_data(
    tmp_path: Path,
) -> None:
    powershell_executable = shutil.which("pwsh") or shutil.which("powershell")
    if powershell_executable is None:
        pytest.skip("PowerShell is not available for the bounded argument probe.")

    outputs, _index_path, _trust_path = _render(
        tmp_path, profile="documents", platforms=("win-64",)
    )
    installer = next(path for path in outputs if path.suffix == ".ps1")
    parser_command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:CARLINA_TEST_INSTALLER,[ref]$tokens,[ref]$errors) | Out-Null; "
        "if($errors.Count){$errors | Out-String | Write-Error; exit 1}"
    )
    parser_result = subprocess.run(
        [powershell_executable, "-NoProfile", "-Command", parser_command],
        env={**os.environ, "CARLINA_TEST_INSTALLER": str(installer)},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert parser_result.returncode == 0, parser_result.stderr

    blocked_parent = tmp_path / "regular-file"
    blocked_parent.write_text("synthetic blocker", encoding="utf-8")
    marker = tmp_path / "injection-marker"
    override = (
        f"{blocked_parent}/child ' ; Set-Content -LiteralPath '{marker}' injected"
        "\nsecond-line"
    )

    result = subprocess.run(
        [
            powershell_executable,
            "-NoProfile",
            "-File",
            str(installer),
            "-ProgramRoot",
            override,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize("bad_profile", ["", "documents-v9", None, 3, True])
def test_release_index_schema_rejects_malformed_application_profiles(
    tmp_path: Path,
    bad_profile: object,
) -> None:
    index_path, _trust_path = _signed_index(
        tmp_path, profile="workbench", platforms=("linux-64",)
    )
    payload: dict[str, Any] = json.loads(index_path.read_text(encoding="utf-8"))
    payload["application_profile"] = bad_profile

    with pytest.raises(ReleaseManifestError):
        parse_release_index(payload)


def test_documents_renderer_still_requires_a_valid_index_signature(
    tmp_path: Path,
) -> None:
    _outputs, index_path, trust_path = _render(
        tmp_path,
        profile="documents",
        platforms=("linux-64",),
    )
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["application_profile"] = "workbench"
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    output_dir = tmp_path / "tampered-installers"

    with pytest.raises(ReleaseSignatureError):
        render_installers(
            index_path,
            trust_path,
            index_url="https://downloads.example.test/preview/index.json",
            output_dir=output_dir,
        )
    assert not output_dir.exists()


@pytest.mark.parametrize("bad_profile", ["", "documents-v9", None, 3, True])
def test_script_renderers_reject_profiles_outside_runtime_enum(
    bad_profile: object,
) -> None:
    with pytest.raises(InstallerRenderError, match="application profile"):
        _shell_installer(
            version="1.2.3",
            channel="preview",
            index_url="https://downloads.example.test/preview/index.json",
            index_sha256="a" * 64,
            artifact_url="https://downloads.example.test/preview/artifact.sh",
            artifact_filename="artifact.sh",
            artifact_sha256="b" * 64,
            artifact_size=456,
            trust_b64="",
            application_profile=bad_profile,  # type: ignore[arg-type]
        )
