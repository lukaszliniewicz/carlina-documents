"""Fast contract tests for durable data-root runtime profile binding."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import stat
import time
from pathlib import Path

import pytest

from carlina.profile_binding import (
    bind_runtime_profile,
    profile_marker_path,
    read_runtime_profile,
    validate_runtime_profile,
)
from carlina.runtime_profiles import (
    ContentPolicy,
    RootProfileBinding,
    RuntimeProfile,
    RuntimeProfileError,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_validation_leaves_fresh_root_unwritten(
    tmp_path: Path, profile: RuntimeProfile
):
    root = tmp_path / profile.value

    assert validate_runtime_profile(root, profile) is None
    assert not root.exists()
    root.mkdir()
    assert validate_runtime_profile(root, profile) is None
    assert list(root.iterdir()) == []


def test_validation_checks_bound_and_legacy_roots_without_writes(tmp_path: Path):
    bound = tmp_path / "bound"
    binding = bind_runtime_profile(bound, RuntimeProfile.DOCUMENTS)
    bound_before = _snapshot(bound)

    assert validate_runtime_profile(bound, RuntimeProfile.DOCUMENTS) == binding
    with pytest.raises(RuntimeProfileError, match="another runtime profile"):
        validate_runtime_profile(bound, RuntimeProfile.WORKBENCH)
    assert _snapshot(bound) == bound_before

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "retained.db").write_bytes(b"synthetic retained database")
    legacy_before = _snapshot(legacy)
    legacy_binding = validate_runtime_profile(legacy, RuntimeProfile.WORKBENCH)
    assert legacy_binding is not None and legacy_binding.legacy
    with pytest.raises(RuntimeProfileError, match="Markerless existing"):
        validate_runtime_profile(legacy, RuntimeProfile.DOCUMENTS)
    assert _snapshot(legacy) == legacy_before


def _marker_bytes(
    profile: RuntimeProfile = RuntimeProfile.WORKBENCH,
    content_policy: str = ContentPolicy.INSTALLATION_ENVELOPE_V2.value,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "profile": profile.value,
            "content_policy": content_policy,
        }
    ).encode("utf-8")


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[str, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", None)
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _initializer_process(
    root: str,
    profile_value: str,
    start_barrier: object,
    result_queue: object,
) -> None:
    try:
        start_barrier.wait(timeout=10)  # type: ignore[attr-defined]
        result = bind_runtime_profile(Path(root), RuntimeProfile(profile_value))
    except RuntimeProfileError as error:
        result_queue.put(("error", str(error)))  # type: ignore[attr-defined]
    except BaseException as error:
        result_queue.put(("crash", type(error).__name__))  # type: ignore[attr-defined]
    else:
        result_queue.put(  # type: ignore[attr-defined]
            ("success", result.profile.value, result.legacy)
        )


def _run_concurrent_initializers(
    root: Path, profiles: tuple[RuntimeProfile, RuntimeProfile]
) -> list[tuple[object, ...]]:
    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_initializer_process,
            args=(str(root), profile.value, start_barrier, result_queue),
        )
        for profile in profiles
    ]
    results: list[tuple[object, ...]] = []
    deadline = time.monotonic() + 15
    try:
        for process in processes:
            process.start()
        for _ in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "Concurrent profile initialization exceeded its deadline."
                )
            results.append(result_queue.get(timeout=remaining))
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
            assert not process.is_alive()
            assert process.exitcode == 0
        return results
    except queue.Empty as error:
        raise AssertionError("Concurrent profile initialization timed out.") from error
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
        result_queue.close()
        result_queue.join_thread()


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_fresh_root_binds_each_supported_profile(
    tmp_path: Path, profile: RuntimeProfile
):
    root = tmp_path / profile.value
    expected_policy = (
        ContentPolicy.PLAINTEXT_V1
        if profile is RuntimeProfile.DOCUMENTS
        else ContentPolicy.INSTALLATION_ENVELOPE_V2
    )

    binding = bind_runtime_profile(root, profile)

    assert binding == RootProfileBinding(
        profile=profile,
        content_policy=expected_policy,
        legacy=False,
    )
    marker = profile_marker_path(root)
    assert marker == root / "state" / "runtime" / "profile.json"
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert document == {
        "schema_version": 1,
        "profile": profile.value,
        "content_policy": expected_policy.value,
    }
    assert read_runtime_profile(root) == binding


def test_missing_marker_read_does_not_create_the_root(tmp_path: Path):
    root = tmp_path / "missing"

    assert read_runtime_profile(root) is None
    assert not root.exists()


def test_empty_directory_scaffolding_does_not_prevent_fresh_binding(
    tmp_path: Path,
):
    root = tmp_path / "scaffold"
    (root / "empty" / "nested").mkdir(parents=True)
    (root / "state" / "runtime" / "empty").mkdir(parents=True)

    binding = bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert binding.legacy is False
    assert (root / "empty" / "nested").is_dir()
    assert (root / "state" / "runtime" / "empty").is_dir()


def test_valid_marker_bind_is_idempotent_without_changing_bytes_or_mtime(
    tmp_path: Path,
):
    root = tmp_path / "root"
    first = bind_runtime_profile(root, RuntimeProfile.WORKBENCH)
    marker = profile_marker_path(root)
    original_bytes = marker.read_bytes()
    original_mtime = marker.stat().st_mtime_ns

    second = bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert second == first
    assert marker.read_bytes() == original_bytes
    assert marker.stat().st_mtime_ns == original_mtime


def test_markerless_existing_root_is_read_only_legacy_for_workbench(tmp_path: Path):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "existing-content.db").write_bytes(b"synthetic existing bytes")
    (root / "empty-directory").mkdir()
    before = _snapshot(root)

    binding = bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert binding == RootProfileBinding(
        profile=RuntimeProfile.WORKBENCH,
        content_policy=ContentPolicy.INSTALLATION_ENVELOPE_V2,
        legacy=True,
    )
    assert read_runtime_profile(root) is None
    assert _snapshot(root) == before


def test_documents_profile_rejects_markerless_existing_root_without_writes(
    tmp_path: Path,
):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "existing-content.db").write_bytes(b"synthetic existing bytes")
    before = _snapshot(root)

    with pytest.raises(RuntimeProfileError, match="Markerless existing"):
        bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert _snapshot(root) == before
    assert read_runtime_profile(root) is None


def test_marker_appearing_during_freshness_inspection_wins_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "existing-content.db").write_bytes(b"synthetic existing bytes")
    marker = profile_marker_path(root)

    def publish_during_inspection(_root: Path, _marker_parent: Path) -> bool:
        marker.parent.mkdir(parents=True)
        marker.write_bytes(_marker_bytes(RuntimeProfile.DOCUMENTS))
        return False

    monkeypatch.setattr(
        "carlina.profile_binding._is_fresh_root", publish_during_inspection
    )

    binding = bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert binding == RootProfileBinding(
        profile=RuntimeProfile.DOCUMENTS,
        content_policy=ContentPolicy.INSTALLATION_ENVELOPE_V2,
        legacy=False,
    )


def test_mismatched_stored_profile_fails_without_mutating_marker_or_root(
    tmp_path: Path,
):
    root = tmp_path / "root"
    bind_runtime_profile(root, RuntimeProfile.WORKBENCH)
    marker = profile_marker_path(root)
    marker_bytes = marker.read_bytes()
    marker_mtime = marker.stat().st_mtime_ns
    before = _snapshot(root)

    with pytest.raises(RuntimeProfileError, match="already bound") as error:
        bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert "synthetic" not in str(error.value)
    assert _snapshot(root) == before
    assert marker.read_bytes() == marker_bytes
    assert marker.stat().st_mtime_ns == marker_mtime


@pytest.mark.parametrize(
    "payload",
    [
        b"{not-json",
        b'{"schema_version":1,"schema_version":1,"profile":"workbench",'
        b'"content_policy":"installation-envelope-v2"}',
        b" " * 4097,
        b'{"schema_version":1,"profile":"workbench",'
        b'"content_policy":"installation-envelope-v2","unexpected":0}',
        b'{"schema_version":1,"profile":"unknown",'
        b'"content_policy":"installation-envelope-v2"}',
        b'{"schema_version":1,"profile":"workbench","content_policy":"plaintext"}',
        b'{"schema_version":2,"profile":"workbench",'
        b'"content_policy":"installation-envelope-v2"}',
        b'{"schema_version":true,"profile":"workbench",'
        b'"content_policy":"installation-envelope-v2"}',
        b'{"schema_version":1,"profile":4,"content_policy":"installation-envelope-v2"}',
    ],
    ids=[
        "malformed-json",
        "duplicate-key",
        "oversize",
        "unknown-key",
        "unknown-profile",
        "unsupported-codec",
        "unsupported-version",
        "boolean-version",
        "wrong-profile-type",
    ],
)
def test_invalid_marker_always_fails_without_fallback_or_rewrite(
    tmp_path: Path, payload: bytes
):
    root = tmp_path / "root"
    marker = profile_marker_path(root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(payload)
    before = _snapshot(root)

    with pytest.raises(RuntimeProfileError):
        read_runtime_profile(root)
    with pytest.raises(RuntimeProfileError):
        bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert _snapshot(root) == before
    assert marker.read_bytes() == payload


def test_content_policy_names_are_explicit():
    assert list(ContentPolicy) == [
        ContentPolicy.INSTALLATION_ENVELOPE_V2,
        ContentPolicy.PLAINTEXT_V1,
    ]


def test_existing_encrypted_document_marker_retains_its_policy(tmp_path: Path):
    root = tmp_path / "encrypted-prototype"
    marker = profile_marker_path(root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(_marker_bytes(RuntimeProfile.DOCUMENTS))
    before = _snapshot(root)

    binding = bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert binding.content_policy is ContentPolicy.INSTALLATION_ENVELOPE_V2
    assert _snapshot(root) == before


def test_plaintext_workbench_marker_is_rejected_without_rewrite(tmp_path: Path):
    root = tmp_path / "invalid-workbench"
    marker = profile_marker_path(root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(_marker_bytes(content_policy=ContentPolicy.PLAINTEXT_V1.value))
    before = _snapshot(root)

    with pytest.raises(RuntimeProfileError, match="incompatible"):
        bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert _snapshot(root) == before


@pytest.mark.parametrize("layout", ["state", "runtime", "marker"])
def test_symlink_state_runtime_or_marker_is_rejected_without_escape(
    tmp_path: Path, layout: str
):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        if layout == "state":
            root.mkdir()
            (root / "state").symlink_to(outside, target_is_directory=True)
        elif layout == "runtime":
            (root / "state").mkdir(parents=True)
            (root / "state" / "runtime").symlink_to(outside, target_is_directory=True)
        else:
            marker_parent = root / "state" / "runtime"
            marker_parent.mkdir(parents=True)
            target = outside / "outside-profile.json"
            target.write_bytes(_marker_bytes())
            (marker_parent / "profile.json").symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(
            f"symlinks are unavailable on this platform: {type(error).__name__}"
        )

    before = _snapshot(outside)
    with pytest.raises(RuntimeProfileError):
        bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert _snapshot(outside) == before


@pytest.mark.parametrize("layout", ["root", "state", "runtime"])
def test_non_directory_ancestor_is_rejected(tmp_path: Path, layout: str):
    root = tmp_path / "root"
    if layout == "root":
        root.write_bytes(b"not a directory")
    elif layout == "state":
        root.mkdir()
        (root / "state").write_bytes(b"not a directory")
    else:
        (root / "state").mkdir(parents=True)
        (root / "state" / "runtime").write_bytes(b"not a directory")
    before = _snapshot(tmp_path)

    with pytest.raises(RuntimeProfileError):
        bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert _snapshot(tmp_path) == before


def test_symlink_data_root_is_resolved_once_and_marker_stays_under_target(
    tmp_path: Path,
):
    target = tmp_path / "actual-root"
    target.mkdir()
    alias = tmp_path / "root-alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(
            f"symlinks are unavailable on this platform: {type(error).__name__}"
        )

    binding = bind_runtime_profile(alias, RuntimeProfile.DOCUMENTS)

    assert binding.legacy is False
    assert profile_marker_path(alias) == target / "state" / "runtime" / "profile.json"
    assert profile_marker_path(alias).is_file()
    assert not (alias / "state" / "runtime" / "profile.json").is_symlink()


def test_exact_crash_temporary_file_at_marker_parent_is_ignored_and_preserved(
    tmp_path: Path,
):
    root = tmp_path / "root"
    marker_parent = profile_marker_path(root).parent
    marker_parent.mkdir(parents=True)
    leftover = marker_parent / ".profile.0123456789abcdef0123456789abcdef.tmp"
    leftover.write_bytes(b"crash-leftover")

    binding = bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert binding.legacy is False
    assert leftover.read_bytes() == b"crash-leftover"
    assert profile_marker_path(root).is_file()


@pytest.mark.parametrize("leftover_location", ["wrong-pattern", "wrong-parent"])
def test_non_owned_temporary_pattern_counts_as_existing_content(
    tmp_path: Path, leftover_location: str
):
    root = tmp_path / "root"
    marker_parent = profile_marker_path(root).parent
    marker_parent.mkdir(parents=True)
    if leftover_location == "wrong-pattern":
        leftover = marker_parent / ".profile.NOT-lowercase-or-uuid.tmp"
    else:
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        leftover = elsewhere / ".profile.0123456789abcdef0123456789abcdef.tmp"
    leftover.write_bytes(b"synthetic content")

    binding = bind_runtime_profile(root, RuntimeProfile.WORKBENCH)

    assert binding.legacy is True
    assert leftover.read_bytes() == b"synthetic content"
    assert read_runtime_profile(root) is None


def test_unsupported_hardlink_cleans_only_its_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    marker_parent = profile_marker_path(root).parent
    marker_parent.mkdir(parents=True)
    leftover = marker_parent / ".profile.0123456789abcdef0123456789abcdef.tmp"
    leftover.write_bytes(b"previous crash leftover")
    created_temporaries: list[Path] = []

    def unsupported_link(source: Path, target: Path) -> None:
        assert target == profile_marker_path(root)
        created_temporaries.append(source)
        assert source.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(source.stat().st_mode) & 0o077 == 0
        raise OSError("simulated hard-link limitation")

    monkeypatch.setattr("carlina.profile_binding.os.link", unsupported_link)

    with pytest.raises(RuntimeProfileError, match="Atomic runtime profile publication"):
        bind_runtime_profile(root, RuntimeProfile.DOCUMENTS)

    assert len(created_temporaries) == 1
    assert not created_temporaries[0].exists()
    assert leftover.read_bytes() == b"previous crash leftover"
    assert not profile_marker_path(root).exists()


def test_two_independent_same_profile_initializers_both_succeed(tmp_path: Path):
    root = tmp_path / "shared-root"

    results = _run_concurrent_initializers(
        root, (RuntimeProfile.WORKBENCH, RuntimeProfile.WORKBENCH)
    )

    assert [result[0] for result in results] == ["success", "success"]
    assert all(
        result[1:] == (RuntimeProfile.WORKBENCH.value, False) for result in results
    )
    assert read_runtime_profile(root) == RootProfileBinding(
        profile=RuntimeProfile.WORKBENCH,
        content_policy=ContentPolicy.INSTALLATION_ENVELOPE_V2,
        legacy=False,
    )


def test_two_independent_different_profile_initializers_choose_one_winner(
    tmp_path: Path,
):
    root = tmp_path / "shared-root"

    results = _run_concurrent_initializers(
        root, (RuntimeProfile.WORKBENCH, RuntimeProfile.DOCUMENTS)
    )

    successes = [result for result in results if result[0] == "success"]
    failures = [result for result in results if result[0] == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "already bound" in str(failures[0][1])
    stored = read_runtime_profile(root)
    assert stored is not None
    assert stored.profile.value == successes[0][1]
    assert stored.legacy is False
