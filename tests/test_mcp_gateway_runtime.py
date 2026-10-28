from __future__ import annotations

import socket
import sys
from threading import Thread
from types import ModuleType, SimpleNamespace

import pytest
from flask import Flask

from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.core import mcp_gateway_runtime
from iarf_assistant.core.mcp_gateway_runtime import (
    McpGatewayHandle,
    McpGatewayRuntimeError,
    gateway_status,
    normalize_loopback_host,
    start_configured_mcp_gateway,
    start_mcp_gateway,
)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("127.42.0.7", "127.42.0.7"),
        ("::1", "::1"),
    ],
)
def test_normalize_loopback_host_accepts_only_loopback(
    value: str,
    expected: str,
) -> None:
    assert normalize_loopback_host(value) == expected


@pytest.mark.parametrize("value", ["0.0.0.0", "192.0.2.1", "example.com", ""])
def test_normalize_loopback_host_rejects_remote_or_ambiguous_values(
    value: str,
) -> None:
    with pytest.raises(McpGatewayRuntimeError):
        normalize_loopback_host(value)


def test_configured_gateway_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CARLINA_MCP_ENABLED", raising=False)
    application = Flask("test")

    assert start_configured_mcp_gateway(application, main_port=5003) is None
    assert application.extensions["carlina_mcp_gateway"] is None


def test_documents_profile_enables_gateway_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CARLINA_MCP_ENABLED", raising=False)
    application = Flask("documents")
    application.extensions["carlina_runtime_composition"] = (
        resolve_runtime_composition(RuntimeProfile.DOCUMENTS, environment={})
    )
    started: list[tuple[Flask, str, int]] = []
    handle = McpGatewayHandle(
        host="127.0.0.1",
        port=5006,
        server=SimpleNamespace(started=True),
        thread=Thread(),
    )

    def start(app: Flask, *, host: str, port: int) -> McpGatewayHandle:
        started.append((app, host, port))
        return handle

    monkeypatch.setattr(mcp_gateway_runtime, "start_mcp_gateway", start)

    assert start_configured_mcp_gateway(application, main_port=5005) is handle
    assert started == [(application, "127.0.0.1", 5006)]


def test_workbench_and_invalid_compositions_default_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("CARLINA_MCP_ENABLED", raising=False)
    workbench = Flask("workbench")
    workbench.extensions["carlina_runtime_composition"] = (
        resolve_runtime_composition(RuntimeProfile.WORKBENCH, environment={})
    )
    invalid = Flask("invalid")
    invalid.extensions["carlina_runtime_composition"] = object()

    assert start_configured_mcp_gateway(workbench, main_port=5003) is None
    assert start_configured_mcp_gateway(invalid, main_port=5003) is None
    assert workbench.extensions["carlina_mcp_gateway"] is None
    assert invalid.extensions["carlina_mcp_gateway"] is None


@pytest.mark.parametrize(
    ("profile", "override", "enabled"),
    [
        (RuntimeProfile.DOCUMENTS, "false", False),
        (RuntimeProfile.WORKBENCH, "true", True),
    ],
)
def test_explicit_enable_value_overrides_profile_default(
    monkeypatch, profile: RuntimeProfile, override: str, enabled: bool
) -> None:
    monkeypatch.setenv("CARLINA_MCP_ENABLED", override)
    application = Flask(profile.value)
    application.extensions["carlina_runtime_composition"] = (
        resolve_runtime_composition(profile, environment={})
    )
    calls: list[int] = []
    handle = McpGatewayHandle(
        host="127.0.0.1",
        port=5004,
        server=SimpleNamespace(started=True),
        thread=Thread(),
    )

    def start(app: Flask, *, host: str, port: int) -> McpGatewayHandle:
        calls.append(port)
        app.extensions["carlina_mcp_gateway"] = handle
        return handle

    monkeypatch.setattr(mcp_gateway_runtime, "start_mcp_gateway", start)

    result = start_configured_mcp_gateway(application, main_port=5003)

    assert (result is handle) is enabled
    assert calls == ([5004] if enabled else [])
    assert application.extensions["carlina_mcp_gateway"] is (
        handle if enabled else None
    )


def test_configured_gateway_rejects_invalid_enable_value(monkeypatch) -> None:
    monkeypatch.setenv("CARLINA_MCP_ENABLED", "possibly")
    with pytest.raises(McpGatewayRuntimeError, match="CARLINA_MCP_ENABLED"):
        start_configured_mcp_gateway(Flask("test"), main_port=5003)


def test_configured_gateway_rejects_port_collision(monkeypatch) -> None:
    monkeypatch.setenv("CARLINA_MCP_ENABLED", "true")
    monkeypatch.setenv("CARLINA_MCP_PORT", "5003")

    with pytest.raises(McpGatewayRuntimeError, match="distinct"):
        start_configured_mcp_gateway(Flask("test"), main_port=5003)


def test_failed_gateway_start_does_not_store_a_handle(monkeypatch) -> None:
    class UnstartedServer:
        started = False
        should_exit = False
        force_exit = False

        def __init__(self, _config) -> None:
            pass

        def run(self) -> None:
            return None

    uvicorn = ModuleType("uvicorn")
    uvicorn.Config = lambda *_args, **_kwargs: object()
    uvicorn.Server = UnstartedServer
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setattr(
        "iarf_assistant.core.mcp_gateway.create_mcp_application",
        lambda *_args, **_kwargs: object(),
    )
    application = Flask("failed-start")

    with pytest.raises(McpGatewayRuntimeError, match="did not start"):
        start_mcp_gateway(
            application,
            host="127.0.0.1",
            port=_unused_loopback_port(),
            startup_timeout_seconds=0.1,
        )

    assert "carlina_mcp_gateway" not in application.extensions


def test_real_gateway_starts_and_stops_on_loopback() -> None:
    application = Flask("test")
    handle = start_mcp_gateway(
        application,
        host="127.0.0.1",
        port=_unused_loopback_port(),
    )
    try:
        assert handle.thread.is_alive()
        assert bool(getattr(handle.server, "started"))
        assert application.extensions["carlina_mcp_gateway"] is handle
        assert gateway_status(application) == {
            "enabled": True,
            "ready": True,
            "url": f"http://127.0.0.1:{handle.port}/mcp",
            "host": "127.0.0.1",
            "port": handle.port,
        }
    finally:
        handle.stop()
    assert not handle.thread.is_alive()
    assert gateway_status(application)["ready"] is False


def test_gateway_status_brackets_ipv6_url() -> None:
    application = Flask("ipv6-status")
    application.extensions["carlina_mcp_gateway"] = McpGatewayHandle(
        host="::1",
        port=5060,
        server=SimpleNamespace(started=True),
        thread=SimpleNamespace(is_alive=lambda: True),
    )

    assert gateway_status(application) == {
        "enabled": True,
        "ready": True,
        "url": "http://[::1]:5060/mcp",
        "host": "::1",
        "port": 5060,
    }
