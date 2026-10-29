from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import pytest

from scripts.documents_source_inventory import (
    DocumentsSourceInventoryError,
    documents_backend_members,
)

_REQUIRED = (
    "carlina/__init__.py",
    "carlina/documents.py",
    "carlina/managed_launcher.py",
    "iarf_assistant/__init__.py",
    "iarf_assistant/app.py",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "iarf_assistant/addons/doc_processor/manifest.json",
    "iarf_assistant/addons/translator/manifest.json",
    "iarf_assistant/addons/sentence_segmenter/manifest.json",
)


def _write(root: Path, member: str, contents: bytes = b"synthetic") -> None:
    path = root / member
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def _seed_tree(root: Path, members: Iterable[str] = ()) -> None:
    for member in (*_REQUIRED, *members):
        _write(root, member)


def test_installation_schema_is_selected_without_other_canonical_json(
    tmp_path: Path,
) -> None:
    schema = "carlina/core/installation-event-v1.schema.json"
    unrelated = "carlina/core/private-runtime.json"
    _seed_tree(tmp_path, (schema, unrelated))

    selected = documents_backend_members(tmp_path)

    assert schema in selected
    assert unrelated not in selected


def _assert_inventory_error(
    root: Path, expected_message: str, *, extra_members: tuple[str, ...] = ()
) -> None:
    with pytest.raises(DocumentsSourceInventoryError) as caught:
        documents_backend_members(root, extra_members=extra_members)
    assert str(caught.value) == expected_message


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except NotImplementedError, OSError:
        pytest.skip("symlink creation is unavailable")


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            )
            for path in root.rglob("*")
        )
    )


def test_backend_inventory_is_sorted_complete_and_read_only(tmp_path: Path) -> None:
    members = (
        "carlina/z_legacy_module.py",
        "carlina/a_module.py",
        "iarf_assistant/top_level.py",
        "iarf_assistant/core/core_module.py",
        "iarf_assistant/core/schema.json",
        "iarf_assistant/core/addons/catalog.json",
        "iarf_assistant/core/alembic_templates/env.py",
        "iarf_assistant/core/alembic_templates/script.py.mako",
        "iarf_assistant/database/models.py",
        "iarf_assistant/addon_api/contracts.py",
        "iarf_assistant/knowledge/index.py",
        "iarf_assistant/migrations/alembic.ini",
        "iarf_assistant/migrations/env.py",
        "iarf_assistant/migrations/script.py.mako",
        "iarf_assistant/migrations/versions/001_initial.py",
        "iarf_assistant/addons/doc_processor/engine.py",
        "iarf_assistant/addons/doc_processor/workers/native.py",
        "iarf_assistant/addons/doc_processor/requirements-documents.txt",
        "iarf_assistant/addons/doc_processor/requirements-runtime.lock",
        "iarf_assistant/addons/doc_processor/conda-win-64.lock",
        "iarf_assistant/addons/doc_processor/pixi.toml",
        "iarf_assistant/addons/doc_processor/pixi.lock",
        "iarf_assistant/addons/doc_processor/Dockerfile",
        "iarf_assistant/addons/doc_processor/.dockerignore",
        "iarf_assistant/addons/doc_processor/docker-compose.yml",
        "iarf_assistant/addons/doc_processor/docker-requirements.txt",
        "iarf_assistant/addons/doc_processor/entrypoint.sh",
        "iarf_assistant/addons/doc_processor/migrations/env.py",
        "iarf_assistant/addons/doc_processor/migrations/versions/001.py",
        "iarf_assistant/addons/doc_processor/skills/extract/worker.md",
        "iarf_assistant/addons/doc_processor/skills/README.md",
        "iarf_assistant/addons/doc_processor/container/worker.py",
        "iarf_assistant/addons/doc_processor/container/requirements.txt",
        "iarf_assistant/addons/doc_processor/container/runtime.lock",
        "iarf_assistant/addons/doc_processor/container/start.sh",
        "iarf_assistant/addons/doc_processor/container/Dockerfile",
        "iarf_assistant/addons/translator/engine.py",
        "iarf_assistant/addons/sentence_segmenter/worker.py",
        "iarf_assistant/addons/stripe/unrelated.py",
        "root_module.py",
        "docs/unrelated.md",
        "carlina/.venv/ignored.py",
        "carlina/.pixi/ignored.py",
        "carlina/__pycache__/ignored.py",
        "carlina/node_modules/ignored.py",
        "carlina/env/ignored.py",
        "carlina/lib/ignored.py",
        "carlina/data/ignored.py",
        "carlina/logs/ignored.py",
        "carlina/cache/ignored.py",
        "carlina/build/ignored.py",
        "carlina/dist/ignored.py",
        "carlina/.bin/ignored.py",
        "carlina/.codegraph/ignored.py",
        "carlina/graphify-out/ignored.py",
        "carlina/instance/ignored.py",
        "carlina/uploads/ignored.py",
        "carlina/temp/ignored.py",
        "carlina/workspace_docs/ignored.py",
        "carlina/library_importer_runs/ignored.py",
        "carlina/collections/ignored.py",
        "carlina/.env",
        "carlina/.env.production",
        "carlina/master-keys.json",
        "carlina/session.key",
        "carlina/server.pid",
        "carlina/leader.json",
        "carlina/state.db",
        "carlina/state.sqlite",
        "carlina/state.sqlite3",
        "carlina/ignored.pyc",
        "carlina/ignored.pyo",
        "carlina/.git/ignored.py",
        "iarf_assistant/core/master-keys.json",
        "iarf_assistant/core/leader.json",
        "iarf_assistant/addons/doc_processor/frontend/ignored.py",
        "iarf_assistant/addons/doc_processor/static/ignored.py",
        "iarf_assistant/addons/doc_processor/templates/ignored.py",
    )
    root = tmp_path / "synthetic"
    _seed_tree(root, reversed(members))
    before = _tree_snapshot(root)

    result = documents_backend_members(root)

    assert result == tuple(sorted(result))
    assert len(result) == len(set(result))
    assert not any(
        member.startswith("iarf_assistant/core/client_api/") for member in result
    )
    assert set(_REQUIRED) <= set(result)
    assert {
        "carlina/a_module.py",
        "carlina/z_legacy_module.py",
        "iarf_assistant/top_level.py",
        "iarf_assistant/core/core_module.py",
        "iarf_assistant/core/schema.json",
        "iarf_assistant/core/addons/catalog.json",
        "iarf_assistant/core/alembic_templates/env.py",
        "iarf_assistant/core/alembic_templates/script.py.mako",
        "iarf_assistant/database/models.py",
        "iarf_assistant/addon_api/contracts.py",
        "iarf_assistant/knowledge/index.py",
        "iarf_assistant/migrations/alembic.ini",
        "iarf_assistant/migrations/env.py",
        "iarf_assistant/migrations/script.py.mako",
        "iarf_assistant/migrations/versions/001_initial.py",
        "iarf_assistant/addons/doc_processor/engine.py",
        "iarf_assistant/addons/doc_processor/workers/native.py",
        "iarf_assistant/addons/doc_processor/manifest.json",
        "iarf_assistant/addons/doc_processor/requirements-documents.txt",
        "iarf_assistant/addons/doc_processor/requirements-runtime.lock",
        "iarf_assistant/addons/doc_processor/conda-win-64.lock",
        "iarf_assistant/addons/doc_processor/pixi.toml",
        "iarf_assistant/addons/doc_processor/pixi.lock",
        "iarf_assistant/addons/doc_processor/Dockerfile",
        "iarf_assistant/addons/doc_processor/.dockerignore",
        "iarf_assistant/addons/doc_processor/docker-compose.yml",
        "iarf_assistant/addons/doc_processor/docker-requirements.txt",
        "iarf_assistant/addons/doc_processor/entrypoint.sh",
        "iarf_assistant/addons/doc_processor/migrations/env.py",
        "iarf_assistant/addons/doc_processor/migrations/versions/001.py",
        "iarf_assistant/addons/doc_processor/skills/extract/worker.md",
        "iarf_assistant/addons/doc_processor/skills/README.md",
        "iarf_assistant/addons/doc_processor/container/worker.py",
        "iarf_assistant/addons/doc_processor/container/requirements.txt",
        "iarf_assistant/addons/doc_processor/container/runtime.lock",
        "iarf_assistant/addons/doc_processor/container/start.sh",
        "iarf_assistant/addons/doc_processor/container/Dockerfile",
        "iarf_assistant/addons/translator/engine.py",
        "iarf_assistant/addons/sentence_segmenter/worker.py",
    } <= set(result)
    assert not {
        "root_module.py",
        "docs/unrelated.md",
        "iarf_assistant/addons/stripe/unrelated.py",
        "carlina/.env",
        "carlina/.env.production",
        "carlina/master-keys.json",
        "carlina/session.key",
        "carlina/server.pid",
        "carlina/leader.json",
        "carlina/state.db",
        "carlina/state.sqlite",
        "carlina/state.sqlite3",
        "carlina/ignored.pyc",
        "carlina/ignored.pyo",
        "iarf_assistant/core/master-keys.json",
        "iarf_assistant/core/leader.json",
    } & set(result)
    assert _tree_snapshot(root) == before


def test_capture_payload_exclusion_preserves_shared_source_and_core_state(
    tmp_path: Path,
) -> None:
    capture_members = (
        "iarf_assistant/core/client_api/__init__.py",
        "iarf_assistant/core/client_api/device_auth.py",
        "iarf_assistant/core/client_api/contracts/compass/v1/__init__.py",
        "iarf_assistant/core/client_api/contracts/compass/v1/capture.schema.json",
        "iarf_assistant/core/client_api/contracts/compass/v1/openapi.yaml",
    )
    retained_members = (
        "iarf_assistant/models.py",
        "iarf_assistant/migrations/versions/20260812_compass_client_auth.py",
        "iarf_assistant/core/client_api_compatibility.py",
        "iarf_assistant/core/addons/native_manager.py",
    )
    root = tmp_path / "shared-source"
    _seed_tree(root, (*capture_members, *retained_members))
    before = _tree_snapshot(root)

    result = documents_backend_members(root)

    assert set(retained_members) <= set(result)
    assert not set(capture_members) & set(result)
    assert not any(
        member.startswith("iarf_assistant/core/client_api/") for member in result
    )
    assert all(
        (root / member).read_bytes() == b"synthetic" for member in capture_members
    )
    assert _tree_snapshot(root) == before


def test_assistant_payload_exclusion_retains_shared_data_and_vector_backends(
    tmp_path: Path,
) -> None:
    assistant_members = (
        "iarf_assistant/rag_service.py",
        "iarf_assistant/maintenance.py",
        "iarf_assistant/knowledge/routes.py",
        "iarf_assistant/knowledge/worker.py",
        "iarf_assistant/core/context_fabric.py",
        "iarf_assistant/core/collection_service.py",
        "iarf_assistant/core/workflow_executor.py",
        "iarf_assistant/core/worker_process.py",
    )
    retained_members = (
        "iarf_assistant/models.py",
        "iarf_assistant/migrations/versions/dd2d3e720c4b_add_workflowtemplate.py",
        "iarf_assistant/migrations/versions/20260812_compass_client_auth.py",
        "iarf_assistant/core/document_knowledge.py",
        "iarf_assistant/core/knowledge_fabric.py",
        "iarf_assistant/core/vector_backend.py",
        "iarf_assistant/core/task_queue.py",
        "iarf_assistant/core/worker_runtime.py",
        "iarf_assistant/core/model_router.py",
    )
    root = tmp_path / "shared-source"
    _seed_tree(root, (*assistant_members, *retained_members))
    before = _tree_snapshot(root)

    result = documents_backend_members(root)

    assert set(result) >= set(retained_members)
    assert not set(assistant_members) & set(result)
    assert _tree_snapshot(root) == before
    for member in assistant_members:
        _assert_inventory_error(
            root, "extra member is not allowed", extra_members=(member,)
        )


def test_core_collections_code_is_retained_without_collection_data(
    tmp_path: Path,
) -> None:
    source_members = (
        "iarf_assistant/core/collections/__init__.py",
        "iarf_assistant/core/collections/projections.py",
    )
    excluded_members = (
        "collections/item.py",
        "carlina/collections/item.py",
        "iarf_assistant/core/collections/data/snapshot.py",
        "iarf_assistant/core/collections/protected.sqlite",
        "iarf_assistant/core/collections/session.key",
    )
    root = tmp_path / "synthetic"
    _seed_tree(root, (*source_members, *excluded_members))
    before = _tree_snapshot(root)

    result = documents_backend_members(root)

    assert set(source_members) <= set(result)
    assert not set(excluded_members) & set(result)
    assert _tree_snapshot(root) == before


def test_workbench_payload_exclusion_preserves_common_execution_and_native_host(
    tmp_path: Path,
) -> None:
    excluded = (
        "iarf_assistant/chat_builtin_tools.py",
        "iarf_assistant/chat_orchestrator.py",
        "iarf_assistant/chat_prompt_builder.py",
        "iarf_assistant/chat_routes.py",
        "iarf_assistant/administration_frontend.py",
        "iarf_assistant/runtime_routes.py",
        "iarf_assistant/core/chat_sessions/__init__.py",
        "iarf_assistant/core/chat_sessions/projections.py",
        "iarf_assistant/core/chat_turns/tool_loop.py",
        "iarf_assistant/core/chat_turns/future/chat_service.py",
        "iarf_assistant/core/workflow_steps/__init__.py",
        "iarf_assistant/core/workflow_steps/executor.py",
        "iarf_assistant/core/run_orchestrator.py",
        "iarf_assistant/core/runs/compiler.py",
        "iarf_assistant/core/code_session_service.py",
        "iarf_assistant/core/code_session_sdk.py",
        "iarf_assistant/core/code_session_bootstrap.py",
        "iarf_assistant/core/project_workspace_service.py",
        "iarf_assistant/core/project_process_supervisor.py",
        "iarf_assistant/core/runtime_interactions.py",
        "iarf_assistant/core/finalization_runner.py",
        "iarf_assistant/core/turn_finalization.py",
        "iarf_assistant/core/workflow_worker.py",
        "iarf_assistant/core/addon_manager.py",
        "iarf_assistant/core/addons/container_orchestration.py",
        "iarf_assistant/core/addons/container_runtime.py",
        "iarf_assistant/core/addons/audio_spoke.py",
        "iarf_assistant/core/mcp_news.py",
        "iarf_assistant/knowledge/document_parser.py",
    )
    retained = (
        "iarf_assistant/models.py",
        "iarf_assistant/migrations/versions/20260920_news_mcp.py",
        "iarf_assistant/core/agent_events.py",
        "iarf_assistant/core/agent_loop.py",
        "iarf_assistant/core/execution_kernel.py",
        "iarf_assistant/core/runtime_state.py",
        "iarf_assistant/core/runs/projections.py",
        "iarf_assistant/core/project_workspace_roots.py",
        "iarf_assistant/core/workflow_templates.py",
        "iarf_assistant/core/memory_defaults.py",
        "iarf_assistant/core/addon_manager_core.py",
        "iarf_assistant/core/addons/native_manager.py",
        "iarf_assistant/core/addons/container_lifecycle_support.py",
    )
    root = tmp_path / "shared-source"
    _seed_tree(root, (*excluded, *retained))
    before = _tree_snapshot(root)

    result = documents_backend_members(root)

    assert set(retained) <= set(result)
    assert not set(excluded) & set(result)
    assert _tree_snapshot(root) == before
    for member in excluded:
        assert (root / member).read_bytes() == b"synthetic"
        _assert_inventory_error(
            root, "extra member is not allowed", extra_members=(member,)
        )


def test_inventory_is_independent_of_filesystem_creation_order(tmp_path: Path) -> None:
    members = (
        "carlina/first.py",
        "carlina/second.py",
        "iarf_assistant/core/z.py",
        "iarf_assistant/core/a.py",
        "iarf_assistant/addons/translator/b.py",
        "iarf_assistant/addons/translator/a.py",
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    _seed_tree(first, members)
    _seed_tree(second, reversed(members))

    assert documents_backend_members(first) == documents_backend_members(second)


def test_opaque_extra_members_follow_the_explicit_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "extras"
    _seed_tree(root)
    extras = (
        "README.md",
        "COPYING-AGPL-3.0.txt",
        "third_party/frontend/NOTICE.json",
        "third_party/frontend/notices/synthetic--1.0--LICENSE.txt",
        "third_party/models/NOTICE.json",
        "third_party/models/segment-any-text-sat-12l-sm.md",
        "third_party/models/FacebookAI-xlm-roberta-base.md",
        "pyproject.toml",
        "pixi.toml",
        "pixi.lock",
        "package.json",
        "package-lock.json",
        "scripts/build_tailwind.py",
        "scripts/documents_dependency_inventory.py",
        "scripts/generate_documents_inventory.py",
        "scripts/documents_conda_notices.py",
        "scripts/collect_documents_notices.py",
        "scripts/assemble_documents_notices.py",
        "scripts/assemble_documents_sources.py",
        "scripts/package_application_source.py",
        "scripts/package_application_companions.py",
        "scripts/build_clean_release.py",
        "scripts/build_documents_release.py",
        "scripts/build_documents_appimage.py",
        "scripts/build_documents_windows_installer.py",
        "scripts/documents_appimage.py",
        "packaging/appimage/AppRun",
        "packaging/appimage/carlina-documents.desktop",
        "packaging/windows/carlina-documents.ico",
        "packaging/windows/requirements-build.lock",
        "third_party/appimage/NOTICE.json",
        "third_party/appimage/notices/synthetic-license.txt",
        "third_party/windows-bootstrap/NOTICE.json",
        "third_party/windows-bootstrap/notices/synthetic-license.txt",
        "scripts/documents_source_inventory.py",
        "scripts/addon_publication_policy.py",
        "scripts/build_managed_pack.py",
        "scripts/prepare_publisher_bootstrap.py",
        "scripts/build_publisher_bootstrap.py",
        "scripts/build_release_index.py",
        "scripts/render_managed_installers.py",
        "scripts/managed_pixi_smoke.py",
        "scripts/verify_release_artifacts.py",
        "policy/release-exceptions.json",
        "docs/documents-application.md",
        "docs/epub-export.md",
        "docs/addon-api-v1.md",
        "docs/architecture.md",
        "docs/engineering-policy.md",
        "docs/extraction-efficiency.md",
        "docs/mcp-integration-architecture.md",
        "docs/translator.md",
        "docs/update-and-recovery.md",
        "docs/adr/0012-documents-agpl-distribution.md",
        "iarf_assistant/static/index.html",
        "iarf_assistant/static/dist/app.bin",
        "iarf_assistant/templates/document.bin",
        "frontend/apps/core/src/bootstrap.bin",
        "frontend/apps/core/src/lib/helper.bin",
        "frontend/apps/core/static/dist/chunk.bin",
        "frontend/packages/sdk/entry.bin",
        "frontend/packages/sdk/src/lib/helper.bin",
        "iarf_assistant/addons/doc_processor/static/dist/chunk.bin",
        "iarf_assistant/addons/doc_processor/templates/print.bin",
        "iarf_assistant/addons/doc_processor/frontend/src/entry.bin",
        "iarf_assistant/addons/doc_processor/frontend/src/lib/helper.bin",
        "iarf_assistant/addons/translator/static/icon.bin",
        "iarf_assistant/addons/translator/frontend/src/lib/helper.bin",
        "iarf_assistant/addons/sentence_segmenter/frontend/worker.bin",
        "iarf_assistant/addons/sentence_segmenter/frontend/src/lib/helper.bin",
    )
    for member in extras:
        _write(root, member, b"\x00\xff opaque bytes")
    forbidden_sibling = "scripts/unapproved_release_helper.py"
    _write(root, forbidden_sibling, b"forbidden sibling")
    private_runbook = "docs/operations.md"
    _write(root, private_runbook, b"private operator evidence stays outside the offer")

    result = documents_backend_members(root, extra_members=extras)

    assert set(extras) <= set(result)
    assert forbidden_sibling not in result
    assert private_runbook not in result
    assert "iarf_assistant/addons/doc_processor/static/dist/chunk.bin" in result
    assert "frontend/apps/core/static/dist/chunk.bin" in result


@pytest.mark.parametrize(
    "member",
    (
        "",
        ".",
        "./README.md",
        "README.md/../pyproject.toml",
        "README.md//extra",
        "README.md/",
        "/README.md",
        "C:\\README.md",
        "D:README.md",
        "docs\\documents-application.md",
    ),
)
def test_malformed_extra_paths_are_rejected_without_echoing_paths(
    tmp_path: Path, member: str
) -> None:
    root = tmp_path / "malformed"
    _seed_tree(root)

    _assert_inventory_error(root, "invalid extra member", extra_members=(member,))


@pytest.mark.parametrize(
    "member",
    (
        "docs/private.md",
        "docs/implementation-plan.md",
        "docs/project-ledger.md",
        "README.txt",
        "private.py",
        "frontend/apps/other/src/app.bin",
        "frontend/packages-other/sdk/app.bin",
        "iarf_assistant/addons/stripe/frontend/app.bin",
        "iarf_assistant/addons/doc_processor/private.bin",
        "iarf_assistant/core/client_api/device_auth.py",
        "iarf_assistant/core/client_api/contracts/compass/v1/capture.schema.json",
        "frontend/apps/core/build/app.bin",
        "frontend/apps/core/lib/native.bin",
        "frontend/apps/core/src/lib/.env.production",
        "frontend/apps/core/src/lib/state.sqlite3",
        "frontend/apps/core/src/lib/env/native.bin",
        "frontend/apps/core/.env.production",
        "frontend/apps/core/state.db",
        "frontend/apps/core/state.sqlite-wal",
        "frontend/apps/core/native.pyc",
    ),
)
def test_unlisted_or_globally_denied_extra_paths_are_rejected(
    tmp_path: Path, member: str
) -> None:
    root = tmp_path / "disallowed"
    _seed_tree(root)

    _assert_inventory_error(
        root, "extra member is not allowed", extra_members=(member,)
    )


def test_duplicate_extra_paths_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "duplicate"
    _seed_tree(root)

    _assert_inventory_error(
        root, "duplicate extra member", extra_members=("README.md", "README.md")
    )


def test_extra_member_must_be_a_regular_file_and_cannot_have_link_components(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extra-links"
    _seed_tree(root)
    _write(root, "assets/opaque.bin")
    _write(root, "frontend/apps/core/target.bin")
    _symlink(root / "README.md", root / "assets/opaque.bin")

    _assert_inventory_error(
        root, "source member contains a symlink", extra_members=("README.md",)
    )

    (root / "README.md").unlink()
    linked_directory = root / "frontend/apps/core/linked"
    _symlink(linked_directory, root / "frontend/apps/core", directory=True)
    _assert_inventory_error(
        root,
        "source member contains a symlink",
        extra_members=("frontend/apps/core/linked/target.bin",),
    )


def test_selected_backend_symlink_files_and_directories_are_rejected(
    tmp_path: Path,
) -> None:
    file_root = tmp_path / "file-link"
    _seed_tree(file_root, ("carlina/target.py",))
    _symlink(file_root / "carlina/linked.py", file_root / "carlina/target.py")
    _assert_inventory_error(file_root, "source member contains a symlink")

    directory_root = tmp_path / "directory-link"
    _seed_tree(directory_root, ("carlina/target/module.py",))
    _symlink(
        directory_root / "carlina/linked",
        directory_root / "carlina/target",
        directory=True,
    )
    _assert_inventory_error(directory_root, "source member contains a symlink")


def test_missing_required_anchor_fails_without_partial_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing-anchor"
    _seed_tree(root, ("carlina/extra.py",))
    (root / "carlina/documents.py").unlink()

    _assert_inventory_error(root, "required source member is missing")


def test_non_regular_selected_member_is_rejected(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    root = tmp_path / "fifo"
    _seed_tree(root)
    fifo = root / "carlina/fifo.py"
    fifo.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("FIFO creation is unavailable")

    _assert_inventory_error(root, "source member is not a regular file")


def test_non_regular_extra_member_is_rejected(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    root = tmp_path / "extra-fifo"
    _seed_tree(root)
    fifo = root / "README.md"
    try:
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("FIFO creation is unavailable")

    _assert_inventory_error(
        root, "source member is not a regular file", extra_members=("README.md",)
    )


def test_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _assert_inventory_error(missing, "source root must be an existing directory")
    file_root = tmp_path / "file"
    file_root.write_bytes(b"synthetic")
    _assert_inventory_error(file_root, "source root must be an existing directory")


def test_root_symlink_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    _seed_tree(real_root)
    root_link = tmp_path / "root-link"
    _symlink(root_link, real_root, directory=True)

    _assert_inventory_error(root_link, "source member contains a symlink")


def test_extra_members_must_exist(tmp_path: Path) -> None:
    root = tmp_path / "absent-extra"
    _seed_tree(root)

    _assert_inventory_error(
        root, "required source member is missing", extra_members=("README.md",)
    )
