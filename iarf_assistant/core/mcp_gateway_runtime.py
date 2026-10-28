"""Lifecycle adapter for Carlina's loopback MCP ASGI listener."""

from __future__ import annotations

import ipaddress
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from carlina.runtime_composition import RuntimeComposition
from carlina.runtime_profiles import RuntimeProfile

if TYPE_CHECKING:
    from flask import Flask

MCP_ENABLED_ENV: Final = "CARLINA_MCP_ENABLED"
MCP_HOST_ENV: Final = "CARLINA_MCP_HOST"
MCP_PORT_ENV: Final = "CARLINA_MCP_PORT"
_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final = frozenset({"", "0", "false", "no", "off"})


class McpGatewayRuntimeError(RuntimeError):
    """Raised when the opt-in MCP listener cannot start safely."""


class McpGatewayStatus(TypedDict):
    """Safe listener state for the local Documents connection API."""

    enabled: bool
    ready: bool
    url: str | None
    host: str | None
    port: int | None


def _enabled_from_environment(app: Flask) -> bool:
    raw_value = os.environ.get(MCP_ENABLED_ENV)
    if raw_value is None:
        composition = app.extensions.get("carlina_runtime_composition")
        return (
            isinstance(composition, RuntimeComposition)
            and composition.profile is RuntimeProfile.DOCUMENTS
        )

    raw = raw_value.strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise McpGatewayRuntimeError(
        f"{MCP_ENABLED_ENV} must be one of 1/true/yes/on or 0/false/no/off."
    )


def normalize_loopback_host(value: str) -> str:
    """Return a canonical loopback bind host or fail closed."""

    host = str(value or "").strip().lower()
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise McpGatewayRuntimeError(
            "The MCP listener must bind to localhost or a numeric loopback address."
        ) from exc
    if not address.is_loopback:
        raise McpGatewayRuntimeError(
            "The MCP listener is loopback-only; remote binding is not supported."
        )
    return str(address)


def _configured_port(main_port: int) -> int:
    raw = os.environ.get(MCP_PORT_ENV, "").strip()
    try:
        port = int(raw) if raw else int(main_port) + 1
    except ValueError as exc:
        raise McpGatewayRuntimeError(f"{MCP_PORT_ENV} must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise McpGatewayRuntimeError(f"{MCP_PORT_ENV} must be between 1 and 65535.")
    if port == int(main_port):
        raise McpGatewayRuntimeError(
            "The MCP listener requires a port distinct from the Flask listener."
        )
    return port


@dataclass(slots=True)
class McpGatewayHandle:
    """Own one Uvicorn server thread and its bounded shutdown."""

    host: str
    port: int
    server: object
    thread: threading.Thread

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        setattr(self.server, "should_exit", True)
        self.thread.join(timeout=max(0.1, float(timeout_seconds)))
        if self.thread.is_alive():
            setattr(self.server, "force_exit", True)
            self.thread.join(timeout=2.0)


def start_mcp_gateway(
    app: Flask,
    *,
    host: str,
    port: int,
    startup_timeout_seconds: float = 10.0,
) -> McpGatewayHandle:
    """Start the SDK-owned ASGI application on a verified loopback address."""

    import uvicorn

    from iarf_assistant.core.mcp_gateway import create_mcp_application

    bind_host = normalize_loopback_host(host)
    bind_port = int(port)
    if not 1 <= bind_port <= 65535:
        raise McpGatewayRuntimeError("The MCP port must be between 1 and 65535.")

    asgi_app = create_mcp_application(app, host=bind_host, port=bind_port)
    config = uvicorn.Config(
        asgi_app,
        host=bind_host,
        port=bind_port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        name="carlina-mcp-gateway",
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + max(0.1, float(startup_timeout_seconds))
    while thread.is_alive() and not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise McpGatewayRuntimeError(
            f"The MCP listener did not start on http://{bind_host}:{bind_port}/mcp."
        )
    handle = McpGatewayHandle(
        host=bind_host,
        port=bind_port,
        server=server,
        thread=thread,
    )
    app.extensions["carlina_mcp_gateway"] = handle
    return handle


def start_configured_mcp_gateway(
    app: Flask,
    *,
    main_port: int,
) -> McpGatewayHandle | None:
    """Start the listener when explicitly enabled or by Documents default."""

    if not _enabled_from_environment(app):
        app.extensions["carlina_mcp_gateway"] = None
        return None
    host = normalize_loopback_host(os.environ.get(MCP_HOST_ENV, "127.0.0.1"))
    port = _configured_port(main_port)
    return start_mcp_gateway(app, host=host, port=port)


def gateway_status(app: Flask) -> McpGatewayStatus:
    """Describe the listener from its live handle, never configuration alone."""

    handle = app.extensions.get("carlina_mcp_gateway")
    if not isinstance(handle, McpGatewayHandle):
        return {
            "enabled": False,
            "ready": False,
            "url": None,
            "host": None,
            "port": None,
        }

    host = normalize_loopback_host(handle.host)
    authority = f"[{host}]" if ":" in host else host
    ready = handle.thread.is_alive() and bool(getattr(handle.server, "started", False))
    return {
        "enabled": True,
        "ready": ready,
        "url": f"http://{authority}:{handle.port}/mcp",
        "host": host,
        "port": handle.port,
    }


__all__ = [
    "MCP_ENABLED_ENV",
    "MCP_HOST_ENV",
    "MCP_PORT_ENV",
    "McpGatewayHandle",
    "McpGatewayRuntimeError",
    "McpGatewayStatus",
    "gateway_status",
    "normalize_loopback_host",
    "start_configured_mcp_gateway",
    "start_mcp_gateway",
]
