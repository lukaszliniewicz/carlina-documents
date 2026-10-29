"""Select the backend-owned source members for the standalone Documents artifact."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, cast


class DocumentsSourceInventoryError(ValueError):
    """Raised when a source member violates the standalone inventory contract."""


_ADDON_IDS: Final = ("doc_processor", "translator", "sentence_segmenter")
_WORKBENCH_ONLY_PACKAGES: Final = frozenset(
    {
        ("iarf_assistant", "core", "client_api"),
        ("iarf_assistant", "core", "chat_sessions"),
        ("iarf_assistant", "core", "chat_turns"),
        ("iarf_assistant", "core", "workflow_steps"),
    }
)
_WORKBENCH_ONLY_MODULES: Final = frozenset(
    {
        ("iarf_assistant", "administration_frontend.py"),
        ("iarf_assistant", "automation_frontend.py"),
        ("iarf_assistant", "chat_builtin_tools.py"),
        ("iarf_assistant", "chat_constants.py"),
        ("iarf_assistant", "chat_context_store.py"),
        ("iarf_assistant", "chat_execution_helpers.py"),
        ("iarf_assistant", "chat_finalization.py"),
        ("iarf_assistant", "chat_frontend.py"),
        ("iarf_assistant", "chat_orchestrator.py"),
        ("iarf_assistant", "chat_prompt_builder.py"),
        ("iarf_assistant", "chat_routes.py"),
        ("iarf_assistant", "context_frontend.py"),
        ("iarf_assistant", "discovery_routes.py"),
        ("iarf_assistant", "knowledge_frontend.py"),
        ("iarf_assistant", "rag_service.py"),
        ("iarf_assistant", "maintenance.py"),
        ("iarf_assistant", "profile_routes.py"),
        ("iarf_assistant", "run_frontend.py"),
        ("iarf_assistant", "runtime_operations_frontend.py"),
        ("iarf_assistant", "runtime_routes.py"),
        ("iarf_assistant", "system_routes.py"),
        ("iarf_assistant", "upload_routes.py"),
        ("iarf_assistant", "knowledge", "document_parser.py"),
        ("iarf_assistant", "knowledge", "routes.py"),
        ("iarf_assistant", "knowledge", "worker.py"),
        ("iarf_assistant", "core", "addon_manager.py"),
        ("iarf_assistant", "core", "addons", "audio_spoke.py"),
        ("iarf_assistant", "core", "addons", "container_orchestration.py"),
        ("iarf_assistant", "core", "addons", "container_runtime.py"),
        ("iarf_assistant", "core", "code_session_bootstrap.py"),
        ("iarf_assistant", "core", "code_session_sdk.py"),
        ("iarf_assistant", "core", "code_session_service.py"),
        ("iarf_assistant", "core", "context_fabric.py"),
        ("iarf_assistant", "core", "collection_service.py"),
        ("iarf_assistant", "core", "finalization_runner.py"),
        ("iarf_assistant", "core", "mcp_news.py"),
        ("iarf_assistant", "core", "project_process_supervisor.py"),
        ("iarf_assistant", "core", "project_workspace_service.py"),
        ("iarf_assistant", "core", "run_orchestrator.py"),
        ("iarf_assistant", "core", "runs", "compiler.py"),
        ("iarf_assistant", "core", "runtime_interactions.py"),
        ("iarf_assistant", "core", "turn_finalization.py"),
        ("iarf_assistant", "core", "workflow_executor.py"),
        ("iarf_assistant", "core", "workflow_worker.py"),
        ("iarf_assistant", "core", "worker_process.py"),
    }
)
_DENIED_COMPONENTS: Final = frozenset(
    {
        ".git",
        ".pixi",
        ".venv",
        "__pycache__",
        "node_modules",
        "env",
        "lib",
        "data",
        "logs",
        "cache",
        "build",
        "dist",
        ".bin",
        ".codegraph",
        "graphify-out",
        "instance",
        "uploads",
        "temp",
        "workspace_docs",
        "library_importer_runs",
        "collections",
    }
)
_DENIED_FILENAMES: Final = frozenset(
    {"master-keys.json", "session.key", "server.pid", "leader.json"}
)
_REQUIRED_MEMBERS: Final = (
    "carlina/__init__.py",
    "carlina/documents.py",
    "carlina/managed_launcher.py",
    "iarf_assistant/__init__.py",
    "iarf_assistant/app.py",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    *(f"iarf_assistant/addons/{addon_id}/manifest.json" for addon_id in _ADDON_IDS),
)
_ROOT_EXTRA_MEMBERS: Final = frozenset(
    {
        "README.md",
        "COPYING-AGPL-3.0.txt",
        "third_party/frontend/NOTICE.json",
        "third_party/models/NOTICE.json",
        "third_party/appimage/NOTICE.json",
        "third_party/windows-bootstrap/NOTICE.json",
        "packaging/appimage/AppRun",
        "packaging/appimage/carlina-documents.desktop",
        "packaging/windows/carlina-documents.ico",
        "packaging/windows/requirements-build.lock",
        "iarf_assistant/addons/sentence_segmenter/container/.dockerignore",
        "third_party/models/segment-any-text-sat-12l-sm.md",
        "third_party/models/FacebookAI-xlm-roberta-base.md",
        "pyproject.toml",
        "pixi.toml",
        "pixi.lock",
        "package.json",
        "package-lock.json",
        "policy/release-exceptions.json",
        "policy/documents-closure.json",
        "eslint.config.mjs",
        "frontend/tsconfig.base.json",
        "scripts/run_frontend_workspaces.mjs",
        "scripts/build_tailwind.py",
        "scripts/documents_dependency_inventory.py",
        "scripts/measure_documents_closure.py",
        "scripts/check_documents_closure.py",
        "scripts/check_markdown_links.py",
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
        "scripts/documents_source_inventory.py",
        "scripts/addon_publication_policy.py",
        "scripts/build_managed_pack.py",
        "scripts/prepare_publisher_bootstrap.py",
        "scripts/build_publisher_bootstrap.py",
        "scripts/build_release_index.py",
        "scripts/render_managed_installers.py",
        "scripts/managed_pixi_smoke.py",
        "scripts/verify_release_artifacts.py",
    }
)
_STATIC_TEMPLATE_EXTRA_ROOTS: Final = (
    ("iarf_assistant", "static"),
    ("iarf_assistant", "templates"),
)

_ROOT_ERROR: Final = "source root must be an existing directory"
_MISSING_ERROR: Final = "required source member is missing"
_SYMLINK_ERROR: Final = "source member contains a symlink"
_NON_DIRECTORY_ERROR: Final = "source member parent is not a directory"
_NON_REGULAR_ERROR: Final = "source member is not a regular file"
_TREE_ERROR: Final = "source tree could not be inventoried"
_INVALID_EXTRA_ERROR: Final = "invalid extra member"
_DUPLICATE_EXTRA_ERROR: Final = "duplicate extra member"
_DISALLOWED_EXTRA_ERROR: Final = "extra member is not allowed"

_FilePredicate = Callable[[str], bool]


def _is_denied(parts: tuple[str, ...], *, is_extra: bool = False) -> bool:
    # Workbench consumes these implementations from shared Git. Documents uses
    # the common Knowledge data layer and vector backend, not assistant services.
    if parts[:3] in _WORKBENCH_ONLY_PACKAGES or parts in _WORKBENCH_ONLY_MODULES:
        return True
    for index, component in enumerate(parts):
        is_static_distribution = (
            is_extra
            and component == "dist"
            and index > 0
            and parts[index - 1] == "static"
        )
        # This shared runtime package is code; other collections trees retain
        # the mutable-data exclusion.
        is_core_collections = (
            component == "collections"
            and index == 2
            and parts[:3] == ("iarf_assistant", "core", "collections")
        )
        # Declared frontend src/lib directories contain corresponding source,
        # unlike the excluded library trees of installed Python environments.
        is_frontend_source_lib = (
            is_extra
            and component == "lib"
            and (
                (
                    index == 4
                    and parts[:3] == ("frontend", "apps", "core")
                    and parts[3] == "src"
                )
                or (
                    index == 4
                    and parts[:2] == ("frontend", "packages")
                    and parts[3] == "src"
                )
                or (
                    index == 5
                    and parts[:2] == ("iarf_assistant", "addons")
                    and parts[2] in _ADDON_IDS
                    and parts[3:5] == ("frontend", "src")
                )
            )
        )
        if component in _DENIED_COMPONENTS and not (
            is_static_distribution or is_core_collections or is_frontend_source_lib
        ):
            return True
        if (
            component in _DENIED_FILENAMES
            or component == ".env"
            or component.startswith(".env.")
        ):
            return True
        if component.endswith((".pyc", ".pyo")):
            return True
        lowered = component.lower()
        if lowered.endswith((".db", ".sqlite", ".sqlite3", "-journal", "-wal", "-shm")):
            return True
    return False


def _validate_root(root: Path) -> Path:
    try:
        requested_root = Path(root)
        absolute_root = Path(os.path.abspath(requested_root))
        ancestors = (*absolute_root.parents, absolute_root)
        root_status: os.stat_result | None = None
        for ancestor in reversed(ancestors):
            status = ancestor.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise DocumentsSourceInventoryError(_SYMLINK_ERROR)
            if ancestor == absolute_root:
                root_status = status
    except DocumentsSourceInventoryError:
        raise
    except OSError, TypeError, ValueError:
        raise DocumentsSourceInventoryError(_ROOT_ERROR) from None
    if root_status is None or not stat.S_ISDIR(root_status.st_mode):
        raise DocumentsSourceInventoryError(_ROOT_ERROR)
    return absolute_root


def _lstat_relative(
    root: Path, parts: tuple[str, ...], *, missing_ok: bool = False
) -> os.stat_result | None:
    if not parts:
        if missing_ok:
            return None
        raise DocumentsSourceInventoryError(_MISSING_ERROR)
    current = root
    status: os.stat_result | None = None
    for index, component in enumerate(parts):
        current = current / component
        try:
            status = current.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise DocumentsSourceInventoryError(_MISSING_ERROR) from None
        except OSError:
            raise DocumentsSourceInventoryError(_TREE_ERROR) from None
        if stat.S_ISLNK(status.st_mode):
            raise DocumentsSourceInventoryError(_SYMLINK_ERROR)
        if index < len(parts) - 1 and not stat.S_ISDIR(status.st_mode):
            raise DocumentsSourceInventoryError(_NON_DIRECTORY_ERROR)
    return status


def _add_regular_member(root: Path, parts: tuple[str, ...], selected: set[str]) -> None:
    status = _lstat_relative(root, parts)
    if status is None or not stat.S_ISREG(status.st_mode):
        raise DocumentsSourceInventoryError(_NON_REGULAR_ERROR)
    selected.add("/".join(parts))


def _collect_direct_files(
    root: Path,
    directory: tuple[str, ...],
    selected: set[str],
    eligible: _FilePredicate,
) -> None:
    status = _lstat_relative(root, directory, missing_ok=True)
    if status is None:
        return
    if not stat.S_ISDIR(status.st_mode):
        raise DocumentsSourceInventoryError(_NON_DIRECTORY_ERROR)
    try:
        with os.scandir(root.joinpath(*directory)) as entries:
            entry_names = sorted(entry.name for entry in entries)
    except OSError:
        raise DocumentsSourceInventoryError(_TREE_ERROR) from None
    for name in entry_names:
        parts = (*directory, name)
        if _is_denied(parts) or not eligible(name):
            continue
        _add_regular_member(root, parts, selected)


def _walk_error(_error: OSError) -> None:
    raise DocumentsSourceInventoryError(_TREE_ERROR) from None


def _collect_tree_files(
    root: Path,
    directory: tuple[str, ...],
    selected: set[str],
    eligible: _FilePredicate,
    *,
    excluded_root_directories: frozenset[str] = frozenset(),
) -> None:
    status = _lstat_relative(root, directory, missing_ok=True)
    if status is None:
        return
    if not stat.S_ISDIR(status.st_mode):
        raise DocumentsSourceInventoryError(_NON_DIRECTORY_ERROR)

    start = root.joinpath(*directory)
    try:
        walker = os.walk(start, topdown=True, followlinks=False, onerror=_walk_error)
        for current_name, directory_names, file_names in walker:
            current = Path(current_name)
            current_parts = current.relative_to(root).parts
            kept_directories: list[str] = []
            for name in sorted(directory_names):
                parts = (*current_parts, name)
                if _is_denied(parts):
                    continue
                if (
                    len(parts) == len(directory) + 1
                    and name in excluded_root_directories
                ):
                    continue
                child_status = _lstat_relative(root, parts)
                if child_status is None or not stat.S_ISDIR(child_status.st_mode):
                    raise DocumentsSourceInventoryError(_NON_DIRECTORY_ERROR)
                kept_directories.append(name)
            directory_names[:] = kept_directories

            for name in sorted(file_names):
                parts = (*current_parts, name)
                if _is_denied(parts) or not eligible(name):
                    continue
                _add_regular_member(root, parts, selected)
    except DocumentsSourceInventoryError:
        raise
    except OSError:
        raise DocumentsSourceInventoryError(_TREE_ERROR) from None


def _is_python(name: str) -> bool:
    return name.endswith(".py")


def _is_json(name: str) -> bool:
    return name.endswith(".json")


def _is_alembic_template(name: str) -> bool:
    return name.endswith((".py", ".mako", ".ini"))


def _is_addon_root_resource(name: str) -> bool:
    return (
        (name.startswith("requirements") and name.endswith((".txt", ".lock")))
        or (name.startswith("conda-") and name.endswith(".lock"))
        or name
        in {
            "pixi.toml",
            "pixi.lock",
            "Dockerfile",
            ".dockerignore",
            "docker-compose.yml",
            "docker-requirements.txt",
            "entrypoint.sh",
        }
    )


def _is_addon_migration(name: str) -> bool:
    return name.endswith((".py", ".ini", ".mako"))


def _is_markdown(name: str) -> bool:
    return name.endswith(".md")


def _is_container_resource(name: str) -> bool:
    return name == "Dockerfile" or name.endswith((".py", ".txt", ".lock", ".sh"))


def _is_allowed_extra(parts: tuple[str, ...]) -> bool:
    member = "/".join(parts)
    notice_root = ("third_party", "frontend", "notices")
    if member in _ROOT_EXTRA_MEMBERS or (
        parts[: len(notice_root)] == notice_root and len(parts) == len(notice_root) + 1
    ):
        return True
    if (
        parts[:3]
        in {
            ("third_party", "appimage", "notices"),
            ("third_party", "windows-bootstrap", "notices"),
        }
        and len(parts) == 4
        and parts[-1].endswith(".txt")
    ):
        return True
    if member in {
        "docs/documents-application.md",
        "docs/epub-export.md",
        "docs/addon-api-v1.md",
        "docs/architecture.md",
        "docs/engineering-policy.md",
        "docs/extraction-efficiency.md",
        "docs/mcp-integration-architecture.md",
        "docs/translator.md",
        "docs/update-and-recovery.md",
    }:
        return True
    if member in {
        "docs/adr/0011-root-bound-ordinary-content-policy.md",
        "docs/adr/0012-documents-agpl-distribution.md",
    }:
        return True
    if any(
        parts[: len(prefix)] == prefix and len(parts) > len(prefix)
        for prefix in _STATIC_TEMPLATE_EXTRA_ROOTS
    ):
        return True
    if parts[:3] == ("frontend", "apps", "core") and len(parts) > 3:
        return True
    if parts[:2] == ("frontend", "packages") and len(parts) > 2:
        return True
    if parts[:2] == ("frontend", "contracts") and len(parts) > 2:
        return True
    return (
        len(parts) > 4
        and parts[:2] == ("iarf_assistant", "addons")
        and parts[2] in _ADDON_IDS
        and parts[3] in {"static", "templates", "frontend"}
    )


def _validated_extra_parts(raw_members: object) -> tuple[tuple[str, ...], ...]:
    if isinstance(raw_members, str) or not isinstance(raw_members, Sequence):
        raise DocumentsSourceInventoryError(_INVALID_EXTRA_ERROR)
    try:
        members: tuple[object, ...] = tuple(cast(Sequence[object], raw_members))
    except TypeError, ValueError:
        raise DocumentsSourceInventoryError(_INVALID_EXTRA_ERROR) from None
    if any(not isinstance(member, str) for member in members):
        raise DocumentsSourceInventoryError(_INVALID_EXTRA_ERROR)
    string_members = tuple(member for member in members if isinstance(member, str))
    if len(set(string_members)) != len(string_members):
        raise DocumentsSourceInventoryError(_DUPLICATE_EXTRA_ERROR)

    validated: list[tuple[str, ...]] = []
    for member in sorted(string_members):
        if (
            not member
            or "\\" in member
            or "\x00" in member
            or member.startswith("/")
            or re.match(r"^[A-Za-z]:", member) is not None
        ):
            raise DocumentsSourceInventoryError(_INVALID_EXTRA_ERROR)
        parts = tuple(member.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise DocumentsSourceInventoryError(_INVALID_EXTRA_ERROR)
        if _is_denied(parts, is_extra=True) or not _is_allowed_extra(parts):
            raise DocumentsSourceInventoryError(_DISALLOWED_EXTRA_ERROR)
        validated.append(parts)
    return tuple(validated)


def documents_backend_members(
    root: Path, *, extra_members: Sequence[str] = ()
) -> tuple[str, ...]:
    """Return the sorted backend source inventory without reading or copying files."""
    source_root = _validate_root(root)
    extras = _validated_extra_parts(extra_members)
    selected: set[str] = set()

    for member in _REQUIRED_MEMBERS:
        parts = tuple(member.split("/"))
        if _is_denied(parts):
            raise DocumentsSourceInventoryError(_DISALLOWED_EXTRA_ERROR)
        _add_regular_member(source_root, parts, selected)

    _collect_tree_files(source_root, ("carlina",), selected, _is_python)
    _collect_direct_files(
        source_root,
        ("carlina", "core"),
        selected,
        lambda name: name == "installation-event-v1.schema.json",
    )
    _collect_direct_files(source_root, ("iarf_assistant",), selected, _is_python)

    for directory in (
        ("iarf_assistant", "core"),
        ("iarf_assistant", "database"),
        ("iarf_assistant", "addon_api"),
        ("iarf_assistant", "knowledge"),
        ("iarf_assistant", "migrations"),
    ):
        _collect_tree_files(source_root, directory, selected, _is_python)

    _collect_direct_files(source_root, ("iarf_assistant", "core"), selected, _is_json)
    _collect_direct_files(
        source_root, ("iarf_assistant", "core", "addons"), selected, _is_json
    )
    _collect_direct_files(
        source_root,
        ("iarf_assistant", "core", "alembic_templates"),
        selected,
        _is_alembic_template,
    )
    _collect_direct_files(
        source_root,
        ("iarf_assistant", "migrations"),
        selected,
        lambda name: name in {"alembic.ini", "env.py", "script.py.mako"},
    )

    for addon_id in _ADDON_IDS:
        addon = ("iarf_assistant", "addons", addon_id)
        _collect_tree_files(
            source_root,
            addon,
            selected,
            _is_python,
            excluded_root_directories=frozenset({"frontend", "static", "templates"}),
        )
        _collect_direct_files(source_root, addon, selected, _is_addon_root_resource)
        _collect_tree_files(
            source_root, (*addon, "migrations"), selected, _is_addon_migration
        )
        _collect_tree_files(source_root, (*addon, "skills"), selected, _is_markdown)
        _collect_tree_files(
            source_root, (*addon, "container"), selected, _is_container_resource
        )

    for parts in extras:
        _add_regular_member(source_root, parts, selected)

    return tuple(sorted(selected))


__all__ = [
    "DocumentsSourceInventoryError",
    "documents_backend_members",
]
