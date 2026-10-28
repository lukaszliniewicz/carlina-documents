"""Static, no-import contracts for add-on Alembic revision bundles."""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_REVISION_FILES = 1_000
MAX_REVISION_FILE_BYTES = 1 * 1024 * 1024
MAX_REVISION_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REVISION_ID_LENGTH = 32
MAX_VERSION_TABLE_LENGTH = 63
MAX_PURGE_TABLES = 100
MAX_OWNED_TABLES = 10_000
REVISION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION_TABLE_PREFIX = "alembic_version_"
_ALLOWED_MIGRATION_IMPORT_PREFIXES = (
    "alembic",
    "sqlalchemy",
)
_ALLOWED_CARLINA_MIGRATION_IMPORTS = frozenset(
    {
        "iarf_assistant",
        "iarf_assistant.database.types",
    }
)
_DISALLOWED_DYNAMIC_IMPORT_MODULES = frozenset({"importlib"})
_DISALLOWED_DYNAMIC_CALLS = frozenset({"__import__", "eval", "exec"})


@dataclass(frozen=True, slots=True)
class AddonMigrationRevision:
    revision: str
    down_revision: str | None
    path: Path
    source_sha256: str
    created_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AddonMigrationContract:
    addon_id: str
    migrations_dir: Path
    versions_dir: Path
    version_table: str
    purge_tables: tuple[str, ...]
    owned_tables: tuple[str, ...]
    revisions: tuple[AddonMigrationRevision, ...]
    graph_sha256: str

    @property
    def base_revision(self) -> str:
        return self.revisions[0].revision

    @property
    def head_revision(self) -> str:
        return self.revisions[-1].revision

    @property
    def revision_ids(self) -> tuple[str, ...]:
        return tuple(item.revision for item in self.revisions)

    def resolve_target(self, target: str) -> str | None:
        normalized = str(target or "").strip()
        if normalized == "head":
            return self.head_revision
        if normalized == "base":
            return None
        if normalized in self.revision_ids:
            return normalized
        raise ValueError(
            f"Unknown revision {normalized!r} for add-on {self.addon_id!r}."
        )

    def revision_index(self, revision: str | None) -> int:
        if revision is None:
            return -1
        try:
            return self.revision_ids.index(revision)
        except ValueError as exc:
            raise ValueError(
                f"Revision {revision!r} is not part of add-on {self.addon_id!r}."
            ) from exc


def expected_addon_version_table(addon_id: str) -> str:
    """Return a portable, collision-resistant table name for an add-on ID."""

    normalized_id = str(addon_id or "").strip().casefold()
    safe_stem = re.sub(r"[^a-z0-9_]", "_", normalized_id).strip("_") or "addon"
    changed = safe_stem != normalized_id
    digest_suffix = f"_{hashlib.sha256(normalized_id.encode()).hexdigest()[:8]}"
    suffix = digest_suffix if changed else ""
    available = MAX_VERSION_TABLE_LENGTH - len(_VERSION_TABLE_PREFIX) - len(suffix)
    if len(safe_stem) > available:
        suffix = digest_suffix
        available = MAX_VERSION_TABLE_LENGTH - len(_VERSION_TABLE_PREFIX) - len(suffix)
        safe_stem = safe_stem[:available]
    return f"{_VERSION_TABLE_PREFIX}{safe_stem}{suffix}"


def _regular_file_issue(path: Path, label: str) -> str | None:
    if not path.is_file() or path.is_symlink():
        return f"{label} is missing or is not a regular file"
    return None


def _migration_layout(
    addon_dir: Path,
) -> tuple[Path, Path, list[Path], tuple[str, ...]]:
    migrations = addon_dir / "migrations"
    versions = migrations / "versions"
    issues: list[str] = []
    if not migrations.exists():
        return migrations, versions, [], ("the migrations directory is missing",)
    if not migrations.is_dir() or migrations.is_symlink():
        return (
            migrations,
            versions,
            [],
            ("migrations must be a real directory, not a link",),
        )
    config_issue = _regular_file_issue(migrations / "alembic.ini", "alembic.ini")
    if config_issue:
        issues.append(config_issue)
    environment = migrations / "env.py"
    if environment.exists() and (not environment.is_file() or environment.is_symlink()):
        issues.append("env.py must be a regular file when present")
    if not versions.is_dir() or versions.is_symlink():
        issues.append("versions is missing or is not a real directory")
        return migrations, versions, [], tuple(issues)
    try:
        entries = sorted(versions.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        issues.append(f"versions cannot be read: {exc}")
        return migrations, versions, [], tuple(issues)
    revision_files: list[Path] = []
    for candidate in entries:
        if candidate.name == "__pycache__" and candidate.is_dir():
            continue
        if candidate.is_symlink():
            issues.append(f"versions/{candidate.name} must not be a symbolic link")
        elif candidate.is_file() and candidate.suffix.casefold() == ".py":
            revision_files.append(candidate)
    if not revision_files:
        issues.append("versions contains no reviewed Python revision")
    if len(revision_files) > MAX_REVISION_FILES:
        issues.append(f"versions contains more than {MAX_REVISION_FILES} revisions")
    return migrations, versions, revision_files, tuple(issues)


def _read_migration_settings(
    config_path: Path,
    addon_id: str,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, configparser.Error) as exc:
        return None, (), (f"alembic.ini cannot be parsed: {exc}",)
    if not parser.has_section("alembic"):
        return None, (), ("alembic.ini has no [alembic] section",)
    version_table = parser.get("alembic", "version_table", fallback="").strip()
    expected = expected_addon_version_table(addon_id)
    issues: list[str] = []
    if not version_table:
        issues.append("alembic.ini must declare an explicit version_table")
    elif (
        not SQL_IDENTIFIER_PATTERN.fullmatch(version_table)
        or len(version_table) > MAX_VERSION_TABLE_LENGTH
    ):
        issues.append("version_table must be a portable SQL identifier")
    elif version_table != expected:
        issues.append(f"version_table must be {expected!r} for add-on {addon_id!r}")
    version_schema = parser.get("alembic", "version_table_schema", fallback="").strip()
    if version_schema:
        issues.append("version_table_schema is not supported for local add-on schemas")
    raw_purge_tables = parser.get("alembic", "purge_tables", fallback="")
    purge_tables: list[str] = []
    seen_tables: set[str] = set()
    for table_name in re.split(r"[\s,]+", raw_purge_tables.strip()):
        if not table_name:
            continue
        folded = table_name.casefold()
        if (
            not SQL_IDENTIFIER_PATTERN.fullmatch(table_name)
            or len(table_name) > MAX_VERSION_TABLE_LENGTH
        ):
            issues.append(
                f"purge table {table_name!r} is not a portable SQL identifier"
            )
        elif folded in seen_tables:
            issues.append(f"purge table {table_name!r} is declared more than once")
        elif folded == str(version_table or "").casefold():
            issues.append("purge_tables must not include the Alembic version table")
        else:
            seen_tables.add(folded)
            purge_tables.append(table_name)
    if len(purge_tables) > MAX_PURGE_TABLES:
        issues.append(f"purge_tables contains more than {MAX_PURGE_TABLES} entries")
    return version_table or None, tuple(purge_tables), tuple(issues)


def _literal_assignment(tree: ast.Module, name: str) -> tuple[Any, str | None]:
    values: list[ast.expr | None] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in statement.targets
            ):
                values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ):
            values.append(statement.value)
    if len(values) != 1 or values[0] is None:
        return None, f"must assign {name} exactly once at module scope"
    try:
        return ast.literal_eval(values[0]), None
    except TypeError, ValueError:
        return None, f"{name} must be a literal value"


def _valid_zero_argument_function(tree: ast.Module, name: str) -> bool:
    matches = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef) and statement.name == name
    ]
    if len(matches) != 1:
        return False
    arguments = matches[0].args
    return not (
        arguments.posonlyargs
        or arguments.args
        or arguments.vararg
        or arguments.kwonlyargs
        or arguments.kwarg
    )


def _migration_import_issues(tree: ast.Module, label: str) -> list[str]:
    issues: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                issues.append(f"{label} must not use relative imports")
                continue
            modules = [str(node.module or "")]
        else:
            continue
        for module in modules:
            root = module.partition(".")[0]
            allowed = (
                module not in _DISALLOWED_DYNAMIC_IMPORT_MODULES
                and (
                    module in _ALLOWED_CARLINA_MIGRATION_IMPORTS
                    or root in sys.stdlib_module_names
                    or any(
                        module == prefix or module.startswith(prefix + ".")
                        for prefix in _ALLOWED_MIGRATION_IMPORT_PREFIXES
                    )
                )
                and not (
                    isinstance(node, ast.ImportFrom) and module == "iarf_assistant"
                )
            )
            if not allowed:
                issues.append(
                    f"{label} imports {module!r}; revisions must be "
                    "self-contained after package removal"
                )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id in _DISALLOWED_DYNAMIC_CALLS
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
        ):
            issues.append(
                f"{label} uses dynamic code or module loading; revisions "
                "must be statically self-contained"
            )
    return issues


def _migration_module_structure_issues(tree: ast.Module, label: str) -> list[str]:
    issues: list[str] = []
    for index, statement in enumerate(tree.body):
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.FunctionDef):
            if statement.decorator_list:
                issues.append(f"{label} must not execute module-level decorators")
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            if value is None:
                issues.append(
                    f"{label} module-level assignments must contain literal values"
                )
                continue
            try:
                ast.literal_eval(value)
            except TypeError, ValueError:
                issues.append(
                    f"{label} module-level assignments must contain literal values"
                )
            continue
        issues.append(
            f"{label} contains executable module-level {type(statement).__name__} code"
        )
    return issues


def _read_revision_source(
    path: Path,
    label: str,
) -> tuple[str | None, ast.Module | None, tuple[str, ...]]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, None, (f"{label} cannot be inspected: {exc}",)
    if size > MAX_REVISION_FILE_BYTES:
        return None, None, (f"{label} exceeds {MAX_REVISION_FILE_BYTES} bytes",)
    try:
        source = path.read_text(encoding="utf-8-sig")
        return source, ast.parse(source, filename=str(path)), ()
    except (OSError, UnicodeError, SyntaxError) as exc:
        return None, None, (f"{label} cannot be parsed: {exc}",)


def _revision_identifier_issues(
    label: str,
    field: str,
    value: object,
) -> list[str]:
    if field == "revision" and (not isinstance(value, str) or not value):
        return [f"{label} revision must be a non-empty string"]
    if field == "down_revision" and value is None:
        return []
    if not isinstance(value, str):
        return [f"{label} down_revision must be one string or None"]
    if (
        not value
        or len(value) > MAX_REVISION_ID_LENGTH
        or not REVISION_ID_PATTERN.fullmatch(value)
    ):
        if field == "revision":
            return [
                (
                    f"{label} revision must be a safe identifier of at most "
                    f"{MAX_REVISION_ID_LENGTH} characters"
                )
            ]
        return [f"{label} down_revision is not a safe revision identifier"]
    if value.casefold() in {"base", "head"}:
        return [f"{label} {field} uses a reserved Alembic target name"]
    return []


def _revision_metadata(
    tree: ast.Module,
    label: str,
) -> tuple[object, object, list[str]]:
    revision, revision_issue = _literal_assignment(tree, "revision")
    down_revision, down_issue = _literal_assignment(tree, "down_revision")
    issues: list[str] = []
    if revision_issue:
        issues.append(f"{label} {revision_issue}")
    if down_issue:
        issues.append(f"{label} {down_issue}")
    issues.extend(_revision_identifier_issues(label, "revision", revision))
    issues.extend(_revision_identifier_issues(label, "down_revision", down_revision))
    for metadata_name in ("branch_labels", "depends_on"):
        value, issue = _literal_assignment(tree, metadata_name)
        if issue is not None:
            issues.append(f"{label} {issue}")
        elif value is not None:
            issues.append(f"{label} {metadata_name} must be None")
    return revision, down_revision, issues


def _migration_entry_point_issues(tree: ast.Module, label: str) -> list[str]:
    return [
        (f"{label} must define one synchronous zero-argument {function_name} function")
        for function_name in ("upgrade", "downgrade")
        if not _valid_zero_argument_function(tree, function_name)
    ]


def _created_table_contract(
    tree: ast.Module,
    label: str,
) -> tuple[list[str], list[str]]:
    created_tables: list[str] = []
    issues: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"create_table", "rename_table"}
        ):
            continue
        argument_index = 1 if node.func.attr == "rename_table" else 0
        if len(node.args) <= argument_index:
            issues.append(f"{label} {node.func.attr} must name its table literally")
            continue
        try:
            table_name = ast.literal_eval(node.args[argument_index])
        except TypeError, ValueError:
            table_name = None
        if (
            not isinstance(table_name, str)
            or not SQL_IDENTIFIER_PATTERN.fullmatch(table_name)
            or len(table_name) > MAX_VERSION_TABLE_LENGTH
        ):
            issues.append(
                f"{label} {node.func.attr} must use a portable literal table name"
            )
        elif table_name not in created_tables:
            created_tables.append(table_name)
    return created_tables, issues


def _parse_revision(
    path: Path,
) -> tuple[AddonMigrationRevision | None, tuple[str, ...]]:
    label = f"versions/{path.name}"
    source, tree, read_issues = _read_revision_source(path, label)
    if source is None or tree is None:
        return None, read_issues
    issues: list[str] = []
    issues.extend(_migration_import_issues(tree, label))
    issues.extend(_migration_module_structure_issues(tree, label))
    revision, down_revision, metadata_issues = _revision_metadata(tree, label)
    issues.extend(metadata_issues)
    issues.extend(_migration_entry_point_issues(tree, label))
    created_tables, table_issues = _created_table_contract(tree, label)
    issues.extend(table_issues)
    if issues or not isinstance(revision, str):
        return None, tuple(issues)
    return (
        AddonMigrationRevision(
            revision,
            down_revision if isinstance(down_revision, str) else None,
            path,
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
            tuple(created_tables),
        ),
        (),
    )


def _graph_digest(
    version_table: str,
    purge_tables: tuple[str, ...],
    owned_tables: tuple[str, ...],
    revisions: tuple[AddonMigrationRevision, ...],
) -> str:
    payload = {
        "format": "carlina.alembic-python-v1",
        "version_table": version_table,
        "purge_tables": list(purge_tables),
        "owned_tables": list(owned_tables),
        "revisions": [
            {
                "revision": item.revision,
                "down_revision": item.down_revision,
                "source_sha256": item.source_sha256,
            }
            for item in revisions
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _linear_revision_chain(
    revisions: list[AddonMigrationRevision],
) -> tuple[tuple[AddonMigrationRevision, ...] | None, tuple[str, ...]]:
    issues: list[str] = []
    by_id: dict[str, AddonMigrationRevision] = {}
    for item in revisions:
        if item.revision in by_id:
            issues.append(f"revision {item.revision!r} is declared more than once")
        else:
            by_id[item.revision] = item
    if issues:
        return None, tuple(issues)
    bases = [item for item in revisions if item.down_revision is None]
    if len(bases) != 1:
        issues.append(f"the revision graph must have one base, found {len(bases)}")
    children: dict[str, list[AddonMigrationRevision]] = {}
    for item in revisions:
        parent = item.down_revision
        if parent is None:
            continue
        if parent not in by_id:
            issues.append(
                f"revision {item.revision!r} references missing parent {parent!r}"
            )
            continue
        children.setdefault(parent, []).append(item)
    branches = sorted(parent for parent, items in children.items() if len(items) > 1)
    if branches:
        issues.append(
            "the revision graph must be linear; branching parents: "
            + ", ".join(repr(item) for item in branches)
        )
    if issues or len(bases) != 1:
        return None, tuple(issues)
    ordered: list[AddonMigrationRevision] = []
    seen: set[str] = set()
    current = bases[0]
    while current.revision not in seen:
        ordered.append(current)
        seen.add(current.revision)
        next_items = children.get(current.revision, [])
        if not next_items:
            break
        current = next_items[0]
    if len(ordered) != len(revisions):
        issues.append("the revision graph is cyclic or disconnected")
        return None, tuple(issues)
    return tuple(ordered), ()


def inspect_addon_migration_contract(
    addon_dir: str | Path,
    addon_id: str,
) -> tuple[AddonMigrationContract | None, tuple[str, ...]]:
    """Inspect one migration bundle without importing its Python modules."""

    root = Path(addon_dir).resolve()
    migrations, versions, files, layout_issues = _migration_layout(root)
    issues = list(layout_issues)
    version_table: str | None = None
    purge_tables: tuple[str, ...] = ()
    if (migrations / "alembic.ini").is_file():
        version_table, purge_tables, config_issues = _read_migration_settings(
            migrations / "alembic.ini",
            addon_id,
        )
        issues.extend(config_issues)
    total_bytes = 0
    revisions: list[AddonMigrationRevision] = []
    for path in files[:MAX_REVISION_FILES]:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        revision, revision_issues = _parse_revision(path)
        issues.extend(revision_issues)
        if revision is not None:
            revisions.append(revision)
    if total_bytes > MAX_REVISION_TOTAL_BYTES:
        issues.append(
            f"revision sources exceed {MAX_REVISION_TOTAL_BYTES} bytes in total"
        )
    ordered, graph_issues = (
        _linear_revision_chain(revisions) if revisions else (None, ())
    )
    issues.extend(graph_issues)
    owned_tables = tuple(
        dict.fromkeys(
            table_name
            for revision in (ordered or ())
            for table_name in revision.created_tables
        )
    )
    if len(owned_tables) > MAX_OWNED_TABLES:
        issues.append(f"migration graph owns more than {MAX_OWNED_TABLES} tables")
    undeclared_purge_ownership = sorted(set(purge_tables) - set(owned_tables))
    if undeclared_purge_ownership:
        issues.append(
            "purge_tables are not created or adopted by this migration graph: "
            + ", ".join(repr(item) for item in undeclared_purge_ownership)
        )
    if version_table is not None and version_table in owned_tables:
        issues.append("the migration graph must not create its Alembic version table")
    if issues or version_table is None or ordered is None:
        return None, tuple(dict.fromkeys(issues))
    return (
        AddonMigrationContract(
            addon_id=addon_id,
            migrations_dir=migrations,
            versions_dir=versions,
            version_table=version_table,
            purge_tables=purge_tables,
            owned_tables=owned_tables,
            revisions=ordered,
            graph_sha256=_graph_digest(
                version_table,
                purge_tables,
                owned_tables,
                ordered,
            ),
        ),
        (),
    )


def migration_upgrade_issues(
    previous: AddonMigrationContract | None,
    target: AddonMigrationContract | None,
) -> tuple[str, ...]:
    """Return unsafe changes between two installed package generations."""

    if previous is None:
        return ()
    if target is None:
        return ("the target package removes the existing migration contract",)
    issues: list[str] = []
    if previous.version_table != target.version_table:
        issues.append("the target package changes migration version-table ownership")
    if not set(previous.purge_tables).issubset(target.purge_tables):
        issues.append("the target package removes declared final-purge table ownership")
    if len(target.revisions) < len(previous.revisions):
        issues.append("the target package truncates the installed revision history")
    else:
        for before, after in zip(previous.revisions, target.revisions, strict=False):
            if (
                before.revision != after.revision
                or before.down_revision != after.down_revision
                or before.source_sha256 != after.source_sha256
            ):
                issues.append(
                    f"the target package rewrites installed revision {before.revision!r}"
                )
                break
    return tuple(issues)


def stored_migration_upgrade_issues(
    previous: Mapping[str, Any] | None,
    target: AddonMigrationContract | None,
) -> tuple[str, ...]:
    """Validate an update against the persisted public migration descriptor.

    The descriptor intentionally stores only the full graph digest. Recomputing
    that digest over the target's historical prefix proves that every retained
    revision has the same parent and source without persisting executable code.
    """

    if previous is None:
        return ()
    if target is None:
        return ("the target package removes the existing migration contract",)
    try:
        previous_format = str(previous["format"])
        previous_table = str(previous["version_table"])
        raw_purge_tables = previous.get("purge_tables", ())
        raw_owned_tables = previous["owned_tables"]
        raw_revision_ids = previous["revision_ids"]
        previous_digest = str(previous["graph_sha256"])
        if (
            previous_format != "alembic_python_v1"
            or not isinstance(raw_purge_tables, (list, tuple))
            or not isinstance(raw_owned_tables, (list, tuple))
            or not isinstance(raw_revision_ids, (list, tuple))
        ):
            raise TypeError
        previous_purge_tables = tuple(str(item) for item in raw_purge_tables)
        previous_owned_tables = tuple(str(item) for item in raw_owned_tables)
        previous_revision_ids = tuple(str(item) for item in raw_revision_ids)
        if (
            not previous_revision_ids
            or len(previous_digest) != 64
            or any(not item for item in previous_revision_ids)
        ):
            raise ValueError
    except KeyError, TypeError, ValueError:
        return ("the installed migration descriptor is malformed",)

    issues: list[str] = []
    if previous_table != target.version_table:
        issues.append("the target package changes migration version-table ownership")
    if not set(previous_purge_tables).issubset(target.purge_tables):
        issues.append("the target package removes declared final-purge table ownership")
    if len(target.revisions) < len(previous_revision_ids):
        issues.append("the target package truncates the installed revision history")
        return tuple(issues)
    target_prefix = target.revisions[: len(previous_revision_ids)]
    if tuple(item.revision for item in target_prefix) != previous_revision_ids:
        issues.append("the target package rewrites the installed revision sequence")
        return tuple(issues)
    prefix_owned_tables = tuple(
        dict.fromkeys(
            table_name
            for revision in target_prefix
            for table_name in revision.created_tables
        )
    )
    if prefix_owned_tables != previous_owned_tables:
        issues.append("the target package changes installed database table ownership")
        return tuple(issues)
    prefix_digest = _graph_digest(
        previous_table,
        previous_purge_tables,
        prefix_owned_tables,
        target_prefix,
    )
    if prefix_digest != previous_digest:
        issues.append("the target package rewrites installed migration source")
    return tuple(issues)


__all__ = [
    "MAX_REVISION_ID_LENGTH",
    "AddonMigrationContract",
    "AddonMigrationRevision",
    "expected_addon_version_table",
    "inspect_addon_migration_contract",
    "migration_upgrade_issues",
    "stored_migration_upgrade_issues",
]
