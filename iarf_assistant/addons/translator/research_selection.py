"""Exact, fail-closed research-tool selection for document-block translation.

The legacy ``research_plugin_ids`` setting is intentionally not consulted here.
It describes an older discovery surface and cannot prove which provider, tool,
or saved account a durable document operation is permitted to use.  Block
research therefore accepts only explicit, canonical identities and freezes
their existing tool declarations after validating the complete selection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from carlina.core.tool_risk import tool_is_read_only

from .block_contract import BlockContractError
from .block_research import freeze_block_research_tool_declarations

_AVAILABLE_STATES = frozenset({"active", "degraded"})
_SELECTION_KEYS = frozenset(
    {
        "tool_id",
        "provider_id",
        "provider_domain",
        "addon_id",
        "connection_mode",
        "connection_id",
    }
)
_MAX_SELECTIONS = 32
_MAX_SELECTION_BYTES = 16 * 1024
_MAX_RESEARCH_PRIORITY = 1000


def _error(code: str) -> BlockContractError:
    return BlockContractError(code)


def _bounded_json(value: object, code: str, *, maximum: int) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error(code) from exc
    if restored != value or len(encoded) > maximum:
        raise _error(code)
    return restored


def _identity(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.encode("utf-8")) > 128
    ):
        raise _error(code)
    return value


def _status_is_available(record: object) -> bool:
    return getattr(getattr(record, "status", None), "state", None) in _AVAILABLE_STATES


def _connection_definition_id(manager: Any, provider: object) -> str | None:
    """Return an exactly declared connector definition, if the provider has one.

    ``ProviderRecord`` preserves its manifest provider object in ``config``.
    Capability providers are a second declarative source, but we use one only
    when its identifier exactly matches the record's canonical provider ID.
    Ambiguous declarations fail closed instead of guessing a connector.
    """

    declarations: set[str] = set()
    config = getattr(provider, "config", None)
    if isinstance(config, Mapping):
        for key in ("connector_definition_id", "connector_definition"):
            value = config.get(key)
            if value is not None:
                declarations.add(_identity(value, "research-provider-connector"))

    contract_getter = getattr(manager, "get_addon_contract", None)
    if callable(contract_getter):
        contract = contract_getter(getattr(provider, "addon_id", ""))
        canonical_provider_id = getattr(provider, "provider_id", None)
        for candidate in getattr(contract, "capability_providers", ()) or ():
            if getattr(candidate, "id", None) != canonical_provider_id:
                continue
            value = getattr(candidate, "connector_definition", "")
            if value:
                declarations.add(_identity(value, "research-provider-connector"))

    if len(declarations) > 1:
        raise _error("research-provider-connector")
    return next(iter(declarations), None)


def _normalize_one(raw: object) -> dict[str, str]:
    material = _bounded_json(
        raw, "research-tool-selection", maximum=_MAX_SELECTION_BYTES
    )
    if not isinstance(material, dict) or set(material) != _SELECTION_KEYS:
        raise _error("research-tool-selection")
    result = {
        name: _identity(material[name], "research-tool-selection")
        for name in ("tool_id", "provider_id", "provider_domain", "addon_id")
    }
    mode = material["connection_mode"]
    connection_id = material["connection_id"]
    if mode not in {"saved", "connectionless"} or not isinstance(connection_id, str):
        raise _error("research-tool-selection")
    if mode == "saved":
        result["connection_id"] = _identity(connection_id, "research-tool-selection")
    elif connection_id:
        raise _error("research-tool-selection")
    else:
        result["connection_id"] = ""
    result["connection_mode"] = mode
    return result


def _validated_connection(
    manager: Any,
    *,
    owner_user_id: int,
    addon_id: str,
    connection_id: str,
    required_definition_id: str | None,
) -> object:
    validate = getattr(manager, "validate_visible_connection_binding", None)
    if not callable(validate):
        raise _error("research-tool-connection")
    try:
        connection = validate(
            connection_id=connection_id,
            owner_user_id=owner_user_id,
            connector_addon_id=addon_id,
        )
    except (KeyError, LookupError, PermissionError, RuntimeError, ValueError) as exc:
        raise _error("research-tool-connection") from exc
    if (
        getattr(connection, "id", None) != connection_id
        or getattr(connection, "connector_addon_id", addon_id) != addon_id
        or getattr(connection, "state", None) != "ready"
    ):
        raise _error("research-tool-connection")
    if required_definition_id is not None and (
        getattr(connection, "connector_definition_id", None) != required_definition_id
    ):
        raise _error("research-tool-connection")
    return connection


def _validate_one(
    manager: Any, *, owner_user_id: int, selected: dict[str, str]
) -> None:
    tool = manager.get_tool_record(selected["tool_id"])
    provider = manager.get_provider_record(
        selected["provider_domain"], selected["provider_id"]
    )
    if (
        tool is None
        or provider is None
        or not _status_is_available(tool)
        or not _status_is_available(provider)
        or getattr(tool, "id", None) != selected["tool_id"]
        or getattr(provider, "provider_id", None) != selected["provider_id"]
        or getattr(provider, "domain", None) != selected["provider_domain"]
        or getattr(tool, "addon_id", None) != selected["addon_id"]
        or getattr(provider, "addon_id", None) != selected["addon_id"]
        or not tool_is_read_only(tool)
    ):
        raise _error("research-tool-selection")
    declared_tool_ids = _declared_research_tool_ids(provider)
    if (
        declared_tool_ids is not None
        and selected["tool_id"] not in declared_tool_ids
    ):
        raise _error("research-tool-selection")
    required_definition_id = _connection_definition_id(manager, provider)
    if selected["connection_mode"] == "saved":
        _validated_connection(
            manager,
            owner_user_id=owner_user_id,
            addon_id=selected["addon_id"],
            connection_id=selected["connection_id"],
            required_definition_id=required_definition_id,
        )


def normalize_research_tool_selections(
    manager: Any,
    owner_user_id: int,
    raw: object,
) -> tuple[dict[str, str], ...]:
    """Normalize and prove a bounded collection of explicit research choices."""

    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool):
        raise _error("research-owner")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise _error("research-tool-selection")
    if len(raw) > _MAX_SELECTIONS:
        raise _error("research-tool-selection")

    normalized: list[dict[str, str]] = []
    seen_selection_ids: set[tuple[str, str, str, str, str, str]] = set()
    seen_tool_ids: set[str] = set()
    for value in raw:
        selected = _normalize_one(value)
        identity = (
            selected["addon_id"],
            selected["connection_id"],
            selected["connection_mode"],
            selected["provider_domain"],
            selected["provider_id"],
            selected["tool_id"],
        )
        if identity in seen_selection_ids or selected["tool_id"] in seen_tool_ids:
            raise _error("research-tool-selection")
        _validate_one(manager, owner_user_id=owner_user_id, selected=selected)
        seen_selection_ids.add(identity)
        seen_tool_ids.add(selected["tool_id"])
        normalized.append(selected)
    return tuple(normalized)


def freeze_research_selections(
    manager: Any,
    owner_user_id: int,
    raw: object,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Return canonical choices with the already-established frozen declarations.

    The two values are deliberately kept together so callers cannot retain a
    declaration generated from one selection list while dispatching another.
    This function never reads or migrates ``research_plugin_ids``.
    """

    selections = normalize_research_tool_selections(manager, owner_user_id, raw)
    declarations = freeze_block_research_tool_declarations(
        addon_manager=manager,
        owner_user_id=owner_user_id,
        selections=selections,
    )
    return {
        "research_tool_selections": selections,
        "tool_declarations": declarations,
    }


def _visible_connections(
    manager: Any,
    *,
    owner_user_id: int,
    addon_id: str,
) -> tuple[object, ...] | None:
    """Read a source-free visible connection catalog, or report no safe catalog."""

    public_list = getattr(manager, "visible_connections", None)
    try:
        if callable(public_list):
            values = public_list(owner_user_id, connector_addon_id=addon_id)
        else:
            from ...core.addons.connections import ConnectionService
            from ...extensions import db

            values = ConnectionService(db.session()).visible_to_user(
                owner_user_id, connector_addon_id=addon_id
            )
    except LookupError, PermissionError, RuntimeError, ValueError:
        return None
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    return tuple(values)


def _private_data_warning(tool: object, provider: object) -> tuple[bool, str | None]:
    """Surface declarative risk hints without treating them as authority."""

    metadata: list[object] = [
        getattr(tool, "side_effects", ()),
        getattr(tool, "resource_requirements", ()),
        getattr(provider, "capabilities", ()),
        getattr(provider, "operations", ()),
        getattr(provider, "config", {}),
        getattr(provider, "kind", ""),
    ]
    try:
        haystack = json.dumps(metadata, ensure_ascii=False, sort_keys=True).casefold()
    except TypeError, ValueError:
        haystack = ""
    private_tokens = (
        "filesystem",
        "file",
        "email",
        "mail",
        "cloud",
        "drive",
        "storage",
        "private",
        "connector",
        "account",
    )
    if any(token in haystack for token in private_tokens):
        return (
            True,
            (
                "This explicit read-only selection may access private or connected data; "
                "the workflow still grants no authority beyond the selected tool and connection."
            ),
        )
    return False, None


def _declared_research_tool_ids(provider: object) -> frozenset[str] | None:
    """Return an optional exact tool allowlist declared by one provider.

    Provider records historically described a whole add-on domain, so the
    catalog paired every read-only tool with every provider owned by that
    add-on.  A provider may now narrow that projection explicitly.  Missing
    metadata keeps the compatible broad behavior; malformed metadata fails
    closed for that provider instead of inventing tool/provider pairings.
    """

    config = getattr(provider, "config", None)
    if not isinstance(config, Mapping) or "research_tool_ids" not in config:
        return None
    raw = config["research_tool_ids"]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return frozenset()
    result: set[str] = set()
    for value in raw:
        try:
            result.add(_identity(value, "research-provider-tool"))
        except BlockContractError:
            return frozenset()
    return frozenset(result)


def _research_provider_hints(provider: object) -> dict[str, object]:
    """Project optional non-authoritative presentation/default hints safely."""

    config = getattr(provider, "config", None)
    raw = config.get("research") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    section = raw.get("section")
    role = raw.get("role")
    recommended = raw.get("recommended", False)
    priority = raw.get("priority", 0)
    if (
        section not in {"web", "library", "advanced"}
        or not isinstance(recommended, bool)
        or not isinstance(priority, int)
        or isinstance(priority, bool)
        or not 0 <= priority <= _MAX_RESEARCH_PRIORITY
    ):
        return {}
    try:
        normalized_role = _identity(role, "research-provider-role")
    except BlockContractError:
        return {}
    return {
        "research_section": section,
        "research_role": normalized_role,
        "research_recommended": recommended,
        "research_priority": priority,
    }


def exact_research_tool_catalog(
    manager: Any, owner_user_id: int
) -> tuple[dict[str, Any], ...]:
    """Return explicit executable tool/provider pairs without source content.

    A catalog row is never a permission grant.  It only gives the UI enough
    canonical information to form a later explicit selection.  Unavailable
    saved-connection pairs remain marked rather than being silently rewritten
    to a different account or provider.
    """

    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool):
        raise _error("research-owner")
    get_providers = getattr(manager, "get_provider_records", None)
    if not callable(get_providers):
        raise _error("research-tool-catalog")
    candidate_providers = get_providers()
    if not isinstance(candidate_providers, Sequence) or isinstance(
        candidate_providers, (str, bytes)
    ):
        raise _error("research-tool-catalog")
    providers = cast(Sequence[Any], candidate_providers)

    tools: list[Any] = []
    registry = getattr(getattr(manager, "registries", None), "tools", None)
    list_tools = getattr(registry, "list", None)
    if callable(list_tools):
        candidate_tools = list_tools()
        if not isinstance(candidate_tools, Sequence) or isinstance(
            candidate_tools, (str, bytes)
        ):
            raise _error("research-tool-catalog")
        tools = list(cast(Sequence[Any], candidate_tools))
    else:
        get_tools = getattr(manager, "get_tool_records", None)
        if callable(get_tools):
            candidate_tools = get_tools()
            if isinstance(candidate_tools, Sequence) and not isinstance(
                candidate_tools, (str, bytes)
            ):
                tools = list(cast(Sequence[Any], candidate_tools))
        else:
            raise _error("research-tool-catalog")

    rows: list[dict[str, Any]] = []
    connection_cache: dict[str, tuple[object, ...] | None] = {}
    for tool in tools:
        if not _status_is_available(tool) or not tool_is_read_only(tool):
            continue
        tool_id = getattr(tool, "id", None)
        addon_id = getattr(tool, "addon_id", None)
        if not isinstance(tool_id, str) or not isinstance(addon_id, str):
            continue
        # Resolve the exact record once more: catalog rows must not expose an
        # alias or a stale registry object as selectable identity.
        if getattr(manager.get_tool_record(tool_id), "id", None) != tool_id:
            continue
        for provider in providers:
            if (
                not _status_is_available(provider)
                or getattr(provider, "addon_id", None) != addon_id
                or not isinstance(getattr(provider, "provider_id", None), str)
                or not isinstance(getattr(provider, "domain", None), str)
            ):
                continue
            declared_tool_ids = _declared_research_tool_ids(provider)
            if declared_tool_ids is not None and tool_id not in declared_tool_ids:
                continue
            provider_id = provider.provider_id
            provider_domain = provider.domain
            # Do not emit an alias-based pair when exact canonical resolution
            # is unavailable or resolves to another provider.
            resolved = manager.get_provider_record(provider_domain, provider_id)
            if (
                resolved is None
                or getattr(resolved, "provider_id", None) != provider_id
                or getattr(resolved, "domain", None) != provider_domain
                or getattr(resolved, "addon_id", None) != addon_id
            ):
                continue
            try:
                required_definition_id = _connection_definition_id(manager, provider)
            except BlockContractError:
                continue
            if addon_id not in connection_cache:
                connection_cache[addon_id] = _visible_connections(
                    manager, owner_user_id=owner_user_id, addon_id=addon_id
                )
            visible = connection_cache[addon_id]
            connections: list[dict[str, str]] = []
            if visible is not None:
                for connection in visible:
                    if (
                        getattr(connection, "state", None) != "ready"
                        or getattr(connection, "connector_addon_id", None) != addon_id
                        or (
                            required_definition_id is not None
                            and getattr(connection, "connector_definition_id", None)
                            != required_definition_id
                        )
                    ):
                        continue
                    connection_id = getattr(connection, "id", None)
                    label = getattr(connection, "label", None)
                    if isinstance(connection_id, str) and isinstance(label, str):
                        connections.append({"id": connection_id, "label": label})
            private_data_capable, authority_warning = _private_data_warning(
                tool, provider
            )
            connectionless = required_definition_id is None
            executable = connectionless or bool(connections)
            row: dict[str, Any] = {
                "tool_id": tool_id,
                "tool_name": str(getattr(tool, "name", tool_id)),
                "provider_id": provider_id,
                "provider_name": str(getattr(provider, "name", provider_id)),
                "provider_domain": provider_domain,
                "addon_id": addon_id,
                "connection_modes": (
                    ("connectionless", "saved") if connectionless else ("saved",)
                ),
                "connections": tuple(connections),
                "required_connector_definition_id": required_definition_id,
                "executable": executable,
                "private_data_capable": private_data_capable,
                **_research_provider_hints(provider),
            }
            if not executable:
                row["unavailable_reason"] = "no_visible_ready_matching_connection"
            if authority_warning is not None:
                row["authority_warning"] = authority_warning
            rows.append(row)
    return tuple(rows)


__all__ = [
    "exact_research_tool_catalog",
    "freeze_research_selections",
    "normalize_research_tool_selections",
]
