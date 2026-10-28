"""Opaque, user-authorized references for project workspace roots."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_VALID_ACCESS = {"read_only", "read_write"}


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceRoot:
    id: str
    label: str
    path: Path
    access: str
    admin_only: bool
    allow_all_authenticated: bool
    allowed_user_ids: frozenset[int]

    def permits(self, *, user_id: int | None, is_admin: bool) -> bool:
        if is_admin:
            return True
        if user_id is None or self.admin_only:
            return False
        return self.allow_all_authenticated or user_id in self.allowed_user_ids

    def public_card(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "access": self.access,
        }


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceSelection:
    root: ProjectWorkspaceRoot
    path: Path
    relative_path: str

    def public_card(self) -> dict[str, str]:
        card = self.root.public_card()
        card["relative_path"] = self.relative_path
        return card


def _stable_root_id(path: Path) -> str:
    digest = hashlib.sha256(os.fsencode(str(path))).hexdigest()[:16]
    return f"root-{digest}"


def _allowed_users(value: Any) -> tuple[bool, frozenset[int]]:
    if value == "*":
        return True, frozenset()
    if not isinstance(value, Iterable) or isinstance(
        value,
        (str, bytes, Mapping),
    ):
        return False, frozenset()
    allow_all = False
    user_ids: set[int] = set()
    for item in value:
        if item == "*":
            allow_all = True
            continue
        if isinstance(item, bool):
            continue
        try:
            parsed = int(item)
        except TypeError, ValueError:
            continue
        if parsed > 0:
            user_ids.add(parsed)
    return allow_all, frozenset(user_ids)


def _configured_items(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, str):
        return [item.strip() for item in value.split(os.pathsep) if item.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return list(value)
    return []


def parse_project_workspace_roots(value: Any) -> tuple[ProjectWorkspaceRoot, ...]:
    """Parse root configuration; invalid or duplicate entries fail closed."""

    roots: list[ProjectWorkspaceRoot] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for item in _configured_items(value):
        config = dict(item) if isinstance(item, Mapping) else {"path": item}
        raw_path = str(config.get("path") or "").strip()
        if not raw_path:
            continue
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except OSError, RuntimeError:
            continue
        if not path.is_dir():
            continue
        root_id = str(config.get("id") or _stable_root_id(path)).strip()
        if not _ROOT_ID_PATTERN.fullmatch(root_id):
            continue
        if root_id in seen_ids or path in seen_paths:
            continue
        access = str(config.get("access") or "read_write").strip().lower()
        if access not in _VALID_ACCESS:
            continue
        allow_all, allowed_user_ids = _allowed_users(config.get("allowed_user_ids"))
        has_explicit_audience = bool(allow_all or allowed_user_ids)
        admin_only = bool(config.get("admin_only", not has_explicit_audience))
        label = str(config.get("label") or path.name or root_id).strip()[:100]
        roots.append(
            ProjectWorkspaceRoot(
                id=root_id,
                label=label,
                path=path,
                access=access,
                admin_only=admin_only,
                allow_all_authenticated=allow_all,
                allowed_user_ids=allowed_user_ids,
            )
        )
        seen_ids.add(root_id)
        seen_paths.add(path)
    return tuple(roots)


def authorized_project_workspace_roots(
    value: Any,
    *,
    user_id: int | None,
    is_admin: bool,
) -> tuple[ProjectWorkspaceRoot, ...]:
    return tuple(
        root
        for root in parse_project_workspace_roots(value)
        if root.permits(user_id=user_id, is_admin=is_admin)
    )


def _resolve_relative_path(root: Path, relative_path: Any) -> tuple[Path, str]:
    relative_text = str(relative_path or "").strip()
    relative = Path(relative_text) if relative_text else Path()
    if relative.is_absolute():
        raise PermissionError("workspace_path must be relative to the selected root.")
    candidate = (root / relative).resolve()
    try:
        normalized = candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError("workspace_path escapes the selected root.") from exc
    if not candidate.is_dir():
        raise ValueError("The selected project workspace directory does not exist.")
    relative_result = "" if normalized == Path() else normalized.as_posix()
    return candidate, relative_result


def resolve_project_workspace_selection(
    value: Any,
    *,
    user_id: int | None,
    is_admin: bool,
    root_id: Any = None,
    relative_path: Any = None,
    legacy_absolute_path: Any = None,
) -> ProjectWorkspaceSelection:
    """Resolve an opaque root ID; raw paths remain admin-only compatibility."""

    roots = authorized_project_workspace_roots(
        value,
        user_id=user_id,
        is_admin=is_admin,
    )
    selected = next(
        (root for root in roots if root.id == str(root_id or "").strip()),
        None,
    )
    if selected is not None:
        path, normalized = _resolve_relative_path(selected.path, relative_path)
        return ProjectWorkspaceSelection(selected, path, normalized)

    legacy_text = str(legacy_absolute_path or "").strip()
    if legacy_text and is_admin:
        candidate = Path(legacy_text).expanduser().resolve()
        for root in roots:
            try:
                normalized = candidate.relative_to(root.path)
            except ValueError:
                continue
            if not candidate.is_dir():
                raise ValueError(
                    "The selected project workspace directory does not exist."
                )
            relative_result = "" if normalized == Path() else normalized.as_posix()
            return ProjectWorkspaceSelection(root, candidate, relative_result)

    raise PermissionError(
        "The project workspace root is unknown or not authorized for this user."
    )


def resolve_project_workspace_access(
    selection: ProjectWorkspaceSelection,
    requested_access: Any,
) -> str:
    """Validate that a request does not exceed the configured root access."""

    access = str(requested_access or selection.root.access).strip().lower()
    if access not in _VALID_ACCESS:
        raise ValueError("access must be read_only or read_write.")
    if selection.root.access == "read_only" and access == "read_write":
        raise PermissionError(
            "The selected project workspace root is configured as read-only."
        )
    return access


def public_project_workspace_roots(
    value: Any,
    *,
    user_id: int | None,
    is_admin: bool,
) -> list[dict[str, str]]:
    return [
        root.public_card()
        for root in authorized_project_workspace_roots(
            value,
            user_id=user_id,
            is_admin=is_admin,
        )
    ]


def public_workspace_reference_for_path(
    value: Any,
    path: Any,
    *,
    user_id: int | None,
    is_admin: bool,
) -> dict[str, str] | None:
    try:
        candidate = Path(str(path or "")).expanduser().resolve()
    except OSError, RuntimeError, ValueError:
        return None
    for root in authorized_project_workspace_roots(
        value,
        user_id=user_id,
        is_admin=is_admin,
    ):
        try:
            relative = candidate.relative_to(root.path)
        except ValueError:
            continue
        card = root.public_card()
        card["relative_path"] = "" if relative == Path() else relative.as_posix()
        return card
    return None


__all__ = [
    "ProjectWorkspaceRoot",
    "ProjectWorkspaceSelection",
    "authorized_project_workspace_roots",
    "parse_project_workspace_roots",
    "public_project_workspace_roots",
    "public_workspace_reference_for_path",
    "resolve_project_workspace_access",
    "resolve_project_workspace_selection",
]
