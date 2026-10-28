"""Exact selection/catalog tests for block-translation research tools."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

_contract = importlib.import_module(
    "iarf" + "_assistant.addons.translator.block_contract"
)
_selection = importlib.import_module(
    "iarf" + "_assistant.addons.translator.research_selection"
)

BlockContractError = _contract.BlockContractError


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
                "additionalProperties": False,
            },
        },
    }


def _tool(tool_id: str, function_name: str, *, addon_id: str = "research"):
    return SimpleNamespace(
        id=tool_id,
        addon_id=addon_id,
        name=function_name.replace("_", " ").title(),
        legacy_name=function_name,
        function_schema=_schema(function_name),
        side_effects=["external_read"],
        approval_required=False,
        resource_requirements=[],
        status=SimpleNamespace(state="active"),
    )


def _provider(
    provider_id: str,
    *,
    addon_id: str = "research",
    domain: str = "research",
    connector_definition_id: str | None = None,
    research_tool_ids: object | None = None,
    research: object | None = None,
):
    config = {}
    if connector_definition_id is not None:
        config["connector_definition_id"] = connector_definition_id
    if research_tool_ids is not None:
        config["research_tool_ids"] = research_tool_ids
    if research is not None:
        config["research"] = research
    return SimpleNamespace(
        id=f"{domain}.{provider_id}",
        addon_id=addon_id,
        provider_id=provider_id,
        domain=domain,
        name=provider_id.title(),
        aliases=[f"{provider_id}-alias"],
        config=config,
        capabilities=["search"],
        operations=["read"],
        kind="cloud",
        status=SimpleNamespace(state="active"),
    )


class _ToolRegistry:
    def __init__(self, records):
        self._records = tuple(records)

    def list(self):
        return list(self._records)


class _Manager:
    def __init__(self, tools, providers, connections=()):
        self.tools = tuple(tools)
        self.providers = tuple(providers)
        self.connections = tuple(connections)
        self.registries = SimpleNamespace(tools=_ToolRegistry(self.tools))
        self.loaded_addons = {record.addon_id: object() for record in self.tools}

    def get_tool_record(self, identity):
        for tool in self.tools:
            if identity in {tool.id, tool.legacy_name}:
                return tool
        return None

    def get_provider_record(self, domain, identity):
        for provider in self.providers:
            if domain != provider.domain:
                continue
            if identity in {provider.provider_id, provider.id, *provider.aliases}:
                return provider
        return None

    def get_provider_records(self):
        return list(self.providers)

    def validate_visible_connection_binding(
        self, *, connection_id, owner_user_id, connector_addon_id
    ):
        assert owner_user_id == 41
        for connection in self.connections:
            if connection.id != connection_id:
                continue
            if connection.connector_addon_id != connector_addon_id:
                raise PermissionError("other connector")
            if connection.state != "ready":
                raise RuntimeError("not ready")
            return connection
        raise LookupError("missing")

    def visible_connections(self, owner_user_id, *, connector_addon_id):
        assert owner_user_id == 41
        return [
            item
            for item in self.connections
            if item.connector_addon_id == connector_addon_id
        ]

    def get_addon_contract(self, _addon_id):
        return None


def _connection(connection_id: str, *, definition: str = "research.account"):
    return SimpleNamespace(
        id=connection_id,
        label=f"Account {connection_id}",
        connector_addon_id="research",
        connector_definition_id=definition,
        state="ready",
    )


def _selection_for(
    tool_id: str,
    provider_id: str,
    *,
    connection_mode: str = "connectionless",
    connection_id: str = "",
    addon_id: str = "research",
    domain: str = "research",
):
    return {
        "tool_id": tool_id,
        "provider_id": provider_id,
        "provider_domain": domain,
        "addon_id": addon_id,
        "connection_mode": connection_mode,
        "connection_id": connection_id,
    }


def test_catalog_keeps_every_explicit_tool_provider_pair_distinct():
    manager = _Manager(
        [_tool("tool.search", "search"), _tool("tool.lookup", "lookup")],
        [_provider("alpha"), _provider("beta")],
    )

    rows = _selection.exact_research_tool_catalog(manager, 41)

    assert {(row["tool_id"], row["provider_id"]) for row in rows} == {
        ("tool.search", "alpha"),
        ("tool.search", "beta"),
        ("tool.lookup", "alpha"),
        ("tool.lookup", "beta"),
    }
    assert all(row["executable"] is True for row in rows)
    assert all(row["connections"] == () for row in rows)
    assert all(row["private_data_capable"] is True for row in rows)


def test_catalog_honours_an_exact_provider_research_tool_allowlist():
    manager = _Manager(
        [_tool("tool.search", "search"), _tool("tool.lookup", "lookup")],
        [_provider(
            "alpha",
            research_tool_ids=["tool.search"],
            research={
                "section": "web",
                "role": "general_search",
                "recommended": True,
                "priority": 10,
            },
        )],
    )

    rows = _selection.exact_research_tool_catalog(manager, 41)

    assert [(row["tool_id"], row["provider_id"]) for row in rows] == [
        ("tool.search", "alpha")
    ]
    assert rows[0]["research_section"] == "web"
    assert rows[0]["research_role"] == "general_search"
    assert rows[0]["research_recommended"] is True
    with pytest.raises(BlockContractError, match="research-tool-selection"):
        _selection.normalize_research_tool_selections(
            manager, 41, [_selection_for("tool.lookup", "alpha")]
        )


@pytest.mark.parametrize("declared", ["tool.search", [""], [7]])
def test_catalog_fails_closed_for_a_malformed_provider_tool_allowlist(declared):
    manager = _Manager(
        [_tool("tool.search", "search")],
        [_provider("alpha", research_tool_ids=declared)],
    )

    assert _selection.exact_research_tool_catalog(manager, 41) == ()


@pytest.mark.parametrize(
    "selection",
    [
        _selection_for("tool.search", "alpha-alias"),
        _selection_for("tool.search", "alpha", addon_id="other"),
        _selection_for("tool.search", "alpha", domain="other"),
    ],
)
def test_alias_cross_addon_and_cross_domain_are_rejected(selection):
    manager = _Manager([_tool("tool.search", "search")], [_provider("alpha")])

    with pytest.raises(BlockContractError, match="research-tool-selection"):
        _selection.normalize_research_tool_selections(manager, 41, [selection])


def test_saved_connection_must_match_declared_provider_connector_definition():
    manager = _Manager(
        [_tool("tool.search", "search")],
        [_provider("alpha", connector_definition_id="research.account")],
        [_connection("wrong", definition="other.account")],
    )
    selection = _selection_for(
        "tool.search", "alpha", connection_mode="saved", connection_id="wrong"
    )

    with pytest.raises(BlockContractError, match="research-tool-connection"):
        _selection.freeze_research_selections(manager, 41, [selection])


def test_catalog_exposes_multiple_matching_connections_without_defaulting():
    manager = _Manager(
        [_tool("tool.search", "search")],
        [_provider("alpha", connector_definition_id="research.account")],
        [_connection("one"), _connection("two")],
    )

    row = _selection.exact_research_tool_catalog(manager, 41)[0]

    assert row["connection_modes"] == ("saved",)
    assert row["connections"] == (
        {"id": "one", "label": "Account one"},
        {"id": "two", "label": "Account two"},
    )
    assert "default_connection_id" not in row
    frozen = _selection.freeze_research_selections(
        manager,
        41,
        [
            _selection_for(
                "tool.search", "alpha", connection_mode="saved", connection_id="two"
            )
        ],
    )
    assert frozen["research_tool_selections"][0]["connection_id"] == "two"


def test_connectionless_selection_is_exact_and_cannot_carry_a_connection_id():
    manager = _Manager([_tool("tool.search", "search")], [_provider("alpha")])

    normalized = _selection.normalize_research_tool_selections(
        manager, 41, [_selection_for("tool.search", "alpha")]
    )
    assert normalized == (_selection_for("tool.search", "alpha"),)
    with pytest.raises(BlockContractError, match="research-tool-selection"):
        _selection.normalize_research_tool_selections(
            manager,
            41,
            [
                _selection_for(
                    "tool.search",
                    "alpha",
                    connection_mode="connectionless",
                    connection_id="must-not-be-kept",
                )
            ],
        )


def test_selection_and_declaration_roundtrip_stays_canonical():
    manager = _Manager(
        [_tool("tool.search", "search"), _tool("tool.lookup", "lookup")],
        [_provider("alpha"), _provider("beta")],
    )

    frozen = _selection.freeze_research_selections(
        manager,
        41,
        [
            _selection_for("tool.search", "alpha"),
            _selection_for("tool.lookup", "beta"),
        ],
    )

    assert frozen["research_tool_selections"] == (
        _selection_for("tool.search", "alpha"),
        _selection_for("tool.lookup", "beta"),
    )
    assert [item["tool_name"] for item in frozen["tool_declarations"]] == [
        "search",
        "lookup",
    ]
    assert [item["provider_id"] for item in frozen["tool_declarations"]] == [
        "alpha",
        "beta",
    ]
